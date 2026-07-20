"""PostgreSQL/Greenplum SQL parser (Phase 2 MVP backend).

Ported from POC dflw_parser_pg_sql.py with the following fixes (vision Q1):

  * autodoc-driven identity: when a file has an autodoc header, object_key /
    type / schema / name / build come from there (single source of truth).
    Tokenization is only a fallback for files without autodoc.
  * join precedence: ``left join`` / ``full outer join`` are checked BEFORE
    the generic ``join`` branch (POC had the generic branch first, swallowing
    all join variants — unreachable-elif bug).
  * edge de-duplication uses Edge.dedup_key() (hashable tuple) instead of the
    POC's set(tuple(sorted(d.items()))) that broke on mixed value types.

Known limitations (deliberate for MVP; sqlglot backend addresses them later):
  * Identifier matching is token-equality after normalization; aliases and
    qualified names may produce false negatives/positives.
  * CTEs, dynamic SQL, and comments are not parsed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger

from db_project_manager.domain.graph import DependencyGraph, Edge, Relation, Vertex
from db_project_manager.infrastructure.parsing.base import ObjectGraphParser
from db_project_manager.infrastructure.parsing.normalize import (
    get_normalized_file_content,
    get_object_name,
)
from db_project_manager.infrastructure.sql.autodoc import extract_header

#: Object types this parser can extract.
SUPPORTED_TYPES: tuple[str, ...] = (
    "schema",
    "sequence",
    "table",
    "external_table",
    "view",
    "materialized_view",
    "function",
    "procedure",
    "trigger",
)

#: Mapping from keyword after CREATE -> object_type. Order matters: longest
#: multi-word keywords first so they win over single-word ones.
_CREATE_KEYWORD_TO_TYPE: tuple[tuple[tuple[str, ...], str], ...] = (
    (("readable", "table"), "external_table"),
    (("writable", "table"), "external_table"),
    (("materialized", "view"), "materialized_view"),
    (("or", "replace", "view"), "view"),
    (("or", "replace", "function"), "function"),
    (("or", "replace", "procedure"), "procedure"),
    (("or", "replace", "trigger"), "trigger"),
    (("schema",), "schema"),
    (("sequence",), "sequence"),
    (("table",), "table"),
    (("view",), "view"),
    (("function",), "function"),
    (("procedure",), "procedure"),
    (("proc",), "procedure"),
    (("trigger",), "trigger"),
)

#: Default catalog name used in object_key when autodoc is missing.
_FALLBACK_CATALOG = "codebase"

#: Directory names skipped when walking the codebase.
_SKIP_DIRS = {".dbm_graph", ".git", ".venv", "__pycache__", "node_modules"}


def _relative_posix(path: Path, root: Path) -> str:
    """Return path relative to root using forward slashes (OS-independent).

    Stored in object_source_file and serialized into .dbm_graph/, so it must
    look identical on Windows and Linux for reproducible graphs.
    """
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    return rel.as_posix()


class PgSqlParser(ObjectGraphParser):
    """Parse a directory of SQL files into a DependencyGraph."""

    def supported_object_types(self) -> tuple[str, ...]:
        return SUPPORTED_TYPES

    def parse_directory(self, root: str | Path) -> DependencyGraph:
        root = Path(root)
        if not root.is_dir():
            raise NotADirectoryError(f"Каталог кодовой базы не найден: {root}")

        sql_files = sorted(p for p in self._iter_sql_files(root))
        logger.info(f"Найдено SQL-файлов для парсинга: {len(sql_files)}")

        graph = DependencyGraph()
        parsed: list[dict[str, Any]] = []  # vertex + words used for edge scan

        # --- vertices ---
        for path in sql_files:
            try:
                vertex, words = self._parse_file(path, root)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Не удалось разобрать файл {path}: {e}")
                continue
            if vertex is None:
                continue
            graph.add_vertex(vertex)
            parsed.append({"vertex": vertex, "words": words, "path": path})

        # --- edges ---
        # Match each object name / full_name against every file's word stream.
        names_index = self._build_names_index(graph)
        for entry in parsed:
            edges = self._scan_edges(entry["words"], entry["vertex"], names_index)
            for edge in edges:
                graph.add_edge(edge)

        logger.info(
            f"Парсинг завершён: вершин={len(graph.vertices)}, рёбер={len(graph.edges)}"
        )
        return graph

    # --- file walking ---

    @staticmethod
    def _iter_sql_files(root: Path):
        for path in root.rglob("*.sql"):
            if any(part in _SKIP_DIRS for part in path.parts):
                continue
            yield path

    # --- per-file parsing ---

    def _parse_file(self, path: Path, root: Path) -> tuple[Vertex | None, list[str]]:
        raw = path.read_text(encoding="utf-8-sig")
        normalized = get_normalized_file_content(raw)
        words = normalized.split(" ")

        autodoc = extract_header(raw)
        if autodoc and autodoc.get("object"):
            obj_meta = autodoc["object"]
            object_type = str(obj_meta.get("object_type", "")).lower()
            if object_type not in SUPPORTED_TYPES:
                logger.debug(f"Пропуск файла {path}: неподдерживаемый тип {object_type!r}")
                return None, []
            schema = obj_meta.get("object_schema")
            name = str(obj_meta.get("object_name", ""))
            object_key = str(obj_meta.get("object_key", ""))
            catalog = str(obj_meta.get("object_catalog", _FALLBACK_CATALOG))
            build = bool(autodoc.get("project", {}).get("build", True))
            # Phase 4: signature is embedded in the autodoc for overloaded
            # functions/procedures. object_key already carries it as a
            # /signature/<hash> suffix (written by SQLGenerator), so the parser
            # does not rebuild the key — it only restores the signature field
            # for downstream consumers (Vertex serialization, future edge work).
            signature = str(obj_meta.get("object_signature", "") or "")
            vertex = Vertex(
                object_key=object_key,
                object_catalog=catalog,
                object_schema=schema,
                object_type=object_type,
                object_name=name,
                object_signature=signature,
                object_source_file=_relative_posix(path, root),
                build=build,
            )
            return vertex, words

        # Fallback: tokenize to find object type/name (no autodoc).
        extracted = self._extract_object_from_words(words)
        if not extracted["type"] or extracted["type"] == "null":
            logger.debug(f"Пропуск файла {path}: тип объекта не определён")
            return None, []
        schema = extracted.get("schema") or "public"
        name = extracted.get("name", "")
        object_key = (
            f"pg_database/{_FALLBACK_CATALOG}/schema/{schema}/type/{extracted['type']}/name/{name}"
        )
        vertex = Vertex(
            object_key=object_key,
            object_catalog=_FALLBACK_CATALOG,
            object_schema=schema,
            object_type=extracted["type"],
            object_name=name,
            object_source_file=_relative_posix(path, root),
            build=True,
        )
        return vertex, words

    def _extract_object_from_words(self, words: list[str]) -> dict[str, Any]:
        """Token-based fallback when a file has no autodoc header."""
        result: dict[str, Any] = {"type": "null", "full_name": "null"}
        for i, word in enumerate(words):
            if word != "create":
                continue
            rest = words[i + 1 : i + 4]
            for keywords, obj_type in _CREATE_KEYWORD_TO_TYPE:
                if tuple(rest[: len(keywords)]) == keywords:
                    name_token = rest[len(keywords)]
                    parsed = get_object_name(name_token)
                    result.update(
                        type=obj_type,
                        full_name=parsed["full_name"],
                        schema=parsed["schema"],
                        name=parsed["name"],
                    )
                    return result
        return result

    # --- edge scanning ---

    def _build_names_index(self, graph: DependencyGraph) -> dict[str, str]:
        """Map every known object identifier -> object_key.

        We index by bare name and full_name; the scanner looks up tokens.
        """
        index: dict[str, str] = {}
        for key, vertex in graph.vertices.items():
            if vertex.object_name:
                index.setdefault(vertex.object_name, key)
            if vertex.object_schema and vertex.object_name:
                full = f"{vertex.object_schema}.{vertex.object_name}"
                index.setdefault(full, key)
        return index

    def _scan_edges(
        self,
        words: list[str],
        vertex: Vertex,
        names_index: dict[str, str],
    ) -> list[Edge]:
        """Scan a file's normalized word stream for references to other objects.

        Each match produces a directed edge source -> destination where
        ``source`` is the current object (dependent) and ``destination`` is the
        referenced object (dependency). Direction follows the POC convention.
        """
        edges: list[Edge] = []
        seen: set[tuple[str, str, str, str]] = set()

        # Skip the autodoc comment block at the start: it lists identity, not
        # SQL references, and may produce false matches.
        scan_words = self._strip_autodoc(words)

        self_vertex_key = vertex.object_key
        for i, token in enumerate(scan_words):
            dest_key = names_index.get(token)
            if not dest_key or dest_key == self_vertex_key:
                continue

            relation, action = self._classify_at(scan_words, i)
            if relation is None:
                continue

            edge = Edge(
                source_object_key=self_vertex_key,
                destination_object_key=dest_key,
                relation=relation,
                action=action,
            )
            if edge.dedup_key() in seen:
                continue
            seen.add(edge.dedup_key())
            edges.append(edge)

        return edges

    @staticmethod
    def _strip_autodoc(words: list[str]) -> list[str]:
        """Drop the leading autodoc comment block from the word stream.

        If the markers are present, scanning starts after the closing marker.
        """
        close_marker = "[[autodoc-yaml]>]".lower()
        try:
            close_idx = words.index(close_marker)
        except ValueError:
            return words
        # Skip past the comment-closing '*/' if present shortly after.
        tail = words[close_idx + 1 :]
        try:
            star_idx = tail.index("*/")
            return tail[star_idx + 1 :]
        except ValueError:
            return tail

    @staticmethod
    def _classify_at(words: list[str], i: int) -> tuple[Relation | None, str]:
        """Classify the reference at position ``i`` using the preceding tokens.

        Returns (relation, action) or (None, ""). Order of checks matters:
        more specific patterns (left join, full outer join) must come BEFORE
        the generic 'join'.
        """
        prev = words[i - 1] if i >= 1 else ""
        prev2 = words[i - 2] if i >= 2 else ""
        prev3 = words[i - 3] if i >= 3 else ""

        # FK declaration
        if prev == "references":
            return Relation.REFERENCES_BY, "references"

        # Sequence usage
        if prev == "nextval":
            return Relation.SEQUENCE_NEXTVAL_IN, "nextval"

        # JOIN variants — specific first (vision Q1 fix)
        if prev == "join" and prev2 == "left":
            return Relation.PROVIDE_DATA_TO, "select left join"
        if prev == "join" and prev2 == "right":
            return Relation.PROVIDE_DATA_TO, "select right join"
        if prev == "join" and prev2 == "outer" and prev3 == "full":
            return Relation.PROVIDE_DATA_TO, "select full outer join"
        if prev == "join" and prev2 == "inner":
            return Relation.PROVIDE_DATA_TO, "select inner join"
        if prev == "join":
            return Relation.PROVIDE_DATA_TO, "select inner join"

        # DML
        if prev == "from" and prev2 == "delete":
            return Relation.CHANGE_DATA_IN, "delete by"
        if prev == "into" and prev2 == "insert":
            return Relation.CHANGE_DATA_IN, "insert by"
        if prev == "into" and prev2 == "merge":
            return Relation.CHANGE_DATA_IN, "upsert by"
        if prev == "table" and prev2 == "truncate":
            return Relation.CHANGE_DATA_IN, "truncate by"
        if prev == "update":
            return Relation.CHANGE_DATA_IN, "update by"

        # SELECT (plain FROM or after the JOIN chain)
        if prev == "from":
            return Relation.PROVIDE_DATA_TO, "select"

        return None, ""
