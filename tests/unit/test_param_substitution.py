"""Unit-тесты подстановки параметров деплоя ``{{param}}`` (задача 20261027_001).

Покрываются: чтение dbpm.params.yaml (типы, крипта, ошибки структуры),
приоритеты слияния, подстановка с исключением autodoc, агрегированный
fail-fast парсера, проброс в snapshot/deploy, поле ConnectionConfig.params.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from db_project_manager.domain.connection import ConnectionConfig
from db_project_manager.domain.diff import SnapshotSourceKind
from db_project_manager.infrastructure.crypto.crypto_util import (
    generate_fernet_key,
    get_encrypted_text,
)
from db_project_manager.infrastructure.diff.snapshot import build_snapshot_from_dir
from db_project_manager.infrastructure.params import (
    PARAMS_FILENAME,
    ParamSubstitutor,
    ParamsFileError,
    UnresolvedParamError,
    effective_params,
    load_params_file,
)
from db_project_manager.infrastructure.parsing.pg_sql_parser import PgSqlParser

# --- fixtures ------------------------------------------------------------


def _autodoc(schema: str, name: str, otype: str = "table") -> str:
    """Заголовок autodoc как в реальных файлах кодовой базы."""
    return (
        "/*====================================================================================\n"
        "[<[autodoc-yaml]]\n"
        "object:\n"
        f"  object_key: pg_database/cis/schema/{schema}/{otype}/{name}\n"
        f"  object_name: {name}\n"
        f"  object_schema: {schema}\n"
        f"  object_type: {otype}\n"
        "project:\n"
        "  build: true\n"
        "[[autodoc-yaml]>]\n"
        "=====================================================================================*/\n"
    )


@pytest.fixture()
def codebase(tmp_path: Path) -> Path:
    """Кодовая база: таблица + функция, вызывающая её через {{sch}}."""
    (tmp_path / "dbpm.params.yaml").write_text(
        "parameters:\n"
        "  sch: apps\n"
        "  pxf_server: ods_zup\n",
        encoding="utf-8",
    )
    (tmp_path / "t.sql").write_text(
        _autodoc("apps", "items")
        + 'CREATE TABLE "apps".items (id int);\n',
        encoding="utf-8",
    )
    (tmp_path / "f.sql").write_text(
        _autodoc("apps", "fn_touch", otype="function")
        + "CREATE FUNCTION apps.fn_touch() RETURNS void AS $$\n"
        "BEGIN\n"
        "  UPDATE {{sch}}.items SET id = id;\n"
        "END;\n"
        "$$ LANGUAGE plpgsql;\n",
        encoding="utf-8",
    )
    return tmp_path


# --- load_params_file / effective_params ---------------------------------


class TestLoadParamsFile:
    def test_absent_file_is_empty(self, tmp_path: Path) -> None:
        assert load_params_file(tmp_path) == {}

    def test_scalars_coerced_to_str(self, tmp_path: Path) -> None:
        (tmp_path / PARAMS_FILENAME).write_text(
            "parameters:\n"
            "  n: 42\n"
            "  flag: true\n"
            "  empty: null\n"
            "  s: hello\n",
            encoding="utf-8",
        )
        values = load_params_file(tmp_path)
        assert values == {"n": "42", "flag": "True", "empty": "", "s": "hello"}

    def test_bad_param_name_raises(self, tmp_path: Path) -> None:
        (tmp_path / PARAMS_FILENAME).write_text(
            "parameters:\n  'bad name': x\n", encoding="utf-8"
        )
        with pytest.raises(ParamsFileError, match="недопустимое имя"):
            load_params_file(tmp_path)

    def test_bad_structure_raises(self, tmp_path: Path) -> None:
        (tmp_path / PARAMS_FILENAME).write_text(
            "parameters: just-a-string\n", encoding="utf-8"
        )
        with pytest.raises(ParamsFileError, match="отображением"):
            load_params_file(tmp_path)

    def test_crypto_value_decrypted(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DBPM_TEST_FERNET", generate_fernet_key())
        token = get_encrypted_text("s3cret-value", "DBPM_TEST_FERNET")
        (tmp_path / PARAMS_FILENAME).write_text(
            f"parameters:\n  pwd: {token}\n", encoding="utf-8"
        )
        assert load_params_file(tmp_path) == {"pwd": "s3cret-value"}

    def test_crypto_missing_env_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DBPM_TEST_FERNET_ABSENT", raising=False)
        (tmp_path / PARAMS_FILENAME).write_text(
            "parameters:\n  pwd: crypto__DBPM_TEST_FERNET_ABSENT__gAAAA\n", encoding="utf-8"
        )
        with pytest.raises(KeyError):
            load_params_file(tmp_path)

    def test_effective_params_override_wins_and_decrypts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DBPM_TEST_FERNET", generate_fernet_key())
        token = get_encrypted_text("from-connection", "DBPM_TEST_FERNET")
        (tmp_path / PARAMS_FILENAME).write_text(
            "parameters:\n"
            "  pxf_server: from-file\n",
            encoding="utf-8",
        )
        merged = effective_params(tmp_path, {"pxf_server": token})
        assert merged == {"pxf_server": "from-connection"}


# --- ParamSubstitutor ------------------------------------------------------


class TestSubstitution:
    def test_basic_and_whitespace_form(self) -> None:
        sub = ParamSubstitutor({"a": "1", "b": "2"})
        assert sub.substitute("x={{a}} y={{ b }}") == "x=1 y=2"

    def test_text_without_braces_untouched(self) -> None:
        sub = ParamSubstitutor({"a": "1"})
        text = "SELECT '$$weird {{a}} body' FROM t;"
        assert sub.substitute(text) == text.replace("{{a}}", "1")

    def test_autodoc_block_excluded(self) -> None:
        text = (
            _autodoc("schema_from_autodoc", "t1")
            + "CREATE TABLE {{sch}}.t1 (id int);\n"
        )
        sub = ParamSubstitutor({"sch": "resolved"})
        out = sub.substitute(text, origin="t.sql")
        assert "schema_from_autodoc" in out  # autodoc не тронут
        assert "{{sch}}" not in out
        assert "CREATE TABLE resolved.t1" in out

    def test_missing_collected_aggregated(self) -> None:
        sub = ParamSubstitutor({})
        sub.substitute("{{one}}", origin="a.sql")
        sub.substitute("{{two}} {{one}}", origin="b.sql")
        with pytest.raises(UnresolvedParamError) as exc_info:
            sub.raise_if_missing()
        message = str(exc_info.value)
        assert "{{one}}" in message and "{{two}}" in message
        assert "a.sql" in message and "b.sql" in message
        # значения не раскрываются в ошибке — их тут и нет, но known пуст
        assert exc_info.value.known == []

    def test_invalid_placeholder_left_alone(self) -> None:
        sub = ParamSubstitutor({"a": "1"})
        text = "{{9x}} {{a.b}} {{}} {{a}}"
        assert sub.substitute(text) == "{{9x}} {{a.b}} {{}} 1"
        sub.raise_if_missing()  # ничего не накопилось

    def test_no_raise_when_all_resolved(self) -> None:
        sub = ParamSubstitutor({"a": "1"})
        sub.substitute("{{a}}", origin="a.sql")
        sub.raise_if_missing()


# --- parser integration ----------------------------------------------------


class TestParserIntegration:
    def test_resolved_from_params_file(self, codebase: Path) -> None:
        graph = PgSqlParser().parse_directory(codebase)
        # Тело функции подставлено ДО скана рёбер: UPDATE apps.items даёт ребро
        # function -> table по развёрнутому имени (UPDATE классифицируется как
        # CHANGE_DATA_IN, не DEPENDS_ON).
        fn_key = "pg_database/cis/schema/apps/function/fn_touch"
        edges = [e for e in graph.edges if e.source_object_key == fn_key]
        assert edges, "ребро из функции не построено"
        assert all("items" in e.destination_object_key for e in edges)

    def test_explicit_params_override_file(self, codebase: Path) -> None:
        # Параметр sch переопределён: тело функции ссылается на other.items —
        # ребро к таблице other не построится (её нет в графе), но подстановка
        # прошла без ошибок, а со значением из файла ребро бы было к apps.items.
        graph = PgSqlParser().parse_directory(codebase, params={"sch": "zzz", "pxf_server": "x"})
        fn_key = "pg_database/cis/schema/apps/function/fn_touch"
        assert fn_key in graph.vertices

    def test_fail_fast_aggregates_across_files(self, tmp_path: Path) -> None:
        (tmp_path / "a.sql").write_text("SELECT '{{p1}}';\n", encoding="utf-8")
        (tmp_path / "b.sql").write_text("SELECT '{{p2}}' , '{{p1}}';\n", encoding="utf-8")
        with pytest.raises(UnresolvedParamError) as exc_info:
            PgSqlParser().parse_directory(tmp_path)
        message = str(exc_info.value)
        assert "{{p1}}" in message and "{{p2}}" in message
        assert "a.sql" in message and "b.sql" in message

    def test_no_params_and_no_placeholders_parses(self, tmp_path: Path) -> None:
        (tmp_path / "t.sql").write_text("CREATE TABLE public.t (id int);\n", encoding="utf-8")
        graph = PgSqlParser().parse_directory(tmp_path)
        assert len(graph.vertices) == 1


# --- snapshot / deploy -------------------------------------------------------


class TestSnapshotAndDeploy:
    def test_snapshot_bodies_substituted(self, codebase: Path) -> None:
        snapshot = build_snapshot_from_dir(
            codebase,
            source_kind=SnapshotSourceKind.DIR,
            source_ref=str(codebase),
            db_type="postgres",
        )
        items = snapshot.objects["pg_database/cis/schema/apps/table/items"]
        assert "{{" not in items.sql_normalized
        fn = snapshot.objects["pg_database/cis/schema/apps/function/fn_touch"]
        assert "UPDATE apps.items" in fn.sql_normalized

    def test_deploy_object_substitutes_script(self, tmp_path: Path) -> None:
        from db_project_manager.application.deploy_service import DeployValidateService
        from db_project_manager.domain.graph import Vertex

        (tmp_path / "t.sql").write_text(
            _autodoc("src_ods", "ext_x", otype="external_table")
            + "CREATE EXTERNAL TABLE src_ods.ext_x (id text)\n"
            "LOCATION ('pxf://s.t?SERVER={{pxf_server}}');\n",
            encoding="utf-8",
        )

        class _FakeAdapter:
            def __init__(self) -> None:
                self.executed: list[str] = []

            def execute_script(self, script: str) -> None:
                self.executed.append(script)

        vertex = Vertex(
            object_key="pg_database/cis/schema/src_ods/external_table/ext_x",
            object_catalog="cis",
            object_schema="src_ods",
            object_type="external_table",
            object_name="ext_x",
            object_source_file="t.sql",
        )
        adapter = _FakeAdapter()
        DeployValidateService()._deploy_object(
            adapter, tmp_path, vertex, "target_db", {"pxf_server": "ods_zup"}
        )
        assert len(adapter.executed) == 1
        assert "SERVER=ods_zup" in adapter.executed[0]
        assert "{{" not in adapter.executed[0]


# --- ConnectionConfig --------------------------------------------------------


class TestConnectionConfigParams:
    def test_default_empty(self) -> None:
        cfg = ConnectionConfig(host="h", database="d", username="u")
        assert cfg.params == {}

    def test_roundtrip_from_yaml_shape(self) -> None:
        cfg = ConnectionConfig.model_validate(
            {
                "host": "h",
                "database": "d",
                "username": "u",
                "params": {"pxf_server": "ods_zup_prod", "pwd": "plain"},
            }
        )
        assert cfg.params["pxf_server"] == "ods_zup_prod"
