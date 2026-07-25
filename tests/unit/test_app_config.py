"""Tests for db_project_manager.infrastructure.config.app_config."""

from __future__ import annotations

from db_project_manager.infrastructure.config.app_config import load_cfg


def test_load_cfg_defaults_when_none() -> None:
    cfg = load_cfg(None)
    assert cfg.paths.default_output_dir == "./output"
    assert cfg.paths.logs_dir == "./logs"
    assert cfg.logging.level == "INFO"
    assert cfg.default_connection_file is None


def test_load_cfg_from_dict() -> None:
    data = {
        "paths": {"default_output_dir": "/tmp/out", "logs_dir": "/tmp/logs"},
        "logging": {"level": "DEBUG", "console": False},
        "default_connection_file": "/tmp/conn.yaml",
        "unknown_key": "ignored",  # extra ignored
    }
    cfg = load_cfg(data)
    assert cfg.paths.default_output_dir == "/tmp/out"
    assert cfg.paths.logs_dir == "/tmp/logs"
    assert cfg.logging.level == "DEBUG"
    assert cfg.logging.console is False
    assert cfg.default_connection_file == "/tmp/conn.yaml"


def test_load_cfg_partial_uses_defaults(tmp_path) -> None:
    yaml_file = tmp_path / "config.yaml"
    yaml_file.write_text("paths:\n  default_output_dir: ./x\n", encoding="utf-8")
    cfg = load_cfg(yaml_file)
    assert cfg.paths.default_output_dir == "./x"
    # logging uses defaults
    assert cfg.logging.level == "INFO"
    assert cfg.logging.console is True


def test_load_cfg_empty_file(tmp_path) -> None:
    yaml_file = tmp_path / "empty.yaml"
    yaml_file.write_text("", encoding="utf-8")
    cfg = load_cfg(yaml_file)
    assert cfg.paths.default_output_dir == "./output"
