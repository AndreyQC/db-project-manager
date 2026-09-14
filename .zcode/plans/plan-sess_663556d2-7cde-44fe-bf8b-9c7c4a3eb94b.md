# Сравнение состояния БД с файловой системой — драфт задачи (Phase 9)

> Это план **создания драфта-документа** (task-файла по `_tasks_` конвенции).
> Никаких изменений в коде — только новый файл `_docs_/_tasks_/2026-07-28/20260728_001_compare_db_vs_fs_draft.md`,
> плюс при желании запись в `BACKLOG.md`.

## Что будет в драфте

### Метаблок
- Дата: 2026-07-28
- Ветка: dev
- Контекст: REFRESH_CONTEXT.md, последн**ий** чекпоинт 20260725_002 (Phase 7 closed),
  BACKLOG P1 (Phase 8 = overload resolution), `domain/graph.py`, `application/reverse_engineer.py`,
  `application/graph_service.py`, `presentation/cli/main.py`, `infrastructure/database/postgres/queries.py`,
  `infrastructure/parsing/pg_sql_parser.py`, LESSONS §27 (нормализация SQL),
  §35 (fully-qualified DDL), §42 (PySide6 worker GC).

### Постановка задачи (как сформулировал пользователь)
Инструмент должен уметь сравнивать состояние текущей БД с тем, что лежит в виде
файлов в ФС, и формировать отчёт о расхождениях. Процесс:
1. Получить из БД метаданные таблиц (включая `reltuples` для оценки наличия данных).
2. Сформировать в каталоге `temp_model_dir` reverse-engineer этой БД.
3. Построить граф для неё.
4. Построить граф для БД, представленной каталогом в ФС.
5. Выполнить сравнение и показать diff в зависимости от того, что выбрано как
   `source` и что как `target` (каждый из них — либо подключение, либо каталог).

### Принятые решения (из Q&A с пользователем)

| Параметр | Решение |
|---|---|
| Гранулярность MVP | **Наличие объектов + структура (нормализованный SQL)**. Графовые рёбра (edge diff) → **в BACKLOG** как отдельная задача. |
| Типы объектов | tables + views + materialized_views + functions + procedures + sequences (структурный diff). Extensions и database_settings исключить (шумят между средами). |
| Формат отчёта | **Сырой снимок двух состояний** (`source.json`, `target.json`) + файл диффа (`diff_report.json`). |
| Источник указания стороны | **Явный флаг типа**: `--source-dir` XOR `--source-connection-file`, и аналогично для target. |
| Нормализация SQL | **sqlglot AST** (`sqlglot.parse_one(...).normalize().sql()`), новая зависимость. |
| Row counts | **Да, как информационная метка** в `source.json`/`target.json` для таблиц. |
| temp_model_dir | **`--keep-model-dir`**: reverse-engineer БД-источника во временый каталог (`tempfile.mkdtemp`), по умолчанию чистится, с флагом — сохраняется для аудита. |
| Снимок | **Снимок объектов без рёбер** (вершины + нормализованный SQL + hash + estimated_rows). |
| diff_type | **Тип источника** — управляется флагами `--source-dir`/`--source-connection-file` (а не отдельным параметром). |

### Архитектурный набросок (для последующего plan/result)

**Новые файлы**:
- `src/db_project_manager/domain/diff.py` — pydantic-модели:
  - `ObjectSnapshot` — `{object_key, object_schema, object_name, object_type, object_signature, sql_normalized, sql_hash, estimated_rows (Optional[int])}`
  - `StateSnapshot` — `{source_kind: "db"|"dir", source_ref: str, generated_at, objects: dict[object_key, ObjectSnapshot]}`
  - `DiffEntry` — `{object_key, status: "added"|"removed"|"changed"|"unchanged", source_snapshot?, target_snapshot?}`
  - `DiffReport` — `{source, target, generated_at, summary: {added, removed, changed, unchanged}, entries: list[DiffEntry]}`
- `src/db_project_manager/infrastructure/diff/snapshot.py` — построение `StateSnapshot` из каталога reverse-engineer:
  - `build_snapshot_from_dir(codebase_dir, *, source_kind, source_ref, row_counts: dict | None) -> StateSnapshot`
  - читает `DependencyGraph` (`BuildGraphService.build`), для каждой вершины читает SQL из `object_source_file`, стрипит autodoc-блок (`extract_header` + substring), парсит через sqlglot → нормализует → SHA-256.
- `src/db_project_manager/infrastructure/diff/normalize_sql.py` — обёртка над sqlglot:
  - `normalize_sql(sql: str, *, dialect="postgres") -> str` (с try/except — при ошибке парсинга fallback на regex-нормализацию + warning)
  - `sql_hash(sql: str) -> str` (8 hex от нормализованного)
- `src/db_project_manager/infrastructure/diff/comparator.py` — `compare(source: StateSnapshot, target: StateSnapshot) -> DiffReport`.
- `src/db_project_manager/application/compare_service.py` — `CompareService.run(source_spec, target_spec, output_dir, *, keep_model_dir, progress)`:
  - для стороны типа `db`: `tempfile.mkdtemp()` → `ReverseEngineerService.run` → считать `row_counts` через новый адаптер-метод → `build_snapshot_from_dir`.
  - для стороны типа `dir`: `build_snapshot_from_dir` напрямую (каталог уже reverse-engineer).
  - вызывает comparator, пишет `source.json`/`target.json`/`diff_report.json` в `output_dir`.
  - cleanup в `finally` (если не `keep_model_dir`).
- `src/db_project_manager/presentation/cli/main.py` — новый `compare_app = typer.Typer(...)` + `app.add_typer(compare_app, name="compare")`, команда `@compare_app.command("run")`.

**Новые элементы в существующих файлах**:
- `infrastructure/database/postgres/queries.py`: `GET_TABLE_ROW_COUNTS` (SQL из исходного запроса пользователя).
- `infrastructure/database/postgres/adapter.py`: метод `get_table_row_counts() -> list[RowCount]` (или просто `dict[(schema, name) -> int]`), и соответствующий метод в `DatabaseAdapter` ABC (`infrastructure/database/base.py`) + `FakeAdapter` в `tests/conftest.py` (по уроку §18).
- `pyproject.toml`: зависимость `sqlglot`.

**CLI API** (план):
```
db-pm compare run \
    (--source-dir <path> | --source-connection-file <path>) \
    (--target-dir <path> | --target-connection-file <path>) \
    --output-dir <path> \
    [--keep-model-dir] [--config <path>]
```
Контракт: ровно один из `--source-dir`/`--source-connection-file` (и для target); иначе exit code 2. Фильтр типов объектов — внутренний, без флага (extensions/settings исключены; в BACKLOG — сделать настраиваемым).

**Отчёт** (в `--output-dir`):
- `source.json`, `target.json` — полный снимок каждого состояния (машинно-читаемый, для дальнейшей обработки/импорта).
- `diff_report.json` — `{summary: {...}, entries: [...]}`.

### Тесты
- `tests/unit/test_normalize_sql.py` — нормализация устойчива к форматированию, порядку колонок в `CREATE TABLE`, регистру ключевых слов (по уроку §27); fallback при синтаксических ошибках.
- `tests/unit/test_comparator.py` — синтетические пары snapshot: added/removed/changed/unchanged, edge cases (пустые снимки, перегрузки с разными signature hash).
- `tests/unit/test_compare_service.py` — оркестрация с моком `ReverseEngineerService` + `FakeAdapter` (новый метод); cleanup временного каталога; `keep_model_dir=True` сохраняет.
- Обновить `tests/unit/test_queries.py` — проверка нового `GET_TABLE_ROW_COUNTS` на валидные имена столбцов `pg_class`/`pg_namespace` (по уроку §3).

### NOT done / отложено (явный список в драфте)
- **Edge diff** (сравнение рёбер графа) → отдельный пункт BACKLOG (P2).
- **GUI action** в реестре Phase 7 → отдельная задача после обкатки логики в CLI.
- **Markdown-отчёт** (человекочитаемый свод) → BACKLOG P3.
- **AST qualify-refs для снятия различий из-за bare refs** — Bare refs в телах функций/представлениях могут давать false-positive `changed` (по уроку §35). На уровне SQL-тела нормализация их не устранит. Зафиксировать как ограничение MVP; mitigation — `qualify-refs` пост-процессор на обеих сторонах перед сравнением (его уже можно запустить как pre-step).
- **Фильтр типов объектов через флаг** (сейчас захардкожено: tables/views/materialized_views/functions/procedures/sequences).

### Контрольный список (по урокам)
- [ ] Новая typer-подгруппа — сразу мультикомандная (по образцу `deploy`) (§9).
- [ ] Новый метод `DatabaseAdapter` — сначала прогнать существующие тесты с `FakeAdapter` (§18).
- [ ] Нормализация: глобальные преобразования ДО токенизации (sqlglot делает сам, но осознать) (§27).
- [ ] Сетевой адаптер в `try/finally`, cleanup temp-каталога (по образцу deploy_service) (§11).
- [ ] SQL-запрос — статическая проверка имён столбцов (§3).
- [ ] `git add -- "_tasks_/..."` для каталогов с `-` (§12).
- [ ] Bare refs в телах функций/views — документировать как ограничение (§35).
- [ ] Новая зависимость (sqlglot) — проверить наличие wheel для Windows (§10).

### Запланированные артефакты (в драфте)
- План реализации (поэтапный).
- Список `USER_INPUT` — закрытых: все ключевые развилки решены через Q&A.
- Open questions: нет (после Q&A все закрыты); в драфте явно отметить, что дизайн готов к плану→результат.

### Связанные документы (кросс-ссылки)
- `_checkpoints_/20260725_002_checkpoint.md` — текущее состояние проекта.
- `_tasks_/BACKLOG.md` — фаза 8 (overload resolution) и куда добавить edge diff.
- `LESSONS_LEARNED.md` §3, §9, §11, §18, §27, §35 — релевантные уроки.
- `_tasks_/phase_07/Phase_7_vision_final.md` — паттерн action registry (если позже добавлять GUI).

## Файл драфта

**Путь:** `_docs_/_tasks_/2026-07-28/20260728_001_compare_db_vs_fs_draft.md`

**Коммит (после утверждения плана):**
```
docs(tasks): add 20260728_001 — compare DB vs filesystem draft (Phase 9)
```
Один логический документ — один коммит. Код и документы не смешиваются (по TASK_CONVENTIONS §6).

## Замечание про BACKLOG

Драфт упомянет, что **edge diff** идёт в BACKLOG как новая запись (P2, связана с этой
задачей). Само редактирование BACKLOG.md — отдельный коммит после утверждения драфта
(`docs(backlog): add edge diff (P2, from Phase 9 draft)`), либо оставляем на момент
реализации фазы. В рамках **этого** плана — только драфт; BACKLOG правится по
согласованию с пользователем после просмотра драфта.