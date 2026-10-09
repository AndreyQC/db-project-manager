"""Tests for db_project_manager.infrastructure.crypto.crypto_util."""

from __future__ import annotations

import pytest

from db_project_manager.infrastructure.crypto.crypto_util import (
    format_cipher_token,
    generate_fernet_key,
    get_cipher_env,
    get_decrypted_nested_dict,
    get_decrypted_text,
    get_encrypted_text,
    _is_cipher_token,
)

from tests.conftest import TEST_CRYPTO_ENV


# --- roundtrip ---


def test_encrypt_decrypt_roundtrip(crypto_env: str) -> None:
    plaintext = "s3cret-p@ssword"
    token = get_encrypted_text(plaintext, crypto_env)

    assert token.startswith(f"crypto__{crypto_env}__")
    assert get_decrypted_text(token) == plaintext


def test_format_cipher_token() -> None:
    assert format_cipher_token("MY_ENV", "abc123") == "crypto__MY_ENV__abc123"


def test_is_cipher_token() -> None:
    assert _is_cipher_token("crypto__ENV__payload") is True
    assert _is_cipher_token("plain-password") is False
    assert _is_cipher_token("crypto__only_one_part") is False


def test_get_cipher_env_extracts_env_name(crypto_env: str) -> None:
    token = get_encrypted_text("s3cret", crypto_env)
    assert get_cipher_env(token) == crypto_env


def test_get_cipher_env_non_token_returns_none() -> None:
    assert get_cipher_env("plain-password") is None
    assert get_cipher_env("crypto__only_one_part") is None
    assert get_cipher_env("") is None


# --- nested dict ---


def test_decrypt_nested_dict(crypto_env: str) -> None:
    plain_pw = "hunter2"
    token = get_encrypted_text(plain_pw, crypto_env)

    data = {
        "host": "localhost",
        "password": token,
        "nested": {"deep": token, "plain": "keep-me"},
        "list_field": [token, "untouched"],
    }
    result = get_decrypted_nested_dict(data)

    assert result["host"] == "localhost"
    assert result["password"] == plain_pw
    assert result["nested"]["deep"] == plain_pw
    assert result["nested"]["plain"] == "keep-me"
    assert result["list_field"][0] == plain_pw
    assert result["list_field"][1] == "untouched"


def test_decrypt_nested_dict_non_string_left_as_is(crypto_env: str) -> None:
    data = {"port": 5432, "flag": True, "none_val": None}
    result = get_decrypted_nested_dict(data)
    assert result == data


# --- error / fallback paths ---


def test_decrypt_without_env_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # Build a token referencing an env var that is not set.
    token = "crypto__MISSING_ENV_VAR__gAAAAABmfake"
    monkeypatch.delenv("MISSING_ENV_VAR", raising=False)
    with pytest.raises(KeyError, match="MISSING_ENV_VAR"):
        get_decrypted_text(token)


def test_decrypt_non_token_string_returned_as_is() -> None:
    # A string that is not in crypto__ format must be returned unchanged.
    assert get_decrypted_text("just-a-plain-password") == "just-a-plain-password"


def test_decrypt_bad_token_returns_original(crypto_env: str) -> None:
    # Correct prefix/format but invalid ciphertext: nested helper keeps the original.
    bad = format_cipher_token(crypto_env, "not-a-valid-fernet-token")
    result = get_decrypted_nested_dict({"password": bad})
    assert result["password"] == bad


def test_generate_fernet_key_is_usable(crypto_env: str, monkeypatch: pytest.MonkeyPatch) -> None:
    key = generate_fernet_key()
    monkeypatch.setenv(TEST_CRYPTO_ENV, key)
    token = get_encrypted_text("hello", TEST_CRYPTO_ENV)
    assert get_decrypted_text(token) == "hello"


# --- regression: "__" внутри Fernet-ciphertextа (base64url) ---


def test_cipher_token_with_double_underscore_in_ciphertext() -> None:
    """Fernet-токен может содержать '__' — хвост после ENV обязан остаться целым.

    Живой случай из флейка test_encrypt_decrypt_roundtrip: ciphertext
    '...pRhY__RRl6T9...' разваливался split('__') без лимита.
    """
    token = "crypto__DBPM_TEST_CRYPTO_KEY__gAAAAABqxMuygkzBOZmuM8l-4Ah4KG0iM-Ish1TgPkx3N3_XmnwcizZW19pRhY__RRl6T9j4Xz7UVwOjZ4xEA_ezmdsDcV0hKw=="
    assert _is_cipher_token(token) is True
    assert get_cipher_env(token) == "DBPM_TEST_CRYPTO_KEY"


def test_encrypt_decrypt_roundtrip_survives_underscore_rich_token(crypto_env: str) -> None:
    """Многократный roundtrip: вероятность '__' внутри токена ~2% на штуку."""
    for i in range(40):
        token = get_encrypted_text(f"secret-{i}", crypto_env)
        assert get_decrypted_text(token) == f"secret-{i}"


def test_missing_env_raises_crypto_key_missing(monkeypatch) -> None:
    """Нет env-ключа → CryptoKeyMissing с именем переменной (наследник KeyError)."""
    from db_project_manager.infrastructure.crypto.crypto_util import CryptoKeyMissing

    monkeypatch.delenv(TEST_CRYPTO_ENV, raising=False)
    token = format_cipher_token(TEST_CRYPTO_ENV, "gAAAAABfake-payload")
    with pytest.raises(CryptoKeyMissing) as exc_info:
        get_decrypted_text(token)
    assert exc_info.value.env_var == TEST_CRYPTO_ENV
