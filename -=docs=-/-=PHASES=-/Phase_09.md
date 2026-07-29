# Phase 9: Сравнение состояния БД с файловой системой

> Дата: 2026-07-29
> Статус: завершена
> План: `-=docs=-/-=tasks=-/phase_09/001_plan_phase_09.md`
> Результат: `-=docs=-/-=tasks=-/phase_09/002_result_phase_09.md`
> Драфт (норматив): `-=docs=-/-=tasks=-/2026-07-28/20260728_001_compare_db_vs_fs_draft.md`

---

## 1. Цель фазы

- Сравнивать состояние БД (через подключение) с состоянием, зафиксированным в
  виде reverse-engineer дерева файлов, и формировать отчёт о расхождениях.
- Каждая сторона сравнения — либо подключение к БД, либо каталог reverse-engineer.
- Сравнивать только совместимые типы БД (PG↔PG, GP↔GP): тип БД сохраняется в
  манифесте каталога при reverse-engineer.

---

## 2. Что сделано

| Категория | Файл → Что изменилось |
|-----------|-----------------------|
| Domain | `domain/diff.py` → модели `CodebaseManifest`, `ObjectSnapshot`, `StateSnapshot`, `DiffEntry`, `DiffReport`, enum'ы |
| Reverse-engineer | `application/reverse_engineer.py` → пишет `dbpm.manifest.json` в корень каталога |
| Config | `infrastructure/config/codebase_manifest.py` → `write_manifest`/`read_manifest` (атомарно, валидация db_type) |
| Diff infra | `infrastructure/diff/normalize_sql.py` → sqlglot AST-нормализация + regex-fallback, `sql_hash` |
| Diff infra | `infrastructure/diff/snapshot.py` → `build_snapshot_from_dir` (граф + SQL + хеши) |
| Diff infra | `infrastructure/diff/comparator.py` → `compare` (set-операции по `object_key` + `sql_hash`) |
| Application | `application/compare_service.py` → `CompareService.run` (оркестрация, проверка db_type, cleanup temp) |
| Database | `infrastructure/database/postgres/queries.py` → `GET_TABLE_ROW_COUNTS` |
| Database | `infrastructure/database/base.py` → 8-й abstractmethod `get_table_row_counts` |
| Database | `infrastructure/database/postgres/adapter.py` → реализация `get_table_row_counts` |
| CLI | `presentation/cli/main.py` → подгруппа `compare` + команда `run` |
| Deps | `pyproject.toml` → `sqlglot~=27.0.0` |
| Tests | 8 новых тест-файлов, +62 теста |

Ключевые коммиты: `8a29e63` (S01), `983b5e3` (S02), `600b747` (S03), `5df02be`
(S04), `9d55825` (S05), `e5b5669` (S06), `b5b0680` (S07), `b779928` (S08).

---

## 3. Ключевые архитектурные решения

- **Манифест каталога** (`dbpm.manifest.json`): тип БД хранится в корне reverse-
  engineer дерева, отдельно от autodoc (манифест = вся БД, autodoc = один объект).
  Без этого сравнение «каталог vs каталог» по типам было невозможно — тип
  терялся при reverse-engineer.
- **Hard error при несовместимых типах** (PG↔GP): сравнение прерывается с exit
  code 2. PG и GP разделяют PostgreSQL-основу, но различаются типами и
  распределениями — структурный diff был бы шумным и вводящим в заблуждение.
- **Старые каталоги требуют манифест**: каталог без `dbpm.manifest.json` нельзя
  сравнить — предлагается повторный reverse-engineer.
- **sqlglot AST-нормализация**: делает hash нечувствительным к форматированию,
  регистру ключевых слов, комментариям. Regex-fallback на непарсимом SQL.
- **Стабильная идентичность** через `object_key` (с суффиксом `/signature/<hash>`
  для перегрузок): set-операции дают added/removed напрямую (урок §26).
- **`--keep-model-dir`**: аналог `--keep-db` — reverse-engineer БД-стороны во
  временный каталог, cleanup в `finally`, флаг сохраняет для аудита.

---

## 4. Проверки

```bash
unset SSL_CERT_FILE REQUESTS_CA_BUNDLE CURL_CA_BUNDLE && uv run pytest tests/unit/ -q
# 369 passed (307 baseline + 62 новых) — OK
uv run ruff check src/ tests/
# All checks passed!
```

Smoke CLI: `db-pm compare --help` — подгруппа с командой `run`; `db-pm compare run
--help` — все опции видны, `--output-dir` обязательна.

---

## 5. Известные ограничения / NOT done

- **Bare refs в телах функций/views** (урок §35) могут давать false-positive
  `changed` — sqlglot парсит текст как есть. Mitigation: `qualify-refs` на обе
  стороны перед сравнением.
- **Edge diff** (сравнение рёбер графа) — не реализован, в BACKLOG P2.
- **GUI action** для compare — не реализован (CLI-only), в BACKLOG P3.
- **Markdown-отчёт** — не реализован (JSON only), в BACKLOG P3.
- **Фильтр типов объектов** захардкожен в `DIFFED_TYPES`, в BACKLOG P3 — настраиваемый.

---

## 6. Где читать дальше

1. `-=docs=-/-=tasks=-/2026-07-28/20260728_001_compare_db_vs_fs_draft.md` — нормативный дизайн, §2 (решения).
2. `-=docs=-/-=tasks=-/phase_09/002_result_phase_09.md` — результат, отклонения от плана.
3. `LESSONS_LEARNED.md` §3, §18, §27, §35 — релевантные уроки.
4. `-=docs=-/-=tasks=-/BACKLOG.md` — edge diff (P2), GUI compare / Markdown (P3).
