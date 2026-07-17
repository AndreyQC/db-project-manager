"""Tests for db_project_manager.infrastructure.config.connection_store."""

from __future__ import annotations

import pytest

from db_project_manager.domain.connection import ConnectionConfig
from db_project_manager.infrastructure.config.connection_store import (
    ConnectionStore,
    ConnectionStoreError,
)
from db_project_manager.infrastructure.crypto.crypto_util import _is_cipher_token


def _make_cfg(**kwargs) -> ConnectionConfig:
    base = {
        "host": "localhost",
        "port": 5432,
        "database": "mydb",
        "username": "myuser",
        "password": "plaintext-secret",
        "type": "postgres",
    }
    base.update(kwargs)
    return ConnectionConfig(**base)


def test_save_encrypts_password(crypto_env: str, tmp_path) -> None:
    store = ConnectionStore(tmp_path)
    cfg = _make_cfg()

    path = store.save(cfg, name="prod", crypto_env=crypto_env)

    assert path.exists()
    # On disk the password must be a crypto token, not plaintext.
    text = path.read_text(encoding="utf-8")
    assert "plaintext-secret" not in text
    assert "crypto__" in text


def test_save_load_roundtrip(crypto_env: str, tmp_path) -> None:
    store = ConnectionStore(tmp_path)
    cfg = _make_cfg()

    store.save(cfg, name="prod", crypto_env=crypto_env)
    loaded = store.load_by_name("prod")

    assert loaded.host == "localhost"
    assert loaded.port == 5432
    assert loaded.database == "mydb"
    assert loaded.username == "myuser"
    # Password is decrypted back to plaintext.
    assert loaded.password == "plaintext-secret"
    assert loaded.name == "prod"


def test_save_keeps_existing_token(crypto_env: str, tmp_path) -> None:
    """If password is already a crypto token, it is not re-encrypted."""
    from db_project_manager.infrastructure.crypto.crypto_util import get_encrypted_text

    token = get_encrypted_text("pw", crypto_env)
    cfg = _make_cfg(password=token)
    store = ConnectionStore(tmp_path)

    path = store.save(cfg, name="prod", crypto_env=crypto_env)
    text = path.read_text(encoding="utf-8")
    # The same token is preserved.
    assert token in text


def test_load_by_name_decrypts(crypto_env: str, tmp_path) -> None:
    store = ConnectionStore(tmp_path)
    store.save(_make_cfg(password="hunter2"), name="ci", crypto_env=crypto_env)
    loaded = store.load_by_name("ci")
    assert loaded.password == "hunter2"


def test_load_missing_file_raises(tmp_path) -> None:
    store = ConnectionStore(tmp_path)
    with pytest.raises(ConnectionStoreError, match="не найден"):
        store.load_by_name("nope")


def test_invalid_name_rejected(tmp_path) -> None:
    store = ConnectionStore(tmp_path)
    with pytest.raises(ConnectionStoreError, match="Недопустимое имя"):
        store.path_for("../escape")


def test_list_names(crypto_env: str, tmp_path) -> None:
    store = ConnectionStore(tmp_path)
    assert store.list_names() == []
    store.save(_make_cfg(), name="a", crypto_env=crypto_env)
    store.save(_make_cfg(), name="b", crypto_env=crypto_env)
    assert store.list_names() == ["a", "b"]


def test_delete(crypto_env: str, tmp_path) -> None:
    store = ConnectionStore(tmp_path)
    store.save(_make_cfg(), name="tmp", crypto_env=crypto_env)
    assert store.path_for("tmp").exists()
    store.delete("tmp")
    assert not store.path_for("tmp").exists()
    # Deleting again is a no-op.
    store.delete("tmp")


def test_token_is_cipher_format() -> None:
    assert _is_cipher_token("crypto__ENV__payload") is True
    assert _is_cipher_token("plain") is False
