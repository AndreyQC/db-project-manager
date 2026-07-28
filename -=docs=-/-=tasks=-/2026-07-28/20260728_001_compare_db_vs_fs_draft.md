# Сравнение состояния БД с файловой системой (Phase 9) — draft

> **Дата:** 2026-07-28
> **Ветка:** dev
> **Статус документа:** draft (дизайн готов к плану→результату; все USER_INPUT закрыты)

> Контекст:
> - `-=docs=-/REFRESH_CONTEXT.md` — точка входа в проект
> - `-=docs=-/-=CHECKPOINTS=-/20260725_002_checkpoint.md` — последний checkpoint (Phase 7 closed)
> - `-=docs=-/-=tasks=-/BACKLOG.md` — открытые задачи; Phase 8 = overload resolution (P1)
> - `src/db_project_manager/domain/graph.py` — `Vertex`, `Edge`, `DependencyGraph`
> - `src/db_project_manager/application/reverse_engineer.py` — `ReverseEngineerService.run()`
> - `src/db_project_manager/application/graph_service.py` — `BuildGraphService.build()`
> - `src/db_project_manager/presentation/cli/main.py` — typer-приложение, подгруппы `graph`/`deploy`
> - `src/db_project_manager/infrastructure/database/postgres/queries.py` — каталог SQL-запросов
> - `src/db_project_manager/infrastructure/parsing/pg_sql_parser.py` — парсер codebase→graph
> - `src/db_project_manager/infrastructure/sql/autodoc.py` — autodoc-заголовок, `extract_header`
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
5. Выполнить сравнение и показать diff в зависимости от порядка, заданного
   параметром `diff_type`.

Каждая сторона сравнения (source / target) — это **либо** подключение к БД, **либо**
уже сформированный каталог reverse-engineer. Сравнение формирует отчёт на диске в
формате, пригодном для последующего использования (машинно-читаемый).

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
| 6 | Row counts (`reltuples`) | **Да, информационная метка** | Лежит в `source.json`/`target.json` рядом с таблицами, не влияет на структурный diff. Полезно оценить масштаб расхождений. Запрос из §1 идёт в `queries.py`. |
| 7 | `temp_model_dir` | **`--keep-model-dir`** | Reverse-engineer БД-источника выполняется во временный каталог (`tempfile.mkdtemp()`); по умолчанию чистится в `finally`, флаг сохраняет для аудита. Аналог `--keep-db` в `deploy validate`. |
| 8 | Что лежит в снимке | **Снимок объектов без рёбер** | Список `ObjectSnapshot` (object_key + метаданные + нормализованный SQL + hash + estimated_rows). Без `Edge` — их сравнение в BACKLOG. |
| 9 | `diff_type` | **Тип источника** | Управляется флагами `--source-dir`/`--source-connection-file` (а не отдельным параметром). `added` = есть в source, нет в target; `removed` = наоборот. |

---

## 3. Архитектурный набросок (для последующего `_plan`/`_result`)

### 3.1. Новые файлы

| Путь | Назначение |
|---|---|
| `src/db_project_manager/domain/diff.py` | Pydantic-модели: `ObjectSnapshot`, `StateSnapshot`, `DiffEntry`, `DiffReport`, enum `SnapshotSourceKind`, `DiffStatus`. |
| `src/db_project_manager/infrastructure/diff/__init__.py` | Пакет diff-инфраструктуры. |
| `src/db_project_manager/infrastructure/diff/normalize_sql.py` | `normalize_sql(sql, *, dialect="postgres") -> str` (sqlglot + regex-fallback), `sql_hash(sql) -> str` (8 hex от нормализованного). |
| `src/db_project_manager/infrastructure/diff/snapshot.py` | `build_snapshot_from_dir(codebase_dir, *, source_kind, source_ref, row_counts=None) -> StateSnapshot` — читает граф + SQL каждой вершины, нормализует, хеширует. |
| `src/db_project_manager/infrastructure/diff/comparator.py` | `compare(source: StateSnapshot, target: StateSnapshot) -> DiffReport`. |
| `src/db_project_manager/application/compare_service.py` | `CompareService.run(source_spec, target_spec, output_dir, *, keep_model_dir, progress) -> Path` — оркестрация: reverse-engineer при необходимости, снимки обеих сторон, сравнение, запись отчёта. |

### 3.2. Изменения в существующих файлах

| Файл | Изменение |
|---|---|
| `src/db_project_manager/infrastructure/database/postgres/queries.py` | + `GET_TABLE_ROW_COUNTS` (SQL из §1). |
| `src/db_project_manager/infrastructure/database/base.py` | + абстрактный метод `get_table_row_counts() -> list[...]` в `DatabaseAdapter`. |
| `src/db_project_manager/infrastructure/database/postgres/adapter.py` | + реализация `get_table_row_counts()` через `self._exec(GET_TABLE_ROW_COUNTS, {})`. |
| `tests/conftest.py` | + `get_table_row_counts()` в `FakeAdapter` (no-op/заглушка) — по уроку §18. |
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
    generated_at: str         # UTC iso
    objects: dict[str, ObjectSnapshot]  # key = object_key

class DiffEntry(BaseModel):
    object_key: str
    status: DiffStatus
    source_snapshot: ObjectSnapshot | None = None
    target_snapshot: ObjectSnapshot | None = None

class DiffReport(BaseModel):
    source: StateSnapshot
    target: StateSnapshot
    generated_at: str
    summary: dict[str, int]   # {"added": N, "removed": N, "changed": N, "unchanged": N}
    entries: list[DiffEntry]
```

### 3.4. CLI API

```
db-pm compare run \
    (--source-dir <path> | --source-connection-file <path>) \
    (--target-dir <path> | --target-connection-file <path>) \
    --output-dir <path> \
    [--keep-model-dir] [--config <path>]
```

- Контракт: **ровно один** из `--source-dir`/`--source-connection-file` (и для target).
  Иначе exit code 2 с понятным сообщением.
- Фильтр типов объектов (extensions/settings исключены) — внутренний, без флага.
  Сделать настраиваемым → BACKLOG.

### 3.5. Отчёт (в `--output-dir`)

| Файл | Содержание |
|---|---|
| `source.json` | Полный снимок source-состояния (`StateSnapshot.model_dump_json(indent=2)`). |
| `target.json` | Полный снимок target-состояния. |
| `diff_report.json` | `{summary: {...}, entries: [...]}` — результат сравнения (`DiffReport`). |

Формат — pretty-printed JSON (`indent=2`, `ensure_ascii=False`) — по образцу `graph_store.py`.

### 3.6. Поток `CompareService.run`

```
1. Распарсить source_spec/target_spec (dir|connection-file) -> (kind, ref, conn_cfg|None).
2. Для каждой стороны:
   - DIR:   build_snapshot_from_dir(ref, source_kind=DIR, source_ref=ref)
   - DB:    temp_dir = tempfile.mkdtemp(prefix="dbpm_compare_")
            ReverseEngineerService.run(conn_cfg, temp_dir, progress)  # пишет <temp>/<database>/
            row_counts = adapter.get_table_row_counts()
            build_snapshot_from_dir(temp_dir/<database>, source_kind=DB,
                                    source_ref=conn_cfg.name or database,
                                    row_counts=row_counts)
            if not keep_model_dir: shutil.rmtree(temp_dir)  # в finally
3. report = compare(source_snapshot, target_snapshot)
4. Записать source.json, target.json, diff_report.json в output_dir.
5. Вернуть output_dir.
```

Сетевой адаптер — в `try/finally`, cleanup temp-каталога — по образцу `deploy_service.py`
(урок §11).

---

## 4. План реализации (поэтапный, для последующего `_plan`/`_result`)

1. **Зависимости + модели.** Добавить `sqlglot` в `pyproject.toml`, проверить установку
   на Windows. Создать `domain/diff.py`.
2. **Нормализация SQL.** `infrastructure/diff/normalize_sql.py`: sqlglot AST + regex
   fallback + тесты на устойчивость (урок §27).
3. **Запрос row counts.** `GET_TABLE_ROW_COUNTS` в `queries.py`; метод в `DatabaseAdapter`
   ABC и в `PGDatabaseAdapter`; заглушка в `FakeAdapter` (урок §18); статическая
   проверка имён столбцов `pg_class`/`pg_namespace` в `test_queries.py` (урок §3).
4. **Снимок.** `infrastructure/diff/snapshot.py`: чтение графа через `BuildGraphService`,
   чтение SQL каждой вершины из `object_source_file`, стрип autodoc-блока, нормализация,
   хеширование.
5. **Компаратор.** `infrastructure/diff/comparator.py`: set-операции по `object_key`,
   сравнение `sql_hash` для общих. Тесты на синтетических парах.
6. **Сервис.** `application/compare_service.py`: оркестрация с reverse-engineer,
   cleanup temp-каталога, запись отчёта.
7. **CLI.** `presentation/cli/main.py`: подгруппа `compare` + команда `run`. Контракт
   взаимоисключающих флагов (exit code 2 при нарушении).
8. **Тесты оркестрации.** `test_compare_service.py`: мок `ReverseEngineerService` +
   `FakeAdapter`; проверка cleanup и `keep_model_dir`.

---

## 5. Тесты

| Файл | Что покрывает |
|---|---|
| `tests/unit/test_normalize_sql.py` | Нормализация устойчива к форматированию, порядку колонок в `CREATE TABLE`, регистру ключевых слов (урок §27); fallback при синтаксических ошибках (sqlglot не падает, regex-нормализация + warning). |
| `tests/unit/test_comparator.py` | Синтетические пары snapshot: added/removed/changed/unchanged; edge cases (пустые снимки, перегрузки с разными `object_signature`, таблицы с/без `estimated_rows`). |
| `tests/unit/test_compare_service.py` | Оркестрация с моком `ReverseEngineerService` + `FakeAdapter` (новый метод); cleanup временного каталога по умолчанию; сохранение при `keep_model_dir=True`. |
| `tests/unit/test_queries.py` (обновить) | Проверка нового `GET_TABLE_ROW_COUNTS` на валидные имена столбцов `pg_class`/`pg_namespace` (урок §3). |

---

## 6. NOT done / отложено (явный список)

- **Edge diff** (сравнение рёбер графа: появился/исчез FK, вызов функции) → **новый
  пункт BACKLOG (P2)**, связан с этой задачей. Сравнение по `Edge.dedup_key()` после
  фильтрации общих вершин.
- **GUI action** в реестре Phase 7 (`ActionSpec` + диалог + worker + `build_cli` +
  контракт-тест) → отдельная задача после обкатки логики в CLI.
- **Markdown-отчёт** (человекочитаемый свод с секциями added/removed, сгруппированными
  по схеме/типу, summary вверху) → **BACKLOG P3**. JSON есть, markdown — следующий шаг.
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
- [ ] `git add -- "-=docs=-/..."` для каталогов с `-` в имени (§12).
- [ ] Bare refs в телах функций/views — документировать как ограничение MVP; предложить
  `qualify-refs` как mitigation (§35).
- [ ] Новая зависимость (sqlglot) — проверить наличие wheel для win32 **до** интеграции
  (§10).

---

## 8. Кросс-ссылки

- `-=docs=-/-=CHECKPOINTS=-/20260725_002_checkpoint.md` — текущее состояние проекта
  (Phase 7 closed; Phase 8 = overload resolution).
- `-=docs=-/-=tasks=-/BACKLOG.md` — куда добавить **edge diff** (P2, новая запись).
- `LESSONS_LEARNED.md` §3, §9, §11, §18, §27, §35, §42 — релевантные уроки.
- `-=docs=-/-=tasks=-/phase_07/Phase_7_vision_final.md` — паттерн action registry
  (для будущей GUI-интеграции).
- `src/db_project_manager/application/deploy_service.py` — образец оркестрации с
  cleanup в `finally` и `--keep-db` эскейп-хэтчем.

---

## 9. Статус USER_INPUT

Все ключевые развилки закрыты через Q&A с пользователем (см. §2). Открытых вопросов
для дизайна нет — документ готов к переходу `_draft` → `_plan`/`_result` (по конвенции
`-=tasks=-/TASK_CONVENTIONS.md` §2.2, для сложной архитектурной задачи можно
`_draft` → `_final`).
