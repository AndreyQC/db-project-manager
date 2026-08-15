# Phase 10: CD Foundation — драфт (vision draft)

> **Дата:** 2026-08-11
> **Ветка:** dev
> **Статус:** draft (после закрытия USER_INPUT → `_final`, затем `_plan` → реализация)
>
> Контекст:
> - `_checkpoints_/20260804_001_checkpoint.md` — текущее состояние (Phase 14 done)
> - `_tasks_/ROADMAP.md` §2, шаг 2 (Phase 10) и §4 (направление B — Controlled Deployment)
> - `_tasks_/ROADMAP.md` §3 (safety principle), §7 (правила безопасности), §8 (целевой пайплайн)
> - `_tasks_/ROADMAP.md` §9 — USER_INPUT Q3 (pre/post идемпотентность), Q4 (git-tag vs commit), Q5 (migrations/ location)
> - `src/db_project_manager/application/deploy_service.py` — текущий stateless deploy
> - `src/db_project_manager/infrastructure/database/base.py` — контракт `DatabaseAdapter` (9 abstract methods)
> - `src/db_project_manager/infrastructure/graph/topological_sort.py` — `sort_by_type_and_topology`
> - `src/db_project_manager/presentation/cli/main.py:361-422` — `db-pm deploy validate`
> - `src/db_project_manager/infrastructure/config/app_config.py` — `CFG` (`extra="ignore"`)
> - `src/db_project_manager/infrastructure/config/codebase_manifest.py` — прецедент версионированного JSON-артефакта
> - `LESSONS_LEARNED.md` §12 (git add для `-`-каталога), §18/§45 (расширение ABC ломает fakes), §19 (DDL whitelist), §23 (strip autodoc), §28 (PyYAML numeric quoting), §32 (locale fallback), §34/§35 (fully-qualified DDL)

---

## 1. Постановка проблемы

Phase 2 научила инструмент **validation deploy**: применить кодовую базу в пустую
временную БД (`<prefix>_<server-UTC-timestamp>`), проверить что DDL исполняется без
ошибок, удалить temp-БД (`deploy_service.py:113-234`). Это проверка «в вакууме»: схема
корректна синтаксически и непротиворечива по зависимостям. Но этот деплой **stateless** —
каждый прогон начинает с нуля, у БД нет памяти о том, что и когда на неё деплоилось.

Для реального CD-пайплайна (ROADMAP §8) этого недостаточно:

1. **Нет состояния версии.** `schema_version` — что уже применено, какая версия source
   (`dbpm.manifest.json:source_version`, calver `YYYY.MM.DD.NN`). Без него нельзя сравнить
   «текущая версия БД» с «источником» (CD-2) и тем более обеспечить правило «запрет деплоя,
   если target новее source» (ROADMAP §8 шаг 2).
2. **Нет истории скриптов.** `script_history` — какие pre/post-скрипты выполнялись, с каким
   checksum, успешно ли. Без неё идемпотентность (CD-4: «то же имя + тот же checksum +
   success → пропуск») невозможна: каждый прогон гонит все скрипты заново.
3. **Нет pre/post runner.** `_deploy_object` (`deploy_service.py:238-259`) деплоит только
   вершины графа. Pre/post-скрипты (единственный легальный способ менять таблицы с данными
   — ROADMAP §7 правило 5) некуда положить и некому исполнить.
4. **Нет контракта на целевую БД.** Весь flow завязан на `create_database` + `drop_database`
   вокруг temp-БД. Понятия «deploy into existing DB» не существует.

Phase 10 — **фундамент** CD-ядра (10→13 по ROADMAP §2): заводит служебную схему,
версионирование и pre/post runner как **механику**. Real-target deploy (в существующую
БД), safety-gate и ALTER — приходят позже (Phase 11/12). Phase 10 тестируется в рамках
**validate-flow**: temp-БД получает `__deploy` → механика прогоняется end-to-end → temp-БД
удаляется. Это позволяет проверить `__deploy`/versioning/pre-post без новых рисков
(урок §32: locale fallback на `template0` уже отлажен; temp-БД — безопасный полигон).

Зависимости: Phase 10 — шаг 2, зависит от Phase 8 (✓ done: overload resolution чинит
топосорт деплоя). Phase 11 (Safety Gate) зависит от Phase 10.

## 2. Цель фазы

1. **Служебная схема `__deploy`** с таблицами версионирования: `schema_version`,
   `script_history` (CD-1). `script_watermark` — опционально (см. USER_INPUT CDF-5).
2. **Версионирование**: чтение текущей версии БД (`schema_version.version`), сравнение с
   `source_version` из manifest (calver), запрет деплоя если target новее source,
   предупреждение при отставании (CD-2).
3. **Discovery pre/post-скриптов** в `__migrations/{pre,post}/` с сортируемыми именами
   `YYYY-MM-DD_NNN_description.sql` (CD-3).
4. **Идемпотентный pre/post runner**: checksum-based skip/error, запись в `script_history`
   (CD-4).
5. **Документированное требование идемпотентности** pre/post-скриптов с примерами (CD-5,
   Should).
6. **Интеграция в validate-flow**: temp-БД получает `__deploy`, механика versioning +
   pre/post прогоняется end-to-end (без real-target deploy — это Phase 11+).

## 3. Принятые решения (закрыты через Q&A с пользователем — см. §8)

| Развилка | Решение | Обоснование |
|---|---|---|
| Pre/post: идемпотентны всегда или run-once (ROADMAP Q3) | **см. USER_INPUT CDF-1 (ЗАКРЫТО)** | Решение: **всегда идемпотентны**; run-once — только через явный флаг. Соответствует ROADMAP §7 правило 5. |
| Версия: источник и схема (ROADMAP Q4) | **см. USER_INPUT CDF-2 (ЗАКРЫТО)** | Решение: **manifest `source_version`** (calver `YYYY.MM.DD.NN`, см. CDF-9), required, без git-зависимости. Версия explicit и MR-контролируемая (видна в PR reviewer'ом), не выводится из git-state в runtime. `format_version` bump 1→2. |
| Расположение `__migrations/` (ROADMAP Q5) | **см. USER_INPUT CDF-3 (ЗАКРЫТО)** | Решение: **`<output>/<db>/__migrations/{pre,post}/`** (с `__`-префиксом, консистентно с `__deploy`). Recommendation было без `__`; пользователь уточнил — служебные каталоги должны визуально отличаться от user-схем. |
| Имя служебной схемы | **см. USER_INPUT CDF-4 (ЗАКРЫТО)** | Решение: **configurable с default `__deploy`** (опция `deploy.service_schema` в `config.yaml`). Хардкод ломает коллизию с user-схемой `__deploy`; configurable решает. |
| `__deploy`: «магия» в adapter или явная схема в коде | **см. USER_INPUT CDF-10 (ЗАКРЫТО)** | Решение: **явная схема в кодовой базе** (`.sql` файлы с autodoc, deploy применяет через стандартный `EARLY_DDL_TYPES`). Противоположно изначальному draft'у (`ensure_deploy_schema` в adapter) — всё явно, MR-controlled, в духе проекта. |
| `__deploy`-обязательность при deploy | **Hard error если нет** (CDF-10/CDF-7) | Deploy падает, если в кодовой базе отсутствует `__deploy`-схема. Seed делается reverse-engineer'ом (§4.7). CDF-7 закрыт неявно (вытекает из CDF-10). |
| Canonical-DDL-валидация при deploy | **Warning при mismatch** (CDF-10, подход b) | db-pm знает canonical-DDL `schema_version`/`script_history` (embedded); сравнивает SHA-256 с кодовой базой; mismatch → warning (не блокирует). Ловит рассинхрон раньше runtime-ошибок. Checksum — без autodoc-секции (см. CDF-6). |
| `immutable`-флаг в autodoc | **В секции `project`, omit при false** (CDF-10) | Расширение Phase 1 контракта: `Vertex.immutable: bool = False`; reverse-engineer ставит `True` для объектов служебной схемы. Regenerate перезаписывает `immutable`-файлы всегда. См. §4.8. |
| `script_history` vs audit | **state + history разделены** (пользователь) | `script_history` — PRIMARY KEY `(name, type)`, UPSERT, одна запись (быстрый lookup для runner'а, линейная логика). `script_audit_log` — append-only, INSERT каждый прогон, несёт `deploy_version`/`deploy_source` для отчётов Phase 13 (CD-17). `record_script_execution` атомарно пишет в обе. См. §4.1, §4.4. |
| Объём Phase 10 | **Фундамент: механика в validate-flow + расширение RE** | Real-target deploy (в существующую БД) отложен до Phase 11+; Phase 10 тестирует механику на temp-БД И добавляет seed/sync `__deploy` в reverse-engineer (для замкнутого цикла). Снижает риск, позволяет end-to-end проверку без нового контракта. |
| Где живут новые domain-модели | **`domain/deploy.py`** (новый) | Текущие deploy-типы — dataclass'ы в `deploy_service.py:60-86`. Version/run/plan-модели — pydantic, отдельный модуль (по образцу `domain/diff.py`). |
| Расширение `DatabaseAdapter` | **4 новых abstract methods** (без `ensure_deploy_schema`) | `get_schema_version`, `record_schema_version`, `get_script_history`, `record_script_execution` — 4 метода (DDL убран в кодовую базу). Обновить ВСЕ fakes (урок §45: `DeployFakeAdapter`, `FakeAdapter`). |
| Checksum скрипта | **см. USER_INPUT CDF-6 (ЗАКРЫТО)** | Решение: **SHA-256 от canonical-normalized текста**, БЕЗ autodoc-секции (checksum от executable SQL, не metadata). Сначала strip autodoc, потом normalize (whitespace/line-endings). |
| `script_watermark` (CD-1 опционально) | **см. USER_INPUT CDF-5 (ЗАКРЫТО)** | Решение: **отложить** — `schema_version` + `script_history` покрывают MVP; watermark избыточен до real-target deploy. |
| Идемпотентность `CREATE` в `__deploy`-DDL | **см. USER_INPUT CDF-8 (ЗАКРЫТО)** | Решение: **`IF NOT EXISTS`** (PostgreSQL 9.3+). Один round-trip, атомарно, не гонка. Идемпотентный re-deploy и safe regenerate. |

## 4. Архитектура (для последующего `_plan`)

### 4.1. Служебная схема `__deploy` — обычная схема в дереве кода

> Решение пользователя (см. USER_INPUT CDF-10): `__deploy` описывается как обычная схема в
> дереве кода (`.sql` файлы с autodoc), не создаётся «магией» в adapter. Deploy требует её
> наличия в кодовой базе (hard error если нет). Подход явный, MR-controlled, в духе проекта.

`__deploy` состоит из трёх файлов в дереве кода (рядом с user-схемами):

```
<codebase_dir>/
├── dbpm.manifest.json
├── __deploy/
│   └── tables/
│       ├── schema_version.sql     # CREATE TABLE ... + autodoc header
│       ├── script_history.sql     # state: одна запись на (name, type)
│       └── script_audit_log.sql   # append-only история всех попыток
├── bookings/...
├── app/...
└── __migrations/{pre,post}/...
```

**`__deploy/tables/schema_version.sql`** (содержимое файла, с autodoc):

```sql
[[<[autodoc-yaml]
object:
  object_key: pg_database/<db>/schema/__deploy/table/schema_version
  object_type: table
  object_schema: __deploy
  object_name: schema_version
project:
  build: true
  immutable: true
[autodoc-yaml]>]]

CREATE TABLE IF NOT EXISTS "__deploy"."schema_version" (
    id              SERIAL PRIMARY KEY,
    version         TEXT NOT NULL,         -- calver 'YYYY.MM.DD.NN' из manifest
    applied_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    source          TEXT NOT NULL          -- 'validate' | 'deploy' | 'manual'
);
```

**`__deploy/tables/script_history.sql`** (state — одна запись на `(name, type)`, UPSERT):

```sql
[[<[autodoc-yaml]
object:
  ...
project:
  build: true
  immutable: true
[autodoc-yaml]>]]

CREATE TABLE IF NOT EXISTS "__deploy"."script_history" (
    script_name     TEXT NOT NULL,         -- '2026-08-11_001_add_column.sql'
    script_type     TEXT NOT NULL,         -- 'pre' | 'post'
    checksum        TEXT NOT NULL,         -- SHA-256 hex последнего выполнения
    success         BOOLEAN NOT NULL,
    executed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    error_message   TEXT,                  -- NULL при success=TRUE
    duration_ms     INTEGER NOT NULL,
    PRIMARY KEY (script_name, script_type)   -- одна запись на имя (state, не история)
);
```

**`__deploy/tables/script_audit_log.sql`** (append-only история всех попыток):

```sql
[[<[autodoc-yaml]
object:
  ...
project:
  build: true
  immutable: true
[autodoc-yaml]>]]

CREATE TABLE IF NOT EXISTS "__deploy"."script_audit_log" (
    id              SERIAL PRIMARY KEY,
    script_name     TEXT NOT NULL,
    script_type     TEXT NOT NULL,         -- 'pre' | 'post'
    checksum        TEXT NOT NULL,
    success         BOOLEAN NOT NULL,
    error_message   TEXT,
    duration_ms     INTEGER NOT NULL,
    executed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    deploy_version  TEXT,                  -- source_version деплоя (для отчётов Phase 13)
    deploy_source   TEXT                   -- 'validate' | 'deploy' | 'manual'
);
```

- **`IF NOT EXISTS`** → идемпотентность при повторном reverse-engineer/regenerate и при
  re-deploy (урок §35: fully-qualified, double-quoted).
- **`immutable: true`** в autodoc (секция `project`, omit при false — см. §4.8) — признак
  того, что файл управляется db-pm, пользователь не должен редактировать.
- **Seed при первом reverse-engineer** (БД без `__deploy`): db-pm инжектирует эти файлы из
  встроенных шаблонов (по образцу `infrastructure/sql/templates/`). См. §4.7.
- **Reverse-engineer из БД с `__deploy`**: реверсит как обычную схему + автоматически ставит
  `immutable: true` по имени служебной схемы (configurable, CDF-4).
- **Deploy**: проверяет наличие `__deploy` в коде → применяется через стандартный
  `EARLY_DDL_TYPES` flow (schema → table, топосорт). Никакого `ensure_deploy_schema` в
  adapter (см. §4.6).
- **Структура-валидация при deploy** (CDF-10, подход b): db-pm знает canonical-DDL
  `schema_version`/`script_history` (SHA-256 от normalized DDL), сравнивает с тем, что в
  кодовой базе; mismatch → warning (не блокирует). Ловит рассинхрон (пользователь всё-таки
  отредактировал, или БД со старой структурой db-pm) раньше runtime-ошибки.
- `schema_version.version` — calver `YYYY.MM.DD.NN` (см. §4.2, CDF-9 обновлён).
- `schema_version` — append-only: новая строка на каждый деплой. Текущая версия = строка с
  `MAX(applied_at)` (или `MAX(id)`).
- `script_history` — **state**, не история: одна запись на `(script_name, script_type)`
  (PRIMARY KEY), UPSERT при каждом прогоне. Оптимизирована под lookup runner'а (§4.4):
  один SELECT → одна строка → линейная логика skip/error/execute.
- `script_audit_log` — **append-only история** всех попыток: INSERT при каждом выполнении,
  никогда не UPDATE. `deploy_version`/`deploy_source` связывают выполнение с конкретным
  деплоем → прямые JOIN'ы в отчётах Phase 13 (CD-17). Runner эту таблицу НЕ читает в flow —
  только пишет.
- Разделение **state vs history**: `script_history` отвечает «какой контент сейчас применён»
  (быстро, для логики), `script_audit_log` — «что происходило» (для расследований и отчётов).
- `source` / `deploy_source` различают validate-deploy (temp-БД) от real-deploy (Phase 11+)
  — полезно для аудита, не для логики.

### 4.2. Версионирование через `dbpm.manifest.json`

> USER_INPUT CDF-2 закрыт пользователем: версия БД — explicit поле `source_version` в
> `dbpm.manifest.json`, схема calver `YYYY.MM.DD.NN`, **обязательное**, **без git-зависимости**.
> Альтернатива (git-commit/tag через subprocess) отвергнута: manifest explicit + MR-controlled.

**Расширение manifest** (additive, `format_version` bump 1→2). Текущая схема —
`domain/diff.py:48-60`, `codebase_manifest.py`:

```json
{
  "db_type": "postgres",
  "database": "bookings_demo",
  "generated_at": "2026-07-28T12:34:56+00:00",
  "tool_version": "0.1.0",
  "format_version": 2,
  "source_version": "2026.08.11.01"
}
```

- `source_version` — calver `YYYY.MM.DD.NN`: 4-значный год, 2-значный месяц (01–12),
  2-значный день (01–31), 2-значный zero-padded номер релиза в дне. Regex валидации:
  `^\d{4}\.(0[1-9]|1[0-2])\.(0[1-9]|[12]\d|3[01])\.\d{2}$`. Примеры: `2026.08.11.01`,
  `2026.08.11.02`, `2026.08.11.99`. Лексикографическое сравнение строк = хронологическое —
  все компоненты fixed-width (4/2/2/2). Несколько релизов в день (hotfix, errata) —
  реальный сценарий.上限 99 релизов в день. Seed при первом RE: `YYYY.MM.DD.01`, где DD —
  день RE. См. USER_INPUT CDF-9 (закрыт).
- **Обязательное**: manifest без `source_version` → `ManifestError` («добавьте source_version
  в dbpm.manifest.json»). Старые manifest'ы (`format_version=1`) — regenerятся через
  reverse-engineer (который seed'ит `source_version`, §4.7); для ручного ввода — явное
  редактирование.
- **Кто обновляет**: разработчик правит `source_version` вручную в MR перед деплоем новой
  ревизии (bump номера: `.01` → `.02`). Это и есть MR-контроль: bump версии = осознанный
  шаг, видимый в PR review. db-pm НЕ bump'ает автоматически (это сломало бы принцип
  explicit-version). При reverse-engineer БД с `__deploy` — версия sync'ается автоматически
  (§4.7), без ручного ввода.
- **Git полностью убран**: нет subprocess-вызовов `git`, нет полей `git_commit`/`git_tag` ни в
  manifest, ни в `schema_version`. Если reviewer'у нужен git-context — он видит его в MR
  (история коммитов, PR description).

**Сравнение версий (CD-2):**
- Прочитать `current = get_schema_version()` (MAX-строка из `schema_version`, calver-строка);
  если таблица пуста (первый deploy) → `current = None`.
- Прочитать `source = manifest.source_version` (calver-строка).
- `current is None` → первый deploy, proceed.
- `current == source` → «уже применено», no-op deploy (warning «source_version совпадает с
  текущей — вероятно, забыли bump в manifest»).
- `current > source` (лексикографически, fixed-width calver) → **запрет деплоя**
  (`DeployError` «target новее source: откат запрещён, ROADMAP §7 правило 8 forward-only»).
- `current < source` → proceed (нормальный forward-deploy).

**Bump discipline**: если разработчик забыл bump → `current == source` → no-op + warning.
Не блокирует, но привлекает внимание (явное сообщение в логе и в progress-callback). Это
компромисс: жёсткая блокировка (`DeployError`) была бы строже, но ломает re-deploy той же
ревизии для recovery; MVP выбирает мягкий warning.

`--require-tag` (из исходной формулировки Q4) — **больше не нужен**: calver сам по себе
играет роль «released version». Production-строгость достигается branch-protection в git
(напр. require MR для bump'а `source_version`) — это вне scope инструмента.

### 4.3. Discovery pre/post-скриптов

`__migrations/` живёт рядом с деревом схемы (CDF-3 ЗАКРЫТ):
`<codebase_dir>/__migrations/{pre,post}/`. Префикс `__` — консистентно с `__deploy`,
визуально отличает служебный каталог от user-схем. Если каталога нет → нет скриптов, runner
пропускается (не ошибка).

**Соглашение об именах** (CD-3): `YYYY-MM-DD_NNN_description.sql`, где
- `YYYY-MM-DD` — дата (сортировка),
- `NNN` — трёхзначный порядковый номер внутри дня,
- `description` — человекочитаемое описание (snake_case).

Discovery: `sorted(glob('__migrations/pre/*.sql'))` → deterministic порядок по имени.
Валидация имени: regex `^\d{4}-\d{2}-\d{2}_\d{3}_.+\.sql$`; нарушение → warning (не error,
чтобы не ломать на legacy-скриптах). Дубликат `(имя, checksum)` в истории → skip (§4.4).

### 4.4. Pre/post runner (идемпотентное выполнение)

Для каждого скрипта (из `__migrations/{pre,post}/`) по порядку:

1. Прочитать текст, посчитать `checksum = sha256(canonical_normalize(strip_autodoc(text)))`
   (CDF-6 ЗАКРЫТ: checksum от executable SQL, **без** autodoc-секции).
2. `existing = get_script_history(script_name, script_type)` — **одна запись или None**
   (PRIMARY KEY `(script_name, script_type)`, §4.1).
3. Линейная логика (без итерации по списку):

   ```
   if existing is None:
       → EXECUTE                          # новый скрипт (никогда не выполнялся)
   elif existing.checksum == current and existing.success:
       → SKIP                             # идемпотентность — уже применён успешно
   elif existing.checksum == current and not existing.success:
       → ERROR                            # тот же контент уже падал — не повторять вслепую
   elif existing.checksum != current:
       → ERROR                            # CD-4: имя то же, контент изменился — ambiguous
   ```

4. При EXECUTE: `execute_script(strip_autodoc_if_any(text))`, затем `record_script_execution`
   — **атомарно** (одна транзакция):
   - **UPSERT** в `script_history` (state — обновили «что сейчас применено»);
   - **INSERT** в `script_audit_log` (history — добавили «что произошло»).
5. При error → stop (по умолчанию) или continue (`--continue-on-error`, как в validate).
   Ошибка тоже пишется в обе таблицы (success=FALSE, error_message) — для future-retry
   логики и audit'а.

`canonical_normalize` (CDF-6 ЗАКРЫТ):
1. **Сначала strip autodoc-секции** (`[<[autodoc-yaml]...[autodoc-yaml]>]`) — checksum
   считается от executable SQL, не от metadata. Если у pre/post-скрипта появится autodoc
   (напр. описание/теги в будущем), изменение metadata не должно менять checksum.
   Используется существующий `_strip_autodoc` (`deploy_service.py:261-273`).
2. Затем normalize: strip trailing whitespace на строках, удалить blank-строки,
   нормализовать line-endings к `\n`. SQL-комментарии (`-- ...`) НЕ удаляются (они могут
   быть значимы; MVP без AST-нормализации).

Примечание: pre/post-скрипты в `__migrations/` сейчас — plain SQL пользователя (без
autodoc), но checksum-логика унифицирована с canonical-DDL-валидацией `__deploy` (§4.6),
где autodoc есть всегда — поэтому strip делается всегда (no-op если autodoc'а нет).

Порядок в пайплайне (ROADMAP §8): pre-скрипты **до** объектов дельты, post-скрипты **после**.
В Phase 10 (validate-flow) это: validate-`__deploy`-presence → version-check → pre-scripts →
объекты графа (включая `__deploy`-схему и таблицы из кодовой базы) → post-scripts → record version.

### 4.5. Интеграция в validate-flow

Phase 10 **не вводит** real-target deploy (в существующую БД). Механика `__deploy` +
versioning + pre/post прогоняется **внутри temp-БД** validate-flow:

```
DeployValidateService.run():
  1-5. [существует] create temp-DB, reconnect
  6. НОВОЕ: validate __deploy presence        # CDF-10: __deploy обязана быть в кодовой базе
  7. НОВОЕ: canonical-DDL check (warning)     # CDF-10 подход (b): SHA-256 vs код db-pm
  8. НОВОЕ: version-check                     # source vs пустая temp-БД → proceed
  9. НОВОЕ: pre-runner (__migrations/pre/)    # если каталог есть
  10. [существует] deploy vertices (топосорт)  # __deploy-схема/таблицы идут через EARLY_DDL
  11. НОВОЕ: post-runner (__migrations/post/)
  12. НОВОЕ: record_schema_version(source)
  13. [существует] finally: drop temp-DB
```

**Ключевая разница от предыдущей редакции draft'а:** `__deploy` НЕ создаётся через
`ensure_deploy_schema()` в начале — она применяется как обычная схема из кодовой базы на
шаге 10 (через `EARLY_DDL_TYPES`: schema → table, топосорт). До этого шага проверяется лишь
её наличие (шаг 6) и структура (шаг 7). Version-check (шаг 8) читает `schema_version` ПОСЛЕ
её создания на шаге 10? — нет, на пустой temp-БД её ещё нет. Поэтому на шаге 8 для temp-БД
`current = None` (первый deploy), proceed; на шаге 12 — после применения объектов —
`record_schema_version` пишет версию.

Это **сквозной тест механики** на безопасном полигоне: если `__deploy`-DDL ломается на
locale/поиске (урок §32), если checksum-логика даёт сбой, если pre/post-порядок неверен —
всё всплывёт в temp-БД, не на проде. Фикс codebase fixture (`tests/fixtures/codebase_sample/`)
добавляет `__migrations/pre/` + `__migrations/post/` с 1-2 идемпотентными скриптами И
`__deploy/tables/{schema_version,script_history}.sql` (с `immutable: true` в autodoc).

Real-target deploy (deploy без temp-БД, в существующую схему) — **Phase 11+**, отдельная
CLI-команда (`db-pm deploy run`?), отдельный flow. Phase 10 заводит только механику и
контракт (`DatabaseAdapter`-методы, domain-модели), переиспользуемый позже.

### 4.6. Расширение `DatabaseAdapter`

4 новых abstract methods (по образцу Phase 9 `get_table_row_counts`, `base.py:86-87`).
Метод `ensure_deploy_schema` **НЕ добавляется** — `__deploy` применяется через стандартный
deploy-mechanic из `.sql` файлов в кодовой базе (решение пользователя, §4.1):

| Method | Signature | Назначение |
|---|---|---|
| `get_schema_version` | `(schema_name: str) -> str \| None` | Последняя `version` (calver) из `schema_version`, или None если пусто |
| `record_schema_version` | `(schema_name: str, version: str, source: str) -> None` | INSERT в `schema_version` (append-only) |
| `get_script_history` | `(schema_name: str, script_name: str, script_type: str) -> ScriptRecord \| None` | Одна запись (state) по `(name, type)` или None |
| `record_script_execution` | `(schema_name: str, record: ScriptRecord, deploy_version: str, deploy_source: str) -> None` | **Атомарно**: UPSERT в `script_history` + INSERT в `script_audit_log` (одна транзакция) |

Все методы работают с **calver-строкой** (`2026.08.11.01`), не с git-полями — git полностью
убран из контракта (USER_INPUT CDF-2 закрыт).

`record_script_execution` принимает `deploy_version`/`deploy_source` — они пишутся ТОЛЬКО в
`script_audit_log` (для отчётов Phase 13, CD-17). `script_history` их не хранит (state не
нуждается в ссылке на деплой — там только «что сейчас применено»).

**Контрактные модели** (новый `domain/deploy.py`, pydantic): `ScriptRecord` (исполнение
pre/post-скрипта: `script_name, script_type, checksum, success, error_message, duration_ms`).
`SchemaVersion` как отдельная модель не нужна — `version` это просто строка calver. По образцу
`domain/diff.py` (Phase 9).

**Реализация в `PGDatabaseAdapter`** (`postgres/adapter.py`): SQL-константы в
`postgres/queries.py` (по образцу существующих `GET_*`). `record_script_execution` —
`INSERT ... ON CONFLICT (script_name, script_type) DO UPDATE` для history + отдельный INSERT
для audit_log, в одной явной транзакции (`BEGIN`/`COMMIT`, не autocommit). DDL-идентификаторы
— double-quoted, fully-qualified (урок §35).

**Canonical-DDL-валидация** (CDF-10, подход b; CDF-6 — checksum без autodoc): db-pm содержит
embedded canonical DDL **трёх** таблиц — `schema_version`/`script_history`/`script_audit_log`
(в `infrastructure/sql/templates/deploy/` или `infrastructure/deploy/canonical_ddl.py`). При
deploy читается DDL из кодовой базы (`__deploy/tables/*.sql`), **strip autodoc-секции**
(унифицировано с pre/post-checksum, §4.4), нормализуется, сравнивается SHA-256 с canonical.
Mismatch → warning в лог/progress-callback (не блокирует deploy). Это ловит рассинхон
(пользователь отредактировал DDL, или reverse-engineer из БД со старой структурой db-pm)
раньше runtime-ошибок `record_schema_version`/`record_script_execution` (нет колонки).
Checksum стабилен при изменении только autodoc (напр. db-pm добавил комментарий в metadata)
— изменения metadata не должны сигнализировать о рассинхоне DDL.

**Урок §45:** расширение ABC ломает ВСЕ test-fakes. В одном коммите обновляем:
`DeployFakeAdapter` (`test_deploy_service.py:26`), `FakeAdapter`
(`test_reverse_engineer.py:26`). Fake'и хранят state в `dict` (`__script_history` keyed by
`(name, type)`, `__script_audit_log` как list), эмулируют version/history/audit.

### 4.7. Seed и sync `__deploy` через reverse-engineer

Reverse-engineer становится ответственным за создание/поддержание `__deploy` в дереве
кодовой базы. Это замыкает цикл БД ↔ codebase:

```
[1] RE БД без __deploy (первый импорт / legacy)
    → seed: инжектировать __deploy/tables/{schema_version,script_history,script_audit_log}.sql
      из встроенных шаблонов db-pm
    → autodoc immutable:true для этих файлов
    → manifest source_version = YYYY.MM.DD.01 (DD = день RE)

[2] deploy применяет __deploy через стандартный EARLY_DDL + record_schema_version

[3] RE БД с __deploy (повторный, после деплоев)
    → реверсит __deploy как обычную схему (через GET_TABLES) — все 3 таблицы
    → autodoc immutable:true автоматически по имени служебной схемы (configurable, CDF-4)
    → manifest source_version = прочитанная из __deploy.schema_version (sync БД → codebase)

[4] пользователь меняет кодовую базу, bumps source_version вручную (2026.08.11.01 → .02)

[5] deploy записывает новую версию → цикл на [3]
```

**Точки расширения в существующем коде:**
- `application/reverse_engineer.py`: после `adapter.get_database_structure()` — проверка
  наличия `__deploy` в схемах; если нет → инжектировать seed-вершины (schema + **3 tables**)
  с `immutable=True` перед генерацией дерева. Если есть — обычный reverse + post-processing
  (проставить `immutable=True` по имени схемы).
- `infrastructure/sql/autodoc.py` (`autodoc.py:16,87`): расширение `project`-секции —
  добавить `immutable` поле (см. §4.8).
- `infrastructure/sql/templates/`: новый каталог `deploy/` с `schema_version.sql.j2`,
  `script_history.sql.j2`, `script_audit_log.sql.j2` (seed-шаблоны, по образцу существующих
  шаблонов).
- `domain/graph.py:38-66` (`Vertex`): новое поле `immutable: bool = False` (по образцу
  существующего `build: bool = True`). Парсер autodoc читает его.

**Edge-cases:**
- RE БД без `__deploy`, но существующий manifest уже содержит `source_version` (повторный RE
  в тот же день) → **сохранять** существующую версию, не пересоздавать seed `.01`.
- RE БД с `__deploy`, но `schema_version` пустая (схема создана, версии не записаны) →
  warning + fallback на seed `YYYY.MM.DD.01`.
- RE БД с `__deploy`, но структура не совпадает с canonical db-pm (старая версия db-pm) →
  реверсится как есть + warning (canonical-mismatch, как в deploy, §4.6). Пользователь решает:
  регенерировать seed через `db-pm reverse-engineer --reseed-deploy` (опция plan'а) или
  оставить как есть.

### 4.8. Autodoc-поле `immutable` в секции `project`

> Решение пользователя: поле живёт в секции `project` autodoc (рядом с `build`),
> указывается **явно только при `true`**; `false` — default, поле опускается для краткости.

Расширение Phase 1 контракта. Текущий формат (`infrastructure/sql/autodoc.py:16,87`):

```yaml
# обычный объект (95% файлов)
object:
  object_key: ...
  ...
project:
  build: true
```

```yaml
# __deploy-объект (после расширения)
object:
  object_key: ...
  ...
project:
  build: true
  immutable: true    # только когда true; при false — поле отсутствует
```

**Поведение:**
- **Vertex.immutable** (`domain/graph.py`, новое поле `bool = False`): парсится из autodoc
  при graph build; `False` по умолчанию (поле отсутствует → `False`).
- **Reverse-engineer**: ставит `immutable=True` для всех объектов в служебной схеме (по
  configurable имени, CDF-4) — независимо от того, seed'ил он их или реверсил из БД.
- **GUI**: lock-иконка на узле дерева, read-only preview. Встроенного редактора сейчас нет,
  но флаг готов к будущему (если появится — `immutable`-файлы открываются read-only).
- **Regenerate** (повторный reverse-engineer): `immutable`-файлы **всегда
  перезаписываются** из шаблона/реверса — пользовательские правки (если он их всё-таки
  сделал) не сохраняются. Это мягкая защита (см. §4.6 canonical-валидация для жёсткой).
- **Deploy**: `immutable`-объекты применяются как обычные (deploy не различает их). Это
  ответственность пользователя — не коммитить правки `__deploy`-файлов (canonical-валидация
  дает warning при рассинхроне).
- **Compare (Phase 9)**: `immutable`-объекты участвуют в compare как обычные. Опция
  «исключить служебные схемы» — backlog (если окажется шумно).

**Почему в `project`, а не в `object`**: `immutable` — это свойство «как управлять файлом»
(как и `build`), не свойство самого объекта БД. Логически принадлежит секции `project`.
Omit-при-false — для краткости JSON/YAML, как делает `build` (можно omit-ить и `build` при
`true`, но `build` почти всегда `true`, поэтому оставляем явным; `immutable` почти всегда
`false`, поэтому omit'им).

## 5. Тесты (для `_plan`)

- **Unit на domain-модели** (`domain/deploy.py`): `ScriptRecord` roundtrip pydantic, defaults,
  edge cases (пустой `error_message` при success).
- **Unit на calver-валидацию** (`source_version`): regex
  `^\d{4}\.(0[1-9]|1[0-2])\.(0[1-9]|[12]\d|3[01])\.\d{2}$` — принимает `2026.08.11.01`,
  `2026.12.31.99`; отвергает `2026.8.11.01`, `2026.13.01.01`, `2026.08.32.01`, `2026.08.11.1`,
  `2026-08-11-01`. Параметризованный тест (валидные/невалидные).
- **Unit на calver-comparison** (lexicographic): `2026.08.11.01 < 2026.08.11.02 < 2026.08.11.99
  < 2026.08.12.01 < 2026.09.01.01`; zero-padded день и номер — обязательны для корректности
  (см. USER_INPUT CDF-9).
- **Unit на manifest-extension**: roundtrip `CodebaseManifest` с `source_version`; старый
  manifest (`format_version=1`, без `source_version`) → `ManifestError` с подсказкой.
- **Unit на canonical_normalize + checksum**: детерминизм (один текст → один hash),
  стабильность (whitespace-only diff → тот же hash), чувствительность (реальное изменение →
  другой hash).
- **Unit на pre/post runner** (на `DeployFakeAdapter`): skip при success+same-checksum,
  error при failed+same-checksum, error при success+different-checksum, execute при new-name
  (None). Параметризованный тест на все 4 ветки; проверяет что `script_history` после EXECUTE
  содержит ровно ОДНУ запись на `(name, type)` (UPSERT, не накопление).
- **Unit на `script_audit_log`**: 3 прогона одного скрипта (failed→success→re-run с тем же
  checksum) → в `script_history` ОДНА строка (state), в `script_audit_log` — ТРИ (append-only
  история). `deploy_version`/`deploy_source` корректно записаны в каждой audit-записи.
- **Unit на транзакционность `record_script_execution`**: при simulated failure после UPSERT
  в `script_history`, но до INSERT в `script_audit_log` → откат обеих (явная транзакция,
  `BEGIN`/`COMMIT`/`ROLLBACK`); state не «рассинхронизирован» с audit.
- **Unit на version-check**: пустая `__deploy` → ok (первый deploy); `current == source` →
  no-op + warning; `current > source` → `DeployError` (forward-only); `current < source` →
  proceed.
- **Unit на discovery**: сортировка по имени, regex-валидация, пустой каталог → no-op.
- **Unit на RE-seed** (`__deploy`-инъекция): reverse-engineer на structure БЕЗ `__deploy` →
  в сгенерированном дереве есть `__deploy/tables/{schema_version,script_history}.sql` с
  `immutable: true` в autodoc. На structure С `__deploy` → реверсится как обычная схема,
  `immutable: true` ставится автоматически. На structure С `__deploy` + существующий manifest
  → `source_version` сохраняется (не пересоздаётся seed).
- **Unit на immutable-поле**: парсинг autodoc с `immutable: true` → `Vertex.immutable == True`;
  autodoc без поля → `Vertex.immutable == False`. Roundtrip `model_dump_json` не пишет поле
  при `False` (omit).
- **Unit на canonical-DDL-валидацию** (CDF-10 подход b): codebase с canonical `__deploy`-DDL →
  warning отсутствует; codebase с модифицированным `__deploy`-DDL (другая колонка) → warning
  в progress-callback, deploy продолжается.
- **Unit на deploy-presence-check**: codebase без `__deploy`-схемы → `DeployError`
  («codebase must include __deploy schema; run reverse-engineer to seed it»).
- **Регрессия существующих deploy-тестов** (урок §45): 13 тестов `test_deploy_service.py`
  остаются зелёными (fakes обновлены, flow расширен аддитивно; fixture получает
  `__deploy/tables/` + `__migrations/`).
- **Регрессия Phase 9 compare-тестов**: `CodebaseManifest` расширено additively (`source_version`
  required при `format_version=2`); `Vertex` получает `immutable: bool = False` (additive);
  compare-тесты не должны сломаться.
- **Регрессия Phase 1 reverse-engineer тестов**: seed/sync `__deploy` — additive расширение
  flow; существующие тесты (без `__deploy` в structure) получают 2 новых файла в выводе,
  нужно обновить assertions.
- **Integration** (`@pytest.mark.integration`, testcontainers): end-to-end — temp-БД с
  `__deploy`, pre/post-скрипты, version record; после drop проверяем что `__deploy`
  отсутствует (т.к. temp-БД удалена). RE → deploy → RE цикл: seed → deploy записал версию →
  повторный RE читает её. По образцу `test_deploy_validate_e2e.py`.
- **Smoke CLI**: `db-pm deploy validate --dir <fixture с __deploy/ + __migrations/ и manifest с
  source_version>` → в логе видны шаги validate-presence / canonical-check / pre / version /
  post (progress callback).

## 6. NOT done / отложено (явный список в `_plan`)

- **Real-target deploy** (deploy в существующую БД, не temp) — **Phase 11+**. Phase 10
  только механика в validate-flow.
- **Safety-gate** (pre-analysis, оценка данных, отчёт-рекомендация) — **Phase 11**
  (CD-6..CD-10).
- **Структурный column-diff + ALTER-план** — **Phase 12** (CD-ALT-1..CD-ALT-4, CD-11..CD-15).
- **Post-deploy runner как отдельный шаг CD-пайплайна** — Phase 10 вводит механику, но
  full post-deploy в real-target (CD-16) — Phase 13.
- **`script_watermark`** — отложено (CDF-5), `schema_version` + `script_history` покрывают MVP.
- **CLI опция `--reseed-deploy`** для reverse-engineer (на случай canonical-mismatch при RE
  из БД со старой структурой db-pm) — в `_plan` как опциональный шаг, MVP пропускает.
- **Жёсткая canonical-DDL-валидация** (hard-block вместо warning) — если warning окажется
  недостаточен в продакшене; сейчас подход (b) мягкий.
- **Фильтр служебных схем в compare (Phase 9)** — `__deploy` участвует в compare как обычная
  схема; опция исключения — backlog.
- **GUI для истории версий/скриптов** — вне scope (Phase 18 CD-18 — Should).
- **AI-assisted pre-script generation** — отдельный overlay (ROADMAP §6, после Phase 12).
- **Параметры скриптов с валидацией** (CD-19) — Phase 13, Should.

## 7. Чеклист по урокам

- [ ] §12: `git add -- "_docs_/_tasks_/phase_10/..."` — дефис в имени каталога `_docs_`.
- [ ] §18/§45: расширение `DatabaseAdapter` (+5 methods) → в одном коммите обновить ВСЕ
  fakes (`DeployFakeAdapter`, `FakeAdapter`); `grep -rn "(DatabaseAdapter)"` перед стартом.
- [ ] §19: `CREATE SCHEMA` / `CREATE TABLE` с configurable именем схемы — whitelist +
  double-quote (как `_validate_db_name` в `postgres/adapter.py:28-35`).
- [ ] §23: pre/post-скрипты могут содержать autodoc-блок (если генерируются) — strip перед
  `execute_script`.
- [ ] §28: в тестах `script_history.checksum`/`version` — SHA-256 hex (числовые строки
  маловероятны, но roundtrip через pydantic, не substring-матчинг).
- [ ] §32: temp-БД создаётся через `template0` fallback (уже в deploy_service); `__deploy`-
  DDL должен работать на дефолтном `search_path` (не полагаться на `public`).
- [ ] §34/§35: ВСЕ идентификаторы в `__deploy`-DDL и version/script-запросах —
  fully-qualified (`"__deploy"."schema_version"`), double-quoted. Контракт фиксируется
  тестом `assert '"__deploy"."schema_version"' in ddl`.
- [ ] §45: fakes обновляются в одном коммите с ABC (не откладывать «на потом»).
- [ ] TASK_CONVENTIONS §6: код (adapter/domain/runner) и документы (`_plan`, `_result`)
  не смешиваются в одном коммите.

## 8. USER_INPUT (рекомендации; закрытие → перенос в `_final`)

> Стиль ROADMAP §9 — рекомендации; если пользователь согласен, переношу в `_final`.
> Префикс `CDF-N` (CD Foundation) — чтобы не коллизировать с ROADMAP `CD-N` (user stories
> CD-1..CD-19) и `CD-ALT-N` (Phase 12).

- **CDF-1** (ROADMAP Q3, **ЗАКРЫТО**) Pre/post-скрипты всегда идемпотентны, или допустим run-once?
  *Рекомендация:* **всегда идемпотентны**; run-once — только через явный флаг
  `--allow-run-once` (с warning). Соответствует ROADMAP §7 правило 5 (pre-скрипты —
  единственный легальный способ менять таблицы с данными; идемпотентные). Идемпотентность =
  повторяемость = безопасность при retry/recovery. Альтернатива: run-once по умолчанию
  ( Alembic-style) — ломает recovery-сценарии.
  *USER_INPUT* : **всегда идемпотентны**
- **CDF-2** (ROADMAP Q4, **ЗАКРЫТО**) Источник версии: git-commit/tag или manifest?
  *Решение (пользователь):* **manifest `source_version`** (calver `YYYY.MM.DD.NN`, см. CDF-9),
  **обязательное**, **git полностью убран** из versioning (нет subprocess-вызовов, нет
  git-полей в `schema_version`). Обоснование: explicit + MR-controlled — bump версии виден
  в PR reviewer'ом, не выводится из git-state в runtime; работает на codebase без git
  (tarball, export). Расширяет существующий `format_version: 1` precedent (manifest уже
  versioned в git через MR). Альтернатива (git-derived) отвергнута: implicit, ломается без
  git-окружения, нет защиты от «забыл bump».
- **CDF-9** (ЗАКРЫТО) Calver-формат: `YYYY.MM.NN` или `YYYY.MM.DD.NN`?
  *Решение (пользователь):* **`YYYY.MM.DD.NN`** (год.месяц.день.номер-в-день). Regex
  `^\d{4}\.(0[1-9]|1[0-2])\.(0[1-9]|[12]\d|3[01])\.\d{2}$`. Примеры: `2026.08.11.01`,
  `2026.08.11.02`, `2026.08.11.99`. Причина: несколько релизов в день (hotfix, errata) —
  реальный сценарий; синхрон с naming convention миграций (`YYYY-MM-DD_NNN_description.sql`
  — дата версии и дата миграции визуально совпадают). Все компоненты fixed-width (4/2/2/2) →
  лексикографическое сравнение = хронологическое.上限 99 релизов в день — более чем
  достаточно. Seed при первом RE: `YYYY.MM.DD.01`, где DD — день RE.
- **CDF-3** (ROADMAP Q5, **ЗАКРЫТО**) Где живёт `migrations/`?
  *Рекомендация:* **`<output>/<db>/migrations/{pre,post}/`** — рядом с деревом схемы.
  Единый артефакт ревизии (схема + миграции), commute с codebase. Скрипты версионируются в
  git вместе с кодовой базой. Альтернатива: отдельный корневой `migrations/` — разрывает
  связь схема↔миграция.
  *USER_INPUT* : **`<output>/<db>/__migrations/{pre,post}/`** — рядом с деревом схемы.
- **CDF-4** (**ЗАКРЫТО**) Имя служебной схемы — hardcoded `__deploy` или configurable?
  *Рекомендация:* **configurable с default `__deploy`** (опция `deploy.service_schema` в
  `config.yaml`). `__deploy` с double-`_`-префиксом — маловероятная коллизия с user-схемой,
  но configurable закрывает edge-case (user уже использует `__deploy`). Whitelist на имя
  (урок §19). Альтернатива: хардкод — проще, но негибко.
  *USER_INPUT* : **configurable с default `__deploy`** (опция `deploy.service_schema` в
  `config.yaml`)
- **CDF-5** (**ЗАКРЫТО**) `script_watermark` (CD-1 помечен опционально) — в Phase 10 или отложить?
  *Рекомендация:* **отложить**. `schema_version` (версия) + `script_history` (исполнение)
  покрывают MVP; watermark (высокая отметка применённых скриптов) избыточен, пока нет
  real-target deploy с partial-прогонами. Альтернатива: реализовать сразу — добавляет
  таблицу, но не логики.
  *USER_INPUT* : **отложить**.
- **CDF-6** (**ЗАКРЫТО**) Checksum-алгоритм и canonical-нормализация скрипта?
  *Рекомендация:* **SHA-256 от canonical-normalized текста**: strip trailing whitespace на
  строках, нормализовать `\r\n`→`\n`, удалить trailing blank-строки. SQL-комментарии НЕ
  удаляются (могут быть значимы; MVP без AST). SHA-256 — стандарт, нет коллизий.
  Альтернатива: MD5 (короче, но устаревший); AST-нормализация (sqlglot) — точно, но
  зависимость + сложность, отложено до false-positive случаев.
  *USER_INPUT* **SHA-256 от canonical-normalized текста** но при нормализации текста не учитывать раздел autodoc - считать контрольную сумму по нормализованному тексту без autodoc.
- **CDF-7** (**ЗАКРЫТО**) `__deploy` в кодовой базе — обязательно или за флагом seed'ится?
  *Рекомендация:* **обязательно** (после решения CDF-10 это вытекает). Любая кодовая база,
  прошедшая через db-pm reverse-engineer, содержит `__deploy` (seed'ится автоматически,
  §4.7). Deploy падает, если схемы нет (CDF-10). Флаг `--skip-deploy` для debug — в backlog.
  *USER_INPUT*: закрыт неявно через CDF-10 (пользователь не отдельным пунктом, но решение
  CDF-10 «deploy требует наличия __deploy — hard error» полностью покрывает CDF-7).
- **CDF-8** (**ЗАКРЫТО**) Идемпотентность `CREATE` в `__deploy`-DDL (теперь в `.sql` файлах):
  `IF NOT EXISTS` или check-then-create?
  
  *Рекомендация:* **`IF NOT EXISTS`** (PostgreSQL 9.3+). Один round-trip, атомарно, не
  гонка. Check-then-create (два запроса) — race condition при параллельном deploy.
  `IF NOT EXISTS` в seed-DDL → идемпотентный re-deploy и safe regenerate (повторный RE не
  ломает существующую БД при deploy). Альтернатива: без `IF NOT EXISTS` — падает на
  повторном deploy, что приемлемо для валидации (fresh temp-БД), но ломает real-target
  deploy (Phase 11+).
  *USER_INPUT* `IF NOT EXISTS`
- **CDF-10** (ЗАКРЫТО) `__deploy`: «магия» в adapter или явная схема в коде? И валидация
  структуры при deploy?
  *Решение (пользователь):*
  1. **`__deploy` — явная схема в кодовой базе** (`.sql` файлы с autodoc, reverse-engineer
     seed'ит/sync'ает, deploy применяет через стандартный `EARLY_DDL_TYPES`). Не
     `ensure_deploy_schema` в adapter — DDL виден пользователю, MR-controlled.
  2. **Deploy требует наличия `__deploy` в коде** — hard error если нет.
  3. **Canonical-DDL-валидация: подход (b) warning** — db-pm знает canonical SHA-256
     `schema_version`/`script_history`, сравнивает с кодовой базой; mismatch → warning
     (не блокирует). Ловит рассинхон раньше runtime.
  4. **`immutable` autodoc-поле в секции `project`, omit при false** — reverse-engineer
     ставит `True` для объектов служебной схемы; regenerate перезаписывает всегда; GUI
     lock-иконка (когда будет редактор). См. §4.8.
  Обоснование: явность > магия (в духе проекта); warning достаточно для MVP (жёсткий
  hard-block — в backlog если warning окажется слабым); `immutable` — soft-protection через
  UI + regenerate, не блокирует deploy. Альтернатива (adapter-magic) отвергнута: скрытность,
  негибкость.

## 9. Где читать дальше

- `_checkpoints_/20260804_001_checkpoint.md` — текущее состояние (Phase 14 done)
- `_tasks_/ROADMAP.md` §2 (порядок), §4 (CD-1..CD-5), §7 (правила безопасности), §8 (пайплайн), §9 (Q3/Q4/Q5)
- `_phases_/Phase_02.md` — validation deploy (fundament Phase 10)
- `src/db_project_manager/application/deploy_service.py` — текущий stateless flow
- `src/db_project_manager/infrastructure/database/base.py` — контракт adapter
- `LESSONS_LEARNED.md` §12, §18, §19, §23, §28, §32, §34, §35, §45
- `_tasks_/phase_14/Phase_14_vision_draft.md` — образец структуры draft (этот файл сделан по нему)
