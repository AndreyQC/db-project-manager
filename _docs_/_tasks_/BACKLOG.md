# Backlog — открытые задачи

> Назначение: единое место для задач, не вошедших в завершённые фазы. Каждая запись —
> кандидат в будущую фазу (Phase 5+). Пополняется при закрытии фаз (checkpoint) и по
> мере обнаружения проблем в реальных прогонах.
>
> Связанные документы:
> - `_checkpoints_/20260720_001_checkpoint.md` — последний checkpoint (Phase 4 done)
> - `_tasks_/phase_00/003_roadmap_migration.md` — исходный roadmap (частью устарел:
>   Phase 3 в нём описана как «Миграции», фактически сделана как SSH-туннель)
>
> Дата заведения: 2026-07-20

---

## Приоритеты

- **P1 — блокирует пользовательские сценарии.** Без этого инструмент не решает
  основную задачу для реальной БД.
- **P2 — заметное ухудшение UX/читаемости.** Работает, но раздражает.
- **P3 — качество/будущие возможности.**

---

## P1. Разрешение перегруженных вызовов в edge detection

**Статус: ЗАКРЫТ** — реализовано в Phase 8 (литералы MVP, коммиты `d4b52e2`…`5587418`).
См. `_tasks_/phase_08/Phase_8_result.md`, `_phases_/Phase_08.md`. Follow-up: колонки и
рекурсия вызовов как аргументы → новые P2-записи ниже.

**Контекст:** Phase 4 зафиксировала MVP-ограничение — `_build_names_index`
(`pg_sql_parser.py:212-224`) индексирует объекты по голому `object_name` через
`setdefault`. При перегрузках вызовы в SQL создают рёбра к «первой попавшейся» вершине.

**Цель:** по аргументам вызова функции (`sp_x(123)` → `int4`) определять, к какой
именно перегрузке вести ребро.

**Сложность:** полноценный type inference — нужно определять типы выражений-аргументов
(литералы, колонки, результаты других вызовов). Это объёмная задача.

**MVP-вариант:** покрывать только простые случаи — литералы и прямые ссылки на колонки
с известным типом. Для неразрешимых вызовов — ребро к первой перегрузке (как сейчас),
с warning в лог.

**Кандидат на Phase 6+** (после extensions и миграций). Зафиксировано в
`Phase_4_vision_final.md` §6 (Q3).

---

## P2. DEPENDS_ON рёбра от объектов к extension

**Контекст:** Phase 5 поставила extensions с приоритетом −2, что гарантирует
порядок «extensions до всего остального». Точечные рёбра `DEPENDS_ON` нужны, если
появится сценарий, где **часть** объектов зависит от конкретного extension, а часть —
нет, и нужно более точное управление порядком внутри групп.

**Реализация:** детект через `pg_depend` / `pg_type.typtype='e'` /
`atttypid` → `pg_type` → extension (по `typnamespace`).
Запросы сложнее, чем кажутся; пока выигрыша в порядке деплоя нет —
`extension: -2` уже достаточно.

**Кандидат на Phase 6+** (связан с P1 — overload resolution).

---

## P2. Интеграционный тест reverse→graph→deploy на реальной PostgreSQL с перегрузками

**Контекст:** Phase 4 покрыта только синтетическими фикстурами
(`sample_structure.json`, `codebase_sample/app/functions/`). Нет проверки на живой БД.

**Цель:** через testcontainers (harness уже есть из Phase 2) поднять PG, создать
перегруженные функции, прогнать `reverse-engineer → graph build → deploy validate`
на чистой временной БД. Контроль: обе перегрузки деплоятся.

**Статус: ЗАКРЫТ** — реализован в Phase 5 (P5.S08):
`tests/integration/test_phase5_extensions_e2e.py`.

---

## P3. DEPENDS_ON рёбра от объектов к extension

**Контекст:** Phase 5 поставила extensions с приоритетом −2, что гарантирует
порядок «extensions до всего остального». Точечные рёбра `DEPENDS_ON` нужны, если
появится сценарий, где **часть** объектов зависит от конкретного extension, а часть —
нет, и нужно более точное управление порядком внутри групп.

**Реализация:** детект через `pg_depend` / `pg_type.typtype='e'` /
`atttypid` → `pg_type` → extension (по `typnamespace`).
Запросы сложнее, чем кажутся; пока выигрыша в порядке деплоя нет —
`extension: -2` уже достаточно.

**Кандидат на Phase 6+** (связан с P1 — overload resolution).

---

## P3+. AST-based qualify через sqlglot (follow-up к Phase 6)

**Контекст:** Phase 6 `QualifyRefsService` использует regex для поиска bare refs.
Покрывает function-call и FROM/JOIN, но не CTE с тем же именем, dynamic SQL,
идентификаторы в строковых литералах и `$function$` телах.

**Триггер к реализации:** появление false positives/negatives в проде (deploy
падает на функции, где regex квалифицировал что-то лишнее или пропустил нужное).

**Реализация:** парсинг через `sqlglot` (Python, поддерживает PG диалект),
AST-traversal для точного определения Table/Func nodes, квалификация только
реальных идентификаторов объектов.

**Не блокирует текущую работу** — regex-MVP + `_qualify_report.md` достаточно
для большинства схем. Известное ограничение зафиксировано в `LESSONS_LEARNED.md` §36.

---

## P3+. Пин версий extensions

**Контекст:** Phase 5 vision Q3: версия extension **не** пишется в `VERSION '<ver>'`
DDL — только информационно в autodoc. При деплое на кластер с другой версией
пакета молча используется кластерная версия.

**Реализация:** опциональный флаг `--pin-extension-versions` в генераторе;
`VERSION '<ver>'` из `extension_version` autodoc вставляется в рендер.
Требует конфиг-флага на уровне generation, не на уровне deploy.

**Кандидат на Phase 6+**.

---

## P3+. Устаревший roadmap `phase_00/003_roadmap_migration.md`

**Проблема:** §10 «Фазы реализации» описывает Phase 3 как «Миграции и расширение СУБД»,
но фактически Phase 3 сделана как SSH-туннель (`_tasks_/phase_03/`). Фразы «Фаза 3 —
MSSQL и MySQL адаптеры» и «Фаза 2 — Snowflake» рассинхронизированы с реальностью.

**Действие:** либо переписать §10 под фактические фазы (1=фундамент, 2=граф+deploy,
3=SSH, 4=перегрузки, 5=extensions), либо пометить раздел устаревшим со ссылкой
на этот беклог и каталоги `phase_NN/` как источник правды.

**Кандидат на doc-cleanup коммит** (не блокирует ничего).

---

## P3. Flaky env-тест `test_crypto_util::test_decrypt_nested_dict`
**Симптом:** периодически падает в полном прогоне, в изоляции зелёный.

**Причина:** пересечение `monkeypatch.setenv(TEST_CRYPTO_ENV, ...)` между
`test_crypto_util` и `test_connection_store` (описано в `LESSONS_LEARNED.md` §13, §22).

**Действие:** полностью изолировать env-зависимые тесты (свой ключ, своя переменная,
явная teardown), либо перевести на `monkeypatch.context()` / session-scoped fixture.

**Не критично, но влияет на доверие к CI.**

---

## P3. GEXF-экспорт графа (нативный формат Gephi)

**Контекст:** Phase 7 (действие `graph_prepare`) готовит граф для Gephi через
существующий `graphml`-экспорт (`infrastructure/graph/export.py`) — Gephi его
открывает, атрибуты узлов/рёбер на месте. GEXF — родной формат Gephi,
поддерживает динамику/визуальные атрибуты богаче.

**Действие:** добавить `gexf` в `export_graph` и в combo форматов
`GraphPrepareDialog` (`EXPORT_FORMATS` в `actions/models.py`).

**Триггер:** если graphml-импорта в Gephi перестанет хватать.

---

## P3. Per-row dropdown действий в списке подключений

**Контекст:** Phase 7 vision U2 — dropdown действий размещён на панели справа
от всего списка, а не в каждой строке. Вариант «dropdown в строке выбранного
элемента» (`QListView.setIndexWidget`) отложен: усложняет модель списка без
явного выигрыша в UX (диалоги действий всё равно содержат выбор подключения).

**Триггер:** запрос пользователя на действия «по месту» в списке.

---

## P2. Edge diff (сравнение рёбер графа зависимостей)

**Статус: ЗАКРЫТ** — реализовано в Phase 14 (вкладка «Рёбра» в DV + секция в markdown,
коммит `141e9fc`). См. `_phases_/Phase_14.md`, `_tasks_/phase_14/Phase_14_result.md`.

**Контекст (исторический):** Phase 9 (compare) сравнивает *наличие объектов + структуру*
(SQL), но не рёбра графа. Появился/исчез FK, вызов функции, JOIN — не видны в отчёте.
Естественное продолжение compare: после фильтрации общих вершин сравнить рёбра
по `Edge.dedup_key()` (`(source_object_key, destination_object_key, relation, action)`).

**Действие (выполнено):** расширить `DiffReport` секцией `edge_entries` (added/removed
edges); обновить `comparator.py` и `snapshot.py` (снимать не только вершины, но и рёбра).

**Связано:** `_tasks_/phase_09/002_result_phase_09.md` §4; `domain/graph.py`
(`Edge.dedup_key`).

**Триггер:** запрос на детекцию изменившихся зависимостей (FK, вызовы функций)
между двумя состояниями БД.

---

## P3. GUI action для compare — ВЫПОЛНЕНО (2026-07-29)

**Статус:** выполнено. Реализовано как 4-е действие в реестре Phase 7
(коммит `c26ecd7`). См. `_tasks_/2026-07-29/20260729_002_compare_gui_action_final.md`.

**Контекст:** Phase 9 реализована как CLI-only (`db-pm compare run`). GUI action
(через реестр Phase 7) не добавлен — по решению пользователя логика обкатается в
CLI, GUI отдельной задачей.

**Действие:** добавить `compare` в `ACTIONS` (`presentation/gui/actions/registry.py`)
+ `CompareSettings` модель + диалог (два source/target combo dir|connection,
output_dir, keep_model_dir) + `build_cli_compare` + worker + контракт-тест
(CliRunner). По образцу `DeployValidateDialog` (два поля подключения) +
`GraphPrepareDialog` (поля каталога).

**Триггер:** после обкатки compare в CLI, запрос на GUI-доступ.

---

## P3. Markdown-отчёт сравнения

**Статус: ЗАКРЫТ** — реализовано в Phase 14 (CLI `db-pm compare report`, коммит
`211a402`; генератор `3649b0c`). См. `_phases_/Phase_14.md`.

**Контекст (исторический):** Phase 9 пишет отчёт только в JSON (`source.json`,
`target.json`, `diff_report.json`) — machine-readable, для дальнейшей обработки.
Человекочитаемый свод отсутствовал.

**Действие (выполнено):** генерировать `diff_report.md` рядом с JSON — секции
added/removed/changed, сгруппированные по схеме/типу, summary наверху; для changed —
collapsible unified-diff; плюс секция Edges (после Phase 14 S5).

**Триггер:** запрос на ревью-удобный формат отчёта (например, коммитить в репо
как артефакт code review).

---

## P3. Delta Viewer follow-up'ы (после Phase 14)

**Контекст:** Phase 14 реализовала MVP Delta Viewer. Несколько мелочей осознанно
отложены (зафиксированы в `_phases_/Phase_14.md` §5).

- **Side-by-side diff** (QTableWidget 2 синхронизированные колонки) вместо unified-diff.
  *Триггер:* отзыв пользователя, что unified-diff неудобочитаем для больших DDL.
- **Опция `--markdown` в `compare run`** — сахар поверх `compare report`, чтобы
  получать markdown за один прогон (без отдельной команды).
- **`QAbstractItemModel` вместо `QTreeWidget`** — если производительность на больших
  схемах (тысячи объектов) окажется недостаточной.
- **Ручной тест пользователем** на реальном `diff_report.json` сравнения живой БД —
  offscreen-smoke покрывает контракты, но review-UX нуждается в человеческой оценке.

---

## P3. Настраиваемый фильтр типов объектов в compare

**Контекст:** Phase 9 сравнивает типы, захардкоженные в `DIFFED_TYPES`
(`infrastructure/diff/snapshot.py`): tables/views/materialized_views/functions/
procedures/sequences. Extensions и database_settings исключены (шумят между
средами).

**Действие:** добавить флаг `--include-types` (или `--exclude-types`) в
`db-pm compare run`, пробрасывать в `build_snapshot_from_dir`.

**Триггер:** если пользователю понадобится сравнивать extensions/settings или,
наоборот, сузить сравнение до отдельных типов.

---

## P2. Overload resolution — колонки как аргументы вызова

**Контекст:** Phase 8 (overload resolution, BACKLOG P1 — закрыта) покрывает только
литеральные аргументы (`sp_x(123)`→int4, `sp_x('x')`→text). Вызовы с колонками как
аргументами (`sp_x(t.col)`) неразрешимы — ребро к first-wins + запись в отчёт.

**Цель:** для прямой ссылки на колонку таблицы с известным типом выводить тип аргумента и
разрешать перегрузку.

**Реализация:** индекс колонок — парсинг `CREATE TABLE` (regex по DDL) или вытягивание из
catalog в reverse-engineer (доп. поле в autodoc таблиц). Сложнее литералов; типов колонок
может быть много, ambiguity (`t.col` где `t` — алиас) — отдельная проблема.

**Связано:** `_tasks_/phase_08/Phase_8_result.md` §5; `infrastructure/parsing/overload_resolution.py`.

**Триггер:** реальные перегрузки в кодовой базе, разрешаемые только по колонкам.

---

## P2. Overload resolution — тип результата вызова функции как аргумент

**Контекст:** Phase 8 не поддерживает рекурсию inference — аргумент вида `sp_x(sp_y(1))`
неразрешим (вложенный вызов → unknown).

**Цель:** по `return_type` вложенной функции выводить тип её результата и использовать как
тип аргумента внешнего вызова.

**Реализация:** требует `return_type` на `Vertex` (сейчас его нет — в structure dict есть,
но в autodoc/graph не пробрасывается). Аналогично пробросу `argument_types` (P8.S1–S3), но
поле одно и для function. Циклические зависимости вывода (A вызывает B вызывает A) —
разрешать итеративно с фиксpointом.

**Связано:** `_tasks_/phase_08/Phase_8_result.md` §5.

**Триггер:** глубокие цепочки вызовов в кодовой базе.

---

## P3. Регрессионный тест build=False → исключение из деплоя

**Контекст:** при закрытии Phase 8 выявлено, что путь `build:false в autodoc → вершина
исключена из деплоя` не покрыт тестом. Сама фильтрация работает (проверено прямой
проверкой `deploy_order` на реальном каталоге), но если будущие правки сломают
`filter_build_true` / чтение `build` — это никто не заметит.

**Действие:** тест: фикстура с вершиной `build:false` → после `deploy_order(build_only=True)`
её нет в списке; `build:true` — есть. Плюс проверка, что парсер читает `build: false`
как Python `False` (не строку `"false"` → `bool("false")==True`).

**Связано:** `domain/graph.py` (`filter_build_true`), `pg_sql_parser.py:184` (чтение build),
`application/graph_service.py` (`deploy_order`).

**Не блокирует ничего, но повышает доверие к фильтрации деплоя.**

---

## P1. Integration: RE→deploy падает на database_setting без настроек (пустой SQL)

**Контекст (выявлено при закрытии Phase 11):** два интеграционных теста падали ещё
ДО Phase 11 (проверено на коммите `5667eda`): `test_qualify_refs_e2e::
test_bare_function_call_qualified_and_deploys` и `test_phase5_extensions_e2e::
test_extensions_and_overloads_roundtrip`.

**Симптом:** reverse-engineer БД без специфичных настроек уровня БД генерирует
`settings/database settings.sql`, содержащий ТОЛЬКО комментарии (исполнимого SQL нет).
Deploy затем падает: `psycopg2.ProgrammingError: can't execute an empty query`.

**Корневая причина (гипотеза):** Phase 5 генерирует `database_setting`-объект всегда,
даже когда `db_properties` пусты; deploy обязан либо пропускать comment-only скрипты,
либо RE не должен эмитить пустой объект.

**Действие:** (a) в `_deploy_object`/`execute_script` — skip, если после
`strip_autodoc` и удаления `--`-комментариев текст пуст; или (b) RE не генерирует
`database settings.sql` при отсутствии реальных настроек. Покрыть тестом.

**Триггер:** любой integration-прогон RE→deploy на «чистой» PG (10 упавших
интеграционных тестов при закрытии Phase 11 уже починены фикстурой `8ed60ab`;
эти 2 — отдельная корневая причина).

**Статус:** выполнено (2026-08-15). При разборе вскрылись **три** независимые
причины, а не одна гипотеза выше:

1. **Comment-only скрипт (гипотеза подтвердилась).** `_get_database_properties`
   всегда непуст (encoding/lc_collate/lc_ctype), поэтому `database settings.sql`
   эмитится даже при пустых settings — body = только комментарии → empty query.
   Вариант (b) отвергнут: autodoc-заголовок файла несёт `db_properties` для
   `CREATE DATABASE` temp-БД. Реализован вариант (a): новый
   `infrastructure/sql/sql_text.py::has_executable_sql` (учёт `'…'`, `"…"`,
   `$$…$$`, `--`, `/* */` — false «пусто» тихо пропустил бы исполняемый SQL);
   `_deploy_object` пропускает comment-only с логом, вершина остаётся в графе.
   Регрессия: `test_deploy_service.py::test_comment_only_script_is_skipped_not_failed`.
2. **Баг сетапа `test_phase5_extensions_e2e`:** `CREATE EXTENSION IF NOT EXISTS
   uuid-ossp` без кавычек — имя с дефисом парсится как `uuid - ossp` (SyntaxError);
   тест не доходил до деплоя. Плюс два латентных бага того же теста: устаревший
   `build_only=True` (API-дрейф `BuildGraphService.build`) и фильтр ключей
   `"function public.f"` вместо актуального формата `/function/name/f/`.
3. **Межтестовая контаминация:** общая БД `postgres` контейнера накапливала
   объекты от предыдущих тестов → order-dependent падение qualify-теста на
   `_validate_deploy_presence`. Фикстура `pg_conn_cfg` теперь создаёт чистую
   пер-тестовую БД (`dbpm_it_<hex>`) и удаляет её в teardown.

**Проверки:** `uv run pytest -m integration` — 16 passed (было 14 passed /
2 failed); unit — 700 passed; ruff — чисто.

---

## P3. GUI-действия deploy plan/apply + рендер плана деплоя

**Статус: ЗАКРЫТ** — реализован в Phase 15 (`_docs_/_phases_/Phase_15.md`):
GUI-обёртки `deploy_plan`/`deploy_apply` с preflight-warning
(`DeployApplyDialog`, чекбокс гейтит OK), `PlanViewerWindow` для просмотра
`plan.json` (дерево, фильтры safe/needs-pre/blocked, DDL-таб), chain
analyze → plan → apply через prefill в Plan Viewer. Все три CLI-флага
(`--include-drops`/`--no-rehearsal`/`--keep-rehearsal-db`) доступны из GUI.
Тесты: 20 новых unit-тестов (8 contract CLI, 4 offscreen-smoke диалогов,
8 plan_viewer). Lessons §59-§60.

---

## P3. Multi-statement normalize + comment-level diff (по итогам Phase 12)

**Контекст:** `infrastructure/diff/normalize_sql.py` использует `parse_one` —
нормализует только ПЕРВЫЙ statement тела. `COMMENT ON`-строки в табличных файлах
не участвуют в `sql_hash`: comment-only правки дают UNCHANGED (поведение Phase 9,
сохранено в Phase 12). ColumnSnapshot.comment в v1 не заполняется по той же
причине (см. `Phase_12_plan.md` S2, «Известное ограничение»).

**Действие:** перевести normalize на `sqlglot.parse` (все statements) с защитным
переходом — смена хэша сделает ВСЕ существующие объекты «изменившимися», нужен
формат-бамп/миграция (напр. версии snapshot или канонический пересчёт обеих
сторон). Затем: `extract_columns` парсит `COMMENT ON COLUMN` → `comment` в
`ColumnSnapshot` → `COMMENT_CHANGED` в column-diff (классифицируется safe).

**Триггер:** первые жалобы «отредактировал комментарий — деплой не увидел».

**Связано:** `infrastructure/diff/{normalize_sql,columns}.py`, `domain/delta.py`.

---

## P3. validate_deploy_ddl не проверяет наличие schema __deploy.sql

**Контекст (Phase 15.5, cis_zup feedback 2026-09-02):** при диагностике
`InvalidSchemaName` для таблиц `__deploy` обнаружено, что
`infrastructure/deploy/canonical_ddl.py::validate_deploy_ddl` (вызывается в
`DeployValidateService.run` и `DeployApplyService._run_pipeline` перед apply)
проверяет только3 таблицы (`schema_version`/`script_history`/`script_audit_log`).
Наличие **`schema __deploy.sql`** НЕ проверяется.

**Действие:** добавить проверку наличия файла
`<codebase>/__deploy/schema __deploy.sql`. Если отсутствует — warning
(как сейчас для таблиц), без блокировки apply (CDF-10 approach b).
Compare с содержимым canonical: пустая схема `CREATE SCHEMA IF NOT
EXISTS "__deploy";` + autodoc. SHA-256 нормализованного тела должен
совпадать с `script_checksum(strip_autodoc("CREATE SCHEMA IF NOT
EXISTS \"__deploy\";\n"))`.

**Триггер:** первое ручное удаление schema __deploy.sql из codebase +
последующий deploy apply.

**Связано:** Phase 15.5 fix `DIFFED_TYPES + schema` (commit `606cd3d`)
восстанавливает CREATE SCHEMA в плане для **пустой** target-БД, но не
предотвращает silent drop schema.sql из codebase. Это второй шаг
гигиены `__deploy`.

**Не блокирует.** Реальный cis_zup имеет schema __deploy.sql на месте.

---

## P3. RE snapshot для target-БД seed'ит phantom `__deploy` в temp_root

**Статус: ЗАКРЫТ через Phase 15.5.2** (commit `160cdd7`).
Альтернативное решение — отдельная утилита `db-pm deploy
init-service-schema` для явного bootstrap на пустой target-БД.

**Оригинальный контекст:** при cis_zup feedback 2026-09-02 (повторный прогон
на пустой target-БД `local-PG-18_DB__cis_zup_dev_U_postgres`) deploy apply
не создавал `__deploy` schema +3 таблицы. Root cause: target-side RE
в `CompareService._build_db_side` вызывает
`ReverseEngineerService.run(...)`, который через `_seed_or_sync_deploy`
**всегда** записывает canonical `__deploy/schema __deploy.sql` +
3 таблицы в temp_root (даже если `__deploy` реально нет в БД).
Compare видит их как UNCHANGED; DeltaPlan action=skip; apply ничего
не выполняет для `__deploy`.

**Закрытие:** вместо глобального рефакторинга RE (флаг `seed_deploy`
в конструкторе / параметр `.run()`, поведение RE-сервиса в compare vs
codebase-write) сделана отдельная команда
`db-pm deploy init-service-schema --target-connection-file ...` —
вызывает `ServiceSchemaInitializer`, который через
`adapter.get_database_structure()` проверяет реальное состояние БД
(НЕ temp_snapshot) и идемпотентно создаёт `__deploy` через
`CREATE SCHEMA IF NOT EXISTS` + `CREATE TABLE IF NOT EXISTS`. На повторное
выполнение — no-op.

**Урок (LESSONS §62):** RE-write-to-codebase и RE-snapshot-for-compare
выглядят одинаково в коде (`ReverseEngineerService.run`),
но имеют разные инварианты. SEEDирование `__deploy` в codebase-write —
правильно (rebuild); SEEDирование в temp_snapshot — **маскирует
отсутствие `__deploy` в реальной БД** при compare с пустым target.
Чистое исправление требует разделения этих путей (доп. параметр в
RE-сервисе или две разные RE-функции). Для MVP — отдельная утилита
достаточна; глобальный рефакторинг RE остаётся техдолгом на случай,
если пользователи предпочтут одну кнопку.

---

## P3. (бывший) Долгая цепочка verify-then-bootstrap при каждом deploy apply

**Контекст:** Phase 15.5.2 ввёл `deploy init-service-schema` как отдельный
шаг. Сейчас пользователь должен явно запускать его перед
`deploy apply` на свежей БД. Можно автоматизировать: в
`DeployApplyService._run_pipeline` перед `manifest = read_manifest(...)`
проверить, существует ли `__deploy` schema в target-БД (через `get_schema_version`
или `adapter.execute_script("SELECT 1 FROM pg_namespace WHERE nspname='__deploy'")`).
Если нет + `manifest.source_version` есть — вызвать `ServiceSchemaInitializer`
и продолжить. Альтернатива: падать с понятным сообщением «run
db-pm deploy init-service-schema first».

**Действие:** держать поведение explicit-init (так безопаснее — пользователь
видит отдельный шаг). Если CI-сценарии потребуют авто-init — добавить
`--init-service-schema` флаг в `deploy apply`.

**Не блокирует.**

---

## P3. YAML-сравнение source vs target вместо hash-сравнения (Phase 16+)

**Контекст (cis_zup feedback 2026-09-03, 2026-09-04):** Phase 12
`DeployApplyService` строит `DeltaPlan` на основе `sql_hash` сравнения
source-side и target-side snapshots. Hash считается через
`sqlglot`-нормализацию всего DDL-тела. Это даёт **false-positive CHANGED**
на форматных различиях, которые PG и наш SQL-парсер считают
семантически эквивалентными:

* `CAST('utc' AS TEXT)` vs `'utc'` (Phase 15.5.3 — закрыто post-AST
  regex нормализацией, см. `LESSONS §63`);
* в перспективе — другие эквивалентные формы DEFAULTs, имена ограничений
  (`group_id_fkey` vs автоматически-сгенерированное), whitespace внутри
  DDL, и т.п.

Каждое такое различие выливается в:

1. safety gate violation (touch=CHANGED + covered=[] + presence=HAS_DATA);
2. column-diff классифицируется как DEFAULT_CHANGED (NEEDS_PRE);
3. deploy apply падает, если нет покрывающего pre-скрипта.

**Альтернативный подход (предложен пользователем):** не опираться на
`sql_hash` при сравнении. Вместо этого:

1. Строить YAML-снимок **codebase-стороны** через `db-pm yaml generate`
   по SQL-файлам в dir (Phase 13 уже умеет; текущая команда —
   `yaml generate --source <dir>`).
2. Строить YAML-снимок **target-стороны** через `db-pm yaml generate` по
   БД-стороне (нужна `yaml generate --source <conn>` — расширение Phase 13).
3. Сравнивать два YAML **по каждому объекту** (структурно, а не по hash):

   - compare columns: name/type/nullable/default → per-column diff
     (Phase 12 ALREADY делает это для changed таблиц).
   - compare функций/proc/views — по тексту тела, нормализованному
     ровно тем же sqlglot pipeline, что у Phase 13.
   - compare constraints/indexes/etc. — пока нет (backlog P3 — multi-statement
     normalize, Phase 12 LESSONS §3).

**Выигрыш:**
- YAML-сравнение устойчиво к форматным репрезентациям PG
  (`CAST` ↔ literal, autogen names, whitespace).
- Можно выявлять «реальные» отличия (тип колонки, default value),
  игнорируя косметические.
- Один формат хранения (`YamlProject`) и для diff, и для source-of-truth
  codebase (yaml apply уже умеет генерировать из YAML).

**Зависимости / сложность:**
- Расширить `YamlGenerateService` (Phase 13) на source=DB.
- Snapshotter нужен — общий с `compare_service`/`reverse_engineer`.
- Comparator (`compare_service` или новый) должен принимать два YAML на
  вход и выдавать структурный diff без hash.
- Один новый контракт тестов + e2e.

**Связано:** Phase 13 (`infrastructure/yaml_project/`),
Phase 12 (`domain/delta.py::DeltaPlan`),
`infrastructure/diff/normalize_sql.py` — сейчас там hash-логика; YAML-сравнение
позволит **deprecate** hash для source-vs-target (но оставить для
codebase-vs-codebase, например при проверке integrity).

**Не блокирует.** Phase 15.5.3 закрывает наиболее частый кейс
(default-format), но архитектурно правильнее уйти от hash целиком.
Записываем как Phase 16+ кандидат (после Phase 16 post-deploy отчётов).

---

## P3. Авто-генератор seed «одной записи на таблицу» (ALT-8b)

**Контекст (Phase 12, ALT-8):** seed репетиции — пользовательские скрипты
`__migrations/seed/*.sql` (детерминированно, FK-порядок на авторе). Ручной seed
скучен для больших схем.

**Действие:** генератор черновика seed по структуре: INSERT одной строки на
таблицу из DEFAULT-значений (NOT NULL-колонки без DEFAULT → таблица в протокол
пропусков), порядок вставок по топосорту FK. Черновик предлагается
`db-pm deploy rehearsal-seed --dir ... > __migrations/seed/...` — коммитится и
дальше живёт как обычный seed-скрипт (не регенерируется).

**Триггер:** регулярная работа с apply на схемах >30 таблиц.

**Связано:** `application/deploy_apply_service.py` (`_run_seed`), BACKLOG-запись
P3 GUI plan/apply.

---

## P3. deploy analyze: даунгрейд safe-alter нарушений по ALT-3

**Контекст (Phase 12):** `deploy plan`/`apply` даунгрейдят gate-нарушения через
колоночную классификацию (`_gate_residual_violations`): тронутая таблица с
данными, дельта по которой safe (ADD COLUMN nullable), пропускается. Standalone
`deploy analyze` (Phase 11) остался table-level строгим — та же ситуация даёт
VIOLATIONS, хотя `apply` прошёл бы.

**Действие:** синхронизировать `SafetyGateService.analyze` с residual-логикой
(или задокументировать расхождение как осознанное: analyze = «худший случай»,
apply = точная классификация).

**Триггер:** путаница пользователей «analyze красный, apply зелёный».

**Связано:** `application/{safety_gate_service,deploy_apply_service}.py`;
`_tasks_/phase_12/Phase_12_result.md` (отклонения).

---

## P2. Именованные пресеты конфигураций действий GUI (save/load YAML)

**Контекст (фидбек пользователя, 2026-08-31):** частые повторяющиеся операции
между двумя базами (например, compare dev↔prod, deploy plan на один и тот же
таргет). GUI хранит только «последние использованные» значения каждого действия —
`gui_settings.json`, ровно один набор на действие (`GuiSettingsStore`,
`presentation/gui/main_window.py:48`). Переключение между несколькими рабочими
наборами (dev↔prod, staging↔prod) требует перенастройки диалога вручную каждый
раз; хочется сохранять настроенные конфигурации в файл и выбирать из файлов.

**Решение по USER_INPUT (2026-08-31):**
- охват — **все** действия GUI (единый механизм, не только «двухбазовые»);
- хранение — **папка пресетов**, один YAML-файл на пресет, выбор из выпадающего
  списка в GUI;
- **только GUI** (CLI уже покрыт флагами + copy-CLI из GUI);
- подключения — **по имени из `connections/`** (секреты остаются зашифрованными
  там, пресеты секретов не содержат).

**Действие:**
- Папка пресетов по умолчанию `presets/` рядом с `connections/` (вне git, как и
  `connections/`); путь настраивается в `config.yaml` → `paths.presets_dir`.
- Формат файла: обёртка `preset_version` / `action` (action_id) / `name` +
  тело по pydantic-моделям `presentation/gui/actions/models.py`
  (`extra="ignore"` — forward-compat по образцу `gui_settings.json`).
- UX: dropdown пресетов текущего действия (место — action panel или шапка
  диалога, решить в плане) + «Сохранить как…» / «Обновить»; выбранный пресет
  подставляется в диалог через существующий `set_settings()`. Назначение
  `gui_settings.json` не меняется — «последние использованные» значения.
- Валидация при загрузке: подключение с указанным именем существует в
  `connections/`, каталоги существуют; ошибка — видимым статусом, не молча.

**Триггер:** зафиксирован — фидбек пользователя от 2026-08-31.

**Связано:** `presentation/gui/actions/{models,registry,dialogs}.py`,
`presentation/gui/widgets/action_panel.py`,
`infrastructure/config/gui_settings.py`, `config.example.yaml`.

**Кандидат:** Phase 15 (полировка GUI в её составе) или отдельная GUI-задача до
неё — не зависит от CD-17..19.
