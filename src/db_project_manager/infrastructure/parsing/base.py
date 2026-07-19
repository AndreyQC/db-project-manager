"""Generic object-graph parser contract.

A parser reads a directory of project files (SQL today, later Informatica /
Airflow / etc.) and produces a DependencyGraph of the objects defined there.

Phase 2 decision (vision Q1): the interface is intentionally not SQL-specific
so the same graph/toposort/deploy machinery can be reused for non-SQL projects
via concrete parser implementations.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from db_project_manager.domain.graph import DependencyGraph


class ObjectGraphParser(ABC):
    """Parse a project directory into a DependencyGraph."""

    @abstractmethod
    def parse_directory(self, root: str | Path) -> DependencyGraph:
        """Walk ``root``, read each project file, return vertices + edges."""

    @abstractmethod
    def supported_object_types(self) -> tuple[str, ...]:
        """Object types this parser knows how to extract (e.g. ('table', 'view'))."""
