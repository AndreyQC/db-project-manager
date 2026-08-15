"""Tests for db_project_manager.domain.deploy (Phase 10, step S1).

Pure-domain coverage: calver validation/seeding, ``ScriptRecord`` round-trip,
``canonical_normalize`` and ``script_checksum``. No DB, no filesystem. The
intended-use case for ``strip_autodoc`` (infrastructure) + ``script_checksum``
(domain) is exercised to lock in the CDF-6 contract.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from db_project_manager.domain.deploy import (
    CALVER_RE,
    ScriptRecord,
    calver_seed,
    canonical_normalize,
    script_checksum,
    validate_calver,
)
from db_project_manager.infrastructure.sql.autodoc import strip_autodoc


# ---------------------------------------------------------------- calver


@pytest.mark.parametrize(
    "value",
    [
        "2026.08.11.01",
        "2026.08.11.99",
        "2026.12.31.07",
        "2026.01.01.00",
        "2030.06.15.42",
    ],
)
def test_calver_valid(value: str) -> None:
    validate_calver(value)  # must not raise
    assert CALVER_RE.match(value)


@pytest.mark.parametrize(
    "value",
    [
        "2026.8.11.01",        # month not zero-padded
        "2026.13.01.01",       # month > 12
        "2026.00.01.01",       # month 0
        "2026.08.32.01",       # day > 31
        "2026.08.00.01",       # day 0
        "2026.08.11.1",        # release number not 2 digits
        "2026.08.11",          # missing release number
        "2026-08-11-01",       # wrong separator
        "2026.08.11.abc",      # non-numeric release
        "26.08.11.01",         # 2-digit year
        "",                    # empty
        "2026.08.11.001",      # 3-digit release (regex requires exactly 2)
    ],
)
def test_calver_invalid(value: str) -> None:
    with pytest.raises(ValueError, match="invalid calver"):
        validate_calver(value)


def test_calver_comparison_lexicographic_equals_chronological() -> None:
    # Fixed-width components → string compare equals date compare (CDF-9).
    assert "2026.08.11.01" < "2026.08.11.02"
    assert "2026.08.11.02" < "2026.08.11.99"
    assert "2026.08.11.99" < "2026.08.12.01"
    assert "2026.08.12.01" < "2026.09.01.01"
    assert "2026.12.31.99" < "2027.01.01.01"


def test_calver_seed_returns_first_release_of_day() -> None:
    now = datetime(2026, 8, 13, 14, 30, tzinfo=timezone.utc)
    assert calver_seed(now) == "2026.08.13.01"


def test_calver_seed_defaults_to_now() -> None:
    # No argument → uses current UTC; smoke check on format only.
    value = calver_seed()
    assert CALVER_RE.match(value)
    assert value.endswith(".01")


# ---------------------------------------------------------- ScriptRecord


def test_script_record_roundtrip() -> None:
    rec = ScriptRecord(
        script_name="2026-08-11_001_init.sql",
        script_type="pre",
        checksum="a" * 64,
        success=True,
        duration_ms=42,
    )
    # error_message defaults to None on success.
    assert rec.error_message is None
    # executed_at auto-populated.
    assert rec.executed_at is not None

    # Round-trip via JSON (pydantic v2) preserves all fields.
    parsed = ScriptRecord.model_validate_json(rec.model_dump_json())
    assert parsed.script_name == rec.script_name
    assert parsed.script_type == "pre"
    assert parsed.checksum == rec.checksum
    assert parsed.success is True
    assert parsed.duration_ms == 42


def test_script_record_accepts_failure_with_error_message() -> None:
    rec = ScriptRecord(
        script_name="bad.sql",
        script_type="post",
        checksum="b" * 64,
        success=False,
        error_message="syntax error at line 1",
        duration_ms=5,
    )
    assert rec.success is False
    assert rec.error_message == "syntax error at line 1"


def test_script_record_rejects_unknown_script_type() -> None:
    # Literal["pre", "post"] — anything else must fail validation.
    with pytest.raises(ValueError):
        ScriptRecord(
            script_name="x.sql",
            script_type="between",  # type: ignore[arg-type]
            checksum="c" * 64,
            success=True,
            duration_ms=1,
        )


# ---------------------------------------------------- canonical_normalize


def test_canonical_normalize_strips_trailing_whitespace() -> None:
    assert canonical_normalize("SELECT 1;   \nFROM t;\t") == "SELECT 1;\nFROM t;"


def test_canonical_normalize_normalizes_line_endings() -> None:
    assert canonical_normalize("SELECT 1;\r\nFROM t;\r\n") == "SELECT 1;\nFROM t;"


def test_canonical_normalize_drops_blank_lines() -> None:
    assert canonical_normalize("SELECT 1;\n\n\nFROM t;\n\n") == "SELECT 1;\nFROM t;"


def test_canonical_normalize_idempotent() -> None:
    once = canonical_normalize("SELECT 1;\n\nFROM t;   ")
    twice = canonical_normalize(once)
    assert once == twice


def test_canonical_normalize_preserves_sql_comments() -> None:
    # CDF-6: SQL comments are NOT stripped (may be semantic; AST would be needed).
    text = "-- important note\nSELECT 1;"
    assert canonical_normalize(text) == "-- important note\nSELECT 1;"


def test_canonical_normalize_does_not_strip_autodoc() -> None:
    # Contract: domain stays pure — autodoc stripping is the caller's job.
    text = "[<[autodoc-yaml]]\nobject: {...}\n[[autodoc-yaml]>]]\n\nSELECT 1;"
    normalized = canonical_normalize(text)
    # The autodoc markers survive — caller must strip them via strip_autodoc.
    assert "[<[autodoc-yaml]]" in normalized
    assert "SELECT 1;" in normalized


# --------------------------------------------------------- script_checksum


def test_script_checksum_deterministic() -> None:
    text = "SELECT 1;\nFROM t;"
    assert script_checksum(text) == script_checksum(text)


def test_script_checksum_stable_under_whitespace_only_diff() -> None:
    # Trailing whitespace and line-ending differences do not change checksum.
    a = script_checksum("SELECT 1;\nFROM t;")
    b = script_checksum("SELECT 1;   \r\nFROM t;\r\n")
    c = script_checksum("SELECT 1;\n\n\nFROM t;")
    assert a == b == c


def test_script_checksum_returns_64_char_hex() -> None:
    checksum = script_checksum("SELECT 1;")
    assert len(checksum) == 64
    assert all(c in "0123456789abcdef" for c in checksum)


def test_script_checksum_sensitive_to_sql_change() -> None:
    assert script_checksum("SELECT 1;") != script_checksum("SELECT 2;")


def test_script_checksum_invariant_under_autodoc_change() -> None:
    """CDF-6 contract: caller strips autodoc → checksum reflects SQL only.

    Two renders of the same SQL with different autodoc metadata must produce
    the same checksum, because the caller (pre/post runner, canonical-DDL
    validator) strips the autodoc first.
    """
    sql_a = (
        "[<[autodoc-yaml]]\n"
        "object: {object_name: foo}\n"
        "[[autodoc-yaml]>]]\n"
        "*/\n"
        "CREATE TABLE foo (id int);"
    )
    sql_b = (
        "[<[autodoc-yaml]]\n"
        "object: {object_name: foo, note: changed-metadata-only}\n"
        "[[autodoc-yaml]>]]\n"
        "*/\n"
        "CREATE TABLE foo (id int);"
    )
    # Different raw text, but identical after strip_autodoc → same checksum.
    assert script_checksum(strip_autodoc(sql_a)) == script_checksum(strip_autodoc(sql_b))


def test_script_checksum_detects_real_sql_change_after_strip() -> None:
    sql_a = (
        "[<[autodoc-yaml]]\nobject: {}\n[[autodoc-yaml]>]]\n*/\n"
        "CREATE TABLE foo (id int);"
    )
    sql_b = (
        "[<[autodoc-yaml]]\nobject: {}\n[[autodoc-yaml]>]]\n*/\n"
        "CREATE TABLE foo (id int, name text);"
    )
    assert script_checksum(strip_autodoc(sql_a)) != script_checksum(strip_autodoc(sql_b))
