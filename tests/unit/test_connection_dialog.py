"""Offscreen tests for the connection dialog crypto-key selector (QT_QPA_PLATFORM=offscreen).

The dialog lets the user pick which env variable (holding a Fernet key) the
connection's secrets are encrypted with. Covered:
- auto-detection of env vars whose value is a valid Fernet key;
- the default var (crypto_env ctor arg) is preselected;
- accept is blocked when the chosen var is missing / not a key / empty;
- saving encrypts with the SELECTED var, not the default;
- editing prefills the var the stored file was encrypted with.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from cryptography.fernet import Fernet  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

from db_project_manager.domain.connection import ConnectionConfig, SSH_TunnelConfig  # noqa: E402
from db_project_manager.infrastructure.config.connection_store import (  # noqa: E402
    ConnectionStore,
)
from db_project_manager.infrastructure.crypto.crypto_util import get_encrypted_text  # noqa: E402
from db_project_manager.presentation.gui.widgets.connection_dialog import (  # noqa: E402
    ConnectionDialog,
)

ENV_PRIMARY = "ENVOS_CRYPTO_01"
ENV_SECONDARY = "ENVOS_CRYPTO_02"
KEY_PRIMARY = Fernet.generate_key().decode("ascii")
KEY_SECONDARY = Fernet.generate_key().decode("ascii")


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def two_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_PRIMARY, KEY_PRIMARY)
    monkeypatch.setenv(ENV_SECONDARY, KEY_SECONDARY)


def _make_cfg(password: str = "plain-secret") -> ConnectionConfig:
    return ConnectionConfig(
        name="prod",
        host="localhost",
        port=5432,
        database="mydb",
        username="myuser",
        password=password,
        type="postgres",
    )


def _fill_required(dlg: ConnectionDialog) -> None:
    dlg.name_edit.setText("prod")
    dlg.host_edit.setText("localhost")
    dlg.database_edit.setText("mydb")
    dlg.username_edit.setText("myuser")
    dlg.password_edit.setText("plain-secret")


@pytest.fixture
def critical_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Collect QMessageBox.critical texts so the dialog never blocks on a modal."""
    calls: list[str] = []

    def _record(*args, **kwargs):
        calls.append(args[-1] if args else "")
        return None

    monkeypatch.setattr(QMessageBox, "critical", _record)
    return calls


def test_detect_lists_only_valid_fernet_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_PRIMARY, KEY_PRIMARY)
    monkeypatch.setenv("MY_CUSTOM_CRYPTO_VAR", KEY_SECONDARY)
    # Contains CRYPTO but its value is a var NAME, not a key — must be excluded.
    monkeypatch.setenv("NOT_A_KEY_CRYPTO", ENV_PRIMARY)

    detected = ConnectionDialog._detect_crypto_envs()

    assert ENV_PRIMARY in detected
    assert "MY_CUSTOM_CRYPTO_VAR" in detected
    assert "NOT_A_KEY_CRYPTO" not in detected


def test_combo_defaults_to_ctor_env_and_lists_detected(qapp, two_keys) -> None:
    dlg = ConnectionDialog(ConnectionStore(), crypto_env=ENV_PRIMARY)
    items = [dlg.crypto_env_combo.itemText(i) for i in range(dlg.crypto_env_combo.count())]
    assert dlg.selected_crypto_env() == ENV_PRIMARY
    assert ENV_SECONDARY in items


def test_accept_blocked_when_env_var_missing(qapp, tmp_path, two_keys, critical_calls) -> None:
    dlg = ConnectionDialog(ConnectionStore(tmp_path), crypto_env=ENV_PRIMARY)
    _fill_required(dlg)
    dlg.crypto_env_combo.setCurrentText("NO_SUCH_CRYPTO_VAR")

    dlg._on_accept()

    assert dlg.result() != QDialog.DialogCode.Accepted
    assert len(critical_calls) == 1


def test_accept_blocked_when_env_value_not_a_fernet_key(
    qapp, tmp_path, monkeypatch: pytest.MonkeyPatch, critical_calls
) -> None:
    monkeypatch.setenv("BROKEN_CRYPTO_KEY", "not-a-fernet-key")
    dlg = ConnectionDialog(ConnectionStore(tmp_path), crypto_env=ENV_PRIMARY)
    _fill_required(dlg)
    dlg.crypto_env_combo.setCurrentText("BROKEN_CRYPTO_KEY")

    dlg._on_accept()

    assert dlg.result() != QDialog.DialogCode.Accepted
    assert len(critical_calls) == 1


def test_accept_blocked_when_env_empty(qapp, tmp_path, two_keys, critical_calls) -> None:
    dlg = ConnectionDialog(ConnectionStore(tmp_path), crypto_env=ENV_PRIMARY)
    _fill_required(dlg)
    dlg.crypto_env_combo.setCurrentText("   ")

    dlg._on_accept()

    assert dlg.result() != QDialog.DialogCode.Accepted
    assert len(critical_calls) == 1


def test_save_encrypts_with_selected_env(qapp, tmp_path, two_keys) -> None:
    store = ConnectionStore(tmp_path)
    dlg = ConnectionDialog(store, crypto_env=ENV_PRIMARY)
    _fill_required(dlg)
    dlg._set_crypto_env(ENV_SECONDARY)

    dlg._on_accept()

    assert dlg.result() == QDialog.DialogCode.Accepted
    text = (tmp_path / "prod.yaml").read_text(encoding="utf-8")
    assert f"crypto__{ENV_SECONDARY}__" in text
    assert f"crypto__{ENV_PRIMARY}__" not in text
    # Round-trip with the secondary key set by the fixture.
    assert store.load_by_name("prod").password == "plain-secret"


def test_edit_prefills_stored_env(qapp, tmp_path, two_keys) -> None:
    store = ConnectionStore(tmp_path)
    store.save(_make_cfg(), name="prod", crypto_env=ENV_SECONDARY)

    # Ctor default is the primary var, but the file says secondary — the
    # dialog must keep the stored key instead of silently switching it.
    dlg = ConnectionDialog(store, name="prod", crypto_env=ENV_PRIMARY)

    assert dlg.selected_crypto_env() == ENV_SECONDARY


# --- conditional key validation: unset env var is OK when nothing to encrypt ---


def test_needs_encryption_variants(two_keys) -> None:
    token = get_encrypted_text("pw", ENV_SECONDARY)

    assert ConnectionDialog._needs_encryption(_make_cfg()) is True  # plaintext password
    assert ConnectionDialog._needs_encryption(_make_cfg(password="")) is False
    assert ConnectionDialog._needs_encryption(_make_cfg(password=token)) is False
    # Token DB password but a plaintext SSH secret still needs the key.
    with_ssh = _make_cfg(password=token)
    with_ssh.ssh_tunnel = SSH_TunnelConfig(
        ssh_host="bastion", ssh_port=22, ssh_user="u", ssh_pass="plain-ssh"
    )
    assert ConnectionDialog._needs_encryption(with_ssh) is True


def test_accept_allows_unset_env_when_secrets_already_tokens(
    qapp, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENVOS_CRYPTO_KUBER_01 set only in the target env (e.g. Kubernetes):
    a token password round-trips locally without the key — the var name is
    just recorded, and the token is kept as-is on save."""
    monkeypatch.setenv("ENVOS_CRYPTO_KUBER_01", KEY_SECONDARY)
    token = get_encrypted_text("plain-secret", "ENVOS_CRYPTO_KUBER_01")
    monkeypatch.delenv("ENVOS_CRYPTO_KUBER_01", raising=False)

    store = ConnectionStore(tmp_path)
    dlg = ConnectionDialog(store, crypto_env=ENV_PRIMARY)
    _fill_required(dlg)
    dlg.password_edit.setText(token)
    dlg.crypto_env_combo.setCurrentText("ENVOS_CRYPTO_KUBER_01")

    dlg._on_accept()

    assert dlg.result() == QDialog.DialogCode.Accepted
    text = (tmp_path / "prod.yaml").read_text(encoding="utf-8")
    assert token in text  # kept byte-for-byte, not re-encrypted


def test_accept_still_blocks_plaintext_when_env_unset(
    qapp, tmp_path, monkeypatch: pytest.MonkeyPatch, critical_calls
) -> None:
    """A plaintext password MUST be encrypted on save — an unset var is a hard
    error no matter the name (encryption needs the key material locally)."""
    monkeypatch.delenv("ENVOS_CRYPTO_KUBER_01", raising=False)
    monkeypatch.setenv(ENV_PRIMARY, KEY_PRIMARY)

    dlg = ConnectionDialog(ConnectionStore(tmp_path), crypto_env=ENV_PRIMARY)
    _fill_required(dlg)
    dlg.crypto_env_combo.setCurrentText("ENVOS_CRYPTO_KUBER_01")

    dlg._on_accept()

    assert dlg.result() != QDialog.DialogCode.Accepted
    assert len(critical_calls) == 1
