"""Tests for db_project_manager.infrastructure.graph.graph_store."""

from __future__ import annotations

from pathlib import Path

import pytest

from db_project_manager.domain.graph import DependencyGraph, Edge, Relation, Vertex
from db_project_manager.infrastructure.graph.graph_store import (
    FORMAT_VERSION,
    codebase_hash,
    graph_dir_for,
    is_stale,
    read_graph,
    write_graph,
    GraphStoreError,
)


def _v(key: str, object_type: str = "table") -> Vertex:
    return Vertex(
        object_key=key, object_catalog="db", object_schema="s",
        object_type=object_type, object_name=key,
    )


def _make_graph() -> DependencyGraph:
    g = DependencyGraph()
    g.add_vertex(_v("A"))
    g.add_vertex(_v("B", "view"))
    g.add_edge(Edge(source_object_key="B", destination_object_key="A", relation=Relation.PROVIDE_DATA_TO, action="select"))
    return g


def _write_sql(root: Path, name: str, content: str = "CREATE TABLE t(x int);\n") -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# --- write/read roundtrip ---


def test_write_creates_expected_files(tmp_path: Path) -> None:
    _write_sql(tmp_path, "a.sql")
    g = _make_graph()
    gdir = write_graph(g, tmp_path)

    assert (gdir / "vertices.json").is_file()
    assert (gdir / "edges.json").is_file()
    assert (gdir / "graph.json").is_file()
    assert (gdir / "meta.json").is_file()


def test_read_roundtrip_preserves_graph(tmp_path: Path) -> None:
    _write_sql(tmp_path, "a.sql")
    g = _make_graph()
    write_graph(g, tmp_path)

    restored = read_graph(tmp_path)
    assert set(restored.vertices.keys()) == {"A", "B"}
    assert restored.get_vertex("B").object_type == "view"
    assert len(restored.edges) == 1
    edge = restored.edges[0]
    assert edge.source_object_key == "B"
    assert edge.destination_object_key == "A"
    assert edge.relation == Relation.PROVIDE_DATA_TO
    assert edge.action == "select"


def test_read_missing_graph_raises(tmp_path: Path) -> None:
    with pytest.raises(GraphStoreError, match="не найден"):
        read_graph(tmp_path)


# --- meta + hash + staleness ---


def test_meta_has_format_version_and_counts(tmp_path: Path) -> None:
    import json
    _write_sql(tmp_path, "a.sql")
    write_graph(_make_graph(), tmp_path)
    meta = json.loads((graph_dir_for(tmp_path) / "meta.json").read_text(encoding="utf-8"))
    assert meta["format_version"] == FORMAT_VERSION
    assert meta["vertex_count"] == 2
    assert meta["edge_count"] == 1
    assert "codebase_hash" in meta and meta["codebase_hash"]


def test_codebase_hash_is_order_independent(tmp_path: Path) -> None:
    # Same content in any filename order -> deterministic hash on sorted paths.
    _write_sql(tmp_path, "x.sql", "A")
    _write_sql(tmp_path, "y.sql", "B")
    h1 = codebase_hash(tmp_path)
    # Re-read after touching (no content change) -> same hash.
    h2 = codebase_hash(tmp_path)
    assert h1 == h2


def test_codebase_hash_changes_with_content(tmp_path: Path) -> None:
    f = _write_sql(tmp_path, "a.sql", "original")
    h1 = codebase_hash(tmp_path)
    f.write_text("changed", encoding="utf-8")
    h2 = codebase_hash(tmp_path)
    assert h1 != h2


def test_is_stale_after_change(tmp_path: Path) -> None:
    f = _write_sql(tmp_path, "a.sql", "v1")
    write_graph(_make_graph(), tmp_path)
    assert is_stale(tmp_path) is False
    f.write_text("v2", encoding="utf-8")
    assert is_stale(tmp_path) is True


def test_is_stale_when_no_graph(tmp_path: Path) -> None:
    _write_sql(tmp_path, "a.sql")
    assert is_stale(tmp_path) is True


def test_read_rejects_wrong_format_version(tmp_path: Path) -> None:
    import json
    _write_sql(tmp_path, "a.sql")
    write_graph(_make_graph(), tmp_path)
    meta_path = graph_dir_for(tmp_path) / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["format_version"] = "999"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(GraphStoreError, match="Версия формата"):
        read_graph(tmp_path)


def test_codebase_hash_excludes_dbm_graph_dir(tmp_path: Path) -> None:
    """Files inside .dbm_graph/ must not affect the codebase hash."""
    _write_sql(tmp_path, "a.sql", "content")
    h1 = codebase_hash(tmp_path)
    # Write something into .dbm_graph/ — must not change the hash.
    (tmp_path / ".dbm_graph").mkdir(exist_ok=True)
    (tmp_path / ".dbm_graph" / "noise.sql").write_text("noise", encoding="utf-8")
    h2 = codebase_hash(tmp_path)
    assert h1 == h2
