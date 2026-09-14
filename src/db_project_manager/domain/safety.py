"""Domain models and helpers for the Safety Gate (Phase 11).

This module is pure: no I/O, no DB, no infrastructure imports. It carries the
shared vocabulary of the ``deploy analyze`` dry-run safety gate:

* :class:`StatsConfidence` / :class:`TablePresenceStats` — the **normalized,
  database-agnostic** per-table presence signal produced by adapters
  (``DatabaseAdapter.get_table_presence_stats``, SG-5). Each adapter maps its
  own catalog metadata onto this model; the domain never references
  database-specific catalog names (SG-M).
* :class:`DataPresence` / :func:`classify_presence` — the fail-safe domain
  classification (SG-4): anything that cannot be *confidently* declared empty
  is treated as ``HAS_DATA`` («лучше потерять день, чем данные»).
* :class:`TableTouchKind` / :class:`TouchedTable` — one table touched by the
  pending delta (``CHANGED``/``REMOVED`` at hash level; column-level change
  types are Phase 12, CD-ALT-1).
* :class:`VersionCheckOutcome` / :func:`check_version_relation` — the
  forward-only version gate (SG-6).
* :class:`SafetyGateVerdict` — the overall result written to the report and
  mapped to CLI exit codes.

See ``_docs_/_tasks_/phase_11/Phase_11_vision_final.md`` §3 (SG-0..SG-7,
SG-M) and §4.2 for the decisions encoded here.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class StatsConfidence(str, Enum):
    """Abstract freshness signal of a row-count estimate (SG-5).

    Filled in by each adapter from its own metadata sources:

    * PG/Greenplum — ``pg_class.reltuples`` + ``pg_stat_user_tables``
      (never-analyzed and heavy drift map to ``STALE``).
    * future adapters (Snowflake/MSSQL/MySQL) — their own system views; a
      database that exposes no freshness signal reports ``UNKNOWN``.

    ``UNKNOWN`` and ``STALE`` are both treated fail-safe by
    :func:`classify_presence`: the table is assumed to have data.
    """

    FRESH = "fresh"
    STALE = "stale"
    UNKNOWN = "unknown"


class TablePresenceStats(BaseModel):
    """Normalized per-table presence signal returned by an adapter (SG-5).

    Precedent for adapters returning domain models: Phase 10
    ``get_script_history -> ScriptRecord``. ``estimated_rows`` comes from
    planner metadata (e.g. ``reltuples``), never from ``COUNT(*)``
    (LESSONS_LEARNED §3).
    """

    model_config = ConfigDict(extra="ignore")

    object_schema: str
    name: str
    estimated_rows: int | None = None
    confidence: StatsConfidence = StatsConfidence.UNKNOWN


class DataPresence(str, Enum):
    """Result of the domain classification (SG-4).

    ``UNKNOWN`` means "cannot confidently declare the table empty" and is
    **treated as** ``HAS_DATA`` by the gate (fail-safe, CD-7: stale statistics
    count as data).
    """

    HAS_DATA = "has_data"
    EMPTY = "empty"
    UNKNOWN = "unknown"


class TableTouchKind(str, Enum):
    """How a table is touched by the pending delta (hash-level, SG-A).

    ``ADDED``/``UNCHANGED`` tables are safe by definition (CD-10: new objects
    are always allowed, untouched objects are no-ops) and are not modelled
    here; column-level change types are Phase 12 (CD-ALT-1).
    """

    CHANGED = "changed"   # present in both code and DB, sql_hash differs
    REMOVED = "removed"   # present in DB, absent from code (will be DROPped)


def classify_presence(stats: TablePresenceStats) -> DataPresence:
    """Classify data presence from the normalized signal (SG-4, fail-safe).

    Rules:

    * ``estimated_rows > 0``  -> ``HAS_DATA`` (regardless of confidence);
    * ``estimated_rows == 0`` **and** ``confidence == FRESH`` -> ``EMPTY``;
    * everything else (``0`` + ``STALE``/``UNKNOWN``, ``None``, negative
      sentinels such as PG's ``-1`` "never analyzed", unknown confidence)
      -> ``UNKNOWN``, which the gate treats as ``HAS_DATA``.
    """
    if stats.estimated_rows is not None and stats.estimated_rows > 0:
        return DataPresence.HAS_DATA
    if stats.estimated_rows == 0 and stats.confidence is StatsConfidence.FRESH:
        return DataPresence.EMPTY
    return DataPresence.UNKNOWN


class TouchedTable(BaseModel):
    """One table touched by the pending delta, with its gate assessment.

    ``covered_by`` lists the pre-script file names that declared this table in
    their autodoc ``project.covers`` (SG-3). Coverage is an author's claim —
    the gate checks the declaration, not the script semantics.
    """

    model_config = ConfigDict(extra="ignore")

    object_schema: str
    name: str
    touch: TableTouchKind
    estimated_rows: int | None = None
    confidence: StatsConfidence = StatsConfidence.UNKNOWN
    presence: DataPresence = DataPresence.UNKNOWN
    covered_by: list[str] = Field(default_factory=list)

    @property
    def covered(self) -> bool:
        return bool(self.covered_by)

    @property
    def is_violation(self) -> bool:
        """CD-10: a data-bearing touched table without a covering pre-script."""
        return self.presence in (DataPresence.HAS_DATA, DataPresence.UNKNOWN) and not self.covered


class VersionCheckOutcome(str, Enum):
    """Forward-only version relation between target DB and source (SG-6)."""

    PROCEED = "proceed"             # target None or older than source
    WARN_SAME = "warn_same"         # target == source ("forgot to bump?")
    ERROR_NEWER = "error_newer"     # target > source — forward-only violation


def check_version_relation(target: str | None, source: str) -> VersionCheckOutcome:
    """Compare calver versions (SG-6, forward-only — ROADMAP §7 item 8).

    Both arguments are calver ``YYYY.MM.DD.NN`` strings (fixed-width, so plain
    string comparison equals chronological order, CDF-9). ``target`` is the
    version recorded in the target DB (``__deploy.schema_version``), ``source``
    is ``dbpm.manifest.json:source_version``. Validation of the calver format
    itself is the manifest's job (Phase 10); this function only compares.
    """
    if target is None:
        return VersionCheckOutcome.PROCEED
    if target == source:
        return VersionCheckOutcome.WARN_SAME
    if target > source:
        return VersionCheckOutcome.ERROR_NEWER
    return VersionCheckOutcome.PROCEED


class SafetyGateVerdict(BaseModel):
    """Overall result of ``deploy analyze`` (CD-9).

    ``clean`` is False when at least one uncovered data-bearing touched table
    was found (safety-gate violation). The CLI maps: clean -> exit 0,
    violation -> exit 1, hard errors happen earlier (exit 2).
    """

    model_config = ConfigDict(extra="ignore")

    clean: bool
    db_type: str
    source_version: str | None = None
    target_version: str | None = None
    touched: list[TouchedTable] = Field(default_factory=list)
    # Phase 15.7: objects excluded from the whole analysis because their autodoc
    # has ``project.build: false`` — reported so their absence is explainable.
    ignored_build_false: int = 0

    @property
    def violations(self) -> list[TouchedTable]:
        return [t for t in self.touched if t.is_violation]
