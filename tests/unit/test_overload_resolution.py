"""Tests for db_project_manager.infrastructure.parsing.overload_resolution.

Phase 8 core: literal type inference + argument splitting + overload matching.
These are pure functions tested in isolation — no file or graph dependencies.
"""

from __future__ import annotations

import pytest

from db_project_manager.infrastructure.parsing.overload_resolution import (
    infer_call_signature,
    infer_literal_type,
    resolve_overload,
    split_call_args,
)


# --- infer_literal_type ---


class TestInferStringLiteral:
    def test_simple_string(self) -> None:
        assert infer_literal_type("'hello'") == "text"

    def test_string_with_embedded_quote_escape(self) -> None:
        # SQL '' is an embedded single quote, not a terminator.
        assert infer_literal_type("'it''s'") == "text"

    def test_string_with_comma(self) -> None:
        assert infer_literal_type("'a,b'") == "text"

    def test_empty_string_literal(self) -> None:
        assert infer_literal_type("''") == "text"


class TestInferDoubleQuoted:
    def test_double_quoted_is_identifier_not_text(self) -> None:
        # In PostgreSQL "x" is an identifier, not a string. We must NOT infer
        # text — this would misroute a column/identifier call to a text overload.
        assert infer_literal_type('"column_name"') is None


class TestInferBool:
    @pytest.mark.parametrize("lit", ["true", "false"])
    def test_lowercase(self, lit: str) -> None:
        assert infer_literal_type(lit) == "bool"

    @pytest.mark.parametrize("lit", ["TRUE", "False", "FALSE"])
    def test_case_insensitive(self, lit: str) -> None:
        assert infer_literal_type(lit) == "bool"


class TestInferNull:
    @pytest.mark.parametrize("lit", ["null", "NULL", "Null"])
    def test_null_carries_no_type(self, lit: str) -> None:
        assert infer_literal_type(lit) is None


class TestInferInt:
    @pytest.mark.parametrize("lit", ["0", "1", "123", "456789"])
    def test_positive(self, lit: str) -> None:
        assert infer_literal_type(lit) == "int4"

    @pytest.mark.parametrize("lit", ["-1", "-123"])
    def test_negative(self, lit: str) -> None:
        assert infer_literal_type(lit) == "int4"

    @pytest.mark.parametrize("lit", ["+7"])
    def test_explicit_plus(self, lit: str) -> None:
        assert infer_literal_type(lit) == "int4"


class TestInferFloat:
    """Float literals resolve to numeric/float8 in PG, but the exact type is
    context-dependent — we refuse to infer (return None)."""

    @pytest.mark.parametrize("lit", ["1.5", "-0.5", "1.", ".5", "1e3", "1.5e-3", "-2.0E2"])
    def test_float_is_unknown(self, lit: str) -> None:
        assert infer_literal_type(lit) is None


class TestInferNonLiteral:
    @pytest.mark.parametrize(
        "lit",
        [
            "user_id",          # bare column reference
            "t.col",            # qualified column reference
            "sp_y(x)",          # nested function call
            "1 + 1",            # arithmetic
            "x::int4",          # explicit cast
            "ARRAY[1]",         # constructor
            "NULL::text",       # cast of null
            "",                 # empty
            "  ",               # whitespace only
        ],
    )
    def test_non_inferrable(self, lit: str) -> None:
        assert infer_literal_type(lit) is None


# --- split_call_args ---


class TestSplitCallArgs:
    def test_empty_body_is_zero_args(self) -> None:
        assert split_call_args("") == []

    def test_single_arg(self) -> None:
        assert split_call_args("123") == ["123"]

    def test_multiple_args(self) -> None:
        assert split_call_args("1, 2, 3") == ["1", "2", "3"]

    def test_whitespace_trimmed(self) -> None:
        assert split_call_args("  1  ,   2  ") == ["1", "2"]

    def test_nested_parens_do_not_split(self) -> None:
        # f(g(x, y), z) -> ["g(x, y)", "z"]
        assert split_call_args("g(x, y), z") == ["g(x, y)", "z"]

    def test_string_with_comma_does_not_split(self) -> None:
        # 'a,b' contains a comma but it is inside a string literal.
        assert split_call_args("'a,b', 2") == ["'a,b'", "2"]

    def test_string_with_escaped_quote(self) -> None:
        # 'it''s' has an embedded quote; the comma after is a real separator.
        assert split_call_args("'it''s', 2") == ["'it''s'", "2"]

    def test_multiple_nested_levels(self) -> None:
        assert split_call_args("g(h(x, y), z), w") == ["g(h(x, y), z)", "w"]


# --- infer_call_signature ---


class TestInferCallSignature:
    def test_all_literals(self) -> None:
        assert infer_call_signature(["123", "'x'"]) == ("int4", "text")

    def test_single_bool(self) -> None:
        assert infer_call_signature(["true"]) == ("bool",)

    def test_zero_args(self) -> None:
        assert infer_call_signature([]) == ()

    def test_one_unknown_makes_whole_call_unknown(self) -> None:
        # A column reference among literals -> the whole signature is unknowable.
        assert infer_call_signature(["123", "user_id"]) is None

    def test_float_makes_whole_call_unknown(self) -> None:
        assert infer_call_signature(["1.5", "123"]) is None


# --- resolve_overload ---


class TestResolveOverload:
    OVERLOADS = [
        ("key_int", ("int4",)),
        ("key_text", ("text",)),
    ]

    def test_unique_match_returns_key(self) -> None:
        assert resolve_overload(("int4",), self.OVERLOADS) == "key_int"
        assert resolve_overload(("text",), self.OVERLOADS) == "key_text"

    def test_unknown_call_signature_is_unresolved(self) -> None:
        assert resolve_overload(None, self.OVERLOADS) is None

    def test_zero_matches_is_unresolved(self) -> None:
        # No overload takes a single bool.
        assert resolve_overload(("bool",), self.OVERLOADS) is None

    def test_ambiguous_int_width_is_unresolved(self) -> None:
        """If overloads differ only in integer width (int2/int8, no int4) and
        the call is an integer literal (inferred int4), there is no match —
        PG would pick int4, which is not offered, so we conservatively skip."""
        overloads = [
            ("key_int2", ("int2",)),
            ("key_int8", ("int8",)),
        ]
        assert resolve_overload(("int4",), overloads) is None

    def test_two_arg_signature_matches(self) -> None:
        overloads = [
            ("key_int_text", ("int4", "text")),
            ("key_int", ("int4",)),
        ]
        assert resolve_overload(("int4", "text"), overloads) == "key_int_text"

    def test_no_argument_call_against_argful_overloads_unresolved(self) -> None:
        # A zero-arg call () cannot match any overload that requires arguments.
        assert resolve_overload((), self.OVERLOADS) is None

    def test_empty_overloads_unresolved(self) -> None:
        assert resolve_overload(("int4",), []) is None
