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


# --- crypto_env_for: raw-file key variable lookup (для диалога редактирования) ---


def test_crypto_env_for_returns_password_token_env(crypto_env: str, tmp_path) -> None:
    store = ConnectionStore(tmp_path)
    store.save(_make_cfg(), name="prod", crypto_env=crypto_env)
    assert store.crypto_env_for("prod") == crypto_env


def test_crypto_env_for_plain_password_returns_none(tmp_path) -> None:
    store = ConnectionStore(tmp_path)
    (tmp_path / "plain.yaml").write_text(
        "host: localhost\npassword: plain-secret\n", encoding="utf-8"
    )
    assert store.crypto_env_for("plain") is None


def test_crypto_env_for_missing_file_returns_none(tmp_path) -> None:
    store = ConnectionStore(tmp_path)
    assert store.crypto_env_for("nope") is None


def test_crypto_env_for_falls_back_to_ssh_token(crypto_env: str, tmp_path) -> None:
    """Plain DB password but an encrypted SSH field: the key var is still found."""
    from db_project_manager.infrastructure.crypto.crypto_util import get_encrypted_text

    store = ConnectionStore(tmp_path)
    (tmp_path / "mixed.yaml").write_text(
        "host: localhost\n"
        "password: plain-secret\n"
        "ssh_tunnel:\n"
        f"  ssh_pass: {get_encrypted_text('ssh-pw', crypto_env)}\n",
        encoding="utf-8",
    )
    assert store.crypto_env_for("mixed") == crypto_env


# --- Phase 18: allow_drop_schemas flag round-trip ---


def test_allow_drop_schemas_roundtrip_true(crypto_env: str, tmp_path) -> None:
    store = ConnectionStore(tmp_path)
    cfg = _make_cfg(allow_drop_schemas=True)

    path = store.save(cfg, name="dev", crypto_env=crypto_env)

    text = path.read_text(encoding="utf-8")
    assert "allow_drop_schemas: true" in text
    loaded = store.load_by_name("dev")
    assert loaded.allow_drop_schemas is True


def test_allow_drop_schemas_defaults_false_when_absent(
    crypto_env: str, tmp_path
) -> None:
    """Old YAML files without the field must parse as False (backwards compat)."""
    store = ConnectionStore(tmp_path)
    cfg = _make_cfg()
    path = store.save(cfg, name="legacy", crypto_env=crypto_env)

    # Strip the flag to simulate a pre-Phase-18 connection file.
    lines = [
        line for line in path.read_text(encoding="utf-8").splitlines()
        if not line.startswith("allow_drop_schemas")
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    loaded = store.load_by_name("legacy")
    assert loaded.allow_drop_schemas is False


def test_allow_drop_schemas_not_in_connect_options(crypto_env: str, tmp_path) -> None:
    """The flag is a top-level field, never inside ``options`` (which psycopg
    receives as connect_args — a tool flag there would break connections)."""
    store = ConnectionStore(tmp_path)
    cfg = _make_cfg(allow_drop_schemas=True, options={"connect_timeout": 5})

    store.save(cfg, name="dev", crypto_env=crypto_env)
    loaded = store.load_by_name("dev")

    assert loaded.options == {"connect_timeout": 5}
    assert "allow_drop_schemas" not in loaded.options


# --- interactive password fallback (машина без env-ключей, Phase 20) ---


def test_missing_key_prompts_for_password(crypto_env, tmp_path, monkeypatch) -> None:
    """env-ключ недоступен + колбэк → пароль запрашивается и попадает в cfg."""
    from db_project_manager.infrastructure.crypto.crypto_util import get_encrypted_text

    token = get_encrypted_text("real-secret", crypto_env)
    (tmp_path / "prod.yaml").write_text(
        f"host: db.host\nport: 5432\ndatabase: mydb\nusername: myuser\n"
        f"password: {token}\ntype: postgres\n",
        encoding="utf-8",
    )
    monkeypatch.delenv(crypto_env, raising=False)

    prompts: list[str] = []

    def fake_prompt(env_var: str, description: str) -> str:
        prompts.append((env_var, description))
        return "typed-password"

    store = ConnectionStore(tmp_path, password_prompt=fake_prompt)
    cfg = store.load_by_name("prod")

    assert cfg.password == "typed-password"
    assert len(prompts) == 1
    assert prompts[0][0] == crypto_env
    assert "myuser@db.host" in prompts[0][1]


def test_missing_key_without_prompt_keeps_token(crypto_env, tmp_path, monkeypatch) -> None:
    """Без колбэка (GUI/MCP) — прежнее поведение: токен возвращается как есть."""
    from db_project_manager.infrastructure.crypto.crypto_util import get_encrypted_text

    token = get_encrypted_text("real-secret", crypto_env)
    (tmp_path / "prod.yaml").write_text(
        f"host: db.host\nport: 5432\ndatabase: mydb\nusername: myuser\n"
        f"password: {token}\ntype: postgres\n",
        encoding="utf-8",
    )
    monkeypatch.delenv(crypto_env, raising=False)

    store = ConnectionStore(tmp_path)
    cfg = store.load_by_name("prod")

    assert cfg.password == token
