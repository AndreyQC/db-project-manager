"""Domain model for a database connection."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator, field_serializer

#: Supported database adapter types.
SUPPORTED_DB_TYPES = ("postgres", "greenplum")


class ConnectionType(str, Enum):
    """Connection type — determines how the database connection is established."""

    DIRECT = "direct"       # Direct database connection (host:port)
    SSH_TUNNEL = "ssh_tunnel"  # Database via SSH tunnel


class SSH_TunnelConfig(BaseModel):
    """SSH tunnel configuration for connecting to a database via a jump host.

    The ``ssh_pass`` field may be either:
      - a plain string (transient, e.g. typed in a dialog), or
      - a crypto token in the form ``crypto__<ENV_VAR>__<ciphertext>``
        (as stored on disk; decrypted at load time by ConnectionStore).
    """

    model_config = ConfigDict(extra="ignore")

    ssh_host: str = Field(..., min_length=1, description="Jump host IP or hostname")
    ssh_port: int = Field(default=22, ge=1, le=65535, description="SSH port")
    ssh_user: str = Field(..., min_length=1, description="SSH username")
    ssh_pass: str = Field(default="", description="SSH password (plaintext or crypto token)")
    remote_bind_host: str = Field(
        default="127.0.0.1",
        description="Database host as seen from the jump host",
    )
    remote_bind_port: int = Field(
        default=5432, ge=1, le=65535, description="Database port as seen from the jump host"
    )
    local_bind_port: int = Field(
        default=0, ge=0, le=65535, description="Local port for tunnel (0 = auto-select)"
    )


class ConnectionConfig(BaseModel):
    """Parameters needed to connect to a database.

    The ``password`` field may be either:
      - a plain string (transient, e.g. typed in a dialog), or
      - a crypto token in the form ``crypto__<ENV_VAR>__<ciphertext>``
        (as stored on disk; decrypted at load time by ConnectionStore).
    """

    model_config = ConfigDict(extra="ignore")

    host: str = Field(..., min_length=1)
    port: int = Field(default=5432, ge=1, le=65535)
    database: str = Field(..., min_length=1)
    username: str = Field(..., min_length=1)
    password: str = ""
    type: str = Field(default="postgres", description="postgres | greenplum | snowflake | mssql")
    name: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)

    # SSH tunnel configuration
    connection_type: ConnectionType = Field(
        default=ConnectionType.DIRECT,
        description="direct | ssh_tunnel",
    )
    ssh_tunnel: SSH_TunnelConfig | None = Field(
        default=None,
        description="SSH tunnel parameters (required when connection_type=ssh_tunnel)",
    )

    @model_validator(mode="after")
    def _validate_ssh_tunnel_required(self) -> "ConnectionConfig":
        """Ensure ssh_tunnel is set when connection_type is SSH_TUNNEL."""
        if self.connection_type == ConnectionType.SSH_TUNNEL and self.ssh_tunnel is None:
            raise ValueError(
                "ssh_tunnel is required when connection_type is ssh_tunnel"
            )
        return self

    @field_serializer("connection_type")
    def _serialize_connection_type(self, value: ConnectionType) -> str:
        """Serialize ConnectionType enum as string for YAML."""
        return value.value

    @property
    def is_greenplum(self) -> bool:
        """Whether this connection targets a Greenplum cluster."""
        return self.type.lower() == "greenplum"
