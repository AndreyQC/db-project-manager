"""Unit tests for BuildGraphService (Phase 8 overload-resolution report)."""

from __future__ import annotations

from pathlib import Path

from db_project_manager.application.graph_service import (
    OVERLOAD_REPORT_FILENAME,
    BuildGraphService,
)

# Autodoc header template (markers + YAML) used for hand-built fixture files.
_AUTODOC = """\
/*====================================================================================
[<[autodoc-yaml]]
object:
  object_catalog: db
  object_schema: {schema}
  object_type: {otype}
  object_name: {name}
  object_key: {key}
{sig_line}{arg_line}
project:
  build: true
[[autodoc-yaml]>]
=====================================================================================*/

"""


def _write_routine(
    root: Path,
    schema: str,
    name: str,
    otype: str,
    signature: str,
    arg_types: str,
    body: str,
) -> None:
    """Write a single routine SQL file with an autodoc header."""
    key = (
        f"pg_database/db/schema/{schema}/type/{otype}/name/{name}"
        + (f"/signature/{signature}" if signature else "")
    )
    sig_line = f"  object_signature: '{signature}'\n" if signature else ""
    arg_line = f"  argument_types: {arg_types}\n" if arg_types else ""
    header = _AUTODOC.format(
        schema=schema, otype=otype, name=name, key=key,
        sig_line=sig_line, arg_line=arg_line,
    )
    path = root / schema / f"{otype}s" / f"{otype} {name}__{signature}.sql"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + body, encoding="utf-8")


def test_overload_report_written_when_call_unresolved(tmp_path: Path) -> None:
    """An overloaded routine called with a non-literal argument (a column) is
    unresolved — the report must be written and name the routine + routed key."""
    root = tmp_path / "codebase"
    # Two overloads of app.f, distinguishable by argument type.
    _write_routine(
        root, "app", "f", "function", "aaa11111", "int4",
        "CREATE OR REPLACE FUNCTION app.f(a int4) RETURNS int4 LANGUAGE sql AS $$ SELECT a $$;",
    )
    _write_routine(
        root, "app", "f", "function", "bbb22222", "text",
        "CREATE OR REPLACE FUNCTION app.f(a text) RETURNS text LANGUAGE sql AS $$ SELECT a $$;",
    )
    # Caller invokes f(some_column) — a column is NOT a literal, so inference
    # fails and the edge falls back to first-wins (routed to the int4 overload).
    _write_routine(
        root, "app", "caller", "function", "ccc33333", "int4",
        "CREATE OR REPLACE FUNCTION app.caller(p int4) RETURNS void LANGUAGE plpgsql AS $$ "
        "BEGIN PERFORM app.f(p); END; $$;",
    )

    service = BuildGraphService()
    service.build(root)

    report = root / OVERLOAD_REPORT_FILENAME
    assert report.is_file(), "overload-resolution report must be written when calls are unresolved"
    text = report.read_text(encoding="utf-8")
    assert "# Overload Resolution Report" in text
    assert "`app.f`" in text
    # The routed key is the first overload (int4 -> aaa11111).
    assert "aaa11111" in text
    assert "Unresolved overloaded calls: 1" in text


def test_overload_report_not_written_when_all_resolved(tmp_path: Path) -> None:
    """When every overloaded call resolves (literal arguments), no report is
    written — resolved edges are visible in the graph directly."""
    root = tmp_path / "codebase"
    _write_routine(
        root, "app", "f", "function", "aaa11111", "int4",
        "CREATE OR REPLACE FUNCTION app.f(a int4) RETURNS int4 LANGUAGE sql AS $$ SELECT a $$;",
    )
    _write_routine(
        root, "app", "f", "function", "bbb22222", "text",
        "CREATE OR REPLACE FUNCTION app.f(a text) RETURNS text LANGUAGE sql AS $$ SELECT a $$;",
    )
    # Both calls use literals -> both resolve. No unresolved note -> no file.
    _write_routine(
        root, "app", "caller", "function", "ccc33333", "int4",
        "CREATE OR REPLACE FUNCTION app.caller(p int4) RETURNS void LANGUAGE plpgsql AS $$ "
        "BEGIN PERFORM app.f(1); PERFORM app.f('x'); END; $$;",
    )

    service = BuildGraphService()
    service.build(root)

    assert not (root / OVERLOAD_REPORT_FILENAME).is_file()


def test_overload_report_not_written_for_non_overloaded(tmp_path: Path) -> None:
    """A singleton routine (no overloads) never triggers resolution, so no
    report is written even if it is called."""
    root = tmp_path / "codebase"
    _write_routine(
        root, "app", "g", "function", "ddd44444", "uuid",
        "CREATE OR REPLACE FUNCTION app.g(id uuid) RETURNS boolean LANGUAGE sql AS $$ SELECT true $$;",
    )
    _write_routine(
        root, "app", "caller", "function", "eee55555", "int4",
        "CREATE OR REPLACE FUNCTION app.caller(p int4) RETURNS boolean LANGUAGE plpgsql AS $$ "
        "BEGIN RETURN app.g('00000000-0000-0000-0000-000000000000'::uuid); END; $$;",
    )

    service = BuildGraphService()
    service.build(root)

    assert not (root / OVERLOAD_REPORT_FILENAME).is_file()
