"""Validation deploy service (Phase 2, Scenario A).

Deploys a codebase into a fresh empty database on a server where the user has
CREATEDB privilege, to verify the full script assembles without errors.

Flow (vision §1.4):
  1. connect to server (maintenance connection)
  2. check_can_create_db — else PermissionError before touching anything
  3. name = sanitize(prefix or codebase dir name) + server-UTC timestamp
  4. create_database(name); reconnect to the new database
  5. build graph, filter build=true (Q8), topologically sort
  6. execute each object's SQL in deploy order, with stratified error policy:
       * early DDL (schema/sequence/table/index/constraint) -> fail-fast + cleanup
       * late objects (view/matview/function/procedure/trigger) -> per-object,
         continue-on-error collects all failures
  7. finally: drop the temp DB unless keep_db=True

Result is a DeployResult carrying success, the temp DB name, per-object errors
and progress counters. CLI/GUI consume it without knowing any internals.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

from db_project_manager.application.graph_service import BuildGraphService
from db_project_manager.domain.connection import ConnectionConfig
from db_project_manager.domain.graph import Vertex
from db_project_manager.infrastructure.database.base import DatabaseAdapter, DatabaseError
from db_project_manager.infrastructure.database.registry import get_adapter
from db_project_manager.infrastructure.sql.autodoc import strip_autodoc

#: Object types considered early DDL: structural, downstream of any failure
#: here makes further deploy meaningless -> fail-fast + cleanup.
EARLY_DDL_TYPES = {
    "schema", "sequence", "table", "external_table", "index", "constraint",
    # Phase 5: extensions and db settings must succeed or the rest is meaningless.
    "extension", "database_setting",
}

#: Late object types: independent enough to log per-object errors and continue.
LATE_OBJECT_TYPES = {"view", "materialized_view", "function", "procedure", "trigger"}

#: Progress callback signature: (message, current_step, total_steps).
ProgressCallback = Callable[[str, int, int], None]


class DeployPermissionError(Exception):
    """User lacks CREATEDB privilege."""


class DeployError(Exception):
    """Validation deploy failed (one or more object errors)."""


@dataclass
class ObjectError:
    """One failed object during deploy."""

    object_key: str
    object_type: str
    object_name: str
    source_file: str
    error: str


@dataclass
class DeployResult:
    """Outcome of a validation deploy."""

    success: bool
    db_name: str
    objects_total: int = 0
    objects_done: int = 0
    errors: list[ObjectError] = field(default_factory=list)

    @property
    def dropped_db(self) -> bool:
        """True if the temp DB was dropped after the run (cleanup happened)."""
        return self._dropped_db

    _dropped_db: bool = False


#: Whitelist for the prefix portion of the temp-DB name (sanitized lower-case).
_PREFIX_RE = re.compile(r"[^a-z0-9]+")


def sanitize_prefix(name: str) -> str:
    """Lowercase and collapse non [a-z0-9] runs to a single underscore.

    Empty result falls back to 'dbpm' so the final name is always valid.
    """
    collapsed = _PREFIX_RE.sub("_", name.lower()).strip("_")
    return collapsed or "dbpm"


class DeployValidateService:
    """Orchestrates validation deploy on a DatabaseAdapter."""

    def __init__(
        self,
        graph_service: BuildGraphService | None = None,
        adapter_factory: Callable[[ConnectionConfig], DatabaseAdapter] | None = None,
    ) -> None:
        self.graph_service = graph_service or BuildGraphService()
        self._adapter_factory = adapter_factory or get_adapter

    def run(
        self,
        conn_cfg: ConnectionConfig,
        codebase_dir: str | Path,
        *,
        prefix: str | None = None,
        keep_db: bool = False,
        continue_on_error: bool = False,
        progress: ProgressCallback | None = None,
    ) -> DeployResult:
        """Run validation deploy and return the structured result.

        Raises:
            DeployPermissionError: user lacks CREATEDB.
            DeployError: early-DDL failure or (without continue_on_error) any failure.
        """
        codebase_dir = Path(codebase_dir)
        adapter = self._adapter_factory(conn_cfg)

        # 1. Connect to server.
        self._emit(progress, "Подключение к серверу БД…", 0, 0)
        adapter.connect(conn_cfg)

        # 2. Permission check — fail fast with a clear message.
        if not adapter.check_can_create_db():
            adapter.disconnect()
            msg = (
                f"У пользователя '{conn_cfg.username}' нет права CREATEDB. "
                "Validation deploy требует создания временной базы данных."
            )
            logger.error(msg)
            raise DeployPermissionError(msg)

        # 3. Build deploy order (graph + filter + toposort).
        self._emit(progress, "Построение графа зависимостей…", 0, 0)
        deploy_vertices = self.graph_service.deploy_order(codebase_dir, build_only=True)
        total = len(deploy_vertices)

        # Extract db-level CREATE DATABASE properties from the database_setting
        # vertex (if present). These are carried in vertex.extra["db_properties"]
        # from the autodoc header written by SQLGenerator (P5.S03/S05).
        db_props: dict[str, str] = {}
        for v in deploy_vertices:
            if v.object_type == "database_setting" and v.extra:
                db_props = v.extra.get("db_properties") or {}
                break

        # 4. Name + create temp DB.
        prefix_value = sanitize_prefix(prefix or codebase_dir.name)
        timestamp = adapter.get_server_timestamp_utc()
        db_name = f"{prefix_value}_{timestamp}"

        self._emit(progress, f"Создание временной базы данных: {db_name}", 0, total)
        adapter.create_database(
            db_name,
            encoding=db_props.get("encoding"),
            lc_collate=db_props.get("lc_collate"),
            lc_ctype=db_props.get("lc_ctype"),
            template=db_props.get("template"),
        )

        # Reconnect to the freshly created DB by overriding the cfg's database.
        result = DeployResult(success=True, db_name=db_name, objects_total=total)
        try:
            target_cfg = conn_cfg.model_copy(update={"database": db_name})
            adapter.disconnect()
            adapter.connect(target_cfg)

            # 5. Execute in deploy order with stratified error policy.
            for i, vertex in enumerate(deploy_vertices, start=1):
                self._emit(progress, f"[{i}/{total}] {vertex.object_type} {vertex.object_name}", i, total)
                try:
                    self._deploy_object(adapter, codebase_dir, vertex, db_name)
                    result.objects_done = i
                except DatabaseError as e:
                    obj_err = ObjectError(
                        object_key=vertex.object_key,
                        object_type=vertex.object_type,
                        object_name=vertex.object_name,
                        source_file=vertex.object_source_file,
                        error=str(e),
                    )
                    result.errors.append(obj_err)
                    result.success = False
                    if vertex.object_type in EARLY_DDL_TYPES:
                        # Early failure -> abort, cleanup will follow.
                        logger.error(
                            f"Ошибка в раннем DDL {vertex.object_type} '{vertex.object_name}': {e}. "
                            "Дальнейший деплой бессмысленен."
                        )
                        break
                    if not continue_on_error:
                        logger.error(
                            f"Ошибка в объекте {vertex.object_type} '{vertex.object_name}': {e}"
                        )
                        break
                    logger.warning(
                        f"Пропуск объекта {vertex.object_type} '{vertex.object_name}' "
                        f"из-за ошибки ({continue_on_error=}): {e}"
                    )
        finally:
            # 6. Cleanup unless explicitly kept.
            if not keep_db:
                try:
                    adapter.disconnect()
                    adapter.connect(conn_cfg)
                    adapter.drop_database(db_name)
                    result._dropped_db = True
                except Exception as e:  # noqa: BLE001
                    logger.error(f"Не удалось удалить временную БД {db_name}: {e}")
                finally:
                    adapter.disconnect()
            else:
                adapter.disconnect()

        if not result.success:
            summary = self._format_errors(result.errors)
            logger.error(f"Validation deploy завершён с ошибками:\n{summary}")
        else:
            logger.info(f"Validation deploy успешен: база {db_name}")

        return result

    # --- helpers ---

    def _deploy_object(
        self,
        adapter: DatabaseAdapter,
        codebase_dir: Path,
        vertex: Vertex,
        target_db_name: str,
    ) -> None:
        """Read the object's SQL file and execute it."""
        source = codebase_dir / vertex.object_source_file
        if not source.is_file():
            raise DatabaseError(f"Файл объекта не найден: {source}")
        script = source.read_text(encoding="utf-8-sig")
        # Strip the autodoc header so only SQL reaches the server.
        script = strip_autodoc(script)
        # Phase 5: for database_setting, replace the original db name in
        # ALTER DATABASE ... SET statements with the actual target DB name.
        # The original name is stored in object_catalog (written by SQLGenerator).
        if vertex.object_type == "database_setting" and vertex.object_catalog:
            script = script.replace(
                f'"{vertex.object_catalog}"', f'"{target_db_name}"'
            )
        adapter.execute_script(script)

    @staticmethod
    def _format_errors(errors: list[ObjectError]) -> str:
        if not errors:
            return ""
        lines = []
        for err in errors:
            lines.append(
                f"  - [{err.object_type}] {err.object_name} ({err.source_file}): {err.error}"
            )
        return "\n".join(lines)

    @staticmethod
    def _emit(progress: ProgressCallback | None, message: str, current: int, total: int) -> None:
        if progress is not None:
            progress(message, current, total)
