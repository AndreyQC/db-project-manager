"""Reading/writing connection files (connections/*.yaml).

Connection files store a single connection's parameters. The password is
persisted encrypted (crypto__<ENV_VAR>__<token>) and decrypted on load.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from db_project_manager.domain.connection import ConnectionConfig
from db_project_manager.infrastructure.crypto.crypto_util import (
    _is_cipher_token,
    get_decrypted_nested_dict,
    get_encrypted_text,
)


class ConnectionStoreError(Exception):
    """Raised on connection file I/O or validation errors."""


class ConnectionStore:
    """Persist and load ConnectionConfig to/from YAML files."""

    def __init__(self, connections_dir: str | Path | None = None) -> None:
        self.connections_dir = Path(connections_dir) if connections_dir else Path("connections")

    # --- paths ---

    def path_for(self, name: str) -> Path:
        """Return the file path for a named connection."""
        safe = self._safe_stem(name)
        return self.connections_dir / f"{safe}.yaml"

    @staticmethod
    def _safe_stem(name: str) -> str:
        """Sanitize a connection name into a filesystem-safe stem."""
        stem = Path(name).stem if name.endswith(".yaml") else name
        # Reject path separators / suspicious characters.
        if any(ch in stem for ch in ("/", "\\", ":", "*", "?", '"', "<", ">", "|")):
            raise ConnectionStoreError(f"Недопустимое имя подключения: {name!r}")
        return stem

    # --- write ---

    def save(
        self,
        cfg: ConnectionConfig,
        *,
        name: str | None = None,
        crypto_env: str,
    ) -> Path:
        """Persist a connection, encrypting the password under ``crypto_env``.

        Args:
            cfg: Connection parameters. If password is already a crypto token,
                it is kept as-is (re-encrypting would produce a different token
                but still valid; we avoid needless churn).
            name: Connection name (file stem). Falls back to cfg.name.
            crypto_env: Environment variable name holding the Fernet key.

        Returns:
            Path of the written file.
        """
        stem = name or cfg.name
        if not stem:
            raise ConnectionStoreError("Не задано имя подключения (name или cfg.name)")

        data: dict[str, Any] = cfg.model_dump(exclude={"name"})

        # Encrypt the password unless it is already a cipher token.
        password = data.get("password", "")
        if password and not _is_cipher_token(password):
            data["password"] = get_encrypted_text(password, crypto_env)

        self.connections_dir.mkdir(parents=True, exist_ok=True)
        path = self.path_for(stem)
        with path.open("w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
        return path

    # --- read ---

    def load(self, path: str | Path, *, name: str | None = None) -> ConnectionConfig:
        """Load and decrypt a connection file.

        Args:
            path: Path to the connection YAML file.
            name: Optional connection name override (otherwise derived from file stem).
        """
        path = Path(path)
        if not path.exists():
            raise ConnectionStoreError(f"Файл подключения не найден: {path}")

        with path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        if not isinstance(raw, dict):
            raise ConnectionStoreError(f"Файл подключения имеет неверный формат: {path}")

        decrypted = get_decrypted_nested_dict(raw)
        conn_name = name if name is not None else path.stem
        decrypted.setdefault("name", conn_name)
        try:
            return ConnectionConfig.model_validate(decrypted)
        except Exception as exc:  # pydantic ValidationError
            raise ConnectionStoreError(f"Ошибка валидации подключения {path}: {exc}") from exc

    def load_by_name(self, name: str) -> ConnectionConfig:
        """Load a connection by its name within the connections directory."""
        return self.load(self.path_for(name), name=name)

    # --- listing ---

    def list_names(self) -> list[str]:
        """Return the names (stems) of all connection files."""
        if not self.connections_dir.exists():
            return []
        return sorted(p.stem for p in self.connections_dir.glob("*.yaml"))

    def delete(self, name: str) -> None:
        """Delete a connection file by name (no error if missing)."""
        path = self.path_for(name)
        if path.exists():
            path.unlink()
