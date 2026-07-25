"""Unit tests for GuiSettingsStore (gui_settings.json persistence)."""

import json
import logging

from db_project_manager.infrastructure.config.gui_settings import GuiSettingsStore


def test_roundtrip_action_settings(tmp_path):
    store = GuiSettingsStore(tmp_path / "gui_settings.json")
    store.save_action_settings("reverse_engineer", {"connection": "qr", "output_dir": "C:/out"})
    assert store.get_action_settings("reverse_engineer") == {
        "connection": "qr",
        "output_dir": "C:/out",
    }


def test_missing_file_returns_empty(tmp_path):
    store = GuiSettingsStore(tmp_path / "nonexistent.json")
    assert store.load() == {}
    assert store.get_action_settings("reverse_engineer") == {}
    assert store.get_last_action() is None


def test_corrupt_json_returns_empty_with_warning(tmp_path, caplog):
    path = tmp_path / "gui_settings.json"
    path.write_text("{not valid json", encoding="utf-8")
    store = GuiSettingsStore(path)
    with caplog.at_level(logging.WARNING):
        assert store.load() == {}
    assert "дефолт" in caplog.text


def test_non_dict_root_returns_empty(tmp_path, caplog):
    path = tmp_path / "gui_settings.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    store = GuiSettingsStore(path)
    with caplog.at_level(logging.WARNING):
        assert store.load() == {}
    assert "не объект" in caplog.text


def test_last_action_roundtrip(tmp_path):
    store = GuiSettingsStore(tmp_path / "gui_settings.json")
    assert store.get_last_action() is None
    store.set_last_action("deploy_validate")
    assert store.get_last_action() == "deploy_validate"


def test_partial_update_preserves_other_actions(tmp_path):
    store = GuiSettingsStore(tmp_path / "gui_settings.json")
    store.save_action_settings("reverse_engineer", {"connection": "a", "output_dir": "C:/a"})
    store.save_action_settings("deploy_validate", {"codebase_dir": "C:/b"})
    store.save_action_settings("reverse_engineer", {"connection": "c", "output_dir": "C:/c"})
    assert store.get_action_settings("deploy_validate") == {"codebase_dir": "C:/b"}
    assert store.get_action_settings("reverse_engineer") == {"connection": "c", "output_dir": "C:/c"}


def test_version_written_on_save(tmp_path):
    path = tmp_path / "gui_settings.json"
    store = GuiSettingsStore(path)
    store.save_action_settings("graph_prepare", {"codebase_dir": "C:/g"})
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert data["actions"]["graph_prepare"]["codebase_dir"] == "C:/g"


def test_no_tmp_file_left_after_save(tmp_path):
    store = GuiSettingsStore(tmp_path / "gui_settings.json")
    store.save_action_settings("graph_prepare", {"codebase_dir": "C:/g"})
    assert not (tmp_path / "gui_settings.json.tmp").exists()
