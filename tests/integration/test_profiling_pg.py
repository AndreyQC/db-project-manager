"""Phase 20 e2e: профайлинг на реальном PostgreSQL (testcontainers).

Требует Docker; запускается явно: uv run pytest -m integration
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from db_project_manager.application.profiling_service import ProfilingError, ProfilingService
from db_project_manager.domain.connection import ConnectionConfig

pytestmark = pytest.mark.integration

DDL = """
CREATE SCHEMA p20;
CREATE TABLE p20.sales (
    id bigserial PRIMARY KEY,
    region text,
    amount numeric(12,2),
    flag boolean,
    created_at timestamp
);
INSERT INTO p20.sales (region, amount, flag, created_at)
SELECT
    (ARRAY['east', 'west', 'north', 'south'])[1 + (i % 4)],
    (i % 100)::numeric,
    i % 2 = 0,
    '2026-01-01'::timestamp + (i || ' hours')::interval
FROM generate_series(1, 500) AS i;
INSERT INTO p20.sales (region, amount, flag, created_at) VALUES (NULL, NULL, NULL, NULL);
ANALYZE p20.sales;
"""


def _write_connection(dir_path: Path, name: str, cfg: ConnectionConfig, *, profiling: bool) -> Path:
    block = "\nprofiling:\n  enabled: true\n" if profiling else ""
    path = dir_path / f"{name}.yaml"
    path.write_text(
        f"host: {cfg.host}\nport: {cfg.port}\ndatabase: {cfg.database}\n"
        f"username: {cfg.username}\npassword: {cfg.password}\ntype: postgres\n{block}",
        encoding="utf-8",
    )
    return dir_path


@pytest.fixture()
def sales_db(pg_conn_cfg):
    from db_project_manager.infrastructure.database.registry import get_adapter

    adapter = get_adapter(pg_conn_cfg)
    adapter.connect(pg_conn_cfg)
    adapter.execute_script(DDL)
    yield pg_conn_cfg
    adapter.disconnect()


def test_profile_table_end_to_end(sales_db, tmp_path):
    conns = _write_connection(tmp_path, "p20", sales_db, profiling=True)
    service = ProfilingService(connections_dir=str(conns))

    profiles = service.profile_tables("p20", ["p20.sales"])

    assert len(profiles) == 1
    tp = profiles[0]
    assert (tp.schema_name, tp.table_name) == ("p20", "sales")
    assert tp.row_count == 501
    assert tp.stats_fresh is True
    assert tp.sampled is False
    assert tp.warnings == []
    cols = {c.name: c for c in tp.columns}
    assert cols["region"].distinct_count == 4
    assert cols["region"].top_values[0]["count"] == 125
    assert cols["region"].top_values[0]["value"] in ("east", "west", "north", "south")
    assert cols["region"].null_count == 1
    assert cols["amount"].percentiles["p50"] == pytest.approx(50.0, abs=1.0)
    assert sum(h["count"] for h in cols["amount"].histogram) == 500
    assert {v["value"] for v in cols["flag"].top_values} == {True, False}
    assert cols["created_at"].min_value is not None
    # Профиль сериализуем в JSON целиком (литералы данных строкифицированы адаптером)
    json.dumps(tp.model_dump(), ensure_ascii=False)


def test_profiling_disabled_connection_refused(sales_db, tmp_path):
    conns = _write_connection(tmp_path, "no_prof", sales_db, profiling=False)
    service = ProfilingService(connections_dir=str(conns))
    with pytest.raises(ProfilingError, match="profiling"):
        service.profile_tables("no_prof", ["p20.sales"])
