"""Orchestration service for the compare feature (Phase 9).

The compare feature diffs two *sides*: each is either a live database connection
or a reverse-engineer codebase directory on disk. This service normalizes both
sides into a :class:`~db_project_manager.domain.diff.StateSnapshot`, checks that
their db_types are compatible, runs the comparator, and writes the report.

For a DB side, the service reverse-engineers into a temp directory (cleaned up
in ``finally`` unless ``keep_model_dir`` is set — the same escape hatch as
``--keep-db`` in deploy validate). The manifest written by reverse-engineer
(Phase 9 / S02) is read back to confirm the db_type. For a DIR side, the
manifest must already be present (old trees without it are rejected with a clear
message).

Pipeline (draft §3.6):
    1. build source snapshot (DIR: read manifest + snapshot; DB: temp RE + rows)
    2. build target snapshot (same)
    3. check db_type equality (hard error on mismatch)
    4. compare
    5. write source.json / target.json / diff_report.json to output_dir
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from db_project_manager.application.graph_service import BuildGraphService
from db_project_manager.application.reverse_engineer import (
    ProgressCallback,
    ReverseEngineerError,
    ReverseEngineerService,
)
from db_project_manager.domain.connection import ConnectionConfig
from db_project_manager.domain.diff import (
    DiffReport,
    IGNORED_BUILD_FALSE_KEY,
    SnapshotSourceKind,
    StateSnapshot,
)
from db_project_manager.infrastructure.config.codebase_manifest import (
    ManifestError,
    read_manifest,
)
from db_project_manager.infrastructure.database.base import DatabaseAdapter, DatabaseError
from db_project_manager.infrastructure.database.registry import get_adapter
from db_project_manager.infrastructure.deploy.canonical_ddl import DEFAULT_SERVICE_SCHEMA
from db_project_manager.infrastructure.diff.comparator import compare, identity_key
from db_project_manager.infrastructure.diff.snapshot import build_snapshot_from_dir

SOURCE_FILENAME = "source.json"
TARGET_FILENAME = "target.json"
DIFF_REPORT_FILENAME = "diff_report.json"

#: Summary key: how many service-schema objects were excluded from the diff
#: (Phase 16.8) — parity with ``ignored_build_false``.
IGNORED_SERVICE_SCHEMA_KEY = "ignored_service_schema"

#: Summary key: implicit serial sequences folded into their columns (16.10).
IGNORED_SERIAL_SEQUENCES_KEY = "ignored_serial_sequences"

#: Canonical integer type names (columns.py ``_TYPE_ALIASES`` folds int4/int8/
#: int2/serial* here): only integer columns can own an implicit serial sequence.
_INT_TYPES = frozenset({"int", "integer", "int4", "bigint", "int8", "smallint", "int2"})


class CompareError(Exception):
    """Raised on incompatible db_types, missing manifest, or a bad side spec."""


@dataclass(frozen=True)
class SideSpec:
    """One comparison side: either a live DB or a codebase directory.

    For ``kind=DB``, ``conn_cfg`` must be set and ``ref`` is the connection-file path.
    For ``kind=DIR``, ``conn_cfg`` is None and ``ref`` is the codebase directory.
    """

    kind: SnapshotSourceKind
    ref: str
    conn_cfg: ConnectionConfig | None = None


class CompareService:
    """Orchestrates snapshot building, db_type check, comparison, and report writing."""

    def __init__(
        self,
        reverse_engineer: ReverseEngineerService | None = None,
        adapter_factory: Callable[[ConnectionConfig], DatabaseAdapter] | None = None,
        graph_service: BuildGraphService | None = None,
        service_schema: str = DEFAULT_SERVICE_SCHEMA,
    ) -> None:
        self._reverse_engineer = reverse_engineer or ReverseEngineerService()
        self._adapter_factory = adapter_factory or get_adapter
        self._graph_service = graph_service or BuildGraphService()
        self._service_schema = service_schema

    def run(
        self,
        source: SideSpec,
        target: SideSpec,
        output_dir: str | Path,
        *,
        keep_model_dir: bool = False,
        progress: ProgressCallback | None = None,
    ) -> Path:
        """Run the comparison and write the report to ``output_dir``.

        Returns the output directory path. Raises :class:`CompareError` on
        db_type mismatch or a missing manifest.
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        temp_dirs: list[Path] = []
        try:
            self._emit(progress, "Подготовка снимка source…", 0, 4)
            src_snap = self._build_side(source, "source", temp_dirs, keep_model_dir, progress, output_dir)

            self._emit(progress, "Подготовка снимка target…", 1, 4)
            tgt_snap = self._build_side(target, "target", temp_dirs, keep_model_dir, progress, output_dir)

            if src_snap.db_type != tgt_snap.db_type:
                raise CompareError(
                    f"Несовместимые типы БД: source={src_snap.db_type}, "
                    f"target={tgt_snap.db_type}. Сравнение допускается только для "
                    f"одинаковых типов (PG↔PG, GP↔GP)."
                )

            ignored = self._exclude_build_false(src_snap, tgt_snap)
            ignored_service = self._exclude_service_schema(src_snap, tgt_snap)
            ignored_serial = self._exclude_serial_sequences(src_snap, tgt_snap)

            self._emit(progress, "Сравнение снимков…", 2, 4)
            report = compare(src_snap, tgt_snap)
            if ignored:
                report.summary[IGNORED_BUILD_FALSE_KEY] = ignored
            if ignored_service:
                report.summary[IGNORED_SERVICE_SCHEMA_KEY] = ignored_service
            if ignored_serial:
                report.summary[IGNORED_SERIAL_SEQUENCES_KEY] = ignored_serial

            self._emit(progress, "Запись отчёта…", 3, 4)
            self._write_report(report, output_dir)

            logger.info(
                f"Сравнение завершено: added={report.summary.get('added', 0)}, "
                f"removed={report.summary.get('removed', 0)}, "
                f"changed={report.summary.get('changed', 0)}, "
                f"unchanged={report.summary.get('unchanged', 0)}. "
                f"Отчёт: {output_dir}"
            )
            return output_dir
        finally:
            if not keep_model_dir:
                for d in temp_dirs:
                    shutil.rmtree(d, ignore_errors=True)

    # --- per-side snapshot build ---

    def _build_side(
        self,
        side: SideSpec,
        label: str,
        temp_dirs: list[Path],
        keep_model_dir: bool,  # noqa: ARG002 — kept for symmetry / future per-side flags
        progress: ProgressCallback | None,
        output_dir: Path,
    ) -> StateSnapshot:
        if side.kind == SnapshotSourceKind.DIR:
            return self._build_dir_side(side)
        return self._build_db_side(side, label, temp_dirs, progress, output_dir)

    def _build_dir_side(self, side: SideSpec) -> StateSnapshot:
        """Read the manifest from a codebase dir, then build the snapshot."""
        try:
            manifest = read_manifest(side.ref)
        except ManifestError as e:
            raise CompareError(str(e)) from e
        return build_snapshot_from_dir(
            side.ref,
            source_kind=SnapshotSourceKind.DIR,
            source_ref=side.ref,
            db_type=manifest.db_type,
            graph_service=self._graph_service,
        )

    def _build_db_side(
        self,
        side: SideSpec,
        label: str,
        temp_dirs: list[Path],
        progress: ProgressCallback | None,
        output_dir: Path,
    ) -> StateSnapshot:
        """Reverse-engineer the DB into a temp dir, then build the snapshot."""
        if side.conn_cfg is None:
            raise CompareError(f"SideSpec {label} (DB) missing conn_cfg")

        temp_root = Path(tempfile.mkdtemp(prefix=f"dbpm_compare_{label}_"))
        temp_dirs.append(temp_root)
        conn_cfg = side.conn_cfg

        # Reverse-engineer writes <temp_root>/<database>/ + dbpm.manifest.json.
        try:
            self._reverse_engineer.run(conn_cfg, temp_root, progress=progress)
        except ReverseEngineerError as e:
            raise CompareError(f"Reverse-engineer стороны {label} не удался: {e}") from e

        # Phase 15.7 (BACKLOG P3): keep what the RE actually read from the DB in
        # the run dir instead of losing it with the temp dir — this is the
        # artifact that makes false-positive CHANGED diagnosis possible. Copied
        # right after RE so it survives even a later snapshot-build failure.
        self._copy_snapshot_dir(temp_root, output_dir / label)

        codebase_dir = temp_root / conn_cfg.database
        # Confirm the manifest was written by reverse-engineer (db_type comes from
        # conn_cfg, but we read the manifest to fail fast if it is missing/corrupt).
        try:
            read_manifest(codebase_dir)
        except ManifestError as e:
            raise CompareError(str(e)) from e

        # Row counts from the live DB (informational marker for tables).
        row_counts: dict[tuple[str, str], int | float | None] = {}
        adapter = self._adapter_factory(conn_cfg)
        try:
            adapter.connect(conn_cfg)
            try:
                for row in adapter.get_table_row_counts():
                    row_counts[(row["schema_name"], row["table_name"])] = row["estimated_rows"]
            finally:
                adapter.disconnect()
        except DatabaseError as e:
            logger.warning(f"Не удалось получить row counts стороны {label}: {e}")

        return build_snapshot_from_dir(
            codebase_dir,
            source_kind=SnapshotSourceKind.DB,
            source_ref=conn_cfg.name or conn_cfg.database,
            db_type=conn_cfg.type,
            row_counts=row_counts,
            graph_service=self._graph_service,
        )

    # --- service-schema exclusion (Phase 16.8) + build=false (Phase 15.7) ---

    # --- serial-sequence folding (Phase 16.10) ---

    @staticmethod
    def _exclude_serial_sequences(src_snap: StateSnapshot, tgt_snap: StateSnapshot) -> int:
        """Drop implicitly-owned serial sequences from both snapshots (in place).

        A sequence named ``<table>_<col>_seq`` where the same snapshot has that
        table with an integer ``<col>`` and no/default-nextval default is the
        implicit artefact of a SERIAL column — the pg_dump model folds it into
        the column and never emits it as a standalone object. Diffing it is
        harmful in both directions: the codebase (serial4 spelling) never
        declares it → REMOVED → with ``--include-drops`` a DROP SEQUENCE that
        fails on the column ownership (no CASCADE), without the flag a CD-11
        block (ALT-6) — exactly the cis_zup_gp_dev 2026-09-14 deadlock.

        Known trade-off: a deliberately hand-tuned sequence that happens to
        match ``<table>_<col>_seq`` + integer column is also folded — its
        START/INCREMENT drift stops being diffed (pg_dump has the same blind
        spot). Rename it to break the pattern if it must be tracked.

        Returns:
            The number of excluded object identities.
        """
        excluded: set[str] = set()
        for snap in (src_snap, tgt_snap):
            sequences = {
                (o.object_schema, o.object_name): key
                for key, o in snap.objects.items()
                if o.object_type == "sequence"
            }
            for obj in snap.objects.values():
                if obj.object_type != "table":
                    continue
                for column in obj.columns or []:
                    expected = f"{obj.object_name}_{column.name}_seq"
                    seq_key = sequences.get((obj.object_schema, expected))
                    if seq_key is None or column.type not in _INT_TYPES:
                        continue
                    if column.default is None or expected in (column.default or ""):
                        excluded.add(identity_key(seq_key))
        if not excluded:
            return 0
        for snap in (src_snap, tgt_snap):
            for key in [k for k in snap.objects if identity_key(k) in excluded]:
                del snap.objects[key]
            snap.edges = [
                edge
                for edge in snap.edges
                if identity_key(edge.source_object_key) not in excluded
                and identity_key(edge.destination_object_key) not in excluded
            ]
        logger.info(f"Имплицитные serial-последовательности исключены из сравнения: {len(excluded)}.")
        return len(excluded)

    def _exclude_service_schema(self, src_snap: StateSnapshot, tgt_snap: StateSnapshot) -> int:
        """Drop the deploy service schema objects from both snapshots (in place).

        The service schema (default ``__deploy``) is db-pm's own runtime state —
        the deploy journal (``schema_version``/``script_history``/
        ``script_audit_log``) maintained by the tool itself (canonical_ddl.py,
        ``immutable`` markers). Diffing it against the codebase is a category
        error: the canonical seeded DDL can never hash-match the catalog render
        (IF NOT EXISTS / inline SERIAL vs named constraint / serial4 /
        DISTRIBUTED — live finding 2026-09-14), which turned the service tables
        into blocked ALTERs (CD-11) on Greenplum. Same treatment as GP admin
        schemas (LESSONS §71): tool-owned schemas are never compared.

        Warns (per side, on every run) when the service schema is absent —
        a side without it cannot carry the deploy journal.

        Returns:
            The number of excluded object identities.
        """
        schema = self._service_schema
        for label, snap in (("source", src_snap), ("target", tgt_snap)):
            present = any(o.object_schema == schema for o in snap.objects.values())
            if not present:
                logger.warning(
                    f"Сервис-схема '{schema}' отсутствует на стороне {label}: "
                    "деплой-журнал (schema_version/script_history/script_audit_log) "
                    "на этой стороне не ведётся. deploy apply создаст её при необходимости "
                    "(service_schema_initializer)."
                )
        excluded = {
            identity_key(key)
            for snap in (src_snap, tgt_snap)
            for key, obj in snap.objects.items()
            if obj.object_schema == schema
        }
        if not excluded:
            return 0
        for snap in (src_snap, tgt_snap):
            for key in [k for k in snap.objects if identity_key(k) in excluded]:
                del snap.objects[key]
            snap.edges = [
                edge
                for edge in snap.edges
                if identity_key(edge.source_object_key) not in excluded
                and identity_key(edge.destination_object_key) not in excluded
            ]
        logger.info(f"Сервис-схема '{schema}' исключена из сравнения: {len(excluded)} объектов.")
        return len(excluded)

    @staticmethod
    def _exclude_build_false(src_snap: StateSnapshot, tgt_snap: StateSnapshot) -> int:
        """Drop ``build=false`` objects from both snapshots (in place).

        Objects marked ``project.build: false`` in autodoc are not managed by
        the deploy tooling — ``deploy validate``/``plan`` already skip them.
        Excluding them from BOTH sides (matched by catalog-insensitive
        :func:`~...comparator.identity_key`) prevents two failure modes:
        reporting them as CHANGED (noise/violations in ``deploy analyze``) or,
        if only the source side were filtered, as REMOVED. Edges touching an
        excluded object are dropped from both sides too, else the edge diff
        would show phantom changes. RE-generated autodoc always writes
        ``build: true``, so in practice the DIR side drives the exclusion.

        Returns:
            The number of excluded object identities.
        """
        excluded = {
            identity_key(key)
            for snap in (src_snap, tgt_snap)
            for key, obj in snap.objects.items()
            if obj.build is False
        }
        if not excluded:
            return 0
        for snap in (src_snap, tgt_snap):
            for key in [k for k in snap.objects if identity_key(k) in excluded]:
                del snap.objects[key]
            snap.edges = [
                edge
                for edge in snap.edges
                if identity_key(edge.source_object_key) not in excluded
                and identity_key(edge.destination_object_key) not in excluded
            ]
        return len(excluded)

    @staticmethod
    def _copy_snapshot_dir(temp_root: Path, dest: Path) -> None:
        """Copy a DB-side RE snapshot into the run dir (Phase 15.7, BACKLOG P3).

        Best-effort diagnostics aid: a copy failure is logged and never fails
        the comparison. Repeated compares into the same run dir overwrite
        (``dirs_exist_ok=True``) — the latest DB read wins.
        """
        try:
            shutil.copytree(temp_root, dest, dirs_exist_ok=True)
        except OSError as e:
            logger.warning(f"Не удалось сохранить RE-snapshot стороны в {dest}: {e}")

    # --- report writing ---

    @staticmethod
    def _write_report(report: DiffReport, output_dir: Path) -> None:
        """Write source.json, target.json, diff_report.json (pretty JSON)."""
        (output_dir / SOURCE_FILENAME).write_text(
            report.source.model_dump_json(indent=2), encoding="utf-8"
        )
        (output_dir / TARGET_FILENAME).write_text(
            report.target.model_dump_json(indent=2), encoding="utf-8"
        )
        (output_dir / DIFF_REPORT_FILENAME).write_text(
            report.model_dump_json(indent=2), encoding="utf-8"
        )

    @staticmethod
    def _emit(progress: ProgressCallback | None, message: str, current: int, total: int) -> None:
        if progress is not None:
            progress(message, current, total)


def parse_row_counts(rows: list[dict[str, Any]]) -> dict[tuple[str, str], int | float | None]:
    """Helper to turn adapter row-count rows into a {(schema, table): count} dict.

    Public so tests can construct expected row-count maps without duplicating logic.
    """
    return {(r["schema_name"], r["table_name"]): r["estimated_rows"] for r in rows}
