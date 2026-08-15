"""Application configuration (pydantic CFG) loaded from YAML.

Modeled after the shared_pckg config approach: YAML or dict -> optional
crypto decryption -> validated CFG object. Unlike the legacy stub, no
database connections live here; connections are separate files (see
connection_store.py).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field

from db_project_manager.infrastructure.crypto.crypto_util import get_decrypted_nested_dict


class PathsConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    default_output_dir: str = "./output"
    logs_dir: str = "./logs"


class LoggingConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    level: str = "INFO"
    console: bool = True
    file_name: str = "app.log"


class DeployConfig(BaseModel):
    """Controlled-deployment settings (Phase 10+).

    ``service_schema`` is the configurable name of the CD-foundation schema
    (CDF-4: configurable with default ``__deploy``). Carried by the config so
    reverse-engineer (seed/sync) and deploy (validate-presence / canonical-DDL
    warning) read the same name without code duplication.
    """

    model_config = ConfigDict(extra="ignore")

    service_schema: str = "__deploy"


class CFG(BaseModel):
    """Top-level application configuration."""

    model_config = ConfigDict(extra="ignore")

    paths: PathsConfig = Field(default_factory=PathsConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    # Optional default connection file path used by the GUI.
    default_connection_file: str | None = None
    deploy: DeployConfig = Field(default_factory=DeployConfig)


def load_yaml_file(path: Path) -> dict[str, Any]:
    """Read a YAML file into a dict (empty dict if file is empty)."""
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("Корень YAML должен быть объектом (mapping)")
    return raw


def load_raw_dict(source: Union[str, Path, Mapping[str, Any]]) -> dict[str, Any]:
    """Return a dict from a YAML path or an already-built mapping."""
    if isinstance(source, Mapping):
        return dict(source)
    return load_yaml_file(Path(source))


def decrypt_config_tree(data: dict[str, Any]) -> dict[str, Any]:
    """Recursively decrypt any crypto__ tokens in the config tree."""
    return get_decrypted_nested_dict(data)


def load_cfg(
    source: Union[str, Path, Mapping[str, Any], None] = None,
    *,
    decrypt: bool = True,
) -> CFG:
    """Load configuration: YAML/dict -> optional decryption -> validated CFG.

    Args:
        source: Path to YAML, a mapping, or None to use defaults.
        decrypt: Whether to decrypt crypto__ tokens (default True).
    """
    if source is None:
        return CFG()
    raw = load_raw_dict(source)
    if decrypt:
        raw = decrypt_config_tree(raw)
    return CFG.model_validate(raw)
