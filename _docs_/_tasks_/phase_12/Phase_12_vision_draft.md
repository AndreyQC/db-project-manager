# Phase 12: ALTER + Delta — драфт дизайна (vision draft)

> **Дата:** 2026-08-15
> **Ветка:** dev
> **Статус:** draft (закрыты ALT-1..ALT-7; открыт новый `USER_INPUT` ALT-8 — механика
> засева данных для репетиции; после закрытия → `_final`)
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
4. **Apply-пайплайн (CD-11, CD-14, CD-15):** version-check → safety-gate → **репетиция на
   аналоге таргета** (ALT-5: клон состояния таргета в temp-БД + данные + прогон полного
   пайплайна) → при успехе применение к живому таргету с stop-on-error → post-скрипты →
   запись `schema_version` (`deploy_source="apply"`).
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

## 4. Вопросы (`USER_INPUT`) — ALT-1..ALT-7 закрыты, открыт ALT-8

> Закрываются пользователем. Закрытые решения фиксируются в тексте пункта; после закрытия
> ALT-8 → `_final`.

### ALT-1. Источник column-структуры для diff-слоя — ЗАКРЫТО: вариант (b) парсинг SQL-тела
**Решение (выбрано пользователем):** колонки **не хранятся в autodoc** — они извлекаются
парсингом SQL-тела при snapshot-build. SQL-скрипт = единственный источник правды.

Пересмотрено после вопроса о code-first сценарии: autodoc-вариант (a) создаёт второй
источник правды (autodoc дублирует тело) и конкретный баг-сценарий: пользователь добавил
колонку в SQL, прогнал RE (autodoc запомнил), затем удалил колонку из SQL руками →
stale autodoc «видит» колонку, которой нет ни в текущем SQL, ни в БД → column-diff даёт
ADDED → классификация safe → **неправильная авто-операция** ALTER ADD. Без сверки опасно;
сверка же требует того же парсинга тела, что и вариант (b) — тогда хранение избыточно.

Механика (b):
- snapshot-builder уже прогоняет каждое тело через sqlglot (`normalize_sql` для хэша) —
  извлечение `ColumnDef` из `Create`-узла почти бесплатно, без новых зависимостей;
- обе стороны симметричны: DIR-сторона — файлы кодовой базы, DB-сторона — temp RE →
  канонический DDL → тот же парсер; **ноль изменений контракта адаптера** (§45 не
  срабатывает: `GET_COLUMNS` уже существует, новые abstract-методы не нужны);
- цена: карта синонимов типов (`integer`→`int4`, `character varying`→`varchar`; RE пишет
  канонические `udt_name`, пользователь — как хочет) — ограниченный словарь, покрытый
  unit-тестами;
- парсинг не удался / колонок не найдено (`PARTITION OF`, экзотика) → `columns=None` →
  structural diff недоступен → fail-safe needs-pre при данных, помечено в отчёте;
- autodoc остаётся компактным identity-only (§23), формат файлов не меняется.

### ALT-2. Гранулярность структурного diff в MVP — ЗАКРЫТО: колонки only (согласовано)
`ColumnDiff` покрывает add/drop/type/nullable/default/comment
колонок. Прочие табличные изменения (constraints, indexes, partitioning) не структурно
диффятся: если `sql_hash` различается, а column-diff пуст/неполный → изменение классифицируется
как `unrepresented` → консервативно needs-pre (при данных) / re-render empty-table path.
Структурный diff констрейнтов/индексов — BACKLOG. Риск занижения: пропущенный ADD CONSTRAINT
может быть safe, но ошибиться в обратную сторону (незамеченная опасность) невозможно —
перестраховка в безопасную сторону.

### ALT-3. Точная таблица классификации операций (CD-ALT-2..4) — ЗАКРЫТО (согласовано)
Принята матрица (консервативный whitelist):

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

### ALT-4. Поверхность команд: одна или две — ЗАКРЫТО: две (согласовано)
`db-pm deploy plan` — полный dry-run до применения: gate (reuse Phase 11)
+ ALTER-план + **записанные артефакты** (`delta/NNN_*.sql`, `plan.json`, `plan.md`). Ничего не
выполняет, кроме чтения. `db-pm deploy apply` — полный пайплайн §5.6, также пишет те же
артефакты перед выполнением. Review (CD-13) = посмотреть артефакты `plan`; CI может гонять
`plan` как расширенный gate. Альтернатива — одна команда с `--execute` — хуже: путаница
режимов, сложнее в GUI/CI.

### ALT-5. Транзакционность apply — ЗАКРЫТО: репетиция на аналоге таргета (решение пользователя)
**Решение:** единая транзакция на весь деплой **отвергнута** — по практике обернуть весь
деплой в одну транзакцию практически невозможно и опасно (долгие локи на живой БД,
невозможность `CREATE INDEX CONCURRENTLY` и пр.). Вместо rollback-гарантии — **репетиция**:

1. В пустую temp-БД накатывается **текущее состояние таргета**: RE(target) → temp-кодовая
   база → deploy (механика `DeployValidateService`, но с `keep_db` и без дропа) —
   воспроизводит структуру живой цели, включая `__deploy` (RE seed'ит его, Phase 10 S6).
2. В репетиционную БД добавляются **данные** (минимум по записи на таблицу — механика
   засева = ALT-8).
3. Против репетиционной БД прогоняется **полный apply-пайплайн** (pre → дельта → post →
   версия). Любой сбой = отладка на аналоге, таргет не тронут.
4. Репетиция зелёная → тот же пайплайн против **живого таргета**.

Свойства модели:
- **Скрипты дельты — без mega-транзакции**: каждое выполняется отдельным `execute_script`
  (AUTOCOMMIT), stop-on-error, `continue-on-error` не предлагается вовсе.
- **Восстановление после сбоя на таргете** не через rollback, а через пересчёт дельты:
  apply всегда стартует со свежего compare — частично применённая дельта просто станет
  короче; исполненные pre/post скипаются по `script_history` (идемпотентность Phase 10).
  Повторный run «доводит» деплой до конца.
- **Цена:** RE таргета + полный deploy его состояния в temp-БД на каждый apply (требует
  CREATEDB, как validate). Принимается осознанно — это плата за безопасность без локов.
- Репетиция опционально отключается флагом `--no-rehearsal` (для CI, где таргет и так
  одноразовый).

### ALT-8. Механика засева данных репетиционной БД — ОТКРЫТ (появился из ALT-5)
**Рекомендация (a) — пользовательские seed-скрипты.** Опциональный каталог
`<codebase>/__migrations/seed/*.sql` (рядом с pre/post; конвенция имён та же):
выполняются репетиционным пайплайном после наката состояния таргета, до apply. Пусто/нет
каталога → репетиция без данных (всё ещё ловит структурные/зависимостные ошибки и ошибки
pre-скриптов на пустых таблицах). Плюсы: детерминированно, FK-порядок и NOT NULL-колонки —
зона ответственности автора (как у pre-скриптов), ноль магии; seed НЕ выполняется на живом
таргете никогда (только в репетиции).
Альтернатива (b) — авто-генератор «одной записи на таблицу» из DEFAULT-значений (таблицы с
NOT NULL-колонками без DEFAULT — skip с warning): удобно, но FK-порядок вставок и кастомные
constraints делают его ненадёжным; BACKLOG P3 как удобство поверх (a).
Отмечу: при классификации ALT-3 до авто-дельты на таблицах с данными всё равно доходят
только SAFE-операции — seed критичен прежде всего для **pre-скриптов** (им нужны данные,
чтобы отработать/упасть по-настоящему).

### ALT-6. Обработка REMOVED-объектов — ЗАКРЫТО: никогда не дропать автоматически (согласовано)
REMOVED (есть в БД, нет в коде) → в плане
появляется как `blocked`-запись + **закомментированный** `DROP ...` в артефакте; применение
требует явного флага `--include-drops` (и для таблиц с данными — всё равно только через
pre-скрипт). Соответствует принципу «явность > магия»: кодовая база не удалила объект —
возможно, RE снял не всё.

### ALT-7. GUI в этой фазе — ЗАКРЫТО: без GUI-действий (согласовано)
`deploy plan`/`apply` — CLI-only. Apply — первая
мутирующая живую БД команда; GUI-обёртка (run-only по образцу Phase 11) имеет смысл после
обкатки CLI; рендер плана в Delta Viewer — туда же. По USER_INPUT: GUI-действия
plan/apply + рендер плана — **добавлены в BACKLOG** (см. `_tasks_/BACKLOG.md`, P3
«GUI-действия deploy plan/apply + рендер плана»).

## 5. Предлагаемая архитектура

### 5.1. Извлечение колонок — `infrastructure/diff/columns.py` (новый, ALT-1b)
- `extract_columns(body: str) -> list[ColumnSnapshot] | None`: sqlglot
  `parse_one(body, read="postgres")` → узел `Create` → `ColumnDef`-список → нормализация типов
  через карту синонимов (`integer`→`int4` и т.п.). `None` = не удалось извлечь (не CREATE
  TABLE, `PARTITION OF`, экзотика) → fail-safe.
- Интеграция в `build_snapshot_from_dir`: парсинг уже выполняется там для хэша
  (`normalize_sql`) — refactor: один `parse_one`, из него и хэш, и колонки (для `table`
  только; остальным типам `columns=None`).
- Стороны симметричны: DIR-сторона — файлы кодовой базы (в т.ч. написанные руками),
  DB-сторона — temp RE → канонический DDL → тот же экстрактор. Дрейф «метаданные vs SQL»
  невозможен по построению — SQL-тело единственный источник правды.
- Autodoc не меняется: остаётся identity-only (§23); формат файлов и обратная совместимость
  старых кодовых баз — без изменений (колонки извлекаются из того, что уже есть в файле).

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
Ядро — `_run_pipeline(adapter, codebase_dir, …)`, один и тот же код для репетиции и таргета:
```
1. manifest + db_type + version-check (SG-6, hard error при target > source)
2. safety-gate (SafetyGateService.analyze, reuse): violations → падение ДО pre-скриптов
3. pre-скрипты: ScriptRunner.run_phase("pre") — идемпотентно, история в __deploy
4. повторная дельта (CD-11): свежий CompareService.run + классификация;
   любые не-SAFE операции на таблицах с данными → падение (semantics: pre-скрипт обязан
   довести покрытые таблицы до состояния, где остаточная дельта безопасна)
5. генерация артефактов (§5.5) — на диск, лог путей
6. применение дельты: по одному execute_script на операцию (AUTOCOMMIT, ALT-5 —
   без mega-транзакции), stop-on-error; лог каждого шага (progress-callback)
7. post-скрипты: ScriptRunner.run_phase("post"); ошибка → пайплайн падает
8. record_schema_version(service_schema, source_version, "apply") (CD-15)
```
`deploy apply` (с репетицией, ALT-5):
```
A. РЕПЕТИЦИЯ (если не --no-rehearsal):
   a1. RE(target) → temp-кодовая база (manifest наследует version таргета, §48)
   a2. DeployValidateService-механика наката этой кодовой базы в temp-БД (keep_db;
       CREATEDB обязателен) — состояние таргета воспроизведено, __deploy создан RE
   a3. seed: __migrations/seed/*.sql (ALT-8) — только здесь, не на живом таргете
   a4. _run_pipeline против репетиционной БД; сбой → падение, таргет не тронут,
       temp-БД можно оставить для отладки (флаг/лог имени)
   a5. cleanup temp-БД (по умолчанию дропается; --keep-rehearsal-db для отладки)
B. ЦЕЛЬ: _run_pipeline против живого таргета (fresh compare → дельта, pre/post
   скипнутся по script_history, если уже применялись в прошлый раз)
```
`deploy plan` = шаги 1-2 + 4-5 (без 3, 6-8; повторная дельта на живой БД без pre — та же,
что на шаге 2, поэтому plan = gate + артефакты).

### 5.7. CLI — `presentation/cli/main.py`
`deploy plan --dir --target-connection-file --output-dir` и
`deploy apply --dir --target-connection-file --output-dir [--include-drops]
[--no-rehearsal] [--keep-rehearsal-db]`.
Exit codes как у analyze: 0 — ок; 1 — нарушения/needs-pre без pre; 2 — hard error.
`--help` явно: «apply ИЗМЕНЯЕТ целевую БД (после репетиции на temp-аналоге)».

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
  (pre → delta → post → record_version="apply"); ошибка в середине дельты → стоп, версия
  НЕ записана; повторный прогон после частичного применения → дельта короче (пересчёт),
  pre скипается по script_history.
- **Unit rehearsal (fake adapter):** сбой пайплайна на репетиционной БД → таргет не тронут
  (ни одного вызова против таргет-конфига); seed-скрипты выполняются только против
  репетиции; `--no-rehearsal` пропускает фазу A; temp-БД дропается по умолчанию.
- **Unit CLI:** план/apply строят вызовы; exit codes; `--include-drops` влияет только на REMOVED.
- **Integration (testcontainers):** (1) таблица с данными + add nullable column → plan: safe,
  apply проходит (репетиция + таргет), колонка появилась, версия записана; (2) drop column
  на таблице с данными без pre → plan: needs-pre/blocked, apply падает ДО pre; (3) то же +
  pre-скрипт с `covers`, который сам делает ALTER → apply проходит, повторная дельта чиста
  (CD-11); (4) ошибка в скрипте дельты на репетиции → apply падает, таргет не изменён,
  повторный apply после исправления идемпотентен (pre скипается, дельта пересчитана);
  (4a) ошибка в скрипте дельты на самом таргете (rehearsal отключена) → стоп, повторный
  apply доводит остаток дельты; (5) DDL, из которого колонки не извлекаются (напр.
  `PARTITION OF`-наследник) → `columns=None` → fail-safe needs-pre при данных;
  (6) тип-синонимы: `integer` в кодовой базе против `int4` в БД → TYPE_CHANGED **не**
  возникает; (7) seed-каталог: скрипты выполняются в репетиции и не выполняются на таргете.

## 7. NOT done / отложено

- **AI-трек** (CD-AI-1 pre-backfill черновики, CD-AI-2 объяснения) — overlay после Phase 12
  (ROADMAP §6; prerequisite — column-diff — закрывается здесь).
- **Структурный diff констрейнтов/индексов/partitioning** — BACKLOG (ALT-2).
- **Detect column rename** — осознанно никогда в авто-классификации (только drop+add).
- **Post-deploy отчёты, side-by-side DDL diff, история версий в GUI** (CD-16..18) — Phase 13.
- **GUI-действия plan/apply + рендер плана в Delta Viewer** — BACKLOG (добавлено по
  USER_INPUT ALT-7; см. `_tasks_/BACKLOG.md`).
- **Greenplum:** распределённые таблицы (ALTER может требовать распределённых операций) —
  валидация при появлении кластера; presence уже fail-safe.

## 8. Чеклист по урокам

- [ ] §3: оценка данных — только метаданные (presence-stats Phase 11), без COUNT.
- [ ] §12: `git add -- "_docs_/_tasks_/phase_12/..."` (дефис в `_docs_`).
- [ ] §19: все идентификаторы в генерируемом DDL — whitelist + double-quote (`_quote_identifier`).
- [ ] §23: тела объектов в дельту — после `strip_autodoc`.
- [ ] §26/§28: identity колонок — по имени в рамках таблицы; тесты JSON — roundtrip, не substring.
- [ ] §34/§35: ALTER/CREATE в дельте — fully-qualified `"schema"."table"."column"`; контракт-тест.
- [ ] §44: sqlglot-API (`Create`/`ColumnDef`, извлечение типов) — smoke-тест актуального вызова
  **до** написания `columns.py` (API дрейфует между версиями).
- [ ] §45: **цель фазы — ноль новых abstract-методов адаптера** (колонки из SQL-тела,
  ALT-1b; репетиция переиспользует существующие методы); если всё же понадобится — все
  fakes в одном коммите.
- [ ] §48: RE(target) в репетиции наследует version таргета — порядок действий и
  ожидания в тестах фиксировать явно.
- [ ] §49: skip comment-only скриптов дельты через `has_executable_sql`.
- [ ] TASK_CONVENTIONS §6: код и документы в разных коммитах; vision_draft — отдельный коммит
  `docs(tasks): phase_12 vision draft`.

## 9. Где читать дальше

- `_tasks_/ROADMAP.md` §4 (CD-ALT-1..15), §7 (правила), §8 (пайплайн шаги 3-9), §6 (AI Won't)
- `_tasks_/phase_11/Phase_11_vision_final.md` — gate/presence/covers/отчёты (переиспользование)
- `_phases_/Phase_09.md`, `_phases_/Phase_10.md` — compare и `__deploy`-механика
- `src/db_project_manager/infrastructure/diff/comparator.py` — точка расширения column-diff
- `src/db_project_manager/application/deploy_service.py` — образец оркестрации/прогресса/ошибок
