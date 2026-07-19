"""Tests for db_project_manager.domain.connection — ConnectionType and SSH_TunnelConfig."""

from __future__ import annotations

import pytest

from db_project_manager.domain.connection import (
    ConnectionConfig,
    ConnectionType,
    SSH_TunnelConfig,
)


class TestSSH_TunnelConfig:
    """Tests for SSH_TunnelConfig model."""

    def test_full_config(self) -> None:
        """All fields can be set and serialized."""
        tunnel = SSH_TunnelConfig(
            ssh_host="192.168.1.100",
            ssh_port=22,
            ssh_user="root",
            ssh_pass="secret",
            remote_bind_host="127.0.0.1",
            remote_bind_port=5432,
            local_bind_port=0,
        )
        data = tunnel.model_dump()
        assert data["ssh_host"] == "192.168.1.100"
        assert data["ssh_port"] == 22
        assert data["ssh_user"] == "root"
        assert data["ssh_pass"] == "secret"
        assert data["remote_bind_host"] == "127.0.0.1"
        assert data["remote_bind_port"] == 5432
        assert data["local_bind_port"] == 0

    def test_defaults(self) -> None:
        """Required fields are enforced, optional have sane defaults."""
        tunnel = SSH_TunnelConfig(ssh_host="bastion.example.com", ssh_user="admin")
        data = tunnel.model_dump()
        assert data["ssh_host"] == "bastion.example.com"
        assert data["ssh_user"] == "admin"
        assert data["ssh_port"] == 22
        assert data["ssh_pass"] == ""
        assert data["remote_bind_host"] == "127.0.0.1"
        assert data["remote_bind_port"] == 5432
        assert data["local_bind_port"] == 0

    def test_minimal_required(self) -> None:
        """Only ssh_host and ssh_user are required."""
        tunnel = SSH_TunnelConfig(ssh_host="10.0.0.1", ssh_user="ubuntu")
        assert tunnel.ssh_host == "10.0.0.1"
        assert tunnel.ssh_user == "ubuntu"

    def test_empty_host_rejected(self) -> None:
        """Empty ssh_host is rejected."""
        with pytest.raises(Exception):  # pydantic ValidationError
            SSH_TunnelConfig(ssh_host="", ssh_user="user")

    def test_port_range(self) -> None:
        """Port must be in valid range 1-65535."""
        with pytest.raises(Exception):
            SSH_TunnelConfig(ssh_host="host", ssh_user="user", ssh_port=0)
        with pytest.raises(Exception):
            SSH_TunnelConfig(ssh_host="host", ssh_user="user", ssh_port=70000)


class TestConnectionType:
    """Tests for ConnectionType enum."""

    def test_direct_value(self) -> None:
        assert ConnectionType.DIRECT.value == "direct"

    def test_ssh_tunnel_value(self) -> None:
        assert ConnectionType.SSH_TUNNEL.value == "ssh_tunnel"

    def test_serialization_in_dump(self) -> None:
        """ConnectionType serializes to its value string in model_dump."""
        cfg = ConnectionConfig(
            host="localhost",
            port=5432,
            database="mydb",
            username="myuser",
            password="secret",
            connection_type=ConnectionType.DIRECT,
        )
        data = cfg.model_dump()
        assert data["connection_type"] == "direct"

    def test_ssh_tunnel_serialization(self) -> None:
        """ConnectionType.SSH_TUNNEL serializes to 'ssh_tunnel'."""
        tunnel = SSH_TunnelConfig(ssh_host="host", ssh_user="user")
        cfg = ConnectionConfig(
            host="localhost",
            port=5432,
            database="mydb",
            username="myuser",
            password="secret",
            connection_type=ConnectionType.SSH_TUNNEL,
            ssh_tunnel=tunnel,
        )
        data = cfg.model_dump()
        assert data["connection_type"] == "ssh_tunnel"


class TestConnectionConfigSSHValidation:
    """Tests for ssh_tunnel validation in ConnectionConfig."""

    def test_ssh_tunnel_requires_ssh_tunnel_block(self) -> None:
        """connection_type=SSH_TUNNEL without ssh_tunnel raises ValidationError."""
        with pytest.raises(Exception) as exc_info:
            ConnectionConfig(
                host="localhost",
                port=5432,
                database="mydb",
                username="myuser",
                password="secret",
                connection_type=ConnectionType.SSH_TUNNEL,
            )
        assert "ssh_tunnel is required" in str(exc_info.value)

    def test_ssh_tunnel_with_block_passes(self) -> None:
        """connection_type=SSH_TUNNEL with ssh_tunnel is valid."""
        tunnel = SSH_TunnelConfig(ssh_host="192.168.1.100", ssh_user="root")
        cfg = ConnectionConfig(
            host="127.0.0.1",
            port=5432,
            database="mydb",
            username="myuser",
            password="secret",
            connection_type=ConnectionType.SSH_TUNNEL,
            ssh_tunnel=tunnel,
        )
        assert cfg.connection_type == ConnectionType.SSH_TUNNEL
        assert cfg.ssh_tunnel is not None
        assert cfg.ssh_tunnel.ssh_host == "192.168.1.100"

    def test_direct_connection_does_not_require_ssh_tunnel(self) -> None:
        """connection_type=DIRECT works without ssh_tunnel."""
        cfg = ConnectionConfig(
            host="localhost",
            port=5432,
            database="mydb",
            username="myuser",
            password="secret",
            connection_type=ConnectionType.DIRECT,
        )
        assert cfg.connection_type == ConnectionType.DIRECT
        assert cfg.ssh_tunnel is None

    def test_direct_default_is_direct(self) -> None:
        """Default connection_type is DIRECT."""
        cfg = ConnectionConfig(
            host="localhost",
            port=5432,
            database="mydb",
            username="myuser",
            password="secret",
        )
        assert cfg.connection_type == ConnectionType.DIRECT
        assert cfg.ssh_tunnel is None

    def test_roundtrip_serialization(self) -> None:
        """SSH_TUNNEL config survives model_dump/model_validate roundtrip."""
        tunnel = SSH_TunnelConfig(
            ssh_host="bastion.example.com",
            ssh_port=2222,
            ssh_user="deploy",
            ssh_pass="crypto__ENV__token",
            remote_bind_host="10.0.0.5",
            remote_bind_port=5433,
            local_bind_port=0,
        )
        cfg = ConnectionConfig(
            host="127.0.0.1",
            port=5432,
            database="production",
            username="app_user",
            password="pgpass",
            type="postgres",
            name="prod-tunnel",
            connection_type=ConnectionType.SSH_TUNNEL,
            ssh_tunnel=tunnel,
        )
        data = cfg.model_dump()
        restored = ConnectionConfig.model_validate(data)
        assert restored.connection_type == ConnectionType.SSH_TUNNEL
        assert restored.ssh_tunnel is not None
        assert restored.ssh_tunnel.ssh_host == "bastion.example.com"
        assert restored.ssh_tunnel.ssh_port == 2222
        assert restored.ssh_tunnel.ssh_user == "deploy"
        assert restored.ssh_tunnel.ssh_pass == "crypto__ENV__token"
        assert restored.ssh_tunnel.remote_bind_host == "10.0.0.5"
        assert restored.ssh_tunnel.remote_bind_port == 5433
        assert restored.ssh_tunnel.local_bind_port == 0
        assert restored.name == "prod-tunnel"
