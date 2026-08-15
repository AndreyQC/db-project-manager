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
from db_project_manager.domain.diff import DiffReport, SnapshotSourceKind, StateSnapshot
from db_project_manager.infrastructure.config.codebase_manifest import (
    ManifestError,
    read_manifest,
)
from db_project_manager.infrastructure.database.base import DatabaseAdapter, DatabaseError
from db_project_manager.infrastructure.database.registry import get_adapter
from db_project_manager.infrastructure.diff.comparator import compare
from db_project_manager.infrastructure.diff.snapshot import build_snapshot_from_dir

SOURCE_FILENAME = "source.json"
TARGET_FILENAME = "target.json"
DIFF_REPORT_FILENAME = "diff_report.json"


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
    ) -> None:
        self._reverse_engineer = reverse_engineer or ReverseEngineerService()
        self._adapter_factory = adapter_factory or get_adapter
        self._graph_service = graph_service or BuildGraphService()

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
            src_snap = self._build_side(source, "source", temp_dirs, keep_model_dir, progress)

            self._emit(progress, "Подготовка снимка target…", 1, 4)
            tgt_snap = self._build_side(target, "target", temp_dirs, keep_model_dir, progress)

            if src_snap.db_type != tgt_snap.db_type:
                raise CompareError(
                    f"Несовместимые типы БД: source={src_snap.db_type}, "
                    f"target={tgt_snap.db_type}. Сравнение допускается только для "
                    f"одинаковых типов (PG↔PG, GP↔GP)."
                )

            self._emit(progress, "Сравнение снимков…", 2, 4)
            report = compare(src_snap, tgt_snap)

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
    ) -> StateSnapshot:
        if side.kind == SnapshotSourceKind.DIR:
            return self._build_dir_side(side)
        return self._build_db_side(side, label, temp_dirs, progress)

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
    ) -> StateSnapshot:
        """Reverse-engineer the DB into a temp dir, then build the snapshot."""
        if side.conn_cfg is None:
            raise CompareError(f"SideSpec {label} (DB) is missing conn_cfg")

        temp_root = Path(tempfile.mkdtemp(prefix=f"dbpm_compare_{label}_"))
        temp_dirs.append(temp_root)
        conn_cfg = side.conn_cfg

        # Reverse-engineer writes <temp_root>/<database>/ + dbpm.manifest.json.
        try:
            self._reverse_engineer.run(conn_cfg, temp_root, progress=progress)
        except ReverseEngineerError as e:
            raise CompareError(f"Reverse-engineer стороны {label} не удался: {e}") from e

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
