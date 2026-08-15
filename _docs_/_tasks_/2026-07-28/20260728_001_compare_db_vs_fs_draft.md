# Сравнение состояния БД с файловой системой (Phase 9) — draft

> **Дата:** 2026-07-28
> **Ветка:** dev
> **Статус документа:** draft (дизайн готов к плану→результату; все USER_INPUT закрыты)

> Контекст:
> - `_docs_/REFRESH_CONTEXT.md` — точка входа в проект
> - `_docs_/_checkpoints_/20260725_002_checkpoint.md` — последний checkpoint (Phase 7 closed)
> - `_docs_/_tasks_/BACKLOG.md` — открытые задачи; Phase 8 = overload resolution (P1)
> - `src/db_project_manager/domain/connection.py` — `ConnectionConfig.type` (`postgres|greenplum`)
> - `src/db_project_manager/domain/graph.py` — `Vertex`, `Edge`, `DependencyGraph`
> - `src/db_project_manager/application/reverse_engineer.py` — `ReverseEngineerService.run()`
> - `src/db_project_manager/application/graph_service.py` — `BuildGraphService.build()`
> - `src/db_project_manager/infrastructure/sql/sql_generator.py` — `SQLGenerator.generate_scripts()`
> - `src/db_project_manager/infrastructure/sql/autodoc.py` — autodoc-заголовок, `extract_header`
> - `src/db_project_manager/presentation/cli/main.py` — typer-приложение, подгруппы `graph`/`deploy`
> - `src/db_project_manager/infrastructure/database/postgres/queries.py` — каталог SQL-запросов
> - `src/db_project_manager/infrastructure/parsing/pg_sql_parser.py` — парсер codebase→graph
> - `LESSONS_LEARNED.md` §3, §9, §11, §18, §27, §35, §42 — релевантные уроки

---

## 1. Постановка задачи

Инструмент должен уметь сравнивать состояние текущей БД с тем, что лежит в виде
файлов в файловой системе (как результат `reverse-engineer`), и формировать отчёт о
расхождениях. Процесс (как сформулировал пользователь):

1. Получить из БД выбранного подключения метаданные таблиц, включая `reltuples`
   (оценка наличия данных). SQL-запрос:
   ```sql
   SELECT n.nspname AS schema_name,
          c.relname AS table_name,
          c.reltuples AS estimated_rows
   FROM pg_class c
   JOIN pg_namespace n ON n.oid = c.relnamespace
   WHERE c.relkind = 'r'
     AND n.nspname NOT IN ('pg_catalog', 'information_schema')
   ORDER BY c.reltuples DESC NULLS LAST;
   ```
2. Сформировать в новом каталоге (указанном пользователем) `temp_model_dir` все файлы
   для этой базы (через существующий `ReverseEngineerService`).
3. Построить граф для неё (через существующий `BuildGraphService`).
4. Построить граф для базы, представленной каталогом в ФС.
4. Выполнить сравнение и показать diff в зависимости от порядка, заданного
   параметром `diff_type`.

Каждая сторона сравнения (source / target) — это **либо** подключение к БД, **либо**
уже сформированный каталог reverse-engineer. Сравнение формирует отчёт на диске в
формате, пригодном для последующего использования (машинно-читаемый).

**Дополнительное требование (пользователь, 2026-07-28):** сравнение должно работать
только для совместимых пар типов БД (PG↔PG, GP↔GP). Тип БД хранится в
`ConnectionConfig.type`, но при reverse-engineer **не сохраняется в каталог** — это
разрыв, который нужно закрыть. Решение: при reverse-engineer писать в корень
каталога манифест (JSON-метаданные) с типом БД; сравнение читает манифест обеих
сторон и проверяет совместимость.

---

## 2. Принятые решения (закрытые USER_INPUT)

Все ключевые развилки согласованы с пользователем до составления плана.

| # | Параметр | Решение | Обоснование |
|---|---|---|---|
| 1 | Гранулярность MVP | **Наличие объектов + структура (нормализованный SQL)** | Сравнение множеств `object_key` (added/removed) + для общих — сравнение нормализованного SQL (changed/unchanged). Графовые рёбра → отдельная задача (см. §6). |
| 2 | Типы объектов | **tables + views + materialized_views + functions + procedures + sequences** | Структурный diff схемы. Extensions и database_settings исключены: они часто отличаются между средами и шумят в отчёте. |
| 3 | Формат отчёта | **Сырой снимок двух состояний** | `source.json`, `target.json` (полные снимки) + `diff_report.json` (результат сравнения). Машинно-читаемый JSON, для дальнейшей обработки/импорта. |
| 4 | Указание стороны (source/target) | **Явный флаг типа** | `--source-dir` XOR `--source-connection-file`, аналогично для target. Явное лучше авто-детекта (неопределённость «строка — это путь или имя подключения?»). |
| 5 | Нормализация SQL | **sqlglot AST** | `sqlglot.parse_one(...).normalize().sql(dialect="postgres")`. Игнорирует порядок колонок в `CREATE TABLE`, регистр ключевых слов, форматирование. Новая зависимость `sqlglot` (~pure-python, есть wheel для Windows). |
| 6 | Row counts (`reltuples`) | **Да, информационная метка** | Лежит в `source.json`/`target.json` рядом с таблицами, не влияет на структурный diff. Полесно оценить масштаб расхождений. Запрос из §1 идёт в `queries.py`. |
| 7 | `temp_model_dir` | **`--keep-model-dir`** | Reverse-engineer БД-источника выполняется во временный каталог (`tempfile.mkdtemp()`); по умолчанию чистится в `finally`, флаг сохраняет для аудита. Аналог `--keep-db` в `deploy validate`. |
| 8 | Что лежит в снимке | **Снимок объектов без рёбер** | Список `ObjectSnapshot` (object_key + метаданные + нормализованный SQL + hash + estimated_rows). Без `Edge` — их сравнение в BACKLOG. |
| 9 | `diff_type` | **Тип источника** | Управляется флагами `--source-dir`/`--source-connection-file` (а не отдельным параметром). `added` = есть в source, нет в target; `removed` = наоборот. |
| **10** | **Где хранить тип БД** | **Отдельный `dbpm.manifest.json` в корне каталога reverse-engineer** | Манифест = свойства всей БД (тип, имя, время генерации); autodoc = свойства объекта. Чистое разделение: сравнение читает манифест обеих сторон. Не зависит от SQL/autodoc. |
| **11** | **Несовместимые типы БД** | **Hard error** | Сравнение PG↔GP или PG↔(каталог-без-манифеста) прерывается с понятной ошибкой и exit code 2. Безопасно: пользователь не получит бессмысленный diff между разными СУБД. |
| **12** | **Старые каталоги (без манифеста)** | **Требовать манифест** | Каталог без `dbpm.manifest.json` нельзя сравнивать: пользователю предлагается повторный reverse-engineer (который теперь пишет манифест). Строго, но избегает неоднозначности типа. |

### 2.1. Матрица совместимости типов БД

Сравнение допускается, когда типы **строго равны**. Текущие типы БД (`SUPPORTED_DB_TYPES`
в `domain/connection.py`):

| Тип | Совместим с |
|---|---|
| `postgres` | `postgres` |
| `greenplum` | `greenplum` |

Будущие типы (`snowflake`, `mssql` — пока не реализованы) добавятся в эту матрицу по
мере появления адаптеров. PG↔GP **не разрешается** (даже при общей PostgreSQL-основе):
могут различаться типы данных, распределения таблиц (GP-specific `DISTRIBUTED BY`),
системные схемы — структурный diff был бы шумным и потенциально вводящим в заблуждение.

---

## 3. Архитектурный набросок (для последующего `_plan`/`_result`)

### 3.1. Новые файлы

| Путь | Назначение |
|---|---|
| `src/db_project_manager/domain/diff.py` | Pydantic-модели: `ObjectSnapshot`, `StateSnapshot`, `DiffEntry`, `DiffReport`, `CodebaseManifest`, enum `SnapshotSourceKind`, `DiffStatus`. |
| `src/db_project_manager/infrastructure/diff/__init__.py` | Пакет diff-инфраструктуры. |
| `src/db_project_manager/infrastructure/diff/snapshot.py` | `build_snapshot_from_dir(codebase_dir, *, source_kind, source_ref, db_type, row_counts=None) -> StateSnapshot` — читает граф + SQL каждой вершины, нормализует, хеширует. |
| `src/db_project_manager/infrastructure/diff/normalize_sql.py` | `normalize_sql(sql, *, dialect="postgres") -> str` (sqlglot + regex-fallback), `sql_hash(sql) -> str` (8 hex от нормализованного). |
| `infrastructure/diff/snapshot.py` | `build_snapshot_from_dir(codebase_dir, *, source_kind, source_ref, row_counts=None) -> StateSnapshot` — читает граф + SQL каждой вершины, нормализует, хеширует. |
| `src/db_project_manager/infrastructure/diff/comparator.py` | `compare(source: StateSnapshot, target: StateSnapshot) -> DiffReport`. |
| `src/db_project_manager/application/compare_service.py` | `CompareService.run(source_spec, target_spec, output_dir, *, keep_model_dir, progress) -> Path` — оркестрация: reverse-engineer при необходимости, проверка совместимости типов, снимки обеих сторон, сравнение, запись отчёта. |
| `src/db_project_manager/infrastructure/config/codebase_manifest.py` | `CodebaseManifest` (pydantic): `{db_type, database, generated_at, tool_version}`. `write_manifest(manifest, codebase_root)`, `read_manifest(codebase_root) -> CodebaseManifest`, `ManifestError`. |

### 3.2. Изменения в существующих файлах

| Файл | Изменение |
|---|---|
| `src/db_project_manager/infrastructure/database/postgres/queries.py` | + `GET_TABLE_ROW_COUNTS` (SQL из §1). |
| `src/db_project_manager/infrastructure/database/base.py` | + абстрактный метод `get_table_row_counts() -> list[...]` в `DatabaseAdapter`. |
| `src/db_project_manager/infrastructure/database/postgres/adapter.py` | + реализация `get_table_row_counts()` через `self._exec(GET_TABLE_ROW_COUNTS, {})`. |
| `tests/conftest.py` | + `get_table_row_counts()` в `FakeAdapter` (no-op/заглушка) — по уроку §18. |
| `src/db_project_manager/application/reverse_engineer.py` | `ReverseEngineerService.run` пишет `dbpm.manifest.json` в корень каталога после генерации. Манифест = `{db_type: conn_cfg.type, database: conn_cfg.database, generated_at: UTC iso, tool_version}`. |
| `src/db_project_manager/presentation/cli/main.py` | + подгруппа `compare_app = typer.Typer(...)` + `app.add_typer(compare_app, name="compare")` + команда `@compare_app.command("run")`. |
| `pyproject.toml` | + зависимость `sqlglot` (проверить наличие wheel для win32 — урок §10). |

### 3.3. Доменные модели (`domain/diff.py`)

```python
class SnapshotSourceKind(str, Enum):
    DB = "db"      # подключение к базе
    DIR = "dir"    # каталог reverse-engineer в ФС

class DiffStatus(str, Enum):
    ADDED = "added"           # есть в source, нет в target
    REMOVED = "removed"       # есть в target, нет в source
    CHANGED = "changed"       # есть везде, sql_hash различается
    UNCHANGED = "unchanged"   # есть везде, sql_hash совпадает

class ObjectSnapshot(BaseModel):
    object_key: str
    object_schema: str | None
    object_name: str
    object_type: str
    object_signature: str = ""
    sql_normalized: str       # нормализованный SQL (для аудита/отладки)
    sql_hash: str             # 8 hex SHA-256 от sql_normalized
    estimated_rows: int | None = None   # только для tables; None иначе

class StateSnapshot(BaseModel):
    source_kind: SnapshotSourceKind
    source_ref: str           # путь к каталогу или имя подключения
    db_type: str              # postgres | greenplum (из манифеста/конфига)
    generated_at: str         # UTC iso
    objects: dict[str, ObjectSnapshot]  # key = object_key

class DiffEntry(BaseModel):
    object_key: str
    status: DiffStatus
    source_snapshot: ObjectSnapshot | None = None
    target_snapshot: ObjectSnapshot | None = None
    detail: str = ""          # human-readable, напр. "db_type mismatch"

class DiffReport(BaseModel):
    source: StateSnapshot
    target: StateSnapshot
    generated_at: str
    summary: dict[str, int]   # {"added": N, "removed": N, "changed": N, "unchanged": N}
    entries: list[DiffEntry]
```

### 3.4. Манифест каталога (`CodebaseManifest`)

`dbpm.manifest.json` в корне каталога reverse-engineer — отдельный от autodoc
файл-манифест, хранящий свойства всей БД. Сравнение читает манифест обеих сторон для
проверки совместимости типов.

```python
# domain/diff.py (или infrastructure/config/codebase_manifest.py)
class CodebaseManifest(BaseModel):
    db_type: str              # "postgres" | "greenplum" — тип БД-источника
    database: str             # имя базы
    generated_at: str         # UTC iso timestamp reverse-engineer
    tool_version: str = ""    # версия db-pm (из metadata, для совместимости)
    format_version: int = 1   # версия формата манифеста
```

Файл на диске (`dbpm.manifest.json`):
```json
{
  "db_type": "postgres",
  "database": "bookings_demo",
  "generated_at": "2026-07-28T12:34:56+00:00",
  "tool_version": "0.9.0",
  "format_version": 1
}
```

- **Где пишется:** `ReverseEngineerService.run`, после `generate_scripts`, в корень
  каталога `<output>/<database>/`. (Не в `SQLGenerator` — манифест не зависит от
  шаблонов/SQL-рендеринга, это свойство подключения/БД, а не объекта.)
- **Где читается:** `CompareService.run` для стороны `DIR`; `BuildGraphService` не
  трогает манифест (парсер читает autodoc, не манифест).
- **Обратная совместимость:** старые каталоги (без манифеста) → compare прерывается с
  понятным сообщением «нужен повторный reverse-engineer» (exit code 2, см. §3.7).
  Reverse-engineer старых БД продолжает работать как прежде — манифест просто
  дописывается в корень каталога.
- **Безопасность:** манифест не содержит секретов (нет паролей, адресов, туннелей);
  только тип/имя БД и timestamp. Безопасно коммитить в репо вместе с остальной
  codebase.

### 3.5. CLI API

```
db-pm compare run \
    (--source-dir <path> | --source-connection-file <path>) \
    (--target-dir <path> | --target-connection-file <path>) \
    --output-dir <path> \
    [--keep-model-dir] [--config <path>]
```

- Контракт: **ровно один** из `--source-dir`/`--source-connection-file` (и для target).
  Иначе exit code 2.
- **Несовместимые типы БД** (PG↔GP) или **отсутствие манифеста** для стороны `DIR` →
  exit code 2 с понятным сообщением.
- Фильтр типов объектов (extensions/settings исключены) — внутренний, без флага.
  Сделать настраиваемым → BACKLOG.

### 3.6. Поток `CompareService.run`

```
1. Распарсить source_spec/target_spec (dir|connection-file) -> (kind, ref, conn_cfg|None).
2. Для каждой стороны:
   - DIR:   manifest = read_manifest(ref)
            build_snapshot_from_dir(ref, source_kind=DIR, source_ref=ref,
                                    db_type=manifest.db_type, row_counts=None)
   - DB:    temp_dir = tempfile.mkdtemp(prefix="dbpm_compare_")
            ReverseEngineerService.run(conn_cfg, temp_dir, progress)  # пишет <temp>/<database>/ + dbpm.manifest.json
            row_counts = adapter.get_table_row_counts()
            manifest = read_manifest(temp_dir/<database>)  # проверка что записан
            build_snapshot_from_dir(temp_dir/<database>, source_kind=DB,
                                    source_ref=conn_cfg.name or database,
                                    db_type=conn_cfg.type, row_counts=row_counts)
            if not keep_model_dir: shutil.rmtree(temp_dir)  # в finally
3. Проверка совместимости типов:
   if source_snapshot.db_type != target_snapshot.db_type:
       raise CompareError(f"Несовместимые типы БД: source={source_snapshot.db_type}, "
                          f"target={target_snapshot.db_type}. Сравнение допускается "
                          f"только для одинаковых типов (PG↔PG, GP↔GP).")
4. report = compare(source_snapshot, target_snapshot)
5. Записать source.json, target.json, diff_report.json в output_dir.
6. Вернуть output_dir.
```

Сетевой адаптер — в `try/finally`, cleanup temp-каталога — по образцу `deploy_service.py`
(урок §11).

### 3.7. Обработка несовместимых типов / отсутствующего манифеста

| Ситуация | Поведение | Exit code |
|---|---|---|
| `source.db_type == target.db_type` | Продолжить сравнение. | — |
| `source.db_type != target.db_type` (напр. PG↔GP) | Прервать, понятное сообщение: «Несовместимые типы БД: source=postgres, target=greenplum». | 2 |
| Сторона `DIR`, манифест отсутствует | Прервать: «Каталог <dir> не содержит dbpm.manifest.json. Выполните повторный reverse-engineer (теперь он сохраняет тип БД).» | 2 |
| Манифест есть, но `db_type` неизвестен (опечатка) | Прервать: «Неизвестный тип БД в манифесте: <value>. Ожидается: postgres, greenplum.». | 2 |

«Понятное сообщение» = `typer.secho(..., fg=RED, err=True)` + `raise typer.Exit(code=2)`,
по образцу `_load_connection` в `cli/main.py`.

---

## 4. План реализации (поэтапный, для последующего `_plan`/`_result`)

1. **Зависимости + модели.** Добавить `sqlglot` в `pyproject.toml`, проверить установку
   на Windows. Создать `domain/diff.py` (`ObjectSnapshot`, `StateSnapshot`, `DiffEntry`,
   `DiffReport`, `CodebaseManifest`, enums).
2. **Манифест reverse-engineer.** `infrastructure/config/codebase_manifest.py`
   (`write_manifest`/`read_manifest`/`ManifestError`); подключить запись в
   `ReverseEngineerService.run`. Обновить `test_reverse_engineer_service.py` —
   проверка, что `dbpm.manifest.json` создан в корне и содержит корректный `db_type`.
3. **Нормализация SQL.** `infrastructure/diff/normalize_sql.py`: sqlglot AST + regex
   fallback + тесты на устойчивость (урок §27).
4. **Запрос row counts.** `GET_TABLE_ROW_COUNTS` в `queries.py`; метод в `DatabaseAdapter`
   ABC и в `PGDatabaseAdapter`; заглушка в `FakeAdapter` (урок §18); статическая
   проверка имён столбцов `pg_class`/`pg_namespace` в `test_queries.py` (урок §3).
5. **Снимок.** `infrastructure/diff/snapshot.py`: чтение графа через `BuildGraphService`,
   чтение SQL каждой вершины из `object_source_file` → стрип autodoc → нормализация → хеширование.
6. **Компаратор.** `infrastructure/diff/comparator.py`: set-операции по `object_key`,
   сравнение `sql_hash` для общих. Тесты на синтетических парах.
7. **Сервис.** `application/compare_service.py`: оркестрация с reverse-engineer,
   проверка совместимости типов, cleanup temp-каталога, запись отчёта.
8. **CLI.** `presentation/cli/main.py`: подгруппа `compare` + команда `run`. Контракт
   взаимоисключающих флагов + несовместимые типы (exit code 2).
9. **Тесты оркестрации.** `test_compare_service.py`: мок `ReverseEngineerService` +
   `FakeAdapter`; проверка cleanup, `keep_model_dir`, и **блокировки при
   несовместимых типах** (PG vs GP → `CompareError`).

---

## 5. Тесты

### 5.1. Новые тесты

| Файл | Что покрывает |
|---|---|
| `tests/unit/test_normalize_sql.py` | Нормализация устойчива к форматированию, порядку колонок в `CREATE TABLE`, регистру ключевых слов (урок §27); fallback при синтаксических ошибках (sqlglot не падает, regex-нормализация + warning). |
| `tests/unit/test_comparator.py` | Синтетические пары snapshot: added/removed/changed/unchanged; edge cases (пустые снимки, перегрузки с разными `object_signature`, таблицы с/без `estimated_rows`). |
| `tests/unit/test_codebase_manifest.py` | Чтение/запись манифеста; edge cases — нет манифеста, повреждён манифест, неизвестный `db_type`; корректность полей (`db_type`, `database`, `generated_at`, `tool_version`, `format_version`). |
| `tests/unit/test_compare_service.py` | Оркестрация с моком `ReverseEngineerService` + `FakeAdapter` (новый метод); cleanup временного каталога по умолчанию; сохранение при `keep_model_dir=True`; **блокировка при несовместимых типах (PG↔GP → CompareError)**. |
| `tests/unit/test_queries.py` (обновить) | Проверка нового `GET_TABLE_ROW_COUNTS` на валидные имена столбцов `pg_class`/`pg_type`/`pg_namespace` (урок §3). |

### 5.2. Обновляемые существующие тесты

| Файл | Изменение |
|---|---|
| `tests/unit/test_reverse_engineer_service.py` | Проверка: после `ReverseEngineerService.run`, в корне каталога создан `dbpm.manifest.json`, и `read_manifest(...)` возвращает `db_type == conn_cfg.type`. |
| `tests/unit/test_sql_generator.py` | Фикстуры могут потребовать добавления манифеста — проверить, нужно ли; если генератор не трогается (манифест пишет сервис), то нет. |
| `tests/unit/test_pg_sql_parser.py` | Если парсер игнорирует `dbpm.manifest.json` (он не `.sql`), тесты проходят без изменений. Зафиксировать это явно. |

---

## 6. NOT done / отложено (явный список)

- **Edge diff** (сравнение рёбер графа: появился/исчез FK, вызов функции) → **новый
  пункт BACKLOG (P2)**, связан с этой задачей. Сравнение по `Edge.dedup_key()` после
  фильтрации общих вершин.
- **GUI action** в реестре Phase 7 (`ActionSpec` + диалог + worker + `build_cli` +
  контракт-тест) → отдельная задача после обкатки логики в CLI.
- **Markdown-отчёт** (человекочитаемый свод с секциями added/removed, сгруппированными
  по схеме/типу, со summary наверху) → **BACKLOG P3**. JSON есть, markdown — следующий шаг.
- **Bare refs в телах функций/views как источник false-positive `changed`** (урок §35).
  Нормализация SQL на уровне AST не устранит различия из-за bare references — sqlglot
  парсит текст как есть. **Mitigation:** запускать `qualify-refs` пост-процессор на
  обеих сторонах перед сравнением (можно как pre-step в `CompareService`, опционально).
  Зафиксировать как ограничение MVP.
- **Фильтр типов объектов через флаг** (сейчас захардкожено: tables/views/
  materialized_views/functions/procedures/sequences; extensions/settings исключены) →
  BACKLOG P3.
- **AST qualify-refs через sqlglot** (более точная, чем regex) — связан с уроком §36,
  отдельная задача.
- **Человекочитаемый свод о совместимости типов** (например, показать diff даже при
  разных типах с предупреждением «сравнение между СУБД, результат может быть
  некорректен») — осознанно не сделано в MVP; текущий hard-error безопаснее. Если
  появится запрос — можно сделать `--allow-cross-db-type` флаг.

---

## 7. Контрольный список (по `LESSONS_LEARNED.md`)

- [ ] Новая typer-подгруппа — сразу мультикомандная, по образцу `deploy` (§9).
- [ ] Новый метод `DatabaseAdapter` (`get_table_row_counts`) — сначала прогнать
  существующие тесты с обновлённым `FakeAdapter` (§18).
- [ ] Нормализация: глобальные преобразования ДО токенизации (sqlglot делает сам, но
  осознать, что `numeric(10,2)`-подобные кейсы обрабатываются корректно) (§27).
- [ ] Сетевой адаптер в `try/finally`, cleanup temp-каталога в `finally` (по образцу
  `deploy_service.py`) (§11).
- [ ] SQL-запрос `GET_TABLE_ROW_COUNTS` — статическая проверка имён столбцов `pg_class`/
  `pg_namespace` (по образцу `test_queries.py`) (§3).
- [ ] `git add -- "_docs_/..."` для каталогов с `-` в имени (§12).
- [ ] Bare refs в телах функций/views — документировать как ограничение MVP; предложить
  `qualify-refs` как mitigation (§35).
- [ ] Новая зависимость (sqlglot) — проверить наличие wheel для win32 **до** интеграции
  (§10).
- [ ] Манифест reverse-engineer — проверить, что старые существующие тесты
  `test_reverse_engineer_service.py` / `test_sql_generator.py` не ломаются от
  появления `dbpm.manifest.json` в корне каталога.

---

## 8. Кросс-ссылки

- `_docs_/_checkpoints_/20260725_002_checkpoint.md` — текущее состояние проекта
  (Phase 7 closed; Phase 8 = overload resolution).
- `_docs_/_tasks_/BACKLOG.md` — куда добавить **edge diff** (P2, новая запись).
- `LESSONS_LEARNED.md` §3, §9, §11, §18, §27, §35, §42 — релевантные уроки.
- `_docs_/_tasks_/phase_07/Phase_7_vision_final.md` — паттерн action registry
  (для будущей GUI-интеграции).
- `src/db_project_manager/application/deploy_service.py` — образец оркестрации с
  cleanup в `finally` и `--keep-db` эскейп-хэтчем.

---

## 9. Статус USER_INPUT

Все ключевые развилки закрыты через Q&A с пользователем (см. §2). Открытых вопросов
для дизайна нет — документ готов к переходу `_draft` → `_plan`/`_result` (по конвенции
`_tasks_/TASK_CONVENTIONS.md` §2.2, для сложной архитектурной задачи можно
`_draft` → `_final`).
