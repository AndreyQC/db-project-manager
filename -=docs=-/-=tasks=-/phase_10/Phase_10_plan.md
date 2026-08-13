# Phase 10: План реализации — CD Foundation

> **Дата:** 2026-08-13
> **Ветка:** dev
> **Статус:** plan (нормативный документ для пошаговой реализации; на основе `_final`)
>
> Норматив-дизайн: `-=tasks=-/phase_10/Phase_10_vision_final.md`.
> Предшественник (история обсуждения): `-=tasks=-/phase_10/Phase_10_vision_draft.md`.
> Контекст: чекпойнт 20260804_001; ROADMAP §2/§4/§7/§8; Phase_02.md; LESSONS §12, §18/§45,
> §19, §23, §28, §32, §34/§35.

---

## Принцип разбиения коммитов

Один логический шаг — один коммит (TASK_CONVENTIONS §6). **Код и документы не смешиваются.**
Шаги идут «снизу-вверх» по dependency-цепочке: сначала чистые helpers/domain (нет
зависимостей от adapter/PG), затем расширение контракта (adapter + fakes в одном коммите),
затем механика (canonical/templates/RE-seed/runner), затем интеграция в deploy flow, и
наконец фикстуры + финальные регрессионные тесты. Тесты пишутся в том же коммите, что и код.

Каждый шаг заканчивается прогоном:
```bash
unset SSL_CERT_FILE REQUESTS_CA_BUNDLE CURL_CA_BUNDLE && uv run pytest tests/unit/ -q
uv run ruff check src/ tests/
```

Зависимости между шагами:
```
S1 (domain) ─┬─> S2 (manifest)
             ├─> S3 (Vertex.immutable + autodoc)
             └─> S4 (adapter + fakes) ─┬─> S5 (canonical DDL + templates)
                                       ├─> S6 (RE seed/sync)
                                       └─> S7 (pre/post runner) ─> S8 (deploy integration)
S9 (fixture) ──────────────────────────────────────────────────> S10 (регрессия + integration + smoke)
```

---

## Шаг P10.S1 — Domain-модели и helpers (`domain/deploy.py`)

**Цель:** чистые, изолированно тестируемые доменные типы и функции — без зависимости от
adapter/PG. Основа для всех последующих шагов.

**Новый файл:** `src/db_project_manager/domain/deploy.py`

Содержание:
- `CALVER_RE = r"^\d{4}\.(0[1-9]|1[0-2])\.(0[1-9]|[12]\d|3[01])\.\d{2}$"` — regex валидации
  `source_version` (CDF-9).
- `def validate_calver(value: str) -> None:` — поднимает `ValueError` с понятным сообщением,
  если не матчит.
- `def calver_seed(now: datetime | None = None) -> str:` — возвращает `YYYY.MM.DD.01` для
  текущего UTC-времени (seed при первом RE). Использует `datetime.now(timezone.utc)`.
- `class ScriptRecord(BaseModel):` — модель исполнения скрипта (CDF-11):
  ```python
  script_name: str
  script_type: Literal["pre", "post"]
  checksum: str           # SHA-256 hex, 64 символа
  success: bool
  error_message: str | None = None   # None при success=TRUE
  duration_ms: int
  executed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
  ```
  `model_config = ConfigDict(extra="ignore")` (по образцу `domain/diff.py:66`).
- `def canonical_normalize(text: str) -> str:` — нормализация для checksum (CDF-6):
  1. strip autodoc-блока через существующий `_strip_autodoc` (вынести из `deploy_service.py`
     в `infrastructure/sql/autodoc.py` или продублировать как `strip_autodoc` в `domain/` —
     **решение: вынести в `infrastructure/sql/autodoc.py:strip_autodoc`**, `deploy_service`
     импортирует; иначе циклическая зависимость).
  2. Нормализация: splitlines → strip trailing whitespace на каждой → отбросить пустые →
     join `\n`; `\r\n` → `\n`.
- `def script_checksum(text: str) -> str:` — `hashlib.sha256(canonical_normalize(text)
  .encode("utf-8")).hexdigest()`.

**Рефакторинг (часть шага):** вынести `_strip_autodoc` из `deploy_service.py:261-273` в
`infrastructure/sql/autodoc.py` как `strip_autodoc(text: str) -> str`. `deploy_service`
импортирует — поведение не меняется, regression-тесты `test_deploy_service.py` должны
остаться зелёными (только refactor, без новой логики).

**Импорты:** `hashlib`, `re`, `datetime`, `pydantic`, `infrastructure.sql.autodoc.strip_autodoc`.

**Тесты (этот же коммит):** `tests/unit/test_deploy_domain.py`
- `test_calver_valid` (параметризованный): `2026.08.11.01`, `2026.08.11.99`, `2026.12.31.07`
  → `validate_calver` не падает.
- `test_calver_invalid` (параметризованный): `2026.8.11.01`, `2026.13.01.01`,
  `2026.08.32.01`, `2026.08.11.1`, `2026-08-11-01`, `2026.08.11` → `ValueError`.
- `test_calver_comparison`: `assert "2026.08.11.01" < "2026.08.11.02" < "2026.08.12.01"`.
- `test_calver_seed`: mock now → `"2026.08.13.01"`.
- `test_script_record_roundtrip`: pydantic roundtrip, `error_message=None` при success.
- `test_canonical_normalize_strips_autodoc`: текст с autodoc-блоком → без него, идемпотентно.
- `test_canonical_normalize_whitespace`: trailing whitespace и `\r\n` → canonical.
- `test_script_checksum_deterministic`: один текст → тот же hash.
- `test_script_checksum_strips_autodoc`: тот же SQL с разным autodoc → тот же checksum.
- `test_script_checksum_sensitive`: реальное изменение SQL → другой hash.

**NOT done тут:** manifest-extension (S2), adapter-контракт (S4) — пока чистые типы.

---

## Шаг P10.S2 — Расширение `CodebaseManifest` (`source_version`)

**Цель:** manifest несёт calver-версию кодовой базы; старые manifest'ы валидируются с
понятной ошибкой.

**Файлы:**
- `src/db_project_manager/domain/diff.py` (`CodebaseManifest`, строки 48-60): добавить поле
  ```python
  source_version: str = ""   # calver YYYY.MM.DD.NN; empty → ManifestError при format_version=2
  ```
  Не `None` и не обязательное на уровне модели — валидация логическая в `read_manifest`
  (см. ниже), чтобы не ломать чтение старых manifest'ов для migration-целей.
- `src/db_project_manager/infrastructure/config/codebase_manifest.py`:
  - `MANIFEST_FORMAT_VERSION = 2` (было 1).
  - В `read_manifest`: после pydantic-валидации — если `format_version >= 2` и
    `source_version` пусто/невалидно → `ManifestError("...добавьте source_version...")`;
    `validate_calver(manifest.source_version)` — пробрасывает ValueError как ManifestError.
  - В `write_manifest`: payload получает `format_version=2`; если `source_version` пусто →
    `ManifestError` (нельзя записать невалидный manifest).

**Импорты:** `domain.deploy.validate_calver`.

**Тесты:** расширить `tests/unit/test_codebase_manifest.py` (если есть; иначе создать).
- `test_manifest_v2_with_source_version_roundtrip`: roundtrip pydantic + JSON, поле
  сохраняется.
- `test_read_manifest_v2_missing_source_version_raises`: `format_version=2` без
  `source_version` → `ManifestError`.
- `test_read_manifest_v2_invalid_calver_raises`: `"source_version": "2026.08.11"` →
  `ManifestError`.
- `test_read_manifest_v1_still_parses_for_migration`: `format_version=1` без
  `source_version` → парсится (но не используется deploy), чтобы RE мог его перезаписать.
- `test_write_manifest_requires_source_version`: пустое → `ManifestError`.

**Регрессия Phase 9 compare-тестов:** `CodebaseManifest` расширено additively — compare
читает `db_type`/`database`, новые поля не ломают (урок §18: contract additive).

**NOT done тут:** кто пишет `source_version` при RE — см. S6 (RE-seed).

---

## Шаг P10.S3 — `Vertex.immutable` + autodoc-поле

**Цель:** Phase 1 контракт расширен маркером «управляется db-pm»; парсер autodoc его читает.

**Файлы:**
- `src/db_project_manager/domain/graph.py` (`Vertex`, строки 38-66): добавить поле
  ```python
  immutable: bool = False
  ```
  рядом с `build: bool = True`. `model_config` не меняется (уже `extra="ignore"`).
- `src/db_project_manager/infrastructure/sql/autodoc.py` (около строк 16, 87): расширить
  `project`-секцию. Текущий формат: `{"object": obj, "project": {"build": True}}` → при
  `immutable=True`: `{"object": obj, "project": {"build": True, "immutable": True}}`.
  Реализация: `make_autodoc(obj, *, build=True, immutable=False)` — omit `immutable` при
  `False` (CDF-10).
- Парсер autodoc (`extract_header` или эквивалент) — читает `project.immutable`, по
  умолчанию `False`.

**Импорты:** без новых (внутренние).

**Тесты:** `tests/unit/test_autodoc.py` (расширить).
- `test_make_autodoc_omits_immutable_when_false`: dump не содержит `immutable`.
- `test_make_autodoc_includes_immutable_when_true`: dump содержит `immutable: true` в `project`.
- `test_extract_header_parses_immutable`: roundtrip — `make_autodoc(..., immutable=True)` →
  `extract_header(...)` → `obj["project"]["immutable"] is True`.
- `test_extract_header_defaults_immutable_false`: autodoc без поля → `False`.

**Регрессия graph-тестов:** `Vertex` получает additive поле с default — существующие тесты
не ломаются (урок §18). Если есть тесты, проверяющие точный `model_dump()` Vertex —
обновить assertions (проверить через `grep`).

**NOT done тут:** кто ставит `immutable=True` (RE в S6).

---

## Шаг P10.S4 — Расширение контракта `DatabaseAdapter` (+4 methods) + fakes

**Цель:** контракт adapter'а несёт 4 новых method для работы с `__deploy`. ВСЕ fakes
обновляются в одном коммите (урок §45).

**Файлы:**
- `src/db_project_manager/infrastructure/database/base.py`: добавить 4 abstract methods
  (рядом с существующими 9, по образцу `get_table_row_counts:86-87`):
  ```python
  @abstractmethod
  def get_schema_version(self, schema_name: str) -> str | None: ...
  @abstractmethod
  def record_schema_version(self, schema_name: str, version: str, source: str) -> None: ...
  @abstractmethod
  def get_script_history(self, schema_name: str, script_name: str, script_type: str) -> ScriptRecord | None: ...
  @abstractmethod
  def record_script_execution(self, schema_name: str, record: ScriptRecord,
                              deploy_version: str, deploy_source: str) -> None: ...
  ```
  Импорт `ScriptRecord` из `domain.deploy`.
- `src/db_project_manager/infrastructure/database/postgres/adapter.py`: реализация в
  `PGDatabaseAdapter`. SQL-константы в `postgres/queries.py`:
  - `GET_SCHEMA_VERSION = 'SELECT version FROM "__deploy"."schema_version" ORDER BY applied_at DESC LIMIT 1'`.
  - `INSERT_SCHEMA_VERSION = 'INSERT INTO "__deploy"."schema_version" (version, source) VALUES (%s, %s)'`.
  - `GET_SCRIPT_HISTORY` — SELECT по `(name, type)` → ScriptRecord или None.
  - `UPSERT_SCRIPT_HISTORY` — `INSERT ... ON CONFLICT (script_name, script_type) DO UPDATE SET ...`.
  - `INSERT_SCRIPT_AUDIT_LOG` — INSERT в `script_audit_log`.
  - `record_script_execution` — **явная транзакция**: `with adapter.connection.begin():`
    UPSERT history + INSERT audit. Если audit-INSERT падает → rollback обеих.
  - SQL-идентификаторы double-quoted, fully-qualified (урок §35).
- `tests/unit/test_deploy_service.py:26-104` (`DeployFakeAdapter`): реализовать 4 метода.
  State в `__script_history: dict[tuple[str, str], ScriptRecord]` keyed by `(name, type)`;
  `__script_audit_log: list[dict]`. `record_script_execution` обновляет dict + append list.
- `tests/unit/test_reverse_engineer.py:26` (`FakeAdapter`): реализовать 4 метода как no-op
  stubs (не используются в RE-тестах, но ABC требует реализации — урок §45).

**Урок §45 (критично):** до расширения ABC — `grep -rn "(DatabaseAdapter)" src/ tests/`
найти ВСЕ наследники. Проверить, что их ЧЕТЫРЕ: `PGDatabaseAdapter`, `DeployFakeAdapter`,
`FakeAdapter`, + возможные integration-fakes в `tests/integration/conftest.py`. ВСЕ
обновить в одном коммите.

**Тесты:**
- `tests/unit/test_pg_adapter_deploy.py` (новый): на `PGDatabaseAdapter` через testcontainers
  с `@pytest.mark.integration`. Создать `__deploy` руками → проверить 4 метода (roundtrip
  version, history UPSERT, audit append, transaction rollback).
- `tests/unit/test_deploy_service.py`: regression — 13 существующих тестов зелёные.

**NOT done тут:** canonical-валидация DDL (S5), deploy-flow использование методов (S8).

---

## Шаг P10.S5 — Canonical DDL + seed-шаблоны + валидация-warning

**Цель:** db-pm знает canonical-структуру `__deploy`-таблиц; deploy warns при рассинхроне;
seed-шаблоны готовы для RE (S6).

**Файлы:**
- `src/db_project_manager/infrastructure/sql/templates/deploy/` (новый каталог):
  - `schema_version.sql.j2`, `script_history.sql.j2`, `script_audit_log.sql.j2` — Jinja2
    шаблоны, генерируют DDL с autodoc-заголовком (`immutable: true`). По образцу
    существующих шаблонов (`table.sql.j2` и др.). Параметры: `schema_name` (default
    `__deploy`), `db_name` (для `object_key`).
- `src/db_project_manager/infrastructure/deploy/canonical_ddl.py` (новый):
  - `EXPECTED_DEPLOY_DDL: dict[str, str]` — DDL трёх таблиц как константы (render из
    шаблонов при package-build, или хардкод DDL с `object_key`-placeholder).
  - `def canonical_checksums() -> dict[str, str]:` — `{table_name: sha256(strip_autodoc(ddl))}`
    для трёх таблиц. Считается lazily, кэшируется.
  - `def validate_deploy_ddl(codebase_dir: Path, schema_name: str) -> list[str]:` —
    читает `__deploy/tables/*.sql` из кодовой базы, strip autodoc, normalize, sha256,
    сравнивает с canonical. Возвращает список warning-сообщений (пустой = OK).
- Использование в `deploy_service.py` (вызов в S8): пока только подключение функции,
    основной вызов в S8.

**Импорты:** `infrastructure.sql.templates.deploy.*`, `domain.deploy.{canonical_normalize,
script_checksum}`, Jinja2 env (существующий).

**Тесты:** `tests/unit/test_canonical_ddl.py`
- `test_canonical_checksums_stable`: тот же canonical → тот же SHA-256 (золотое значение
  зафиксировано в тесте; обновляется при намеренном изменении canonical).
- `test_validate_deploy_ddl_match`: codebase с canonical-DDL → `[]` (нет warning'ов).
- `test_validate_deploy_ddl_mismatch`: codebase с модифицированной колонкой → warning с
  указанием таблицы.
- `test_validate_deploy_ddl_missing_table`: отсутствует `script_audit_log.sql` → warning.
- `test_seed_template_renders`: render `schema_version.sql.j2` → валидный SQL с autodoc.

**NOT done тут:** использование `validate_deploy_ddl` в deploy-flow (S8).

---

## Шаг P10.S6 — Reverse-engineer: seed/sync `__deploy`

**Цель:** замкнуть цикл БД ↔ codebase — RE создаёт `__deploy` в дереве при первом импорте,
sync'ает версию при повторном.

**Файлы:**
- `src/db_project_manager/application/reverse_engineer.py` (главный flow):
  после `adapter.get_database_structure()` — проверить наличие служебной схемы
  (configurable, default `__deploy`) в `structure["schemas"]`:
  - **Нет**: инжектировать seed — 3 вершины (schema, 3 tables) с `immutable=True` в
    `structure` перед генерацией дерева. Manifest: если уже есть с `source_version` →
    сохранить; иначе `source_version = calver_seed(now)`.
  - **Есть**: оставить как обычную схему (реверсится через `GET_TABLES`), но при генерации
    autodoc для объектов в служебной схеме — ставить `immutable=True`. Manifest:
    `source_version = adapter.get_schema_version(schema_name)`; если None → fallback
    `calver_seed(now)` + warning.
- `src/db_project_manager/infrastructure/config/codebase_manifest.py`: `write_manifest`
  принимает `source_version` (пробрасывается из RE; `format_version=2` обязательно).

**Конфиг:** добавить в `app_config.py` опцию `deploy.service_schema: str = "__deploy"`
(в `CFG`, уже `extra="ignore"`). RE читает её для имени служебной схемы.

**Импорты:** `domain.deploy.{calver_seed, validate_calver}`, `infrastructure.deploy.canonical_ddl`
(seed-шаблоны рендерятся в `__deploy/tables/*.sql`).

**Тесты:** `tests/unit/test_reverse_engineer_deploy.py`
- `test_re_no_deploy_seeds_three_tables`: structure БЕЗ `__deploy` → в выводе RE есть
  `__deploy/tables/{schema_version,script_history,script_audit_log}.sql`, autodoc
  `immutable: true`.
- `test_re_no_deploy_seeds_manifest_source_version`: structure БЕЗ `__deploy`, manifest
  пустой → `source_version == "2026.08.13.01"` (mock now).
- `test_re_no_deploy_preserves_existing_source_version`: structure БЕЗ `__deploy`, manifest
  с `source_version="2026.07.15.03"` → сохраняется (не seed'ится `.01`).
- `test_re_with_deploy_syncs_source_version`: structure С `__deploy`, `schema_version`
  возвращает `"2026.08.10.05"` → manifest `source_version == "2026.08.10.05"`.
- `test_re_with_deploy_empty_schema_version_falls_back_to_seed`: structure С `__deploy`,
  `get_schema_version` → None → `source_version == calver_seed` + warning в логе.
- `test_re_with_deploy_sets_immutable_on_all_deploy_objects`: 3 таблицы `__deploy` → все с
  `immutable: true`.

**Регрессия RE-тестов:** существующие тесты (без `__deploy` в structure) получат 3 новых
файла в выводе + manifest с `source_version`. Обновить assertions.

**NOT done тут:** deploy-time использование seed-информации (S8).

---

## Шаг P10.S7 — Pre/post runner (`application/script_runner.py`)

**Цель:** идемпотентное выполнение pre/post-скриптов с записью state + history.

**Новый файл:** `src/db_project_manager/application/script_runner.py`

Содержание:
- `class ScriptRunner:` — принимает `adapter: DatabaseAdapter`, `schema_name: str`,
  `deploy_version: str`, `deploy_source: str`.
- `def run_phase(self, phase: Literal["pre", "post"], migrations_dir: Path,
  on_progress: Callable | None = None) -> list[ScriptRecord]:`
  - Discovery: `sorted((migrations_dir / phase).glob("*.sql"))` (CDF-3). Если каталога нет
    → empty list, no-op.
  - Для каждого файла (по порядку): см. _final §4.4 — `checksum = script_checksum(text)`,
    `existing = adapter.get_script_history(...)`, ветвление EXECUTE/SKIP/ERROR.
  - При EXECUTE: `time.perf_counter()` до/после `adapter.execute_script(strip_autodoc(text))`
    → `ScriptRecord(success=True/False, error_message=..., duration_ms=...)`.
    `adapter.record_script_execution(record, deploy_version, deploy_source)` (атомарно).
  - При ERROR: `ScriptRecord(success=False)` тоже пишется в state+audit (для future-retry
    логики и audit'a).
  - На ошибку скрипта: `raise DeployScriptError(record)` (или return errors list, см.
    `--continue-on-error`).
- Валидация имени файла regex `^\d{4}-\d{2}-\d{2}_\d{3}_.+\.sql$` → warning при нарушении
  (не skip; MVP мягкий).

**Импорты:** `domain.deploy.{ScriptRecord, script_checksum}`, `infrastructure.sql.autodoc.strip_autodoc`,
`infrastructure.database.base.DatabaseAdapter`, `deploy_service.ProgressCallback`.

**Тесты:** `tests/unit/test_script_runner.py` (на `DeployFakeAdapter`).
- `test_run_phase_empty_dir_noop`: нет каталога → `[]`.
- `test_run_phase_new_script_executes_and_records`: один новый скрипт → EXECUTE, state +
  audit по одной записи.
- `test_run_phase_skip_when_same_checksum_success`: повторный прогон того же checksum +
  success → SKIP, no execute call.
- `test_run_phase_error_when_same_checksum_failed`: state с `success=False` → ERROR без
  выполнения.
- `test_run_phase_error_when_different_checksum`: state с другим checksum → ERROR.
- `test_run_phase_records_audit_per_attempt`: 3 разных прогона (failed→success→re-run) → 3
  audit, 1 state (UPSERT).
- `test_run_phase_invalid_name_warning`: файл `legacy.sql` → warning, но выполняется.
- `test_run_phase_continue_on_error`: 2 скрипта, первый падает → оба выполняются, errors
  list содержит оба.
- `test_run_phase_stop_on_error_default`: 2 скрипта, первый падает → только первый, raise.

**NOT done тут:** интеграция в deploy-flow (S8).

---

## Шаг P10.S8 — Deploy integration: validate-flow расширение

**Цель:** `DeployValidateService.run()` прогоняет полную механику Phase 10 в temp-БД.

**Файлы:**
- `src/db_project_manager/application/deploy_service.py`:
  - В `run()` (после `create_database`, перед основным циклом deploy):
    1. **validate `__deploy` presence**: scan `codebase_dir` на наличие `__deploy/tables/`.
       Если 3 файла отсутствуют → `DeployError("codebase must include __deploy schema; run
       reverse-engineer to seed it")`.
    2. **canonical-DDL check**: `warnings = validate_deploy_ddl(codebase_dir, schema_name)`;
       для каждого warning → `_emit(warning)` в progress-callback (не блокирует).
    3. **version-check**: `current = adapter.get_schema_version(schema_name)`; на temp-БД
       всегда None (deploy ещё не записывал) → proceed. На real-target (Phase 11+) —
       алгоритм сравнения (final §4.2).
    4. **pre-runner**: `ScriptRunner(adapter, ...).run_phase("pre", codebase_dir / "__migrations")`.
    5. **[существующий deploy-loop]** по вершинам графа (включая `__deploy`-вершины через
       `EARLY_DDL_TYPES`).
    6. **post-runner**: `ScriptRunner(...).run_phase("post", ...)`.
    7. **record version**: `adapter.record_schema_version(schema_name, source_version,
       source="validate")`.
  - Прогресс-сообщения расширить: `"[6/13] validate __deploy presence"`, `"[7/13] canonical
    DDL check"`, и т.д.
  - `source_version` читается из manifest через `read_manifest(codebase_dir)`.

**Импорты:** `application.script_runner.ScriptRunner`, `infrastructure.deploy.canonical_ddl.validate_deploy_ddl`,
`infrastructure.config.codebase_manifest.read_manifest`, `domain.deploy.validate_calver`.

**Тесты:** расширить `tests/unit/test_deploy_service.py`.
- `test_deploy_without_deploy_schema_raises`: codebase без `__deploy/` → `DeployError`.
- `test_deploy_emits_canonical_warning_on_mismatch`: модифицированный DDL → warning emit,
  deploy продолжается.
- `test_deploy_executes_pre_scripts`: fixture с `__migrations/pre/*.sql` → adapter recorded.
- `test_deploy_executes_post_scripts`: аналогично для post.
- `test_deploy_records_schema_version`: после успеха → `get_schema_version` возвращает
  `source_version`.
- `test_deploy_progress_messages`: callback содержит шаги validate-presence / canonical /
  pre / version / post.

**Регрессия:** 13 существующих тестов `test_deploy_service.py` — теперь требуют fixture с
`__deploy/` + `__migrations/` (S9). Обновить fixture-использование в тестах.

**NOT done тут:** fixture ещё не обновлён (S9).

---

## Шаг P10.S9 — Fixture: `__deploy/` + `__migrations/` + manifest v2

**Цель:** тестовый codebase содержит всё нужное для Phase 10 механики.

**Файлы:**
- `tests/fixtures/codebase_sample/__deploy/tables/` (новый каталог): 3 файла, сгенерированные
  из canonical templates (можно просто скопировать рендер):
  - `schema_version.sql`, `script_history.sql`, `script_audit_log.sql` — с autodoc
    (`immutable: true`).
- `tests/fixtures/codebase_sample/__migrations/pre/2026-08-11_001_init.sql` — простой
  идемпотентный скрипт (напр. `CREATE TABLE IF NOT EXISTS app.tmp (id int);`).
- `tests/fixtures/codebase_sample/__migrations/post/2026-08-11_001_check.sql` —
  идемпотентный post-скрипт.
- `tests/fixtures/codebase_sample/dbpm.manifest.json`: обновить до `format_version: 2` +
  `"source_version": "2026.08.11.01"`.
- Опционально: `tests/fixtures/codebase_without_deploy/` — урезанный fixture без `__deploy/`,
  для `test_deploy_without_deploy_schema_raises` (или модифицировать существующий test на
  лету monkeypatch'ить path).

**Тесты:** обновить assertions в существующих тестах, которые считают файлы/объекты
(`objects_total == 13` → теперь +3 таблицы `__deploy` + 2 миграции не считаются как graph
объекты — миграции не в дереве схемы).

- `test_deploy_service.py`: `objects_total` augment на 3 (`13 → 16`).
- `test_reverse_engineer.py`: assertions на число сгенерированных файлов augment.
- `test_compare_cli.py` (Phase 9): compare fixture получит +3 `__deploy`-объекта;
  assertions обновить (или добавить `__deploy` в exclude-фильтр — backlog).

**NOT done тут:** финальная регрессия + integration (S10).

---

## Шаг P10.S10 — Регрессия + integration tests + smoke

**Цель:** сквозное подтверждение Phase 10 — все unit зелёные, integration проходит,
CLI-smoke корректен.

**Integration тесты:** `tests/integration/test_deploy_validate_e2e.py` (расширить, уже
`@pytest.mark.integration`, testcontainers).
- `test_deploy_validate_creates_deploy_schema`: после deploy → `__deploy`-схема существует
  в temp-БД (до drop) с 3 таблицами.
- `test_deploy_validate_records_version`: после deploy → `SELECT version FROM
  __deploy.schema_version` = `source_version` из manifest.
- `test_deploy_validate_pre_post_executed`: pre/post скрипты применились (например, pre
  создаёт таблицу, post проверяет — видимы в temp-БД до drop).
- `test_re_deploy_re_cycle`: RE → deploy → повторный RE — `source_version` sync'ается из
  `schema_version` корректно. Это **ключевой end-to-end тест замкнутого цикла**.
- `test_deploy_validate_canonical_mismatch_warning`: fixture с модифицированным DDL →
  warning в stdout, deploy продолжается.

**Smoke CLI:**
```bash
unset SSL_CERT_FILE REQUESTS_CA_BUNDLE CURL_CA_BUNDLE && uv run python -m db_project_manager \
    deploy validate --dir tests/fixtures/codebase_sample \
    --connection-file connections/test.yaml --keep-db
# Ожидаемый лог содержит:
# [6/13] validate __deploy presence
# [7/13] canonical DDL check
# [9/13] pre-runner (2 scripts)
# ... graph deploy ...
# [11/13] post-runner (2 scripts)
# [12/13] record schema_version 2026.08.11.01
```

**Smoke RE:**
```bash
unset SSL_CERT_FILE ... && uv run python -m db_project_manager reverse-engineer \
    --connection-file connections/test.yaml --output /tmp/re_test
# В /tmp/re_test/<db>/ есть __deploy/tables/{3 файла}, __migrations/ (пустой или с legacy),
# dbpm.manifest.json с source_version.
```

**Регрессия:**
- `uv run pytest tests/unit/ -q` → все зелёные (baseline 516 тестов + ~30-40 новых Phase 10).
- `uv run ruff check src/ tests/` → All checks passed.
- `uv run pytest -m integration` → 5+ Phase 10 integration зелёные (если Docker доступен).

**Финальные проверки перед закрытием Phase 10:**
- [ ] Чеклист по урокам (final §7) — все пункты закрыты.
- [ ] BACKLOG обновлён (если что-то из P2/P3 закрыто Phase 10).
- [ ] ROADMAP §9 — USER_INPUT Q3/Q4/Q5 помечены закрытыми (как Q1/Q2 в Phase 14).
- [ ] Чекпойнт `20260813_001_checkpoint.md` написан.
- [ ] `-=PHASES=-/Phase_10.md` написан (свод фазы).
- [ ] `Phase_10_result.md` написан (результат, ссылки на коммиты).

---

## Чеклист по урокам (для самопроверки перед каждым коммитом)

- [ ] §12: `git add -- "-=docs=-/-=tasks=-/phase_10/..."` (дефис в `-=docs=-`).
- [ ] §18/§45: при расширении `DatabaseAdapter` (S4) — ВСЕ fakes в одном коммите; `grep
  -rn "(DatabaseAdapter)"` перед стартом.
- [ ] §19: configurable имя `__deploy` — whitelist + double-quote во ВСЕХ DDL/queries.
- [ ] §23: pre/post-скрипты — strip autodoc перед `execute_script` и перед checksum.
- [ ] §28: roundtrip через pydantic для `version`/`checksum`, не substring-матчинг.
- [ ] §32: temp-БД через `template0` fallback; `__deploy`-DDL работает на дефолтном
  `search_path`.
- [ ] §34/§35: все идентификаторы fully-qualified (`"__deploy"."schema_version"`), double-
  quoted. Контракт тестом.
- [ ] §45: fakes обновляются в одном коммите с ABC.
- [ ] TASK_CONVENTIONS §6: код и документы — разные коммиты.

---

## NOT done в Phase 10 (явно, для `Phase_10.md` и чекпойнта)

- Real-target deploy (deploy в существующую БД, без temp) — Phase 11+.
- Safety-gate (pre-analysis) — Phase 11.
- Структурный column-diff + ALTER-план — Phase 12.
- CLI `--reseed-deploy` для RE (canonical-mismatch fallback) — backlog.
- Жёсткая canonical-DDL-валидация (hard-block вместо warning) — backlog.
- Фильтр служебных схем в compare — backlog.
- `script_watermark` — отложено (CDF-5).

---

## Где читать дальше

- `-=tasks=-/phase_10/Phase_10_vision_final.md` — нормативный дизайн (источник решений).
- `-=tasks=-/phase_10/Phase_10_vision_draft.md` — предшественник (история обсуждения).
- `-=CHECKPOINTS=-/20260804_001_checkpoint.md` — текущее состояние (Phase 14 done).
- `-=tasks=-/ROADMAP.md` §2/§4/§7/§8.
- `-=PHASES=-/Phase_02.md` — validation deploy (fundament).
- `LESSONS_LEARNED.md` §12, §18, §19, §23, §28, §32, §34, §35, §45.
- `-=tasks=-/phase_14/Phase_14_plan.md` — образец структуры plan (этот файл сделан по нему).
