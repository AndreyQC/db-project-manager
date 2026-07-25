"""Application service: build a DependencyGraph from a codebase directory.

Used by:
  * CLI 'db-pm graph build' — produces .dbm_graph/ artifacts.
  * DeployValidateService — to obtain the deploy-ordered object list.

The service wires the parser (infrastructure) with the graph store
(.dbm_graph/ persistence) so callers stay free of those details.
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from db_project_manager.domain.graph import DependencyGraph
from db_project_manager.infrastructure.graph import graph_store
from db_project_manager.infrastructure.graph.topological_sort import sort_by_type_and_topology
from db_project_manager.infrastructure.parsing.base import ObjectGraphParser
from db_project_manager.infrastructure.parsing.pg_sql_parser import PgSqlParser


class BuildGraphService:
    """Build (and optionally persist) the dependency graph of a codebase."""

    def __init__(self, parser: ObjectGraphParser | None = None) -> None:
        self.parser = parser or PgSqlParser()

    def build(self, codebase_dir: str | Path) -> DependencyGraph:
        """Parse the codebase and return the in-memory graph (no I/O to disk)."""
        codebase_dir = Path(codebase_dir)
        logger.info(f"Построение графа по кодовой базе: {codebase_dir}")
        graph = self.parser.parse_directory(codebase_dir)
        logger.info(
            f"Граф построен: вершин={len(graph.vertices)}, рёбер={len(graph.edges)}"
        )
        return graph

    def build_and_store(self, codebase_dir: str | Path) -> Path:
        """Build the graph and write it to <codebase>/.dbm_graph/."""
        codebase_dir = Path(codebase_dir)
        graph = self.build(codebase_dir)
        gdir = graph_store.write_graph(graph, codebase_dir)
        logger.info(f"Граф записан в: {gdir}")
        return gdir

    def deploy_order(self, codebase_dir: str | Path, *, build_only: bool = True) -> list:
        """Return vertices in deploy order (type+topology).

        Args:
            codebase_dir: codebase root to parse.
            build_only: if True (default), filter out build=false vertices —
                this is the deploy-time view (vision Q8).
        """
        graph = self.build(codebase_dir)
        if build_only:
            graph = graph.filter_build_true()
        return sort_by_type_and_topology(graph)
