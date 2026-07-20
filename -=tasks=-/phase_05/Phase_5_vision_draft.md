# Phase 5 Vision — PostgreSQL extensions и настройки базы (draft)

> Дата: 2026-07-20
> Статус: DRAFT — есть незакрытые `USER_INPUT`
>
> Контекст:
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
  (`(type_priority, topo_index, object_key)`) — добавление `extension: -1` ставит
  их раньше `schema: 0`.
- `EARLY_DDL_TYPES` (deploy_service.py:39) — fail-fast типы; extension логично туда.

---

## 3. Цели и метрики приёмки

**Цель A — extensions end-to-end:**
1. `reverse-engineer` БД с `citext`/`uuid-ossp`/etc. → структура содержит
   `structure["extensions"]`, генерируются файлы `<output>/extensions/extension <name>.sql`.
2. `graph build` на такой codebase → вершины `type/extension` деплоятся первыми
   (приоритет −1, раньше `schema`).
3. `deploy validate` на временной БД с таблицей, использующей extension-тип, — зелёный.

**Цель B — настройки базы:**
4. `reverse-engineer` извлекает свойства базы (encoding, lc_collate, lc_ctype,
   template) и нестандартные `ALTER DATABASE ... SET` параметры.
5. Сгенерированный codebase воспроизводит их; `deploy validate` применяет
   свойства к временной БД.

**Метрики:**
- unit-тесты зелёные (`uv run pytest tests/unit/ -q`), новые слои покрыты по образцу
  Phase 4 (queries/structure/generator/parser/toposort).
- Integration-тест на testcontainers: БД с extension + перегруженной функцией →
  `reverse → graph → deploy validate` → обе перегрузки и extension задеплоены
  (закрывает BACKLOG P2 про integration test).

---

## 4. Проектные решения (предложение)

### 4.1. Extensions

| Слой | Изменение |
|------|-----------|
| queries.py | `GET_EXTENSIONS` — `pg_extension e LEFT JOIN pg_namespace n ON e.extnamespace=n.oid` → `{name, schema, version, comment}`; опционально `pg_available_extensions` для диагностики |
| adapter | `structure["extensions"]` — верхний уровень (extensions глобальны; `SCHEMA` — атрибут, а не владелец) |
| sql_generator | рендер `extension.sql.j2` в `<output>/extensions/extension <name>.sql`, вне per-schema цикла (top-level render в `generate_scripts`) |
| autodoc | `object_schema=None` → ключ `pg_database/<db>/type/extension/name/<name>` |
| parser | `extension` в `SUPPORTED_TYPES`; `(("extension",), "extension")` в `_CREATE_KEYWORD_TO_TYPE` |
| toposort | `TYPE_PRIORITIES["extension"] = -1` |
| deploy | `extension` в `EARLY_DDL_TYPES` (fail-fast — без extension всё последующее бессмысленно) |

Рёбра `DEPENDS_ON` от объектов к extension: **не делаем в Phase 5** (см. Q4) —
приоритет −1 уже гарантирует порядок; точечные рёбра нужны только если появятся
объекты, которые надо деплоить *между* extensions и остальным.

### 4.2. Database settings — два разных механизма

| Подмножество | Источник | Воспроизведение |
|--------------|----------|-----------------|
| Свойства `CREATE DATABASE` | `pg_database` (`pg_encoding_to_char(encoding)`, `datcollate`, `datctype`, `datistemplate`, `datconnlimit`) | параметры `adapter.create_database(...)` при deploy/validate |
| Параметры `ALTER DATABASE ... SET` | `pg_db_role_setting` (`setdatabase = oid базы`, `setrole = 0` — только уровень БД) | SQL-файл(ы) с `ALTER DATABASE <name> SET <param> = <value>` |

Свойства `CREATE DATABASE` — **не скрипт**, а метаданные `structure["database"]`,
которые deploy использует при создании целевой/временной БД. Параметры SET —
скрипт(ы) в codebase, деплоятся рано (приоритет −1, рядом с extensions).

### 4.3. Новый тип вершины `database_setting`

`object_type = "database_setting"`, `object_schema = None`. Приоритет −1
(deploy до schema, после/рядом с extension — порядок между ними не критичен,
но для детерминизма: extension −2? см. Q6).

---

## 5. Открытые вопросы

### Q1. Охват «настроек базы»

(a) только свойства `CREATE DATABASE` (encoding, collate, ctype, template);
(b) только `ALTER DATABASE ... SET` (параметры из `pg_db_role_setting`);
(c) оба.

```text
USER_INPUT:
оба — свойства CREATE DATABASE критичны для validate
(локаль влияет на сортировки/сравнения), SET-параметры — частый источник
расхождений «на тесте работало». Трудоёмкость (b) невысока: один запрос,
один шаблон, один тип вершины.
```

### Q2. Представление SET-параметров в codebase

(a) один файл `settings/database settings.sql` со всеми `ALTER DATABASE ... SET`;
(b) по файлу на параметр (`database_setting work_mem.sql` и т.д.).

(a) — компактно, порядок параметров обычно неважен; (b) — гранулярный `build`-флаг
и граф-вершины на параметр, но раздувает codebase.

```text
USER_INPUT:
(a) один файл — параметры SET семантически независимы,
единая вершина `database_setting` проще в графе и деплое. Если позже понадобится
гранулярность — переход на (b) локален (генератор + парсер).
```

### Q3. Версия extension в рендере

(a) пиновать `VERSION '<версия>'` как на исходной БД;
(b) не пиновать — `CREATE EXTENSION IF NOT EXISTS <name>` (дефолтная версия кластера).

Пин гарантирует воспроизводимость, но падает, если на целевом кластере нет именно
этой версии пакета. Без пина — молчаливое расхождение версий.

```text
USER_INPUT:
(b) не пиновать по умолчанию, версию хранить в autodoc
(информационно). Пин — опция позже (`--pin-extension-versions`), когда появится
конфиг генерации. Для validate на том же сервере дефолтная версия совпадёт.
```

### Q4. Рёбра DEPENDS_ON от объектов к extension

(a) MVP: без рёбер, порядок обеспечен `TYPE_PRIORITIES["extension"] = -1`;
(b) полноценно: детект через `pg_depend`/`pg_type` (atttypid → тип → extension).

```text
USER_INPUT:
(a) MVP без рёбер — приоритет −1 ставит extensions раньше всех
типов, рёбра ничего не меняют в порядке деплоя. Детект через pg_depend — отдельная
задача (запросы сложные, выигрыша в порядке нет) — в BACKLOG P3.
```

### Q5. Role-level настройки (`pg_db_role_setting.setrole != 0`)

Настройки «база + роль» (например `ALTER DATABASE x SET ... ` для конкретной роли)
таскать или только db-level (`setrole = 0`)?

```text
USER_INPUT:
 только db-level (setrole = 0) — роли вне scope инструмента
(нет reverse ролей/грантов), перенос role-specific настроек без переноса ролей
бессмысленен и опасен. Фильтр фиксировать в запросе + тестом.
```

### Q6. Порядок extension vs database_setting в топосорте

(a) оба −1 (порядок между ними — по topo_index/object_key, семантически безразличен);
(b) extension = −2, database_setting = −1 (явный детерминированный порядок:
сначала расширения, потом параметры).

```text
USER_INPUT:
(b) extension −2, database_setting −1 — детерминизм без
зависимости от имён файлов; SET-параметры могут ссылаться на объекты extension
(редко, но возможно, напр. параметры с типами).
```

### Q7. Integration-тест в scope Phase 5?

BACKLOG P2: testcontainers-прогон reverse → graph → deploy validate на реальной PG
с extension + перегрузками. Делать в Phase 5 или отдельно?

```text
USER_INPUT:
в Phase 5 — extensions без real-DB проверки = повторение
ситуации Phase 4 (синтетические фикстуры). Testcontainers harness есть с Phase 2;
маркер `integration`, в дефолтный прогон не входит (LESSONS §21).
```

### Q8. Шумные/дефолтные параметры

`pg_db_role_setting` содержит только явно установленные значения (не дефолты
кластера) — шума нет. Но свойства `pg_database` (encoding и т.д.) — всегда.
Переносить ли `datconnlimit`, `datistemplate` и служебные поля?

```text
USER_INPUT:
 переносить: encoding, lc_collate, lc_ctype, template
(4 поля, влияют на поведение). НЕ переносить: datconnlimit, datistemplate,
datallowconn — это эксплуатационные свойства исходного сервера, на временной/целевой
БД они неуместны. Список фиксировать в коде + документировать в плане.
```

---

## 6. Риски

| Риск | Митигация |
|------|-----------|
| `CREATE DATABASE ... TEMPLATE` требует отсутствия коннектов к template-БД | в validate используем template только если он существует на сервере; иначе fallback на `template0` + warning |
| extension требует superuser / trusted-роль | задокументировать; ошибка fail-fast с именем extension (EARLY_DDL_TYPES) |
| `ALTER DATABASE SET` на несуществующий параметр (версии сервера различаются) | `continue_on_error` не применять к database_setting — параметры критичны; ошибка говорит сама за себя |
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
