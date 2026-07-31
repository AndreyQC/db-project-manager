# Phase 8: Overload resolution в edge detection

> **Дата:** 2026-07-31
> **Статус:** завершён
> **План/дизайн:** `-=tasks=-/phase_08/Phase_8_vision_final.md`, `Phase_8_plan.md`
> **Результат:** `-=tasks=-/phase_08/Phase_8_result.md`
> **Последний коммит фазы:** `2df1962`

---

## Цель фазы

BACKLOG P1: вызов перегруженной функции/процедуры (`sp_x(int4)` / `sp_x(text)`) должен
создавать ребро к **правильной** перегрузке, а не к «первой попавшейся» (first-wins в
`_build_names_index`). Phase 4 уже дала перегрузкам разные `object_key`, но edge-detection
это не использовал — теперь использует.

- По аргументам вызова определять, к какой перегрузке вести ребро (MVP — литералы).
- Неразрешимые вызовы — детерминированный fallback (first-wins) + transparent-протокол.
- Raw типы аргументов доступны на графе (`Vertex.argument_types`).
- Регрессия: число рёбер не убавляется.

## Что сделано

| Слой | Файл | Что изменилось |
|------|------|----------------|
| Domain | `domain/graph.py` | + `Vertex.argument_types: str = ""` (данные для inference) |
| SQL gen | `infrastructure/sql/sql_generator.py` | `_render_kind` пробрасывает `argument_types` в autodoc (function/procedure) |
| Parser | `infrastructure/parsing/pg_sql_parser.py` | чтение `argument_types`; `_build_overload_index`; routing перегрузок в `_scan_edges` отдельным проходом по raw SQL; сбор `_resolution_notes` |
| Parser (новый) | `infrastructure/parsing/overload_resolution.py` | чистые функции: inference литералов, `split_call_args`, `resolve_overload`, `find_calls`, `_mask_noncode` |
| Service | `application/graph_service.py` | `BuildGraphService.build` пишет `_overload_resolution_report.md` |
| Fixtures | `tests/fixtures/codebase_sample/...` | + `argument_types` в 4 файлах; новый `function sp_x_caller.sql` (вызывает обе перегрузки) |

## Ключевые архитектурные решения

- **Inference консервативен:** только литералы (`'…'`→text, `\d+`→int4, true/false→bool);
  всё прочее (колонки, вложенные вызовы, дробные, NULL) → unresolved. Никогда не угадывает
  (принцип безопасности, LESSONS §36).
- **Routing по raw SQL, не по word-потоку:** скобки вызова сносятся нормализатором.
  Перегруженные имена пропускаются в основном цикле; разрешения — post-loop, по ребру на
  resolved перегрузку (`Edge.dedup_key` схлопывает повторы).
- **Маскирование vs извлечение:** `_mask_noncode` бланкует строки/комментарии для *поиска*
  вызова, но тело аргументов срезается из **оригинала** (иначе `'a)b'` не дошёл бы до
  inference).
- **Определения ≠ вызовы:** `find_calls` пропускает `CREATE FUNCTION/PROCEDURE name(...)`.
- **Контракт `parse_directory → DependencyGraph` не сломан** (LESSONS §18): notes копятся
  в атрибуте парсера; отчёт пишет сервис — парсер без I/O.
- **Resolved в отчёт не попадают** — они видны в графе; отчёт = аудит неразрешённого.

## Проверки

```bash
unset SSL_CERT_FILE REQUESTS_CA_BUNDLE CURL_CA_BUNDLE && uv run pytest tests/unit/ -q
# 473 passed (380 baseline + 93 Phase 8)
uv run ruff check src/ tests/
# All checks passed!
```

Новые модули тестов: `test_overload_resolution.py` (+71), `test_graph_service.py` (+3);
расширены `test_sql_generator.py`, `test_pg_sql_parser.py`, `test_graph_model.py`,
`test_graph_store.py`. Регрессия счётчиков фикстуры учтена (snapshot/compare/deploy +1).

Коммиты: `d4b52e2` (S1) → `529e039` (S2) → `e999b2b` (S3) → `885aff5` (S4) →
`953885d` (S5) → `85bb440` (S6) → `5587418` (S7).

## Известные ограничения / NOT done

- **Колонки как аргументы** вызова — требуют индекса колонок (CREATE TABLE / catalog).
  New BACKLOG P2.
- **Тип результата вызова функции** как аргумент (рекурсия inference) — требует
  `return_type` на `Vertex`. New BACKLOG P2.
- **AST-based resolution** (sqlglot) — follow-up при false positives/negatives в проде.
- **FROM/JOIN перегруженной table-function** — классифицируется как `call`, не
  `select function` (rare; follow-up).
- **Прогон на `qr_pamyat`** (716+ рёбер, чеклист) — пользовательский, вне репо; в CI —
  unit-регрессия на `codebase_sample`.
- **Нет регрессионного теста `build=False → исключение из деплоя`** — выявлено при закрытии
  фазы (фильтрация работает, теста нет). New BACKLOG P3.

## Где читать дальше

- `-=tasks=-/phase_08/Phase_8_vision_final.md` — нормативный дизайн
- `-=tasks=-/phase_08/Phase_8_result.md` — результат (детально, с коммитами)
- `LESSONS_LEARNED.md` §26, §27, §36, §38 + «Phase 8» (528-536)
- `-=CHECKPOINTS=-/20260730_001_checkpoint.md` — предыдущее состояние (Phase 9)
