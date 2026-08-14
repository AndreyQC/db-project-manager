# Phase 11: Safety Gate — драфт дизайна (vision draft)

> **Дата:** 2026-08-14
> **Ветка:** dev
> **Статус:** draft (есть открытые `USER_INPUT` SG-1..SG-6; после закрытия → `_final`)
>
> Контекст:
> - `-=CHECKPOINTS=-/20260814_001_checkpoint.md` — текущее состояние (Phase 10 done)
> - `-=tasks=-/ROADMAP.md` §2 (шаг 3), §3 (safety principle), §4 (CD-6..CD-10), §7 (правила), §8 (пайплайн)
> - `-=tasks=-/phase_10/Phase_10_vision_final.md` — нормативный дизайн Phase 10 (образец + фундамент)
> - `-=PHASES=-/Phase_09.md` — compare (fundament CD-6)
> - `src/db_project_manager/application/{compare_service,deploy_service,script_runner}.py`
> - `src/db_project_manager/infrastructure/diff/{comparator,snapshot}.py`
> - `src/db_project_manager/infrastructure/database/{base,postgres/adapter,postgres/queries}.py`
> - `LESSONS_LEARNED.md` §3, §12, §18/§45, §19, §23, §28, §34/§35

---

## 1. Постановка проблемы

Phase 10 заложила **механику** CD-ядра: служебная схема `__deploy`, calver-версионирование,
идемпотентный pre/post runner — но вся она крутится в **validate-flow** (пустая temp-БД:
`deploy_service.py:175-193`, `deploy_source="validate"`). В кодовой базе **нет пути деплоя в
существующую БД с данными** — а safety-gate (CD-6..CD-10) осмысленен **только против такой БД**:
против пустой temp-БД каждая таблица = ADDED (greenfield), данных нет, ловить нечего.

Кроме того, diff-слой сегодня **hash-level**: `DiffStatus` = added/removed/changed/unchanged,
«changed» таблица непрозрачна (`comparator.py:66` решает по `sql_hash`). Column-структура
(`adapter.py:471-509` `_build_table`, `queries.py:52-73` `GET_COLUMNS`) при reverse-engineer
**считывается, но выбрасывается** после генерации DDL — до diff-слоя не доходит; доменной модели
`Column` нет. Структурный column-diff (тип изменения add/drop/alter) по ROADMAP — это явно
**Phase 12 (CD-ALT-1)**.

**Ключевой инсайт для gating:** правило CD-10 — «любое изменение таблицы с данными без
покрывающего pre-скрипта = блок» — **не требует** знания точного типа изменения. Чтобы заблокировать,
достаточно знать «таблица тронута (changed/removed) И есть данные И нет pre-скрипта». Поэтому
Phase 11 может быть построена **поверх существующего hash-diff**, без выноса column-diff вперёд.

## 2. Цель фазы

1. **Новый пользовательский путь — `db-pm deploy analyze`** (dry-run): подключение к
   **существующей целевой БД** (не temp-БД), построение дельты код↔БД, прогон safety-gate,
   выдача отчёта-рекомендации, **non-zero exit при нарушении**. **Ничего к БД не применяется.**
   (Применение безопасных операций + pre-скриптов к живой БД — Phase 12.)
2. **Pre-analysis (CD-6):** из дельты выделить тронутые таблицы (CHANGED/REMOVED; ADDED/UNCHANGED
   безопасны). Гранулярность — табличная (тип изменения уровня колонок → Phase 12).
3. **Оценка данных (CD-7):** для каждой тронутой таблицы — приблизительное число строк через
   метаданные (`pg_class.reltuples`, без `COUNT`); классификация «есть данные» / «пусто»;
   **stale-статистика → «есть данные» (fail-safe)**.
4. **Сопоставление с pre-скриптами (CD-8):** статически определить, какие тронутые таблицы с
   данными «покрыты» pre-скриптом из `__migrations/pre/`.
5. **Gate + отчёт (CD-9):** непокрытая тронутая таблица с данными → нарушение → отчёт-рекомендация
   (markdown + JSON) с конкретным списком и рекомендацией «добавить pre-скрипт для schema.table».
6. **Forward-only version-check (CD-2, перенос из Phase 10):** `target.version > source.version`
   → hard error (как в validate-flow, но против реальной версии целевой БД).
7. **Multi-DB-готовность:** gate НЕ хардкодит PostgreSQL. Диспетч по типу БД — через реестр
   `get_adapter(cfg)` (application/domain слои БД-агностичны); `db_type` из manifest сверяется с типом
   целевого подключения (по образцу `compare_service`); DB-specific знание (как считать строки и
   staleness) инкапсулировано в адаптере, domain работает с нормализованной моделью (см. SG-4/SG-5).
   Fail-safe default: адаптер, который не умеет оценить staleness → `UNKNOWN` → трактется как HAS_DATA.

## 3. Принятые решения (закрыты пользователем до драфта)

| ID | Развилка | Решение | Обоснование |
|----|----------|---------|-------------|
| **SG-0** | Объём фазы: apply или dry-run | **`deploy analyze` (dry-run)** | Gate осмысленен только против БД с данными; apply ALTER — Phase 12. Dry-run тестируем без риска и переиспользует Phase 12 для полного apply. |
| **SG-A** | Гранулярность CD-6: column-diff сейчас или табличный уровень | **Табличный уровень** (hash-diff); column-diff → Phase 12 | Для блокировки достаточно «тронута ∧ есть данные ∧ нет pre»; точный тип изменения нужен для генерации ALTER (Phase 12). Чистая граница фаз. |

## 4. Открытые вопросы (`USER_INPUT`) — с рекомендацией ИИ

> Закрывает пользователь. После закрытия переносятся в §3 как «принятые» → `_final`.

### SG-1. Имя команды и размещение
**Рекомендация:** подкоманда `db-pm deploy analyze` (рядом с `deploy validate`).
Опции: `--dir <codebase>`, `--target-connection-file <conn.yaml>` (существующая целевая БД),
`--output-dir <report_dir>`. Альтернатива `deploy plan`/`deploy check` — но `analyze` точнее
передаёт dry-run. **Согласовано по SG-0.**

### SG-2. Форматы отчёта
**Рекомендация:** **два** — `safety_gate_report.md` (человекочитаемый, для review) и
`safety_gate_report.json` (для CI: verdict + violations машиночитаемо). Плюс переиспользовать
`diff_report.json` от CompareService как сопутствующий артефакт. Markdown-генерация — по образцу
Phase 9/14 `markdown_report.py`.

### SG-3. Механизм «покрытия» таблицы pre-скриптом (CD-8)
**Рекомендация (a) — явное объявление.** Pre-скрипт в autodoc-секции (`project`) объявляет список
покрываемых таблиц: `covers: ["schema.table", ...]`. Gate читает autodoc всех `__migrations/pre/*.sql`
→ множество покрытых `(schema, table)`.
- Соответствует философии проекта «явность > магия» (calver-in-manifest, `immutable`, CDF-10).
- Детерминированно, легко тестировать, аудируемо.
- Gate **не проверяет семантику** покрытия (это claim автора) — только факт объявления.
Альтернативы: (b) парсить SQL pre-скрипта на ссылки таблиц (хрупко, regex-on-SQL, урок §36);
(c) convention по имени файла (ломко). **→ рекомендую (a).**

### SG-4. Оценка данных (CD-7, fail-safe) — БД-агностичная классификация
**Рекомендация:** domain классифицирует presence **только по нормализованной модели**
`TablePresenceStats{ estimated_rows, confidence }` — без единого имени каталога конкретной БД.
Правило (fail-safe = «если не уверены, что пусто — считаем, что есть данные»):
- `estimated_rows > 0` → `HAS_DATA`;
- `estimated_rows == 0` **И** `confidence == FRESH` → `EMPTY`;
- иначе (`0`+`STALE`, `None`, или `confidence == UNKNOWN`) → `UNKNOWN` → **трактовать как HAS_DATA**.

`confidence: FRESH | STALE | UNKNOWN` — это **абстрактный** сигнал; КАЖДЫЙ адаптер вычисляет его
по своим источникам (см. SG-5). Если БД не умеет оценить staleness → `UNKNOWN` → автоматически
fail-safe. Служебную схему `__deploy` (configurable `service_schema`) исключает **слой gate**
(application, БД-агностично), а системные каталоги (`pg_catalog`/`information_schema` и аналоги) —
сам адаптер.

### SG-5. Контракт адаптера — нормализованная модель (multi-DB)
**Рекомендация:** новый abstract `get_table_presence_stats() -> list[TablePresenceStats]`,
возвращающий **domain-модель** (прецедент: Phase 10 `get_script_history -> ScriptRecord`). Каждый
адаптер маппит свои БД-specific метаданные на неё:
- **PG / Greenplum** (`PGDatabaseAdapter`): `pg_class.reltuples` + `pg_stat_user_tables`
  (`last_analyze`/`last_autoanalyze`/`n_mod_since_analyze`) → `confidence` (STALE при never-analyzed
  или большом drift). **Внимание Greenplum:** row-estimates распределённых таблиц могут отличаться —
  валидировать имплементацию на Greenplum отдельно.
- **Snowflake / MSSQL / MySQL** (будущие адаптеры): маппят свои `information_schema`/system views
  (напр. MSSQL `sys.partitions.rows`, MySQL `information_schema.tables.table_rows`); если БД не
  даёт сигнала о свежести → `confidence=UNKNOWN` → fail-safe. Никакого PG-хардкода в domain.

Существующий `get_table_row_counts()` **не трогать** — его потребляет `compare_service`
(`compare_service.py:200`). Урок §18/§45: новый abstract → в одном коммите реализовать в
`PGDatabaseAdapter` + ВСЕ fakes (`grep -rn "(DatabaseAdapter)"`). Для будущих адаптеров контракт
спроектирован реализуемым любой БД (UNKNOWN fallback = безопасно).

### SG-6. Жёсткость version-check (CD-2) в analyze
**Рекомендация:** `target.version > source.version` → **hard error** (forward-only, ROADMAP §7 п.8,
консистентно с Phase 10 CDF-2); `target == source` → warning («забыли bump»); `target < source` /
`target is None` → proceed. Включать текущую и исходную версию в отчёт.

### SG-7. GUI-действие (панель действий Phase 7) — ЗАКРЫТО: вариант (b) run-only
**Решение (выбрано пользователем):** добавить GUI-действие **run-only** по образцу `deploy_validate`
(5-е действие в реестре Phase 7): диалог (кодовая база, целевое подключение, `--output-dir`) →
фоновый worker → verdict (clean / N нарушений) + ссылка на файлы отчёта. Read-only — безопасно для
GUI. Рендер самого markdown/списка нарушений в окне (или вкладка Delta Viewer) — отложено (Phase 14).
Уроки: §41 (offscreen smoke), §42 (worker strong-refs — как deploy_validate), §43 (кнопки диалога
последними), §40 (без коллизий имён полей pydantic).

## 5. Предлагаемая архитектура

### 5.1. Поток `deploy analyze`

```
SafetyGateService.analyze(codebase_dir, target_cfg, output_dir, service_schema):
  1. adapter = get_adapter(target_cfg); adapter.connect(target_cfg)   # реестр по cfg.type; НЕ temp
  2. read manifest(codebase_dir): db_type + source_version
       db_type consistency: manifest.db_type совместим с target_cfg.type (иначе error, как compare)
  3. version-check (SG-6): get_schema_version(service_schema) vs source_version
  4. build delta: CompareService.run(source=DIR(codebase_dir), target=DB(target_cfg))
       → DiffReport (+ RE live-БД во temp-каталог + row_counts)
  5. extract touched tables: DiffEntry where status in {CHANGED, REMOVED} and object_type == "table"
  6. presence stats: adapter.get_table_presence_stats() → lookup {(schema, name): TablePresenceStats}  # SG-5
       исключить service_schema (application-слой); classification HAS_DATA/EMPTY/UNKNOWN — в domain   # SG-4
  7. coverage: parse autodoc `covers` из __migrations/pre/*.sql → set            # SG-3
  8. build verdict: violation = touched ∧ presence in {HAS_DATA, UNKNOWN} ∧ not covered
  9. write safety_gate_report.md + .json to output_dir                            # SG-2
 10. return SafetyGateVerdict (caller: CLI ставит exit code 0/1)
```

**Важно:** pre/post runner (Phase 10) **НЕ вызывается** против реальной БД — gate читает pre-скрипты
только статически (для покрытия). Выполнение pre-скриптов на живой БД — Phase 12.

### 5.2. Новые domain-модели — `domain/safety.py` (pydantic, по образцу `domain/diff.py`)

```python
class StatsConfidence(str, Enum):   # абстрактный сигнал свежести (адаптер заполняет)
    FRESH   = "fresh"   # статистика достоверна
    STALE   = "stale"   # известна как устаревшая
    UNKNOWN = "unknown" # адаптер не может оценить → fail-safe

class TablePresenceStats:           # нормализованный per-table сигнал (возвращает адаптер)
    schema: str
    name: str
    estimated_rows: int | None
    confidence: StatsConfidence

class DataPresence(str, Enum):      # результат доменной классификации
    HAS_DATA = "has_data"
    EMPTY    = "empty"
    UNKNOWN  = "unknown"   # → трактуется как HAS_DATA (fail-safe)

class TableTouchKind(str, Enum):
    CHANGED  = "changed"   # есть и в коде, и в БД; sql_hash различается
    REMOVED  = "removed"   # есть в БД, нет в коде (будет DROP)

class TouchedTable:           # одна запись анализа
    schema: str
    name: str
    touch: TableTouchKind
    estimated_rows: int | None
    confidence: StatsConfidence   # проброшено из stats для отчёта
    presence: DataPresence        # вычисляется из (estimated_rows, confidence)
    covered_by: list[str]         # имена pre-скриптов, объявивших покрытие
    @property
    def covered(self) -> bool: ...
    @property
    def is_violation(self) -> bool:   # presence in {HAS_DATA, UNKNOWN} and not covered
        ...

class SafetyGateVerdict:
    clean: bool               # нарушений нет
    db_type: str              # из manifest (для отчёта/CI)
    source_version: str | None
    target_version: str | None
    touched: list[TouchedTable]
    @property
    def violations(self) -> list[TouchedTable]: ...
```

Классификация `presence` (fail-safe) — **чистая функция от `TablePresenceStats` → `DataPresence`**,
БД-агностична: `>0`→HAS_DATA; `0`+FRESH→EMPTY; иначе→UNKNOWN. Покрыта табличными unit-тестами на
каждый кейс. Адаптер **не** классифицирует presence — он только нормализует `confidence`.

### 5.3. Сопоставление покрытия — `infrastructure/...` (новый модуль)
Читать `__migrations/pre/*.sql`, доставать autodoc (урок §23: pre-скрипты могут содержать autodoc),
извлекать `project.covers: list[str]` → нормализовать к `(schema, name)`. Нарушение формата
`covers` → warning + skip. Модуль детерминирован, покрыт unit-тестами на фикстурах.

### 5.4. Адаптер (SG-5) — нормализованный контракт, multi-DB
- Новый abstract `get_table_presence_stats() -> list[TablePresenceStats]` в `base.py` (возвращает
  **domain-модель**, по образцу Phase 10 `get_script_history -> ScriptRecord`).
- **PG/Greenplum** (`postgres/adapter.py` + `queries.py` `GET_TABLE_PRESENCE_STATS`): `pg_class`
  `JOIN pg_stat_user_tables` на `(schemaname, relname)`; `confidence = STALE` при never-analyzed
  (`last_analyze IS NULL AND last_autoanalyze IS NULL`) или большом `n_mod_since_analyze`, иначе
  `FRESH`. Исключает `pg_catalog`/`information_schema`. **Отдельно валидировать на Greenplum**
  (распределённые таблицы).
- **Будущие адаптеры** (Snowflake/MSSQL/MySQL): маппят свои системные представления на ту же модель;
  если сигнал свежести недоступен → `confidence=UNKNOWN` (fail-safe).
- Системные схемы исключает адаптер (БД-specific); служебную `service_schema` исключает **gate**
  (application, БД-агностично). Уроки §34/§35: идентификаторы в PG-запросе — fully-qualified.

### 5.5. CLI — `presentation/cli/main.py`
Новая подкоманда `deploy analyze` в группе `deploy` (typer-подгруппа, урок §9/§15). Опции (SG-1).
Exit code: 0 — clean; 1 — нарушение gate; 2 — usage/hard-error (version/соединение). На `--help`
явно указать «read-only, ничего не применяется».

### 5.6. GUI-действие run-only (SG-7) — `presentation/gui/actions/`
5-е действие в реестре Phase 7, по образцу `deploy_validate`:
- `DeployAnalyzeSettings` (model): `codebase_dir`, `target_connection`, `output_dir` (урок §40 —
  имена полей без коллизий с BaseModel).
- `ActionSpec(action_id="deploy_analyze", ...)` в `ACTIONS` (`registry.py:142`).
- Диалог (`dialogs.py`): выбор кодовой базы, целевого подключения (existing DB), `output_dir`; кнопки
  последними (урок §43).
- CLI-билдер (`cli.py`): строит `db-pm deploy analyze --dir ... --target-connection-file ...
  --output-dir ...`.
- Worker: фоновый `QRunnable` (урок §42 — strong-ref через `self._active_workers`, не лямбда в
  `connect`); read-only, ничего не мутирует; результат = verdict (clean / N нарушений) + пути отчётов.
- GUI показывает verdict и предлагает открыть `safety_gate_report.md`.

### 5.7. Reuse / не-дублирование
- **CompareService** — источник дельты и row_counts (не писать свой diff).
- **manifest** (`source_version`), `__deploy`-механика versioning — из Phase 10.
- **Markdown-рендер** — по образцу Phase 9/14.
- Структурный column-diff намеренно **не трогаем** — Phase 12.

## 6. Проверки (для `_plan`)

- **Unit `domain/safety.py`:** roundtrip pydantic (урок §28); `DataPresence`-классификация — чистая
  функция от `TablePresenceStats` (БД-агностична): HAS_DATA (>0), EMPTY (0+FRESH), UNKNOWN
  (0+STALE / None / confidence=UNKNOWN); `is_violation` истина только при HAS_DATA/UNKNOWN ∧ not
  covered; `covered` по covered_by.
- **Unit coverage-парсера:** валидный `covers` → множество; пустой/без autodoc → пусто; битый
  `covers` → warning+skip; несколько скриптов → объединение.
- **Unit gate-логики на синтетическом DiffReport:** touched(removed/changed) × presence × coverage
  → вердикт; ADDED/UNCHANGED/не-табличные игнорируются; empty touched → не violation.
- **Unit version-check (SG-6):** 4 ветки (None/`==`/`>`/`<`).
- **Unit multi-DB (контракт):** minimal fake-adapter возвращает `confidence=UNKNOWN` для всех таблиц
  → domain трактует как HAS_DATA (fail-safe); `manifest.db_type != target_cfg.type` → error.
- **Unit PG-адаптер-query** (`@pytest.mark.integration` или mock catalog): `confidence` корректно
  STALE при never-analyzed/drift; `pg_catalog`/`information_schema` исключены.
- **Unit CLI:** `deploy analyze` строит корректный вызов; exit codes 0/1/2; dry-run не вызывает
  create/execute (контракт-тест: ни один мутирующий метод адаптера не вызван).
- **Smoke GUI** (урок §41, `QT_QPA_PLATFORM=offscreen`): конструктор действия/диалога строится без
  дисплея; CLI-билдер строит корректную строку; worker-контракт (урок §42): после execute → waitForDone
  → processEvents панель разблокирована (по образцу `test_action_panel_smoke.py`).
- **Регрессия** compare/deploy/RE тестов (урок §45: новый abstract method → fakes в одном коммите).
- **Integration** (`@pytest.mark.integration`, testcontainers): (1) реальная PG, таблица с данными,
  код меняет её, нет pre → gate FAIL + отчёт содержит таблицу; (2) тот же сценарий + покрывающий
  pre-скрипт (`covers`) → gate PASS; (3) пустая таблица меняется → PASS; (4) новая таблица/функция
  → PASS; (5) target.version > source → hard error. (Greenplum-валидация распределённых таблиц —
  при появлении тестового кластера.)

## 7. NOT done / отложено

- **Применение дельты к живой БД** (apply безопасных операций + pre-скриптов) — Phase 12.
- **Структурный column-diff** (точный тип изменения add/drop/alter) — Phase 12 (CD-ALT-1).
- **Повторный анализ после pre-скриптов (CD-11)** — Phase 12 (apply-пайплайн).
- **AI-объяснение отчёта (CD-AI-2)** — overlay после Phase 12.
- **Рендер отчёта/нарушений `deploy analyze` в окне GUI** (или вкладка Delta Viewer) — Phase 14;
  run-only GUI-действие входит в Phase 11 (SG-7).
- **CLI `--reseed-deploy`**, жёсткая canonical-DDL-валидация — backlog (из Phase 10).

## 8. Чеклист по урокам

- [ ] §3: данные через `reltuples` (метаданные), не `COUNT` — gate не делает full scan.
- [ ] §12: `git add -- "-=docs=-/-=tasks=-/phase_11/..."` (дефис в `-=docs=-`).
- [ ] §18/§45: новый abstract `get_table_presence_stats` → в одном коммите: `PGDatabaseAdapter` +
  ВСЕ fakes; перед стартом `grep -rn "(DatabaseAdapter)"`. Контракт спроектирован реализуемым любой
  БД (UNKNOWN fallback); будущие Snowflake/MSSQL/MySQL-адаптеры обязаны его реализовать.
- [ ] §19: PG-query идентификаторов — исключение системных схем по whitelist.
- [ ] §23: pre-скрипты содержат autodoc → strip/parse корректно при извлечении `covers`.
- [ ] §28: roundtrip через pydantic, не substring-матчинг (для `version`/`estimated_rows`).
- [ ] §34/§35: идентификаторы в `GET_TABLE_PRESENCE_STATS` — fully-qualified; контракт тестом.
- [ ] **Multi-DB:** domain `safety.py` не содержит ни одного имени каталога конкретной БД;
  `confidence` — абстрактный сигнал адаптера; `db_type` из manifest сверяется с типом подключения.
- [ ] **GUI:** §40 (имена полей `DeployAnalyzeSettings` без коллизий с BaseModel); §41 (offscreen
  smoke); §42 (worker — strong-ref через `_active_workers`, `finished` к bound-методу, НЕ лямбда);
  §43 (кнопки диалога добавлять последними).
- [ ] TASK_CONVENTIONS §6: код и документы — в разных коммитах; vision_draft — отдельный коммит
  `docs(tasks): phase_11 vision draft`.

## 9. Где читать дальше

- `-=tasks=-/ROADMAP.md` §2 (шаг 3), §4 (CD-6..CD-10), §7 (правила), §8 (пайплайн шаги 2-3)
- `-=tasks=-/phase_10/Phase_10_vision_final.md` — фундамент (versioning, `__deploy`, runner)
- `-=PHASES=-/Phase_09.md` — compare (CD-6 substrate)
- `src/db_project_manager/application/compare_service.py` — переиспользуемый движок дельты
- `src/db_project_manager/infrastructure/diff/comparator.py` — hash-diff (граница Phase 12)
- `LESSONS_LEARNED.md` §3, §18/§45, §19, §23, §28, §34/§35
