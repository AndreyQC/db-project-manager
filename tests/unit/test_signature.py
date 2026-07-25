"""Tests for db_project_manager.domain.signature utilities.

Phase 4 — canonical signature + hash for overloaded function/procedure identity.
"""

from __future__ import annotations

import hashlib

from db_project_manager.domain.signature import canonical_signature, signature_hash


# --- canonical_signature ---


def test_canonical_empty_input_returns_empty() -> None:
    assert canonical_signature("") == ""
    assert canonical_signature("   ") == ""
    assert canonical_signature(None) == ""  # type: ignore[arg-type]


def test_canonical_single_type() -> None:
    assert canonical_signature("text") == "text"


def test_canonical_multiple_types_joined_no_spaces() -> None:
    assert canonical_signature("text, varchar, varchar, uuid") == "text,varchar,varchar,uuid"


def test_canonical_lowercased() -> None:
    assert canonical_signature("TEXT, UUID") == "text,uuid"


def test_canonical_strips_modifiers() -> None:
    # varchar(255), numeric(10,2), timestamp(6) — modifier stripped.
    assert canonical_signature("varchar(255)") == "varchar"
    assert canonical_signature("numeric(10,2)") == "numeric"
    assert canonical_signature("timestamp(6)") == "timestamp"


def test_canonical_modifiers_in_mixed_list() -> None:
    assert canonical_signature("int4, varchar(255), numeric(10,2)") == "int4,varchar,numeric"


def test_canonical_skips_empty_parts() -> None:
    assert canonical_signature("int4, , text") == "int4,text"


def test_canonical_whitespace_robust() -> None:
    assert canonical_signature("  int4  ,  text  ") == "int4,text"


# --- signature_hash ---


def test_hash_empty_input_returns_empty() -> None:
    assert signature_hash("") == ""
    assert signature_hash("   ") == ""


def test_hash_is_8_hex_chars() -> None:
    h = signature_hash("int4")
    assert len(h) == 8
    int(h, 16)  # parses as hex without raising


def test_hash_matches_sha256_first_8() -> None:
    canon = "int4"
    expected = hashlib.sha256(canon.encode("utf-8")).hexdigest()[:8]
    assert signature_hash("int4") == expected


def test_hash_distinct_for_distinct_types() -> None:
    assert signature_hash("int4") != signature_hash("int8")
    assert signature_hash("int4") != signature_hash("text")


def test_hash_stable_across_modifiers() -> None:
    """Critical: the same logical signature must hash identically regardless
    of whether the adapter reports it with or without type modifiers."""
    assert signature_hash("varchar(255)") == signature_hash("varchar")
    assert signature_hash("numeric(10,2)") == signature_hash("numeric")
    assert signature_hash("int4, varchar(255)") == signature_hash("int4, varchar")


def test_hash_order_sensitive() -> None:
    # Order of argument types matters for overloading identity.
    assert signature_hash("int4, text") != signature_hash("text, int4")


def test_hash_lowercase_canonical() -> None:
    # Case differences do not change identity.
    assert signature_hash("TEXT") == signature_hash("text")


def test_hash_realistic_overload_pair() -> None:
    """Simulate a real overload case: sp_admin_update_order_delivery with
    different argument type lists. Their hashes must differ — otherwise the
    file-name suffix would collide."""
    long_sig = "text,varchar,varchar,varchar,uuid,uuid"
    short_sig = "int4"
    assert signature_hash(long_sig) != signature_hash(short_sig)
    # And the full long signature from the original bug report still fits in 8 hex.
    h = signature_hash(long_sig)
    assert len(h) == 8
