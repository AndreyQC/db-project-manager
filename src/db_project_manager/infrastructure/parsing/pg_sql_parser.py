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
from db_project_manager.domain.signature import canonical_signature
from db_project_manager.infrastructure.parsing.base import ObjectGraphParser
from db_project_manager.infrastructure.parsing.normalize import (
    get_normalized_file_content,
    get_object_name,
)
from db_project_manager.infrastructure.parsing.overload_resolution import (
    find_calls,
    infer_call_signature,
    resolve_overload,
    split_call_args,
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
    # Phase 5: schema-less global objects.
    "extension",
    "database_setting",
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
    # Phase 5: schema-less global objects (autodoc path is primary; token path as fallback).
    (("extension",), "extension"),
    (("database", "setting"), "database_setting"),
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

    def __init__(self) -> None:
        # Phase 8: notes collected during edge scanning for the overload
        # resolution report. Reset on every parse_directory call. Kept on the
        # instance (not returned) so parse_directory's DependencyGraph contract
        # stays unchanged (LESSONS §18); BuildGraphService reads this after build.
        self._resolution_notes: list[dict[str, Any]] = []

    def supported_object_types(self) -> tuple[str, ...]:
        return SUPPORTED_TYPES

    def parse_directory(self, root: str | Path) -> DependencyGraph:
        root = Path(root)
        if not root.is_dir():
            raise NotADirectoryError(f"Каталог кодовой базы не найден: {root}")

        # Reset per-build: each graph build is independent.
        self._resolution_notes = []

        sql_files = sorted(p for p in self._iter_sql_files(root))
        logger.info(f"Найдено SQL-файлов для парсинга: {len(sql_files)}")

        graph = DependencyGraph()
        parsed: list[dict[str, Any]] = []  # vertex + words + raw used for edge scan

        # --- vertices ---
        for path in sql_files:
            try:
                vertex, words, raw = self._parse_file(path, root)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Не удалось разобрать файл {path}: {e}")
                continue
            if vertex is None:
                continue
            graph.add_vertex(vertex)
            parsed.append({"vertex": vertex, "words": words, "raw": raw, "path": path})

        # --- edges ---
        # Match each object name / full_name against every file's word stream.
        names_index = self._build_names_index(graph)
        overload_index = self._build_overload_index(graph)
        for entry in parsed:
            edges = self._scan_edges(
                entry["words"], entry["raw"], entry["vertex"],
                names_index, overload_index, graph,
            )
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

    def _parse_file(self, path: Path, root: Path) -> tuple[Vertex | None, list[str], str]:
        raw = path.read_text(encoding="utf-8-sig")
        normalized = get_normalized_file_content(raw)
        words = normalized.split(" ")

        autodoc = extract_header(raw)
        if autodoc and autodoc.get("object"):
            obj_meta = autodoc["object"]
            object_type = str(obj_meta.get("object_type", "")).lower()
            if object_type not in SUPPORTED_TYPES:
                logger.debug(f"Пропуск файла {path}: неподдерживаемый тип {object_type!r}")
                return None, [], ""
            schema = obj_meta.get("object_schema")
            name = str(obj_meta.get("object_name", ""))
            object_key = str(obj_meta.get("object_key", ""))
            catalog = str(obj_meta.get("object_catalog", _FALLBACK_CATALOG))
            build = bool(autodoc.get("project", {}).get("build", True))
            # Phase 10 (CDF-10): immutable marker — object managed by db-pm
            # (e.g. lives in __deploy service schema). Defaults to False.
            immutable = bool(autodoc.get("project", {}).get("immutable", False))
            # Phase 4: signature is embedded in the autodoc for overloaded
            # functions/procedures. object_key already carries it as a
            # /signature/<hash> suffix (written by SQLGenerator), so the parser
            # does not rebuild the key — it only restores the signature field
            # for downstream consumers (Vertex serialization, future edge work).
            signature = str(obj_meta.get("object_signature", "") or "")
            # Phase 8: raw argument type list (e.g. "int4", "text,varchar") for
            # overload resolution. Carries DATA, not identity — empty for non-
            # routine / no-arg objects and for autodoc headers written before
            # Phase 8 (resolution then falls back to first-wins, as before).
            argument_types = str(obj_meta.get("argument_types", "") or "")
            # Phase 5: extra carries db_properties for database_setting
            # (encoding/locale from CREATE DATABASE — needed by deploy P5.S07).
            extra: dict[str, Any] = {}
            if object_type == "database_setting" and "properties" in obj_meta:
                extra["db_properties"] = obj_meta["properties"]
            vertex = Vertex(
                object_key=object_key,
                object_catalog=catalog,
                object_schema=schema,
                object_type=object_type,
                object_name=name,
                object_signature=signature,
                argument_types=argument_types,
                object_source_file=_relative_posix(path, root),
                build=build,
                immutable=immutable,
                **({"extra": extra} if extra else {}),
            )
            return vertex, words, raw

        # Fallback: tokenize to find object type/name (no autodoc).
        extracted = self._extract_object_from_words(words)
        if not extracted["type"] or extracted["type"] == "null":
            logger.debug(f"Пропуск файла {path}: тип объекта не определён")
            return None, [], ""
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
        return vertex, words, raw

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
        First-wins on collisions (overloads share a bare name) — the overload
        index below carries the extra data needed to disambiguate call sites.
        """
        index: dict[str, str] = {}
        for key, vertex in graph.vertices.items():
            if vertex.object_name:
                index.setdefault(vertex.object_name, key)
            if vertex.object_schema and vertex.object_name:
                full = f"{vertex.object_schema}.{vertex.object_name}"
                index.setdefault(full, key)
        return index

    @staticmethod
    def _build_overload_index(
        graph: DependencyGraph,
    ) -> dict[str, list[tuple[str, tuple[str, ...]]]]:
        """Map overloaded routine identifiers -> list of ``(object_key, canonical_types)``.

        Only function/procedure names that resolve to MORE than one vertex are
        included — a singleton has nothing to disambiguate and keeps the legacy
        first-wins edge (LESSONS §38: the call-edge branch already handles it).
        Each overload's canonical type tuple is derived from
        ``Vertex.argument_types`` via ``domain.signature.canonical_signature``
        (LESSONS §27: reuse, do not reinvent). A routine whose argument_types is
        empty (no autodoc data, or a no-arg function) yields an empty tuple —
        such overloads can still be matched against a zero-arg call.

        Keys are both the bare name and the ``schema.name`` form, matching
        ``_build_names_index``, so a call can be looked up however it appears.
        """
        # Group routine vertices by bare name to detect overload groups.
        groups: dict[str, list[Vertex]] = {}
        for vertex in graph.vertices.values():
            if vertex.object_type not in {"function", "procedure"} or not vertex.object_name:
                continue
            groups.setdefault(vertex.object_name, []).append(vertex)

        overload_index: dict[str, list[tuple[str, tuple[str, ...]]]] = {}
        for name, vertices in groups.items():
            if len(vertices) < 2:
                continue
            entries: list[tuple[str, tuple[str, ...]]] = []
            for vertex in vertices:
                canon = canonical_signature(vertex.argument_types)
                types_tuple = tuple(t for t in canon.split(",") if t)
                entries.append((vertex.object_key, types_tuple))
            overload_index[name] = entries
            # Also index the schema-qualified form for each distinct schema.
            schemas = {vertex.object_schema for vertex in vertices if vertex.object_schema}
            for schema in schemas:
                overload_index[f"{schema}.{name}"] = entries
        return overload_index

    def _scan_edges(
        self,
        words: list[str],
        raw: str,
        vertex: Vertex,
        names_index: dict[str, str],
        overload_index: dict[str, list[tuple[str, tuple[str, ...]]]],
        graph: DependencyGraph,
    ) -> list[Edge]:
        """Scan a file's normalized word stream for references to other objects.

        Each match produces a directed edge source -> destination where
        ``source`` is the current object (dependent) and ``destination`` is the
        referenced object (dependency). Direction follows the POC convention.

        Phase 8: when a matched routine name is overloaded (present in
        ``overload_index``), the destination is resolved by inspecting the call
        sites in the file's RAW SQL (parens are lost in the normalized stream).
        For each call we infer the argument-type signature from literals and
        match it against the overloads; a single exact match routes the edge to
        that overload. Ambiguous/uninferrable calls fall back to first-wins and
        are recorded for the resolution report (P8.S7).
        """
        edges: list[Edge] = []
        seen: set[tuple[str, str, str, str]] = set()

        # Skip the autodoc comment block at the start: it lists identity, not
        # SQL references, and may produce false matches.
        scan_words = self._strip_autodoc(words)

        self_vertex_key = vertex.object_key
        self_schema = vertex.object_schema

        # Phase 8: overloaded routine names are resolved by a dedicated pass
        # over the raw SQL (one edge per resolved overload), so the main word
        # loop skips them to avoid emitting a stale "first wins" edge. Collect
        # the names handled that way.
        resolved_overload_names: set[str] = set()

        for i, token in enumerate(scan_words):
            # Context-aware sequence lookup: for nextval tokens, try the source
            # object's schema first (e.g. qr.audit_log_id_seq from table qr.audit_log),
            # then fall back to bare name. This handles cases where the table's
            # schema differs from public and nextval() omits the schema qualifier.
            dest_key = names_index.get(token)
            if not dest_key or dest_key == self_vertex_key:
                continue

            # Function/procedure call: if the matched name resolves to a
            # function or procedure vertex, treat it as a call → DEPENDS_ON.
            # Parens are stripped by the normalizer so we cannot check for '(';
            # instead we rely on the destination vertex's object_type. This
            # catches bare calls (sp_helper(x)) and schema-qualified calls
            # (qr.sp_helper(x)) regardless of surrounding SQL context (SELECT,
            # FROM, CASE WHEN, WHERE). The only risk is a column name matching
            # a function name — but the index contains object names only, not
            # column names, so collisions are rare.
            dest_vertex = graph.vertices.get(dest_key)
            if dest_vertex is not None and dest_vertex.object_type in {"function", "procedure"}:
                # Phase 8: an overloaded name is handled by the post-loop pass
                # below (one edge per resolved overload). Skip it here so the
                # first-wins edge does not mask the resolved ones. The actual
                # call/JOIN classification is deferred to that pass too.
                overloads = overload_index.get(token) or overload_index.get(
                    f"{self_schema}.{token}" if self_schema else ""
                )
                if overloads:
                    resolved_overload_names.add(token)
                    continue
                # FROM/JOIN of a function is a table-function call — keep the
                # existing PROVIDE_DATA_TO classification for ordering purposes.
                prev = scan_words[i - 1] if i >= 1 else ""
                if prev not in {"from", "join"}:
                    relation = Relation.DEPENDS_ON
                    action = "call"
                else:
                    relation, action = Relation.PROVIDE_DATA_TO, "select function"
            else:
                # For nextval references, prefer schema-qualified lookup using the
                # source object's schema. E.g. table in schema 'qr' with
                # nextval('audit_log_id_seq') -> try 'qr.audit_log_id_seq' first.
                relation, action = self._classify_at(scan_words, i)
                if relation is None:
                    continue
                if relation == Relation.SEQUENCE_NEXTVAL_IN and self_schema:
                    qualified = f"{self_schema}.{token}"
                    dest_key = names_index.get(qualified) or dest_key

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

        # Phase 8 pass: resolve overloaded routine calls from the raw SQL. Each
        # distinct resolved overload gets its own edge (dedup keeps one per
        # destination). Unresolved/ambiguous calls fall back to first-wins and
        # are recorded for the report. The caller's surrounding context (CALL vs
        # FROM/JOIN) is not re-derived here — all routine-call edges use the
        # generic "call" classification, consistent with the singleton path above
        # for non-FROM call sites (the FROM/JOIN of an overloaded table function
        # is a rare edge case and is accepted as a follow-up).
        #
        # A matched token may be the bare name (sp_x) or a schema-qualified form
        # (app.sp_x) — both resolve to the same overload group, so we process
        # each bare name only once. The schema used for call discovery is taken
        # from the qualified token when present, else from the source vertex.
        handled_bare: set[str] = set()
        for token in sorted(resolved_overload_names):
            bare_name = token.rsplit(".", 1)[-1]
            call_schema = token.rsplit(".", 1)[0] if "." in token else self_schema
            if bare_name in handled_bare:
                continue
            handled_bare.add(bare_name)
            overloads = overload_index.get(bare_name) or overload_index.get(token)
            if not overloads:
                continue
            resolved_keys, unresolved_count, total_calls = self._resolve_overloaded_calls(
                raw, call_schema, bare_name, overloads
            )
            for dest_key in resolved_keys:
                edge = Edge(
                    source_object_key=self_vertex_key,
                    destination_object_key=dest_key,
                    relation=Relation.DEPENDS_ON,
                    action="call",
                )
                if edge.dedup_key() in seen:
                    continue
                seen.add(edge.dedup_key())
                edges.append(edge)
            # No call sites at all in this file (the name only appeared as a
            # definition, a comment, or in the autodoc block) — there is nothing
            # to resolve and no edge to emit. This is distinct from "calls
            # existed but failed to resolve": a zero-call match is silent.
            if total_calls == 0:
                continue
            # Calls existed but NONE resolved (all uninferrable/ambiguous) — keep
            # the legacy first-wins edge so we never lose a dependency, and record
            # it for the report. A divergent case where at least one call resolved
            # is already covered by the loop above (edges emitted for those keys).
            if not resolved_keys:
                fallback_key = overloads[0][0]
                edge = Edge(
                    source_object_key=self_vertex_key,
                    destination_object_key=fallback_key,
                    relation=Relation.DEPENDS_ON,
                    action="call",
                )
                if edge.dedup_key() not in seen:
                    seen.add(edge.dedup_key())
                    edges.append(edge)
                self._record_unresolved(
                    bare_name, call_schema, total_calls, unresolved_count, fallback_key
                )

        return edges

    def _resolve_overloaded_calls(
        self,
        raw: str,
        call_schema: str | None,
        bare_name: str,
        overloads: list[tuple[str, tuple[str, ...]]],
    ) -> tuple[list[str], int, int]:
        """Resolve every call to an overloaded routine in the file's raw SQL.

        Returns ``(resolved_keys, unresolved_count, total_calls)``:
          * ``resolved_keys`` — distinct object_keys of overloads that at least
            one call resolved to (deduplicated, order of first resolution).
          * ``unresolved_count`` — calls that could not be resolved.
          * ``total_calls`` — total call sites found.

        Each call is inferred independently; a single file may legitimately
        resolve to several overloads (e.g. sp_x(1) -> int4, sp_x('a') -> text).
        Ambiguous/uninferrable calls are counted but never guessed.

        Args:
            raw: The file's raw SQL.
            call_schema: The routine's schema (for qualified call discovery),
                or ``None`` to match the bare name only.
            bare_name: The routine's bare name (no schema) — what find_calls
                looks for inside the parens.
            overloads: ``[(object_key, canonical_types)]`` from the overload index.
        """
        call_bodies = find_calls(raw, call_schema, bare_name)
        total_calls = len(call_bodies)
        if not call_bodies:
            return [], 0, 0

        resolved_keys: list[str] = []
        seen_keys: set[str] = set()
        unresolved = 0
        for body in call_bodies:
            args = split_call_args(body)
            call_sig = infer_call_signature(args)
            key = resolve_overload(call_sig, overloads)
            if key is not None and key not in seen_keys:
                seen_keys.add(key)
                resolved_keys.append(key)
            elif key is None:
                unresolved += 1
        return resolved_keys, unresolved, total_calls

    def _record_unresolved(
        self,
        bare_name: str,
        schema: str | None,
        total_calls: int,
        unresolved: int,
        routed_key: str,
    ) -> None:
        """Stash an unresolved-overload note for the resolution report (P8.S7).

        Called when NO call in the file resolved to any overload (all arguments
        uninferrable/ambiguous, or the call signature matched zero overloads).
        Divergent cases — where some calls resolve and some do not — are handled
        inline (edges emitted for the resolved overloads) and not recorded here.

        Kept intentionally light in P8.S6: we only collect enough to warn. The
        markdown report is assembled and written by BuildGraphService in P8.S7,
        so the parser stays free of file I/O (LESSONS §18: keep the contract).
        """
        self._resolution_notes.append(
            {
                "name": bare_name,
                "schema": schema,
                "total_calls": total_calls,
                "unresolved": unresolved,
                "routed_key": routed_key,
            }
        )
        logger.warning(
            f"Overload resolution: '{bare_name}' in schema '{schema}' unresolved "
            f"(no call resolved to a single overload); routed edge to first overload. "
            "See _overload_resolution_report.md"
        )

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
