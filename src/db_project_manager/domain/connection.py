"""Domain model for a database connection."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

#: Supported database adapter types.
SUPPORTED_DB_TYPES = ("postgres", "greenplum")


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

    @property
    def is_greenplum(self) -> bool:
        """Whether this connection targets a Greenplum cluster."""
        return self.type.lower() == "greenplum"
