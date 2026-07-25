"""Graph export to standard formats for external tools.

  * json    — same layout as graph.json (portability, custom tooling)
  * graphml — XML format for Gephi / yEd / the Tauri graph-analyser app
  * dot     — Graphviz source

All exporters write human-readable output (no minification) for diff-ability.
"""

from __future__ import annotations

import json
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

from db_project_manager.domain.graph import DependencyGraph


def export_graph(graph: DependencyGraph, fmt: str, output: str | Path) -> Path:
    """Dispatch to the format-specific exporter.

    Args:
        graph: the graph to export.
        fmt: one of 'json', 'graphml', 'dot' (case-insensitive).
        output: destination file path.

    Returns:
        The output path.
    """
    fmt_normalized = fmt.lower()
    if fmt_normalized == "json":
        return _export_json(graph, output)
    if fmt_normalized == "graphml":
        return _export_graphml(graph, output)
    if fmt_normalized == "dot":
        return _export_dot(graph, output)
    raise ValueError(f"Неподдерживаемый формат экспорта: {fmt!r}. Допустимо: json, graphml, dot.")


# --- JSON ---


def _export_json(graph: DependencyGraph, output: str | Path) -> Path:
    data = {
        "vertices": [v.model_dump(mode="json") for v in graph.vertices.values()],
        "edges": [e.model_dump(mode="json") for e in graph.edges],
    }
    path = Path(output)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


# --- GraphML ---


def _export_graphml(graph: DependencyGraph, output: str | Path) -> Path:
    """Write GraphML 1.0 (directed).

    Vertices carry object_type/object_schema/object_name as data; edges carry
    relation/action. Openable in Gephi, yEd, and the Tauri app.
    """
    lines: list[str] = []
    lines.append('<?xml version="1.0" encoding="UTF-8"?>')
    lines.append(
        '<graphml xmlns="http://graphml.graphdrawing.org/xmlns" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:schemaLocation="http://graphml.graphdrawing.org/xmlns '
        'http://graphml.graphdrawing.org/xmlns/1.0/graphml.xsd">'
    )
    # Key declarations for vertex/edge data attributes.
    lines.append('<key id="object_type" for="node" attr.name="object_type" attr.type="string"/>')
    lines.append('<key id="object_schema" for="node" attr.name="object_schema" attr.type="string"/>')
    lines.append('<key id="object_name" for="node" attr.name="object_name" attr.type="string"/>')
    lines.append('<key id="build" for="node" attr.name="build" attr.type="boolean"/>')
    lines.append('<key id="relation" for="edge" attr.name="relation" attr.type="string"/>')
    lines.append('<key id="action" for="edge" attr.name="action" attr.type="string"/>')
    lines.append('<graph id="G" edgedefault="directed">')

    for vertex in graph.vertices.values():
        label = xml_escape(vertex.object_name or vertex.object_key)
        lines.append(f'  <node id="{xml_escape(vertex.object_key)}">')
        lines.append("    <data key=\"object_type\">" + xml_escape(vertex.object_type) + "</data>")
        lines.append("    <data key=\"object_schema\">" + xml_escape(str(vertex.object_schema)) + "</data>")
        lines.append("    <data key=\"object_name\">" + label + "</data>")
        lines.append(f'    <data key="build">{"true" if vertex.build else "false"}</data>')
        lines.append("  </node>")

    for i, edge in enumerate(graph.edges):
        eid = f"e{i}"
        lines.append(
            f'  <edge id="{eid}" source="{xml_escape(edge.source_object_key)}" '
            f'target="{xml_escape(edge.destination_object_key)}">'
        )
        lines.append("    <data key=\"relation\">" + xml_escape(edge.relation.value) + "</data>")
        lines.append("    <data key=\"action\">" + xml_escape(edge.action) + "</data>")
        lines.append("  </edge>")

    lines.append("</graph>")
    lines.append("</graphml>")
    path = Path(output)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# --- DOT (Graphviz) ---


def _export_dot(graph: DependencyGraph, output: str | Path) -> Path:
    """Write Graphviz DOT (directed). Vertices labelled by short object name."""
    lines: list[str] = ["digraph dbm_graph {", '  rankdir=LR;', '  node [shape=box, fontname="Helvetica"];']

    for vertex in graph.vertices.values():
        label = vertex.object_name or vertex.object_key
        # Color build=false vertices differently so they stand out.
        attrs = f'label="{_dot_escape(label)}"'
        if not vertex.build:
            attrs += ', style="filled", fillcolor="lightgray"'
        lines.append(f'  "{_dot_escape(vertex.object_key)}" [{attrs}];')

    for edge in graph.edges:
        lines.append(
            f'  "{_dot_escape(edge.source_object_key)}" -> '
            f'"{_dot_escape(edge.destination_object_key)}" '
            f'[label="{_dot_escape(edge.action or edge.relation.value)}"];'
        )

    lines.append("}")
    path = Path(output)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _dot_escape(s: str) -> str:
    """Escape characters that are special in DOT identifiers/labels."""
    return s.replace("\\", "\\\\").replace('"', '\\"')
