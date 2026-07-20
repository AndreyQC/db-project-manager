# План Phase 4 — Идентификация перегруженных функций и процедур

> Контекст:
> - `-=tasks=-/phase_04/Phase_4_vision_final.md` — зафиксированные решения
> - `-=tasks=-/phase_03/001_plan_phase_03.md` — образец формата плана фазы
> - `-=CHECKPOINTS=-/20260719_004_checkpoint.md` — текущее состояние проекта
>
> Дата: 2026-07-20

---

## 0. Цель фазы

Реализовать корректную идентификацию перегруженных функций и процедур PostgreSQL
на двух уровнях:

1. **Identity-слой** — сигнатура становится частью `object_key` и `Vertex.object_signature`;
   перегрузки не перезаписывают друг друга в графе.
2. **Имена файлов** — короткие (`function sp_x.sql`) по умолчанию; при перегрузках —
   суффикс из 8 hex-символов SHA-256 (`function sp_x__a1b2c3d4.sql`).

**Критерий «фаза готова»:** метрики приёмки из `Phase_4_vision_final.md` §7 выполнены.

---

## 1. Зафиксированные решения

| # | Решение |
|---|---------|
| Q1 | Суффикс имени файла — короткий SHA-256 хеш (8 hex-символов) |
| Q2 | Охват — полностью (identity-слой + file name) |
| Q3 | Разрешение перегруженных вызовов в edge detection НЕ входит в Phase 4 |

---

## 2. Ключевые факты кодовой базы (учтены в плане)

| Факт | Источник | Следствие |
|------|----------|-----------|
| Деплой берёт путь к файлу из `vertex.object_source_file` | `deploy_service.py:223` | Изменение имени файла НЕ ломает деплой — главное, чтобы парсер проставил путь |
| Сериализация графа использует `model_dump` | `graph_store.py:74,109` | Новое поле `Vertex.object_signature` попадёт в `.dbm_graph/` автоматически |
| `codebase_hash` зависит от содержимого и имён `.sql`-файлов | `graph_store.py:42-62` | Переименование файлов инвалидирует граф, но `is_stale` перестроит его автоматически |
| Pipeline разделён: `reverse-engineer` только генерирует файлы | `reverse_engineer.py:60-73` | Меняем SQLGenerator (запись) и парсер (чтение) независимо; контракт — autodoc |
| Шаблон `function.sql.j2` рендерит `argument_types` в DDL дважды | `function.sql.j2:1,5` | Не трогаем — `COMMENT ON FUNCTION ...(types)` обязателен для PostgreSQL |
| Существующие тесты не покрывают functions/procedures | `tests/fixtures/` | Расширяем фикстуры с нуля под новый контракт |
| `object_name` для перегрузок совпадает | `autodoc.py:66-74` | Поэтому различаем по `object_signature`, не по имени |

---

## 3. Порядок выполнения (P4.S01–P4.S06)

```
P4.S01  Доменная модель: Vertex.object_signature + signature utilities
       └─► P4.S02  Autodoc: сигнатура в object_key + header
              └─► P4.S03  SQLGenerator: короткие имена файлов + SHA-суффикс
                     └─► P4.S04  PgSqlParser: восстановление сигнатуры из автодока
                            └─► P4.S05  MVP-ограничение edge detection (только документация)
                                   └─► P4.S06  Чекпойнт
```

Зависимости строго последовательные: каждый шаг использует контракт предыдущего.

---

## P4.S01. Доменная модель — Vertex.object_signature + signature utilities

**Файлы:**
- **новый** `src/db_project_manager/domain/signature.py`
- `src/db_project_manager/domain/graph.py`
- **новый** `tests/unit/test_signature.py`
- `tests/unit/test_graph_model.py`

### `signature.py`

```python
"""Canonical signature utilities for overloaded functions/procedures.

PostgreSQL allows function/procedure overloading by argument types. The same
name can have multiple distinct objects. This module provides:
  * canonical_signature — normalized argument type list (mod-stripped, lowercased)
  * signature_hash — 8-hex-char SHA-256 of canonical signature (for file names)

argument_types arrives from pg_type.typname via the adapter (queries.py:255-261),
already normalized (int4/int8/bool/text, not integer/bigint/boolean/text) — so
no alias mapping is required. Only type modifiers like (255) or (64,0) need
stripping, since the same logical function can be reported with or without them.
"""
from __future__ import annotations

import hashlib
import re

# Match a parenthesized modifier at end of a type token: varchar(255), numeric(10,2)
_MOD_RE = re.compile(r"\s*\([^)]*\)\s*$")


def canonical_signature(argument_types: str) -> str:
    """Normalize argument type list for stable hashing.

    'text, varchar(255), uuid' -> 'text,varchar,uuid'
    Empty/whitespace input -> ''.
    """
    if not argument_types or not argument_types.strip():
        return ""
    parts = [_MOD_RE.sub("", p).strip().lower() for p in argument_types.split(",")]
    parts = [p for p in parts if p]
    return ",".join(parts)


def signature_hash(argument_types: str) -> str:
    """Return 8 hex chars of SHA-256 over canonical_signature.

    Empty argument_types -> '' (no hash for functions without args).
    """
    canon = canonical_signature(argument_types)
    if not canon:
        return ""
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:8]
```

### Изменения `graph.py`

Добавить поле в `Vertex` (после `object_name`, строки 50-51):

```python
object_name: str = ""
object_signature: str = Field("", description="Canonical signature for overloaded functions/procedures")
object_source_file: str = ...
```

### Тесты

**`tests/unit/test_signature.py`** (новый):
- `canonical_signature("text, varchar, varchar, uuid")` → `"text,varchar,varchar,uuid"`
- `canonical_signature("varchar(255)")` == `canonical_signature("varchar")`
  → одинаковый хеш (модификаторы игнорируются)
- `canonical_signature("numeric(10,2)")` == `canonical_signature("numeric")`
- `canonical_signature("")` → `""`; `signature_hash("")` → `""`
- `signature_hash("int4")` != `signature_hash("int8")`
- `signature_hash` — ровно 8 hex-символов

**`tests/unit/test_graph_model.py`**:
- `Vertex` создаётся с `object_signature=""` по умолчанию.
- `Vertex` с `object_signature="int4"` валиден; поле в `model_dump()` присутствует.

### Чек-лист
- [ ] `domain/signature.py` с `canonical_signature` и `signature_hash`.
- [ ] `Vertex.object_signature` добавлено.
- [ ] `tests/unit/test_signature.py` зелёный.
- [ ] `test_graph_model.py` расширен.
- [ ] `uv run ruff check` чист.

---

## P4.S02. Autodoc — сигнатура в object_key + header

**Файлы:**
- `src/db_project_manager/infrastructure/sql/autodoc.py`
- `tests/unit/test_autodoc.py`

### Изменения `autodoc.py`

1. `build_metadata(...)` и `ensure_header(...)` принимают опциональный
   `object_signature: str = ""`.
2. `_build_object_key` добавляет суффикс при непустой сигнатуре:
   ```python
   def _build_object_key(*, object_catalog, object_schema, object_type, object_name, object_signature=""):
       schema_part = f"schema/{object_schema}/" if object_schema else ""
       key = f"pg_database/{object_catalog}/{schema_part}type/{object_type}/name/{object_name}"
       if object_signature:
           key += f"/signature/{object_signature}"
       return key
   ```
3. `build_metadata` включает `object_signature` в блок `object:` (только если непустой —
   чтобы не засорять автодок для table/view).

```python
def build_metadata(*, object_catalog, object_schema, object_type, object_name, object_signature=""):
    object_key = _build_object_key(
        object_catalog=object_catalog, object_schema=object_schema,
        object_type=object_type, object_name=object_name,
        object_signature=object_signature,
    )
    obj = {
        "object_catalog": object_catalog,
        "object_schema": object_schema,
        "object_type": object_type,
        "object_name": object_name,
        "object_key": object_key,
    }
    if object_signature:
        obj["object_signature"] = object_signature
    return {"object": obj, "project": {"build": True}}
```

### Обратная совместимость

- Для всех объектов без `object_signature` (table/view/sequence/function без аргументов)
  `object_key` **не меняется** — суффикс не добавляется.
- Старые `.dbm_graph/` остаются корректными.

### Тесты `test_autodoc.py`

- function с сигнатурой `a1b2c3d4` → ключ заканчивается на `/signature/a1b2c3d4`.
- function без сигнатуры → ключ БЕЗ `/signature/` (как раньше).
- table → ключ без изменений (регресс).
- `build_metadata` для function с сигнатурой содержит `object_signature` в YAML.
- `build_metadata` для table НЕ содержит `object_signature`.
- Roundtrip `ensure_header` → `extract_header` сохраняет `object_signature`.

### Чек-лист
- [ ] `object_signature` в `build_metadata` и `ensure_header`.
- [ ] `_build_object_key` добавляет `/signature/<hash>` при непустой сигнатуре.
- [ ] Тесты на 3 сценария (function с сигнатурой / без / table).
- [ ] Roundtrip-тест.
- [ ] ruff чист.

---

## P4.S03. SQLGenerator — короткие имена файлов + SHA-суффикс

**Файлы:**
- `src/db_project_manager/infrastructure/sql/sql_generator.py`
- `tests/unit/test_sql_generator.py`
- `tests/fixtures/sample_structure.json`

### Изменения `sql_generator.py`

1. `_function_ctx` / `_procedure_ctx` возвращают **три** значения вместо двух:
   контекст, **имя удалено** (формирует `_render_kind`), сигнатура. Чтобы не ломать
   интерфейс ctx_builder для других типов — переходим на новую сигнатуру ctx_builder
   только для functions/procedures, либо делаем ctx_builder всегда возвращать
   `(ctx, base_name, signature)`. Выбрано второе (единообразие):
   ```python
   @staticmethod
   def _function_ctx(fn):
       sig = signature_hash(fn.get("argument_types", ""))
       ctx = {k: fn.get(k) for k in ("schema", "name", "argument_types", "definition", "comment")}
       return ctx, f"function {fn['name']}.sql", sig

   @staticmethod
   def _procedure_ctx(proc):
       sig = signature_hash(proc.get("argument_types", ""))
       ctx = {k: proc.get(k) for k in ("schema", "name", "argument_types", "definition", "comment")}
       return ctx, f"procedure {proc['name']}.sql", sig

   @staticmethod
   def _table_ctx(table):
       # ... ctx build ...
       return ctx, f"table {table['name']}.sql", ""

   # аналогично для sequence/view/matview
   ```

2. `_render_kind` собирает финальное имя файла:
   - группирует элементы по `base_name` (т.е. по `function <name>.sql`);
   - если в группе один элемент → имя = `base_name`;
   - если >1 → каждому добавляется `__<signature>`: `function sp_x__a1b2c3d4.sql`
     (вставка перед `.sql`).

3. `_render_one` принимает `object_signature` и прокидывает в `ensure_header`.

```python
def _render_kind(self, schema_info, kind, object_type, schema_dir, ctx_builder, object_catalog):
    items = schema_info.get(kind) or schema_info.get(f"{object_type}s") or []
    if not items:
        return
    kind_dir = schema_dir / kind
    kind_dir.mkdir(parents=True, exist_ok=True)
    schema_name = schema_info.get("name")

    # First pass: build (ctx, base_name, signature) for every item.
    prepared = []
    for item in items:
        ctx, base_name, sig = ctx_builder(item)
        prepared.append((item, ctx, base_name, sig))

    # Group by base_name to detect overloads.
    name_groups: dict[str, list[tuple]] = {}
    for entry in prepared:
        item, ctx, base_name, sig = entry
        name_groups.setdefault(base_name, []).append(entry)

    for base_name, group in name_groups.items():
        overloaded = len(group) > 1
        for item, ctx, _, sig in group:
            file_name = self._with_suffix(base_name, sig) if overloaded else base_name
            self._render_one(
                f"{object_type}.sql.j2",
                ctx,
                kind_dir,
                file_name,
                object_catalog=object_catalog or "",
                object_schema=item.get("schema", schema_name),
                object_type=object_type,
                object_name=item.get("name", ""),
                object_signature=sig,
            )

@staticmethod
def _with_suffix(base_name: str, sig: str) -> str:
    """Insert __<sig> before .sql: 'function sp_x.sql' -> 'function sp_x__a1b2c3d4.sql'."""
    if not sig:
        return base_name
    stem, dot, ext = base_name.rpartition(".")
    return f"{stem}__{sig}{dot}{ext}"
```

### Расширение `sample_structure.json`

Добавить schema `app` с:
```json
"functions": [
  {"schema": "app", "name": "sp_x", "argument_types": "int4", "definition": "...", "comment": null},
  {"schema": "app", "name": "sp_x", "argument_types": "text", "definition": "...", "comment": null},
  {"schema": "app", "name": "sp_y", "argument_types": "uuid", "definition": "...", "comment": null}
]
```

### Тесты `test_sql_generator.py`

- Одиночная функция → файл `function sp_y.sql`.
- Две перегрузки `sp_x` → файлы `function sp_x__<hash1>.sql`, `function sp_x__<hash2>.sql`,
  хеши различаются.
- В автодок-заголовке файлов перегрузок — `/signature/<hash>`.
- В автодок-заголовке одиночной функции — НЕТ `/signature/` (сигнатура есть в YAML-блоке,
  но ключ не расширен — см. S02: суффикс добавляется только когда есть реальная коллизия;
  сигнатура в YAML остаётся для парсера).

  > **Уточнение:** ключ в автодоке должен СОВПАДАТЬ с тем, что парсер потом использует как ID.
  > Если генератор пишет ключ без `/signature/` для одиночной функции, но парсер ничего не
  > добавляет (берёт ключ из автодока как есть) — согласованность сохраняется. Сигнатура в
  > YAML нужна только для потенциального будущего edge-detection, в Phase 4 не используется
  > парсером для построения ключа.

- Регресс: table/view/sequence → имена без изменений.

### Чек-лист
- [ ] ctx_builders возвращают `(ctx, base_name, signature)`.
- [ ] `_render_kind` группирует по `base_name`, добавляет суффикс при перегрузке.
- [ ] `_render_one` прокидывает `object_signature` в `ensure_header`.
- [ ] `sample_structure.json` расширен schema `app` с перегрузками.
- [ ] Тесты: одиночная/перегрузки/регресс.
- [ ] ruff чист.

---

## P4.S04. PgSqlParser — восстановление сигнатуры из автодока

**Файлы:**
- `src/db_project_manager/infrastructure/parsing/pg_sql_parser.py`
- `tests/unit/test_pg_sql_parser.py`
- `tests/fixtures/codebase_sample/` (новые файлы)

### Изменения `pg_sql_parser.py`

В ветке с автодоком (`_parse_file`, строки 147-167) — читать `object_signature`:

```python
if autodoc and autodoc.get("object"):
    obj_meta = autodoc["object"]
    # ... существующее ...
    signature = str(obj_meta.get("object_signature", "") or "")
    vertex = Vertex(
        object_key=object_key,
        object_catalog=catalog,
        object_schema=schema,
        object_type=object_type,
        object_name=name,
        object_signature=signature,
        object_source_file=_relative_posix(path, root),
        build=build,
    )
```

Сам `object_key` парсер берёт из автодока как есть — генератор уже встроил `/signature/<hash>`
при необходимости (S02 + S03). Парсер ничего не достраивает.

Fallback-ветка без автодока (строки 169-188) — `object_signature=""` (как было).

### Расширение `codebase_sample/`

Добавить `app/functions/`:
- `function sp_x__<hash1>.sql` (сигнатура `int4`, автодок с `object_signature: <hash1>`)
- `function sp_x__<hash2>.sql` (сигнатура `text`, автодок с `object_signature: <hash2>`)
- `function sp_y.sql` (сигнатура `uuid`, без коллизии — `/signature/` в ключе НЕТ)

Хеши вычисляются по `signature_hash("int4")`, `signature_hash("text")` — фиксируются
в фикстурах заранее (тест сам вычисляет и подставляет).

### Тесты `test_pg_sql_parser.py`

- `parse_directory(codebase_sample)` → содержит 2 вершины с разными `object_key`
  для перегрузок `sp_x` (контроль по `object_key`, а не по `object_name`).
- `object_source_file` каждой перегрузки проставлен корректно (относительный posix-путь).
- `object_signature` вершин соответствует хешам.
- `filter_build_true` сохраняет обе перегрузки.
- Регресс: table/view/sequence из существующего `codebase_sample/` парсятся как раньше.

### Чек-лист
- [ ] Чтение `object_signature` из автодока в `_parse_file`.
- [ ] Прокидывание в `Vertex`.
- [ ] Фикстуры `codebase_sample/app/functions/` созданы.
- [ ] Тесты: 2 перегрузки → 2 вершины, регресс.
- [ ] ruff чист.

---

## P4.S05. MVP-ограничение edge detection (только документация)

**Файлы:** `-=tasks=-/phase_04/Phase_4_vision_final.md` (уже зафиксировано в §6).

Код `_build_names_index` (`pg_sql_parser.py:212-224`) **НЕ меняется**.

**Поведение:** при перегрузках вызовы в SQL создают рёбра к вершине, первой попавшей
в `names_index` (по `setdefault`). Это принято как MVP — разрешение перегрузок по
аргументам вызова вынесено в Phase 5+.

Документирование считается выполненным в `Phase_4_vision_final.md` §6 (Q3).

### Чек-лист
- [ ] В `Phase_4_vision_final.md` §6 явно описано ограничение.
- [ ] В `LESSONS_LEARNED.md` добавлен пункт о silent overwrite perегрузок (был, исправлен).

---

## P4.S06. Чекпойнт

**Файл:** `-=CHECKPOINTS=-/20260720_001_checkpoint.md` + коммит
`docs(checkpoint): add 20260720_001 — Phase 4 complete`.

Структура — по `CHECKPOINTS_CONVENTION.md` (9 секций, 80-150 строк).

### Чек-лист
- [ ] Все 9 секций заполнены.
- [ ] Метрики приёмки из `Phase_4_vision_final.md` §7 подтверждены (галочки).
- [ ] `LESSONS_LEARNED.md` расширен (если есть новые уроки).
- [ ] Коммит `docs(checkpoint): ...`.

---

## 4. Порядок коммитов (рекомендуемый)

1. `docs(phase_04): add Phase 4 vision draft`
2. `docs(phase_04): finalize Phase 4 vision (all USER_INPUT resolved)`
3. `docs(phase_04): add detailed phase 4 plan`
4. `feat(domain): Vertex.object_signature + signature utilities (P4.S01)`
5. `feat(autodoc): signature in object_key + header (P4.S02)`
6. `feat(generator): short file names + SHA suffix for overloads (P4.S03)`
7. `feat(parser): restore signature from autodoc header (P4.S04)`
8. `docs(checkpoint): add 20260720_001 — Phase 4 complete`

Документы (1–3) и код (4–7) не смешивать в одном коммите — по конвенции `-=tasks=-`.

---

## 5. Метрики приёмки Phase 4

- [ ] `reverse-engineer` на БД с перегруженными функциями → файлы с уникальными короткими
      именами + SHA-суффиксом; на БД без перегрузок → просто `function sp_y.sql`.
- [ ] `graph build` → каждая перегрузка = отдельная вершина с уникальным `object_key`.
- [ ] `db-pm deploy` → все файлы (включая перегрузки) деплоятся без коллизий.
- [ ] Все существующие unit-тесты остаются зелёными.
- [ ] Новые тесты на P4.S01–P4.S04 зелёные.
- [ ] `uv run ruff check` чист.
- [ ] В `object_key` для table/view/sequence и function-без-аргументов формат **не изменился**.

---

## 6. Риски и смягчения

| Риск | Смягчение |
|------|-----------|
| `codebase_hash` инвалидируется | Механизм `is_stale` перестроит граф автоматически |
| Имя файла всё ещё >255 символов | Невозможно: макс. ≈ 86 символов |
| Старые `.dbm_graph/` несовместимы | `object_key` для неперегруженных объектов не меняется |
| Edge detection не различает перегрузки | Зафиксировано как MVP-ограничение → Phase 5+ |
| Регрессии в deploi/test контракте | Деплой берёт путь из `object_source_file`, не из имени |

---

## 7. Следующие шаги (за пределами Phase 4)

- Phase 5+: разрешение перегруженных вызовов в edge detection (type inference).
- Phase 5 (roadmap): миграции на БД с данными (diff, ALTER-план, pre/post-deploy).
