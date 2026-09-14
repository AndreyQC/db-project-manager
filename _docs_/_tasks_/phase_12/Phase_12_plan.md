# Phase 12: План реализации — ALTER + Delta (`deploy plan` / `deploy apply`)

> **Дата:** 2026-08-16
> **Ветка:** dev
> **Статус:** plan (нормативный документ для пошаговой реализации; на основе `_final`)
>
> Норматив-дизайн: `_tasks_/phase_12/Phase_12_vision_final.md` (все решения ALT-1..ALT-8).
> Предшественник (история обсуждения): `_tasks_/phase_12/Phase_12_vision_draft.md`.
> Контекст: чекпойнт 20260815_001; ROADMAP §2/§4 (CD-ALT-1..15)/§7/§8; Phase_09/10/11;
> LESSONS §3, §12, §19, §23, §26/§28, §31, §34/§35, §44, §45, §46, §49, Phase 11 §48.

---

## Принцип разбиения коммитов

Один логический шаг — один коммит (TASK_CONVENTIONS §6). **Код и документы не смешиваются.**
Порядок «снизу-вверх»: domain → SQL-экстракция → comparator → классификатор/рендер →
delta-сервис → apply-сервис → CLI → integration → регрессия/документы. Тесты — в том же
коммите, что и код.

Каждый шаг заканчивается прогоном:
```bash
unset SSL_CERT_FILE REQUESTS_CA_BUNDLE CURL_CA_BUNDLE && uv run pytest tests/unit/ -q
uv run ruff check src/ tests/
```

Зависимости между шагами:
```
S1 (domain/delta + diff-расширение) ─> S2 (columns.py + snapshot) ─> S3 (comparator diff)
S3 ─> S4 (alter_plan: classify + render) ─> S5 (delta_service + артефакты)
S5 ─> S6 (deploy_apply_service: пайплайн + репетиция) ─> S7 (CLI) ─> S8 (integration e2e)
                                                              └─> S9 (регрессия + docs)
```

**Критический инвариант фазы:** `sql_hash` существующих объектов НЕ меняется ни на одном
шаге (иначе вся кодовая база «изменится»). S2 не трогает `normalize_sql`; каждый шаг
включает регрессию на хэш-стабильность, где уместно.

---

## Шаг P12.S1 — Domain-модели (`domain/delta.py` + расширение `domain/diff.py`)

**Цель:** чистые типы/функции без I/O — основа для S3/S4/S5.

**Файлы:**
- Новый `src/db_project_manager/domain/delta.py` (final §4.2):
  - `ColumnSnapshot(BaseModel)`: `name: str`, `type: str` (каноническая строка),
    `nullable: bool`, `default: str | None = None`. (`comment` в v1 НЕ входит — см.
    «Известные ограничения» ниже.)
  - `ColumnChangeKind(str, Enum)`: `ADDED | DROPPED | TYPE_CHANGED | NULLABILITY_CHANGED |
    DEFAULT_CHANGED | COMMENT_CHANGED` (COMMENT_CHANGED объявлен, но в v1 не порождается).
  - `ColumnDiff(BaseModel)`: `column: str`, `kind: ColumnChangeKind`,
    `source_column: ColumnSnapshot | None`, `target_column: ColumnSnapshot | None`.
  - `OperationClass(str, Enum)`: `SAFE | NEEDS_PRE | BLOCKED`.
  - `PlannedOperation(BaseModel)`: `object_key`, `object_type`, `object_schema: str | None`,
    `object_name`, `action: Literal["create","alter","rerender","drop","skip"]`,
    `column_diffs: list[ColumnDiff] = []`, `classification: OperationClass`,
    `reason: str`, `script_file: str = ""`.
  - `DeltaPlan(BaseModel)`: `db_type: str`, `source_version: str | None`,
    `target_version: str | None`, `operations: list[PlannedOperation]`,
    `include_drops: bool = False`; properties `violations` (BLOCKED), `needs_pre_ops`,
    `safe_ops`.
  - `model_config = ConfigDict(extra="ignore")` везде (по образцу `domain/diff.py`).
- `src/db_project_manager/domain/diff.py` (additive, дефолты):
  - `ObjectSnapshot.columns: list[ColumnSnapshot] | None = None` — `None` = не извлечено
    (fail-safe маркер), `[]` = извлечено, колонок нет.
  - `DiffEntry.column_diffs: list[ColumnDiff] = []`, `DiffEntry.columns_unavailable: bool
    = False`.

**Тесты:** `tests/unit/test_delta_domain.py`
- roundtrip pydantic всех моделей (§28), enum → строки в JSON.
- Старый `source.json` (без `columns`) парсится: `ObjectSnapshot.model_validate_json` →
  `columns is None`; `DiffEntry` без новых полей → дефолты.
- `DeltaPlan.violations/needs_pre_ops/safe_ops` — фильтрация по classification.

**NOT done тут:** кто заполняет (S2/S3/S4).

---

## Шаг P12.S2 — Экстракция колонок (`infrastructure/diff/columns.py`) + интеграция в snapshot

**Цель:** `extract_columns` из SQL-тела; колонки в `ObjectSnapshot` обеих сторон (ALT-1b).

**Первое действие шага — smoke sqlglot (§44), до написания модуля:**
```bash
unset ... && uv run python -c "<проверка канонизации типов>"
```
Проверить пары: `int4`≡`integer`≡`int`, `int8`≡`bigint`, `int2`≡`smallint`, `bpchar(3)`≡`char(3)`,
`character varying(10)`≡`varchar(10)`, `timestamptz`≡`timestamp with time zone`, `numeric(10,2)`.
Вывод определяет форму `canonical_type`: если sqlglot сам даёт одинаковый рендер для
синонимов — достаточно `kind.sql(dialect).lower()`; если нет — поверх добавляется dict
`_TYPE_ALIASES` (ограниченный, покрытый тестами).

**Новый файл:** `src/db_project_manager/infrastructure/diff/columns.py`
- `def extract_columns(body: str, *, dialect: str = DEFAULT_DIALECT) -> list[ColumnSnapshot] | None`
  - `sqlglot.parse_one(body, read=dialect)`; не `Create` (или exceptions) → `None`.
  - `Create` без `expressions` (колонок) → `None` (напр. `PARTITION OF`-наследник) —
    fail-safe.
  - Для каждого `ColumnDef`: `name = col_def.name`; `type = canonical_type(col_def.kind)`;
    `nullable = not any(isinstance(c, exp.NotNullColumnConstraint) for c in col_def.constraints)`;
    `default = c.default.sql(dialect)` при `DefaultColumnConstraint` (иначе `None`).
- `normalize_sql` НЕ трогаем (хэш-стабильность); `extract_columns` делает свой `parse_one` —
  изоляция от регрессий важнее экономии одного парса.
- **Двойная симметрия:** одна функция для DIR (hand-written DDL) и DB (RE → канонический
  DDL) сторон.

**Интеграция:** `infrastructure/diff/snapshot.py::build_snapshot_from_dir` — для
`object_type == "table"`: `ObjectSnapshot(..., columns=extract_columns(body))`; остальным
типам `columns=None`.

**Тесты:** `tests/unit/test_extract_columns.py`
- Канонический RE-DDL (фикстура `table aircrafts.sql`): имена/типы/nullable вытащены верно.
- Hand-written: `integer`/`character varying`/inline `NOT NULL`/`DEFAULT 42`/`DEFAULT now()`
  (default — текст выражения).
- Синонимы: `integer` vs `int4` → одинаковый `type` (параметризованный).
- `PARTITION OF` / view-DDL / пустое тело / битый SQL → `None`.
- Тело с FK/PK/CHECK-constraints inline — констрейнты не ломают экстракцию, в колонки не
  попадают.
- `test_snapshot_has_columns`: `build_snapshot_from_dir` на фикстуре codebase_sample —
  у таблиц `columns` заполнен, у functions/views — `None`.
- `test_hash_unchanged`: `sql_hash` до/после интеграции — по фикстуре эталонные значения
  захардкожены в тесте (ловит случайную порчу normalize).

**Известное ограничение (в `Phase_12_result.md` и BACKLOG-кандидат):** `normalize_sql`
берёт `parse_one` = первый statement → `COMMENT ON`-строки в теле не участвуют в хэше;
comment-only правки дают UNCHANGED (поведение Phase 9 сохранено). Comment-level diff
требует multi-statement normalize — отдельная задача (менять хэш сейчас = весь мир
«изменился»).

**NOT done тут:** сравнение колонок (S3).

---

## Шаг P12.S3 — Comparator: column-diff для CHANGED-таблиц (CD-ALT-1)

**Цель:** `DiffEntry.column_diffs` для таблиц; чистая функция, без I/O.

**Файлы:**
- `src/db_project_manager/infrastructure/diff/columns.py`: `def diff_columns(source:
  list[ColumnSnapshot], target: list[ColumnSnapshot]) -> list[ColumnDiff]` —
  - имена: `set(source) ∪ set(target)`, сортировка по имени (детерминизм);
  - только source → ADDED; только target → DROPPED (rename НЕ детектируем — ALT-3);
  - общие: `type` различается → TYPE_CHANGED; `nullable` → NULLABILITY_CHANGED;
    `default` → DEFAULT_CHANGED (сравнение после collapse-whitespace); один столбец может
    дать несколько ColumnDiff.
- `src/db_project_manager/infrastructure/diff/comparator.py::compare`: для
  `status=CHANGED ∧ object_type=table`:
  - обе стороны `columns is not None` → `column_diffs=diff_columns(...)`;
  - хотя бы одна `None` → `columns_unavailable=True`, `column_diffs=[]`.

**Тесты:** `tests/unit/test_column_diff.py` + расширение `test_comparator.py`
- Параметризованные пары «было/стало»: add/drop/type/nullable/default/комбо на одной
  колонке (2 записи)/без изменений (пусто).
- DEFAULT-текст: `42` vs ` 42 ` → без diff (нормализация пробелов).
- Сторона без columns (None) → `columns_unavailable=True`, diffs пуст.
- Comparator: CHANGED таблица с колонками → заполнено; view/function → нет; UNCHANGED →
  нет (не считается).
- `columns_unavailable` roundtrip через `DiffReport.model_validate_json` (§28).

**NOT done тут:** классификация (S4).

---

## Шаг P12.S4 — Классификатор + ALTER-рендер (`infrastructure/deploy/alter_plan.py`)

**Цель:** матрица ALT-3 как чистые функции (CD-ALT-2..4).

**Новый файл:** `src/db_project_manager/infrastructure/deploy/alter_plan.py`

- `def _is_volatile_default(default: str | None) -> bool`: детерминированный
  консервативный разбор — literal (число/строка/NULL/true/false, допускается каст литерала)
  → False; всё прочее (`now()`, `current_timestamp`, `uuid_generate_v4()`, ссылки на
  sequence через `nextval` и т.п.) → True. Реализация через `sqlglot.parse_one(default)`:
  `exp.Literal` (или `Cast` от Literal) → False; иначе True; parse-fail → True (fail-safe).
- `def _classify_column_diffs(diffs: list[ColumnDiff], has_data: bool) -> tuple[OperationClass, str]`
  — ядро матрицы ALT-3 для HAS_DATA:
  - ADDED: nullable без DEFAULT или literal-DEFAULT → SAFE; volatile DEFAULT → NEEDS_PRE.
  - DROPPED → NEEDS_PRE.
  - TYPE_CHANGED → NEEDS_PRE (всегда, включая расширения).
  - NULLABILITY_CHANGED: NOT NULL→NULL (расширение) → SAFE; NULL→NOT NULL → NEEDS_PRE.
  - DEFAULT_CHANGED: добавление DEFAULT → SAFE; удаление → NEEDS_PRE; замена → NEEDS_PRE
    (консервативно, не различаем).
  - Итог по таблице: любой NEEDS_PRE → операция NEEDS_PRE; иначе SAFE. Пустые diffs при
    `columns` доступны → «unrepresented» → NEEDS_PRE (ALT-2).
- `def classify(entry: DiffEntry, presence: DataPresence, covered: bool, *,
  include_drops: bool) -> PlannedOperation` — статус-уровень:
  - ADDED → `create`, SAFE («новый объект», §7 п.3).
  - UNCHANGED → `skip`, SAFE.
  - REMOVED → `include_drops=False` → BLOCKED (reason: «нет в кодовой базе; авто-DROP
    запрещён»); `=True` → не-табличные → `drop` (DROP-скрипт), таблица EMPTY → `drop`,
    HAS_DATA/UNKNOWN → BLOCKED (даже с флагом — ALT-6).
  - CHANGED не-таблица → `rerender`, SAFE (§7 п.4).
  - CHANGED таблица: presence EMPTY → `rerender`, SAFE (пересоздание пустых, §7 п.2);
    HAS_DATA/UNKNOWN → `_classify_column_diffs`; NEEDS_PRE → при `covered=False`
    → BLOCKED (покрытия нет), `covered=True` → NEEDS_PRE; `columns_unavailable=True` →
    NEEDS_PRE (fail-safe).
- `def render_alter(op: PlannedOperation) -> str` — только SAFE-подмножество ALTER:
  - ADDED: `ALTER TABLE "s"."t" ADD COLUMN "c" <type>` (+ ` DEFAULT <literal>` при
    literal-default; nullable — без NOT NULL).
  - NULLABILITY widening: `ALTER COLUMN "c" DROP NOT NULL`.
  - DEFAULT добавление: `ALTER COLUMN "c" SET DEFAULT <expr>`.
  - Идентификаторы: только `_quote_identifier` whitelist `[A-Za-z_][A-Za-z0-9_]*` (§19);
    всё — fully-qualified `"schema"."table"` (§34/§35).
  - NEEDS_PRE/BLOCKED → пустая строка (не рендерим авто-DDL для опасного).

**Тесты:** `tests/unit/test_alter_plan.py`
- Матрица классификации — параметризованная: (kind × has_data × covered × include_drops)
  → класс + reason-подстрока; все строки матрицы final §3 покрыты.
- `rerender` для EMPTY-таблиц и не-табличных; `skip` для UNCHANGED.
- REMOVED: без флага → BLOCKED; с флагом не-таблица/EMPTY → drop; HAS_DATA+флаг → BLOCKED.
- `_is_volatile_default`: `42`, `'x'`, `NULL`, `true`, `42::int` → False; `now()`,
  `current_timestamp`, `uuid_generate_v4()`, `nextval('s.t_id_seq')`, битая строка → True.
- Рендер: `'"app"."orders"' in ddl` (контракт §35); имя с дефисом → ValueError whitelist'а
  (§19); NEEDS_PRE → пустой рендер.

**NOT done тут:** артефакты/порядок (S5).

---

## Шаг P12.S5 — Сборка дельты (`application/delta_service.py`) + артефакты

**Цель:** `DeltaPlan` + файлы-артефакты в порядке применения (CD-12/CD-13).

**Новый файл:** `src/db_project_manager/application/delta_service.py`
```python
class DeltaService:
    def __init__(self, graph_service: BuildGraphService | None = None): ...
    def build_plan(self, report: DiffReport, stats: dict[tuple[str|None, str], TablePresenceStats],
                   coverage: dict[tuple[str|None, str], list[str]], *, db_type: str,
                   source_version: str | None, target_version: str | None,
                   include_drops: bool = False) -> DeltaPlan
    def write_artifacts(self, plan: DeltaPlan, codebase_dir: Path, output_dir: Path) -> list[Path]
```
- `build_plan`: classify каждого entry (presence через `classify_presence` Phase 11;
  служебная схема исключается вызывающим); порядок операций:
  - позиция объекта в `graph_service.deploy_order(codebase_dir)` (топосорт; фильтр
    build=true — применяем только то, что деплоится);
  - REMOVED-объекты в графе кодовой базы отсутствуют → в конец плана, сортировка по
    object_key (детерминизм).
- `write_artifacts` (`output_dir/delta/`, `mkdir(parents=True, exist_ok=True)` — §31):
  - `NNN_<type>_<schema>_<name>.sql`, NNN = индекс в плане (стабильная нумерация);
  - `create`/`rerender` не-таблица — тело файла объекта после `strip_autodoc` (§23);
  - `rerender` таблица — `DROP TABLE IF EXISTS "s"."t";` + тело (пересоздание пустых);
  - `alter` — конкатенация `render_alter` утверждений;
  - `drop` — `DROP ...;`; BLOCKED-REMOVED — закомментированный `-- DROP ...` с reason;
  - `skip`/NEEDS_PRE/BLOCKED — файл не пишется (или пишется только для REMOVED);
    comment-only (`has_executable_sql` — §49) → пересclassify в `skip`.
  - `plan.json` = `DeltaPlan.model_dump_json(indent=2)`; `plan.md` по образцу
    `safety_report.py`: шапка (db_type, версии, include_drops), таблица операций
    (`NNN | object | action | class | reason | ~rows | covered_by`), счётчики по классам,
    секция «Требуют pre-скриптов» с рекомендацией.

**Тесты:** `tests/unit/test_delta_service.py` (фикстуры: синтетический DiffReport + мини-
codebase в `tmp_path` с manifest/autodoc; graph_service реальный на мини-codebase)
- Порядок операций = deploy_order мини-codebase; REMOVED — в конце, отсортированы.
- Артефакты: имена/содержимое по action; alter-DDL fully-qualified; rerender-таблица
  содержит `DROP TABLE IF EXISTS`; REMOVED без флага — закомментированный DROP.
- `plan.json` roundtrip (§28); `plan.md` содержит классы/reasons/счётчики.
- Comment-only create → skip; вложенные директории создаются (§31).
- `include_drops` влияет только на REMOVED-строки плана.

**NOT done тут:** выполнение (S6).

---

## Шаг P12.S6 — Apply-пайплайн + репетиция (`application/deploy_apply_service.py`)

**Цель:** полный пайплайн final §4.6 (CD-11/14/15, ALT-5/ALT-8).

**Рефактор (первым действием шага):** `DeployValidateService` — извлечь переиспользуемый
метод `deploy_into_new_db(conn_cfg, codebase_dir, db_name, *, progress) -> DeployResult`
(создание БД + подключение + накат вершин; без pre/post/version/drop — их делает validate
снаружи). `run()` переиспользует метод; поведение validate не меняется — существующие
тесты `test_deploy_service.py` остаются зелёными (это регрессия шага).

**Новый файл:** `src/db_project_manager/application/deploy_apply_service.py`
- `class DeployApplyError(Exception)` — hard errors (exit 2): manifest/версия/тип БД/репетиция.
- `class DeployApplyRejected(Exception)` — нарушения gate/CD-11 (exit 1).
- `class DeployApplyService`:
  ```python
  def __init__(self, adapter_factory=get_adapter, compare_service=None,
               gate_service: SafetyGateService | None = None,
               deploy_validate: DeployValidateService | None = None,
               reverse_engineer: ReverseEngineerService | None = None,
               graph_service=None, service_schema=DEFAULT_SERVICE_SCHEMA): ...
  def plan(self, codebase_dir, target_cfg, output_dir, *, include_drops=False, progress=None) -> DeltaPlan
  def apply(self, codebase_dir, target_cfg, output_dir, *, include_drops=False,
            rehearsal=True, keep_rehearsal_db=False, progress=None) -> ApplyResult
  ```
- `plan()`: manifest + db_type-check + version-check → gate (`gate_service.analyze`,
  violations → `DeployApplyRejected`) → delta (CompareService) → presence/coverage →
  `DeltaService.build_plan` + `write_artifacts`. Ничего не выполняется.
- `apply()`:
  - фаза A (репетиция, если `rehearsal`): temp-каталог → `reverse_engineer.run(target_cfg,
    temp_root)` → `db_name = sanitize_prefix("dbpm_rehearsal") + timestamp` →
    `deploy_validate.deploy_into_new_db(...)` → seed: `sorted((codebase_dir/"__migrations"/
    "seed").glob("*.sql"))` выполнить `execute_script` по очереди (stop-on-error; **только
    здесь**, никогда на таргете; без script_history — репетиционная БД одноразовая) →
    `_run_pipeline(rehearsal_cfg, codebase_dir, output_dir/"rehearsal")` → сбой →
    `DeployApplyError` (сообщение: имя rehearsal-БД оставить/удропать) → дроп в finally
    (кроме `keep_rehearsal_db`).
  - фаза B: `_run_pipeline(target_cfg, codebase_dir, output_dir)`.
- `_run_pipeline(conn_cfg, codebase_dir, output_dir)` (общее ядро):
  1. manifest + db_type + version-check (reuse `check_version_relation`; ERROR_NEWER →
     hard error);
  2. gate: `gate_service.analyze(...)` → violations → `DeployApplyRejected` (ДО pre);
  3. pre: `ScriptRunner.run_phase("pre", codebase_dir/"__migrations")` (existing контракт);
  4. CD-11: свежий `CompareService.run` + `DeltaService.build_plan`; NEEDS_PRE/BLOCKED
     операции на таблицах с данными → `DeployApplyRejected` (semantics: pre обязан довести
     покрытые таблицы до безопасного остатка);
  5. `write_artifacts` (репетиция — в `output_dir/rehearsal/`, таргет — в `output_dir/`;
     audit-trail обеих фаз сохраняется);
  6. применение: по операции — `adapter.execute_script(файл)`; stop-on-error →
     `DeployApplyError` (без continue-on-error — ALT-5); progress по каждой;
  7. post: `ScriptRunner.run_phase("post", ...)`; ошибка → пайплайн падает;
  8. `adapter.record_schema_version(service_schema, source_version, "apply")`.
- `ApplyResult` (dataclass): `planned/applied` счётчики, `rehearsal_db: str | None`,
  `applied_version`, пути артефактов.
- Восстановление документируется в docstring: повторный apply пересчитывает дельту —
  частично применённое исчезает; pre/post скипаются по script_history.

**Тесты:** `tests/unit/test_deploy_apply_service.py`
Фикстуры: `FakeApplyAdapter` (наследник DatabaseAdapter; in-memory: version-строка,
script_history dict, presence list; логирует порядок вызовов); `_StubCompare`
(последовательность DiffReport'ов — до/после pre); `_StubGate` (clean/violations);
мини-codebase в `tmp_path`.
- `test_plan_writes_artifacts_and_mutates_nothing`: plan() → файлы есть; ни один мутирующий
  метод адаптера не вызван.
- `test_apply_rehearsal_failure_leaves_target_untouched`: репетиционный adapter бросает на
  execute → таргет-фабрика ни разу не создала подключение/вызовов нет; rehearsal-БД дропнута.
- `test_apply_order_pre_delta_post_version`: успех → порядок вызовов
  (pre → execute* → post → record_schema_version("apply")).
- `test_gate_violation_stops_before_pre`: stub-gate violations → `DeployApplyRejected`,
  pre не выполнялся.
- `test_cd11_residual_unsafe_stops`: второй DiffReport содержит drop-column при данных →
  rejection ДО execute дельты.
- `test_error_midway_no_version`: execute падает на 2-й операции → `DeployApplyError`,
  record_schema_version НЕ вызван.
- `test_rerun_after_partial`: второй CompareService-ответ = остаточная дельта → повторный
  apply применяет только остаток; pre скипается (script_history success).
- `test_seed_only_in_rehearsal`: seed-скрипт выполнен против rehearsal-cfg и ни разу против
  target-cfg.
- `test_no_rehearsal_flag`: `rehearsal=False` → фаза A пропущена.
- `test_version_newer_hard_error`: → `DeployApplyError`.
- Рефактор-регрессия: полный `test_deploy_service.py` зелёный.

**NOT done тут:** CLI (S7).

---

## Шаг P12.S7 — CLI `deploy plan` + `deploy apply`

**Файл:** `src/db_project_manager/presentation/cli/main.py` (группа `deploy`; урок §46 —
обязательные опции первыми):
```python
@deploy_app.command("plan")
def deploy_plan(directory: Path, target_connection_file: Path, output_dir: Path,
                include_drops: bool = False): ...
@deploy_app.command("apply")
def deploy_apply(directory: Path, target_connection_file: Path, output_dir: Path,
                 include_drops: bool = False, no_rehearsal: bool = False,
                 keep_rehearsal_db: bool = False): ...
```
- `--dir`, `--target-connection-file`, `--output-dir`, `--include-drops`, `--no-rehearsal`,
  `--keep-rehearsal-db`; `--help` apply: «ИЗМЕНЯЕТ целевую БД (после репетиции на
  temp-аналоге)»; plan: «dry-run: артефакты без выполнения».
- Exit codes: 0 — ок; 1 — `DeployApplyRejected` (нарушения/needs-pre); 2 —
  `DeployApplyError`/connection-ошибки. Резюме: счётчики операций по классам + пути
  артефактов (+ имя rehearsal-БД для apply).

**Тесты:** `tests/unit/test_deploy_plan_apply_cli.py` (CliRunner, сервис monkeypatch на stub)
- Корректный вызов сервиса (аргументы проброшены, флаги отражены) для обеих команд.
- exit 0/1/2 сценарии; вывод содержит счётчики/пути.
- `--include-drops`/`--no-rehearsal`/`--keep-rehearsal-db` пробрасываются.

**NOT done тут:** GUI — BACKLOG P3 (ALT-7).

---

## Шаг P12.S8 — Integration e2e (`@pytest.mark.integration`, testcontainers)

**Новый файл:** `tests/integration/test_deploy_plan_apply_e2e.py`

Сетап (фикстура `pg_conn_cfg` — пер-тестовая БД, BACKLOG P1): схема `app`, таблицы
`orders` (~50 строк), `empty_t` (0 строк), функция; RE → codebase; bump
`source_version` (порядок — урок Phase 11 §48). Модификации по сценарию.

Сценарии (final §5):
1. `test_add_nullable_column_safe_applied`: + nullable column в DDL `app.orders` →
   `plan`: alter/SAFE; `apply` (с репетицией): колонка появилась в таргете, версия
   записана (`deploy_source="apply"`), артефакты в `output_dir` + `output_dir/rehearsal`.
2. `test_drop_column_with_data_needs_pre_blocked`: убрать колонку из `app.orders` →
   plan: NEEDS_PRE/BLOCKED; apply → exit-ошибка ДО pre (БД не изменилась).
3. `test_covers_pre_script_resolves`: то же + `__migrations/pre/...sql` с `covers` и
   ALTER внутри → apply проходит; CD-11: повторная дельта чиста.
4. `test_rehearsal_failure_target_untouched`: pre-скрипт с синтаксической ошибкой →
   apply падает на репетиции, структура таргета не изменилась (проверка колонок до/после).
5. `test_retry_after_midway_error_completes`: `--no-rehearsal` + операция, падающая один
   раз (напр. временно битый rerender) → стоп; исправление → повторный apply доводит
   (дельта короче, pre скипается).
6. `test_unextractable_columns_failsafe`: партиция `PARTITION OF` в codebase-стороне →
   columns=None → needs-pre при данных.
7. `test_type_synonyms_no_false_change`: hand-written `integer` против RE `int4` →
   CHANGED нет вообще (хэш) либо column-diff без TYPE_CHANGED.
8. `test_seed_runs_only_in_rehearsal`: `__migrations/seed/*.sql` с INSERT → репетиционная
   БД получила строки, таргет — нет (после успешного apply).

**Smoke CLI (ручная проверка):** против локальной dev-БД — plan → просмотр `plan.md` →
apply → повторный apply («нечего делать»).

---

## Шаг P12.S9 — Регрессия + документация + закрытие фазы

1. Полная регрессия: `uv run pytest tests/unit/ -q` (baseline 700 + новые Phase 12);
   `uv run ruff check src/ tests/`; `uv run pytest -m integration` (Docker).
2. README: `deploy plan`/`deploy apply` в раздел CLI (apply «изменяет БД», репетиция,
   exit codes); упоминание `__migrations/seed/`.
3. ROADMAP: Phase 12 → ✅ done (коммиты).
4. BACKLOG: новые пункты по итогам (кандидаты уже известны: multi-statement normalize +
   comment-diff; авто-seed P3; GUI P3 уже добавлен).
5. Документы фазы (отдельные коммиты): `Phase_12_result.md`, `_phases_/Phase_12.md`,
   чекпойнт дня.
6. LESSONS_LEARNED: вероятные кандидаты — sqlglot-канонизация типов; рефактор
   DeployValidateService без поведения-дрейфа; rehearsal-паттерн.

---

## Чеклист по урокам (самопроверка перед каждым коммитом)

- [ ] §3: presence через метаданные; никаких COUNT в новой логике.
- [ ] §12: `git add -- "_docs_/_tasks_/phase_12/..."`.
- [ ] §19: идентификаторы в render_alter/артефактах — whitelist + double-quote.
- [ ] §23: тела объектов в артефакты — после `strip_autodoc`.
- [ ] §26/§28: identity колонок по имени; JSON-тесты — roundtrip, не substring.
- [ ] §31: `mkdir(parents=True, exist_ok=True)` для `output_dir/delta`, `rehearsal/`.
- [ ] §34/§35: fully-qualified `"schema"."table"` в DDL; контракт-тест.
- [ ] §44: S2 начинается с smoke sqlglot до кода.
- [ ] §45: **ноль** новых abstract-методов адаптера; FakeApplyAdapter — наследник с
  full-контрактом; при любом расширении ABC — grep всех fakes, один коммит.
- [ ] §46: обязательные typer-опции первыми в сигнатурах.
- [ ] §49: comment-only артефакты — `has_executable_sql`.
- [ ] Phase 11 §48: integration-сценарии с version/RE-синхронизацией — явный порядок
  действий в тесте.
- [ ] **Hash-инвариант:** ни один шаг не меняет `normalize_sql`/`sql_hash`.
- [ ] TASK_CONVENTIONS §6: код и документы — разные коммиты.

---

## NOT done в Phase 12 (явно, для `Phase_12.md` и чекпойнта)

- AI-трек (CD-AI-1/2) — overlay после фазы.
- Структурный diff констрейнтов/индексов/partitioning — BACKLOG (ALT-2).
- Multi-statement normalize + comment-level diff — BACKLOG (новый кандидат из S2).
- Detect column rename — никогда в авто-классификации (ALT-3).
- Авто-генератор seed «одной записи» — BACKLOG P3 (ALT-8).
- GUI plan/apply + рендер плана — BACKLOG P3 (ALT-7).
- Post-deploy отчёты/история (CD-16..18) — Phase 13.
- Greenplum распределённые ALTER — при появлении кластера.

---

## Где читать дальше

- `_tasks_/phase_12/Phase_12_vision_final.md` — нормативный дизайн (источник решений).
- `_tasks_/phase_12/Phase_12_vision_draft.md` — история обсуждения (ALT-1..ALT-8).
- `_tasks_/phase_11/Phase_11_plan.md` — образец структуры и процесса.
- `_checkpoints_/20260815_001_checkpoint.md` — текущее состояние.
- `LESSONS_LEARNED.md` §3, §12, §19, §23, §26/§28, §31, §34/§35, §44, §45, §46, §49.
