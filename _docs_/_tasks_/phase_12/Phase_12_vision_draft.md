# Phase 12: ALTER + Delta — драфт дизайна (vision draft)

> **Дата:** 2026-08-15
> **Ветка:** dev
> **Статус:** draft (открытые `USER_INPUT` ALT-1..ALT-7; после закрытия → `_final`)
>
> Контекст:
> - `_checkpoints_/20260815_001_checkpoint.md` — текущее состояние (Phase 11 done, integration зелёный)
> - `_tasks_/ROADMAP.md` §2 (шаг 4), §4 (CD-ALT-1..4, CD-11..CD-15), §7 (правила безопасности),
>   §8 (целевой пайплайн), §6 (AI-трек: prerequisite = column-diff этой фазы)
> - `_tasks_/phase_11/Phase_11_vision_final.md` — нормативный дизайн Phase 11 (образец + фундамент)
> - `src/db_project_manager/application/{safety_gate_service,deploy_service,script_runner,compare_service}.py`
> - `src/db_project_manager/infrastructure/diff/{comparator,snapshot}.py`, `domain/diff.py`
> - `src/db_project_manager/infrastructure/database/postgres/adapter.py` (`_build_table`, `GET_COLUMNS`)
> - `src/db_project_manager/infrastructure/sql/autodoc.py`
> - `LESSONS_LEARNED.md` §19, §23, §26/§28, §34/§35, §45, §49

---

## 1. Постановка проблемы

Phase 11 дала **dry-run safety-gate** (`deploy analyze`): дельта код↔живая БД, оценка данных,
покрытие pre-скриптами, вердикт CLEAN/VIOLATIONS. Но после «CLEAN» **некуда идти**:
в проекте нет пути **применения** дельты к существующей БД (apply). Всё, что есть —
validation deploy в пустую temp-БД (`DeployValidateService`) и идемпотентный pre/post runner
(`ScriptRunner`), ни разу не запускавшийся против живой цели.

Второй блокер — diff-слой остаётся **hash-level**: `DiffStatus.CHANGED` для таблицы непрозрачен
(`comparator.py:66` решает по `sql_hash`). Column-структура при reverse-engineer считывается
(`adapter.py:538` `_build_table`: name/type/nullable/default/length/precision/scale/comment,
`queries.py:52` `GET_COLUMNS`), но **выбрасывается** после рендера DDL — до diff-слоя не доходит.
Сгенерировать `ALTER TABLE` из «хэши различаются» невозможно. Это и есть prerequisite
ROADMAP для **всех** вариантов генерации ALTER (детерминированного и AI — §6).

## 2. Цель фазы

1. **Структурный column-diff (CD-ALT-1):** колонки таблицы доступны обеим сторонам compare;
   `DiffEntry` для CHANGED-таблицы несёт список изменений уровня колонок
   (added/dropped/type-change/nullable/default), а не только SQL-хэш.
2. **ALTER-план с классификацией (CD-ALT-2..4):** для каждого изменения — стратегия и класс
   `safe / needs-pre-script / blocked`. Add nullable-колонки → safe; drop column /
   alter type / rebuild на таблице с данными → только через покрывающий pre-скрипт.
3. **Генерация дельты (CD-12, CD-13):** SQL-скрипты операций в порядке топосорта, сохранённые
   как **артефакты на диске** + человекочитаемый свод плана (`plan.md`) — review до применения.
4. **Apply-пайплайн (CD-11, CD-14, CD-15):** version-check → safety-gate → pre-скрипты →
   **повторная дельта** (только разрешённые операции, иначе падение) → генерация → применение
   с логированием и stop-on-error → post-скрипты → запись `schema_version`
   (`deploy_source="apply"`).
5. **Детерминированность:** классификация, выбор шаблона ALTER, кавычки, топосорт — только код,
   без AI (ROADMAP §6: CD-AI-3 = Won't). AI-трек (CD-AI-1) надстраивается **после** этой фазы.

## 3. Зафиксировано ранее (не пересматривается)

| Источник | Правило |
|----------|---------|
| ROADMAP §7 п.1-2 | Таблицы с данными: авто-операции запрещены (нет флага «можно потерять данные»); пустые — можно менять/пересоздавать |
| ROADMAP §7 п.3-4 | Новые объекты — всегда можно; не-табличные — деплоятся автоматически |
| ROADMAP §7 п.5-6 | Pre-скрипты — единственный легальный способ трогать таблицы с данными; gate ДО pre |
| ROADMAP §7 п.7 | После pre дельта пересчитывается; остаточные опасные изменения → падение (CD-11) |
| ROADMAP §7 п.8 | Forward-only: target новее source → hard error (SG-6, переиспользуем) |
| Phase 11 final | `covers`-механика, presence-stats, отчёты gate — переиспользуются как есть |

## 4. Открытые вопросы (`USER_INPUT`) — с рекомендацией ИИ

> Закрывает пользователь. После закрытия переносятся в §3 как «принятые» → `_final`.

### ALT-1. Источник column-структуры для diff-слоя
**Рекомендация (a) — autodoc.** RE пишет колонки таблицы в autodoc-заголовок
(`object.columns: [{name, type, nullable, default, …}]`), snapshot-builder читает их оттуда.
Обе стороны compare получают структуру **одинаково**: DB-сторона и так идёт через temp RE
(`compare_service.py:182`), DIR-сторона — из файлов. Это соответствует философии «autodoc =
источник правды для метаданных» (§23) и даёт **ноль изменений контракта адаптера** (§45 не
срабатывает: `GET_COLUMNS` уже существует, новые abstract-методы не нужны).
Старые кодовые базы без `columns` → колонок нет → structural diff недоступен → таблица
возвращается к поведению Phase 11 (changed + данные → needs-pre). Fail-safe, re-RE не обязателен.
Альтернатива (b) — парсить сгенерированный DDL sqlglot'ом на лету: без изменения формата, но
регьекс/AST по собственному выводу — хрупко и дублирует знание, которое RE уже имеет.

### ALT-2. Гранулярность структурного diff в MVP
**Рекомендация: колонки only.** `ColumnDiff` покрывает add/drop/type/nullable/default/comment
колонок. Прочие табличные изменения (constraints, indexes, partitioning) не структурно
диффятся: если `sql_hash` различается, а column-diff пуст/неполный → изменение классифицируется
как `unrepresented` → консервативно needs-pre (при данных) / re-render empty-table path.
Структурный diff констрейнтов/индексов — BACKLOG. Риск занижения: пропущенный ADD CONSTRAINT
может быть safe, но ошибиться в обратную сторону (незамеченная опасность) невозможно —
перестраховка в безопасную сторону.

### ALT-3. Точная таблица классификации операций (CD-ALT-2..4)
**Рекомендация (консервативный whitelist):**

| Изменение | Пустая таблица | Таблица с данными (HAS_DATA/UNKNOWN) |
|---|---|---|
| ADD COLUMN (nullable, без DEFAULT; или DEFAULT = неизменяемое literal / рядом-типов) | safe | **safe** (PG11+ metadata-only; volatile-DEFAULT → needs-pre) |
| ADD COLUMN (volatile DEFAULT: `now()` и т.п.) | safe | needs-pre (rewrites) |
| DROP COLUMN | safe | needs-pre (без `covers` → blocked) |
| ALTER TYPE (в т.ч. расширение `int→bigint`, `varchar(n)+`) | safe | needs-pre (rewrite/lock; всегда) |
| NULLABLE/DEFAULT изменение | safe | needs-pre при сужении (NOT NULL SET, DEFAULT drop); расширение — safe |
| COMMENT (колонки/таблицы) | safe | safe |
| Изменение вне колонок (constraints и пр., ALT-2) | re-render | needs-pre |
| Rebuild таблицы | — | только pre-скриптом, авто — никогда |

Rename колонок **не детектируем** детерминированно (выглядит как drop+add) → трактуется как
DROP+ADD, т.е. needs-pre при данных. Документируем как осознанное ограничение.

### ALT-4. Поверхность команд: одна или две
**Рекомендация: две.** `db-pm deploy plan` — полный dry-run до применения: gate (reuse Phase 11)
+ ALTER-план + **записанные артефакты** (`delta/NNN_*.sql`, `plan.json`, `plan.md`). Ничего не
выполняет, кроме чтения. `db-pm deploy apply` — полный пайплайн §5.6, также пишет те же
артефакты перед выполнением. Review (CD-13) = посмотреть артефакты `plan`; CI может гонять
`plan` как расширенный gate. Альтернатива — одна команда с `--execute` — хуже: путаница
режимов, сложнее в GUI/CI.

### ALT-5. Транзакционность apply
**Рекомендация: дельта — одна транзакция.** DDL в PostgreSQL транзакционен: все скрипты дельты
выполняются в единой транзакции с stop-on-error → откат всей дельты при любой ошибке
(`continue-on-error` для apply не предлагать вовсе — полуприменённая дельта хуже упавшей).
Pre/post-скрипты остаются в AUTOCOMMIT-раннере (Phase 10, идемпотентность + история:
повторный apply скипнет исполненные pre и начнёт с дельты). Запись `schema_version` — после
успешного commit дельты и post-фазы.

### ALT-6. Обработка REMOVED-объектов
**Рекомендация: никогда не дропать автоматически.** REMOVED (есть в БД, нет в коде) → в плане
появляется как `blocked`-запись + **закомментированный** `DROP ...` в артефакте; применение
требует явного флага `--include-drops` (и для таблиц с данными — всё равно только через
pre-скрипт). Соответствует принципу «явность > магия»: кодовая база не удалила объект —
возможно, RE снял не всё.

### ALT-7. GUI в этой фазе
**Рекомендация: без GUI-действий.** `deploy plan`/`apply` — CLI-only. Apply — первая
мутирующая живую БД команда; GUI-обёртка (run-only по образцу Phase 11) имеет смысл после
обкатки CLI — Phase 14/backlog. Рендер плана в Delta Viewer — туда же.

## 5. Предлагаемая архитектура

### 5.1. RE: колонки в autodoc (ALT-1a)
- `build_metadata(...)` / `sql_generator` для `table` добавляют секцию `columns:` (additive;
  парсер-фолбэк: отсутствует → пусто). Поля из `_build_table` как есть; `default` уже
  schema-qualified (§34).
- Автодоговый парсер (`pg_sql_parser`) кладёт колонки в `vertex.extra["columns"]` (механика
  `extra` уже существует — Phase 5 `db_properties`).
- `build_snapshot_from_dir` пробрасывает их в `ObjectSnapshot.columns: list[ColumnSnapshot]`
  (additive, default `[]` — старые `source.json` парсятся, §28 roundtrip-тест).

### 5.2. Domain-модели — `domain/delta.py` (новый; pydantic, по образцу `domain/safety.py`)
```python
class ColumnSnapshot:            # одна колонка стороны compare
    name: str
    type: str                    # canonical (udt_name + модификаторы)
    nullable: bool
    default: str | None
    # length/precision/scale/comment — при необходимости

class ColumnChangeKind(str, Enum):
    ADDED = "added"; DROPPED = "dropped"; TYPE_CHANGED = "type_changed"
    NULLABILITY_CHANGED = "nullability_changed"; DEFAULT_CHANGED = "default_changed"
    COMMENT_CHANGED = "comment_changed"

class ColumnDiff:
    column: str; kind: ColumnChangeKind
    source_column: ColumnSnapshot | None
    target_column: ColumnSnapshot | None

class OperationClass(str, Enum):  # CD-ALT-2
    SAFE = "safe"; NEEDS_PRE = "needs_pre"; BLOCKED = "blocked"

class PlannedOperation:
    object_key: str; object_type: str; object_schema/name
    action: Literal["create", "alter", "rerender", "drop", "skip"]
    column_diffs: list[ColumnDiff]          # таблицы
    classification: OperationClass
    reason: str                             # человеческое «почему»
    script_file: str                        # артефакт

class DeltaPlan:
    operations: list[PlannedOperation]      # в порядке применения (топосорт)
    source_version / target_version / db_type
    @property violations / needs_pre_ops ...
```

### 5.3. Comparator: column-diff для CHANGED-таблиц (CD-ALT-1)
В `compare()` для `status=CHANGED, object_type=table` при непустых `columns` с обеих сторон —
множественное сравнение по имени колонки → `column_diffs` в `DiffEntry` (additive поле).
Обе стороны без колонок → пусто (поведение Phase 11 сохранено). Чистая функция на dict'ах,
без I/O — табличные unit-тесты на синтетических парах «было/стало» (acceptance CD-ALT-1).

### 5.4. Классификатор + ALTER-рендер — `infrastructure/deploy/alter_plan.py` (новый)
- `classify(entry, presence, coverage) -> PlannedOperation`: чистая функция, таблица решений
  ALT-3 + правила Phase 11 (наличие данных, `covers`). Domain-логика без имён каталогов БД.
- `render_alter(PlannedOperation) -> str`: генерация DDL `ALTER TABLE "s"."t" ...` —
  fully-qualified + `_quote_identifier` whitelist (§19, §34/§35 — контракт тестом
  `assert '"schema"."table"' in ddl`). PG-диалект; `db_type` из manifest — guard
  (greenplum совместим с PG-DDL; прочие — hard error, как compare).

### 5.5. Сборка дельты — `application/delta_service.py` (новый)
Вход: `DiffReport` (CompareService), presence-stats, coverage, граф кодовой базы (топосорт
порядка `deploy_order`). Выход: `DeltaPlan` + артефакты:
- `delta/NNN_<type>_<schema>_<name>.sql` — по операции, номер = позиция в порядке применения;
  create/rerender — тело из файла объекта (strip autodoc, §23); alter — рендер §5.4;
  drop — закомментирован (ALT-6); comment-only — skip (`has_executable_sql`, §49).
- `plan.json` (машиночитаемый `DeltaPlan`) + `plan.md` (свод: операции, классификация,
  причины, presence, покрытие) — по образцу `safety_report.py`.

### 5.6. Apply-пайплайн — `application/deploy_apply_service.py` (новый)
```
1. manifest + db_type + version-check (SG-6, hard error при target > source)
2. safety-gate (SafetyGateService.analyze, reuse): violations → падение ДО pre-скриптов
3. pre-скрипты: ScriptRunner.run_phase("pre") — идемпотентно, история в __deploy
4. повторная дельта (CD-11): свежий CompareService.run + классификация;
   любые не-SAFE операции на таблицах с данными → падение (semantics: pre-скрипт обязан
   довести покрытые таблицы до состояния, где остаточная дельта безопасна)
5. генерация артефактов (§5.5) — на диск, лог путей
6. применение дельты: одна транзакция (ALT-5), stop-on-error → rollback;
   лог каждого шага (progress-callback, как DeployValidateService)
7. post-скрипты: ScriptRunner.run_phase("post"); ошибка → пайплайн падает (CD-16 — Phase 13,
   здесь минимально: та же семантика, что в validate-флоу)
8. record_schema_version(service_schema, source_version, "apply") (CD-15)
```
`deploy plan` = шаги 1-2 + 4-5 (без 3, 6-8; повторная дельта на живой БД без pre — та же,
что на шаге 2, поэтому plan = gate + артефакты).

### 5.7. CLI — `presentation/cli/main.py`
`deploy plan --dir --target-connection-file --output-dir` и
`deploy apply --dir --target-connection-file --output-dir [--include-drops]`.
Exit codes как у analyze: 0 — ок; 1 — нарушения/needs-pre без pre; 2 — hard error.
`--help` явно: «apply ИЗМЕНЯЕТ целевую БД».

### 5.8. Reuse / не-дублирование
- CompareService — единственный источник дельты (анализ, план и apply используют один отчёт).
- SafetyGateService — шаг 2 apply (не копия логики gate).
- ScriptRunner, `record_schema_version`, `covers`, presence-stats — Phase 10/11 как есть.
- Топосорт — `graph_service.deploy_order` (порядок применения дельты = порядок deploy).

## 6. Проверки (для `_plan`)

- **Unit column-diff:** синтетические пары ObjectSnapshot с columns — add/drop/type/nullable/
  default/comment; смешанные; колонок нет у одной стороны → пустой diff; rename = drop+add.
- **Unit классификатора:** матрица ALT-3 (изменение × presence × coverage) → класс+reason;
  unrepresented → needs-pre; REMOVED → blocked без `--include-drops`.
- **Unit рендера ALTER:** fully-quoted идентификаторы (§35 контракт-тест); special-char имена
  отклоняются whitelist'ом (§19); comment-only → skip (§49).
- **Unit DeltaPlan/артефакты:** порядок скриптов = deploy_order; нумерация стабильна;
  `plan.json` roundtrip (§28); `plan.md` содержит классификацию и причины.
- **Unit apply (fake adapter):** violations на шаге 2 → ни один мутирующий вызов; не-SAFE
  остаток на шаге 4 → падение, дельта не применена; успешный сценарий → порядок вызовов
  (pre → delta → post → record_version="apply"); ошибка в середине дельты → rollback
  (транзакция) и НЕ запись версии.
- **Unit CLI:** план/apply строят вызовы; exit codes; `--include-drops` влияет только на REMOVED.
- **Integration (testcontainers):** (1) таблица с данными + add nullable column → plan: safe,
  apply проходит, колонка появилась, версия записана; (2) drop column на таблице с данными
  без pre → plan: needs-pre/blocked, apply падает ДО pre; (3) то же + pre-скрипт с `covers`,
  который сам делает ALTER → apply проходит, повторная дельта чиста (CD-11); (4) ошибка в
  дельте → rollback, повторный apply идемпотентен (pre скипается); (5) старая кодовая база без
  `columns` в autodoc → поведение Phase 11 (fail-safe).

## 7. NOT done / отложено

- **AI-трек** (CD-AI-1 pre-backfill черновики, CD-AI-2 объяснения) — overlay после Phase 12
  (ROADMAP §6; prerequisite — column-diff — закрывается здесь).
- **Структурный diff констрейнтов/индексов/partitioning** — BACKLOG (ALT-2).
- **Detect column rename** — осознанно никогда в авто-классификации (только drop+add).
- **Post-deploy отчёты, side-by-side DDL diff, история версий в GUI** (CD-16..18) — Phase 13.
- **GUI-действия plan/apply + рендер плана в Delta Viewer** — Phase 14/backlog (ALT-7).
- **Greenplum:** распределённые таблицы (ALTER может требовать распределённых операций) —
  валидация при появлении кластера; presence уже fail-safe.

## 8. Чеклист по урокам

- [ ] §3: оценка данных — только метаданные (presence-stats Phase 11), без COUNT.
- [ ] §12: `git add -- "_docs_/_tasks_/phase_12/..."` (дефис в `_docs_`).
- [ ] §19: все идентификаторы в генерируемом DDL — whitelist + double-quote (`_quote_identifier`).
- [ ] §23: тела объектов в дельту — после `strip_autodoc`.
- [ ] §26/§28: identity колонок — по имени в рамках таблицы; тесты JSON — roundtrip, не substring.
- [ ] §34/§35: ALTER/CREATE в дельте — fully-qualified `"schema"."table"."column"`; контракт-тест.
- [ ] §45: **цель фазы — ноль новых abstract-методов адаптера** (колонки через autodoc, ALT-1);
  если всё же понадобится — все fakes в одном коммите.
- [ ] §49: skip comment-only скриптов дельты через `has_executable_sql`.
- [ ] Phase 11 §48: порядок действий в integration-тестах, где свойство зависит от
  синхронизации состояний (RE/версии) — фиксировать явно.
- [ ] TASK_CONVENTIONS §6: код и документы в разных коммитах; vision_draft — отдельный коммит
  `docs(tasks): phase_12 vision draft`.

## 9. Где читать дальше

- `_tasks_/ROADMAP.md` §4 (CD-ALT-1..15), §7 (правила), §8 (пайплайн шаги 3-9), §6 (AI Won't)
- `_tasks_/phase_11/Phase_11_vision_final.md` — gate/presence/covers/отчёты (переиспользование)
- `_phases_/Phase_09.md`, `_phases_/Phase_10.md` — compare и `__deploy`-механика
- `src/db_project_manager/infrastructure/diff/comparator.py` — точка расширения column-diff
- `src/db_project_manager/application/deploy_service.py` — образец оркестрации/прогресса/ошибок
