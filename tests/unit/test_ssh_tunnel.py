"""Tests for db_project_manager.infrastructure.database.ssh_tunnel."""

from __future__ import annotations

import pytest

from db_project_manager.domain.connection import SSH_TunnelConfig
from db_project_manager.infrastructure.database.ssh_tunnel import (
    SSH_TunnelError,
    SSHTunnelManager,
)


class TestSSHTunnelManager:
    """Tests for SSHTunnelManager lifecycle and configuration."""

    def test_constructor_default_values(self) -> None:
        """All constructor args are stored correctly."""
        manager = SSHTunnelManager(
            ssh_host="192.168.1.100",
            ssh_port=22,
            ssh_user="root",
            ssh_password="secret",
        )
        assert manager.ssh_host == "192.168.1.100"
        assert manager.ssh_port == 22
        assert manager.ssh_user == "root"
        assert manager.ssh_password == "secret"
        assert manager.remote_bind_host == "127.0.0.1"
        assert manager.remote_bind_port == 5432
        assert manager.local_bind_port == 0

    def test_constructor_full_values(self) -> None:
        """All configuration values can be set."""
        manager = SSHTunnelManager(
            ssh_host="bastion.example.com",
            ssh_port=2222,
            ssh_user="deploy",
            ssh_password="hunter2",
            remote_bind_host="10.0.0.5",
            remote_bind_port=5433,
            local_bind_port=9000,
        )
        assert manager.ssh_host == "bastion.example.com"
        assert manager.ssh_port == 2222
        assert manager.ssh_user == "deploy"
        assert manager.ssh_password == "hunter2"
        assert manager.remote_bind_host == "10.0.0.5"
        assert manager.remote_bind_port == 5433
        assert manager.local_bind_port == 9000

    def test_from_config(self) -> None:
        """SSHTunnelManager.from_config creates manager from SSH_TunnelConfig."""
        config = SSH_TunnelConfig(
            ssh_host="192.168.1.50",
            ssh_port=22,
            ssh_user="admin",
            ssh_pass="encrypted_pass",
            remote_bind_host="127.0.0.1",
            remote_bind_port=5432,
            local_bind_port=0,
        )
        manager = SSHTunnelManager.from_config(config, decrypted_password="decrypted")
        assert manager.ssh_host == "192.168.1.50"
        assert manager.ssh_port == 22
        assert manager.ssh_user == "admin"
        assert manager.ssh_password == "decrypted"
        assert manager.remote_bind_host == "127.0.0.1"
        assert manager.remote_bind_port == 5432

    def test_stop_without_start_noop(self) -> None:
        """stop() without start() is a no-op (no error)."""
        manager = SSHTunnelManager(
            ssh_host="host",
            ssh_port=22,
            ssh_user="user",
            ssh_password="pass",
        )
        manager.stop()  # should not raise
        assert manager.is_active() is False

    def test_is_active_false_initially(self) -> None:
        """Manager is not active before start()."""
        manager = SSHTunnelManager(
            ssh_host="host",
            ssh_port=22,
            ssh_user="user",
            ssh_password="pass",
        )
        assert manager.is_active() is False

    def test_get_local_port_before_start_raises(self) -> None:
        """get_local_port() before start() raises SSH_TunnelError."""
        manager = SSHTunnelManager(
            ssh_host="host",
            ssh_port=22,
            ssh_user="user",
            ssh_password="pass",
        )
        with pytest.raises(SSH_TunnelError, match="not started"):
            manager.get_local_port()
