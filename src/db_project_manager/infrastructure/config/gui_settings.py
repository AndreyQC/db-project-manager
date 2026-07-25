"""GUI action settings persistence (gui_settings.json).

Stores per-action dialog settings (last used values) so the action panel can
prefill dialogs. JSON (not YAML): the file is written only by the application,
human editing is not required. No secrets — only connection names and paths.

Layout:
    {
      "version": 1,
      "last_action": "reverse_engineer",
      "actions": {
        "reverse_engineer": {"connection": "...", "output_dir": "..."},
        ...
      }
    }
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SETTINGS_VERSION = 1
DEFAULT_SETTINGS_PATH = "gui_settings.json"


class GuiSettingsStore:
    """Read/write per-action GUI settings to a JSON file."""

    def __init__(self, path: str | Path = DEFAULT_SETTINGS_PATH) -> None:
        self.path = Path(path)

    # --- read ---

    def load(self) -> dict[str, Any]:
        """Return the whole settings dict ({} if missing or corrupt)."""
        if not self.path.exists():
            return {}
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("gui_settings: не удалось прочитать %s (%s) — используются дефолты", self.path, e)
            return {}
        if not isinstance(data, dict):
            logger.warning("gui_settings: корень %s не объект — используются дефолты", self.path)
            return {}
        return data

    def get_action_settings(self, action_id: str) -> dict[str, Any]:
        """Return saved settings for one action ({} if none)."""
        actions = self.load().get("actions")
        if not isinstance(actions, dict):
            return {}
        settings = actions.get(action_id)
        return dict(settings) if isinstance(settings, dict) else {}

    def get_last_action(self) -> str | None:
        """Return the last executed action_id (None if never set)."""
        last = self.load().get("last_action")
        return last if isinstance(last, str) else None

    # --- write ---

    def save_action_settings(self, action_id: str, settings: dict[str, Any]) -> None:
        """Update one action's settings without touching other sections."""
        data = self.load()
        actions = data.get("actions")
        if not isinstance(actions, dict):
            actions = {}
        actions[action_id] = dict(settings)
        data["actions"] = actions
        self._save(data)

    def set_last_action(self, action_id: str) -> None:
        """Persist the last executed action_id."""
        data = self.load()
        data["last_action"] = action_id
        self._save(data)

    def _save(self, data: dict[str, Any]) -> None:
        """Write settings atomically (temp file + replace)."""
        data["version"] = SETTINGS_VERSION
        tmp_path = self.path.with_name(self.path.name + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, self.path)
