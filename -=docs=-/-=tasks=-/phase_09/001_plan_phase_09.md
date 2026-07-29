# План Phase 9 — Сравнение состояния БД с файловой системой

> Дата: 2026-07-29
>
> Контекст:
> - `-=docs=-/-=tasks=-/2026-07-28/20260728_001_compare_db_vs_fs_draft.md` — нормативный дизайн (источник решений, §1-9)
> - `-=docs=-/-=tasks=-/TASK_CONVENTIONS.md` — цикл plan → result, правила коммитов
> - `LESSONS_LEARNED.md` §3 (статпроверка SQL), §9 (typer-подгруппы), §11 (try/finally cleanup), §18 (ABC + FakeAdapter), §27 (нормализация), §30 (pydantic None), §35 (fully-qualified DDL), §36 (regex-vs-AST), §42 (PySide6 GC — не Phase 9, но в доработках), §10 (wheel-проверка)
> - `src/db_project_manager/application/reverse_engineer.py` — `ReverseEngineerService.run` (точка вставки манифеста)
> - `src/db_project_manager/application/graph_service.py` — `BuildGraphService.build`
> - `src/db_project_manager/presentation/cli/main.py` — образец подгруппы `deploy_app`
> - `src/db_project_manager/infrastructure/database/base.py` — `DatabaseAdapter` ABC
> - `src/db_project_manager/infrastructure/database/postgres/adapter.py` — `_exec`, читающие методы
> - `src/db_project_manager/infrastructure/database/postgres/queries.py` — константы SQL

Шаги `P9.S01…P9.S09`. Каждый шаг — отдельный коммит (`feat(...)`/`test(...)`/`refactor(...)`).
Документы и код не смешивать в одном коммите (TASK_CONVENTIONS §6).
`git add -- "-=docs=-/..."` для путей с `-` (LESSONS §12).

После каждого шага зелёные (LESSONS §1):
```bash
unset SSL_CERT_FILE REQUESTS_CA_BUNDLE CURL_CA_BUNDLE && uv run pytest tests/unit/ -q
uv run ruff check src/ tests/
```
Baseline на старте: **307 unit-тестов** (checkpoint 20260725_002).

---

## Зависимости шагов

```
S01 (модели + sqlglot dep) ─┬→ S02 (манифест RE) ──┐
                            ├→ S03 (normalize_sql)  ┤
                            └→ S04 (row counts) ────┤
                                                   ↓
                              S05 (snapshot) → S06 (comparator) → S07 (service) → S08 (CLI) → S09 (docs)
```
S02/S03/S04 — независимы между собой, делаются после S01 в любом порядке.
**Обязательный порядок между S04 и всеми, кто трогает `DatabaseAdapter` ABC:**
S04 меняет ABC (8-й abstractmethod) → ломает **два** test-fake'а (`FakeAdapter` в
`tests/unit/test_reverse_engineer.py:26`, `DeployFakeAdapter` в
`tests/unit/test_deploy_service.py:26`). Оба правятся **в том же коммите** S04, иначе
collection-time ошибка «Can't instantiate abstract class» (LESSONS §18).

---

## P9.S01. Зависимость sqlglot + доменные модели diff

**Файлы:**
- `pyproject.toml` (edit) — добавить `sqlglot` в `dependencies`. Проверить wheel для
  win32 **до** коммита (LESSONS §10):
  ```bash
  unset SSL_CERT_FILE REQUESTS_CA_BUNDLE CURL_CA_BUNDLE && uv sync
  uv run python -c "import sqlglot; print(sqlglot.__version__)"
  ```
- `src/db_project_manager/domain/diff.py` (new) — pydantic v2-модели:

```python
class SnapshotSourceKind(str, Enum):
    DB = "db"      # подключение к базе
    DIR = "dir"    # каталог reverse-engineer в ФС

class DiffStatus(str, Enum):
    ADDED = "added"
    REMOVED = "removed"
    CHANGED = "changed"
    UNCHANGED = "unchanged"

class CodebaseManifest(BaseModel):
    db_type: str              # postgres | greenplum
    database: str
    generated_at: str         # UTC iso
    tool_version: str = ""
    format_version: int = 1

class ObjectSnapshot(BaseModel):
    model_config = ConfigDict(extra="ignore")
    object_key: str
    object_schema: str | None = None
    object_name: str = ""
    object_type: str
    object_signature: str = ""
    sql_normalized: str
    sql_hash: str             # 8 hex SHA-256
    estimated_rows: int | None = None

class StateSnapshot(BaseModel):
    model_config = ConfigDict(extra="ignore")
    source_kind: SnapshotSourceKind
    source_ref: str
    db_type: str
    generated_at: str         # UTC iso
    objects: dict[str, ObjectSnapshot]

class DiffEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")
    object_key: str
    status: DiffStatus
    source_snapshot: ObjectSnapshot | None = None
    target_snapshot: ObjectSnapshot | None = None

class DiffReport(BaseModel):
    model_config = ConfigDict(extra="ignore")
    source: StateSnapshot
    target: StateSnapshot
    generated_at: str         # UTC iso
    summary: dict[str, int]
    entries: list[DiffEntry]
```

**Урок §30:** dict-поля (`extra`) не принимают `None` в pydantic v2. Здесь `objects` —
обязательное поле без дефолта, так что коллизии нет; но при создании
`StateSnapshot` нельзя передавать `objects=None` — только пустой `{}` или заполненный.

**Тесты:** `tests/unit/test_diff_models.py` (new):
- roundtrip `model_dump_json` → `model_validate_json` для всех моделей;
- `DiffStatus`/`SnapshotSourceKind` сериализуются как строки (`"added"`, `"db"`);
- `ObjectSnapshot` без `estimated_rows` валиден (`None`);
- `CodebaseManifest` дефолты (`tool_version=""`, `format_version=1`).

**Коммит:** `feat(diff): add sqlglot dependency and diff domain models (P9.S01)`

---

## P9.S02. Манифест reverse-engineer

**Файлы:**
- `src/db_project_manager/infrastructure/config/codebase_manifest.py` (new)
- `src/db_project_manager/application/reverse_engineer.py` (edit)
- `tests/unit/test_reverse_engineer.py` (edit) — добавление проверки
- `tests/unit/test_codebase_manifest.py` (new)

### codebase_manifest.py

```python
MANIFEST_FILENAME = "dbpm.manifest.json"
MANIFEST_FORMAT_VERSION = 1

class ManifestError(Exception): ...

def write_manifest(manifest: CodebaseManifest, codebase_root: Path) -> Path:
    """Pretty JSON (indent=2, ensure_ascii=False). Atomic: tmp + os.replace."""
    ...

def read_manifest(codebase_root: Path) -> CodebaseManifest:
    """Read dbpm.manifest.json. ManifestError on missing/corrupt/unparseable."""
    ...
```

Реализация идёт по образцу `GuiSettingsStore` (атомарная запись через temp-file +
`os.replace`); на чтении — `ManifestError` с понятным сообщением для CLI. `db_type`
валидируется против `SUPPORTED_DB_TYPES` (`domain/connection.py`) при чтении —
неизвестный тип → `ManifestError`.

### ReverseEngineerService.run — точка вставки

В `run` (между концом qualify-refs блока, строка 88, и `self._emit(progress, "Готово", ...)`):

```python
from db_project_manager.infrastructure.config.codebase_manifest import write_manifest
from db_project_manager.domain.diff import CodebaseManifest

manifest = CodebaseManifest(
    db_type=conn_cfg.type,
    database=conn_cfg.database,
    generated_at=datetime.now(timezone.utc).isoformat(),
    tool_version=_tool_version(),  # из metadata пакета
)
write_manifest(manifest, result)
```

`result` (возврат `generate_scripts`) — это `Path` к корню сгенерированного дерева
(`<output>/<database>/`), уже `mkdir`'нутый. Манифест пишется в `result / dbpm.manifest.json`.

**Манифест НЕ шаг прогресса:** запись — одна атомарная операция, без долгого ожидания.
Не трогать арифметику `total` (строка 69) и связанные assert'ы в тестах.

### Тесты

`tests/unit/test_codebase_manifest.py` (new):
- `write_manifest` → `read_manifest` roundtrip;
- отсутствующий файл → `ManifestError`;
- повреждённый JSON → `ManifestError`;
- неизвестный `db_type` → `ManifestError`;
- атомарность: tmp-файл не остаётся после записи.

`tests/unit/test_reverse_engineer.py` (edit) — в `test_service_runs_full_flow`
добавить:
```python
from db_project_manager.infrastructure.config.codebase_manifest import read_manifest
m = read_manifest(out)
assert m.db_type == "postgres"
assert m.database == "mydb"
```

**Коммит:** `feat(reverse-engineer): write dbpm.manifest.json with db_type (P9.S02)`

---

## P9.S03. Нормализация SQL (sqlglot + regex-fallback)

**Файлы:**
- `src/db_project_manager/infrastructure/diff/__init__.py` (new, пустой)
- `src/db_project_manager/infrastructure/diff/normalize_sql.py` (new)
- `tests/unit/test_normalize_sql.py` (new)

### normalize_sql.py

```python
def normalize_sql(sql: str, *, dialect: str = "postgres") -> str:
    """Normalize a DDL/DML body via sqlglot AST.

    On parse error: regex-fallback (collapse whitespace, strip comments,
    lower-case keywords) + logger.warning. Never raises.
    """

def sql_hash(sql: str) -> str:
    """8 hex SHA-256 of the normalized SQL."""
```

sqlglot-нормализация:
```python
import sqlglot
from sqlglot.errors import ParseError

try:
    tree = sqlglot.parse_one(sql, read=dialect)
    return tree.normalize().sql(dialect=dialect, comments=False)
except ParseError as e:
    logger.warning(f"sqlglot не смог разобрать SQL, regex-fallback: {e}")
    return _regex_normalize(sql)
```

`_regex_normalize`: убрать `/* ... */` и `-- ...` комментарии, схлопнуть пробелы,
`lower()` ключевых слов (урок §27 — глобальные преобразования ДО токенизации; sqlglot
делает это сам, regex-fallback повторяет ту же идею).

### Тесты

`tests/unit/test_normalize_sql.py`:
- `CREATE TABLE t (a INT, b TEXT)` ≡ `create table T (A int, B text)` (порядок колонок
  сохраняется — это AST `CREATE`, не set; но регистр/форматирование игнорируются);
- `numeric(10,2)` остаётся `numeric(10, 2)` целым, не разбивается (урок §27);
- синтаксически сломанный SQL (`CREATE TABLE ((((`) → regex-fallback без исключения,
  warning в лог (caplog);
- `sql_hash` одинаковый для эквивалентных по AST SQL; разный для разных;
- `sql_hash` возвращает ровно 8 hex символов.

**Коммит:** `feat(diff): sqlglot-based SQL normalization with regex fallback (P9.S03)`

---

## P9.S04. Row counts (запрос + метод адаптера)

**Файлы:**
- `src/db_project_manager/infrastructure/database/postgres/queries.py` (edit)
- `src/db_project_manager/infrastructure/database/base.py` (edit)
- `src/db_project_manager/infrastructure/database/postgres/adapter.py` (edit)
- `tests/unit/test_reverse_engineer.py` (edit) — `FakeAdapter`
- `tests/unit/test_deploy_service.py` (edit) — `DeployFakeAdapter`
- `tests/unit/test_queries.py` (edit) — проверка столбцов

### queries.py — GET_TABLE_ROW_COUNTS

```python
GET_TABLE_ROW_COUNTS = """
    SELECT n.nspname AS schema_name,
           c.relname AS table_name,
           c.reltuples AS estimated_rows
    FROM pg_catalog.pg_class c
    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind = 'r'
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
    ORDER BY c.reltuples DESC NULLS LAST
"""
```

### base.py — 8-й abstractmethod

```python
@abstractmethod
def get_table_row_counts(self) -> list[dict[str, Any]]:
    """Return estimated row counts for user tables.

    Each dict: {schema_name, table_name, estimated_rows (float|None)}.
    """
```

### adapter.py — реализация

```python
def get_table_row_counts(self) -> list[dict[str, Any]]:
    rows = self._exec(q.GET_TABLE_ROW_COUNTS)
    return [
        {"schema_name": schema, "table_name": name, "estimated_rows": est}
        for schema, name, est in rows
    ]
```

### FakeAdapter / DeployFakeAdapter — заглушки

В обоих (LESSONS §18 — ABC расширяется → сначала прогнать существующие тесты):
```python
def get_table_row_counts(self) -> list[dict[str, Any]]:
    return []
```

### test_queries.py — статпроверка столбцов (урок §3)

Добавить множества валидных атрибутов (для алиасов `c` = `pg_class`, `n` = `pg_namespace`):
```python
_VALID_PG_CLASS_ATTRS = {"nspname", "relname", "reltuples", "relkind", "oid", "relnamespace"}
_VALID_PG_NAMESPACE_ATTRS = {"nspname", "oid"}

def test_table_row_counts_query_uses_valid_catalog_columns() -> None:
    _assert_alias_refs(
        queries.GET_TABLE_ROW_COUNTS,
        {"c": _VALID_PG_CLASS_ATTRS, "n": _VALID_PG_NAMESPACE_ATTRS},
    )
    assert "relkind = 'r'" in queries.GET_TABLE_ROW_COUNTS
    assert "pg_catalog" in queries.GET_TABLE_ROW_COUNTS  # NOT IN фильтр
```

**Коммит:** `feat(diff): GET_TABLE_ROW_COUNTS query and adapter method (P9.S04)`

---

## P9.S05. Снимок состояния (snapshot)

**Файлы:**
- `src/db_project_manager/infrastructure/diff/snapshot.py` (new)
- `tests/unit/test_snapshot.py` (new)

### snapshot.py

```python
#: Типы объектов, попадающих в структурный diff. Extensions/settings исключены
#: (шумят между средами) — draft §2, решение 2.
DIFFED_TYPES = frozenset({
    "table", "view", "materialized_view",
    "function", "procedure", "sequence",
})

def build_snapshot_from_dir(
    codebase_dir: str | Path,
    *,
    source_kind: SnapshotSourceKind,
    source_ref: str,
    db_type: str,
    row_counts: dict[tuple[str, str], int | None] | None = None,
    graph_service: BuildGraphService | None = None,
) -> StateSnapshot:
    """Build a StateSnapshot from a reverse-engineer codebase dir.

    Reads the dependency graph (BuildGraphService.build), reads each vertex's
    SQL from object_source_file, strips the autodoc block, normalizes, hashes.
    Filters vertices by DIFFED_TYPES. Attaches estimated_rows for tables from
    row_counts when provided.
    """
```

Этапы:
1. `graph = (graph_service or BuildGraphService()).build(codebase_dir)` — существующий
   парсер читает autodoc → `Vertex` с `object_source_file`.
2. Для каждой вершины с `object_type in DIFFED_TYPES`:
   - прочитать SQL из `Path(codebase_dir) / vertex.object_source_file`;
   - стрип autodoc-блок (найти `MARKER_CLOSE` + `*/`, взять подстроку после);
   - `normalized = normalize_sql(body)`, `sql_hash = sql_hash(normalized)`;
   - `estimated_rows = row_counts.get((schema, name))` для tables, иначе `None`.
3. Собрать `StateSnapshot(generated_at=UTC iso, ...)`.

`row_counts` — это dict, который `CompareService` (S07) соберёт из
`adapter.get_table_row_counts()` (S04): `{(schema, table): estimated_rows}`.

### Тесты

`tests/unit/test_snapshot.py` — на фикстуре `tests/fixtures/codebase_sample/`:
- снимок содержит объекты только из `DIFFED_TYPES` (нет extensions/settings);
- для каждого объекта есть `sql_hash` (8 hex);
- таблицы получают `estimated_rows` из `row_counts`; view/function — `None`;
- `source_kind`/`source_ref`/`db_type` попадают в снимок как переданы;
- пустой каталог (нет `.sql`) → пустой снимок без ошибки.

**Коммит:** `feat(diff): build StateSnapshot from codebase dir (P9.S05)`

---

## P9.S06. Компаратор

**Файлы:**
- `src/db_project_manager/infrastructure/diff/comparator.py` (new)
- `tests/unit/test_comparator.py` (new)

### comparator.py

```python
def compare(source: StateSnapshot, target: StateSnapshot) -> DiffReport:
    """Compare two snapshots by object_key set + sql_hash.

    added   = in source, not in target
    removed = in target, not in source
    changed = in both, sql_hash differs
    unchanged = in both, sql_hash equal
    """
```

Set-операции по ключам `source.objects` / `target.objects`; для общих — сравнение
`sql_hash`. `DiffEntry` несёт `source_snapshot`/`target_snapshot` для changed/unchanged;
added → только source, removed → только target. `summary` считает по `DiffStatus`.

**Проверка db_type НЕ здесь** — это ответственность `CompareService` (S07), который
бросает `CompareError` до вызова `compare`. Компаратор получает уже валидную пару
совместимых снимков.

### Тесты

`tests/unit/test_comparator.py` — синтетические пары:
- только added / только removed / только changed / только unchanged;
- смешанный сценарий (по одному каждого);
- пустые source/target;
- перегрузки: одинаковые `object_name`, разные `object_signature` → разные `object_key`
  → корректный added/removed (по ключу, а не по имени);
- `summary` считает правильно.

**Коммит:** `feat(diff): compare two StateSnapshots into DiffReport (P9.S06)`

---

## P9.S07. CompareService — оркестрация

**Файлы:**
- `src/db_project_manager/application/compare_service.py` (new)
- `tests/unit/test_compare_service.py` (new)

### compare_service.py

```python
class CompareError(Exception):
    """Incompatible db_type, missing manifest, bad spec, etc."""

@dataclass(frozen=True)
class SideSpec:
    kind: SnapshotSourceKind   # DB | DIR
    ref: str                   # путь к каталогу или путь к connection-file
    conn_cfg: ConnectionConfig | None = None  # только для kind=DB

class CompareService:
    def __init__(
        self,
        reverse_engineer: ReverseEngineerService | None = None,
        adapter_factory: Callable[[ConnectionConfig], DatabaseAdapter] | None = None,
        graph_service: BuildGraphService | None = None,
    ) -> None: ...

    def run(
        self,
        source: SideSpec,
        target: SideSpec,
        output_dir: str | Path,
        *,
        keep_model_dir: bool = False,
        progress: ProgressCallback | None = None,
    ) -> Path:
        """Run comparison; write source.json/target.json/diff_report.json to output_dir."""
```

Поток (draft §3.6):
1. `src_snap = self._build_side(source, progress, ...)` — для DIR: `read_manifest` +
   `build_snapshot_from_dir`; для DB: `tempfile.mkdtemp` → `ReverseEngineerService.run`
   → `adapter.get_table_row_counts` → `read_manifest` (проверка записи) →
   `build_snapshot_from_dir`.
2. Аналогично `tgt_snap`.
3. **Проверка совместимости:** `if src_snap.db_type != tgt_snap.db_type: raise CompareError(...)`.
4. `report = compare(src_snap, tgt_snap)`.
5. Запись `source.json`/`target.json`/`diff_report.json` в `output_dir` (pretty JSON).
6. В `finally`: если создавался temp-каталог и не `keep_model_dir` → `shutil.rmtree`.

Каждая сторона DB создаёт **свой** temp-каталог; cleanup в `finally` внешнего `run`
(не вложенный — оба temp-каталога чистятся). Сетевой адаптер — `try/finally disconnect`
внутри `_build_side` (урок §11).

`SideSpec` парсится в CLI (S08) — там `--source-dir`/`--source-connection-file`
взаимоисключающи и становятся `(kind=DIR, ref=path)` или `(kind=DB, ref=..., conn_cfg=...)`.

### Тесты

`tests/unit/test_compare_service.py`:
- `source=DIR, target=DIR`, оба на фикстуре `codebase_sample` с манифестом → отчёт
  записан, `summary.unchanged == N` (идентичные каталоги);
- `keep_model_dir=True` → temp-каталог не удалён (проверить `Path.exists()`);
- `keep_model_dir=False` → temp-каталог удалён;
- **`db_type` mismatch (PG vs GP) → `CompareError`** до вызова `compare`;
- отсутствует манифест для DIR-стороны → `CompareError` с упоминанием пути;
- `_build_side(DB, ...)` с моком `ReverseEngineerService` (fake, не реальная БД) —
  создаёт фейковый temp-каталог с файлами + манифест, `FakeAdapter.get_table_row_counts`
  возвращает `[]`.

**Коммит:** `feat(diff): CompareService orchestration with db_type check (P9.S07)`

---

## P9.S08. CLI — подгруппа `db-pm compare run`

**Файлы:**
- `src/db_project_manager/presentation/cli/main.py` (edit)
- `tests/unit/test_compare_cli.py` (new)

### main.py

По образцу `deploy_app` (урок §9 — мультикомандная подгруппа):
```python
compare_app = typer.Typer(no_args_is_help=True, help="Сравнение состояния БД и кодовой базы.")
app.add_typer(compare_app, name="compare")

@compare_app.command("run")
def compare_run(
    source_dir: Annotated[Optional[Path], typer.Option("--source-dir")] = None,
    source_connection_file: Annotated[Optional[Path], typer.Option("--source-connection-file")] = None,
    target_dir: Annotated[Optional[Path], typer.Option("--target-dir")] = None,
    target_connection_file: Annotated[Optional[Path], typer.Option("--target-connection-file")] = None,
    output_dir: Annotated[Path, typer.Option("--output-dir")] = ...,
    keep_model_dir: Annotated[bool, typer.Option("--keep-model-dir")] = False,
    config: Annotated[Optional[Path], typer.Option("--config")] = None,
) -> None:
    src = _resolve_side("source", source_dir, source_connection_file)  # exit 2 при нарушении
    tgt = _resolve_side("target", target_dir, target_connection_file)
    ...
    try:
        result = service.run(src, tgt, output_dir, keep_model_dir=keep_model_dir, progress=progress)
    except CompareError as e:
        typer.secho(f"✗ {e}", fg=RED, err=True); raise typer.Exit(code=2)
    typer.secho(f"✓ Отчёт сравнения: {result}", fg=GREEN)
```

`_resolve_side(label, dir_opt, conn_opt) -> SideSpec`:
- ровно один из `dir_opt`/`conn_opt` задан, иначе `typer.Exit(code=2)` с понятным
  сообщением (`"Укажите ровно один из --{label}-dir / --{label}-connection-file"`);
- для `conn_opt` — `_load_connection(...)` (существующий helper, exit 2 при ошибке).
- для `dir_opt` — проверка `is_dir()`, иначе exit 2.

### Тесты

`tests/unit/test_compare_cli.py`:
- оба флага source заданы → exit code 2;
- ни один source-флаг не задан → exit code 2;
- `source-dir` + `target-dir` + `output-dir` на двух копиях `codebase_sample` →
  exit code 0, `diff_report.json` существует;
- `CompareError` (PG vs GP) → exit code 2, красное сообщение в stderr.

**Коммит:** `feat(cli): db-pm compare run command (P9.S08)`

---

## P9.S09. Документация и закрытие фазы

1. `-=docs=-/-=tasks=-/phase_09/002_result_phase_09.md` — что сделано, проверки,
   известные ограничения, ссылки на коммиты (TASK_CONVENTIONS §2.1).
2. `LESSONS_LEARNED.md` — уроки Phase 9 по факту (кандидаты: sqlglot AST-нормализация
   на PG-диалекте, атомарность манифеста, влияние расширения ABC на fakes).
3. `-=docs=-/-=CHECKPOINTS=-/<YYYYMMDD>_NNN_checkpoint.md` — «Phase 9 complete»:
   архитектурная карта (новый пакет `infrastructure/diff/`, `application/compare_service.py`,
   подгруппа `compare` в CLI), baseline тестов, известные ограничения.
4. `-=docs=-/-=PHASES=-/Phase_09.md` — свод фазы (PHASES_CONVENTION).
5. `-=docs=-/-=tasks=-/BACKLOG.md` — добавить edge diff (P2, из draft §6).
6. Коммиты раздельно: `docs(tasks):`, `docs(lessons):`, `docs(checkpoint):`,
   `docs(phase_09):`, `docs(backlog):`.

---

## Критерий готовности фазы

- `uv run pytest tests/unit/ -q` зелёный (307 baseline + ~25-35 новых); `uv run ruff
  check src/ tests/` — чисто.
- `db-pm compare run --help` показывает подсказку; `db-pm compare run` с
  взаимоисключающими флагами source → понятная ошибка exit 2.
- Reverse-engineer реальной БД → в корне каталога есть `dbpm.manifest.json` с
  `db_type`.
- Сравнение двух идентичных каталогов → `unchanged == N`, `added/removed/changed == 0`.
- Сравнение PG-каталога vs GP-каталога (фейковые манифесты) → `CompareError`, exit 2.
- В `phase_09/` есть план (этот файл) и result; draft сохранён для истории
  (`-=tasks=-/2026-07-28/20260728_001_..._draft.md`).
