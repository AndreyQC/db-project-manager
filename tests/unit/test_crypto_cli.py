"""Tests for the ``db-pm crypto`` CLI commands (Phase 19.1).

Contract under test:

* ``keygen`` prints a valid Fernet key (usable for encrypt/decrypt round-trip);
* ``encrypt`` reads stdin, prints a single ``crypto__ENV__<token>`` line that
  decrypts back with the key from the env var;
* errors: empty input → exit 1; missing env var → exit 2 with a hint.
"""

from __future__ import annotations

from cryptography.fernet import Fernet
from typer.testing import CliRunner

from db_project_manager.infrastructure.crypto.crypto_util import get_decrypted_text
from db_project_manager.presentation.cli import main as cli_main

runner = CliRunner()


def test_keygen_prints_valid_fernet_key():
    result = runner.invoke(cli_main.app, ["crypto", "keygen"])
    assert result.exit_code == 0
    key = result.output.strip()
    # A generated key must itself work as a Fernet key.
    token = Fernet(key).encrypt(b"probe")
    assert Fernet(key).decrypt(token) == b"probe"


def test_encrypt_roundtrip_from_stdin(monkeypatch):
    monkeypatch.setenv("ENVOS_CRYPTO_01", Fernet.generate_key().decode())
    result = runner.invoke(
        cli_main.app, ["crypto", "encrypt", "ENVOS_CRYPTO_01"], input="s3cret\n"
    )
    assert result.exit_code == 0
    token = result.output.strip()
    assert token.startswith("crypto__ENVOS_CRYPTO_01__")
    assert get_decrypted_text(token) == "s3cret"


def test_encrypt_empty_input_rejected(monkeypatch):
    monkeypatch.setenv("ENVOS_CRYPTO_01", Fernet.generate_key().decode())
    result = runner.invoke(cli_main.app, ["crypto", "encrypt", "ENVOS_CRYPTO_01"], input="\n")
    assert result.exit_code == 1
    assert "Пустое значение" in result.output


def test_encrypt_missing_env_var_rejected(monkeypatch):
    monkeypatch.delenv("ENVOS_CRYPTO_01", raising=False)
    result = runner.invoke(cli_main.app, ["crypto", "encrypt", "ENVOS_CRYPTO_01"], input="s3cret\n")
    assert result.exit_code == 2
    assert "не задана" in result.output
    assert "keygen" in result.output  # hint how to fix
