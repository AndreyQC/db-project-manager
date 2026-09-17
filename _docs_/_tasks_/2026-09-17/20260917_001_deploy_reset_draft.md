# deploy reset — сброс пользовательских схем целевой БД (draft)

> **Дата:** 2026-09-17
> **Статус:** все решения закрыты пользователем 2026-09-17 (D1–D4, D5, D9,
> планирование — Phase 18); имплементация не начата. Готов к оформлению
> vision/plan Phase 18 после закрытия Phase 17 (post-deploy отчёты CD-16..19).
>
> Контекст:
> - `_docs_/_checkpoints_/20260915_001_checkpoint.md` — текущее состояние (Phase 16 закрыта)
> - `_docs_/_tasks_/TASK_CONVENTIONS.md` — регламент
> - `src/db_project_manager/application/safety_gate_service.py` — data-gate CD-6..CD-10
> - `src/db_project_manager/application/deploy_apply_service.py` — pipeline apply
> - `src/db_project_manager/domain/connection.py` — ConnectionConfig
> - `src/db_project_manager/infrastructure/database/postgres/adapter.py` — `GP_ADMIN_SCHEMAS`, `GET_SCHEMAS`

---

## 1. Проблема

Дев-база Greenplum предназначена для активной разработки: тест-сценарии постоянно
добавляются и меняются. Накопленный «дрейф» (объекты в БД, которых нет в коде;
изменённые таблицы с данными) делает каждый `deploy plan`/`apply` заблокированным:
safety-gate классифицирует такие таблицы как CHANGED/REMOVED с данными
(fail-safe: unknown = есть данные, CD-7) → VIOLATIONS → деплой требует
покрывающих pre-скриптов или ручных обходов. Для дев-БД это неоправданно дорого.

Нужна управляемая команда «сброса до чистого листа»: удалить всё содержимое
пользовательских схем целевой БД, после чего деплой проходит как первичный —
все объекты ADDED, gate CLEAN (CD-10), `--include-drops` и pre-скрипты не нужны.
Дополнительная мотивация — гигиена/безопасность: устаревшие тестовые данные
должны удаляться целиком, а не накапливаться.

Риск, который надо исключить конструктивно: команда максимально деструктивна,
поэтому должна выполняться **только** против подключений, явно помеченных как
допускающие сброс.

## 2. Цели

1. Новая команда CLI `db-pm deploy reset` (+ действие в GUI): полный сброс
   пользовательских схем целевой БД.
2. Пер-подключенческий флаг «разрешить drop-схем» (`allow_drop_schemas`) в YAML
   подключения и чекбокс в диалоге подключения GUI. Команда сброса выполняется
   только при установленном флаге — это главный предохранитель.
3. После сброса следующий `db-pm deploy plan` даёт blocked 0, gate CLEAN;
   `deploy apply` разворачивает кодовую базу «с нуля».
4. Полная трассируемость: отчёт `reset_report.{md,json}` с перечнем удалённого.

## 3. Решения (закрыты пользователем 2026-09-17)

| # | Вопрос | Решение |
|---|--------|---------|
| D1 | Область очистки | **Все не-служебные схемы БД**: и схемы из кодовой базы, и накопленные «мусорные» (тест-сценарии). Исключения: служебная схема деплоя (`deploy.service_schema`, по умолчанию `__deploy`), системные (`pg_%`, `information_schema`), GP-админские (`gp_toolkit`, `gp_statistics*` — константа `GP_ADMIN_SCHEMAS`). Иначе оставшиеся «мусорные» схемы продолжат блокировать safety-gate как REMOVED с данными |
| D2 | Судьба `__deploy` | **Сохранить схему и `schema_version`, очистить journal-таблицы** `script_history`, `script_audit_log` (TRUNCATE). Тогда следующий `deploy apply` выполнит pre/post/seed-скрипты заново на чистой БД; forward-only версия не сбрасывается |
| D3 | Гейт флага | **Флаг гейтит только новую команду сброса.** Семантика `--include-drops` в существующих `deploy plan`/`apply` не меняется |
| D4 | Объём UI | **CLI + GUI**: чекбокс в диалоге подключения, действие «Сброс схем БД» в реестре действий GUI с подтверждением, команда CLI |

Остальные решения (D5–D11) приняты при проработке 2026-09-17; D5 и D9
подтверждены пользователем в тот же день, D6–D8/D10/D11 — техническая база,
обоснованная кодом (может корректироваться на плане фазы без смены смысла):

- **D5. Имя команды** — `db-pm deploy reset` (подтверждено пользователем
  2026-09-17).
- **D6. Флаг — отдельное поле ConnectionConfig**, не в `options`.
  `options` уходит напрямую в `connect_args` psycopg
  (`adapter.py::connect`: `connect_args.update(cfg.options)`) — флаг там
  сломал бы подключение. Поле: `allow_drop_schemas: bool = False` — обратно
  совместимо (отсутствие = false).
- **D7. `--dir` обязателен.** Хотя список жертв при D1 формируется из каталога БД,
  кодовая база нужна для: проверки `db_type` манифест ↔ подключение (как SG-M в
  safety-gate — не даёт сбросить PG-кодовой базой GP-кластер по ошибке) и для
  классификации схем в отчёте («есть в коде» / «только в БД»).
- **D8. Подтверждение с вводом имени базы.** Интерактивный шаг печатает
  host/database/username, полный список схем с числом объектов и требует ввести
  имя базы для подтверждения. `--yes` пропускает подтверждение (CI), `--dry-run`
  печатает план без мутаций.
- **D9. Механика сброса дифференцирована, чтобы сохранить права уровня схем**
  (проблема поднята пользователем 2026-09-17; решение подтверждено им же:
  content-drop для кодовых схем и `public`, полный DROP только для «мусорных»,
  ACL-снапшот до мутаций). Кодовая база не знает о правах вообще: рендер схемы —
  голый `CREATE SCHEMA "<name>";` + `COMMENT` (шаблон `schema.sql.j2`), RE не
  читает ACL (`GET_SCHEMAS` выбирает только `nspname` + комментарий). Значит
  безусловный `DROP SCHEMA ... CASCADE` молча уничтожит GRANT'ы на схемы,
  владельцев (`nspowner`) и `ALTER DEFAULT PRIVILEGES` — и деплой их не
  восстановит. Поэтому:
  - схемы, **присутствующие в кодовой базе**, и `public` — **content-drop**:
    дропаются только объекты внутри, сама схема-оболочка (с ACL, владельцем,
    default privileges) не трогается. Деплой видит её как UNCHANGED и не
    исполняет свой `CREATE SCHEMA` (шаблон без `IF NOT EXISTS` — на существующей
    схеме он бы упал);
  - схемы, которых **нет в кодовой базе** («мусорные»), — `DROP SCHEMA ...
    CASCADE` целиком: они должны исчезнуть, иначе их оболочки станут
    REMOVED-объектами в следующем diff;
  - до любых мутаций — **артефакт-снапшот ACL** (`reset_acl_snapshot.sql`):
    `nspowner` + `nspacl` + `pg_default_acl` всех затрагиваемых схем,
    отрендеренные в `ALTER SCHEMA ... OWNER TO` / `GRANT ...` /
    `ALTER DEFAULT PRIVILEGES`. Страховка для ревью и ручного восстановления.
- **D10. Что сброс НЕ сохраняет (осознанные ограничения):** объектные GRANT'ы
  (права на таблицы/функции) — объекты пересоздаются деплоем «голыми», как и
  любые новые объекты сегодня; владелец пересозданных объектов = deploy-
  пользователь, а не исходный. Полное решение — захват ACL в RE/кодовую базу
  (pg_dump-подход, слот `grant` уже зарезервирован в `TYPE_PRIORITIES`) —
  отдельная архитектурная задача, вне скоупа этой; заведена запись в BACKLOG:
  «P3. Захват ACL (прав/владельцев) в RE и кодовую базу — pg_dump-подход».
- **D11. Мульти-БД архитектура: сейчас Greenplum, дальше — другие СУБД**
  (требование пользователя 2026-09-17). Проверено по коду — проект уже имеет
  нужный каркас, reset встраивается в него без GP-специфики в верхних слоях:
  - **Манифест**: `dbpm.manifest.json` несёт `db_type` (format_version 2),
    `read_manifest` валидирует его по `SUPPORTED_DB_TYPES`
    (`domain/connection.py`) — расширение списка на snowflake/mssql делает
    новый db_type легитимным на уровне данных; `ConnectionConfig.type` уже
    документирует `postgres | greenplum | snowflake | mssql`.
  - **Диспатч**: `registry.get_adapter(cfg)` выбирает адаптер по `cfg.type`
    (сейчас postgres/greenplum → `PGDatabaseAdapter`); комментарий реестра так
    и гласит «New DBMS support = register an adapter here». Reset-методы
    добавляются в ABC `DatabaseAdapter` — новый адаптер обязан их реализовать,
    иначе не пройдёт ABC-контракт.
  - **Проверка db_type**: `manifest.db_type == target_cfg.type` (образец SG-M
    в safety-gate) — уже в D7; она же будущий предохранитель: reset для нового
    типа СУБД невозможен, пока для него нет адаптера.
  - **Сервис без SQL**: `SchemaResetService` работает только с методами
    адаптера и доменными моделями, ни одной SQL-строки/диалектизма в
    application-слое (паттерн Phase 16, LESSONS §70-4, §71-3: GP-знание —
    только внутри адаптера за `_is_greenplum`/каталог-запросами).
  - **Диалектные артефакты**: ACL-снапшот рендерит сам адаптер —
    `snapshot_schema_acls` возвращает SQL в диалекте СУБД (для будущих
    адаптеров — их синтаксис GRANT/OWNER, напр. Snowflake `SHOW GRANTS`).
  - **Деградация по возможностям**: extension-шаг пропускается, если адаптер
    сообщает, что расширений/концепции нет (`list_extensions() -> []`);
    GP-специфика (админ-схемы, external tables) инкапсулирована в
    postgres-адаптере.

## 4. Спецификация

### 4.1. ConnectionConfig

```python
allow_drop_schemas: bool = Field(
    default=False,
    description="Разрешить destructive-команду deploy reset для этого подключения",
)
```

- YAML подключения: `allow_drop_schemas: true`.
- `ConnectionStore.save/load` — без изменений (поле сериализуется через
  `model_dump`; плейнтекст, не секрет).
- GUI `connection_dialog.py`: QCheckBox «Разрешить drop-схем» с tooltip-предупреждением
  («Разрешает команду полного сброса схем этой БД. Только для дев/тест-подключений!»).

### 4.2. CLI

```
db-pm deploy reset \
  --dir <codebase> \
  --target-connection-file connections/<dev>.yaml \
  [--dry-run] [--yes] \
  [--output-dir out] [--no-run-subdir] [--config config.yaml]
```

Пайплайн (порядок важен — отказ до подключения, если можно):

1. Загрузка манифеста кодовой базы; проверка `db_type` ↔ `target_cfg.type`
   (несовпадение → exit 2).
2. **Гейт флага**: `not target_cfg.allow_drop_schemas` → exit 1 с инструкцией
   («установите allow_drop_schemas: true в YAML подключения / чекбокс в GUI»).
   До установления соединения.
3. Подключение; `list_schemas()` минус `service_schema` → список жертв;
   счётчики объектов на схему; классификация «в коде / только в БД».
4. Печать плана + подтверждение (D8) или `--dry-run` → стоп, или `--yes`.
5. Исполнение (см. 4.3), прогресс по схемам.
6. Отчёт `reset_report.{md,json}` в run-dir (`create_run_dir`, как у plan/apply).

Exit-коды: `0` — ok/dry-run; `1` — rejected (флаг не установлен, подтверждение
отклонено); `2` — hard error (манифест, соединение, ошибка DDL).

### 4.3. Application-сервис `SchemaResetService` (`application/schema_reset_service.py`)

- `run(codebase_dir, target_cfg, output_dir, *, dry_run=False, progress) -> ResetResult`
  (подтверждение — ответственность CLI/GUI, сервис получает уже подтверждённый вызов;
  но проверку флага сервис дублирует — defense in depth).
- `ResetResult`: `schemas_wiped: list[str]` (content-drop), `schemas_dropped:
  list[str]` (полный DROP — мусорные), `object_counts: dict[str, int]`,
  `extensions_dropped: list[str]`, `journal_truncated: bool`,
  `in_codebase: set[str]`, `acl_snapshot_path: Path | None`,
  `report_paths: list[Path]`.
- Источник множества «схем кодовой базы» (`in_codebase` — определяет выбор
  механики D9): дешёвый скан каталога — директории верхнего уровня
  `codebase_dir` минус `__migrations`, `settings` и сервисная схема. Граф
  зависимостей для reset не строим (избыточен); манифест уже прочитан на шаге
  db_type-проверки.
- Исполнение (D9), отдельная транзакция на шаг, stop-on-error:
  1. **Снапшот ACL**: `reset_acl_snapshot.sql` по всем затрагиваемым схемам —
     до любых мутаций (в `--dry-run` тоже пишется).
  2. **Расширения**, чьи объекты лежат в сбрасываемых схемах: `DROP EXTENSION
     ... CASCADE` — иначе content-drop упрётся в «cannot drop … because
     extension requires it» (прямой дроп extension-объекта запрещён даже с
     CASCADE). Кодовая база пересоздаёт их через `CREATE EXTENSION IF NOT
     EXISTS` — тип `extension` есть в графе.
  3. **Content-drop** для схем из кодовой базы + `public`: пообъектный
     `DROP <type> "<schema>"."<name>" CASCADE` (квалификация/кавычки —
     `_quote_identifier`; bare names запрещены — конвенция AGENTS.md).
     Перечень объектов — из существующего каталог-чтения
     (`get_database_structure` или лёгкий аналог); порядок не критичен —
     все не-служебные схемы очищаются вместе, residual-зависимости закрывает
     CASCADE, недостающее пересоздаст деплой.
  4. **Полный `DROP SCHEMA "<name>" CASCADE`** — только для схем, которых нет
     в кодовой базе (мусор).
  5. `TRUNCATE TABLE <service_schema>.script_history, <service_schema>.script_audit_log`
     (если `__deploy` отсутствует — WARN и пропуск, это допустимо).
  6. `schema_version` не трогаем (D2).
- Жёсткий guard-список в сервисе И в адаптере: никогда не дропаем
  `pg_%`, `information_schema`, `GP_ADMIN_SCHEMAS`, `service_schema`.

### 4.4. Адаптер (новые методы `DatabaseAdapter` + postgres-реализация)

Все методы — в ABC `DatabaseAdapter` (`infrastructure/database/base.py`),
реализация — в `PGDatabaseAdapter`; будущие адаптеры (snowflake/mssql)
реализуют их в своём диалекте (D11). Сервис и CLI знают только интерфейс.
Объявление — `@abstractmethod` по паттерну проекта: существующие тест-фейки
(`FakeApplyAdapter` / `DeployFakeAdapter` / `FakeAdapter` в
`tests/unit/test_deploy_apply_service.py`, `test_deploy_service.py`,
`test_reverse_engineer.py`) механически дополняются no-op реализациями —
иначе перестанут инстанцироваться.

- `list_schemas() -> list[str]` — обёртка над `GET_SCHEMAS` (уже фильтрует
  `pg_%`/`information_schema`) + `GP_ADMIN_SCHEMAS` на GP (как `_get_schemas`).
- `get_schema_object_counts() -> dict[str, int]` — лёгкий каталог-запрос
  (pg_class/pg_proc/…), только для отчёта и подтверждения.
- `drop_schema(name: str)` — `DROP SCHEMA ... CASCADE` c guard'ом системных имён
  (только для «мусорных» схем, D9).
- `drop_schema_contents(schema: str)` — пообъектный `DROP ... CASCADE` внутри
  схемы (оболочку и ACL не трогает). Перечень объектов — каталог-запрос по
  ВСЕМ дропабельным relkind'ам (`r`,`p`,`v`,`m`,`S`,`f`,`x` + функции/
  процедуры), а не только тем, что читает RE: `GET_TABLES` идёт через
  `information_schema.tables … BASE TABLE` и сегодня НЕ видит GP external
  tables (relkind `x`) — опора на RE-структуру оставила бы их после сброса.
  Перечисляются только top-level дропабельные объекты (таблицы, вью, matview,
  последовательности, функции, процедуры, foreign/external tables) — индексы,
  констрейнты и триггеры уходят каскадом вместе со своими владельцами.
  Внутренняя забота postgres-адаптера (D11).
- `snapshot_schema_acls(schemas: list[str]) -> str` — рендер SQL-снапшота прав:
  `pg_namespace.nspowner`/`nspacl` (через `aclexplode`) + `pg_default_acl` →
  `ALTER SCHEMA … OWNER TO` / `GRANT …` / `ALTER DEFAULT PRIVILEGES`.
  Возвращает готовый SQL в диалекте адаптера.
- `truncate_table(schema: str, name: str)` — для journal-таблиц.
- `list_extensions() -> list[dict]` — публичная обёртка существующего `_get_extensions`
  (нужно имя + схема для решения «дропать extension»); адаптеры СУБД без
  концепции расширений возвращают `[]` — шаг пропускается.

### 4.5. GUI

- Чекбокс флага в `connection_dialog.py` (см. 4.1).
- Действие в `actions/registry.py` + `build_cli_deploy_reset` в `actions/cli.py`:
  настройки (каталог, подключение, dry-run), перед запуском воркера —
  подтверждение QMessageBox с перечнем схем (сервис вызывается в два шага:
  dry-run → показать → подтвердить → исполнить).
- В списке подключений — индикатор флага (иконка/тултип), чтобы «горячие»
  подключения были видны.

## 5. Сценарий использования (дев-цикл)

```bash
# 1. Кодовая база актуальна (reverse-engineer / yaml apply уже сделаны)
db-pm deploy reset --dir ./output/cis_zup --target-connection-file connections/dev_gp.yaml
#    → план: 14 схем (9 из кода, 5 только в БД), 3 extension; подтверждение: cis_zup_dev
# 2. db-pm deploy plan  --dir ... --target-connection-file ...   # blocked 0, gate CLEAN
# 3. db-pm deploy apply --dir ... --target-connection-file ...   # первичный деплой, все ADDED
```

## 6. Проверки (критерии приёмки)

- Юнит-тесты (без БД, mock-адаптер):
  - сброс без флага → rejected до `connect()`, exit 1, сообщение-инструкция;
  - guard-список: `__deploy`/`pg_*`/`information_schema`/`gp_toolkit` не попадают в DROP;
  - дифференцированная механика (D9): схема из кода → только content-drop,
    `drop_schema` не вызывается; мусорная схема → полный `DROP SCHEMA`;
  - ACL-снапшот пишется до мутаций (и в dry-run), покрывает все затрагиваемые схемы;
  - расширения в сбрасываемых схемах → DROP EXTENSION до дропа объектов;
  - TRUNCATE только journal-таблиц, `schema_version` нетронут;
  - dry-run не вызывает мутаций; отчёт содержит схемы/счётчики/классификацию;
  - `ConnectionConfig` без `allow_drop_schemas` в YAML парсится как False;
  - флаг НЕ попадает в `options`/connect_args;
  - мульти-БД контракт (D11): все юнит-тесты сервиса — против фейкового
    адаптера, реализующего только ABC `DatabaseAdapter` (гарантия: в
    application-слое нет PG/GP-специфики и SQL); `manifest.db_type !=
    connection.type` → hard error до коннекта; расширение
    `SUPPORTED_DB_TYPES` не требует правок `SchemaResetService`/CLI/GUI.
- Живой прогон на GP-стенде (по практике Phase 16 — вместо Docker-integration):
  сброс → проверить, что GRANT'ы/владельцы схем из кодовой базы сохранились
  (`\dn+` / `pg_namespace.nspacl`) → plan (blocked 0, CLEAN) → apply (схемы
  UNCHANGED, объекты ADDED) → повторный reset идемпотентен.
- `uv run pytest` зелёный, `uv run ruff check .` чистый.

## 7. Риски и открытые вопросы

| Риск | Митигция |
|------|----------|
| Потеря прав уровня схем (GRANT/OWNER/DEFAULT PRIVILEGES) при сбросе | D9: content-drop сохраняет оболочки схем с их ACL; полный DROP — только для «мусорных» схем, которых нет в коде; артефакт `reset_acl_snapshot.sql` до мутаций |
| Объектные GRANT'ы и исходные владельцы объектов теряются при пересоздании | Осознанное ограничение (D10) — поведение эквивалентно сегодняшнему деплою новых объектов; долгосрочное решение — захват ACL в RE (запись в BACKLOG, P3) |
| Extension-объекты в сбрасываемых схемах блокируют дроп содержимого | DROP EXTENSION первым шагом (4.3); пересоздание деплоем из кодовой базы |
| `public`: схема де-факто «общая», дропать её целиком нельзя | `public` всегда content-drop (D9); синхронно с бэклог-записью P2 «исключение public из compare» |
| Ошибка «cannot drop … extension requires it» при пообъектном дропе | Тот же шаг DROP EXTENSION; перечень объектов фильтруется по принадлежности расширениям |
| Права: не-владелец объекта → ошибка DROP | stop-on-error + полный отчёт; дев-БД обычно под владельцем/суперпользователем |
| Долгий пообъектный DROP на GP (сегменты) | прогресс по схемам; предупреждение в выводе |
| Человеческий фактор («не на ту базу») | флаг подключения (главный) + подтверждение вводом имени базы + dry-run + отчёт |
| Планирование: задача назначена на Phase 18; Phase 17 (post-deploy отчёты CD-16..19) идёт раньше | порядок фаз зафиксирован пользователем 2026-09-17 |

## 8. Оценка объёма (шаги Phase 18)

1. `ConnectionConfig.allow_drop_schemas` + тесты (S).
2. Методы адаптера (`list_schemas`, `get_schema_object_counts`, `drop_schema`,
   `drop_schema_contents`, `snapshot_schema_acls`, `truncate_table`,
   `list_extensions`) + тесты (M/L — ACL-рендер самый объёмный) + no-op
   доопределения в 3 существующих тест-фейках (см. §4.4).
3. `SchemaResetService` + отчёт + тесты (M-L).
4. CLI `deploy reset` + подтверждение (M).
5. GUI: чекбокс диалога, действие, подтверждение (M).
6. README/AGENTS-упоминание, чекпойнт (S).
