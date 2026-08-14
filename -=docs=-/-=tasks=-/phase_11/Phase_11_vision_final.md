# Phase 11: Safety Gate — финальный дизайн (vision final)

> **Дата:** 2026-08-14
> **Ветка:** dev
> **Статус:** final (все USER_INPUT закрыты — SG-0..SG-A + multi-DB + SG-7; нормативный документ
> для `_plan` и реализации)
>
> Предшествующий артефакт: `-=tasks=-/phase_11/Phase_11_vision_draft.md` (не удаляется — остаётся
> для истории обсуждения, по TASK_CONVENTIONS §2.2).
>
> Контекст:
> - `-=CHECKPOINTS=-/20260814_001_checkpoint.md` — текущее состояние (Phase 10 done)
> - `-=tasks=-/ROADMAP.md` §2 (шаг 3), §3 (safety principle), §4 (CD-6..CD-10), §7 (правила), §8 (пайплайн)
> - `-=tasks=-/phase_10/Phase_10_vision_final.md` — фундамент (versioning, `__deploy`, runner)
> - `-=PHASES=-/Phase_09.md` — compare (fundament CD-6)
> - `src/db_project_manager/application/{compare_service,deploy_service,script_runner}.py`
> - `src/db_project_manager/infrastructure/diff/{comparator,snapshot}.py`
> - `src/db_project_manager/infrastructure/database/{base,postgres/adapter,postgres/queries}.py`
> - `src/db_project_manager/presentation/gui/actions/{models,registry,cli,dialogs}.py`
> - `LESSONS_LEARNED.md` §3, §12, §18/§45, §19, §23, §28, §34/§35, §40, §41, §42, §43

---

## 1. Постановка проблемы

Phase 10 заложила **механику** CD-ядра (`__deploy`, calver, идемпотентный runner), но вся она
крутится в **validate-flow** — кодовая база применяется в пустую temp-БД
(`deploy_service.py:175-193`, `deploy_source="validate"`). Пути деплоя/анализа против
**существующей БД с данными** нет. Safety-gate (CD-6..CD-10) осмысленен только против такой БД:
против пустой temp-БД каждая таблица = ADDED (greenfield), данных нет, ловить нечего.

Diff-слой сегодня **hash-level**: «changed» таблица непрозрачна (`comparator.py:66` решает по
`sql_hash`); column-структура при RE считывается (`adapter.py:471-509`) и выбрасывается. Структурный
column-diff по ROADMAP — Phase 12 (CD-ALT-1).

**Ключевой инсайт:** правило CD-10 («любое изменение таблицы с данными без покрывающего pre-скрипта
= блок») не требует знания точного типа изменения. Достаточно «таблица тронута ∧ есть данные ∧ нет
покрывающего pre-скрипта». Поэтому Phase 11 строится поверх существующего hash-diff, а column-diff
остаётся Phase 12. Применение дельты (apply) — тоже Phase 12: Phase 11 — **dry-run анализ**.

## 2. Цель фазы

1. **`db-pm deploy analyze`** — dry-run safety-gate: подключение к **существующей целевой БД**
   (первый real-target путь в проекте), построение дельты код↔БД, прогон gate, отчёт-рекомендация,
   **non-zero exit при нарушении**. Ничего к БД не применяется; pre-скрипты не выполняются —
   читаются статически.
2. **Pre-analysis (CD-6):** тронутые таблицы = CHANGED/REMOVED из hash-diff (ADDED/UNCHANGED
   безопасны). Гранулярность — табличная.
3. **Оценка данных (CD-7):** `estimated_rows` через метаданные (без `COUNT`); классификация
   HAS_DATA/EMPTY/UNKNOWN; **stale/unknown → HAS_DATA (fail-safe)**.
4. **Сопоставление с pre-скриптами (CD-8):** явное объявление `project.covers` в autodoc pre-скриптов.
5. **Gate + отчёт (CD-9):** непокрытая тронутая таблица с данными → нарушение → отчёт (markdown+JSON)
   → пайплайн останавливается (exit 1).
6. **Version-check (CD-2):** `target > source` → hard error (forward-only).
7. **Multi-DB:** без PG-хардкода в domain; `db_type` из manifest сверяется с типом подключения;
   DB-specific подсчёт строк/staleness — в адаптере; UNKNOWN-fallback безопасен по умолчанию.
8. **GUI-действие run-only** (5-е действие панели Phase 7, по образцу `deploy_validate`).

## 3. Принятые решения (все закрыты)

| ID | Развилка | Решение | Обоснование |
|----|----------|---------|-------------|
| **SG-0** | Объём: apply или dry-run | **`deploy analyze` (dry-run)**; apply → Phase 12 | Gate осмысленен только против БД с данными; dry-run тестируем без риска; apply строится в Phase 12 сразу с полным циклом (gate → pre → ALTER → apply) |
| **SG-A** | Гранулярность CD-6 | **Табличный уровень** (hash-diff); column-diff → Phase 12 (CD-ALT-1) | Для блокировки достаточно «тронута ∧ данные ∧ нет pre»; тип изменения нужен для генерации ALTER |
| **SG-1** | Имя команды | **`db-pm deploy analyze`**; опции `--dir`, `--target-connection-file`, `--output-dir` | Рядом с `deploy validate`; «analyze» точно передаёт dry-run |
| **SG-2** | Форматы отчёта | **`safety_gate_report.md` + `safety_gate_report.json`**; `diff_report.json` CompareService — сопутствующий артефакт в том же `output_dir` | md — для review, json — для CI; рендер по образцу Phase 9/14 `markdown_report.py` |
| **SG-3** | Механизм покрытия (CD-8) | **Явное объявление** `project.covers: ["schema.table", ...]` в autodoc pre-скрипта | Явность > магия (calver-in-manifest, `immutable`, CDF-10); детерминированно, аудируемо; gate не проверяет семантику — только факт объявления |
| **SG-4** | Оценка данных (CD-7) | **БД-агностичная классификация** по нормализованной модели `TablePresenceStats{estimated_rows, confidence}`: `>0`→HAS_DATA; `0`+FRESH→EMPTY; иначе→UNKNOWN→**трактовать как HAS_DATA** (fail-safe) | Domain без имён каталогов конкретной БД; непроверяемая пустота = данные |
| **SG-5** | Контракт адаптера | **Новый abstract** `get_table_presence_stats() -> list[TablePresenceStats]` (domain-модель, прецедент — Phase 10 `get_script_history -> ScriptRecord`). PG/Greenplum: `pg_class`+`pg_stat_user_tables`→`confidence`; будущие БД — свои системные представления, без сигнала → UNKNOWN (fail-safe). Существующий `get_table_row_counts` **не трогать** (потребитель — compare) | Нормализованный контракт реализуем любой БД; смена существующего метода ломает compare (§45) |
| **SG-6** | Version-check | `target > source` → **hard error** (forward-only, §7 п.8); `==` → warning («забыли bump»); `<` / `None` → proceed. Обе версии — в отчёт | Консистентно с Phase 10 CDF-2 |
| **SG-7** | GUI | **GUI-действие run-only** по образцу `deploy_validate` (5-е в реестре Phase 7): диалог (кодовая база, целевое подключение, `--output-dir`) → фоновый worker → verdict + ссылка на отчёт. Read-only — безопасно. Рендер отчёта в окне/вкладка Delta Viewer — Phase 14 | Интерактивность для DBA; консистентность с `deploy_validate`; богатый показ — отдельная задача |
| **SG-M** | Multi-DB-стратегия | Реестр `get_adapter(cfg)` — диспетч по типу; `manifest.db_type` сверяется с типом подключения (error при несовпадении, как compare); confidence — абстрактный сигнал адаптера | README декларирует Snowflake/MSSQL/MySQL в планах; контракт проектируется сразу под них |

## 4. Финальная архитектура

### 4.1. Поток `deploy analyze`

```
SafetyGateService.analyze(codebase_dir, target_cfg, output_dir, service_schema, on_progress):
  1. adapter = get_adapter(target_cfg); adapter.connect(target_cfg)   # реестр по cfg.type; НЕ temp
  2. manifest = read_manifest(codebase_dir)                            # db_type + source_version
     consistency: manifest.db_type != target_cfg.type → SafetyGateError (fail-fast)
  3. version-check (SG-6): get_schema_version(service_schema) vs source_version
  4. delta = CompareService.run(source=DIR(codebase_dir), target=DB(target_cfg), output_dir)
       # переиспользует RE live-БД во temp-каталог + snapshot + comparator
  5. touched = DiffReport.entries where status in {CHANGED, REMOVED} and object_type == "table"
       # object_type/имя — из доступного снапшота (REMOVED → target_snapshot)
  6. stats = adapter.get_table_presence_stats() → lookup {(schema, name): TablePresenceStats}
       # service исключает service_schema; классификация presence — в domain (SG-4)
  7. coverage = read_pre_coverage(codebase_dir / "__migrations")      # SG-3
  8. verdict: violation = touched ∧ presence in {HAS_DATA, UNKNOWN} ∧ not covered
  9. write safety_gate_report.md + .json to output_dir                 # SG-2
 10. return verdict (CLI: exit 0 clean / 1 violations / 2 hard error)
```

Pre/post runner (Phase 10) **не вызывается** — gate читает pre-скрипты только статически.

### 4.2. Domain-модели — `domain/safety.py` (новый; pydantic, по образцу `domain/diff.py`)

```python
class StatsConfidence(str, Enum):   # абстрактный сигнал свежести; заполняет адаптер
    FRESH   = "fresh"               # статистика достоверна
    STALE   = "stale"               # известна как устаревшая
    UNKNOWN = "unknown"             # адаптер не может оценить → fail-safe

class TablePresenceStats(BaseModel):  # нормализованный per-table сигнал от адаптера
    schema: str
    name: str
    estimated_rows: int | None
    confidence: StatsConfidence

class DataPresence(str, Enum):        # результат доменной классификации
    HAS_DATA = "has_data"
    EMPTY    = "empty"
    UNKNOWN  = "unknown"              # → трактуется как HAS_DATA (fail-safe)

class TableTouchKind(str, Enum):
    CHANGED  = "changed"              # есть и в коде, и в БД; sql_hash различается
    REMOVED  = "removed"              # есть в БД, нет в коде (будет DROP)

def classify_presence(stats: TablePresenceStats) -> DataPresence:
    # >0 → HAS_DATA; 0+FRESH → EMPTY; иначе (0+STALE / None / UNKNOWN / <0) → UNKNOWN

class TouchedTable(BaseModel):
    schema: str
    name: str
    touch: TableTouchKind
    estimated_rows: int | None
    confidence: StatsConfidence
    presence: DataPresence
    covered_by: list[str] = []
    # properties: covered (bool), is_violation (presence in {HAS_DATA, UNKNOWN} and not covered)

class VersionCheckOutcome(str, Enum):  # PROCEED / WARN_SAME / ERROR_NEWER
def check_version_relation(target: str | None, source: str) -> VersionCheckOutcome

class SafetyGateVerdict(BaseModel):
    clean: bool
    db_type: str
    source_version: str | None
    target_version: str | None
    touched: list[TouchedTable]
    # property violations -> list[TouchedTable]
```

Domain не содержит ни одного имени каталога конкретной БД (SG-M).

### 4.3. Coverage-парсер — `infrastructure/deploy/pre_coverage.py` (новый)

- `read_pre_coverage(migrations_dir: Path) -> dict[tuple[str, str], list[str]]` —
  `{(schema, table): [script_name, ...]}`.
- Читает `sorted((migrations_dir / "pre").glob("*.sql"))`; пустой/нет каталога → `{}` (не ошибка).
- Для каждого: `extract_header` (существующий, `infrastructure/sql/autodoc.py`) → секция
  `project.covers: list[str]`; каждая запись `"schema.table"` нормализуется к кортежу.
- Битая запись / не-список → warning + skip (урок §23: pre-скрипты содержат autodoc — парсить
  аккуратно; §36-подобный подход: протокол предупреждений в лог).

### 4.4. Адаптер — `get_table_presence_stats` (SG-5)

- `base.py`: новый abstract `get_table_presence_stats(self) -> list[TablePresenceStats]`.
- **PG/Greenplum** (`postgres/queries.py` `GET_TABLE_PRESENCE_STATS`): `pg_class c JOIN
  pg_namespace n LEFT JOIN pg_stat_user_tables s ON (schemaname, relname)`; `relkind='r'`;
  исключены `pg_catalog`/`information_schema`. Маппинг в адаптере:
  - `estimated_rows: None | <0` → `confidence=STALE` (сигнал «не анализировалась»);
  - never-analyzed (`last_analyze IS NULL AND last_autoanalyze IS NULL`) → `STALE`;
  - большой drift (`n_mod_since_analyze` ≥ `max(estimated_rows, 1)`) → `STALE`;
  - иначе `FRESH`.
  - **Отдельно валидировать на Greenplum** (распределённые таблицы).
- **Будущие адаптеры** (Snowflake/MSSQL/MySQL): свои системные представления; нет сигнала свежести
  → `UNKNOWN` (fail-safe). Контракт обязателен к реализации (урок §45).
- Системные схемы исключает адаптер (БД-specific); служебную `service_schema` исключает **gate**
  (application-слой, БД-агностично).
- Идентификаторы запроса — fully-qualified (`pg_catalog.`) (уроки §34/§35).

### 4.5. Отчёт — `infrastructure/deploy/safety_report.py` (новый)

`write_safety_report(verdict, output_dir) -> list[Path]`:
- `safety_gate_report.json` — полный `verdict.model_dump()` (CI).
- `safety_gate_report.md` — по образцу Phase 9/14: шапка (`db_type`, source/target version, дата),
  verdict, таблица тронутых таблиц (`schema.table | touch | ~rows | confidence | presence | covered_by`),
  секция нарушений с рекомендацией: «добавьте pre-скрипт в `__migrations/pre/` и объявите
  `project.covers: ["schema.table"]`».

### 4.6. CLI — `presentation/cli/main.py`

Подкоманда `deploy analyze` в группе `deploy` (typer-мультикоманда — уже настроена, урок §9).
Опции: `--dir` (обяз.), `--target-connection-file` (обяз.), `--output-dir` (обяз.).
Exit codes: `0` clean; `1` нарушения gate; `2` hard error (соединение/manifest/version/тип БД).
На `--help` — «read-only: ничего не применяется к целевой БД».

### 4.7. GUI-действие run-only (SG-7) — `presentation/gui/actions/`

5-е действие в реестре Phase 7 (`registry.py:142` `ACTIONS`), по образцу `deploy_validate`:
- `DeployAnalyzeSettings` (models.py): `codebase_dir`, `target_connection`, `output_dir`
  (урок §40 — без коллизий имён с BaseModel).
- `ActionSpec(action_id="deploy_analyze", ...)` + регистрация в панели.
- `DeployAnalyzeDialog` (dialogs.py): выбор кодовой базы / целевого подключения / output; кнопки
  последними (урок §43).
- CLI-билдер (cli.py): `db-pm deploy analyze --dir ... --target-connection-file ... --output-dir ...`
  (контракт-тест через CliRunner, урок §39 — argv без имени программы, shlex posix=False).
- Worker: фоновый `QRunnable`; strong-ref через `self._active_workers`, `finished` → bound-метод
  (урок §42); результат — verdict (clean / N нарушений) + пути отчётов; GUI предлагает открыть
  `safety_gate_report.md`.

### 4.8. Multi-DB-стратегия (SG-M)

- Диспетч адаптера — `get_adapter(cfg)` по `cfg.type` (существующий реестр); application/domain
  слои БД не знают.
- `manifest.db_type` (из `dbpm.manifest.json`) сверяется с типом целевого подключения до
  тяжёлой работы → `SafetyGateError` при несовпадении.
- Всё БД-specific (подсчёт строк, staleness, системные каталоги) — внутри адаптера; контракт —
  нормализованная domain-модель `TablePresenceStats`.
- Fail-safe default: адаптер без сигнала свежести → `UNKNOWN` → HAS_DATA. Незавершённый будущий
  адаптер не может «пропустить» таблицу с данными.

## 5. Проверки (для `_plan`)

- **Unit `domain/safety.py`:** roundtrip pydantic (§28); `classify_presence` — параметризованная
  матрица (HAS_DATA/EMPTY/UNKNOWN все ветки); `is_violation` только при HAS_DATA/UNKNOWN ∧ not
  covered; `check_version_relation` — 4 ветки (None/`==`/`>`/`<`).
- **Unit coverage-парсера:** валидный `covers` → множество; нет autodoc → пусто; битая запись →
  warning+skip; несколько скриптов → объединение; нет каталога → `{}`.
- **Unit PG-маппинга confidence** (чистая функция): never-analyzed/drift/`-1`/`NULL` → STALE;
  нормальный → FRESH.
- **Unit gate-логики** (fake adapter + synthetic DiffReport): touched(removed/changed) × presence ×
  coverage → вердикт; ADDED/UNCHANGED/не-табличные игнорируются; empty touched → не violation;
  `service_schema` исключена; `manifest.db_type != cfg.type` → error.
- **Unit отчёта:** clean → md без секции нарушений; violation → md содержит таблицу и рекомендацию;
  json roundtrip (§28).
- **Unit CLI:** корректный вызов; exit codes 0/1/2; dry-run — ни один мутирующий метод адаптера
  не вызван (контракт-тест).
- **Smoke GUI** (§41, offscreen): конструкторы действия/диалога; CLI-билдер строит корректную
  строку; worker-контракт §42 (execute → waitForDone → processEvents → панель разблокирована).
- **Регрессия** compare/deploy/RE/GUI тестов (§45: новый abstract → fakes в одном коммите).
- **Integration** (`@pytest.mark.integration`, testcontainers): (1) PG, таблица с данными, код
  меняет её, нет pre → FAIL + отчёт содержит таблицу; (2) + покрывающий pre (`covers`) → PASS;
  (3) пустая таблица меняется → PASS; (4) новая таблица/функция → PASS; (5) `target.version >
  source` → hard error. Greenplum-валидация распределённых таблиц — при появлении тестового кластера.

## 6. NOT done / отложено

- **Применение дельты к живой БД** (apply безопасных операций + выполнение pre-скриптов) — Phase 12.
- **Структурный column-diff** (тип изменения add/drop/alter) — Phase 12 (CD-ALT-1).
- **Повторный анализ после pre-скриптов (CD-11)** — Phase 12 (apply-пайплайн).
- **Рендер отчёта/нарушений в окне GUI** (вкладка Delta Viewer) — Phase 14.
- **AI-объяснение отчёта (CD-AI-2)** — overlay после Phase 12.
- **CLI `--reseed-deploy`**, жёсткая canonical-DDL-валидация — backlog (из Phase 10).

## 7. Чеклист по урокам

- [ ] §3: данные через `reltuples` (метаданные), не `COUNT` — gate не делает full scan.
- [ ] §12: `git add -- "-=docs=-/-=tasks=-/phase_11/..."` (дефис в `-=docs=-`).
- [ ] §18/§45: новый abstract `get_table_presence_stats` → в одном коммите: `PGDatabaseAdapter` +
  ВСЕ fakes; перед стартом `grep -rn "(DatabaseAdapter)"`.
- [ ] §19: PG-query — исключение системных схем по whitelist.
- [ ] §23: pre-скрипты содержат autodoc → корректный parse при извлечении `covers`.
- [ ] §28: roundtrip через pydantic, не substring-матчинг.
- [ ] §34/§35: идентификаторы в `GET_TABLE_PRESENCE_STATS` — fully-qualified; контракт тестом.
- [ ] §39: GUI→CLI контракт через CliRunner: argv без имени программы, `shlex.split(posix=False)`.
- [ ] §40: имена полей `DeployAnalyzeSettings` без коллизий с BaseModel.
- [ ] §41: offscreen smoke GUI.
- [ ] §42: worker — strong-ref через `_active_workers`; `finished` к bound-методу, НЕ лямбда.
- [ ] §43: кнопки диалога добавлять последними.
- [ ] **Multi-DB:** domain `safety.py` без имён каталогов конкретных БД; `db_type` manifest
  сверяется с подключением.
- [ ] TASK_CONVENTIONS §6: код и документы — в разных коммитах.

## 8. Где читать дальше

- `-=tasks=-/phase_11/Phase_11_vision_draft.md` — предшествующий драфт (история обсуждения)
- `-=tasks=-/ROADMAP.md` §2 (шаг 3), §4 (CD-6..CD-10), §7 (правила), §8 (пайплайн шаги 2-3)
- `-=tasks=-/phase_10/Phase_10_vision_final.md` — фундамент (versioning, `__deploy`, runner)
- `-=PHASES=-/Phase_09.md` — compare (движок дельты для CD-6)
- `src/db_project_manager/application/compare_service.py` — переиспользуемый движок
- `LESSONS_LEARNED.md` §3, §18/§45, §19, §23, §28, §34/§35, §39, §40, §41, §42, §43
