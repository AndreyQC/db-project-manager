# Roadmap — следующие фазы db-project-manager

> **Назначение:** высокоуровневый документ — *какие фазы дальше, в каком порядке, с
> какими зависимостями*. Хранит последовательность и идеи, чтобы не потерять их между
> сессиями. Дополняет `_tasks_/BACKLOG.md`:
>
> | Документ | Гранулярность | Что хранит |
> |----------|---------------|------------|
> | **ROADMAP.md** (этот) | Фазы | Последовательность, зависимости, high-level vision по направлениям |
> | **BACKLOG.md** | Отдельные задачи | Конкретные P1/P2/P3-задачи с контекстом и триггерами |
>
> **Статус:** living-документ (обновляется на месте, как BACKLOG). Подробный дизайн
> конкретной фазы выносится в `phase_NN/Phase_NN_vision_draft.md` → `_final` → `_plan`
> при старте этой фазы. Здесь — порядок и смысл, не детали реализации.
>
> **Дата заведения:** 2026-07-30
> **Источник:** исходный набросок был в `_phases_/Future_phase.md` (удалён — нарушал
> `PHASES_CONVENTION.md`), промежуточная версия — `phase_10/Phase_10_vision_draft.md`.
>
> Контекст:
> - `_checkpoints_/20260731_001_checkpoint.md` — текущее состояние (Phase 8 done)
> - `_tasks_/BACKLOG.md` — P1 overload resolution (✅ закрыт), P2 edge diff + inference-фоллоуапы, P3 compare-мелочи
> - `LESSONS_LEARNED.md` §35 (fully-qualified DDL-контракт), §36 (qualify-refs)

---

## 1. Что уже построено (фундамент)

Эпики ниже не строятся с нуля — большая часть инфраструктуры готова:

| Возможность | Где | Phase |
|-------------|-----|-------|
| reverse-engineer: БД → дерево SQL + autodoc | `application/reverse_engineer.py` | 1 |
| Граф зависимостей + validation deploy (пустая temp-БД) | `application/deploy_service.py`, `domain/graph.py` | 2 |
| SSH-туннель | `infrastructure/database/ssh/` | 3 |
| Идентификация перегрузок (`object_signature`) | `domain/signature.py` | 4 |
| Extensions + настройки БД | `queries.py`, генератор | 5 |
| Qualify-refs пост-процессор (regex) | `application/qualify_refs_service.py` | 6 |
| GUI панель действий (4 действия) | `presentation/gui/actions/` | 7 |
| Compare: source/target/diff JSON | `application/compare_service.py`, `domain/diff.py` | 9 |
| Overload resolution в edge detection (литералы MVP) | `infrastructure/parsing/overload_resolution.py`, `pg_sql_parser.py` | 8 |

**Готовность инфраструктуры для CD:** ~70-75%. Достраивать — служебную схему,
версионирование, pre/post runner, структурный column-diff, генерацию ALTER-плана.
(Overload resolution Phase 8 — частичный; колонки и рекурсия вызовов как аргументы
отложены в BACKLOG P2.)

---

## 2. Карта фаз — порядок и зависимости

> Номера предварительные (сквозная нумерация проекта). Завершённые фазы: **Phase 9** и
> **Phase 8** (по дате Phase 9 закрыта раньше Phase 8, но обе сделаны).
> **Порядок важнее номеров** — он зафиксирован колонкой «Шаг».

| Шаг | Фаза | Направление | Зависит от | Статус |
|-----|------|-------------|------------|--------|
| 1 | **Phase 8** — overload resolution | Граф | — | ✅ done (коммиты `d4b52e2`…`5587418`) |
| 2 | **Phase 10** — CD Foundation | CD | Phase 8 (✓) | ✅ done (коммиты `3d5694c`…`15002ad`) |
| 3 | **Phase 11** — Safety Gate | CD | Phase 10 (✓) | ✅ done (коммиты `35fc2e7`…`1b20480`) |
| 4 | **Phase 12** — ALTER + Delta | CD | Phase 11 (✓) | ✅ done (коммиты `d76d804`…`f55b5a7`) |
| 5 | **Phase 13** — GP↔PG YAML Pipeline | CD | Phase 12 | ✅ done (`a5597c4`) |
| 6 | **Phase 14** — Delta Viewer | DV | Phase 9 (✓) | ✅ done (коммиты `3649b0c`…`141e9fc`) |
| 7 | **Phase 15** — GUI deploy plan/apply + Plan Viewer | CD | Phase 12, 13 | ✅ done |
| 8 | **Phase 16** — Greenplum tuning (живой кластер GP 6.19, ядро PG 9.4) | CD | Phase 15 (✓) | ✓ завершена — `_phases_/Phase_16.md` |
| 9 | **Phase 18** — deploy reset (сброс пользовательских схем) | CD | Phase 12, 16 | ✅ done 2026-09-17 — `_tasks_/2026-09-17/20260917_001_deploy_reset_final.md` |
| 10 | **Phase 17** — Post-deploy, отчёты, полировка (CD-16..19) | CD | Phase 16 | не начата |
| 11 | **Phase 19** — MCP Server (LLM-доступ к БД) | MCP | Phase 10–13, 18 | ✅ done 2026-09-27 (+19.1 логирование запросов) — `_tasks_/2026-09-27/20260927_001_mcp_server_result.md` |
| 12 | **Phase 20** — SQL-профайлер таблиц (PG + GP) | Profiling | Phase 10–13 (✓), 19 (✓) | в работе — план `_tasks_/2026-10-06/20261006_002_phase20_sql_profiler_plan.md` |
| — | **AI track** (overlay) | AI | Phase 12 | не начата, опциональная надстройка |

**Логика порядка:** Phase 8 чинит граф (топосорт деплоя) — **закрыта**, развязка для Phase 10
получена. Затем CD-ядро (10→13) по пайплайну: фундамент → пред-анализ → генерация дельты →
пост-обработка. Delta Viewer опирается только на готовую Phase 9 — может идти параллельно.
AI-трек надстраивается над Phase 12 (нужен структурный column-diff).

---

## 3. Постановка проблемы (контекст CD)

Сегодня validation deploy проверяет схему «в вакууме» (пустая temp-БД), но не решает,
*как* применить дельту к БД с данными. Существующие инструменты (Alembic/Liquibase)
слишком доверяют авто-миграциям; SSDT слишком магический и привязан к SQL Server.
Нужен **controlled deployment** с жёстким safety-gate.

**Принцип безопасности (зафиксирован):** *лучше потерять день, чем данные.*
В автоматической дельте нет флага «разрешить потери данных».

---

## 4. Направление B — Controlled Deployment (детально)

### Phase 10 — CD Foundation: служебная схема, версионирование, pre/post runner

| ID | User Story | Acceptance Criteria | Priority |
|----|------------|---------------------|----------|
| **CD-1** | Автосоздание служебной схемы (`__deploy`) с таблицами версионирования. | • `schema_version` (version, git_tag, git_commit, applied_at, source)<br>• `script_history` (script_name, script_type, checksum, executed_at, success, error_message, duration)<br>• `script_watermark` (опционально) | Must |
| **CD-2** | Определение текущей версии БД и сравнение с источником (git-тег/commit). | • Запрет деплоя, если target новее source<br>• Предупреждение при большом отставании<br>• Чтение текущей версии | Must |
| **CD-3** | Хранение pre/post-скриптов в `migrations/{pre,post}` с сортируемыми именами. | • Соглашение `YYYY-MM-DD_NNN_description.sql`<br>• Скрипты версионируются в git | Must |
| **CD-4** | Идемпотентное выполнение pre/post-скриптов с полной историей. | • То же имя + тот же checksum + success → пропуск<br>• То же имя, другой checksum → ошибка<br>• Запись в `script_history` | Must |
| **CD-5** | Документированное требование идемпотентности pre/post-скриптов. | • Требование зафиксировано<br>• Примеры типичных идемпотентных конструкций | Should |

### Phase 11 — Safety Gate: pre-analysis, оценка данных, отчёт-рекомендация

| ID | User Story | Acceptance Criteria | Priority |
|----|------------|---------------------|----------|
| **CD-6** | До pre-скриптов построить предварительную дельту (код ↔ БД) и выявить таблицы к изменению. | • Список таблиц с типом изменения (add column, drop column, alter type, rebuild) | Must |
| **CD-7** | Для каждой такой таблицы — приблизительное количество строк через метаданные (без COUNT). | • Использование `get_table_row_counts`<br>• Классификация «есть данные» / «пустая»<br>• **Stale-статистика → считать «есть данные» (fail-safe)** | Must |
| **CD-8** | Сопоставить таблицы с данными с pre-скриптами в артефакте. | • Таблица с данными + нет покрывающего pre → нарушение | Must |
| **CD-9** | При нарушении safety-gate — упасть и сформировать отчёт-рекомендацию. | • Список проблемных таблиц + тип изменения<br>• Рекомендация: какие pre-скрипты добавить<br>• Пайплайн останавливается | Must |
| **CD-10** | Жёсткое правило: для таблиц с данными авто-операции запрещены. | • Нет флага «разрешить потери данных»<br>• Разрешено: новые объекты, изменения/пересоздание пустых таблиц, любые изменения не-табличных объектов | Must |

### Phase 12 — ALTER + Delta: структурный column-diff, ALTER-план, применение

> **Prerequisite для ВСЕХ вариантов генерации ALTER** (и детерминированной, и AI):
> comparator должен научиться структурному diff уровня колонок (added/dropped columns,
> changed type/nullable/default). Сейчас он сравнивает хэши целых DDL
> (`infrastructure/diff/comparator.py:65-80`), а структура колонок уже есть в
> `PGDatabaseAdapter._build_table()` (`postgres/adapter.py:353-391`, `GET_COLUMNS`).
> Это новая работа — вернуть структуру в diff-слой.

| ID | User Story | Acceptance Criteria | Priority |
|----|------------|---------------------|----------|
| **CD-ALT-1** | Структурный column-level diff. | • DiffEntry несёт changed-columns (add/drop/type-change/nullable/default), не только SQL-хэш<br>• Покрыт unit-тестами на синтетических парах «было/стало» | Must |
| **CD-ALT-2** | Генерация ALTER-плана: add/drop column, alter type, rebuild. | • Отдельная стратегия на каждый тип изменения<br>• Каждая операция классифицируется: safe / needs-pre-script / blocked<br>• Покрыта тестами | Must |
| **CD-ALT-3** | Drop column / alter type на таблице с данными → только через pre-скрипт. | • Без покрывающего pre → safety-gate violation<br>• Закрытие чеклиста `LESSONS_LEARNED.md` «Контрольный список для Phase 3» | Must |
| **CD-ALT-4** | Add column (nullable или безопасный DEFAULT) → разрешён в авто-дельте. | • Классифицируется как safe | Must |
| **CD-11** | После pre-скриптов заново построить дельту, проверить только разрешённые операции. | • Повторная проверка правила «никаких операций над таблицами с данными»<br>• Остаточные нарушения → падение | Must |
| **CD-12** | Сгенерировать SQL-скрипты деплоя в порядке топосорта. | • Корректный порядок с учётом FK, зависимостей<br>• Скрипты сохраняются как артефакты | Must |
| **CD-13** | Просмотреть сгенерированные скрипты перед применением. | • Артефакты доступны после генерации<br>• В CD применение автоматическое | Must |
| **CD-14** | Применить скрипты дельты с логированием и stop-on-error по умолчанию. | • Подробный лог<br>• `continue-on-error` — только явная настройка | Must |
| **CD-15** | После успешной дельты и post-скриптов — записать новую schema_version. | • git-тег / commit источника, время, связь с выполненными скриптами | Must |

### Phase 13 — GP↔PG YAML Pipeline

| ID | User Story | Acceptance Criteria | Priority |
|----|------------|---------------------|----------|
| **CD-13a** | `db-pm yaml generate` — Greenplum/Postgres directory → portable YAML. | Рекурсивный обход `.sql`, autodoc-парсинг, regex-fallback для GP DDL | Must |
| **CD-13b** | `db-pm yaml apply` — YAML → codebase (manifest + SQL + graph). | GP→PG трансформация: external tables пропускаются, `DISTRIBUTED BY`/`WITH` дропаются | Must |

### Phase 17 — Post-deploy, отчёты, полировка

> Перенумерована с Phase 15: фактически реализованная Phase 15 — GUI deploy
> plan/apply + Plan Viewer; post-deploy/отчёты сдвинуты за Phase 16 (Greenplum
> tuning), которая блокировала живой деплой.

| ID | User Story | Acceptance Criteria | Priority |
|----|------------|---------------------|----------|
| **CD-16** | Выполнять post-скрипты после успешной дельты по тем же правилам. | • Тот же механизм, что и для pre<br>• Ошибки post-скриптов останавливают пайплайн | Must |
| **CD-17** | Итоговый отчёт деплоя. | • Summary + ссылки на артефакты<br>• Список применённых pre/post и объектов дельты | Should |
| **CD-18** | Детальный diff объектов и история версий БД. | • Side-by-side / unified diff DDL<br>• Просмотр `schema_version` и `script_history` | Should |
| **CD-19** | Параметры скриптов с валидацией перед запуском. | • Параметры в автодоке/конфиге<br>• Проверка обязательных параметров | Should |

---

## 5. Направление A — Delta Viewer (Phase 14)

Фронт-энд к существующему `diff_report.json` (Phase 9). Цель — удобный просмотр дельты,
выбор объектов для деплоя, side-by-side diff DDL. MVP для review-сценария. Может идти
параллельно с CD-ядром, т.к. опирается только на готовый Phase 9.

| ID | User Story | Acceptance Criteria | Priority |
|----|------------|---------------------|----------|
| **DV-1** | Открыть JSON-отчёт compare и увидеть дерево объектов с цветовой индикацией статуса. | • Загрузка JSON<br>• Дерево с группировкой по типу объекта<br>• Added/Modified/Dropped цветом<br>• Поиск и фильтры | Must |
| **DV-2** | Включать/выключать объекты и целые типы для деплоя. | • Чекбоксы объекта и типа<br>• «Выбрать всё / Снять всё» по типу<br>• Сохранение выбора в конфигурацию деплоя | Must |
| **DV-3** | Детальный diff объекта (DDL source vs target). | • Side-by-side или unified diff<br>• Подсветка синтаксиса SQL | Should |
| **DV-4** | Экспорт выбранной дельты. | • JSON / CSV / внутренний формат | Should |

---

## 6. Направление C — AI-assisted track (overlay, опционально)

### Принцип: AI предлагает, детерминированный движок проверяет

Единственная безопасная схема для проекта с принципом *«лучше потерять день, чем данные»*:

- **Детерминированно (код, не AI):** классификацию `safe / needs-pre-script / blocked`,
  выбор шаблона ALTER, fully-qualified кавычки (`LESSONS_LEARNED.md` §35), топосорт,
  сам safety-gate. Здесь ошибка = потеря данных → ноль вероятности.
- **AI-агент:** только там, где нет детерминированного ответа — логика переноса/очистки
  данных в pre-скриптах. AI *генерирует кандидат-скрипт*, который **никогда не
  применяется напрямую**.

**Что замыкает цикл и делает это безопасным:** validation deploy (Phase 2) уже умеет
прогонять кодовую базу в пустой temp-БД. Значит, кандидат-скрипт можно *проверить* —
применить в temp-БД, переснять дельту; diff чистый → принять, нет → отвергнуть и отдать
человеку. Это «agentic loop с верификатором», причём верификатор уже написан.

### Три роли (ранжированы по польза/риск)

| ID | Роль | Risk | Priority |
|----|------|------|----------|
| **CD-AI-1** | Генератор pre-deployment backfill-скриптов для таблиц с данными (основная ценность) | низкий (есть верификатор) | Should |
| **CD-AI-2** | Объяснитель отчёта safety-gate на естественном языке (markdown) | очень низкий | Should |
| **CD-AI-3** | Генератор самого ALTER DDL | высокий | **Won't** — не реализовывать |

**CD-AI-1 — User Story:**
> Как DBA, я хочу, чтобы система предложила черновик pre-deployment скрипта для таблицы
> с данными, требующей непростого переноса данных, чтобы не писать его с нуля.
> - Вход: структурный diff + `get_table_row_counts` + список изменений уровня колонок
> - AI генерирует кандидат-скрипт (идемпотентный)
> - Скрипт проходит temp-DB верификацию (apply → re-diff)
> - При провале верификации — отчёт, скрипт не применяется
> - Принятый скрипт коммитится в `migrations/pre/`, далее не регенерируется

**CD-AI-2 — User Story:**
> Как пользователь, я хочу человекочитаемое объяснение отчёта safety-gate.
> - Из структурного diff формируется рекомендация: какие таблицы, сколько строк,
>   какой тип изменения, почему нужен pre-скрипт
> - Markdown-отчёт (закрывает BACKLOG P3 «Markdown-отчёт сравнения»)

**CD-AI-3 — Won't (обоснование):** add column / alter type / rebuild имеют точный
детерминированный ответ. Отдавать это LLM — рисковать галлюцинацией дефолта, ошибкой
кавычек, пропуском FK-зависимости. Девять фаз потрачены на детерминированные гарантии;
маршрутить ALTER через LLM — обнулить их.

### Prerequisite и ограничения AI-трека

- **Структурный column-diff** (общий с детерминированным ALTER) — **закрыт в Phase 12**
  (`infrastructure/diff/columns.py`, `diff_columns`; SQL-тело = источник правды, ALT-1b).
- **Privacy:** DDL схемы чувствителен (имена таблиц/колонок раскрывают бизнес-логику).
  Облачный LLM = публикация схемы. Требует явного решения: on-prem/локальная модель
  vs облачная с opt-in и предупреждением.
- **Недетерминизм AI** съедается одноразовостью: сгенерил → закоммитил в git → не
  регенеришь. Скрипт живёт в репо как любой код.

---

## 7. Направление D — MCP-интеграция (Phase 19)

> План принят 2026-09-27 (сессия с обсуждением crystaldba/postgres-mcp как референса).
> Детальный план: `_tasks_/2026-09-27/20260927_001_mcp_server_plan.md`.

Локальный MCP-сервер `db-pm-mcp` (stdio) поверх существующей инфраструктуры:
`ConnectionStore` (имя → ConnectionConfig с расшифровкой), `registry.get_adapter`,
деплой-сервисы. Отвечает запросу пользователя: запуск скриптов, анализ плана
выполнения, ответы на вопросы по данным + полный деплой-цикл.

**Принципы (зафиксированы при обсуждении плана):**

1. **Готовность к другим СУБД** (MSSQL, Snowflake, MySQL, Oracle…): MCP-секция
   контракта адаптера диалект-агностична, возможности движка объявляются
   capability-флагами. PG/GP — единственная реализация v1; рецепт расширения
   документирован (registry + SUPPORTED_DB_TYPES + optional extra драйвера +
   диалект-мапа sqlglot).
2. **Все настройки MCP — в файлах подключений** (`connections/*.yaml`, блок
   `mcp:`) — продолжение паттерна `allow_drop_schemas`. Поведенческих флагов
   запуска нет (только bootstrap: каталог подключений).
3. **Read-only по умолчанию, запись opt-in.** Двухслойная защита
   (идея из crystaldba/postgres-mcp): sqlglot-классификатор до исполнения +
   read-only транзакция на исполнении как backstop.
4. **Заимствовано из postgres-mcp:** RO-транзакция-backstop, запрет
   EXPLAIN ANALYZE для не-read-only стейтментов, denylist функций
   (dblink/pg_read_file/pg_sleep/lo_*), гранулярная интроспекция
   (list_schemas → list_objects → get_object_details — не выгружать всю
   структуру в контекст LLM), readOnlyHint/destructiveHint, get_top_queries.
   **Осознанно не берём:** hypopg-тюнинг индексов (расширение PG-only),
   health-checks (кандидат в будущие фазы), SSE-транспорт, переезд на
   psycopg3/async.

| ID | User Story | Acceptance Criteria | Priority |
|----|------------|---------------------|----------|
| **MCP-1** | Локальный MCP-сервер над существующими подключениями. | • Entry point `db-pm-mcp`, optional extra `mcp` (`uv sync --extra mcp`)<br>• stdio-транспорт, логи → stderr (stdout занят протоколом)<br>• Инструменты видны MCP-клиенту (ZCode/Claude Desktop) | Must |
| **MCP-2** | Ответы на вопросы по данным (read-only запросы). | • `query`: колонки + строки (dict), row_limit/truncated, statement_timeout<br>• Классификация READ_ONLY + исполнение в read-only транзакции<br>• Не-read-only SQL → отказ с объяснением | Must |
| **MCP-3** | Анализ плана выполнения. | • `explain`: text/JSON (JSON с fallback на text — Greenplum 6)<br>• `analyze=True` только для READ_ONLY стейтментов<br>• `get_top_queries` (pg_stat_statements; graceful NotSupported) | Must |
| **MCP-4** | Запуск SQL-скриптов с предохранителями. | • `run_script` гейтится `mcp.allow_writes` подключения<br>• DESTRUCTIVE/UNKNOWN операторы → дополнительно `confirm_destructive=true`<br>• sqlglot-классификатор с диалектом подключения | Must |
| **MCP-5** | Интроспекция схемы для LLM. | • `list_schemas` / `list_objects(schema, type)` / `get_object_details(schema, object)` поверх `get_database_structure()`<br>• `list_connections` без секретов, с эффективными правами | Must |
| **MCP-6** | Деплой через MCP. | • `deploy_plan`, `deploy_analyze` — read-only<br>• `deploy_apply`, `deploy_reset` — гейт `mcp.allow_deploy` + штатные предохранители (rehearsal temp-БД, SafetyGate, `allow_drop_schemas`, `confirm_database`) | Must |
| **MCP-7** | Политика в файлах подключений. | • Блок `mcp:` (allow_writes, allow_deploy, row_limit, query_timeout_s) в `connections/*.yaml`<br>• Fail-safe дефолты (записи запрещены)<br>• `connections/example.yaml` документирует блок | Must |
| **MCP-8** | Готовность к другим СУБД. | • MCP-секция контракта диалект-агностична (ExplainResult допускает xml/tabular)<br>• Capability-флаги `supports_readonly_txn`, `supports_statement_timeout`<br>• Рецепт расширения в docstring контракта и README | Must |
| **MCP-9** | Тесты и документация. | • Unit: McpSettings, классификатор, адаптер, policy-гейты, инструменты<br>• Integration (Docker): run_query/explain/RO-транзакция e2e<br>• README (раздел MCP), AGENTS.md | Must |

**Правила безопасности фазы (в развитие §8 ниже):**

1. `query` исполняется ТОЛЬКО в read-only транзакции (где движок поддерживает)
   и только для READ_ONLY-классифицированного SQL; один стейтмент на вызов.
2. Denylist функций для read-only режима — read-only транзакция не защищает
   от `dblink()` (открывает своё соединение) и файловых функций.
3. UNKNOWN (не распарсилось) = DESTRUCTIVE (fail-safe), требует confirm.
4. Деплой через MCP не обходит штатные предохранители деплой-пайплайна.

---

## 7a. Направление E — Data Profiling (Phase 20)

> Решения зафиксированы 2026-10-06: final `_tasks_/2026-10-06/20261006_001_ydata_profiling_final.md`,
> план `_tasks_/2026-10-06/20261006_002_phase20_sql_profiler_plan.md`, журнал диалога —
> `20261006_002_phase20_sql_profiler_dialog.md`.

Собственный SQL-профайлер: агрегаты считает БД, наружу выкачиваются только
итоги. ydata-profiling исключён (нет новых зависимостей), HTML не делаем —
JSON. Отдельные генераторы для Postgres 18 и Greenplum 6 (ядро PG 9.4,
LESSONS §70). Read-only по построению: RO-транзакция + statement_timeout
(Phase 19); ANALYZE инструмент не запускает — только совет в отчёте.

| ID | User Story | Acceptance Criteria | Priority |
|----|------------|---------------------|----------|
| **PF-1** | CLI `db-pm profile <connection> --tables schema.table,...`. | • JSON в `reports/<connection>/<schema>.<table>.json`<br>• Сводка в stdout<br>• `reports/` в .gitignore (литералы данных) | Must |
| **PF-2** | Отдельные SQL-генераторы PG/GP. | • PG: TABLESAMPLE, percentile_cont<br>• GP: сэмпл `random() < p`, без TABLESAMPLE (spike 2026-10-06)<br>• Один скан на таблицу: агрегаты всех колонок одним SELECT | Must |
| **PF-3** | Opt-in политика в подключении. | • Блок `profiling:` (enabled, пороги, сэмпл, timeout)<br>• Нет блока/`enabled: false` → отказ без единого запроса к БД | Must |
| **PF-4** | Большие таблицы без статистики. | • Порог по `pg_total_relation_size()` (default 1 GiB)<br>• Дорогие метрики по сэмплу; доля из reltuples или `default_sample_fraction`<br>• ANALYZE не запускается: `stats_fresh` + совет в отчёте | Must |
| **PF-5** | MCP-инструмент `profile_tables`. | • Тот же сервис, readOnlyHint=true<br>• Гейт `profiling.enabled` подключения | Must |
| **PF-6** | Тесты и документация. | • Unit: генераторы SQL, сервис, гейты<br>• Integration: PG 18 testcontainers<br>• README раздел «Профайлинг» | Must |

---

## 8. Правила безопасности (зафиксированные)

1. **Таблицы с данными** — любые операции в авто-дельте запрещены. Нет флага «можно потерять данные».
2. **Пустые таблицы** — можно пересоздавать и изменять автоматически.
3. **Новые объекты** — всегда можно создавать.
4. **Не-табличные объекты** (процедуры, функции, views, types) — деплоятся автоматически.
5. **Pre-скрипты** — единственный легальный способ менять таблицы с данными. Идемпотентные.
6. **Safety-gate** срабатывает до выполнения pre-скриптов и формирует рекомендацию.
7. **После pre** дельта пересчитывается; если опасные изменения остались — пайплайн падает.
8. **Forward-only.** Отката нет. Откат = новый forward-deploy к старой версии схемы
   (через pre-скрипты). Явное ограничение, не недоработка.

---

## 9. Целевой пайплайн

```
1. Сборка артефакта: дерево схемы + migrations/pre + migrations/post
2. Проверки версии и истории (schema_version ≤ source, script_history + checksum)
3. Pre-analysis (safety-gate) — ДО pre-скриптов:
   • compare код ↔ текущая БД → таблицы к изменению
   • оценка наличия данных через метаданные
   • сопоставление с pre-скриптами
   • uncovered таблица с данными → ПАДЕНИЕ + отчёт-рекомендация
4. Выполнение pre-скриптов (история + идемпотентность, порядок по имени)
5. Повторный анализ / генерация дельты (только безопасные операции)
6. Генерация скриптов деплоя + сохранение артефактов
7. Применение дельты (топосорт, лог каждого шага)
8. Выполнение post-скриптов (аналогично pre)
9. Фиксация версии (schema_version + script_history)
```

---

## 10. Открытые вопросы (`USER_INPUT`)

> Закрываются при старте соответствующей фазы (в её `phase_NN/..._vision_draft.md`).

### Общие
- **Q1** — Нужен ли отдельный edge diff (BACKLOG P2), если будет Delta Viewer?
  *Рекомендация (ЗАКРЫТО в Phase 14):* включён в DV как вкладка «Рёбра» + секция в
  markdown. Реализовано (коммит `141e9fc`).
- **Q2** — Delta Viewer: начинать с CLI или сразу GUI?
  *Рекомендация (ЗАКРЫТО в Phase 14):* сначала markdown (CLI `compare report`), затем
  GUI. Реализовано (коммиты `3649b0c`, `211a402`, `b9b3fd1`).
- **Q3** — Pre/post-скрипты всегда идемпотентны, или допустим run-once?
  *Рекомендация (ЗАКРЫТО в Phase 10):* всегда идемпотентны; run-once — только
  через явный флаг. Реализовано в CDF-1 / `application/script_runner.py`.
- **Q4** — Версия: git-тег обязателен или достаточно git-commit?
  *Рекомендация (ЗАКРЫТО в Phase 10):* ни то, ни другое — **manifest
  `source_version`** (calver `YYYY.MM.DD.NN`), required, без git. Реализовано
  в CDF-2 / `infrastructure/config/codebase_manifest.py` (format_version=2).
- **Q5** — Где живёт `migrations/`?
  *Рекомендация (ЗАКРЫТО в Phase 10):* рядом с деревом схемы в
  `<output>/<db>/__migrations/{pre,post}/` (с `__`-префиксом — консистентно с
  `__deploy`). Реализовано в CDF-3 / `application/script_runner.py`.
- **Q6** — Delta Viewer и CD: обязательна ли связь?
  *Рекомендация:* CD работает через CLI, не требует DV; связь опциональна.

### AI-трек
- **Q7** — On-prem/локальная модель или облачная?
  *Рекомендация:* on-prem по умолчанию (DDL чувствителен); облачная — только явный opt-in.
- **Q8** — AI-трек как часть CD или отдельная надстройка (флаг)?
  *Рекомендация:* отдельная надстройка (`--ai-assist`), не часть ядра safety-gate.

---

## 11. Где читать дальше

| Doc | Why |
|-----|-----|
| `_checkpoints_/20260731_001_checkpoint.md` | текущее состояние проекта (Phase 8 done) |
| `_phases_/Phase_08.md` | overload resolution — свод фазы |
| `_tasks_/BACKLOG.md` | отдельные P2/P3-задачи (P1 overload resolution закрыт) |
| `_phases_/Phase_09.md` | compare — фундамент pre-analysis (CD-6) |
| `LESSONS_LEARNED.md` §35 | fully-qualified DDL-контракт — обязателен для деплой-SQL |
| `LESSONS_LEARNED.md` §36 | qualify-refs — ограничение для diff function/view bodies |
| `LESSONS_LEARNED.md` «Контрольный список для Phase 3» | ALTER-план, pre/post-deploy порядок |
