# Результат Phase 9 — Сравнение состояния БД с файловой системой

> Дата: 2026-07-29
>
> Контекст:
> - `-=docs=-/-=tasks=-/2026-07-28/20260728_001_compare_db_vs_fs_draft.md` — нормативный дизайн (§1-9)
> - `-=docs=-/-=tasks=-/phase_09/001_plan_phase_09.md` — план (P9.S01–S09)

Статус: реализация завершена (S01–S08), фаза закрывается этим документом (S09).

---

## 1. Что сделано

| Шаг | Что | Коммит |
|-----|-----|--------|
| P9.S01 | `sqlglot~=27.0.0` зависимость; `domain/diff.py` — модели `CodebaseManifest`, `ObjectSnapshot`, `StateSnapshot`, `DiffEntry`, `DiffReport`, enum'ы `SnapshotSourceKind`, `DiffStatus` | `8a29e63` |
| P9.S02 | `infrastructure/config/codebase_manifest.py` — `write_manifest`/`read_manifest` (атомарно, валидация db_type); `ReverseEngineerService.run` пишет `dbpm.manifest.json` | `983b5e3` |
| P9.S03 | `infrastructure/diff/normalize_sql.py` — sqlglot AST-нормализация + regex-fallback; `sql_hash` (8 hex) | `600b747` |
| P9.S04 | `GET_TABLE_ROW_COUNTS` в `queries.py`; 8-й abstractmethod `get_table_row_counts` в `DatabaseAdapter`; реализация в `PGDatabaseAdapter`; заглушки в `FakeAdapter`/`DeployFakeAdapter` (урок §18) | `5df02be` |
| P9.S05 | `infrastructure/diff/snapshot.py` — `build_snapshot_from_dir` (граф + SQL-нормализация + хеширование, фильтр `DIFFED_TYPES`) | `9d55825` |
| P9.S06 | `infrastructure/diff/comparator.py` — `compare` (set-операции по `object_key` + `sql_hash`) | `e5b5669` |
| P9.S07 | `application/compare_service.py` — `CompareService.run` (оркестрация, проверка db_type, cleanup temp) | `b5b0680` |
| P9.S08 | `presentation/cli/main.py` — подгруппа `compare` + команда `run`; `_resolve_side` | `b779928` |

Видение из драфта выполнено полностью: сравнение наличия объектов + структура
(draft §2, решение 1), типы `tables/views/materialized_views/functions/procedures/
sequences` (решение 2), JSON-снимки + diff-отчёт (решение 3), явные флаги
`--source-dir`/`--source-connection-file` (решение 4), sqlglot AST-нормализация
(решение 5), row counts как метка (решение 6), `--keep-model-dir` (решение 7),
проверка совместимости db_type (решения 10–12).

---

## 2. Отклонения от плана

1. **`sqlglot.normalize()` API.** План предполагал `tree.normalize().sql(...)` (метод
   выражения). В sqlglot 27 `normalize` — параметр `tree.sql(..., normalize=True)`.
   Корректировка только в вызове; поведение (детерминированная нормализация) то же.
2. **`output_dir` первым параметром** в `compare_run`. Typer/Claude требует, чтобы
   параметр без дефолта шёл до опциональных; в плане `--output-dir` стоял после
   опциональных флагов source/target. Перенесён в начало сигнатуры (на CLI-вывод
   не влияет — это не позиционный, а `--output-dir`).
3. **`manifest` в `_build_db_side`** (plan S07) предполагалось использовать для
   `db_type`. В реализации db_type для DB-стороны берётся из `conn_cfg.type`
   (канонический источник для живого подключения), а `read_manifest` вызывается
   только для подтверждения, что манифест записан (fail-fast при ошибке записи).

---

## 3. Проверки

```bash
unset SSL_CERT_FILE REQUESTS_CA_BUNDLE CURL_CA_BUNDLE && uv run pytest tests/unit/ -q
# 369 passed (baseline 307 + 62 новых) — OK
uv run ruff check src/ tests/
# All checks passed!
```

| Тест-файл | Кол-во | Что покрывает |
|-----------|--------|---------------|
| `test_diff_models.py` | 12 | roundtrip JSON, enum-сериализация, дефолты манифеста |
| `test_codebase_manifest.py` | 8 | write/read, атомарность, corrupt/missing/unknown db_type |
| `test_normalize_sql.py` | 12 | регистр/форматирование/комментарии, numeric(10,2) (§27), fallback |
| `test_snapshot.py` | 8 | фильтр типов, хеши, row counts, перегрузки, детерминизм |
| `test_comparator.py` | 8 | added/removed/changed/unchanged, edge cases, перегрузки |
| `test_compare_service.py` | 7 | DIR-DIR, missing manifest, db_type mismatch, temp cleanup, keep_model_dir |
| `test_compare_cli.py` | 6 | взаимоисключающие флаги, несуществующий каталог, отчёт, CompareError |
| `test_queries.py` | +1 | `GET_TABLE_ROW_COUNTS` валидация столбцов (§3) |
| `test_reverse_engineer.py` | +1 | манифест записан после `run` |

Smoke CLI: `db-pm compare --help` и `db-pm compare run --help` — вывод корректен,
`run` отображается как подкоманда, все опции видны.

---

## 4. Известные ограничения

- **Bare refs в телах функций/views** (урок §35). Нормализация SQL через sqlglot
  парсит текст как есть — если функция вызывает `sp_x()` без схемы, а в другом
  состоянии тот же вызов квалифицирован (`app.sp_x()`), sqlglot сочтёт их разными
  → false-positive `changed`. **Mitigation:** запускать `db-pm qualify-refs` на
  обеих сторонах перед сравнением.
- **Edge diff** (сравнение рёбер графа) не реализован — в BACKLOG (P2).
- **GUI action** для compare не реализован — CLI-only в этой фазе; GUI добавляется
  отдельной задачей через action registry (Phase 7).
- **Markdown-отчёт** не реализован — JSON только (machine-readable). Markdown-свод
  → BACKLOG P3.
- **Фильтр типов объектов** захардкожен (`DIFFED_TYPES`); сделать настраиваемым → BACKLOG P3.
- **`numeric(10,2)` → `DECIMAL(10, 2)`**: sqlglot нормализует имя типа. Это
  консистентно (оба состояния проходят одинаковую нормализацию), но если
  пользователю нужно видеть исходные имена типов в отчёте — это ограничение.

---

## 5. Новые файлы

| Файл | Назначение |
|------|------------|
| `src/db_project_manager/domain/diff.py` | Pydantic-модели diff |
| `src/db_project_manager/infrastructure/config/codebase_manifest.py` | Манифест каталога |
| `src/db_project_manager/infrastructure/diff/__init__.py` | Пакет diff |
| `src/db_project_manager/infrastructure/diff/normalize_sql.py` | Нормализация SQL |
| `src/db_project_manager/infrastructure/diff/snapshot.py` | Снимок состояния |
| `src/db_project_manager/infrastructure/diff/comparator.py` | Компаратор |
| `src/db_project_manager/application/compare_service.py` | Оркестрация |
| `tests/unit/test_diff_models.py` | Тесты моделей |
| `tests/unit/test_codebase_manifest.py` | Тесты манифеста |
| `tests/unit/test_normalize_sql.py` | Тесты нормализации |
| `tests/unit/test_snapshot.py` | Тесты снимка |
| `tests/unit/test_comparator.py` | Тесты компаратора |
| `tests/unit/test_compare_service.py` | Тесты сервиса |
| `tests/unit/test_compare_cli.py` | Тесты CLI |

## 6. Следующие шаги

- Phase 8 — overload resolution in edge detection (BACKLOG P1, не начата).
- BACKLOG P2: edge diff (сравнение рёбер графа) — естественное продолжение compare.
- BACKLOG P3: GUI action для compare; Markdown-отчёт; настраиваемый фильтр типов.
