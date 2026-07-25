# Phase 5 Vision — PostgreSQL extensions и настройки базы (final)

> Дата: 2026-07-20
> Статус: FINAL — все `USER_INPUT` закрыты (2026-07-20)
>
> Контекст:
> - `-=tasks=-/phase_05/Phase_5_vision_draft.md` — история обсуждения (альтернативы Q1–Q8)
> - `-=CHECKPOINTS=-/20260720_001_checkpoint.md` — состояние после Phase 4
> - `-=tasks=-/BACKLOG.md` — P1 (extensions end-to-end), P2 (integration test)
> - `-=tasks=-/TASK_CONVENTIONS.md`
> - `LESSONS_LEARNED.md`

---

## 1. Проблема

`db-pm deploy validate` падает на реальной БД, использующей extension-типы:

```
тип "citext" не существует
LINE 13:     "public_email" citext NULL,
```

Причины две:

1. **Extensions оборваны на всех слоях.** Reverse-engineer не читает `pg_extension`,
   структура не содержит extensions, `extension.sql.j2` не вызывается, парсер не знает
   тип `extension`, в `TYPE_PRIORITIES` его нет. Временная БД при `deploy validate`
   создаётся пустой — без `CREATE EXTENSION`.
2. **Настройки базы не переносятся.** Свойства `CREATE DATABASE` (encoding,
   lc_collate, lc_ctype, template) и параметры `ALTER DATABASE ... SET` не
   извлекаются и не воспроизводятся. БД с нестандартной локалью или с параметрами
   уровня базы (например `search_path`, `work_mem`, `statement_timeout`)
   восстанавливается с дефолтами кластера — DDL/DML может вести себя иначе.

Цель Phase 5 — замкнуть оба контура end-to-end:
reverse → structure → render → parse → graph → toposort → deploy/validate.

---

## 2. Текущее состояние (факты по коду)

| Слой | Extensions | Database settings |
|------|-----------|-------------------|
| Queries (`infrastructure/database/postgres/queries.py`) | нет запросов к `pg_extension` | нет запросов к `pg_database` / `pg_db_role_setting` |
| Structure (`adapter.get_database_structure`) | только `{"schemas", "reserved_keywords"}` | нет |
| Template | `templates/extension.sql.j2` есть, не вызывается | нет шаблона |
| SQLGenerator (`_OBJECT_KINDS`, sql_generator.py:114) | не рендерит; extensions не schema-scoped — текущий цикл per-schema не подходит | — |
| Parser (`SUPPORTED_TYPES`, pg_sql_parser.py:36-46) | нет `extension`; файл молча пропускается (autodoc path L150-152) | нет |
| Toposort (`TYPE_PRIORITIES`, topological_sort.py:25-39) | нет; попадёт в UNKNOWN=100 | нет |
| Deploy (`deploy_service.py`) | `create_database(name)` — только имя (adapter.py:170-179) | `ALTER DATABASE` нигде не вызывается |
| Domain (`Vertex.object_type`) | free-form string — новый тип не требует смены модели | то же |
| Graph (`Relation.DEPENDS_ON`, graph.py:34) | комментарий явно резервирует под extension edges | — |

Опорные факты:
- `object_key` поддерживает schema-less объекты (`schema/` сегмент опускается при
  `object_schema=None`, autodoc.py:78-95) — подходит для extensions и db settings.
- `sort_by_type_and_topology`: приоритет типа доминирует над топологией
  (`(type_priority, topo_index, object_key)`) — приоритеты −2/−1 ставят новые типы
  раньше `schema: 0`.
- `EARLY_DDL_TYPES` (deploy_service.py:39) — fail-fast типы; extension и
  database_setting логично туда.

---

## 3. Цели и метрики приёмки

**Цель A — extensions end-to-end:**
1. `reverse-engineer` БД с `citext`/`uuid-ossp`/etc. → структура содержит
   `structure["extensions"]`, генерируются файлы `<output>/extensions/extension <name>.sql`.
2. `graph build` на такой codebase → вершины `type/extension` деплоятся первыми
   (приоритет −2, раньше `schema`).
3. `deploy validate` на временной БД с таблицей, использующей extension-тип, — зелёный.

**Цель B — настройки базы:**
4. `reverse-engineer` извлекает свойства базы (encoding, lc_collate, lc_ctype,
   template) и явно установленные `ALTER DATABASE ... SET` параметры.
5. Сгенерированный codebase воспроизводит их; `deploy validate` применяет
   свойства к временной БД при создании.

**Метрики:**
- unit-тесты зелёные (`uv run pytest tests/unit/ -q`), новые слои покрыты по образцу
  Phase 4 (queries/structure/generator/parser/toposort).
- Integration-тест на testcontainers: БД с extension + перегруженной функцией →
  `reverse → graph → deploy validate` → обе перегрузки и extension задеплоены
  (закрывает BACKLOG P2 про integration test).

---

## 4. Проектные решения

### 4.1. Extensions

| Слой | Изменение |
|------|-----------|
| queries.py | `GET_EXTENSIONS` — `pg_extension e LEFT JOIN pg_namespace n ON e.extnamespace=n.oid` → `{name, schema, version, comment}` |
| adapter | `structure["extensions"]` — верхний уровень (extensions глобальны; `SCHEMA` — атрибут, а не владелец) |
| sql_generator | рендер `extension.sql.j2` в `<output>/extensions/extension <name>.sql`, вне per-schema цикла (top-level render в `generate_scripts`) |
| autodoc | `object_schema=None` → ключ `pg_database/<db>/type/extension/name/<name>`; версия extension — в autodoc информационно |
| parser | `extension` в `SUPPORTED_TYPES`; `(("extension",), "extension")` в `_CREATE_KEYWORD_TO_TYPE` |
| toposort | `TYPE_PRIORITIES["extension"] = -2` |
| deploy | `extension` в `EARLY_DDL_TYPES` (fail-fast — без extension всё последующее бессмысленно) |

### 4.2. Database settings — два разных механизма

| Подмножество | Источник | Воспроизведение |
|--------------|----------|-----------------|
| Свойства `CREATE DATABASE` | `pg_database` (`pg_encoding_to_char(encoding)`, `datcollate`, `datctype`, `datname`-template) | параметры `adapter.create_database(...)` при deploy/validate |
| Параметры `ALTER DATABASE ... SET` | `pg_db_role_setting` (`setdatabase = oid базы`, `setrole = 0` — только уровень БД) | один SQL-файл `settings/database settings.sql` |

Свойства `CREATE DATABASE` — **не скрипт**, а метаданные, которые deploy использует
при создании временной БД. Персистентность в codebase: блок `properties` в autodoc-
заголовке файла `database settings.sql` → парсер кладёт в `vertex.extra`
(→ deploy читает из вершины). Параметры SET — тело того же файла.

Переносимые свойства (фиксированный список в коде): **encoding, lc_collate,
lc_ctype, template**. НЕ переносим: `datconnlimit`, `datistemplate`, `datallowconn`
(эксплуатационные свойства исходного сервера).

### 4.3. Новый тип вершины `database_setting`

`object_type = "database_setting"`, `object_schema = None`, приоритет −1
(после extension −2, до schema 0). Fail-fast в deploy (в `EARLY_DDL_TYPES`).

---

## 5. Решения по открытым вопросам (из draft Q1–Q8)

| # | Вопрос | Решение | Обоснование |
|---|--------|---------|-------------|
| Q1 | Охват settings | **оба** (CREATE DATABASE props + ALTER DATABASE SET) | props критичны для validate (локаль влияет на сортировки/сравнения); SET — частый источник расхождений «на тесте работало»; трудоёмкость невысока |
| Q2 | Файлы SET-параметров | **один файл** `settings/database settings.sql` | параметры семантически независимы; единая вершина проще в графе и деплое; переход на per-param файлы позже локален |
| Q3 | Версия extension | **не пиновать**, версия в autodoc информационно | пин падает при отсутствии версии пакета на целевом кластере; опция `--pin-extension-versions` — позже, с конфигом генерации |
| Q4 | Рёбра DEPENDS_ON → extension | **не делать (MVP)** | приоритет −2 уже ставит extensions первыми; детект через `pg_depend` — отдельная задача → BACKLOG P3 |
| Q5 | Role-level настройки | **нет**, только `setrole = 0` | роли вне scope инструмента; фильтр фиксируется в запросе + тестом |
| Q6 | Порядок в топосорте | **extension = −2, database_setting = −1** | явный детерминизм; SET-параметры могут ссылаться на объекты extension |
| Q7 | Integration-тест | **в scope Phase 5** | extensions без real-DB проверки = повторение Phase 4; harness testcontainers есть; маркер `integration` (LESSONS §21) |
| Q8 | Служебные поля `pg_database` | **только encoding, lc_collate, lc_ctype, template** | остальное — эксплуатационные свойства исходного сервера; список фиксируется в коде |

---

## 6. Риски

| Риск | Митигация |
|------|-----------|
| `CREATE DATABASE ... TEMPLATE` требует отсутствия коннектов к template-БД | в validate используем template только если он существует на сервере; иначе fallback на `template0` + warning |
| encoding/locale несовместимы с дефолтным template (`template1`) | при нестандартных encoding/locale использовать `TEMPLATE template0` (стандартная практика PG) |
| extension требует superuser / trusted-роль | задокументировать; ошибка fail-fast с именем extension (EARLY_DDL_TYPES) |
| `ALTER DATABASE SET` на несуществующий параметр (версии сервера различаются) | `continue_on_error` не применять к database_setting — параметры критичны |
| Greenplum-совместимость (`pg_sequence`-fallback уже есть) | запросы к `pg_extension`/`pg_db_role_setting` держать в том же стиле; GP-специфику не покрывать в Phase 5 |
| autodoc roundtrip новых типов | тесты по образцу Phase 4 (`extract_header` roundtrip, LESSONS §28) |

---

## 7. Вне scope (явно)

- Рёбра `DEPENDS_ON` → extension через `pg_depend` (см. Q4).
- Роли, гранты, `ALTER ROLE ... SET`.
- Пин версий extensions (см. Q3).
- `CREATE DATABASE` при **боевом** deploy (команды боевого deploy пока нет —
  речь про validate; применение settings к боевой целевой БД — тема Phase миграций).
- MSSQL/MySQL/Snowflake адаптеры (устаревший roadmap, BACKLOG P3).
