"""Unit tests for the Phase 15.7 run-directory naming (TimelineNameGenerator)."""

from __future__ import annotations

import datetime
import random
from pathlib import Path

from db_project_manager.infrastructure.files.run_naming import (
    create_run_dir,
    decode_run_dir_name,
    latest_run_dir,
    resolve_report_dir,
)

DT = datetime.datetime(2024, 1, 15, 3, 0, 0)  # day 15 avoids month-length clamp


def test_generate_decode_roundtrip() -> None:
    from db_project_manager.infrastructure.files.run_naming import TimelineNameGenerator

    gen = TimelineNameGenerator(seed=7)
    name = gen.generate_by_date(DT)
    decoded = gen.decode_name_to_date(name)
    assert decoded.year == DT.year
    assert decoded.month == DT.month
    assert decoded.day == DT.day
    assert decoded.hour == DT.hour


def test_create_run_dir_makes_unique_decodable_subdir(tmp_path: Path) -> None:
    run = create_run_dir(tmp_path, now=DT, rng=random.Random(1))
    assert run.parent == tmp_path
    assert run.is_dir()
    assert decode_run_dir_name(run.name) is not None


def test_create_run_dir_disabled_returns_root(tmp_path: Path) -> None:
    assert create_run_dir(tmp_path, enabled=False) == tmp_path
    assert tmp_path.is_dir()


def test_create_run_dir_collision_retries(tmp_path: Path) -> None:
    from db_project_manager.infrastructure.files.run_naming import TimelineNameGenerator

    # Pre-create the name that a fresh generator (same seed, same clock) would emit.
    first = TimelineNameGenerator(seed=7).generate_by_date(DT)
    (tmp_path / first).mkdir()
    run = create_run_dir(tmp_path, now=DT, rng=random.Random(7))
    assert run != tmp_path / first
    assert run.is_dir()


def test_latest_run_dir_orders_by_decoded_time_and_ignores_junk(tmp_path: Path) -> None:
    early = create_run_dir(tmp_path, now=DT, rng=random.Random(1))
    later_dt = DT.replace(hour=9)
    late = create_run_dir(tmp_path, now=later_dt, rng=random.Random(1))
    (tmp_path / "not_a_run_dir").mkdir()
    assert latest_run_dir(tmp_path) == late
    assert early != late


def test_resolve_report_dir_falls_back_to_root(tmp_path: Path) -> None:
    assert resolve_report_dir(tmp_path) == tmp_path
    run = create_run_dir(tmp_path, now=DT, rng=random.Random(2))
    assert resolve_report_dir(tmp_path) == run


def test_decode_run_dir_name_rejects_junk() -> None:
    assert decode_run_dir_name("plain-dir") is None
    assert decode_run_dir_name("dancing-red-crazy-godzilla-45") is not None
