# Phase 8: Overload resolution в edge detection — финальный дизайн (vision final)

> **Дата:** 2026-07-31
> **Ветка:** dev
> **Статус:** final (все USER_INPUT закрыты; нормативный документ для `_plan` и реализации)
>
> Предыдущий артефакт: `_tasks_/phase_08/Phase_8_vision_draft.md` (не удаляется —
> остаётся для истории обсуждения, по TASK_CONVENTIONS §2.2).
>
> Контекст:
> - `_checkpoints_/20260730_001_checkpoint.md` — текущее состояние (Phase 9 done)
> - `_tasks_/ROADMAP.md` §2, шаг 1 — Phase 8 как следующая фаза
> - `_tasks_/BACKLOG.md` P1 «Разрешение перегруженных вызовов в edge detection»
> - `LESSONS_LEARNED.md`: §26 (сигнатура в object_key), §27 (canonical: глобальное
>   до токенизации), §36 (regex-vs-AST + transparent-протокол), §38 (call-edge ветка),
>   «Контрольный список для Phase 8» (строки 528-536)
> - `src/db_project_manager/infrastructure/parsing/pg_sql_parser.py` — `_build_names_index`
>   (231), `_scan_edges` (245), call-ветка (286-294)
> - `src/db_project_manager/domain/graph.py` (`Vertex`, `Edge`),
>   `domain/signature.py` (`canonical_signature`, `signature_hash`)
> - `src/db_project_manager/infrastructure/sql/sql_generator.py` (`_render_kind`,
>   `_function_ctx`, `_procedure_ctx`), `infrastructure/sql/autodoc.py` (`build_metadata`,
>   `ensure_header`), `infrastructure/graph/graph_store.py` (round-trip Vertex)

---

## 1. Постановка проблемы

Phase 4 дала перегруженным функциям/процедурам **разные `object_key`** (суффикс
`/signature/<hash>`), так что перегрузки больше не затирают друг друга в графе
(LESSONS §26). Но edge-detection эту идентификацию **не использует**: `_build_names_index`
(`pg_sql_parser.py:231-243`) индексирует по голому `object_name` через `setdefault`
(first-wins), а `_scan_edges` (ветка call, строки 286-294) при совпадении имени берёт
единственный `dest_key` из индекса. Итог: вызов `app.sp_x(123)` и вызов `app.sp_x('x')`
получают ребро к **одной и той же** (первой вставленной) перегрузке, без учёта аргументов.

Это «первая попавшаяся вершина» из BACKLOG P1. Топосорт деплоя для перегрузок формально
строится, но рёбра недостоверны. Phase 10 (CD Foundation) опирается на граф —
недостоверные рёбра = некорректный порядок при развязке перегрузок через общие зависимости.

**Доп. препятствие (подтверждено исследованием кода):** для вывода типа по аргументам
вызова нужны **raw типы аргументов** перегрузки (`"int4"`, `"text,varchar"`). Сейчас они
есть upstream (adapter `argument_types` + generator в scope и хеширует), но в **autodoc**
пишется только 8-hex `object_signature` (`autodoc.py:80-81`), а `_render_kind`
(`sql_generator.py:283-293`) не пробрасывает `autodoc_extra` для function/procedure.
Парсер (`pg_sql_parser.py:169`) читает только хеш; на `Vertex` (`graph.py:38-59`) raw типов
нет. Вывод невозможен, пока типы не проброшены.

Скобки вызова `()` сносятся нормализатором до пробелов (`normalize.py:29-30`), поэтому
inference нельзя делать по нормализованному word-потоку — нужен regex-проход по **raw SQL**
файла (по образцу `QualifyRefsService` из §36).

## 2. Цель фазы

1. Ребро от вызова перегруженной функции/процедуры к **правильной** перегрузке, когда
   аргументы вызова позволяют это определить (MVP — литералы).
2. Неразрешимые вызовы — детерминированный fallback (к одной перегрузке, как сейчас)
   + **transparent-протокол** (лог + файл), чтобы пользователь знал, чему доверять.
3. Raw типы аргументов доступны на графе (`Vertex.argument_types`) — поле нужно и для
   будущих фаз (edge diff P2, structural column-diff CD-ALT-1).
4. Регрессия: число рёбер на реальной кодовой базе **не уменьшается** (чеклист Phase 8).

## 3. Принятые решения (все закрыты)

| Развилка | Решение | Обоснование |
|---|---|---|
| Гранулярность inference (MVP) | **Только литералы** | Колонки требуют индекса колонок (CREATE TABLE regex или catalog) — заметно больший объём. Литералы закрывают ядро BACKLOG P1 минимально. Колонки/рекурсия вызовов — follow-up (§6). |
| Хранение raw типов | **Новое поле `Vertex.argument_types: str = ""`** + запись `object.argument_types` в autodoc | Typed-доступ; сериализуется в `.dbm_graph/` автоматически (`model_dump`/`model_validate`). `extra="ignore"` на Vertex сейчас дропает unknown-ключи → поле обязательно добавить явно. |
| Протокол неразрешённых | **Лог (warning) + файл `_overload_resolution_report.md`** в корне codebase | По образцу `QualifyRefsService._write_report` (§36): persistent-аудит доверия к графу. Файл `.md` не попадает в SQL-скан. |
| Regex по raw SQL (не AST) | **Regex-MVP** | Согласуется с подходом qualify-refs (§36 tradeoff): покрывает function-call pattern `name(...)`, без новой зависимости (sqlglot есть с Phase 9, но AST-разбор вызовов — overkill для литералов). AST — follow-up при false positives/negatives в проде. |
| **UI-1** CLI-флаг отключения | **Не добавлять** | Поведение детерминированное (resolved → точное ребро, unresolved → fallback); флаг расширяет поверхность без выгоды. |
| **UI-2** Формат отчёта | **Markdown-таблицы** | Единый стиль отчётов проекта (как qualify-refs §36). |
| **UI-3** Размещение resolver-модуля | **`infrastructure/parsing/overload_resolution.py`** | Тестируется изолированно (как `domain/signature.py`); не раздувает `pg_sql_parser.py`. |
| **UI-4** Пустой/отсутствующий `argument_types` у перегрузки | **Unresolved + протокол** | Fallback-first, запись в отчёт (source = «no signature data»). Консервативно. |

## 4. Финальная архитектура

### 4.1. Проброс `argument_types` end-to-end

Цепочка (GAP-места помечены `!`):

1. `postgres/queries.py` GET_FUNCTIONS/GET_PROCEDURES → `argument_types` (raw тип-лист из `pg_type.typname`) ✓ (уже есть)
2. `postgres/adapter.py` `_get_functions`/`_get_procedures` → structure dict `argument_types` ✓
3. `sql_generator._function_ctx`/`_procedure_ctx` → `signature_hash(argument_types)` + в Jinja ctx ✓
4. **`!` `_render_kind` (`sql_generator.py:283-293`)** → пробросить `autodoc_extra={"argument_types": raw}` для function/procedure (механизм `extra` и коллизия-гард в `build_metadata` уже готовы)
5. `autodoc.build_metadata/ensure_header` → пишет `object.argument_types` в YAML ✓ (через `extra`)
6. **`!` `pg_sql_parser._parse_file` (~169)** → читать `argument_types` из autodoc
7. **`!` `domain/graph.py`** → добавить `Vertex.argument_types: str = ""`
8. `graph_store.write_graph/read_graph` → round-trip поля **автоматически** (`model_dump`/`model_validate`, field-list нет)

> Обратная совместимость: старые `.dbm_graph/` без поля — `argument_types` будет `""`,
> inference откатится к current-behavior (fallback). FORMAT_VERSION бампить не нужно
> (поле аддитивное; graph_store проверяет только `"1"`).
> **Регенерация кодовой базы:** старые reverse-engineer-деревья не имеют `argument_types`
> в autodoc → на них inference отключён. Рекомендовать повторный reverse-engineer после Phase 8.

### 4.2. Overload-aware names index + resolution в `_scan_edges`

- `_build_names_index` → для bare-имени с **>1** вершиной (перегрузка) собирать список
  `[(object_key, canonical_types_tuple)]`; для уникального имени — как сейчас (строка).
- `_scan_edges`, call-ветка (286-294): если matched-имя имеет одну вершину — без изменений
  (как сейчас). Если несколько — вызвать resolver.
- **Resolver** (`infrastructure/parsing/overload_resolution.py`):
  - Принимает raw SQL текущего файла, имя функции, схему, список перегрузок.
  - Regex-поиск вызовов `(\bschema\.)?\bname\s*\((...)\)` в raw SQL.
  - Для каждого вызова: split аргументов по верхнему уровню запятых → для каждого
    аргумента type inference → собрать каноническую сигнатуру вызова.
  - Сравнить `canonical_signature` выводимых типов с каждой перегрузкой. **Ровно 1
    совпадение** → та перегрузка. 0 или >1 → unresolved.

### 4.3. Type inference MVP — правила

| Аргумент (raw, trimmed) | Тип | Примечание |
|---|---|---|
| `'…'` / `"…"` | `text` | строковый литерал |
| `\d+` (без точки/e) | `int4` | **ambiguity**: PG может выбрать `int2`/`int4`/`int8`; если перегрузки различаются только шириной int — unresolved |
| `true` / `false` | `bool` | |
| `NULL` / `null` | unknown | не помогает разрешить → unresolved |
| дробное (`\d+\.\d+`, `\d*e\d*`) | `numeric`/`float8` | различие → unresolved |
| всё прочее (идентификатор, `f(...)`, арифметика) | unknown | → fallback + протокол |

> Ключевое свойство: inference **консервативен** — при любой неоднозначности возвращает
> unknown (никогда не угадывает). Это соответствует принципу безопасности проекта.

### 4.4. Протокол `_overload_resolution_report.md`

По образцу `QualifyRefsService._write_report` (`application/qualify_refs_service.py:281-343`):
markdown-таблицы, путь `<codebase_dir>/_overload_resolution_report.md`. Секции:
- заголовок + timestamp + codebase;
- **Разрешено** (resolved): файл, вызов, выбранная перегрузка (object_key), выводимые типы;
- **Не разрешено** (unresolved): файл, вызов, причина (ambiguous / 0 совпадений / нет
  аргументов / не-литерал / no signature data), к какой перегрузке отнесено (fallback-first).
- Замечание: MVP покрывает только литералы; колонки/вложенные вызовы — follow-up.
- `logger.warning` на каждое unresolved (по образцу §36; не подавлять).

## 5. Проверки (для `_plan`)

- **Новая фикстура** `tests/fixtures/codebase_sample/app/functions/function sp_x_caller.sql`
  (или отдельный каталог, чтобы не смешивать со счётчиками рёбер): тело вызывает
  `app.sp_x(123)` (→ int4) и `app.sp_x('literal')` (→ text). Assert: DEPENDS_ON-рёбра идут
  к `sp_x__19f12f3f` (int4) и `sp_x__982d9e3e` (text) соответственно. Это разрыв, который
  сейчас не покрыт (исследование: ни одна фикстура не вызывает `sp_x`).
- **Unit на resolver/inference литералов**: каждый тип литерала → корректный PG-тип;
  ambiguity → unknown; canonical_signature сравнение.
- **Регрессия «рёбра не убавились»**: на существующей фикстуре `codebase_sample` число
  рёбер до/после — не меньше. (Внимание: новая фикстура `sp_x_caller` добавит рёбра —
  regression-test должен учитывать +2 ожидаемых.)
- **(опц.) Integration**: testcontainers с перегрузками + вызовами — edge destination
  равен нужному overload-key.
- Обновить `tests/unit/test_pg_sql_parser.py` (assertion `edge.destination_object_key` ==
  конкретная перегрузка).
- `tests/unit/test_signature.py` уже покрывает canonical — переиспользовать.

## 6. NOT done / отложено

- **Колонки как аргументы** вызова — требуют индекса колонок (CREATE TABLE regex или
  catalog). New BACKLOG-запись P2.
- **Тип результата вызова функции** как аргумент (рекурсия inference) — требует
  `return_type` на `Vertex`. New BACKLOG-запись P2.
- **AST-based resolution** через sqlglot (зависимость уже есть с Phase 9) — follow-up при
  false positives/negatives в проде, как §36 для qualify-refs.
- **Edge diff** (сравнение рёбер графа между состояниями) — отдельный BACKLOG P2
  (Phase 9 follow-up), не часть Phase 8.
- **Поле в `domain/diff.py` snapshot** — не нужно: compare уже различает перегрузки по
  `object_key` (в нём `/signature/<hash>`).
- **CLI/GUI интерфейсы** — Phase 8 внутренняя (graph build), интерфейсы не меняются.

## 7. Чеклист по урокам

- [ ] §26: сигнатура уже в `object_key` — не дублировать идентификацию; поле
  `argument_types` несёт **данные** для inference, а не идентичность.
- [ ] §27: canonical_signature — глобальные преобразования (strip модификаторов) ДО split;
  переиспользовать существующую `domain/signature.py`, не писать свою.
- [ ] §36: regex-MVP + transparent-протокол; ambiguous = корректный skip, не баг;
  idempotent (resolver только читает SQL, не пишет).
- [ ] §38: call-edge ветка уже есть — расширить, не дублировать.
- [ ] §18: контракт `DatabaseAdapter` не расширяется → fake'и не ломаются.
- [ ] §3: нового SQL в `queries.py` нет → стат-проверка столбцов не нужна.
- [ ] §12: `git add -- "_docs_/_tasks_/phase_08/..."` (дефис в имени каталога).
- [ ] «Контрольный список для Phase 8» (LESSONS 528-536): индексация по сигнатуре;
  partial MVP + протокол; регрессия на перегрузках; прогон на `qr_pamyat` (716+ рёбер).

## 8. Где читать дальше

- `_tasks_/phase_08/Phase_8_vision_draft.md` — предшествующий драфт (история обсуждения)
- `_checkpoints_/20260730_001_checkpoint.md` — текущее состояние проекта
- `_tasks_/phase_04/Phase_4_vision_final.md` — Phase 4 (идентификация перегрузок)
- `_phases_/Phase_09.md` — compare (SQL-нормализация через sqlglot)
- `LESSONS_LEARNED.md` §26, §27, §36, §38 + «Phase 8»
