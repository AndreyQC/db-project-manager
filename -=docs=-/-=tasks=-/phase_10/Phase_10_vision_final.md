# Phase 10: CD Foundation — финальный дизайн (vision final)

> **Дата:** 2026-08-13
> **Ветка:** dev
> **Статус:** final (все USER_INPUT закрыты; нормативный документ для `_plan` и реализации)
>
> Предыдущий артефакт: `-=tasks=-/phase_10/Phase_10_vision_draft.md` (не удаляется —
> остаётся для истории обсуждения, по TASK_CONVENTIONS §2.2).
>
> Контекст:
> - `-=CHECKPOINTS=-/20260804_001_checkpoint.md` — текущее состояние (Phase 14 done)
> - `-=tasks=-/ROADMAP.md` §2, шаг 2 (Phase 10) и §4 (направление B — Controlled Deployment)
> - `-=tasks=-/ROADMAP.md` §3 (safety principle), §7 (правила безопасности), §8 (целевой пайплайн)
> - `-=PHASES=-/Phase_02.md` — validation deploy (fundament Phase 10)
> - `src/db_project_manager/application/deploy_service.py` — текущий stateless flow
> - `src/db_project_manager/infrastructure/database/base.py` — контракт `DatabaseAdapter`
> - `src/db_project_manager/infrastructure/config/codebase_manifest.py` — manifest precedent
> - `LESSONS_LEARNED.md` §12, §18/§45, §19, §23, §28, §32, §34, §35

---

## 1. Постановка проблемы

Phase 2 научила инструмент **validation deploy**: применить кодовую базу в пустую
временную БД, проверить что DDL исполняется без ошибок, удалить temp-БД
(`deploy_service.py:113-234`). Это проверка «в вакууме»: схема корректна синтаксически и
непротиворечива по зависимостям. Но этот деплой **stateless** — каждый прогон начинает с
нуля, у БД нет памяти о том, что и когда на неё деплоилось.

Для реального CD-пайплайна (ROADMAP §8) этого недостаточно: нет состояния версии, истории
скриптов, pre/post runner'а, контракта на целевую БД. Phase 10 — **фундамент** CD-ядра
(10→13): заводит служебную схему `__deploy`, версионирование и pre/post runner как
**механику**, тестируемую в рамках validate-flow (temp-БД получает `__deploy` → end-to-end
→ temp-БД удаляется). Real-target deploy, safety-gate и ALTER — приходят позже
(Phase 11/12).

Зависимости: Phase 10 — шаг 2, зависит от Phase 8 (✓ done). Phase 11 (Safety Gate) зависит
от Phase 10.

## 2. Цель фазы

1. **Служебная схема `__deploy`** в дереве кода как обычная схема (`.sql` файлы с autodoc):
   `schema_version`, `script_history`, `script_audit_log`. `script_watermark` — отложен.
2. **Версионирование**: чтение текущей версии БД (`schema_version.version`), сравнение с
   `source_version` из manifest (calver `YYYY.MM.DD.NN`), запрет деплоя если target новее
   source, предупреждение при отставании (CD-2).
3. **Discovery pre/post-скриптов** в `__migrations/{pre,post}/` с сортируемыми именами
   `YYYY-MM-DD_NNN_description.sql` (CD-3).
4. **Идемпотентный pre/post runner**: checksum-based skip/error, запись в `script_history`
   (state) и `script_audit_log` (history) (CD-4).
5. **Документированное требование идемпотентности** pre/post-скриптов с примерами (CD-5,
   Should).
6. **Интеграция в validate-flow + расширение reverse-engineer**: temp-БД получает
   `__deploy` (seed'ится при RE), механика versioning + pre/post прогоняется end-to-end.

## 3. Принятые решения (все закрыты через Q&A с пользователем)

| Развилка | Решение | Обоснование |
|---|---|---|
| **CDF-1** Pre/post: идемпотентны всегда или run-once | **Всегда идемпотентны**; run-once — через явный флаг `--allow-run-once` | Соответствует ROADMAP §7 правило 5. Идемпотентность = повторяемость = безопасность при retry/recovery. |
| **CDF-2** Источник версии: git-commit/tag или manifest | **`manifest.source_version`** (calver `YYYY.MM.DD.NN`), **required**, **без git** | Explicit + MR-controlled: bump версии виден в PR reviewer'ом, не выводится из git-state в runtime; работает без git-окружения. `format_version` bump 1→2. |
| **CDF-3** Расположение `__migrations/` | **`<output>/<db>/__migrations/{pre,post}/`** | Префикс `__` — консистентно с `__deploy`, визуально отличает служебный каталог. Единый артефакт ревизии (схема + миграции). |
| **CDF-4** Имя служебной схемы | **Configurable с default `__deploy`** (опция `deploy.service_schema`) | Хардкод ломает коллизию с user-схемой `__deploy`; configurable закрывает edge-case. Whitelist (урок §19). |
| **CDF-5** `script_watermark` | **Отложить** | `schema_version` + `script_history` покрывают MVP; watermark избыточен до real-target deploy с partial-прогонами. |
| **CDF-6** Checksum-алгоритм | **SHA-256 от canonical-normalized текста БЕЗ autodoc** | Сначала strip autodoc, потом normalize (whitespace/line-endings). Checksum от executable SQL, не metadata — стабилен при изменении только autodoc. Унифицировано для pre/post + canonical-DDL. |
| **CDF-7** `__deploy` в кодовой базе | **Обязательно** (закрыт неявно через CDF-10) | Любая кодовая база, прошедшая RE, содержит `__deploy` (seed'ится автоматически). Deploy падает, если схемы нет. |
| **CDF-8** Идемпотентность `CREATE` в DDL | **`IF NOT EXISTS`** | Один round-trip, атомарно, не гонка. Идемпотентный re-deploy и safe regenerate. |
| **CDF-9** Calver-формат | **`YYYY.MM.DD.NN`** (fixed-width 4/2/2/2) | Несколько релизов в день (hotfix); синхрон с naming convention миграций. Лексикографическое сравнение = хронологическое.上限 99 релизов/день. |
| **CDF-10** `__deploy`: adapter-магия или явная схема | **Явная схема в кодовой базе**; deploy требует её наличия (hard error); canonical-DDL-валидация — **warning при mismatch**; `immutable` autodoc-поле в секции `project`, **omit при false** | Явность > магия (в духе проекта); warning достаточно для MVP; `immutable` — soft-protection через UI + regenerate. |
| **CDF-11** `script_history` vs audit | **State + history разделены**: `script_history` PK `(name, type)` UPSERT; `script_audit_log` append-only с `deploy_version`/`deploy_source` | State оптимизирован под lookup runner'а (линейная логика); audit сохраняет историю попыток для отчётов Phase 13 (CD-17). `record_script_execution` атомарно пишет в обе. |

Дополнительные архитектурные решения (без CDF-номера — реализационные):

- **Объём Phase 10**: фундамент в validate-flow + расширение RE (seed/sync). Real-target
  deploy → Phase 11+.
- **Новые domain-модели**: `domain/deploy.py` (pydantic), по образцу `domain/diff.py`.
- **Расширение `DatabaseAdapter`**: 4 новых abstract methods (без `ensure_deploy_schema`).
- **`Vertex.immutable: bool = False`** — новое поле, парсится из autodoc.

## 4. Финальная архитектура

### 4.1. `__deploy` — обычная схема в дереве кода

`__deploy` состоит из трёх файлов (рядом с user-схемами):

```
<codebase_dir>/
├── dbpm.manifest.json
├── __deploy/
│   └── tables/
│       ├── schema_version.sql
│       ├── script_history.sql
│       └── script_audit_log.sql
├── bookings/...
├── app/...
└── __migrations/{pre,post}/...
```

DDL (с autodoc-заголовком, `immutable: true` в секции `project`):

```sql
-- schema_version.sql (append-only)
CREATE TABLE IF NOT EXISTS "__deploy"."schema_version" (
    id              SERIAL PRIMARY KEY,
    version         TEXT NOT NULL,         -- calver 'YYYY.MM.DD.NN' из manifest
    applied_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    source          TEXT NOT NULL          -- 'validate' | 'deploy' | 'manual'
);

-- script_history.sql (state, PK name+type, UPSERT)
CREATE TABLE IF NOT EXISTS "__deploy"."script_history" (
    script_name     TEXT NOT NULL,
    script_type     TEXT NOT NULL,         -- 'pre' | 'post'
    checksum        TEXT NOT NULL,
    success         BOOLEAN NOT NULL,
    executed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    error_message   TEXT,
    duration_ms     INTEGER NOT NULL,
    PRIMARY KEY (script_name, script_type)
);

-- script_audit_log.sql (append-only history)
CREATE TABLE IF NOT EXISTS "__deploy"."script_audit_log" (
    id              SERIAL PRIMARY KEY,
    script_name     TEXT NOT NULL,
    script_type     TEXT NOT NULL,
    checksum        TEXT NOT NULL,
    success         BOOLEAN NOT NULL,
    error_message   TEXT,
    duration_ms     INTEGER NOT NULL,
    executed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    deploy_version  TEXT,                  -- source_version деплоя (для отчётов Phase 13)
    deploy_source   TEXT                   -- 'validate' | 'deploy' | 'manual'
);
```

- `IF NOT EXISTS` → идемпотентность (CDF-8); fully-qualified, double-quoted (урок §35).
- `schema_version` — append-only, текущая версия = `MAX(applied_at)`.
- `script_history` — **state**: одна запись на `(name, type)`, UPSERT.
- `script_audit_log` — **history**: append-only, INSERT каждый прогон, несёт
  `deploy_version`/`deploy_source` (CDF-11).
- `source` / `deploy_source` различают validate-deploy (temp-БД) от real-deploy (Phase 11+).

### 4.2. Версионирование через `dbpm.manifest.json`

Расширение manifest (additive, `format_version` bump 1→2):

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

- `source_version` — calver `YYYY.MM.DD.NN`, regex
  `^\d{4}\.(0[1-9]|1[0-2])\.(0[1-9]|[12]\d|3[01])\.\d{2}$`. **Обязательное**: manifest без
  поля → `ManifestError`. Старые manifest'ы (`format_version=1`) — regenerятся через RE
  (seed), для ручного ввода — явное редактирование.
- **Кто обновляет**: разработчик bumps в MR (`2026.08.11.01 → .02`). При RE БД с `__deploy`
  — версия sync'ается автоматически из `schema_version` (§4.7).
- **Git полностью убран**: нет subprocess-вызовов `git`, нет git-полей.

**Сравнение версий (CD-2):**
- `current = get_schema_version()` (calver-строка или None); `source = manifest.source_version`.
- `current is None` → proceed (первый deploy).
- `current == source` → no-op + warning («забыли bump»).
- `current > source` (лексикографически) → **`DeployError`** (forward-only, ROADMAP §7 п.8).
- `current < source` → proceed.

### 4.3. Discovery pre/post-скриптов

`__migrations/` рядом с деревом схемы (CDF-3): `<codebase_dir>/__migrations/{pre,post}/`.
Если каталога нет → нет скриптов, runner пропускается (не ошибка).

Соглашение об именах (CD-3): `YYYY-MM-DD_NNN_description.sql`. Discovery:
`sorted(glob('__migrations/pre/*.sql'))`. Валидация имени regex
`^\d{4}-\d{2}-\d{2}_\d{3}_.+\.sql$`; нарушение → warning. Дубликат `(имя, checksum)` в
истории → skip (§4.4).

### 4.4. Pre/post runner (идемпотентное выполнение)

Для каждого скрипта по порядку:

1. `checksum = sha256(canonical_normalize(strip_autodoc(text)))` (CDF-6 — без autodoc).
2. `existing = get_script_history(script_name, script_type)` — **одна запись или None**.
3. Линейная логика:

   ```
   if existing is None:                                     → EXECUTE
   elif existing.checksum == current and existing.success:  → SKIP
   elif existing.checksum == current and not existing.success: → ERROR
   elif existing.checksum != current:                       → ERROR  # CD-4
   ```

4. При EXECUTE: `execute_script(strip_autodoc_if_any(text))`, затем `record_script_execution`
   **атомарно** (одна транзакция): UPSERT в `script_history` + INSERT в `script_audit_log`.
5. При error → stop (по умолчанию) или continue (`--continue-on-error`). Ошибка пишется в
   обе таблицы (`success=FALSE`, `error_message`).

`canonical_normalize`:
1. Strip autodoc-секции (`[<[autodoc-yaml]...[autodoc-yaml]>]`) — checksum от executable
   SQL. Существующий `_strip_autodoc` (`deploy_service.py:261-273`).
2. Strip trailing whitespace на строках, удалить blank-строки, нормализовать `\r\n`→`\n`.
   SQL-комментарии НЕ удаляются (MVP без AST).

Порядок в пайплайне (ROADMAP §8): pre **до** объектов дельты, post **после**.

### 4.5. Интеграция в validate-flow

```
DeployValidateService.run():
  1-5. [существует] create temp-DB, reconnect
  6. validate __deploy presence        # CDF-10: hard error если нет в кодовой базе
  7. canonical-DDL check (warning)     # CDF-10 подход (b)
  8. version-check                     # source vs пустая temp-БД → proceed
  9. pre-runner (__migrations/pre/)
  10. deploy vertices (топосорт)        # __deploy-схема/таблицы через EARLY_DDL
  11. post-runner (__migrations/post/)
  12. record_schema_version(source)
  13. finally: drop temp-DB
```

`__deploy` НЕ создаётся через `ensure_deploy_schema()` — применяется как обычная схема из
кодовой базы на шаге 10. Это **сквозной тест механики** на безопасном полигоне.

### 4.6. Расширение `DatabaseAdapter`

4 новых abstract methods (по образцу Phase 9 `get_table_row_counts`):

| Method | Signature | Назначение |
|---|---|---|
| `get_schema_version` | `(schema_name: str) -> str \| None` | Последняя `version` (calver) из `schema_version` |
| `record_schema_version` | `(schema_name: str, version: str, source: str) -> None` | INSERT в `schema_version` (append-only) |
| `get_script_history` | `(schema_name: str, script_name: str, script_type: str) -> ScriptRecord \| None` | Одна запись (state) по `(name, type)` |
| `record_script_execution` | `(schema_name: str, record: ScriptRecord, deploy_version: str, deploy_source: str) -> None` | **Атомарно**: UPSERT в `script_history` + INSERT в `script_audit_log` |

Реализация в `PGDatabaseAdapter`: SQL-константы в `postgres/queries.py`.
`record_script_execution` — `INSERT ... ON CONFLICT DO UPDATE` для history + INSERT для
audit в одной явной транзакции (`BEGIN`/`COMMIT`).

**Canonical-DDL-валидация** (CDF-10 подход b; CDF-6): db-pm содержит embedded canonical DDL
трёх таблиц (в `infrastructure/sql/templates/deploy/` или
`infrastructure/deploy/canonical_ddl.py`). При deploy — strip autodoc → normalize → SHA-256
→ сравнение с canonical. Mismatch → warning (не блокирует). Checksum стабилен при изменении
только autodoc.

Урок §45: расширение ABC ломает ВСЕ test-fakes → обновляем в одном коммите
(`DeployFakeAdapter`, `FakeAdapter`).

### 4.7. Seed и sync `__deploy` через reverse-engineer

RE ответственен за создание/поддержание `__deploy`. Замыкает цикл БД ↔ codebase:

```
[1] RE БД без __deploy → seed 3 таблицы из шаблонов + immutable:true + manifest source_version = YYYY.MM.DD.01
[2] deploy применяет __deploy через EARLY_DDL + record_schema_version
[3] RE БД с __deploy → реверсит 3 таблицы как обычную схему + immutable:true по имени + sync source_version из schema_version
[4] пользователь bumps source_version вручную (.01 → .02)
[5] deploy записывает новую версию → цикл на [3]
```

Точки расширения:
- `application/reverse_engineer.py`: проверка `__deploy` в схемах после
  `get_database_structure()`; если нет → инжектировать seed-вершины (schema + 3 tables) с
  `immutable=True`. Если есть — обычный reverse + post-processing (ставить `immutable=True`
  по configurable имени схемы).
- `infrastructure/sql/autodoc.py`: расширение `project`-секции полем `immutable`.
- `infrastructure/sql/templates/deploy/`: 3 seed-шаблона
  (`schema_version.sql.j2`, `script_history.sql.j2`, `script_audit_log.sql.j2`).
- `domain/graph.py` (`Vertex`): новое поле `immutable: bool = False`.

Edge-cases:
- RE БД без `__deploy` + существующий manifest с `source_version` (повторный RE) →
  **сохранять** существующую версию, не пересоздавать seed `.01`.
- RE БД с `__deploy`, но `schema_version` пустая → warning + fallback на seed `YYYY.MM.DD.01`.
- RE БД с `__deploy`, структура не совпадает с canonical db-pm → реверсится как есть +
  warning; регенерация через `--reseed-deploy` (опция plan'а) — backlog.

### 4.8. Autodoc-поле `immutable` в секции `project`

Расширение Phase 1 контракта. Поле живёт в `project` (как `build`), указывается **явно
только при `true`**; `false` — default, omit для краткости.

```yaml
# __deploy-объект
object: { ... }
project:
  build: true
  immutable: true    # только при true
```

Поведение:
- `Vertex.immutable: bool = False` — парсится из autodoc при graph build.
- RE ставит `immutable=True` для всех объектов служебной схемы (по имени, configurable).
- GUI: lock-иконка на узле; read-only preview (когда будет редактор).
- Regenerate: `immutable`-файлы **всегда перезаписываются** (пользовательские правки не
  сохраняются).
- Deploy: `immutable`-объекты применяются как обычные.
- Compare (Phase 9): участвуют как обычные; опция исключения — backlog.

## 5. Проверки (для `_plan`)

- **Unit на domain-модели** (`domain/deploy.py`): `ScriptRecord` roundtrip pydantic.
- **Unit на calver-валидацию**: regex принимает `2026.08.11.01`, `2026.12.31.99`; отвергает
  `2026.8.11.01`, `2026.13.01.01`, `2026.08.32.01`, `2026-08-11-01`.
- **Unit на calver-comparison**: `2026.08.11.01 < 2026.08.11.02 < 2026.08.12.01`.
- **Unit на manifest-extension**: roundtrip с `source_version`; `format_version=1` без поля
  → `ManifestError`.
- **Unit на canonical_normalize + checksum**: детерминизм, стабильность, чувствительность;
  без autodoc (добавление/изменение autodoc → тот же checksum).
- **Unit на pre/post runner**: 4 ветки (None/skip+success/error+failed/error+different);
  `script_history` после EXECUTE — ОДНА запись на `(name, type)`.
- **Unit на `script_audit_log`**: 3 прогона = 1 state-запись + 3 audit-записи;
  `deploy_version`/`deploy_source` корректны.
- **Unit на транзакционность**: rollback обеих (history + audit) при failure.
- **Unit на version-check**: 4 ветки (None/`==`/`>`/`<`).
- **Unit на RE-seed**: structure без `__deploy` → 3 файла в дереве с `immutable: true`;
  structure с `__deploy` → реверсится + immutable автоматически; + существующий manifest →
  `source_version` сохраняется.
- **Unit на immutable-поле**: парсинг autodoc → `Vertex.immutable`; roundtrip omit при False.
- **Unit на canonical-DDL-валидацию**: canonical → нет warning; модифицированное → warning,
  deploy продолжается.
- **Unit на deploy-presence-check**: codebase без `__deploy` → `DeployError`.
- **Регрессия** deploy/compare/RE тестов (урок §45: fakes обновляются в одном коммите).
- **Integration** (`@pytest.mark.integration`, testcontainers): end-to-end + RE → deploy →
  RE цикл (seed → deploy записал версию → повторный RE читает её).
- **Smoke CLI**: `db-pm deploy validate --dir <fixture>` → в логе шаги validate-presence /
  canonical-check / pre / version / post.

## 6. NOT done / отложено

- **Real-target deploy** (в существующую БД, не temp) — Phase 11+.
- **Safety-gate** (pre-analysis, оценка данных, отчёт-рекомендация) — Phase 11 (CD-6..CD-10).
- **Структурный column-diff + ALTER-план** — Phase 12 (CD-ALT-1..CD-ALT-4, CD-11..CD-15).
- **Post-deploy runner как отдельный шаг CD-пайплайна** (CD-16) — Phase 13.
- **`script_watermark`** — отложено (CDF-5).
- **CLI `--reseed-deploy`** для RE (canonical-mismatch fallback) — backlog.
- **Жёсткая canonical-DDL-валидация** (hard-block вместо warning) — если warning окажется
  слабым в продакшене.
- **Фильтр служебных схем в compare (Phase 9)** — backlog.
- **GUI для истории версий/скриптов** (CD-18) — Phase 18.
- **AI-assisted pre-script generation** — overlay (ROADMAP §6, после Phase 12).
- **Параметры скриптов с валидацией** (CD-19) — Phase 13.

## 7. Чеклист по урокам

- [ ] §12: `git add -- "-=docs=-/-=tasks=-/phase_10/..."` (дефис в `-=docs=-`).
- [ ] §18/§45: расширение `DatabaseAdapter` (+4 methods) → в одном коммите обновить ВСЕ
  fakes (`DeployFakeAdapter`, `FakeAdapter`); `grep -rn "(DatabaseAdapter)"` перед стартом.
- [ ] §19: `CREATE SCHEMA`/`CREATE TABLE` с configurable именем — whitelist + double-quote.
- [ ] §23: pre/post-скрипты могут содержать autodoc → strip перед `execute_script` и перед
  checksum.
- [ ] §28: roundtrip через pydantic, не substring-матчинг (для `version`/`checksum`).
- [ ] §32: temp-БД через `template0` fallback (уже в deploy_service); `__deploy`-DDL должен
  работать на дефолтном `search_path`.
- [ ] §34/§35: ВСЕ идентификаторы в `__deploy`-DDL и запросах — fully-qualified, double-
  quoted. Контракт тестом `assert '"__deploy"."schema_version"' in ddl`.
- [ ] §45: fakes обновляются в одном коммите с ABC.
- [ ] TASK_CONVENTIONS §6: код и документы не смешиваются в одном коммите.

## 8. Где читать дальше

- `-=tasks=-/phase_10/Phase_10_vision_draft.md` — предшествующий драфт (история обсуждения)
- `-=CHECKPOINTS=-/20260804_001_checkpoint.md` — текущее состояние (Phase 14 done)
- `-=tasks=-/ROADMAP.md` §2 (порядок), §4 (CD-1..CD-5), §7 (правила безопасности), §8 (пайплайн)
- `-=PHASES=-/Phase_02.md` — validation deploy (fundament)
- `src/db_project_manager/application/deploy_service.py` — текущий stateless flow
- `src/db_project_manager/infrastructure/database/base.py` — контракт adapter
- `LESSONS_LEARNED.md` §12, §18, §19, §23, §28, §32, §34, §35, §45
