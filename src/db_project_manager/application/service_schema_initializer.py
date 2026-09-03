"""Service-schema (``__deploy``) initializer (Phase 15.5.2 — cis_zup feedback).

The deploy pipeline assumes the target DB already has the service schema
(``__deploy``) with its 3 bookkeeping tables (``schema_version``,
``script_history``, ``script_audit_log``). On a freshly-CREATE-DATABASE'd target
this is not the case — and ``deploy apply`` cannot bootstrap it because:

* ``ReverseEngineerService._seed_or_sync_deploy`` writes canonical ``__deploy``
  files into a temp dir for snapshot purposes, but the temp dir is read-only
  from the snapshot's perspective (CompareService). The seeded files
  therefore enter the target snapshot, comparator marks them as UNCHANGED,
  and ``DeltaPlan`` action is ``skip`` for ``__deploy`` — apply never executes
  the canonical DDL on the real target DB.

* Real RE-write into a codebase dir (``db-pm reverse-engineer --target-cf ...``)
  DOES seed the DB via ``adapter.connect`` and ``adapter.execute_script``,
  but only as a side effect of writing files — not via a dedicated
  init/bootstrap path. There is no "deploy init-service-schema" command.

This service closes the gap: explicit, idempotent, opt-in bootstrap of the
service schema on a target DB. Idempotency is provided by ``CREATE … IF NOT
EXISTS`` plus a pre-check that exits early when all 4 objects already exist.
The DDL is the same canonical template used by RE seeding
(``canonical_ddl.py::canonical_deploy_ddl``) so the result is bit-identical
to a codebase written by RE / yaml apply.
"""

from __future__ import annotations

from collections.abc import Callable

from loguru import logger

from db_project_manager.domain.connection import ConnectionConfig
from db_project_manager.infrastructure.database.base import DatabaseAdapter, DatabaseError
from db_project_manager.infrastructure.database.registry import get_adapter
from db_project_manager.infrastructure.deploy.canonical_ddl import (
    DEFAULT_SERVICE_SCHEMA,
    canonical_deploy_ddl,
)

#: Progress callback signature — (stage_message, current_step, total_steps).
ProgressCallback = Callable[[str, int, int], None]


class ServiceSchemaInitializerError(Exception):
    """Raised when the bootstrap fails (connection, permissions, SQL error)."""


class ServiceSchemaInitializerResult:
    """Outcome of :meth:`ServiceSchemaInitializer.run`.

    Attributes mirror the runtime checks so a CLI can report a precise summary.
    All fields are populated even on early exits (idempotent no-op).
    """

    def __init__(
        self,
        *,
        service_schema: str,
        schema_present: bool,
        tables_present: list[str],
        tables_missing: list[str],
        created_schema: bool,
        created_tables: list[str],
        schema_version: str | None,
    ) -> None:
        self.service_schema = service_schema
        self.schema_present = schema_present
        self.tables_present = tables_present
        self.tables_missing = tables_missing
        self.created_schema = created_schema
        self.created_tables = created_tables
        self.schema_version = schema_version

    @property
    def changed(self) -> bool:
        """True when at least one object was created during this run."""
        return self.created_schema or bool(self.created_tables)

    @property
    def all_present(self) -> bool:
        """True when ``__deploy`` schema + 3 tables all exist on the target."""
        return self.schema_present and not self.tables_missing


class ServiceSchemaInitializer:
    """Idempotent init of the ``__deploy`` service schema on a target DB.

    Used by ``db-pm deploy init-service-schema`` and (optionally) by
    ``deploy apply`` as a pre-flight step (BACKLOG). All DDL is the canonical
    template so the result is identical to what RE / yaml apply would
    produce — no separate code path.
    """

    _REQUIRED_TABLES: tuple[str, ...] = (
        "schema_version",
        "script_history",
        "script_audit_log",
    )

    def __init__(
        self,
        adapter_factory: Callable[[ConnectionConfig], DatabaseAdapter] | None = None,
        service_schema: str = DEFAULT_SERVICE_SCHEMA,
    ) -> None:
        self._adapter_factory = adapter_factory or get_adapter
        self._service_schema = service_schema

    def run(
        self,
        conn_cfg: ConnectionConfig,
        *,
        progress: ProgressCallback | None = None,
    ) -> ServiceSchemaInitializerResult:
        """Check + bootstrap the service schema on the target DB.

        Algorithm:
          1. Connect; read schema list + per-schema table list (via
             ``get_database_structure``).
          2. If schema present AND all 3 tables present → early exit
             (idempotent no-op, ``changed=False``).
          3. If schema missing → ``CREATE SCHEMA IF NOT EXISTS``.
          4. For each missing table → render canonical DDL → ``execute_script``
             (each table separately so a partial failure leaves a clear log).
          5. Read back ``adapter.get_schema_version`` for the result.
        """
        self._emit(progress, "Подключение к целевой БД…", 0, 3)
        adapter = self._adapter_factory(conn_cfg)
        try:
            adapter.connect(conn_cfg)
        except DatabaseError as e:
            raise ServiceSchemaInitializerError(
                f"Не удалось подключиться к БД: {e}"
            ) from e

        try:
            self._emit(progress, "Чтение структуры БД…", 1, 3)
            structure = adapter.get_database_structure()
            schemas = structure.get("schemas") or []
            schema_names = {s.get("name") for s in schemas if s.get("name")}
            schema_present = self._service_schema in schema_names

            tables_present: list[str] = []
            tables_missing: list[str] = []
            if schema_present:
                for s in schemas:
                    if s.get("name") != self._service_schema:
                        continue
                    existing = {t.get("name") for t in (s.get("tables") or [])}
                    for tbl in self._REQUIRED_TABLES:
                        if tbl in existing:
                            tables_present.append(tbl)
                        else:
                            tables_missing.append(tbl)
            else:
                tables_missing = list(self._REQUIRED_TABLES)

            if schema_present and not tables_missing:
                self._emit(
                    progress,
                    f"Схема {self._service_schema} + 3 таблицы уже существуют — "
                    "идемпотентный no-op.",
                    3, 3,
                )
                version = self._read_schema_version(adapter)
                return ServiceSchemaInitializerResult(
                    service_schema=self._service_schema,
                    schema_present=True,
                    tables_present=tables_present,
                    tables_missing=[],
                    created_schema=False,
                    created_tables=[],
                    schema_version=version,
                )

            self._emit(
                progress,
                f"Bootstrap {self._service_schema}: schema={'нет' if not schema_present else 'ok'}, "
                f"отсутствуют таблицы: {tables_missing or '[]'}",
                2, 3,
            )
            created_schema = False
            created_tables: list[str] = []

            if not schema_present:
                self._emit(
                    progress, f"CREATE SCHEMA \"{self._service_schema}\"…", 2, 3
                )
                try:
                    adapter.execute_script(
                        f'CREATE SCHEMA IF NOT EXISTS "{self._service_schema}";\n'
                    )
                except DatabaseError as e:
                    raise ServiceSchemaInitializerError(
                        f"Не удалось создать схему {self._service_schema}: {e}"
                    ) from e
                created_schema = True

            ddl_by_table = canonical_deploy_ddl(self._service_schema)
            for table_name in self._REQUIRED_TABLES:
                if schema_present and table_name in tables_present:
                    continue
                self._emit(
                    progress,
                    f"CREATE TABLE {self._service_schema}.{table_name}…",
                    3, 3,
                )
                try:
                    adapter.execute_script(
                        _wrap_with_if_not_exists(ddl_by_table[table_name])
                    )
                except DatabaseError as e:
                    raise ServiceSchemaInitializerError(
                        f"Не удалось создать таблицу {self._service_schema}."
                        f"{table_name}: {e}"
                    ) from e
                created_tables.append(table_name)

            version = self._read_schema_version(adapter)
            return ServiceSchemaInitializerResult(
                service_schema=self._service_schema,
                schema_present=True,
                tables_present=list(self._REQUIRED_TABLES),
                tables_missing=[],
                created_schema=created_schema,
                created_tables=created_tables,
                schema_version=version,
            )
        finally:
            try:
                adapter.disconnect()
            except DatabaseError:
                # Disconnect failures are cosmetic — never mask the result.
                logger.debug("Не удалось корректно отключиться от БД.")

    def _read_schema_version(self, adapter: DatabaseAdapter) -> str | None:
        try:
            return adapter.get_schema_version(self._service_schema)
        except DatabaseError as e:
            logger.debug(
                f"Не удалось прочитать schema_version после bootstrap: {e}"
            )
            return None

    @staticmethod
    def _emit(
        progress: ProgressCallback | None,
        message: str,
        current: int,
        total: int,
    ) -> None:
        if progress is not None:
            progress(message, current, total)


def _wrap_with_if_not_exists(ddl: str) -> str:
    """Prepend a verification + ensure script-checksum-equivalent DDL.

    ``canonical_deploy_ddl`` returns a CREATE TABLE without ``IF NOT EXISTS``
    to match the canonical checksum exactly. For the bootstrap path we relax
    that — idempotent retry without a separate pre-check. The resulting table
    is identical because canonical DDL never has an existing-table branch.
    """
    head, _, body = ddl.partition("(")
    if not body:
        return ddl  # unexpected shape — execute as-is.
    # head looks like:    "CREATE TABLE schema.table_name"
    tokens = head.strip().split()
    if len(tokens) < 4:
        return ddl
    qualified = tokens[-1]
    return f'CREATE TABLE IF NOT EXISTS {qualified} ({body}'