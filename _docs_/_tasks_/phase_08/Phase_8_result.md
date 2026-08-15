# Phase 8: Overload resolution в edge detection — результат

> **Дата:** 2026-07-31
> **Ветка:** dev
> **Статус:** реализовано; проверки пройдены
>
> Норматив-дизайн: `_tasks_/phase_08/Phase_8_vision_final.md`.
> План: `_tasks_/phase_08/Phase_8_plan.md` (шаги P8.S1–P8.S8).
> Контекст: чекпойнт 20260730_001 (Phase 9 done), BACKLOG P1, ROADMAP §2 шаг 1.

---

## 1. Цель и что сделано

**Цель BACKLOG P1:** вызов перегруженной функции/процедуры должен создавать ребро к
**правильной** перегрузке (по аргументам), а не к «первой попавшейся» (first-wins в
`_build_names_index`). MVP — литералы; неразрешимые вызовы — детерминированный fallback
+ transparent-протокол.

**Реализовано (8 шагов):**

| Шаг | Коммит | Суть |
|-----|--------|------|
| P8.S1 | `d4b52e2` | `_render_kind` пробрасывает `argument_types` в autodoc для function/procedure |
| P8.S2 | `529e039` | поле `Vertex.argument_types` (round-trip в graph_store автоматический) |
| P8.S3 | `e999b2b` | парсер читает `argument_types` из autodoc; фикстуры обновлены |
| P8.S4 | `885aff5` | `overload_resolution.py`: inference литералов + `resolve_overload` |
| P8.S5 | `953885d` | `find_calls`: поиск вызовов в raw SQL (маскирование строк/комментариев) |
| P8.S6 | `85bb440` | overload-aware index + routing в `_scan_edges`; фикстура `sp_x_caller` |
| P8.S7 | `5587418` | протокол `_overload_resolution_report.md` в `BuildGraphService` |
| P8.S8 | — (проверки) | регрессия: 473 passed, ruff чист, рёбра не убавились |

## 2. Что изменилось (по категориям)

### Backend

| Файл | Что изменилось |
|------|----------------|
| `domain/graph.py` | + поле `Vertex.argument_types: str = ""` (данные для inference, не идентичность — LESSONS §26) |
| `infrastructure/sql/sql_generator.py` | `_render_kind` собирает `autodoc_extra={"argument_types": ...}` для function/procedure; пустое — опускается |
| `infrastructure/parsing/pg_sql_parser.py` | `_parse_file` читает `argument_types`, возвращает `(Vertex, words, raw)`; `_build_overload_index` группирует рутинные перегрузки; `_scan_edges` пропускает перегруженные имена в основном цикле и разрешает их отдельным проходом по raw SQL (по ребру на resolved перегрузку); `_resolve_overloaded_calls` + `_record_unresolved`; `__init__` инициализирует `_resolution_notes` |
| `infrastructure/parsing/overload_resolution.py` (новый) | чистые функции: `infer_literal_type`, `split_call_args`, `infer_call_signature`, `resolve_overload`, `find_calls`, `_mask_noncode` |
| `application/graph_service.py` | `BuildGraphService.build` пишет `_overload_resolution_report.md` при наличии `_resolution_notes`; `_render_overload_report` (markdown-таблицы) |

### Sample-данные / фикстуры

| Файл | Что изменилось |
|------|----------------|
| `tests/fixtures/codebase_sample/app/functions/function sp_x__19f12f3f.sql` | + `argument_types: int4` |
| `tests/fixtures/codebase_sample/app/functions/function sp_x__982d9e3e.sql` | + `argument_types: text` |
| `tests/fixtures/codebase_sample/app/functions/function sp_y.sql` | + `argument_types: uuid` |
| `tests/fixtures/codebase_sample/app/functions/function sp_caller.sql` | + `argument_types: uuid` |
| `tests/fixtures/codebase_sample/app/functions/function sp_x_caller.sql` (новый) | вызывает обе перегрузки `sp_x` литералами — ядро теста edge-routing |

## 3. Ключевые архитектурные решения

- **Inference консервативен.** Только литералы (`'…'`→text, `\d+`→int4, true/false→bool).
  Дробные, NULL, double-quoted-идентификаторы, колонки, вложенные вызовы → `None` → весь
  вызов unresolved. Никогда не угадывает (принцип безопасности, LESSONS §36).
- **int-литерал → int4.** PG по умолчанию назначает нетипизированному целому тип `int4`;
  если перегрузки только `int2`/`int8` (без int4) — вызов неразрешим (conservative skip).
- **Routing — отдельный проход по raw SQL.** Скобки вызова сносятся нормализатором, поэтому
  inference нельзя делать по нормализованному word-потоку. Перегруженные имена
  пропускаются в основном цикле; разрешения — post-loop, по ребру на resolved перегрузку.
  Дедуп (`Edge.dedup_key`) схлопывает повторы.
- **Regex-MVP, не AST.** Согласовано с подходом qualify-refs (LESSONS §36). AST (sqlglot,
  уже в deps) — follow-up при false positives/negatives в проде.
- **Маскирование vs извлечение аргументов.** `_mask_noncode` бланкует строки/комментарии
  для *поиска* вызова (чтобы `sp_x(` в строке не детектировалось), но тело аргументов
  срезается из **оригинала** — иначе строковый литерал-аргумент `'a)b'` не дошёл бы до
  inference.
- **Определения ≠ вызовы.** `find_calls` пропускает `CREATE FUNCTION/PROCEDURE name(...)`
  (параметры — не аргументы вызова) — иначе `infer_call_signature(['a int4'])` давал бы
  ложный signature.
- **Zero-call match — молча.** Имя встретилось в файле (в определении/комментарии), но
  вызовов нет → нет ребра, нет note (не «неразрешённые вызовы», а «вызовов нет»).
- **Контракт `ObjectGraphParser.parse_directory → DependencyGraph` не сломан** (LESSONS
  §18-аналог): notes копятся в атрибуте парсера, отчёт пишет сервис — парсер без I/O.
- **Resolved в отчёт не попадают.** Resolved-рёбра видны в графе напрямую; отчёт — аудит
  того, чему доверять нельзя (неразрешённые → fallback).

## 4. Проверки

```bash
unset SSL_CERT_FILE REQUESTS_CA_BUNDLE CURL_CA_BUNDLE && uv run pytest tests/unit/ -q
# 473 passed (380 baseline + 93 Phase 8)
uv run ruff check src/ tests/
# All checks passed!
```

Новые тесты Phase 8:
- `test_sql_generator.py`: +5 (argument_types в autodoc: int4/text перегрузки, singleton,
  процедуры, регрессия таблиц, no-arg — через `extract_header` roundtrip, LESSONS §28).
- `test_graph_model.py` / `test_graph_store.py`: +5 (поле Vertex, round-trip, default).
- `test_pg_sql_parser.py`: +6 (чтение argument_types; edge-routing к int4/text
  перегрузкам; singleton-call без изменений).
- `test_overload_resolution.py` (новый): +71 (inference каждого литерала; split с
  вложенными скобками/строками; partial-inference отказ; int-width ambiguity; find_calls:
  qualified/bare/множественные/masking/определения/zero-arg).
- `test_graph_service.py` (новый): +3 (отчёт пишется при unresolved; не пишется при all
  resolved / singleton).

Регрессия счётчиков фикстуры (новая `sp_x_caller`): snapshot 11→12, compare 11→12,
deploy 12→13.

**Ручная проверка пользователем** (каталог `qr_pamyat`, вне репо): во время закрытия Phase 8
выявлен побочный вопрос про `build:false` — расследован, оказался устаревшим GUI
(не перезапущен), код корректен. Сам Phase 8 на кодовой базе пользователя не прогнан до
конца — см. «Известные ограничения».

## 5. Известные ограничения / NOT done

- **Колонки как аргументы вызова** (прямые ссылки на колонки таблиц с известным типом) —
  требуют индекса колонок (CREATE TABLE regex или catalog). New BACKLOG P2.
- **Тип результата вызова функции** как аргумент (рекурсия inference) — требует
  `return_type` на `Vertex`. New BACKLOG P2.
- **AST-based resolution** через sqlglot — follow-up при false positives/negatives в проде
  (как §36 для qualify-refs).
- **FROM/JOIN перегруженной table-function** — классифицируется как generic `call`, а не
  `select function` (rare edge case; follow-up).
- **Прогон на `qr_pamyat`** (716+ рёбер, чеклист Phase 8) — пользовательский, вне репо;
  не записан в CI. Unit-регрессия на `codebase_sample` проходит.
- **Нет регрессионного теста на путь `build=False → исключение из деплоя`** — выявлено при
  закрытии фазы (фильтрация работает, но не покрыта тестом). New BACKLOG P3.

## 6. Контрольный список (LESSONS — самопроверка)

- [x] §26: `argument_types` — данные, не идентичность; object_key не дублируется.
- [x] §27: canonical через `domain/signature.py`; split-по-верхнему-уровню для аргументов.
- [x] §36: regex-MVP + transparent-протокол; ambiguous=skip; resolver idempotent (только
  читает SQL).
- [x] §38: call-ветку расширили, не дублировали; singleton-call без изменений.
- [x] §18: контракт `parse_directory → DependencyGraph` не ломали; fake'и не затронуты.
- [x] §3: нового SQL в queries.py нет.
- [x] §12: `git add -- "_docs_/..."`.
- [x] §1: TLS-переменные сняты перед `uv`.
- [x] «Phase 8» (LESSONS 528-536): индексация по сигнатуре ✓; partial MVP + протокол ✓;
  регрессия на перегрузках ✓; прогон на `qr_pamyat` — пользовательский (см. ограничения).

## 7. Где читать дальше

- `_tasks_/phase_08/Phase_8_vision_final.md` — нормативный дизайн
- `_tasks_/phase_08/Phase_8_plan.md` — пошаговый план (P8.S1–S8)
- `_tasks_/phase_08/Phase_8_vision_draft.md` — история обсуждения
- `LESSONS_LEARNED.md` §26, §27, §36, §38 + «Phase 8» (528-536)
- `application/qualify_refs_service.py:281-343` — образец протокола-артефакта
