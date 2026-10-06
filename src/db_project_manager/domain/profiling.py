"""Domain models for table profiling (Phase 20).

Профайлинг — read-only: агрегаты считает сама БД, наружу выкачиваются только
итоги (одна строка агрегатов на таблицу, гистограммы/top-N — до пары сотен
строк). Все значения JSON-сериализуемы: типы драйвера (Decimal, datetime,
memoryview) строкифицирует адаптер при выборке.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ColumnProfile(BaseModel):
    """Aggregated statistics of one table column."""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(description="Имя колонки (как в каталоге)")
    data_type: str = Field(description="Сырой тип из information_schema.columns")
    nullable: bool | None = Field(default=None, description="Допускает NULL (из каталога)")

    null_count: int | None = Field(default=None, description="Число NULL")
    null_frac: float | None = Field(default=None, description="Доля NULL (0..1)")
    distinct_count: int | None = Field(default=None, description="COUNT(DISTINCT); на сэмпле — оценка")

    min_value: Any = Field(default=None, description="min (строкифицировано адаптером)")
    max_value: Any = Field(default=None, description="max")
    avg_value: float | None = Field(default=None, description="avg — только числовые")
    stddev: float | None = Field(default=None, description="stddev_samp — только числовые")

    percentiles: dict[str, Any] = Field(
        default_factory=dict,
        description="Перцентили p01/p25/p50/p75/p99 (числовые и временные колонки)",
    )
    histogram: list[dict[str, Any]] = Field(
        default_factory=list,
        description='Гистограмма width_bucket: [{"bucket": 1, "count": 42}, ...]; '
        "ширина корзины = (max - min) / histogram_buckets",
    )
    top_values: list[dict[str, Any]] = Field(
        default_factory=list,
        description='Top-N частот: [{"value": ..., "count": ...}, ...] (текст, bool, даты)',
    )
    avg_length: float | None = Field(default=None, description="avg(length(col)) — текстовые")
    max_length: int | None = Field(default=None, description="max(length(col)) — текстовые")


class TableProfile(BaseModel):
    """Profiling result of one table (JSON-отчёт CLI и MCP-инструмента)."""

    model_config = ConfigDict(extra="ignore")

    connection: str = Field(description="Имя подключения")
    schema_name: str
    table_name: str
    generated_at: str = Field(description="ISO UTC момент сборки профиля")

    row_count: int | None = Field(default=None, description="Точный count(*) (на сэмпле — сэмпл)")
    estimated_rows: int | None = Field(default=None, description="Оценка pg_class.reltuples")
    size_bytes: int | None = Field(default=None, description="pg_total_relation_size")

    sampled: bool = Field(default=False, description="Дорогие метрики считались по сэмплу")
    stats_fresh: bool | None = Field(
        default=None,
        description="Есть ли статистика (ANALYZE): PG — last_analyze, GP — pg_stats",
    )
    duration_ms: int = Field(default=0, description="Полное время профайлинга таблицы")
    warnings: list[str] = Field(default_factory=list)
    columns: list[ColumnProfile] = Field(default_factory=list)
