# Phase 11: План реализации — Safety Gate (`deploy analyze`)

> **Дата:** 2026-08-14
> **Ветка:** dev
> **Статус:** plan (нормативный документ для пошаговой реализации; на основе `_final`)
>
> Норматив-дизайн: `-=tasks=-/phase_11/Phase_11_vision_final.md`.
> Предшественник (история обсуждения): `-=tasks=-/phase_11/Phase_11_vision_draft.md`.
> Контекст: чекпойнт 20260814_001; ROADMAP §2/§4/§7/§8; Phase_09.md, Phase_10_vision_final;
> LESSONS §3, §12, §18/§45, §19, §23, §28, §34/§35, §39, §40, §41, §42, §43.

---

## Принцип разбиения коммитов

Один логический шаг — один коммит (TASK_CONVENTIONS §6). **Код и документы не смешиваются.**
Шаги «снизу-вверх»: чистый domain → контракт адаптера (+fakes одним коммитом) → инфраструктурные
helpers (coverage, отчёт) → application-сервис → CLI → GUI → integration → регрессия/документы.
Тесты пишутся в том же коммите, что и код.

Каждый шаг заканчивается прогоном:
```bash
unset SSL_CERT_FILE REQUESTS_CA_BUNDLE CURL_CA_BUNDLE && uv run pytest tests/unit/ -q
uv run ruff check src/ tests/
```

Зависимости между шагами:
```
S1 (domain/safety) ─┬─> S2 (adapter + fakes)
                    ├─> S3 (coverage parser)
                    └─> S4 (report writer)
S2 + S3 + S4 ─> S5 (safety_gate_service) ─> S6 (CLI) ─> S7 (GUI)
                                    └────> S8 (integration e2e) ─> S9 (регрессия + docs)
```

---

## Шаг P11.S1 — Domain-модели (`domain/safety.py`)

**Цель:** чистые, изолированно тестируемые типы и функции — без зависимости от adapter/PG.
Основа для S2/S5.

**Новый файл:** `src/db_project_manager/domain/safety.py`

Содержание (спека — final §4.2):
- `StatsConfidence(str, Enum)`: `FRESH | STALE | UNKNOWN`.
- `TablePresenceStats(BaseModel)`: `schema: str`, `name: str`,
  `estimated_rows: int | None = None`, `confidence: StatsConfidence`.
  `model_config = ConfigDict(extra="ignore")` (по образцу `domain/diff.py:66`).
- `DataPresence(str, Enum)`: `HAS_DATA | EMPTY | UNKNOWN`.
- `TableTouchKind(str, Enum)`: `CHANGED | REMOVED`.
- `def classify_presence(stats: TablePresenceStats) -> DataPresence:` — чистая функция:
  `estimated_rows is not None and > 0` → `HAS_DATA`; `== 0 and confidence == FRESH` → `EMPTY`;
  иначе → `UNKNOWN` (SG-4, fail-safe).
- `TouchedTable(BaseModel)`: `schema, name, touch: TableTouchKind, estimated_rows: int | None,
  confidence: StatsConfidence, presence: DataPresence, covered_by: list[str] = []`;
  properties `covered: bool` (`len(covered_by) > 0`) и `is_violation: bool`
  (`presence in {HAS_DATA, UNKNOWN} and not covered`).
- `VersionCheckOutcome(str, Enum)`: `PROCEED | WARN_SAME | ERROR_NEWER`.
- `def check_version_relation(target: str | None, source: str) -> VersionCheckOutcome:`
  (SG-6): `None` → PROCEED; `==` → WARN_SAME; `>` (строковое calver-сравнение,
  лексикографическое = хронологическое, CDF-9) → ERROR_NEWER; `<` → PROCEED.
- `SafetyGateVerdict(BaseModel)`: `clean: bool`, `db_type: str`,
  `source_version: str | None`, `target_version: str | None`, `touched: list[TouchedTable]`;
  property `violations -> list[TouchedTable]`.

**Импорты:** только `enum`, `pydantic`. Никаких infrastructure-импортов (SG-M).

**Тесты (этот же коммит):** `tests/unit/test_safety_domain.py`
- `test_classify_presence_matrix` (параметризованный): `(5, FRESH)→HAS_DATA`,
  `(0, FRESH)→EMPTY`, `(0, STALE)→UNKNOWN`, `(0, UNKNOWN)→UNKNOWN`, `(None, FRESH)→UNKNOWN`,
  `(None, UNKNOWN)→UNKNOWN`, `(-1, FRESH)→UNKNOWN`.
- `test_touched_table_roundtrip` (§28): pydantic roundtrip; enum сериализуются как строки.
- `test_is_violation`: HAS_DATA+uncovered → True; HAS_DATA+covered → False; EMPTY+uncovered →
  False; UNKNOWN+uncovered → True.
- `test_check_version_relation` (параметризованный, 4 ветки): `(None, x)→PROCEED`,
  `("2026.08.11.01","2026.08.11.01")→WARN_SAME`, `("2026.08.12.01","2026.08.11.01")→ERROR_NEWER`,
  `("2026.08.10.01","2026.08.11.01")→PROCEED`.
- `test_verdict_violations_property`: mixed touched → violations только незакрытые с данными.

**NOT done тут:** adapter-контракт (S2), кто заполняет модели (S5).

---

## Шаг P11.S2 — Контракт `get_table_presence_stats` + PG-имплементация + fakes

**Цель:** нормализованный presence-сигнал из адаптера. ВСЕ fakes — в одном коммите (урок §45).

**Файлы:**
- `src/db_project_manager/infrastructure/database/base.py`: новый abstract (после
  `get_table_row_counts`, ~строка 94):
  ```python
  @abstractmethod
  def get_table_presence_stats(self) -> list[TablePresenceStats]:
      """Presence stats (estimated rows + freshness) for user tables, normalized per-DB."""
  ```
  Импорт `TablePresenceStats` из `domain.safety`. Существующий `get_table_row_counts` НЕ трогаем.
- `src/db_project_manager/infrastructure/database/postgres/queries.py`: константа
  `GET_TABLE_PRESENCE_STATS`:
  ```sql
  SELECT n.nspname AS schema_name, c.relname AS table_name,
         c.reltuples AS estimated_rows,
         s.last_analyze, s.last_autoanalyze, s.n_mod_since_analyze
  FROM pg_catalog.pg_class c
  JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
  LEFT JOIN pg_catalog.pg_stat_user_tables s
         ON s.schemaname = n.nspname AND s.relname = c.relname
  WHERE c.relkind = 'r'
    AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  ```
  Идентификаторы fully-qualified через `pg_catalog.` (§34/§35). Служебные схемы (`__deploy`)
  адаптер НЕ исключает — это делает gate (S5, application-слой).
- `src/db_project_manager/infrastructure/database/postgres/adapter.py`: реализация:
  - **Чистая функция маппинга** (module-level, тестируемая без БД):
    `def _map_presence_row(schema, name, estimated_rows, last_analyze, last_autoanalyze,
    n_mod_since_analyze) -> TablePresenceStats`:
    - `estimated_rows is None or < 0` → `STALE`;
    - `last_analyze is None and last_autoanalyze is None` (never analyzed) → `STALE`;
    - `n_mod_since_analyze` is not None and `n_mod_since_analyze >= max(estimated_rows, 1)` → `STALE`;
    - иначе `FRESH`.
  - Метод: `_exec(q.GET_TABLE_PRESENCE_STATS)` → list comprehension по `_map_presence_row`.
- **Fakes (урок §45 — одним коммитом!):** до расширения ABC выполнить
  `grep -rn "(DatabaseAdapter)" src/ tests/` и обновить ВСХ наследников:
  - `tests/unit/test_deploy_service.py` (`DeployFakeAdapter`): in-memory список presence-строк
    (настраиваемый атрибут для тестов gate, S5).
  - `tests/unit/test_reverse_engineer.py` (`FakeAdapter`): no-op → `[]`.
  - Прочие найденные grep'ом (integration-fakes и т.п.) — аналогично.

**Тесты:**
- `tests/unit/test_pg_presence_mapping.py` (новый, чистый unit на `_map_presence_row`):
  параметризованная матрица — normal→FRESH; never-analyzed→STALE; `-1`→STALE; `NULL`→STALE;
  drift (`n_mod_since_analyze >= rows`)→STALE; small drift→FRESH.
- `tests/unit/test_pg_adapter_deploy.py` или новый integration-файл
  (`@pytest.mark.integration`, testcontainers): реальный PG — таблица с INSERT → `estimated_rows`
  прочитан; пустая свежая → FRESH/0; `pg_catalog`/`information_schema` отсутствуют в результате.

**NOT done тут:** использование метода в gate (S5).

---

## Шаг P11.S3 — Coverage-парсер (`infrastructure/deploy/pre_coverage.py`)

**Цель:** статическое извлечение покрытия таблиц pre-скриптами (SG-3).

**Новый файл:** `src/db_project_manager/infrastructure/deploy/pre_coverage.py`

Содержание:
- `def read_pre_coverage(migrations_dir: Path) -> dict[tuple[str, str], list[str]]` —
  `{(schema, table): [script_name, ...]}`.
- Нет каталога `migrations_dir/pre` → `{}` (не ошибка).
- `sorted((migrations_dir / "pre").glob("*.sql"))`; для каждого: `extract_header(text)`
  (существующий, `infrastructure/sql/autodoc.py`) → `project.covers` — список строк
  `"schema.table"`. Нормализация: `split(".", 1)` → `(schema, table)`; strip пробелов.
- Некорректные записи (не строка, нет точки, пустые части) → `logger.warning(...)` + skip (§23:
  pre-скрипты содержат autodoc; протокол предупреждений — в духе §36).
- Файл без autodoc / без `covers` → не участвует (не ошибка).

**Импорты:** `pathlib`, `logging`, `infrastructure.sql.autodoc.extract_header`.

**Тесты (этот же коммит):** `tests/unit/test_pre_coverage.py` (фикстуры через `tmp_path`)
- `test_no_dir_returns_empty`: нет `pre/` → `{}`.
- `test_valid_covers_parsed`: скрипт с `project: {covers: ["app.orders", "app.items"]}` →
  обе пары → `[имя скрипта]`.
- `test_no_autodoc_no_coverage`: обычный SQL без autodoc → `{}`.
- `test_broken_covers_warn_and_skip`: `covers: ["justname", "", 42]` → warning, валидные
  записи при этом сохраняются.
- `test_multiple_scripts_merge`: два скрипта покрывают одну таблицу → список из двух имён.
- `test_scripts_sorted`: порядок обхода сортированный (для детерминизма covered_by).

**NOT done тут:** сопоставление с touched (S5).

---

## Шаг P11.S4 — Отчёт gate (`infrastructure/deploy/safety_report.py`)

**Цель:** рендер вердикта в md + json (SG-2).

**Новый файл:** `src/db_project_manager/infrastructure/deploy/safety_report.py`

Содержание:
- `def write_safety_report(verdict: SafetyGateVerdict, output_dir: Path) -> list[Path]:`
  создаёт `output_dir` при необходимости (`mkdir(parents=True, exist_ok=True)`, §31) и пишет:
  - `safety_gate_report.json` — `verdict.model_dump(mode="json")` (+ `generated_at`).
  - `safety_gate_report.md` — по образцу `infrastructure/diff/markdown_report.py`:
    - шапка: `db_type`, source/target version, дата, verdict (CLEAN / VIOLATIONS: N);
    - таблица тронутых: `schema.table | touch | ~rows | confidence | presence | covered_by`;
    - секция «Нарушения» (только если есть): на каждую — рекомендация «добавьте pre-скрипт в
      `__migrations/pre/` и объявите в его autodoc `project.covers: ["schema.table"]`»;
    - финальная строка-резюме для CI-логов.
- Возвращает пути созданных файлов (GUI покажет их пользователю).

**Тесты:** `tests/unit/test_safety_report.py`
- `test_clean_report`: verdict без touched → md содержит CLEAN, нет секции нарушений; json
  roundtrip через pydantic (§28: `SafetyGateVerdict.model_validate_json`), `clean is True`.
- `test_violation_report`: touched с violation → md содержит `schema.table`, тип touch,
  рекомендацию `project.covers`; json: `touched[0].is_violation` через roundtrip-модель.
- `test_files_created`: оба файла существуют в `output_dir` (§31: вложенные директории).

**NOT done тут:** кто строит verdict (S5).

---

## Шаг P11.S5 — `SafetyGateService` (`application/safety_gate_service.py`)

**Цель:** оркестрация dry-run анализа (final §4.1).

**Новый файл:** `src/db_project_manager/application/safety_gate_service.py`

Содержание:
- `class SafetyGateError(Exception)` — hard errors (соединение/manifest/версия/тип БД).
- `class SafetyGateService:`
  ```python
  def __init__(self, adapter_factory: Callable[[ConnectionConfig], DatabaseAdapter] = get_adapter,
               compare_service: CompareService | None = None, service_schema: str | None = None):
      # service_schema=None → читать из CFG.deploy.service_schema (default "__deploy")
  def analyze(self, codebase_dir: Path, target_cfg: ConnectionConfig, output_dir: Path,
              on_progress: Callable[[str], None] | None = None) -> SafetyGateVerdict:
  ```
  Шаги (каждый — progress-сообщение):
  1. `adapter = self._adapter_factory(target_cfg); adapter.connect(target_cfg)` (real-target!).
  2. `manifest = read_manifest(codebase_dir)`; если `manifest.db_type != target_cfg.type` →
     `SafetyGateError` (SG-M; fail-fast до тяжёлой работы).
  3. version-check: `target_version = adapter.get_schema_version(service_schema)`;
     `check_version_relation(target_version, manifest.source_version)` → ERROR_NEWER →
     `SafetyGateError` (exit 2); WARN_SAME → progress-warning.
  4. delta: `CompareService.run(source=SideSpec(DIR, codebase_dir),
     target=SideSpec(DB, target_cfg), output_dir=output_dir)` → DiffReport (+ row_counts внутри
     DB-стороны). Прогон в `try/except CompareError → SafetyGateError`.
  5. touched: `[e for e in report.entries if e.status in {CHANGED, REMOVED}
     and (e.source_snapshot or e.target_snapshot).object_type == "table"]` —
     object_type/schema/name из доступного снапшота (REMOVED → target_snapshot).
  6. presence: `stats = adapter.get_table_presence_stats()` → lookup
     `{(s.schema, s.name): s}`; **исключить `service_schema`** (application-слой, SG-4/SG-5);
     для каждого touched: `classify_presence(stats)` (нет в stats — например таблица в другой БД —
     → `TablePresenceStats(..., estimated_rows=None, confidence=UNKNOWN)`, fail-safe).
  7. coverage: `read_pre_coverage(codebase_dir / "__migrations")` → `covered_by`.
  8. `SafetyGateVerdict(clean=not violations, db_type=manifest.db_type, ...)`.
  9. `write_safety_report(verdict, output_dir)`.
  10. finally: `adapter.disconnect()`; return verdict.
- **Read-only контракт:** ни один мутирующий метод адаптера не вызывается (проверяется тестом).

**Импорты:** `domain.safety.*`, `application.compare_service.{CompareService, SideSpec}`,
`infrastructure.deploy.{pre_coverage, safety_report}`, `infrastructure.config.codebase_manifest.read_manifest`,
`infrastructure.database.registry.get_adapter`.

**Тесты (этот же коммит):** `tests/unit/test_safety_gate_service.py`
Фикстуры: кодовая база через `tmp_path` (минимальная: manifest + 1-2 `.sql` с autodoc; или копия
`tests/fixtures/codebase_sample`); `DeployFakeAdapter` (S2) с настраиваемым presence;
`_StubCompareService` (по образцу `_StubReverseEngineer` в `test_compare_service.py:49`),
возвращающий синтетический DiffReport.
- `test_db_type_mismatch_raises`: manifest `postgres`, cfg `snowflake` → `SafetyGateError`
  (fail-fast: stub-compare НЕ вызван).
- `test_target_newer_version_raises`: `get_schema_version → "2026.09.01.01"`, source `.08.14` →
  `SafetyGateError`.
- `test_same_version_warns_but_proceeds`: `==` → progress содержит warning, анализ идёт.
- `test_changed_table_with_data_uncovered_violates`: DiffReport CHANGED table + presence
  `(1000, FRESH)` → verdict не clean, violation в отчёте.
- `test_removed_table_with_data_uncovered_violates`: REMOVED + данные → violation.
- `test_covered_table_not_violation`: то же + pre-скрипт с `covers` → clean.
- `test_empty_table_changed_ok`: `(0, FRESH)` → не violation.
- `test_stale_stats_fail_safe`: `(0, STALE)` без coverage → violation (CD-7).
- `test_unknown_table_stats_fail_safe`: таблица отсутствует в stats → UNKNOWN → при отсутствии
  coverage violation.
- `test_added_and_unchanged_ignored`: ADDED/UNCHANGED entries (в т.ч. таблицы с данными) →
  не в touched.
- `test_non_table_objects_ignored`: CHANGED function/view → не в touched (CD-10).
- `test_service_schema_excluded`: touched в `__deploy` → игнорируется.
- `test_no_mutating_adapter_calls`: после analyze — fake фиксирует, что create/drop/execute/
  record_* не вызывались ни разу (read-only контракт).
- `test_reports_written`: `output_dir` содержит md + json.

**NOT done тут:** CLI-обвязка (S6), GUI (S7).

---

## Шаг P11.S6 — CLI `deploy analyze`

**Цель:** пользовательская команда с exit codes для CI (SG-1).

**Файлы:**
- `src/db_project_manager/presentation/cli/main.py`: подкоманда в группе `deploy` (урок §46:
  обязательные опции — первыми в сигнатуре):
  ```python
  @deploy_app.command("analyze")
  def deploy_analyze(dir: Path, target_connection_file: Path, output_dir: Path, ...):
  ```
  - Загружает `ConnectionConfig` через `_load_connection` (существующий helper).
  - `SafetyGateService().analyze(...)`; выводит резюме (verdict + N нарушений + пути отчётов).
  - Exit codes (SG-1): `0` clean; `1` violations (`typer.Exit(1)`); `2` `SafetyGateError`/
    `ConnectionError` (перехват → понятное сообщение → `typer.Exit(2)`).
  - `--help`: «read-only: ничего не применяется к целевой БД».

**Тесты:** `tests/unit/test_deploy_analyze_cli.py` (CliRunner, monkeypatch сервиса на stub)
- `test_cli_clean_exit_zero`: stub clean verdict → exit 0; вывод содержит CLEAN.
- `test_cli_violation_exit_one`: violations → exit 1; вывод содержит имя таблицы.
- `test_cli_hard_error_exit_two`: сервис поднял `SafetyGateError` → exit 2.
- `test_cli_builds_correct_call` (контракт): перехват аргументов stub-сервиса — `dir`,
  `target_connection_file`, `output_dir` проброшены верно.

**NOT done тут:** GUI (S7).

---

## Шаг P11.S7 — GUI-действие run-only (5-е в реестре Phase 7)

**Цель:** запуск analyze из панели действий; verdict + ссылка на отчёт (SG-7).

**Файлы (все — по образцу `deploy_validate`):**
- `src/db_project_manager/presentation/gui/actions/models.py`: `DeployAnalyzeSettings(BaseModel)`:
  `codebase_dir: str`, `target_connection: str`, `output_dir: str` (§40 — имена без коллизий
  с BaseModel; проверить import-warnings).
- `src/db_project_manager/presentation/gui/actions/registry.py`: `ActionSpec(action_id=
  "deploy_analyze", ...)` — 5-й элемент `ACTIONS` (строка 142); заголовок, описание, иконка —
  по образцу существующих.
- `src/db_project_manager/presentation/gui/actions/cli.py`: билдер CLI-строки
  `db-pm deploy analyze --dir ... --target-connection-file ... --output-dir ...`.
- `src/db_project_manager/presentation/gui/actions/dialogs.py`: `DeployAnalyzeDialog` — выбор
  кодовой базы (dir-picker), целевого подключения (список из ConnectionStore), output-dir;
  кнопки — ПОСЛЕДНИМИ через `_add_buttons()` (§43).
- Worker-обвязка: по образцу `deploy_validate` — фоновый `QRunnable`; strong-ref
  `self._active_workers[worker.signals] = (worker, action_id, settings)`; `finished` →
  bound-метод `_on_worker_finished` (§42 — НЕ лямбда); результат = verdict-текст
  (CLEAN / VIOLATIONS: N) + кнопка/ссылка «Открыть safety_gate_report.md».

**Тесты:**
- `tests/unit/test_action_cli_build.py` (или расширить существующий тест билдеров): контракт
  GUI→CLI — построенная строка парсится `_argv` (§39: `shlex.split(cmd, posix=False)`, первый
  токен отброшен) и через `CliRunner` даёт корректный вызов stub-сервиса.
- `tests/unit/test_action_panel_smoke.py` (расширить, offscreen §41):
  - конструктор `DeployAnalyzeDialog` строится offscreen; последний виджет формы —
    `QDialogButtonBox` (§43);
  - execute → waitForDone → processEvents → панель разблокирована (§42, по образцу
    существующего регрессионного теста);
  - после завершения статус содержит CLEAN/VIOLATIONS и путь отчёта.

**NOT done тут:** рендер markdown-отчёта в окне — Phase 14 (DV).

---

## Шаг P11.S8 — Integration e2e (`@pytest.mark.integration`)

**Цель:** сквозная проверка на реальном PG (testcontainers) — final §5.

**Новый файл:** `tests/integration/test_deploy_analyze_e2e.py`

Сетап (общая фикстура): поднять PG-контейнер → `create schema app; create table app.orders(id int
primary key, note text); insert ~100 rows; create table app.empty_t(id int); create function...`
→ RE этой БД в tmp-каталог (получаем codebase c manifest; bump `source_version`) → модификации
по сценарию.

Сценарии:
- `test_changed_data_table_uncovered_fails`: в codebase поменять DDL `app.orders` (напр. добавить
  колонку в файл) → analyze → `clean is False`, violation = `app.orders`, отчёт содержит
  рекомендацию; в БД НИЧЕГО не изменилось (проверить структуру до/после).
- `test_covers_pre_script_passes`: добавить `__migrations/pre/2026-08-14_001_migrate_orders.sql`
  с autodoc `project.covers: ["app.orders"]` → analyze → `clean is True`.
- `test_empty_table_changed_passes`: модифицировать DDL `app.empty_t` → clean (0 строк).
- `test_added_objects_pass`: добавить в codebase новую таблицу/функцию → clean.
- `test_target_newer_version_hard_error`: записать в БД `__deploy.schema_version` версию новее
  manifest → `SafetyGateError`.

**Smoke CLI (ручная проверка):**
```bash
unset SSL_CERT_FILE REQUESTS_CA_BUNDLE CURL_CA_BUNDLE && uv run python -m db_project_manager \
    deploy analyze --dir tests/fixtures/codebase_sample \
    --target-connection-file connections/test.yaml --output-dir /tmp/sg_report
# Ожидаемо: прогресс шагов (connect → manifest → version → compare → presence → coverage →
# verdict), резюме, exit code по вердикту; в connections/test.yaml указать БД С ДАННЫМИ.
```

---

## Шаг P11.S9 — Регрессия + документация + закрытие фазы

**Порядок:**
1. Полная регрессия: `uv run pytest tests/unit/ -q` — все зелёные (baseline 609 + ~50-60 новых
   Phase 11); `uv run ruff check src/ tests/` — чисто; `uv run pytest -m integration` — 5 новых
   Phase 11 e2e зелёные (если Docker доступен).
2. README: добавить `deploy analyze` в раздел CLI (read-only, exit codes).
3. ROADMAP: Phase 11 → ✅ done (коммиты); §9 — при необходимости новые закрытые Q.
4. BACKLOG: если что-то выяснится (напр. Greenplum-специфика) — завести пункт.
5. Документы фазы (каждый — отдельный коммит, TASK_CONVENTIONS §6):
   - `-=tasks=-/phase_11/Phase_11_result.md` — результат, коммиты, известные ограничения.
   - `-=PHASES=-/Phase_11.md` — свод фазы.
   - `-=CHECKPOINTS=-/<date>_001_checkpoint.md` — сессионный снапшот.
6. LESSONS_LEARNED: добавить уроки фазы (вероятные кандидаты: Greenplum-распределённые таблицы
   в presence-stats; CliRunner/exit-code контракт).

---

## Чеклист по урокам (самопроверка перед каждым коммитом)

- [ ] §3: presence через `reltuples`-метаданные, не `COUNT`.
- [ ] §12: `git add -- "-=docs=-/-=tasks=-/phase_11/..."`.
- [ ] §18/§45: S2 — ABC + `PGDatabaseAdapter` + ВСЕ fakes одним коммитом; перед стартом
  `grep -rn "(DatabaseAdapter)"`.
- [ ] §19: whitelist исключения системных схем в PG-запросе.
- [ ] §23: parse autodoc из pre-скриптов — аккуратно (covers может отсутствовать).
- [ ] §28: roundtrip через pydantic (json-отчёт, verdict), не substring.
- [ ] §31: `mkdir(parents=True, exist_ok=True)` перед записью в `output_dir`.
- [ ] §34/§35: `pg_catalog.`-qualified идентификаторы в `GET_TABLE_PRESENCE_STATS`.
- [ ] §39: GUI→CLI контракт — argv без имени программы, `shlex.split(posix=False)`.
- [ ] §40: имена полей `DeployAnalyzeSettings` — проверить import-warnings.
- [ ] §41: offscreen smoke для новых диалогов/панели.
- [ ] §42: worker strong-ref + bound-метод (НЕ лямбда в connect).
- [ ] §43: кнопки диалога — последними.
- [ ] §46: в typer-сигнатуре `deploy analyze` обязательные опции — первыми.
- [ ] TASK_CONVENTIONS §6: код и документы — разные коммиты.

---

## NOT done в Phase 11 (явно, для `Phase_11.md` и чекпойнта)

- Real-target apply (безопасные операции + выполнение pre-скриптов на живой БД) — Phase 12.
- Структурный column-diff + ALTER-план — Phase 12 (CD-ALT-1..4, CD-11..CD-15).
- Повторный анализ после pre-скриптов (CD-11) — Phase 12.
- Рендер отчёта gate в GUI/вкладка Delta Viewer — Phase 14.
- Greenplum-валидация распределённых таблиц в presence-stats — при появлении кластера.
- AI-объяснение отчёта (CD-AI-2) — overlay после Phase 12.

---

## Где читать дальше

- `-=tasks=-/phase_11/Phase_11_vision_final.md` — нормативный дизайн (источник решений).
- `-=tasks=-/phase_11/Phase_11_vision_draft.md` — история обсуждения (USER_INPUT SG-1..SG-7).
- `-=tasks=-/phase_10/Phase_10_plan.md` — образец структуры и процесс реализации.
- `-=CHECKPOINTS=-/20260814_001_checkpoint.md` — текущее состояние (Phase 10 done).
- `-=tasks=-/ROADMAP.md` §2/§4 (CD-6..CD-10)/§7/§8.
- `LESSONS_LEARNED.md` §3, §12, §18/§45, §19, §23, §28, §31, §34/§35, §39-§43, §46.
