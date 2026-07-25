"""Regression tests for temp-DB name validation (Phase 2 deploy surface).

Guards against SQL injection via CREATE DATABASE / DROP DATABASE, which in
the POC built the statement with an f-string from user input
(LESSONS_LEARNED §create_database).
"""

from __future__ import annotations

import pytest

from db_project_manager.infrastructure.database.base import DatabaseError
from db_project_manager.infrastructure.database.postgres.adapter import _validate_db_name


@pytest.mark.parametrize(
    "name",
    [
        "myapp",
        "myapp_20260718T143022",
        "_under",
        "A1_b2",
    ],
)
def test_validate_db_name_accepts_valid(name: str) -> None:
    assert _validate_db_name(name) == name


@pytest.mark.parametrize(
    "name",
    [
        "",                       # empty
        "1starts_with_digit",     # leading digit
        "has space",
        "has-dash",
        'has"quote',
        "has;semicolon",
        "has; DROP TABLE x; --",
        "colons:not:allowed",
        "dot.in.name",
        "9",
    ],
)
def test_validate_db_name_rejects_invalid(name: str) -> None:
    with pytest.raises(DatabaseError, match="Недопустимое имя"):
        _validate_db_name(name)
