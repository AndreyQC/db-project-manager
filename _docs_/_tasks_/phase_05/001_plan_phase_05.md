# План Phase 5 — PostgreSQL extensions и настройки базы

> Дата: 2026-07-20
>
> Контекст:
> - `_tasks_/phase_05/Phase_5_vision_final.md` — нормативный дизайн (источник решений)
> - `_tasks_/TASK_CONVENTIONS.md` — нумерация шагов, правила коммитов
> - `LESSONS_LEARNED.md` — §1 (uv/TLS), §20 (posix-пути), §21 (integration-маркер), §26-28 (Phase 4)

Шаги `P5.S01…P5.S09`. Каждый шаг — отдельный коммит реализации
(`feat(extensions):` / `feat(settings):` / `test(...)` / `docs(...)`).
Документы и код не смешивать в одном коммите.

Порядок шагов — по слоям конвейера reverse → structure → render → autodoc →
parse → graph → deploy → integration → docs. После каждого шага:
`uv run pytest tests/unit/ -q` + `uv run ruff check src/ tests/` зелёные
(с `unset SSL_CERT_FILE REQUESTS_CA_BUNDLE CURL_CA_BUNDLE`, LESSONS §1).

---

## P5.S01. Queries: extensions и database settings

**Файл:** `src/db_project_manager/infrastructure/database/postgres/queries.py`

Новые константы (стиль — как существующие, SQLAlchemy `text()`, параметры через `:`):

1. `GET_EXTENSIONS` — список установленных extensions текущей БД:
   ```sql
   SELECT e.extname AS name,
          n.nspname AS schema,
          e.extversion AS version,
          obj_description(e.oid, 'pg_extension') AS comment
   FROM pg_extension e
   LEFT JOIN pg_namespace n ON n.oid = e.extnamespace
   ORDER BY e.extname
   ```
2. `GET_DATABASE_PROPERTIES` — свойства текущей БД (`current_database()`):
   `pg_encoding_to_char(encoding) AS encoding, datcollate, datctype`
   + имя template-источника недоступно из `pg_database` → template определяем
   эвристикой: если locale/encoding нестандартны — `template0` (см. P5.S07).
   Возвращает одну строку `{encoding, lc_collate, lc_ctype}`.
3. `GET_DATABASE_SETTINGS` — явно установленные параметры уровня БД:
   ```sql
   SELECT unnest(s.setconfig) AS setting
   FROM pg_db_role_setting s
   JOIN pg_database d ON d.oid = s.setdatabase
   WHERE d.datname = current_database() AND s.setrole = 0
   ORDER BY 1
   ```
   Каждый элемент `setconfig` — строка вида `work_mem=64MB` (`param=value`);
   разбор на пару — в адаптере (P5.S02). `setrole = 0` — фильтр Q5 (фиксируется
   здесь, не в коде адаптера).

**Тесты:** `tests/unit/test_queries.py` — расширить существующий валидатор
имён столбцов (LESSONS §3) на новые запросы: проверить, что алиасы `e.*`, `n.*`,
`s.*`, `d.*` ссылаются на валидные столбцы `pg_extension` / `pg_namespace` /
`pg_db_role_setting` / `pg_database`.

**Коммит:** `feat(extensions): add pg_extension/pg_database/pg_db_role_setting queries (P5.S01)`

---

## P5.S02. Adapter: structure["extensions"] и structure["database"]

**Файл:** `src/db_project_manager/infrastructure/database/postgres/adapter.py`

1. `_get_extensions()` — читает `GET_EXTENSIONS` → `list[dict]` вида
   `{name, schema, version, comment}`.
2. `_get_database_properties()` — читает `GET_DATABASE_PROPERTIES` → dict
   `{encoding, lc_collate, lc_ctype}` (одна строка).
3. `_get_database_settings()` — читает `GET_DATABASE_SETTINGS`, разбирает
   `param=value` → `list[dict]` вида `{name, value}`. Значение хранить как строку
   как есть (кавычки/экранирование — на стороне шаблона, P5.S03).
4. `get_database_structure()` — добавить верхнеуровневые ключи:
   ```python
   {"schemas": ..., "reserved_keywords": ...,
    "extensions": [...],
    "database": {"properties": {...}, "settings": [...]}}
   ```
   Extensions/settings читаются один раз (глобальны), не per-schema.

**Тесты:** `tests/unit/test_reverse_engineer.py` (или профильный файл) —
мок `_exec` по образцу существующих тестов: extensions попадают в верхний уровень;
`settings` разбираются в пары; пустой `pg_db_role_setting` → `settings: []`.

**Коммит:** `feat(extensions): expose extensions and db settings in structure (P5.S02)`

---

## P5.S03. SQLGenerator: рендер extensions и database settings

**Файлы:**
- `src/db_project_manager/infrastructure/sql/sql_generator.py`
- `src/db_project_manager/infrastructure/templates/database_setting.sql.j2` (новый)
- `tests/fixtures/sample_structure.json` (добавить `extensions`, `database`)

1. **Extensions** — top-level рендер в `generate_scripts`, вне per-schema цикла:
   `<output>/extensions/extension <name>.sql` через существующий
   `templates/extension.sql.j2` (контекст `{name, schema, version, comment}`).
   VERSION не пиновать (Q3): в ctx передавать `version=None`, фактическую версию —
   в autodoc (см. P5.S04). Autodoc: `object_schema=None` → schema-less ключ.
2. **Database settings** — новый шаблон `database_setting.sql.j2`:
   ```jinja
   -- Параметры уровня базы (ALTER DATABASE ... SET)
   {% for s in settings %}
   ALTER DATABASE {{ _qi(database_name) }} SET {{ s.name }} = '{{ s.value }}';
   {% endfor %}
   ```
   Рендер в `<output>/settings/database settings.sql` (один файл, Q2), только если
   `settings` непустой. Имя целевой БД в `ALTER DATABASE` — placeholder: при deploy
   validate имя временной БД другое → шаблон рендерит с именем исходной БД, а
   deploy перед execute подменяет имя (см. P5.S07, `_deploy_object` для
   database_setting). Это зафиксировать комментарием в шаблоне и плане.
   Файл создаётся даже при пустых `settings`, если есть `properties` — тогда тело
   только комментарий (свойства живут в autodoc, P5.S04).
3. Проверить `trim_blocks`-ловушку (LESSONS §5): контент-строки не должны
   оканчиваться блочным тегом — цикл `{% for %}` на своих строках.

**Тесты:** `tests/unit/test_sql_generator.py` — на обновлённой фикстуре:
файлы `extensions/extension citext.sql`, `settings/database settings.sql`
создаются; assert подстрок (`CREATE EXTENSION IF NOT EXISTS "citext"`,
`ALTER DATABASE ... SET work_mem`); визуальная проверка рендера (LESSONS §5/§6).

**Коммит:** `feat(extensions): render extension and database_setting scripts (P5.S03)`

---

## P5.S04. Autodoc: schema-less типы и блок properties

**Файл:** `src/db_project_manager/infrastructure/sql/autodoc.py`

1. `ensure_header` — принять `object_type="extension"` / `"database_setting"` с
   `object_schema=None` (ключ уже поддерживает пропуск `schema/`, autodoc.py:91) —
   проверить и покрыть тестом. Для extension добавить информационное поле
   `extension_version` в header (Q3).
2. Для `database_setting` — опциональный блок `properties`
   (`{encoding, lc_collate, lc_ctype, template}`) в header; `extract_header`
   возвращает его; парсер кладёт в `vertex.extra["db_properties"]` (P5.S05).
3. Roundtrip-стойкость: тесты через `extract_header` (не substring, LESSONS §28).

**Тесты:** `tests/unit/test_autodoc.py` — roundtrip для обоих типов:
schema-less `object_key`, `properties` сохраняются, версия extension в header.

**Коммит:** `feat(settings): autodoc header for extension/database_setting (P5.S04)`

---

## P5.S05. Parser: новые типы

**Файл:** `src/db_project_manager/infrastructure/parsing/pg_sql_parser.py`

1. `SUPPORTED_TYPES` += `"extension"`, `"database_setting"`.
2. `_CREATE_KEYWORD_TO_TYPE` += `(("extension",), "extension")` (для fallback-
   токенизации; autodoc-путь основной).
3. Autodoc-путь: при `object_type == "database_setting"` переносить
   `properties` из header в `vertex.extra["db_properties"]`.
4. Убедиться, что файл `settings/database settings.sql` не матчится под schema-
   директории — parser опирается на autodoc, путь произвольный; проверить тестом.

**Тесты:** `tests/unit/test_pg_sql_parser.py` — codebase_sample дополнить
`extensions/extension citext.sql` и `settings/database settings.sql`:
обе вершины создаются, `object_schema is None`, `extra["db_properties"]` заполнено.

**Коммит:** `feat(extensions): parse extension and database_setting types (P5.S05)`

---

## P5.S06. Toposort: приоритеты −2/−1

**Файл:** `src/db_project_manager/infrastructure/graph/topological_sort.py`

```python
TYPE_PRIORITIES = {
    "extension": -2, "database_setting": -1,
    "schema": 0, ...  # без изменений
}
```

**Тесты:** `tests/unit/test_topological_sort.py` — граф с extension +
database_setting + schema + table: порядок строго extension → database_setting →
schema → table; детерминизм (повторный прогон — тот же порядок).

**Коммит:** `feat(extensions): priority -2/-1 for extension/database_setting (P5.S06)`

---

## P5.S07. Deploy: свойства БД при create_database + fail-fast типы

**Файлы:**
- `src/db_project_manager/infrastructure/database/base.py` — сигнатура
  `create_database(name, *, encoding=None, lc_collate=None, lc_ctype=None, template=None)`
- `src/db_project_manager/infrastructure/database/postgres/adapter.py` — реализация:
  опциональные `ENCODING`/`LC_COLLATE`/`LC_CTYPE`/`TEMPLATE`; whitelist-валидация
  значений locale/encoding (LESSONS §19: идентификаторы/значения из codebase —
  не f-string без проверки). При нестандартных encoding/locale — `TEMPLATE template0`
  (риск §6 vision). Идентификатор БД — существующий `_validate_db_name`.
- `src/db_project_manager/application/deploy_service.py`:
  1. `EARLY_DDL_TYPES` += `"extension"`, `"database_setting"`.
  2. `DeployValidateService.run`: после `deploy_order` найти вершину
     `database_setting`, взять `extra["db_properties"]` → передать в
     `create_database`. Fallback: нет вершины/свойств — поведение как раньше.
  3. `_deploy_object` для `database_setting`: подмена имени БД в скрипте на имя
     временной БД (параметр `target_db_name`; замена через `ALTER DATABASE` -
     префикс, задокументировать формат). Если в графе есть extension, требующий
     отсутствующий на сервере пакет — fail-fast с именем (уже даёт EARLY_DDL_TYPES).
- FakeAdapter в conftest — расширить под новую сигнатуру (LESSONS §18: сначала
  прогнать существующие тесты, они подсветят потребителей).

**Тесты:** `tests/unit/test_deploy_service.py` — validate применяет properties к
временной БД (FakeAdapter фиксирует аргументы `create_database`); database_setting
деплоится с именем временной БД; ошибка extension — fail-fast.

**Коммит:** `feat(settings): apply db properties in deploy validate (P5.S07)`

---

## P5.S08. Integration-тест (testcontainers)

**Файл:** `tests/integration/test_phase5_real_db.py` (новый, маркер
`@pytest.mark.integration`, deselected по умолчанию — LESSONS §21).

Сценарий на живой PG (testcontainers, harness из Phase 2):
1. Создать БД: `CREATE EXTENSION citext`; таблица с колонкой `citext`;
   перегруженные функции `f(int)` / `f(text)`; `ALTER DATABASE ... SET work_mem`;
   нестандартные `ALTER DATABASE`-параметры.
2. `reverse-engineer` → проверить `structure["extensions"]`, `structure["database"]`.
3. `graph build` → вершины extension/database_setting/обе перегрузки.
4. `deploy validate` → зелёный; обе перегрузки и extension задеплоены (контроль
   через запрос к временной БД или `keep_db`).

Закрывает BACKLOG P2 (integration reverse→graph→deploy с перегрузками).

**Коммит:** `test(integration): real-PG roundtrip with extension and overloads (P5.S08)`

---

## P5.S09. Документация и закрытие фазы

1. `LESSONS_LEARNED.md` — уроки Phase 5 (по факту реализации).
2. `_tasks_/BACKLOG.md` — снять P1 (extensions) и P2 (integration test);
   добавить: рёбра DEPENDS_ON → extension (P3), пин версий extensions (P3),
   template-источник БД через `datdba`/каталог (если всплывёт).
3. `_checkpoints_/<YYYYMMDD>_001_checkpoint.md` — новый checkpoint «Phase 5
   complete» по конвенции (таблица архитектуры, gaps, команды тестов).
4. Коммиты: `docs(lessons):`, `docs(backlog):`, `docs(checkpoint):` — раздельно.

---

## Зависимости шагов

```
S01 → S02 → S03 → S04 → S05 → S06 → S07 → S08 → S09
              (S03 зависит от S04 только по autodoc-вызову ensure_header —
               делать S04 перед S03 при конфликте; допустимо слить S03+S04
               в один шаг, если diff мал)
```

## Критерий готовности фазы

- Все метрики приёмки из `Phase_5_vision_final.md` §3 выполнены.
- `uv run pytest tests/unit/ -q` зелёный; `uv run pytest -m integration` зелёный
  (при запущенном Docker).
- Нет `*_draft.md` в `phase_05/`, кроме исторического vision draft.
