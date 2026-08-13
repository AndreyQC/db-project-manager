"""Tests for the pre/post-deploy script runner (Phase 10 S7).

Uses ``DeployFakeAdapter`` to verify the four-way skip/error/execute decision,
state vs audit semantics, continue/stop error policy, and the CDF-6 contract
(checksum over strip_autodoc-then-normalized SQL only).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from db_project_manager.application.script_runner import (
    ScriptExecutionError,
    ScriptRunner,
)
from tests.unit.test_deploy_service import DeployFakeAdapter

SCHEMA = "__deploy"
DEPLOY_VERSION = "2026.08.13.01"
DEPLOY_SOURCE = "validate"


def _write_script(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _runner(adapter: DeployFakeAdapter) -> ScriptRunner:
    return ScriptRunner(
        adapter,
        SCHEMA,
        deploy_version=DEPLOY_VERSION,
        deploy_source=DEPLOY_SOURCE,
    )


# ------------------------------------------------------------- discovery


def test_run_phase_missing_dir_returns_empty(tmp_path: Path) -> None:
    """No __migrations/ at all → empty list, no exception."""
    adapter = DeployFakeAdapter()
    records = _runner(adapter).run_phase("pre", tmp_path / "__migrations")
    assert records == []


def test_run_phase_missing_phase_subdir_returns_empty(tmp_path: Path) -> None:
    """__migrations/ exists but pre/ doesn't → empty list."""
    (tmp_path / "__migrations" / "post").mkdir(parents=True)
    adapter = DeployFakeAdapter()
    records = _runner(adapter).run_phase("pre", tmp_path / "__migrations")
    assert records == []


def test_run_phase_empty_dir_returns_empty(tmp_path: Path) -> None:
    """pre/ exists but has no .sql → empty list."""
    (tmp_path / "__migrations" / "pre").mkdir(parents=True)
    adapter = DeployFakeAdapter()
    records = _runner(adapter).run_phase("pre", tmp_path / "__migrations")
    assert records == []


# --------------------------------------------------------- 4-way decision


def test_new_script_executes_and_records(tmp_path: Path) -> None:
    """Never-run script → EXECUTE; state + audit each get one record."""
    _write_script(
        tmp_path / "__migrations" / "pre" / "2026-08-11_001_init.sql",
        "CREATE TABLE app.tmp (id int);",
    )
    adapter = DeployFakeAdapter()
    records = _runner(adapter).run_phase("pre", tmp_path / "__migrations")

    assert len(records) == 1
    rec = records[0]
    assert rec.script_name == "2026-08-11_001_init.sql"
    assert rec.script_type == "pre"
    assert rec.success is True

    # State row keyed by (schema, name, type).
    state = adapter._script_history[(SCHEMA, "2026-08-11_001_init.sql", "pre")]
    assert state.success is True
    # Audit row carries deploy_version / deploy_source.
    assert len(adapter._script_audit) == 1
    assert adapter._script_audit[0]["deploy_version"] == DEPLOY_VERSION
    assert adapter._script_audit[0]["deploy_source"] == DEPLOY_SOURCE


def test_skip_when_same_checksum_success(tmp_path: Path) -> None:
    """Re-run with the same script content after success → SKIP, no execute."""
    script_path = tmp_path / "__migrations" / "pre" / "2026-08-11_001_init.sql"
    _write_script(script_path, "CREATE TABLE app.tmp (id int);")

    adapter = DeployFakeAdapter()
    runner = _runner(adapter)
    first = runner.run_phase("pre", tmp_path / "__migrations")
    assert len(first) == 1
    executed_before = list(adapter.executed)

    second = runner.run_phase("pre", tmp_path / "__migrations")
    # SKIP: not in result list, no new execute_script calls.
    assert second == []
    assert adapter.executed == executed_before
    # Audit not extended either (skip = no new attempt).
    assert len(adapter._script_audit) == 1


def test_error_when_same_checksum_failed(tmp_path: Path) -> None:
    """Last attempt with same checksum failed → ERROR, do NOT re-execute.

    Records a fresh failure row and raises ScriptExecutionError (default stop).
    """
    script_path = tmp_path / "__migrations" / "pre" / "2026-08-11_001_x.sql"
    # Use a token that DeployFakeAdapter.fail_on will catch.
    _write_script(script_path, "CREATE TABLE fail.tbl (id int);")

    adapter = DeployFakeAdapter(fail_on={"fail.tbl"})
    runner = _runner(adapter)

    # First run: script fails, raises (default stop_on_error).
    with pytest.raises(ScriptExecutionError):
        runner.run_phase("pre", tmp_path / "__migrations")
    # State records the failure.
    state = adapter._script_history[(SCHEMA, "2026-08-11_001_x.sql", "pre")]
    assert state.success is False

    # Second run with same content → ERROR (don't blindly retry); does NOT call execute_script again.
    executed_before = list(adapter.executed)
    with pytest.raises(ScriptExecutionError):
        runner.run_phase("pre", tmp_path / "__migrations")
    assert adapter.executed == executed_before  # no new execution


def test_error_when_different_checksum(tmp_path: Path) -> None:
    """Script changed after successful apply → ERROR (CD-4)."""
    script_path = tmp_path / "__migrations" / "pre" / "2026-08-11_001_x.sql"
    _write_script(script_path, "CREATE TABLE app.a (id int);")

    adapter = DeployFakeAdapter()
    runner = _runner(adapter)
    runner.run_phase("pre", tmp_path / "__migrations")  # EXECUTE success

    # Now mutate the script body.
    _write_script(script_path, "CREATE TABLE app.a (id int, name text);")
    with pytest.raises(ScriptExecutionError, match="изменился"):
        runner.run_phase("pre", tmp_path / "__migrations")


# --------------------------------------------------- audit vs state semantics


def test_audit_grows_state_upserts_across_attempts(tmp_path: Path) -> None:
    """Three attempts: fail → fix (success) → re-run (skip).

    State must hold ONE row at the end (UPSERT); audit must have THREE.
    Locks the CDF-11 contract.
    """
    script_path = tmp_path / "__migrations" / "pre" / "2026-08-11_001_y.sql"

    # Attempt 1: failing body.
    _write_script(script_path, "CREATE TABLE fail.tbl (id int);")
    adapter = DeployFakeAdapter(fail_on={"fail.tbl"})
    runner = _runner(adapter)
    with pytest.raises(ScriptExecutionError):
        runner.run_phase("pre", tmp_path / "__migrations")
    assert len(adapter._script_audit) == 1

    # Attempt 2: fix the script body (new checksum) — now succeeds.
    adapter.fail_on = set()
    _write_script(script_path, "CREATE TABLE ok.tbl (id int);")
    runner.run_phase("pre", tmp_path / "__migrations")
    assert len(adapter._script_audit) == 2

    # Attempt 3: re-run with the same body → SKIP (no new audit).
    runner.run_phase("pre", tmp_path / "__migrations")
    assert len(adapter._script_audit) == 2  # skip does not extend audit

    # State holds ONE row (UPSERT across attempt 1 → attempt 2).
    assert len(adapter._script_history) == 1
    final_state = adapter._script_history[(SCHEMA, "2026-08-11_001_y.sql", "pre")]
    assert final_state.success is True


# --------------------------------------------------- error policy + name warning


def test_stop_on_error_default_raises_on_first_failure(tmp_path: Path) -> None:
    """Default: first failing script raises; later scripts do not run."""
    _write_script(
        tmp_path / "__migrations" / "pre" / "2026-08-11_001_a.sql",
        "CREATE TABLE fail.tbl (id int);",  # fails
    )
    _write_script(
        tmp_path / "__migrations" / "pre" / "2026-08-11_002_b.sql",
        "CREATE TABLE ok.tbl (id int);",  # would succeed but must not run
    )
    adapter = DeployFakeAdapter(fail_on={"fail.tbl"})
    with pytest.raises(ScriptExecutionError):
        _runner(adapter).run_phase("pre", tmp_path / "__migrations")
    # Second script never executed.
    assert not any("ok.tbl" in s for s in adapter.executed)


def test_continue_on_error_runs_all_and_returns_failures(tmp_path: Path) -> None:
    """continue_on_error=True → both scripts run; failures in returned list."""
    _write_script(
        tmp_path / "__migrations" / "pre" / "2026-08-11_001_a.sql",
        "CREATE TABLE fail.tbl (id int);",
    )
    _write_script(
        tmp_path / "__migrations" / "pre" / "2026-08-11_002_b.sql",
        "CREATE TABLE ok.tbl (id int);",
    )
    adapter = DeployFakeAdapter(fail_on={"fail.tbl"})
    records = _runner(adapter).run_phase(
        "pre", tmp_path / "__migrations", continue_on_error=True
    )
    # Both records returned (one failure, one success).
    assert len(records) == 2
    assert sum(1 for r in records if r.success) == 1
    assert sum(1 for r in records if not r.success) == 1
    # The successful one was actually executed against the adapter; the failing
    # one raised inside execute_script before appending (DeployFakeAdapter
    # checks fail_on first), so only the success path shows up in `executed`.
    assert any("ok.tbl" in s for s in adapter.executed)
    assert not any("fail.tbl" in s for s in adapter.executed)
    # Both attempts written to audit and state (the failure too, for retry).
    assert len(adapter._script_audit) == 2
    assert len(adapter._script_history) == 2


def test_invalid_filename_still_executes(tmp_path: Path) -> None:
    """Name not matching the convention → warning, but the script still runs."""
    _write_script(
        tmp_path / "__migrations" / "pre" / "legacy.sql",
        "CREATE TABLE app.tmp (id int);",
    )
    adapter = DeployFakeAdapter()
    records = _runner(adapter).run_phase("pre", tmp_path / "__migrations")
    assert len(records) == 1
    assert records[0].success is True


# ---------------------------------------------------------- ordering / progress


def test_scripts_run_in_sorted_filename_order(tmp_path: Path) -> None:
    """Order is deterministic by filename, not OS glob order."""
    # Write in non-sorted order.
    for name in (
        "2026-08-11_003_c.sql",
        "2026-08-11_001_a.sql",
        "2026-08-11_002_b.sql",
    ):
        _write_script(
            tmp_path / "__migrations" / "pre" / name,
            f"CREATE TABLE app.{name[:0]} (id int);",
        )
    adapter = DeployFakeAdapter()
    records = _runner(adapter).run_phase("pre", tmp_path / "__migrations")
    names = [r.script_name for r in records]
    assert names == sorted(names)
    assert names == [
        "2026-08-11_001_a.sql",
        "2026-08-11_002_b.sql",
        "2026-08-11_003_c.sql",
    ]


def test_progress_callback_emits_per_script(tmp_path: Path) -> None:
    """Progress callback receives (message, current, total) once per script."""
    for i in range(1, 4):
        _write_script(
            tmp_path / "__migrations" / "pre" / f"2026-08-11_{i:03d}_x.sql",
            "CREATE TABLE app.tmp (id int);",
        )
    adapter = DeployFakeAdapter()
    progress_log: list[tuple[str, int, int]] = []
    _runner(adapter).run_phase(
        "pre",
        tmp_path / "__migrations",
        on_progress=lambda m, c, t: progress_log.append((m, c, t)),
    )
    assert len(progress_log) == 3
    assert progress_log[0][2] == 3   # total
    assert progress_log[0][1] == 1   # current
    assert progress_log[-1][1] == 3


# --------------------------------------------------- checksum contract (CDF-6)


def test_checksum_ignores_autodoc_header(tmp_path: Path) -> None:
    """Adding an autodoc header to the script does NOT change its identity.

    Same script with and without a metadata block must hash identically, so
    the runner treats them as the same content (SKIP on second run, not ERROR).
    """
    body_only = "CREATE TABLE app.ctr (id int);"
    script_path = tmp_path / "__migrations" / "pre" / "2026-08-11_001_z.sql"
    _write_script(script_path, body_only)

    adapter = DeployFakeAdapter()
    runner = _runner(adapter)
    runner.run_phase("pre", tmp_path / "__migrations")  # EXECUTE

    # Rewrite the script: same SQL body but with an autodoc header prepended.
    decorated = (
        "[<[autodoc-yaml]]\n"
        "object: {note: metadata added later}\n"
        "[[autodoc-yaml]>]]\n"
        "*/\n"
        + body_only
    )
    _write_script(script_path, decorated)
    # Second run must SKIP (same executable SQL), not ERROR on checksum drift.
    records = runner.run_phase("pre", tmp_path / "__migrations")
    assert records == []
    # Audit unchanged — skip doesn't add a row.
    assert len(adapter._script_audit) == 1


def test_post_phase_runs_independently_from_pre(tmp_path: Path) -> None:
    """pre and post are separate phases with separate state keys."""
    _write_script(
        tmp_path / "__migrations" / "pre" / "2026-08-11_001_pre.sql",
        "CREATE TABLE app.pre (id int);",
    )
    _write_script(
        tmp_path / "__migrations" / "post" / "2026-08-11_001_post.sql",
        "CREATE TABLE app.post (id int);",
    )
    adapter = DeployFakeAdapter()
    runner = _runner(adapter)
    pre_records = runner.run_phase("pre", tmp_path / "__migrations")
    post_records = runner.run_phase("post", tmp_path / "__migrations")

    assert len(pre_records) == 1
    assert pre_records[0].script_type == "pre"
    assert len(post_records) == 1
    assert post_records[0].script_type == "post"
    # Distinct state rows (script_type is part of the key).
    assert len(adapter._script_history) == 2
