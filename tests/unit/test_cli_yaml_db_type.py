"""CLI db_type resolution cascade for yaml commands (Phase 16.2).

``yaml generate``: the reverse-engineer manifest (``dbpm.manifest.json``)
    supplies the source db_type; ``--db-type`` is only needed for manifest-less
    directories; a flag contradicting the manifest is a usage error.
``yaml apply``: ``--target-db-type`` defaults to the project's own db_type.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from db_project_manager.domain.diff import CodebaseManifest
from db_project_manager.domain.yaml_project import YamlProject
from db_project_manager.infrastructure.config.codebase_manifest import write_manifest
from db_project_manager.infrastructure.yaml_project import serialize_yaml_project
from db_project_manager.presentation.cli.main import app

runner = CliRunner()

_AUTODOC_TABLE_SQL = """/*====================================================================================
[<[autodoc-yaml]]
object:
  object_catalog: testdb
  object_key: pg_database/testdb/schema/s1/type/table/name/users
  object_name: users
  object_schema: s1
  object_type: table
project:
  build: true
[[autodoc-yaml]>]
=====================================================================================*/

CREATE TABLE s1.users (
    id INT NOT NULL
);
"""


def _make_source(root: Path) -> Path:
    """A minimal RE-like source tree: one schema, one autodoc'd table."""
    src = root / "src"
    (src / "s1" / "tables").mkdir(parents=True)
    (src / "s1" / "tables" / "table users.sql").write_text(_AUTODOC_TABLE_SQL, encoding="utf-8")
    return src


def _write_manifest(src: Path, db_type: str) -> None:
    write_manifest(
        CodebaseManifest(
            db_type=db_type,
            database="testdb",
            generated_at="2026-09-09T00:00:00+00:00",
            tool_version="0.0.0",
            source_version="2026.09.09.01",
        ),
        src,
    )


def _minimal_project_yaml_text(db_type: str) -> str:
    project = YamlProject(
        db_type=db_type,
        database="testdb",
        generated_at="2026-09-09T00:00:00+00:00",
        schemas=[],
    )
    return serialize_yaml_project(project)


def _output_of(result) -> str:  # noqa: ANN001 — typer Result type is not exported
    """Version-agnostic stdout+stderr (click 8.2 splits them, older merges)."""
    return result.output + (result.stderr or "")


def test_yaml_generate_takes_db_type_from_manifest(tmp_path: Path) -> None:
    src = _make_source(tmp_path)
    _write_manifest(src, "greenplum")
    out = tmp_path / "project.yaml"

    result = runner.invoke(app, ["yaml", "generate", "--source", str(src), "--output", str(out)])

    assert result.exit_code == 0, _output_of(result)
    assert "Тип БД взят из манифеста: greenplum" in result.output
    assert "db_type: greenplum" in out.read_text(encoding="utf-8")


def test_yaml_generate_flag_agreeing_with_manifest_is_accepted(tmp_path: Path) -> None:
    src = _make_source(tmp_path)
    _write_manifest(src, "greenplum")
    out = tmp_path / "project.yaml"

    result = runner.invoke(
        app,
        ["yaml", "generate", "--source", str(src), "--db-type", "greenplum", "--output", str(out)],
    )

    assert result.exit_code == 0, _output_of(result)
    assert "db_type: greenplum" in out.read_text(encoding="utf-8")


def test_yaml_generate_flag_conflicting_with_manifest_fails(tmp_path: Path) -> None:
    src = _make_source(tmp_path)
    _write_manifest(src, "greenplum")

    result = runner.invoke(
        app,
        [
            "yaml", "generate",
            "--source", str(src), "--db-type", "postgres",
            "--output", str(tmp_path / "p.yaml"),
        ],
    )

    assert result.exit_code == 2
    assert "противоречит манифесту" in _output_of(result)


def test_yaml_generate_requires_db_type_without_manifest(tmp_path: Path) -> None:
    src = _make_source(tmp_path)

    result = runner.invoke(
        app, ["yaml", "generate", "--source", str(src), "--output", str(tmp_path / "p.yaml")],
    )

    assert result.exit_code == 2
    assert "Не удалось определить тип БД" in _output_of(result)


def test_yaml_generate_invalid_db_type_rejected(tmp_path: Path) -> None:
    src = _make_source(tmp_path)

    result = runner.invoke(
        app,
        [
            "yaml", "generate",
            "--source", str(src), "--db-type", "oracle",
            "--output", str(tmp_path / "p.yaml"),
        ],
    )

    assert result.exit_code == 2
    assert "Invalid --db-type" in _output_of(result)


def test_yaml_apply_target_db_type_defaults_to_project(monkeypatch, tmp_path: Path) -> None:
    yaml_file = tmp_path / "project.yaml"
    yaml_file.write_text(_minimal_project_yaml_text("greenplum"), encoding="utf-8")

    captured: dict = {}

    class _FakeResult:
        schemas_count = 0
        objects_count = 0
        converted_external_tables = 0
        output_dir = str(tmp_path / "out")

    class _FakeService:
        def __init__(self, service_schema=None):  # noqa: ANN001, ARG002 — CLI contract
            pass

        def run(self, project, output, target_db_type, convert_external_to_tables):  # noqa: ANN001, ARG002
            captured["target_db_type"] = target_db_type
            return _FakeResult()

    from db_project_manager.application import yaml_apply_service as mod

    monkeypatch.setattr(mod, "YamlApplyService", _FakeService)

    result = runner.invoke(
        app, ["yaml", "apply", "--yaml", str(yaml_file), "--output", str(tmp_path / "out")],
    )

    assert result.exit_code == 0, _output_of(result)
    assert captured["target_db_type"] == "greenplum"
    assert "используется тип проекта: greenplum" in result.output


def test_yaml_apply_explicit_target_db_type_overrides(monkeypatch, tmp_path: Path) -> None:
    yaml_file = tmp_path / "project.yaml"
    yaml_file.write_text(_minimal_project_yaml_text("greenplum"), encoding="utf-8")

    captured: dict = {}

    class _FakeResult:
        schemas_count = 0
        objects_count = 0
        converted_external_tables = 0
        output_dir = str(tmp_path / "out")

    class _FakeService:
        def __init__(self, service_schema=None):  # noqa: ANN001, ARG002 — CLI contract
            pass

        def run(self, project, output, target_db_type, convert_external_to_tables):  # noqa: ANN001, ARG002
            captured["target_db_type"] = target_db_type
            return _FakeResult()

    from db_project_manager.application import yaml_apply_service as mod

    monkeypatch.setattr(mod, "YamlApplyService", _FakeService)

    result = runner.invoke(
        app,
        [
            "yaml", "apply",
            "--yaml", str(yaml_file), "--target-db-type", "postgres",
            "--output", str(tmp_path / "out"),
        ],
    )

    assert result.exit_code == 0, _output_of(result)
    assert captured["target_db_type"] == "postgres"
    assert "используется тип проекта" not in result.output


def test_yaml_apply_invalid_target_db_type_rejected(tmp_path: Path) -> None:
    yaml_file = tmp_path / "project.yaml"
    yaml_file.write_text(_minimal_project_yaml_text("postgres"), encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "yaml", "apply",
            "--yaml", str(yaml_file), "--target-db-type", "oracle",
            "--output", str(tmp_path / "out"),
        ],
    )

    assert result.exit_code == 2
    assert "Invalid --target-db-type" in _output_of(result)
