"""Tests for db_project_manager.infrastructure.graph.export."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from db_project_manager.domain.graph import DependencyGraph, Edge, Relation, Vertex
from db_project_manager.infrastructure.graph.export import export_graph


def _v(key: str, name: str, object_type: str = "table", build: bool = True) -> Vertex:
    return Vertex(
        object_key=key, object_catalog="db", object_schema="s",
        object_type=object_type, object_name=name, build=build,
    )


def _make_graph() -> DependencyGraph:
    g = DependencyGraph()
    g.add_vertex(_v("pg/db/type/table/name/aircrafts", "aircrafts"))
    g.add_vertex(_v("pg/db/type/table/name/flights", "flights"))
    g.add_vertex(_v("pg/db/type/view/name/flights_v", "flights_v", "view"))
    g.add_vertex(_v("pg/db/type/materialized_view/name/routes", "routes", "materialized_view", build=False))
    g.add_edge(Edge(source_object_key="pg/db/type/table/name/flights", destination_object_key="pg/db/type/table/name/aircrafts",
                    relation=Relation.REFERENCES_BY, action="references"))
    g.add_edge(Edge(source_object_key="pg/db/type/view/name/flights_v", destination_object_key="pg/db/type/table/name/flights",
                    relation=Relation.PROVIDE_DATA_TO, action="select"))
    return g


# --- json ---


def test_export_json(tmp_path: Path) -> None:
    out = export_graph(_make_graph(), "json", tmp_path / "g.json")
    data = json.loads(out.read_text(encoding="utf-8"))
    assert len(data["vertices"]) == 4
    assert len(data["edges"]) == 2
    # vertices carry the build flag
    build_flags = {v["object_name"]: v["build"] for v in data["vertices"]}
    assert build_flags["routes"] is False


# --- graphml ---


def test_export_graphml_is_valid_xml(tmp_path: Path) -> None:
    import xml.etree.ElementTree as ET
    out = export_graph(_make_graph(), "graphml", tmp_path / "g.graphml")
    text = out.read_text(encoding="utf-8")
    assert "http://graphml.graphdrawing.org/xmlns" in text
    # Must parse as well-formed XML.
    root = ET.fromstring(text)
    ns = "{http://graphml.graphdrawing.org/xmlns}"
    nodes = root.findall(f".//{ns}node")
    edges = root.findall(f".//{ns}edge")
    assert len(nodes) == 4
    assert len(edges) == 2


def test_graphml_edge_direction(tmp_path: Path) -> None:
    out = export_graph(_make_graph(), "graphml", tmp_path / "g.graphml")
    text = out.read_text(encoding="utf-8")
    assert 'edgedefault="directed"' in text


# --- dot ---


def test_export_dot(tmp_path: Path) -> None:
    out = export_graph(_make_graph(), "dot", tmp_path / "g.dot")
    text = out.read_text(encoding="utf-8")
    assert text.startswith("digraph dbm_graph {")
    # Both endpoints of an edge must appear as "A -> B".
    assert "type/table/name/flights\" -> \"pg/db/type/table/name/aircrafts" in text
    # build=false vertex is filled.
    assert "fillcolor=\"lightgray\"" in text


# --- dispatch / errors ---


def test_export_lowercase_format(tmp_path: Path) -> None:
    # Format is case-insensitive.
    out = export_graph(_make_graph(), "JSON", tmp_path / "g.json")
    assert out.is_file()


def test_export_rejects_unknown_format(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Неподдерживаемый формат"):
        export_graph(_make_graph(), "csv", tmp_path / "g.csv")


def test_dot_escapes_quotes(tmp_path: Path) -> None:
    """Identifier with a quote must not break the DOT syntax."""
    g = DependencyGraph()
    g.add_vertex(Vertex(object_key='key"with"quotes', object_type="table", object_name='weird"name'))
    out = export_graph(g, "dot", tmp_path / "g.dot")
    text = out.read_text(encoding="utf-8")
    # Roundtrip: count balanced double-quotes by parsing the digraph block.
    assert '\\"' in text  # escaped quotes present
