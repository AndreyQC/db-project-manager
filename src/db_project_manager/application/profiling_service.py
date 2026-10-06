"""Table profiling service (Phase 20, PF-1..PF-5).

Policy (final 20261006_001): opt-in через блок ``profiling:`` подключения —
нет блока/``enabled: false`` → отказ до единого запроса к БД. Все запросы —
read-only агрегаты, исполняются через ``adapter.run_query`` (RO-транзакция +
statement_timeout Phase 19) и прогоняются через sqlglot-классификатор как
guard от порчи генератора. ANALYZE никогда не запускается: порог «полный
проход vs сэмпл» — по ``pg_total_relation_size`` (статистики не требует),
``reltuples`` — только для расчёта доли сэмпла, без статистики —
``default_sample_fraction``; в профиль уходит ``stats_fresh`` и совет
«ANALYZE вручную».
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from db_project_manager.application.mcp_service import ConnectionManager
from db_project_manager.domain.connection import ConnectionConfig, ProfilingSettings
from db_project_manager.domain.profiling import ColumnProfile, TableProfile
from db_project_manager.infrastructure.database.base import DatabaseError
from db_project_manager.infrastructure.profiling.base import (
    ColumnCategory,
    PERCENTILE_FRACTIONS,
    PERCENTILE_KEYS,
    ProfilingColumn,
    ProfilingSQLGenerator,
    TableMeta,
    categorize,
)
from db_project_manager.infrastructure.sql.classify import classify_script, dialect_for

#: Максимальное число колонок, читаемое из каталога за один профайлинг.
MAX_COLUMNS = 4000


class ProfilingError(Exception):
    """Profiling refusal or failure (policy gate, validation, execution)."""


class ProfilingService:
    """Builds :class:`TableProfile` for a table list over one connection."""

    def __init__(self, connections_dir: str = "connections", manager: ConnectionManager | None = None) -> None:
        self._manager = manager if manager is not None else ConnectionManager(connections_dir)

    # --- public API ---

    def profile_tables(self, connection: str, tables: list[str]) -> list[TableProfile]:
        """Profile each ``schema.table``; opt-in gate fires before any query."""
        cfg = self._manager.load_config(connection)
        settings = cfg.profiling_settings
        if not settings.enabled:
            raise ProfilingError(
                "профайлинг запрещён для этого подключения: блок profiling: отсутствует "
                "или enabled=false (файл подключения connections/*.yaml)"
            )
        profiler = get_profiler_or_error(cfg)
        timeout_s = max(1, settings.statement_timeout_ms // 1000)
        profiles: list[TableProfile] = []
        with self._manager.connection(connection) as conn:
            for raw in tables:
                schema, table = parse_qualified(raw)
                logger.info(f"profile: {schema}.{table} ({cfg.type})")
                profiles.append(
                    self._profile_one(
                        conn.adapter, profiler, cfg, settings, connection, schema, table, timeout_s
                    )
                )
        return profiles

    # --- internals ---

    def _profile_one(
        self,
        adapter: Any,
        profiler: ProfilingSQLGenerator,
        cfg: ConnectionConfig,
        settings: ProfilingSettings,
        connection: str,
        schema: str,
        table: str,
        timeout_s: int,
    ) -> TableProfile:
        started = time.perf_counter()
        warnings: list[str] = []
        what = f"{schema}.{table}"

        columns = self._load_columns(adapter, cfg, profiler, schema, table, timeout_s)
        col_profiles = {c.name: ColumnProfile(name=c.name, data_type=c.data_type, nullable=c.nullable) for c in columns}

        meta = self._load_meta(adapter, cfg, profiler, schema, table, timeout_s)
        size_bytes = meta.size_bytes
        sampled = (size_bytes or 0) > settings.full_size_threshold_bytes
        fraction = self._sample_fraction(settings, meta)
        if not meta.stats_present:
            warnings.append(
                "статистика не собрана (ANALYZE не выполнялся): reltuples отсутствует — "
                "доля сэмпла = default_sample_fraction; рекомендуется запустить ANALYZE вручную"
            )

        base = profiler.base_expr(schema, table, fraction if sampled else None)
        row = self._main_rows(adapter, cfg, profiler, columns, base, what, timeout_s, settings.column_chunk_size)
        row_count = _as_int(row.get("__row_count")) or 0
        if sampled and row_count == 0 and fraction is not None:
            warnings.append(
                "TABLESAMPLE вернул 0 строк (мало страниц) — повтор сэмпла через random() < p"
            )
            base = profiler.sample_base_expr_random(schema, table, fraction)
            row = self._main_rows(adapter, cfg, profiler, columns, base, what, timeout_s, settings.column_chunk_size)
            row_count = _as_int(row.get("__row_count")) or 0

        for col in columns:
            self._fill_column_metrics(col_profiles[col.name], col, row, row_count)

        if row_count:
            self._collect_histograms_and_topn(
                adapter, cfg, profiler, columns, col_profiles, row, base, what, settings, timeout_s
            )
        elif sampled:
            warnings.append("сэмпл пуст — метрики колонок не собраны")

        return TableProfile(
            connection=connection,
            schema_name=schema,
            table_name=table,
            generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            row_count=row_count,
            estimated_rows=meta.reltuples if (meta.reltuples is not None and meta.reltuples >= 0) else None,
            size_bytes=size_bytes,
            sampled=sampled,
            stats_fresh=meta.stats_present,
            duration_ms=int((time.perf_counter() - started) * 1000),
            warnings=warnings,
            columns=[col_profiles[c.name] for c in columns],
        )

    def _load_columns(
        self, adapter: Any, cfg: ConnectionConfig, profiler: ProfilingSQLGenerator,
        schema: str, table: str, timeout_s: int,
    ) -> list[ProfilingColumn]:
        result = self._run(
            adapter, cfg, profiler.columns_sql(schema, table),
            f"каталог колонок {schema}.{table}", max_rows=MAX_COLUMNS, timeout_s=timeout_s,
        )
        if not result.rows:
            raise ProfilingError(
                f"таблица {schema}.{table} не найдена (проверьте квалификацию schema.table и права)"
            )
        columns: list[ProfilingColumn] = []
        for row in result.rows:
            name = str(row.get("column_name"))
            data_type = str(row.get("data_type") or "")
            columns.append(
                ProfilingColumn(
                    name=name,
                    data_type=data_type,
                    nullable=_opt_bool(row.get("nullable")),
                    category=categorize(data_type),
                )
            )
        return columns

    def _load_meta(
        self, adapter: Any, cfg: ConnectionConfig, profiler: ProfilingSQLGenerator,
        schema: str, table: str, timeout_s: int,
    ) -> TableMeta:
        result = self._run(
            adapter, cfg, profiler.table_meta_sql(schema, table),
            f"метаданные {schema}.{table}", max_rows=1, timeout_s=timeout_s,
        )
        if not result.rows:
            raise ProfilingError(
                f"таблица {schema}.{table} не найдена в pg_class "
                "(поддерживаются обычные и партиционированные таблицы, relkind r/p)"
            )
        return profiler.parse_meta(result.rows[0])

    def _main_rows(
        self, adapter: Any, cfg: ConnectionConfig, profiler: ProfilingSQLGenerator,
        columns: list[ProfilingColumn], base: str, what: str, timeout_s: int,
        chunk_size: int = 50,
    ) -> dict[str, Any]:
        """Run the one-scan aggregate per column chunk; merged row dict."""
        merged: dict[str, Any] = {}
        for start in range(0, len(columns), chunk_size):
            chunk = columns[start:start + chunk_size]
            result = self._run(
                adapter, cfg, profiler.main_sql(chunk, base),
                f"агрегаты {what}", max_rows=1, timeout_s=timeout_s,
            )
            if result.rows:
                merged.update(result.rows[0])
        return merged

    def _collect_histograms_and_topn(
        self, adapter: Any, cfg: ConnectionConfig, profiler: ProfilingSQLGenerator,
        columns: list[ProfilingColumn], col_profiles: dict[str, ColumnProfile],
        row: dict[str, Any], base: str, what: str, settings: ProfilingSettings, timeout_s: int,
    ) -> None:
        for col in columns:
            cp = col_profiles[col.name]
            prefix = f"{col.name}__"
            lo, hi = row.get(prefix + "min"), row.get(prefix + "max")
            if col.category in (ColumnCategory.NUMERIC, ColumnCategory.TEMPORAL) and lo is not None and hi is not None:
                hist_sql = profiler.histogram_sql(col, lo, hi, settings.histogram_buckets, base)
                if hist_sql is not None:
                    result = self._run(
                        adapter, cfg, hist_sql,
                        f"гистограмма {what}.{col.name}",
                        max_rows=settings.histogram_buckets + 5, timeout_s=timeout_s,
                    )
                    cp.histogram = [
                        {"bucket": int(r["bucket"]), "count": int(r["cnt"])}
                        for r in result.rows
                        if r.get("bucket") is not None
                    ]
            if col.category in (ColumnCategory.TEXT, ColumnCategory.BOOL, ColumnCategory.TEMPORAL):
                result = self._run(
                    adapter, cfg, profiler.topn_sql(col, settings.top_n, base),
                    f"top-N {what}.{col.name}", max_rows=settings.top_n, timeout_s=timeout_s,
                )
                cp.top_values = [
                    {"value": r.get("value"), "count": int(r["cnt"])}
                    for r in result.rows
                    if r.get("cnt") is not None
                ]

    def _fill_column_metrics(self, cp: ColumnProfile, col: ProfilingColumn, row: dict[str, Any], row_count: int) -> None:
        prefix = f"{col.name}__"
        cp.null_count = _as_int(row.get(prefix + "notnull"))
        if row_count and cp.null_count is not None:
            cp.null_frac = round(1.0 - cp.null_count / row_count, 6)
        cp.distinct_count = _as_int(row.get(prefix + "distinct"))
        if col.category in (ColumnCategory.NUMERIC, ColumnCategory.TEMPORAL, ColumnCategory.TEXT):
            cp.min_value = row.get(prefix + "min")
            cp.max_value = row.get(prefix + "max")
        if col.category == ColumnCategory.NUMERIC:
            cp.avg_value = _as_float(row.get(prefix + "avg"))
            cp.stddev = _as_float(row.get(prefix + "stddev"))
            cp.percentiles = {
                PERCENTILE_KEYS[frac]: _as_float_or_raw(row.get(prefix + PERCENTILE_KEYS[frac]))
                for frac in PERCENTILE_FRACTIONS
            }
        if col.category == ColumnCategory.TEXT:
            cp.avg_length = _as_float(row.get(prefix + "avglen"))
            cp.max_length = _as_int(row.get(prefix + "maxlen"))

    def _sample_fraction(self, settings: ProfilingSettings, meta: TableMeta) -> float | None:
        """Sample fraction for big tables; None → full pass is affordable."""
        reltuples = meta.reltuples
        if reltuples is not None and reltuples > 0:
            fraction = settings.sample_rows / reltuples
            if fraction >= 0.99:
                return None
            return max(fraction, 1e-6)
        return settings.default_sample_fraction

    def _run(self, adapter: Any, cfg: ConnectionConfig, sql: str, what: str, *, max_rows: int, timeout_s: int) -> Any:
        verdict = classify_script(sql, dialect=dialect_for(cfg.type))
        if len(verdict.statements) != 1 or not verdict.is_read_only:
            raise ProfilingError(
                f"внутренняя ошибка генератора ({what}): сгенерирован не read-only SQL — {verdict.summary()}"
            )
        try:
            return adapter.run_query(sql, max_rows=max_rows, timeout_s=timeout_s, readonly=True)
        except DatabaseError as exc:
            raise ProfilingError(f"{what}: {exc}") from exc


def get_profiler_or_error(cfg: ConnectionConfig) -> ProfilingSQLGenerator:
    """Registry lookup with a policy-friendly error for unknown db types."""
    from db_project_manager.infrastructure.profiling import get_profiler

    try:
        return get_profiler(cfg.type)
    except ValueError as exc:
        raise ProfilingError(str(exc)) from exc


def parse_qualified(raw: str) -> tuple[str, str]:
    """``schema.table`` → (schema, table) с поддержкой квотированных имён.

    Правила идентификаторов PostgreSQL: двойные кавычки снимаются, ``""``
    внутри кавычек — экранирование литеральной кавычки, точка внутри кавычек
    не делит имя (``"my.schema".tbl``), неквотированные части приводятся к
    нижнему регистру (фолдинг планировщика). Неквотированный второй разделитель
    (три и более частей) — ошибка: точка в неквотированном имени не поддерживается.
    """
    name = (raw or "").strip()
    if not name:
        raise ProfilingError("имя таблицы пусто; ожидается schema.table")
    parts = _split_identifiers(name)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ProfilingError(
            f"имя таблицы {raw!r} должно быть квалифицированным: schema.table "
            '(поддерживаются "квотированные" идентификаторы: "My Schema"."My.Table")'
        )
    return parts[0], parts[1]


def _split_identifiers(name: str) -> list[str]:
    """Разбирает qualified-имя на части, уважая двойные кавычки (стандарт SQL).

    Квотированная часть сохраняет регистр и пробелы; неквотированная —
    strip + нижний регистр (фолдинг планировщика PostgreSQL).
    """
    parts: list[str] = []
    buf: list[str] = []
    in_quotes = False
    was_quoted = False
    i = 0
    while i < len(name):
        ch = name[i]
        if in_quotes:
            if ch == '"':
                if name[i + 1: i + 2] == '"':
                    buf.append('"')
                    i += 2
                    continue
                in_quotes = False
            else:
                buf.append(ch)
        elif ch == '"':
            in_quotes = True
            was_quoted = True
        elif ch == ".":
            parts.append("".join(buf) if was_quoted else "".join(buf).strip().lower())
            buf = []
            was_quoted = False
        else:
            buf.append(ch)
        i += 1
    if in_quotes:
        raise ProfilingError(f"незакрытая двойная кавычка в имени {name!r}")
    parts.append("".join(buf) if was_quoted else "".join(buf).strip().lower())
    return parts


def _opt_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "t", "1")
    return None


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_float_or_raw(value: Any) -> Any:
    """Numeric percentile as float; anything exotic (ISO text) kept as-is."""
    as_float = _as_float(value)
    return as_float if as_float is not None else value
