"""Tests for pre-script coverage extraction (Phase 11, step S3).

Filesystem fixtures via ``tmp_path``: pre-scripts with/without autodoc, valid
and broken ``covers`` declarations (LESSONS §36: YAML barewords parse into
bools/int — must be skipped with a warning, not crash).
"""

from __future__ import annotations

from pathlib import Path

from db_project_manager.infrastructure.deploy.pre_coverage import read_pre_coverage

_HEADER = """/*====
[<[autodoc-yaml]]
object:
  object_type: pre_script
  object_name: {name}
project:
  build: true
{covers}[[autodoc-yaml]>]
====*/

-- SQL body; the gate never executes it.
"""


def _write_script(migrations_dir: Path, name: str, covers_yaml: str) -> None:
    pre_dir = migrations_dir / "pre"
    pre_dir.mkdir(parents=True, exist_ok=True)
    (pre_dir / name).write_text(
        _HEADER.format(name=name, covers=covers_yaml), encoding="utf-8"
    )


def test_no_dir_returns_empty(tmp_path: Path) -> None:
    assert read_pre_coverage(tmp_path) == {}


def test_empty_dir_returns_empty(tmp_path: Path) -> None:
    (tmp_path / "pre").mkdir()
    assert read_pre_coverage(tmp_path) == {}


def test_valid_covers_parsed(tmp_path: Path) -> None:
    _write_script(
        tmp_path, "2026-08-14_001_migrate.sql",
        "  covers:\n    - app.orders\n    - app.items\n",
    )
    coverage = read_pre_coverage(tmp_path)
    assert coverage == {
        ("app", "orders"): ["2026-08-14_001_migrate.sql"],
        ("app", "items"): ["2026-08-14_001_migrate.sql"],
    }


def test_script_without_autodoc_contributes_nothing(tmp_path: Path) -> None:
    (tmp_path / "pre").mkdir()
    (tmp_path / "pre" / "2026-08-14_001_plain.sql").write_text(
        "UPDATE app.orders SET x = 1;\n", encoding="utf-8"
    )
    assert read_pre_coverage(tmp_path) == {}


def test_autodoc_without_covers_contributes_nothing(tmp_path: Path) -> None:
    _write_script(tmp_path, "2026-08-14_001_nocovers.sql", "")
    assert read_pre_coverage(tmp_path) == {}


def test_broken_entries_warn_and_skip_valid_kept(tmp_path: Path) -> None:
    # 'justname' (no dot), '' (empty), 42 (int), True (YAML bareword YES→bool)
    # are broken; app.orders is valid and must survive.
    _write_script(
        tmp_path, "2026-08-14_001_mixed.sql",
        "  covers:\n    - app.orders\n    - justname\n"
        "    - ''\n    - 42\n    - YES\n",
    )
    coverage = read_pre_coverage(tmp_path)
    assert coverage == {("app", "orders"): ["2026-08-14_001_mixed.sql"]}


def test_multiple_scripts_merge_sorted(tmp_path: Path) -> None:
    _write_script(
        tmp_path, "2026-08-14_002_second.sql", "  covers:\n    - app.orders\n"
    )
    _write_script(
        tmp_path, "2026-08-14_001_first.sql", "  covers:\n    - app.orders\n"
    )
    coverage = read_pre_coverage(tmp_path)
    # sorted() traversal → deterministic covered_by order (001 before 002)
    assert coverage == {
        ("app", "orders"): ["2026-08-14_001_first.sql", "2026-08-14_002_second.sql"]
    }


def test_covers_not_a_list_is_ignored(tmp_path: Path) -> None:
    _write_script(tmp_path, "2026-08-14_001_strcover.sql", "  covers: app.orders\n")
    assert read_pre_coverage(tmp_path) == {}


def test_whitespace_in_entries_is_stripped(tmp_path: Path) -> None:
    _write_script(
        tmp_path, "2026-08-14_001_ws.sql", "  covers:\n    - '  app.orders  '\n"
    )
    assert read_pre_coverage(tmp_path) == {("app", "orders"): ["2026-08-14_001_ws.sql"]}
