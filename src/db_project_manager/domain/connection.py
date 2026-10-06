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


class McpSettings(BaseModel):
    """Per-connection MCP server policy (Phase 19).

    Stored under the ``mcp:`` key of a connection YAML file. Fail-safe by
    design: everything mutating stays off until explicitly enabled. The block
    carries no secrets, so it needs no encryption (the connection-store
    decrypt pass is a no-op for plain values).

    Effective policy is always an McpSettings instance — unconfigured
    connections resolve to the defaults via
    :attr:`ConnectionConfig.mcp_settings`.
    """

    model_config = ConfigDict(extra="ignore")

    allow_writes: bool = Field(
        default=False,
        description="Разрешить MCP-инструменту run_script изменять данные/схему",
    )
    allow_deploy: bool = Field(
        default=False,
        description="Разрешить MCP-инструменты deploy_apply / deploy_reset (включая запись)",
    )
    row_limit: int = Field(
        default=50,
        ge=1,
        le=1000,
        description="Лимит строк ответа инструмента query (верхняя граница max_rows)",
    )
    query_timeout_s: int = Field(
        default=60,
        ge=1,
        le=3600,
        description="statement_timeout (секунды) для query/explain этого подключения",
    )


class ProfilingSettings(BaseModel):
    """Per-connection table profiling policy (Phase 20).

    Stored under the ``profiling:`` key of a connection YAML file. Fail-safe
    by design: profiling stays off until explicitly enabled — a connection
    without the block rejects ``profile``/``profile_tables`` before a single
    query is sent. The tool is read-only by construction (aggregates only,
    RO transaction backstop); ANALYZE is never run by the profiler.
    """

    model_config = ConfigDict(extra="ignore")

    enabled: bool = Field(
        default=False,
        description="Разрешить профайлинг таблиц (db-pm profile / MCP profile_tables)",
    )
    full_size_threshold_bytes: int = Field(
        default=1073741824,
        ge=1000000,
        le=10**15,
        description="Таблицы больше этого размера (pg_total_relation_size) профилируются по сэмплу",
    )
    sample_rows: int = Field(
        default=100000,
        ge=100,
        le=10000000,
        description="Целевой размер сэмпла (строк) для таблиц выше порога",
    )
    default_sample_fraction: float = Field(
        default=0.01,
        gt=0.0,
        le=1.0,
        description="Доля сэмпла, когда статистика отсутствует и reltuples неизвестен",
    )
    statement_timeout_ms: int = Field(
        default=300000,
        ge=1000,
        le=3600000,
        description="statement_timeout для запросов профайлинга (мс)",
    )
    top_n: int = Field(default=10, ge=1, le=100, description="Top-N частот для дискретных колонок")
    histogram_buckets: int = Field(default=20, ge=2, le=100, description="Корзин гистограммы width_bucket")
    column_chunk_size: int = Field(
        default=50,
        ge=1,
        le=500,
        description="Колонок в одном агрегатном запросе (лимит размера SQL)",
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

    # Phase 18 (deploy reset): destructive-schema-reset gate. Must stay a
    # top-level field — ``options`` goes straight into psycopg connect_args
    # and cannot carry tool flags.
    allow_drop_schemas: bool = Field(
        default=False,
        description="Разрешить destructive-команду deploy reset для этого подключения",
    )

    # Phase 19 (MCP server): per-connection tool policy. None = block absent,
    # callers resolve defaults via the mcp_settings property.
    mcp: McpSettings | None = Field(
        default=None,
        description="Политика MCP-сервера для этого подключения (блок mcp: в yaml)",
    )

    # Phase 20 (table profiling): per-connection policy. None = block absent,
    # callers resolve defaults via the profiling_settings property.
    profiling: ProfilingSettings | None = Field(
        default=None,
        description="Политика профайлинга таблиц для этого подключения (блок profiling: в yaml)",
    )

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

    @property
    def mcp_settings(self) -> McpSettings:
        """Effective MCP policy: configured block or fail-safe defaults."""
        return self.mcp if self.mcp is not None else McpSettings()

    @property
    def profiling_settings(self) -> ProfilingSettings:
        """Effective profiling policy: configured block or fail-safe defaults."""
        return self.profiling if self.profiling is not None else ProfilingSettings()
