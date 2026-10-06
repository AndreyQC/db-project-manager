"""Tests for the MCP query/response JSONL log (Phase 19.1)."""

from __future__ import annotations

import json

import pytest
from loguru import logger

from db_project_manager.infrastructure.config.app_config import load_cfg
from db_project_manager.infrastructure.query_log import (
    QUERY_LOG_FILENAME,
    configure_query_log,
    log_db_call,
)


@pytest.fixture()
def query_log_env(tmp_path):
    """Isolated loguru sinks; everything removed on teardown."""
    logger.remove()
    configure_query_log(tmp_path, enabled=True, retention_days=14)
    yield tmp_path
    logger.remove()


def read_lines(logs_dir):
    return (logs_dir / QUERY_LOG_FILENAME).read_text(encoding="utf-8").splitlines()


def test_event_written_as_jsonl(query_log_env):
    log_db_call({"tool": "query", "connection": "local", "sql": "SELECT 1", "response": {"rows": []}})
    lines = read_lines(query_log_env)
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["tool"] == "query"
    assert event["sql"] == "SELECT 1"
    assert "ts" in event  # timestamp injected by the sink


def test_jsonb_response_serialized(query_log_env):
    rows = [{"id": 1, "doc": {"a": [1, 2]}, "amount": "12.50"}]
    log_db_call({"tool": "query", "connection": "c", "sql": "SELECT ...", "response": rows})
    event = json.loads(read_lines(query_log_env)[0])
    assert event["response"][0]["doc"] == {"a": [1, 2]}
    assert event["response"][0]["amount"] == "12.50"


def test_non_ascii_and_unserializable_values(query_log_env):
    log_db_call({"tool": "run_script", "connection": "c", "sql": "SELECT 'Привет'", "obj": {"x": 1}})
    raw_line = read_lines(query_log_env)[0]
    event = json.loads(raw_line)
    assert event["obj"] == {"x": 1}
    # Cyrillic stays readable in the file (ensure_ascii=False)
    assert "Привет" in raw_line


def test_regular_logs_do_not_land_in_query_log(query_log_env):
    logger.info("обычное приложение-сообщение")
    log_db_call({"tool": "query", "connection": "c", "sql": "SELECT 1"})
    lines = read_lines(query_log_env)
    assert len(lines) == 1
    assert "обычное" not in lines[0]


def test_disabled_sink_writes_nothing(tmp_path):
    logger.remove()
    configure_query_log(tmp_path, enabled=False)
    log_db_call({"tool": "query", "connection": "c", "sql": "SELECT 1"})
    assert not (tmp_path / QUERY_LOG_FILENAME).exists()
    logger.remove()


def test_config_defaults_and_override(tmp_path):
    cfg = load_cfg(None)
    assert cfg.logging.log_queries is True
    assert cfg.logging.queries_retention_days == 14

    import yaml

    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        yaml.safe_dump({"logging": {"log_queries": False, "queries_retention_days": 30}}),
        encoding="utf-8",
    )
    cfg2 = load_cfg(config_file)
    assert cfg2.logging.log_queries is False
    assert cfg2.logging.queries_retention_days == 30
