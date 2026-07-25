"""Persistence of DependencyGraph to .dbm_graph/.

Layout under <codebase-root>/.dbm_graph/:
    vertices.json   — list of vertex dicts (pydantic model_dump)
    edges.json      — list of edge dicts
    graph.json      — {vertices, edges} single-file portability view
    meta.json       — format version, timestamps, codebase hash, counts

Phase 2 decision (vision Q2): .dbm_graph/ is gitignored — the graph is
deterministically rebuilt from the codebase in seconds. meta.json is still
written so we can detect a stale graph via codebase_hash.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from db_project_manager.domain.graph import DependencyGraph, Edge, Vertex

#: Bumped on incompatible changes to the file layout; readers reject mismatches.
FORMAT_VERSION = "1"

#: Directory name (relative to the codebase root).
GRAPH_DIR_NAME = ".dbm_graph"

#: File extensions hashed for the codebase fingerprint.
_HASHED_EXTENSIONS = (".sql",)


class GraphStoreError(Exception):
    """Raised on .dbm_graph/ read/write/version errors."""


def graph_dir_for(codebase_root: str | Path) -> Path:
    """Return the .dbm_graph path for a codebase root."""
    return Path(codebase_root) / GRAPH_DIR_NAME


def codebase_hash(codebase_root: str | Path) -> str:
    """Stable fingerprint of the SQL files under the codebase root.

    Order-independent (sorted file list + per-file sha256), so identical
    content on Windows/Linux produces the same hash.
    """
    root = Path(codebase_root)
    files = sorted(
        p for p in root.rglob("*")
        if p.is_file()
        and p.suffix.lower() in _HASHED_EXTENSIONS
        and GRAPH_DIR_NAME not in p.parts
    )
    hasher = hashlib.sha256()
    for path in files:
        rel = path.relative_to(root).as_posix()
        hasher.update(rel.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(path.read_bytes())
        hasher.update(b"\0")
    return hasher.hexdigest()


def write_graph(graph: DependencyGraph, codebase_root: str | Path) -> Path:
    """Write vertices/edges/graph/meta JSON under <root>/.dbm_graph/.

    Returns the .dbm_graph directory path.
    """
    root = Path(codebase_root)
    gdir = graph_dir_for(root)
    gdir.mkdir(parents=True, exist_ok=True)

    vertices_data = [v.model_dump(mode="json") for v in graph.vertices.values()]
    edges_data = [e.model_dump(mode="json") for e in graph.edges]

    _write_json(gdir / "vertices.json", vertices_data)
    _write_json(gdir / "edges.json", edges_data)
    _write_json(gdir / "graph.json", {"vertices": vertices_data, "edges": edges_data})

    meta = {
        "format_version": FORMAT_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "codebase_hash": codebase_hash(root),
        "vertex_count": len(vertices_data),
        "edge_count": len(edges_data),
    }
    _write_json(gdir / "meta.json", meta)
    return gdir


def read_graph(codebase_root: str | Path) -> DependencyGraph:
    """Read a previously written graph. Raises GraphStoreError if missing/stale."""
    gdir = graph_dir_for(codebase_root)
    graph_file = gdir / "graph.json"
    if not graph_file.exists():
        raise GraphStoreError(f"Граф не найден: {graph_file}. Выполните 'db-pm graph build'.")

    data = _read_json(graph_file)
    meta_file = gdir / "meta.json"
    if meta_file.exists():
        meta = _read_json(meta_file)
        if str(meta.get("format_version", "")) != FORMAT_VERSION:
            raise GraphStoreError(
                f"Версия формата графа {meta.get('format_version')!r} не поддерживается "
                f"(ожидается {FORMAT_VERSION}). Перестройте граф."
            )

    vertices = [Vertex.model_validate(v) for v in data.get("vertices", [])]
    edges = [Edge.model_validate(e) for e in data.get("edges", [])]
    # Relation is a str-Enum; pydantic parses it from the string value.
    return DependencyGraph(vertices={v.object_key: v for v in vertices}, edges=edges)


def is_stale(codebase_root: str | Path) -> bool:
    """True if the stored codebase_hash differs from the current one (or no graph)."""
    gdir = graph_dir_for(codebase_root)
    meta_file = gdir / "meta.json"
    if not meta_file.exists():
        return True
    meta = _read_json(meta_file)
    return meta.get("codebase_hash") != codebase_hash(codebase_root)


def _write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
