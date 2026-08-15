# Phase 8: План реализации — overload resolution в edge detection

> **Дата:** 2026-07-31
> **Ветка:** dev
> **Статус:** plan (нормативный документ для пошаговой реализации; на основе `_final`)
>
> Норматив-дизайн: `_tasks_/phase_08/Phase_8_vision_final.md`.
> Предшественник (история обсуждения): `_tasks_/phase_08/Phase_8_vision_draft.md`.
> Контекст: чекпойнт 20260730_001; BACKLOG P1; ROADMAP §2 шаг 1; LESSONS §26, §27, §36,
> §38, «Phase 8» (528-536).

---

## Принцип разбиения коммитов

Один логический шаг — один коммит (TASK_CONVENTIONS §6). **Код и документы не смешиваются.**
Шаги упорядочены снизу-вверх по цепочке данных (см. _final §4.1): сначала проброс типов
через генератор/парсер (чтобы данные появились), затем домен-поле, затем overload-aware
index + resolver + протокол, затем тесты. Тесты пишутся в том же коммите, что и код
юнит-функции (регрессионные — в отдельном в конце).

Глобальные команды проверок (урок §1 про uv/TLS — снять переменные):
```bash
unset SSL_CERT_FILE REQUESTS_CA_BUNDLE CURL_CA_BUNDLE && uv run pytest tests/unit/ -q
uv run ruff check src/ tests/
```

---

## Шаг P8.S1 — Проброс `argument_types` в autodoc (generator)

**Цель:** `object.argument_types` появляется в YAML-заголовке function/procedure файлов.

**Файл:** `src/db_project_manager/infrastructure/sql/sql_generator.py`

- `_render_kind` (259-293): для `object_type in {"function", "procedure"}` собрать
  `autodoc_extra` из `item.get("argument_types")` и пробросить в `_render_one`. В scope
  цикла (281) есть и `item`, и `ctx` — оба уже несут `argument_types`; берём из `item`
  (контрольная точка). Менять сигнатуру ctx_builder **не нужно** — `autodoc_extra`
  собирается локально в `_render_kind`.
  ```python
  # псевдокод в теле цикла, перед self._render_one(...)
  autodoc_extra = None
  if object_type in {"function", "procedure"}:
      at = item.get("argument_types") or ""
      if at:
          autodoc_extra = {"argument_types": at}
  self._render_one(..., autodoc_extra=autodoc_extra)
  ```
- Коллизия-гард `build_metadata` (`autodoc.py:83-85`) уже защищает стандартные поля —
  `argument_types` с ними не коллидирует, пройдёт.

**Тесты (этот же коммит):** расширить `tests/unit/test_sql_generator.py` — для
function/procedure assert что в сгенерированном файле `argument_types:` присутствует в
autodoc (через `extract_header(...)["object"]["argument_types"]`, как LESSONS §28 — roundtrip
через парсер, не substring). Проверить пустой `argument_types` → поле отсутствует (как
`object_signature` в `build_metadata`).

**NOT done тут:** парсер графа это поле пока не читает — появится в P8.S4.

---

## Шаг P8.S2 — Поле `Vertex.argument_types` (domain)

**Цель:** домен-модель несёт raw типы; round-trip через graph_store.

**Файл:** `src/db_project_manager/domain/graph.py`

- Добавить поле в `Vertex` (после `object_signature`, ~строка 56):
  ```python
  #: Raw comma-joined argument type list for overloaded functions/procedures
  #: (e.g. "int4", "text,varchar"). Empty for non-overloaded / non-routine objects.
  #: Populated by the parser from the autodoc header; used by overload resolution
  #: (Phase 8). Carries DATA for type inference, not identity (identity is object_key).
  argument_types: str = Field("", description="Raw argument types for overloaded functions/procedures")
  ```
- `model_config = ConfigDict(extra="ignore")` (строка 45) — поле теперь явное, не дропнется.

**graph_store** (`infrastructure/graph/graph_store.py`): менять **ничего** —
`model_dump(mode="json")` (74) / `Vertex.model_validate(...)` (109) подхватят новое поле
автоматически. FORMAT_VERSION бампить **не нужно** (поле аддитивное; проверка только `"1"`).

**diff-домен** (`domain/diff.py`): поле **не добавляем** (compare различает перегрузки по
`object_key` — _final §6).

**Тесты:** `tests/unit/test_pg_sql_parser.py` или новый `tests/unit/test_graph_store.py` —
round-trip: Vertex с `argument_types="int4"` → write_graph → read_graph → поле сохранено;
Vertex без поля → `argument_types == ""`.

---

## Шаг P8.S3 — Чтение `argument_types` парсером

**Цель:** `pg_sql_parser._parse_file` кладёт `argument_types` на Vertex.

**Файл:** `src/db_project_manager/infrastructure/parsing/pg_sql_parser.py`

- В autodoc-ветке `_parse_file` (~169, где читается `object_signature`):
  ```python
  argument_types = str(obj_meta.get("argument_types", "") or "")
  ```
  и в конструктор `Vertex(...)` добавить `argument_types=argument_types`.
- Fallback-ветка (без autodoc, ~198): `argument_types=""` (нет данных).

**Тесты:** фикстуры с перегрузками (`function sp_x__19f12f3f.sql` int4,
`function sp_x__982d9e3e.sql` text) — assert `vertex.argument_types` соответствуют
(«int4» / «text»). Сейчас эти фикстуры есть, но поле пустое.

---

## Шаг P8.S4 — Resolver: type inference литералов (новый модуль)

**Цель:** изолированно тестируемый модуль вывода типов и сравнения сигнатур.

**Новый файл:** `src/db_project_manager/infrastructure/parsing/overload_resolution.py`

Содержание (чистые функции, без I/O):
- `infer_literal_type(arg: str) -> str | None` — правила из _final §4.3. Возвращает
  `"text"|"int4"|"bool"` или `None` (unknown). Реализован консервативно: дробные,
  `NULL`, идентификаторы, арифметика → `None`. Целое → `int4` (но компаратор сигнатур
  ниже разрулит int-width ambiguity). Кавычки строковые: single-quote SQL-литерал
  `'…'` или double-quoted-identifier `"…"` (последний в PG — идентификатор, не строка!
  → возвращать `None`, не `text`). **Уточнить в коде комментарием.**
- `infer_call_signature(args: list[str]) -> tuple[str, ...] | None` — применить
  `infer_literal_type` к каждому аргументу; если **хоть один** `None` → весь вызов
  `None` (нельзя разрешить частично). Склеить в кортеж.
- `split_call_args(paren_body: str) -> list[str]` — разбить аргументы по запятым
  **верхнего уровня** (учитывая вложенные скобки и строки; не наивный `split(",")` —
  урок §27 о вложенных разделителях). Пустое тело → `[]` (вызов без аргументов).
- `resolve_overload(call_sig: tuple[str,...] | None, overloads: list[tuple[str, tuple[str,...]]]) -> str | None`
  — `call_sig`None → None. Иначе: сравнить с `overloads[i]` (canonical-кортеж каждой
  перегрузки). Ровно 1 совпадение → `(object_key, sig)`; 0 или >1 → `None`.
  - int-ambiguity: если `call_sig` содержит `int4`, а перегрузки различаются только
    int-вариантами (`int2`/`int4`/`int8`) — несколько совпадений → `None` (unresolved).

**Переиспользование:** `canonical_signature` из `domain/signature.py` для построения
`overloads`-кортежей (урок §27 — не писать свою canonical). На стороне вызова типы уже
каноничны (`int4`, не `integer`), canonical применить для единообразия.

**Тесты (этот же коммит):** `tests/unit/test_overload_resolution.py` —
- `infer_literal_type`: каждый случай (string-literal → text; double-quoted-id → None;
  int → int4; bool; NULL; float; identifier; arithmetic; empty).
- `split_call_args`: `a, b, c`; вложенные скобки `f(g(x, y), z)`; строка с запятой
  `'a,b'`; пустое `()`.
- `infer_call_signature`: смешанный (1 None → весь None).
- `resolve_overload`: ровно 1 совпадение → ключ; int-ambiguity → None; 0 совпадений → None;
  нет аргументов у вызова, а перегрузки разные → None.

---

## Шаг P8.S5 — Resolver: поиск вызовов в raw SQL

**Цель:** извлечь вызовы конкретной функции из тела файла.

**Тот же файл:** `infrastructure/parsing/overload_resolution.py`

- `find_calls(raw_sql: str, schema: str | None, name: str) -> list[CallSite]`
  где `CallSite = tuple[str, str]` (qualified_prefix, paren_body).
- Regex для qualified и bare:
  - qualified: `(?<![\w."])` + escaped(schema) + `\s*\.\s*` + escaped(name) + `\s*\((...)\)`
  - bare: `(?<![\w.])` + escaped(name) + `\s*\((...)\)`
  - группы-захвата тела скобок: balanced-paren match (regex `\(([^()]*(?:\([^()]*\)[^()]*)*)\)`
    покрывает 1 уровень вложенности; для глубже — небольшой ручной scan, т.к. `\((...)\)`
    не может быть произвольно вложенным в regex). Если вложенность >1 встретится —
    пропустить вызов + warning (ограничение regex-MVP, как §36).
- Игнорировать вызовы внутри строковых литералов и `--`/`/* */` комментариев по
  возможности (best-effort; CTE/dynamic-SQL — известные ограничения, как §36). Минимум:
  не матчить `name(` если `name` — часть большего идентификатора (lookbehind).

**Тесты:** `tests/unit/test_overload_resolution.py::test_find_calls` —
- bare вызов `sp_x(123)`;
- qualified `app.sp_x('x')`;
- несколько вызовов в одном файле;
- вызов внутри строкового литерала не детектится (best-effort);
- вложенные скобки 1 уровня (`sp_x(sp_y(1))` — body невыводим, но вызов детектируется).

---

## Шаг P8.S6 — Overload-aware index + интеграция в `_scan_edges`

**Цель:** graph build использует resolver для перегрузок.

**Файл:** `src/db_project_manager/infrastructure/parsing/pg_sql_parser.py`

- `_build_names_index` (231-243): тип возврата — вместо `dict[str, str]` вернуть структуру,
  различающую singleton и overload:
  ```python
  # dict[identifier, Union[str (single object_key), OverloadGroup]]
  ```
  Минимально: `dict[str, list[tuple[str, tuple[str,...]]]]` — для singleton список из 1
  элемента. Bare-name и full-name (`schema.name`) ключи — как сейчас.
  Каждый элемент: `(object_key, canonical_types_tuple)`; canonical_types_tuple строится
  из `vertex.argument_types` через `canonical_signature(...).split(",")`. Если у вершины
  `argument_types` пуст — canonical_types_tuple берётся из `object_signature`? **Нет** —
  hash не разворачивается в типы. Если `argument_types` пуст → кортеж = `()` с флагом
  `signature_unknown=True`; такие перегрузки помечаются как неразрешимые (UI-4).
- Сигнатура `_scan_edges` меняется: принимает новый индекс + доступ к `graph.vertices`
  (уже есть) + **raw SQL** текущего файла (для resolver). Значит `_parse_file` должен
  вернуть и `raw` (или `parse_directory` перечитает путь; чище — вернуть raw из `_parse_file`).
  Меняем возвращаемое значение `_parse_file` на `(Vertex|None, words, raw_sql)`.
- call-ветка `_scan_edges` (286-294): если matched-имя имеет 1 элемент — без изменений
  (current behavior). Если >1:
  - достать raw SQL файла, `schema`, `name`;
  - `find_calls(raw_sql, schema, name)` → для **каждого** вызова `infer_call_signature`
    + `resolve_overload`;
  - стратегия множественных вызовов в одном файле: если **все** вызовы разрешаются к
    **одной** перегрузке → ребро к ней; если вызовы расходятся → по одному ребру на
    каждую разрешённую перегрузку; неразрешённые → fallback-first + протокол.
  - fallback-first: выбрать первую вставленную перегрузку (стабильно — порядок
    `graph.vertices` insertion order).
  - логировать через `logger.warning` (урок §36), собирать записи для протокола.
- Проброс протокола: `parse_directory` собирает `list[ResolutionRecord]` и возвращает
  их вместе с графом? Граф — чистая доменная модель. Чище: resolver-записи копятся в
  поле парсера или возвращаются через отдельный канал. **Решение:** `parse_directory`
  возвращает только граф (контракт `ObjectGraphParser.parse_directory -> DependencyGraph`
  не меняем — урок §18-аналог про контракт); протокол пишется отдельным методом
  `PgSqlParser.last_resolution_report` (атрибут экземпляра) либо report собирателем в
  `BuildGraphService`. Вынести решение в реализацию; в плане — зафиксировать, что контракт
  парсера не ломается. Протокол-артефакт пишет `BuildGraphService` (см. P8.S7), чтобы парсер
  остался без I/O.

**Тесты:** основной функциональный тест Phase 8 — новая фикстура (ниже).

**Новая фикстура:** `tests/fixtures/codebase_sample/app/functions/function sp_x_caller.sql`
(или отдельный подкаталог `overload_caller/`), тело:
```sql
CREATE OR REPLACE FUNCTION app.sp_x_caller() RETURNS void ...
  PERFORM app.sp_x(123);      -- → int4 overload
  PERFORM app.sp_x('literal'); -- → text overload
```
Assert в `test_pg_sql_parser.py`: DEPENDS_ON-рёбра от caller к `sp_x__19f12f3f` (int4) и
`sp_x__982d9e3e` (text) соответственно — `edge.destination_object_key` содержит нужный hash.
Сейчас такого теста нет (исследование: ни одна фикстура не вызывает `sp_x`).

**Внимание:** эта фикстура **добавит рёбра** к `codebase_sample` — regression-тест счёта
рёбер (P8.S8) должен учитывать +N ожидаемых, а не жёстко захардкоженный старый count.

---

## Шаг P8.S7 — Протокол `_overload_resolution_report.md`

**Цель:** transparent-аудит неразрешённых вызовов.

**Файлы:** `src/db_project_manager/application/graph_service.py` (или где живёт
`BuildGraphService`) — пишет артефакт после `parse_directory`, если есть неразрешённые.
По образцу `application/qualify_refs_service.py:281-343`.

- Путь: `<codebase_dir>/_overload_resolution_report.md` (рядом с `_qualify_report.md`).
- Формат (_final §4.4): markdown-таблицы; секции «Разрешено» / «Не разрешено» + причина;
  заголовок + timestamp; футер про MVP-ограничение (только литералы).
- Записывает только если есть хоть одна unresolved (или всегда? _final §4.4 — «всегда», как
  qualify-refs; **решение:** всегда, чтобы пользователь видел что resolution вообще работал,
  даже если всё resolved — секция «Разрешено» подтверждает, пустой «Не разрешено» = ОК).
- `logger.warning` на каждое unresolved (дублирование в лог для CI).
- **Источник данных:** resolver-записи из P8.S6. Контракт парсера не трогаем → записи
  копятся в атрибуте парсера, `BuildGraphService` их читает после билда. (Альтернатива:
  парсер принимает опциональный коллектор-коллбэк — чище для тестов; финал в реализации.)

**Тесты:** `tests/unit/test_graph_service.py` (или новый) — на синтетическом графе с
неразрешённым вызовом: файл отчёта создан, содержит нужную строку (файл+причина),
формат markdown-таблицы.

---

## Шаг P8.S8 — Регрессия и итоговые проверки

**Цель:** Phase 8 не ломает существующее, рёбра не убавились.

- Регрессионный тест счёта рёбер на `codebase_sample`: обновить ожидаемый count с учётом
  новой фикстуры `sp_x_caller` (+2 ребра минимум). Assert «не меньше, чем раньше+N».
- Прогнать полный suite:
  ```bash
  unset SSL_CERT_FILE REQUESTS_CA_BUNDLE CURL_CA_BUNDLE && uv run pytest tests/unit/ -q
  uv run ruff check src/ tests/
  ```
  Цель: 380 baseline + новые тесты Phase 8 зелёные; ruff чисто (чекпойнт 20260730_001).
- (опц., требует Docker) `uv run pytest -m integration` — если добавляем integration-тест
  на edge-routing перегрузок. Решить по доступности Docker; иначе — unit-only (урок §21).

---

## Чеклист по урокам (для самопроверки перед каждым коммитом)

- [ ] §26: `argument_types` — данные, не идентичность; object_key не дублируется.
- [ ] §27: canonical через `domain/signature.py`; split-по-верхнему-уровню для аргументов.
- [ ] §36: regex-MVP + протокол; ambiguous=skip; resolver idempotent (только читает SQL).
- [ ] §38: call-ветку расширяем, не дублируем.
- [ ] §18: контракт `ObjectGraphParser.parse_directory -> DependencyGraph` не ломаем;
  fake'и/моки не затрагиваем.
- [ ] §3: нового SQL в queries.py нет.
- [ ] §12: `git add -- "_docs_/..."`.
- [ ] §1: снимать TLS-переменные перед `uv`.
- [ ] «Phase 8» (LESSONS 528-536): индексация по сигнатуре; partial MVP + протокол;
  регрессия на перегрузках; прогон на `qr_pamyat` (716+ рёбер) — последний пункт
  выполняется пользователем на локальной кодовой базе (вне репо); в CI — unit-регрессия.

---

## NOT done в Phase 8 (явно, для `Phase_08.md` и чекпойнта)

- Колонки как аргументы вызова (→ new BACKLOG P2 после фазы).
- Тип результата вызова функции как аргумент (→ new BACKLOG P2).
- AST-based resolution через sqlglot (→ BACKLOG P3+, по триггеру false positives).
- Прогон на `qr_pamyat` — пользовательский (вне репо); записать результат в чекпойнт.

---

## Где читать дальше

- `_tasks_/phase_08/Phase_8_vision_final.md` — нормативный дизайн
- `_tasks_/phase_08/Phase_8_vision_draft.md` — история обсуждения
- `LESSONS_LEARNED.md` §26, §27, §36, §38 + «Phase 8» (528-536)
- `application/qualify_refs_service.py:281-343` — образец протокола-артефакта
