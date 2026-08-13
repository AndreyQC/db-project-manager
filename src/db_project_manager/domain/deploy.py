"""Domain models and helpers for the CD Foundation (Phase 10).

This module is pure: no I/O, no DB, no infrastructure imports. It carries the
shared vocabulary for the controlled-deployment foundation:

* ``CALVER_RE`` / :func:`validate_calver` / :func:`calver_seed` — the calver
  ``YYYY.MM.DD.NN`` scheme used as ``source_version`` in ``dbpm.manifest.json``.
* :class:`ScriptRecord` — one execution of a pre/post-deploy script (state row
  in ``__deploy.script_history``, audit row in ``__deploy.script_audit_log``).
* :func:`canonical_normalize` / :func:`script_checksum` — stable checksum of
  executable SQL.

Checksum contract (CDF-6): the checksum is computed over **executable SQL only**,
never over the autodoc YAML header. Callers are responsible for stripping the
autodoc block first via :func:`db_project_manager.infrastructure.sql.autodoc.strip_autodoc`
— keeping that dependency out of ``domain/`` preserves the clean layering
(domain depends on nothing infrastructural).

See ``-=docs=-/-=tasks=-/phase_10/Phase_10_vision_final.md`` §3 (CDF-1..CDF-11)
for the decisions encoded here.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Calver ``YYYY.MM.DD.NN`` — fixed-width 4/2/2/2 so lexicographic comparison
# equals chronological (CDF-9). Day in 01..31, month in 01..12, release number
# within the day in 00..99 (zero-padded).
CALVER_RE = re.compile(r"^\d{4}\.(0[1-9]|1[0-2])\.(0[1-9]|[12]\d|3[01])\.\d{2}$")


def validate_calver(value: str) -> None:
    """Raise ``ValueError`` if *value* is not a valid calver ``YYYY.MM.DD.NN``.

    Used by manifest validation (Phase 10: ``source_version`` is required at
    ``format_version=2``) and anywhere a calver is read from external input.
    """
    if not isinstance(value, str) or not CALVER_RE.match(value):
        raise ValueError(
            f"invalid calver {value!r}: expected YYYY.MM.DD.NN "
            "(e.g. '2026.08.11.01'); year/month/day are zero-padded."
        )


def calver_seed(now: datetime | None = None) -> str:
    """Return ``YYYY.MM.DD.01`` for the given UTC time (default: now).

    Used by reverse-engineer when seeding a codebase that has no ``__deploy``
    yet — the very first ``source_version`` (CDF-9, vision_final §4.7).
    """
    now = now or datetime.now(timezone.utc)
    return f"{now.year:04d}.{now.month:02d}.{now.day:02d}.01"


class ScriptRecord(BaseModel):
    """One execution attempt of a pre/post-deploy script.

    Mirrors a row of ``__deploy.script_history`` (state, PK ``(script_name,
    script_type)``) and the corresponding ``__deploy.script_audit_log`` row
    (history, append-only). The same record is written to both tables by
    ``DatabaseAdapter.record_script_execution`` (atomic, single transaction) —
    see vision_final §4.6.

    ``error_message`` is ``None`` when ``success`` is True; the model does not
    enforce that invariant, but adapters and the runner are expected to keep it.
    """

    model_config = ConfigDict(extra="ignore")

    script_name: str
    script_type: Literal["pre", "post"]
    checksum: str            # SHA-256 hex (64 chars) of canonical-normalized SQL
    success: bool
    error_message: str | None = None
    duration_ms: int
    executed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def canonical_normalize(text: str) -> str:
    """Normalize SQL text for a stable checksum.

    Strips trailing whitespace per line, drops blank lines, normalizes line
    endings to ``\\n``. SQL comments (``-- ...``) are preserved — they may be
    semantically significant and removing them needs an AST (MVP, CDF-6).

    **Does NOT strip the autodoc YAML header.** Callers must pass executable SQL
    only — strip via ``infrastructure.sql.autodoc.strip_autodoc`` first. The
    split keeps ``domain/`` free of infrastructure imports.
    """
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").split("\n")]
    lines = [line for line in lines if line]
    return "\n".join(lines)


def script_checksum(text: str) -> str:
    """SHA-256 hex of :func:`canonical_normalize` applied to *text*.

    Caller responsibility (CDF-6): pass SQL with the autodoc header already
    stripped. The same helper is used by the pre/post runner (script identity)
    and the canonical-DDL validator (deploy-time mismatch warning).
    """
    return hashlib.sha256(canonical_normalize(text).encode("utf-8")).hexdigest()
