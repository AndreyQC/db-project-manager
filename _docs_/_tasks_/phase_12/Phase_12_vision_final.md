# Phase 12: ALTER + Delta — финальный дизайн (vision final)

> **Дата:** 2026-08-16
> **Ветка:** dev
> **Статус:** final (все USER_INPUT закрыты — ALT-1..ALT-8; нормативный документ для `_plan`
> и реализации)
>
> Предшествующий артефакт: `_tasks_/phase_12/Phase_12_vision_draft.md` (не удаляется — остаётся
> для истории обсуждения, по TASK_CONVENTIONS §2.2).
>
> Контекст:
> - `_checkpoints_/20260815_001_checkpoint.md` — текущее состояние (Phase 11 done, integration зелёный)
> - `_tasks_/ROADMAP.md` §2 (шаг 4), §4 (CD-ALT-1..4, CD-11..CD-15), §7 (правила безопасности),
>   §8 (целевой пайплайн), §6 (AI-трек: prerequisite = column-diff этой фазы; CD-AI-3 = Won't)
> - `_tasks_/phase_11/Phase_11_vision_final.md` — фундамент (gate/presence/covers/отчёты)
> - `_phases_/Phase_09.md`, `_phases_/Phase_10.md` — compare и `__deploy`-механика
> - `src/db_project_manager/application/{safety_gate_service,deploy_service,script_runner,compare_service}.py`
> - `src/db_project_manager/infrastructure/diff/{comparator,snapshot,normalize_sql}.py`, `domain/diff.py`
> - `src/db_project_manager/infrastructure/database/postgres/adapter.py` (`_build_table`, `GET_COLUMNS`)
> - `LESSONS_LEARNED.md` §3, §12, §19, §23, §26/§28, §34/§35, §44, §45, §49, Phase 11 §48

---

## 1. Постановка проблемы

Phase 11 дала dry-run safety-gate (`deploy analyze`), но после «CLEAN» некуда идти: пути
**применения** дельты к существующей БД в проекте нет. Всё, что есть — validation deploy в
пустую temp-БД (`DeployValidateService`) и идемпотентный pre/post runner (`ScriptRunner`),
ни разу не запускавшийся против живой цели.

Второй блокер — diff-слой **hash-level**: `DiffStatus.CHANGED` для таблицы непрозрачен
(`comparator.py` решает по `sql_hash`); column-структура при RE считывается
(`_build_table`, `GET_COLUMNS`) и выбрасывается после рендера DDL. Сгенерировать `ALTER TABLE`
из «хэши различаются» невозможно — это prerequisite ROADMAP для всех вариантов генерации ALTER.

**Ключевой принцип фазы:** безопасность достигается не транзакцией на живой БД, а
**репетицией** — полный прогон деплоя на воспроизведённом аналоге таргета с данными,
и только затем применение к цели. Плюс консервативная классификация: до таблиц с данными
авто-дельта доносит только заведомо безопасные операции.

## 2. Цель фазы

1. **Структурный column-diff (CD-ALT-1):** колонки извлекаются из SQL-тела при snapshot-build
   (обе стороны симметрично); `DiffEntry` для CHANGED-таблицы несёт column-diff
   (added/dropped/type/nullable/default/comment), а не только хэш.
2. **ALTER-план с классификацией (CD-ALT-2..4):** каждая операция — `safe / needs-pre /
   blocked` по консервативному whitelist (§3 ALT-3); DDL рендерится детерминированно,
   fully-qualified.
3. **Генерация дельты (CD-12, CD-13):** артефакты на диске — `delta/NNN_*.sql` в порядке
   топосорта + `plan.json` (CI) + `plan.md` (review) — до применения.
4. **Apply-пайплайн (CD-11, CD-14, CD-15):** version-check → safety-gate → pre-скрипты →
   повторная дельта (только SAFE на таблицах с данными, иначе падение) → генерация →
   применение по одному скрипту (stop-on-error) → post-скрипты → запись `schema_version`
   (`deploy_source="apply"`); перед живым таргетом — **репетиция** на temp-аналоге с данными.
5. **Детерминированность:** классификация, шаблоны ALTER, кавычки, топосорт — только код,
   без AI (CD-AI-3 = Won't). AI-трек надстраивается после фазы.

## 3. Принятые решения (все закрыты)

| ID | Развилка | Решение | Обоснование |
|----|----------|---------|-------------|
| **ALT-1** | Источник column-структуры | **Парсинг SQL-тела** при snapshot-build (sqlglot, уже в пайплайне хэширования); autodoc не расширяется | SQL-скрипт = единственный источник правды; дрейф «autodoc↔SQL» невозможен по построению (code-first safe). Stale-autodoc сценарий давал бы **неправильную safe-операцию** (ALTER ADD удалённой колонки); сверка потребовала бы того же парсинга — хранение избыточно. Цена: карта тип-синонимов; непарсируемый DDL → `columns=None` → fail-safe |
| **ALT-2** | Гранулярность MVP | **Колонки only**; прочие табличные изменения (constraints/indexes/partitioning) = `unrepresented` → needs-pre при данных (re-render для пустых) | Ошибиться можно только в безопасную сторону; структурный diff констрейнтов — BACKLOG |
| **ALT-3** | Классификация операций | **Консервативный whitelist** (матрица ниже): add nullable/literal-default → safe даже при данных (PG11+); volatile DEFAULT, drop, alter type, сужения, unrepresented → needs-pre; rebuild — только pre-скриптом; rename не детектируем (= drop+add) | «Лучше потерять день, чем данные»; детерминированные гарантии не размываются |
| **ALT-4** | Поверхность команд | **Две**: `deploy plan` (gate + артефакты, ничего не выполняет) и `deploy apply` (полный пайплайн, те же артефакты перед выполнением) | CD-13 review = просмотр артефактов; CI гоняет `plan`; нет путаницы режимов `--execute` |
| **ALT-5** | Транзакционность apply | **Репетиция на аналоге таргета** вместо mega-транзакции: RE(target) → temp-кодовая база → накат в temp-БД → seed данных → полный пайплайн против аналога → при успехе тот же пайплайн против таргета. Скрипты дельты — по одному `execute_script` (AUTOCOMMIT), stop-on-error, без `continue-on-error`. Восстановление — пересчётом дельты (повторный apply доводит остаток; pre/post скипаются по `script_history`) | Практика: обернуть весь деплой в одну транзакцию практически невозможно и опасно (локи, `CREATE INDEX CONCURRENTLY`); отладка на аналоге безопаснее rollback на живой БД. Цена (RE таргета + deploy на каждый apply) принята осознанно |
| **ALT-6** | REMOVED-объекты | **Никогда не дропать автоматически**: `blocked`-запись + закомментированный `DROP` в артефакте; применение — только явный `--include-drops` (таблицы с данными — всё равно только через pre) | «Явность > магия»: отсутствие объекта в коде может означать неполный RE |
| **ALT-7** | GUI | **Без GUI-действий** (CLI-only); GUI plan/apply + рендер плана — BACKLOG P3 (добавлено в `_tasks_/BACKLOG.md`) | Apply — первая мутирующая живую БД команда; GUI после обкатки CLI |
| **ALT-8** | Засев данных репетиции | **(a) Пользовательские seed-скрипты** `__migrations/seed/*.sql`: выполняются только в репетиции, никогда на таргете; пусто/нет каталога → репетиция без данных. Авто-генератор «одной записи» — BACKLOG P3 поверх | Детерминированно; FK-порядок и NOT NULL — зона ответственности автора (как у pre). Seed критичен прежде всего для честного прогона pre-скриптов: SAFE-операции и так единственные, что доходят до таблиц с данными |

**Матрица классификации ALT-3:**

| Изменение | Пустая таблица | Таблица с данными (HAS_DATA/UNKNOWN) |
|---|---|---|
| ADD COLUMN (nullable без DEFAULT; DEFAULT = неизменяемый literal) | safe | **safe** (PG11+ metadata-only) |
| ADD COLUMN (volatile DEFAULT: `now()` и т.п.) | safe | needs-pre (rewrite) |
| DROP COLUMN | safe | needs-pre (без `covers` → blocked) |
| ALTER TYPE (всё, включая расширения `int→bigint`, `varchar(n)+`) | safe | needs-pre (rewrite/lock) |
| NULLABLE/DEFAULT: расширение (drop NOT NULL, add DEFAULT) | safe | safe |
| NULLABLE/DEFAULT: сужение (set NOT NULL, drop DEFAULT) | safe | needs-pre |
| COMMENT (колонки/таблицы) | safe | safe |
| Изменение вне колонок (`unrepresented`, ALT-2) | re-render | needs-pre |
| Rebuild таблицы | — | только pre-скриптом, авто — никогда |

## 4. Финальная архитектура

### 4.1. Извлечение колонок — `infrastructure/diff/columns.py` (новый)
- `extract_columns(body: str) -> list[ColumnSnapshot] | None`: sqlglot
  `parse_one(body, read="postgres")` → `Create` → `ColumnDef`-список → нормализация типов
  картой синонимов (`integer`→`int4`, `character varying`→`varchar`, …) к каноническим
  `udt_name` (как пишет RE). `None` = не CREATE TABLE / `PARTITION OF` / экзотика → fail-safe.
- Интеграция в `build_snapshot_from_dir`: refactor — один `parse_one` на тело, из него и
  `sql_hash` (существующий `normalize_sql`), и колонки (только `object_type == "table"`).
- Стороны симметричны: DIR — файлы кодовой базы (в т.ч. hand-written), DB — temp RE →
  канонический DDL → тот же экстрактор. **Ноль изменений контракта адаптера** (§45 не
  срабатывает). Autodoc остаётся identity-only (§23); формат файлов не меняется.
- §44: перед написанием модуля — smoke-тест актуального sqlglot-API (`Create`/`ColumnDef`).

### 4.2. Domain-модели — `domain/delta.py` (новый; pydantic, по образцу `domain/safety.py`)
```python
class ColumnSnapshot(BaseModel):
    name: str
    type: str                    # canonical (udt_name + модификаторы)
    nullable: bool
    default: str | None
    comment: str | None = None

class ColumnChangeKind(str, Enum):
    ADDED = "added"; DROPPED = "dropped"; TYPE_CHANGED = "type_changed"
    NULLABILITY_CHANGED = "nullability_changed"; DEFAULT_CHANGED = "default_changed"
    COMMENT_CHANGED = "comment_changed"

class ColumnDiff(BaseModel):
    column: str
    kind: ColumnChangeKind
    source_column: ColumnSnapshot | None   # None для ADDED
    target_column: ColumnSnapshot | None   # None для DROPPED

class OperationClass(str, Enum):
    SAFE = "safe"; NEEDS_PRE = "needs_pre"; BLOCKED = "blocked"

class PlannedOperation(BaseModel):
    object_key: str
    object_type: str
    object_schema: str | None
    object_name: str
    action: Literal["create", "alter", "rerender", "drop", "skip"]
    column_diffs: list[ColumnDiff] = []
    classification: OperationClass
    reason: str                             # человеческое «почему» — в plan.md и лог
    script_file: str                        # относительный путь артефакта

class DeltaPlan(BaseModel):
    db_type: str
    source_version: str | None
    target_version: str | None
    operations: list[PlannedOperation]      # порядок применения = топосорт
    include_drops: bool = False
    # properties: violations, needs_pre_ops, safe_ops
```
`ObjectSnapshot.columns: list[ColumnSnapshot] | None = None` (additive; старые JSON парсятся,
§28 roundtrip-тест). `DiffEntry.column_diffs: list[ColumnDiff] = []` — заполняется comparator'ом.

### 4.3. Comparator: column-diff для CHANGED-таблиц (CD-ALT-1)
В `compare()` для `status=CHANGED ∧ object_type=table`: если `columns` не `None` у обеих
сторон — множественное сравнение по имени колонки (union имён; rename не детектируется —
это drop+add). Какая-либо сторона без колонок → `column_diffs=[]` + маркер
`columns_unavailable=True` (классификатор увидит «unrepresented»). Чистая функция на dict'ах,
без I/O — табличные unit-тесты на синтетических парах «было/стало».

### 4.4. Классификатор + ALTER-рендер — `infrastructure/deploy/alter_plan.py` (новый)
- `classify(entry, presence, coverage, *, include_drops) -> PlannedOperation`: чистая
  функция; матрица ALT-3 + правила Phase 11 (presence, `covers`). Пустая таблица (EMPTY) —
  привилегированный путь: любые изменения → `rerender` (пересоздание тела файла), кроме
  безопасных ALTER. ADDED → `create`; REMOVED → `blocked` (без `--include-drops`) /
  закомментированный drop-артефакт.
- `render_alter(op: PlannedOperation) -> str`: `ALTER TABLE "s"."t" ...` — по одному
  утверждению на ColumnDiff; fully-qualified + `_quote_identifier` whitelist `[A-Za-z_]
  [A-Za-z0-9_]*` (§19, §34/§35 — контракт-тест `'"schema"."table"' in ddl`).
- PG-диалект; `db_type` из manifest — guard: greenplum пропускается (совместим с PG-DDL),
  прочие — hard error (как compare).

### 4.5. Сборка дельты — `application/delta_service.py` (новый)
Вход: `DiffReport` (CompareService), presence-lookup, coverage, `deploy_order` (топосорт
`graph_service`). Выход: `DeltaPlan` + артефакты в `output_dir`:
- `delta/NNN_<type>_<schema>_<name>.sql` — одна операция = один файл; `NNN` = позиция в
  порядке применения (стабильная нумерация); create/rerender — тело файла объекта после
  `strip_autodoc` (§23); alter — рендер §4.4; drop — закомментирован (ALT-6);
  comment-only → операция `skip` (`has_executable_sql`, §49).
- `plan.json` (`DeltaPlan.model_dump_json`, CI) + `plan.md` — свод по образцу
  `safety_report.py`: операции с классификацией и `reason`, presence/покрытие таблиц,
  версии, счётчики по классам.

### 4.6. Apply-пайплайн — `application/deploy_apply_service.py` (новый)
Ядро — `_run_pipeline(adapter, codebase_dir, …)` — один код для репетиции и таргета:
```
1. manifest + db_type + version-check (SG-6): target > source → hard error
2. safety-gate (SafetyGateService.analyze, reuse): violations → падение ДО pre-скриптов
3. pre-скрипты: ScriptRunner.run_phase("pre") — идемпотентно, история в __deploy
4. повторная дельта (CD-11): свежий CompareService.run + classify;
   не-SAFE операции на таблицах с данными → падение (semantics: pre-скрипт обязан
   довести покрытые таблицы до состояния, где остаточная дельта безопасна)
5. генерация артефактов (§4.5) — на диск, лог путей
6. применение дельты: по одному execute_script на операцию (AUTOCOMMIT, ALT-5),
   stop-on-error; лог каждого шага (progress-callback, как DeployValidateService)
7. post-скрипты: ScriptRunner.run_phase("post"); ошибка → пайплайн падает
8. record_schema_version(service_schema, source_version, "apply") (CD-15)
```
`deploy apply` (с репетицией, ALT-5):
```
A. РЕПЕТИЦИЯ (если не --no-rehearsal):
   a1. RE(target) → temp-кодовая база (manifest наследует version таргета — §48)
   a2. накат этой кодовой базы в temp-БД (механика DeployValidateService, keep_db;
       CREATEDB обязателен) — состояние таргета воспроизведено; __deploy уже создан RE
   a3. seed: __migrations/seed/*.sql (ALT-8) — только здесь, никогда на таргете
   a4. _run_pipeline против репетиционной БД; сбой → падение, таргет не тронут
   a5. cleanup: temp-БД дропается (по умолчанию); --keep-rehearsal-db — оставить для отладки
B. ЦЕЛЬ: _run_pipeline против живого таргета (fresh compare → дельта; pre/post скипаются
   по script_history, если применялись ранее)
```
`deploy plan` = шаги 1-2 + 4-5 без мутаций (без 3, 6-8; повторная дельта без pre = та же,
что на шаге 2, поэтому plan = gate + артефакты). Восстановление после сбоя на таргете —
повторным apply: дельта пересчитывается, частично применённое исчезает из неё, исполненные
pre/post скипаются (идемпотентность Phase 10).

### 4.7. CLI — `presentation/cli/main.py`
`deploy plan --dir --target-connection-file --output-dir` и
`deploy apply --dir --target-connection-file --output-dir [--include-drops]
[--no-rehearsal] [--keep-rehearsal-db]` — подкоманды группы `deploy` (typer, урок §9).
Exit codes как у analyze: 0 — ок; 1 — нарушения/needs-pre; 2 — hard error. `--help`
apply: «ИЗМЕНЯЕТ целевую БД (после репетиции на temp-аналоге)».

### 4.8. Reuse / не-дублирование
- `CompareService` — единственный источник дельты (analyze/plan/apply).
- `SafetyGateService` — шаг 2 пайплайна; `ScriptRunner`, `record_schema_version`, `covers`,
  presence-stats — Phase 10/11 как есть. Топосорт — `graph_service.deploy_order`.
- Репетиция переиспользует `ReverseEngineerService` + `DeployValidateService`-механику;
  новых abstract-методов адаптера — **ноль**.

## 5. Проверки (для `_plan`)

- **Unit columns.py:** экстракция из канонического RE-DDL и hand-written (`integer`,
  `character varying`, inline-constraints); `PARTITION OF`/не-CREATE → `None`; карта
  синонимов — параметризованная.
- **Unit column-diff (comparator):** синтетические пары ObjectSnapshot — add/drop/type/
  nullable/default/comment; смешанные; сторона без колонок → `columns_unavailable`;
  rename = drop+add.
- **Unit классификатора:** матрица ALT-3 (изменение × presence × coverage) → класс+reason;
  unrepresented → needs-pre; REMOVED → blocked без `--include-drops`; EMPTY → rerender.
- **Unit рендера ALTER:** fully-quoted идентификаторы (§35 контракт-тест); спец-символы в
  именах отклоняются whitelist'ом (§19); comment-only → skip (§49).
- **Unit DeltaPlan/артефакты:** порядок = deploy_order; нумерация стабильна; `plan.json`
  roundtrip (§28); `plan.md` содержит классификацию/reasons.
- **Unit apply (fake adapter):** violations шага 2 → ни одного мутирующего вызова; не-SAFE
  остаток шага 4 → падение до применения; успех → порядок (pre → delta → post →
  record_version="apply"); ошибка в середине дельты → стоп, версия НЕ записана; повторный
  прогон после частичного применения → дельта короче, pre скипается.
- **Unit rehearsal (fake adapter):** сбой на репетиции → таргет не тронут; seed только в
  репетиции; `--no-rehearsal` пропускает фазу A; temp-БД дропается по умолчанию.
- **Unit CLI:** корректные вызовы; exit codes; `--include-drops` влияет только на REMOVED.
- **Integration (testcontainers):** (1) данные + add nullable → plan safe, apply проходит
  (репетиция + таргет), колонка появилась, версия записана; (2) drop column при данных без
  pre → needs-pre/blocked, apply падает ДО pre; (3) то же + pre с `covers`, делающий ALTER
  сам → apply проходит, повторная дельта чиста (CD-11); (4) ошибка скрипта дельты на
  репетиции → таргет не изменён; после исправления повтор идемпотентен; (4a) ошибка на
  таргете (`--no-rehearsal`) → стоп, повтор доводит остаток; (5) `PARTITION OF` →
  columns=None → fail-safe needs-pre; (6) `integer` vs `int4` → TYPE_CHANGED не возникает;
  (7) seed выполняется в репетиции и не выполняется на таргете.

## 6. NOT done / отложено

- **AI-трек** (CD-AI-1 pre-backfill черновики, CD-AI-2 объяснения) — overlay после Phase 12.
- **Структурный diff констрейнтов/индексов/partitioning** — BACKLOG (ALT-2).
- **Detect column rename** — осознанно никогда в авто-классификации (только drop+add).
- **Авто-генератор seed «одной записи на таблицу»** — BACKLOG P3 поверх ALT-8a.
- **Post-deploy отчёты, side-by-side DDL diff, история версий** (CD-16..18) — Phase 13.
- **GUI plan/apply + рендер плана** — BACKLOG P3 (добавлено, см. `_tasks_/BACKLOG.md`).
- **Greenplum:** распределённые таблицы (ALTER распределённых объектов) — валидация при
  появлении кластера; presence уже fail-safe.

## 7. Чеклист по урокам

- [ ] §3: оценка данных — только метаданные (presence-stats), без COUNT.
- [ ] §12: `git add -- "_docs_/_tasks_/phase_12/..."` (дефис в `_docs_`).
- [ ] §19: идентификаторы в генерируемом DDL — whitelist + double-quote (`_quote_identifier`).
- [ ] §23: тела объектов в дельту — после `strip_autodoc`.
- [ ] §26/§28: identity колонок — по имени в рамках таблицы; JSON-тесты — roundtrip.
- [ ] §34/§35: ALTER/CREATE дельты — fully-qualified `"schema"."table"."column"`; контракт-тест.
- [ ] §44: sqlglot-API (`Create`/`ColumnDef`) — smoke до написания `columns.py`.
- [ ] §45: ноль новых abstract-методов адаптера — цель фазы; если понадобится — все fakes
  в одном коммите (`grep -rn "(DatabaseAdapter)"`).
- [ ] §49: skip comment-only скриптов дельты через `has_executable_sql`.
- [ ] Phase 11 §48: RE(target) в репетиции наследует version таргета — порядок действий
  integration-тестов фиксировать явно.
- [ ] TASK_CONVENTIONS §6: код и документы — в разных коммитах.

## 8. Где читать дальше

- `_tasks_/phase_12/Phase_12_vision_draft.md` — предшествующий драфт (история обсуждения,
  включая пересмотр ALT-1 после code-first вопроса и ALT-5 после USER_INPUT о транзакциях)
- `_tasks_/ROADMAP.md` §4 (CD-ALT-1..15), §7 (правила), §8 (пайплайн шаги 3-9), §6 (AI Won't)
- `_tasks_/phase_11/Phase_11_vision_final.md` — gate/presence/covers/отчёты (переиспользование)
- `_phases_/Phase_09.md`, `_phases_/Phase_10.md` — compare и `__deploy`-механика
- `src/db_project_manager/infrastructure/diff/comparator.py` — точка расширения column-diff
- `src/db_project_manager/application/deploy_service.py` — образец оркестрации/прогресса/ошибок
