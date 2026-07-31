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
    find_calls,
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


# --- find_calls (P8.S5) ---


class TestFindCalls:
    def test_bare_call(self) -> None:
        body = "SELECT sp_x(123);"
        assert find_calls(body, "app", "sp_x") == ["123"]

    def test_qualified_call(self) -> None:
        body = "SELECT app.sp_x(123);"
        assert find_calls(body, "app", "sp_x") == ["123"]

    def test_qualified_with_spaces(self) -> None:
        # Whitespace around the dot is unusual but legal.
        body = "SELECT app . sp_x ( 123 );"
        assert find_calls(body, "app", "sp_x") == [" 123 "]

    def test_multiple_calls_in_one_body(self) -> None:
        body = "SELECT app.sp_x(123), app.sp_x('x');"
        assert find_calls(body, "app", "sp_x") == ["123", "'x'"]

    def test_no_calls_returns_empty(self) -> None:
        body = "SELECT 1;"
        assert find_calls(body, "app", "sp_x") == []

    def test_bare_name_not_matched_as_suffix(self) -> None:
        # my_sp_x( must NOT match sp_x( — the lookbehind prevents it.
        body = "SELECT my_sp_x(123);"
        assert find_calls(body, "app", "sp_x") == []

    def test_qualified_name_not_matched_as_suffix(self) -> None:
        # other.sp_x with a different schema must NOT match when we look for app.
        body = "SELECT other.sp_x(123);"
        # Bare form is also tried, so this would match 'sp_x(' unless the
        # lookbehind excludes the dot. The dot precedes sp_x -> excluded.
        assert find_calls(body, "app", "sp_x") == []

    def test_call_with_nested_parens_one_level(self) -> None:
        body = "SELECT app.sp_x(sp_y(1));"
        # Body is the whole "sp_y(1)" including the inner parens.
        assert find_calls(body, "app", "sp_x") == ["sp_y(1)"]

    def test_call_with_string_argument_containing_paren(self) -> None:
        # A paren inside a string literal must not confuse paren balancing.
        body = "SELECT app.sp_x('a)b');"
        assert find_calls(body, "app", "sp_x") == ["'a)b'"]

    def test_call_inside_dollar_quoted_body_is_scanned(self) -> None:
        # The function body delimited by $$ is executable code — calls in it
        # MUST be found.
        body = "CREATE FUNCTION f() RETURNS void AS $$ BEGIN PERFORM app.sp_x(7); END; $$;"
        assert find_calls(body, "app", "sp_x") == ["7"]

    def test_name_inside_string_literal_not_matched(self) -> None:
        # 'app.sp_x(' inside an ordinary string literal is data, not a call.
        body = "SELECT 'app.sp_x(123)'::text;"
        assert find_calls(body, "app", "sp_x") == []

    def test_name_inside_line_comment_not_matched(self) -> None:
        body = "-- app.sp_x(123)\nSELECT 1;"
        assert find_calls(body, "app", "sp_x") == []

    def test_name_inside_block_comment_not_matched(self) -> None:
        body = "/* app.sp_x(123) */ SELECT 1;"
        assert find_calls(body, "app", "sp_x") == []

    def test_zero_arg_call_yields_empty_body(self) -> None:
        body = "SELECT app.sp_x();"
        assert find_calls(body, "app", "sp_x") == [""]

    def test_no_schema_matches_bare_only(self) -> None:
        # schema=None -> only the bare name pattern is used.
        body = "SELECT sp_x(123);"
        assert find_calls(body, None, "sp_x") == ["123"]

    def test_function_definition_not_matched_as_call(self) -> None:
        # 'CREATE FUNCTION app.sp_x(a int4)' is a DEFINITION, not a call — the
        # parens hold parameter declarations. It must be excluded so overload
        # resolution does not infer a bogus signature from 'a int4'.
        body = "CREATE OR REPLACE FUNCTION app.sp_x(a int4) RETURNS int4 AS $$ BEGIN NULL; END; $$;"
        assert find_calls(body, "app", "sp_x") == []

    def test_procedure_definition_not_matched_as_call(self) -> None:
        body = "CREATE PROCEDURE app.sp_x(a int4) LANGUAGE plpgsql AS $$ BEGIN NULL; END; $$;"
        assert find_calls(body, "app", "sp_x") == []

    def test_definition_excluded_but_real_call_kept(self) -> None:
        # A definition and a real call in the same body: only the call survives.
        body = (
            "CREATE FUNCTION app.sp_x(a int4) RETURNS int4 AS $$ "
            "BEGIN PERFORM app.sp_x(7); END; $$;"
        )
        assert find_calls(body, "app", "sp_x") == ["7"]
