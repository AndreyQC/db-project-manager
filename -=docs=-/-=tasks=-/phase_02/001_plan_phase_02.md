# План Phase 2 — Граф зависимостей + Validation Deploy

> Контекст:
> - `-=tasks=-/phase_02/Phase_2_vision_final.md` — зафиксированное видение (все решения Q1–Q8)
> - `-=tasks=-/phase_00/003_roadmap_migration.md` — целевая архитектура
> - `LESSONS_LEARNED.md` — уроки Phase 1
>
> Дата: 2026-07-18

---

## 0. Цель фазы

Реализовать два сценария из зафиксированного видения:

1. **Граф зависимостей** — построение графа объектов БД по кодовой базе (чтение autodoc-блоков + извлечение зависимостей из SQL), хранение в `.dbm_graph/`, экспорт в json/graphml/dot.
2. **Validation Deploy** — развёртывание кодовой базы в пустую временную БД (имя с серверным timestamp) с проверкой прав `CREATEDB`, топосортировкой, фильтром `project.build`, стратифицированной стратегией ошибок.

**Критерий «фаза готова»:** `db-pm graph build` + `db-pm deploy validate` работают на каталоге SQL-файлов через CLI; интеграционные тесты (testcontainers PG) зелёные.

---

## 1. Зафиксированные решения (из `Phase_2_vision_final.md`)

| # | Решение |
|---|---------|
| Q1 | Перенос POC-парсера как MVP + `sqlglot` позже; **generic-интерфейс** парсера (переиспользование для Informatica/Airflow) |
| Q2 | `.dbm_graph/` игнорировать целиком в git |
| Q3 | Cleanup по умолчанию; CLI `--keep-db`, GUI чекбокс (off по умолчанию) |
| Q4 | Пообъектно; ранние DDL (schema/seq/table) fail-fast+cleanup, поздние (views/proc) пообъектный лог с `--continue-on-error` |
| Q5 | Префикс = имя каталога кодовой базы (sanitized `[a-z0-9_]`), `--prefix` |
| Q6 | Только PostgreSQL в Phase 2 |
| Q7 | Только экспорт графа (GUI-просмотр не делаем — есть Tauri-приложение) |
| Q8 | `build:false` объекты в граф попадают, при deploy validate пропускаются |

---

## 2. Целевая структура (новые/изменяемые файлы в Phase 2)

```
src/db_project_manager/
├── domain/
│   ├── graph.py                          # НОВОЕ: Vertex, Edge, Relation, DependencyGraph
│   └── connection.py                     # (без изменений)
├── infrastructure/
│   ├── sql/
│   │   ├── autodoc.py                    # (без изменений — extract_header переиспользуем)
│   │   └── sql_generator.py              # (без изменений)
│   ├── parsing/                          # НОВЫЙ ПАКЕТ
│   │   ├── __init__.py
│   │   ├── base.py                       # ObjectGraphParser (generic ABC) — Q1
│   │   ├── pg_sql_parser.py              # перенос dflw_parser_pg_sql с фиксажами
│   │   └── normalize.py                  # get_normalized_file_content, get_object_name
│   ├── graph/
│   │   ├── __init__.py                   # НОВЫЙ ПАКЕТ
│   │   ├── topological_sort.py           # перенос + проверки циклов + детерминизм
│   │   ├── graph_store.py                # чтение/запись .dbm_graph/*.json + meta + хэш
│   │   └── export.py                     # export json/graphml/dot
│   ├── database/
│   │   ├── base.py                       # ИЗМЕНИТЬ: +execute_script, +create_database, +server_timestamp, +check_createdb
│   │   ├── postgres/adapter.py           # ИЗМЕНИТЬ: реализация новых методов контракта
│   │   └── postgres/queries.py           # +GET_CREATEDB_CHECK, +GET_SERVER_TIMESTAMP_UTC
│   └── ...
├── application/
│   ├── reverse_engineer.py               # (без изменений)
│   ├── graph_service.py                  # НОВОЕ: BuildGraphService (dir → graph → store)
│   └── deploy_service.py                 # НОВОЕ: DeployValidateService (Scenario A)
└── presentation/
    ├── cli/main.py                       # ИЗМЕНИТЬ: +graph subapp, +deploy subapp
    └── gui/...                           # ИЗМЕНИТЬ: +deploy action в MainWindow (опц., см. S15)

tests/
├── unit/
│   ├── test_graph_model.py               # Vertex/Edge/DependencyGraph
│   ├── test_pg_sql_parser.py             # на fixtures/codebase_sample/
│   ├── test_topological_sort.py          # циклы, детерминизм, приоритеты
│   ├── test_graph_store.py               # roundtrip JSON, хэш, build:false preserved
│   └── test_export.py                    # json/graphml/dot выход
├── integration/                          # НОВЫЙ пакет
│   ├── __init__.py
│   ├── conftest.py                       # testcontainers PG fixture
│   ├── test_deploy_validate_e2e.py       # полный сценарий A
│   └── test_graph_build_e2e.py           # graph build на реверс-инжиниринге
└── fixtures/
    └── codebase_sample/                  # синтетический каталог SQL с autodoc для тестов парсера
        ├── bookings/
        │   ├── tables/
        │   │   ├── table aircrafts.sql
        │   │   ├── table flights.sql     # FK к aircrafts (REFERENCES_BY)
        │   │   └── table tickets.sql     # seq nextval (SEQUENCE_NEXTVAL_IN)
        │   ├── sequences/
        │   │   └── sequence tickets_id_seq.sql
        │   └── views/
        │       └── view flights_v.sql    # SELECT из flights (PROVIDE_DATA_TO)
        └── .dbm_graph/                   # генерируется тестом, не коммитится

.gitignore                                # +.dbm_graph/
pyproject.toml                            # +testcontainers в dev dependencies
```

> `samples/` не перенесён из POC (его нет в этом репо). Вместо этого создаём компактный `tests/fixtures/codebase_sample/` с предсказуемыми зависимостями — на нём пишутся и читаются unit-тесты парсера/сортировки.

---

## 3. Порядок выполнения (S10–S16)

```
S10  Доменная модель графа
      └─► S11  Парсер (generic interface + pg_sql + normalize)
             └─► S12  Топосортировка (циклы + детерминизм + фильтр build)
                    ├─► S13  Graph store (.dbm_graph/) + export
                    └─► S14  Deploy validate (Scenario A: права/timestamp/стратификация ошибок)
                           └─► S15  CLI (graph + deploy subapps) + GUI deploy action
                                  └─► S16  Интеграционные тесты + roadmap/README
```

---

## S10. Доменная модель графа

**Файлы:** `domain/graph.py`, `tests/unit/test_graph_model.py`.

**Содержание `domain/graph.py`:**
- `Relation` — enum/string-constrained: `REFERENCES_BY`, `PROVIDE_DATA_TO`, `CHANGE_DATA_IN`, `SEQUENCE_NEXTVAL_IN`, `DEPENDS_ON`.
- `Vertex` (pydantic BaseModel):
  - `object_key: str` (первичный ключ, из autodoc);
  - `object_catalog, object_schema, object_type, object_name: str`;
  - `object_source_file: str` (путь к файлу);
  - `build: bool = True` (из autodoc `project.build`);
  - `extra: dict` (сырые поля autodoc, для будущих нужд).
- `Edge` (pydantic BaseModel):
  - `source_object_key, destination_object_key: str`;
  - `relation: Relation`;
  - `action: str` (select/insert/update/...).
- `DependencyGraph` (dataclass/pydantic):
  - `vertices: dict[str, Vertex]` (по object_key);
  - `edges: list[Edge]`;
  - методы: `add_vertex`, `add_edge`, `get_dependencies(key)`, `get_dependents(key)`, `filter_build_true() -> DependencyGraph`.
- Валидация: Edge ссылается на существующий Vertex (опц. — висячие ссылки ловит `graph validate`, см. S13/S15).

**Тесты `test_graph_model.py`:**
- конструирование Vertex/Edge, сериализация pydantic roundtrip;
- `add_vertex/add_edge`, lookup по key;
- `get_dependencies`/`get_dependents` корректны на 3-вершинном графе;
- `filter_build_true()` оставляет только `build=True`, но не роняет рёбра между ними.

**Чек-лист:**
- [ ] `domain/graph.py` с pydantic-моделями.
- [ ] unit-тесты на чистой модели (без файлового I/O).
- [ ] ruff чист.

---

## S11. Парсер SQL-файлов (generic interface + перенос POC)

**Файлы:** `infrastructure/parsing/base.py`, `infrastructure/parsing/pg_sql_parser.py`, `infrastructure/parsing/normalize.py`, `tests/unit/test_pg_sql_parser.py`, `tests/fixtures/codebase_sample/**`.

**`base.py` — generic-контракт (Q1):**
```python
class ObjectGraphParser(ABC):
    """Парсер каталога файлов проекта → DependencyGraph.

    Generic: не завязан на SQL. Concrete-парсеры (PgSqlParser, будущие
    InformaticaParser, AirflowDagParser) реализуют этот контракт.
    """
    @abstractmethod
    def parse_directory(self, root: Path) -> DependencyGraph: ...

    @abstractmethod
    def supported_object_types(self) -> tuple[str, ...]: ...
```

**`normalize.py`** — перенос вспомогательных функций из POC `dflw_parser_pg_sql.py`:
- `get_normalized_file_content(content)` — замена скобок/табов/переводов, lowercase, удаление `IF NOT EXISTS`/`OR REPLACE`/`EXTERNAL`.
- `get_object_name(qualified)` — разбор `schema.name` → `{schema, name, full_name}`.

**`pg_sql_parser.py`** — перенос `extract_object_from_file` + `search_edges_in_pg_sql_file` + `process_pg_sql_files` с фиксажами:
- **Фикс бага дедупликации рёбер:** заменить `set(tuple(sorted(d.items())))` на хешируемый ключ `(source, destination, relation, action)`.
- **Фикс недостижимых `elif`:** `left join` и `full outer join` должны проверяться **до** общего `join` (в POC общий `join` стоял раньше и поглощал все).
- **Чтение autodoc:** вместо парсинга по позиции токенов — использовать `autodoc.extract_header(script)` для получения object_key/type/name. Если autodoc отсутствует — fallback на токенизацию (как в POC).
- **Фильтр по build:** `build` берётся из autodoc `project.build` (по умолчанию `True`).
- Логирование через loguru, без `sys.path` хаков POC.

**Fixture `tests/fixtures/codebase_sample/`** — компактный, с предсказуемыми рёбрами:
- `bookings/tables/table aircrafts.sql` (PK, без зависимостей);
- `bookings/tables/table flights.sql` (FK → aircrafts, nextval seq);
- `bookings/tables/table tickets.sql` (FK → flights);
- `bookings/sequences/sequence tickets_id_seq.sql`;
- `bookings/views/view flights_v.sql` (SELECT из flights, JOIN aircrafts);
- один объект с `build: false` (например, matview) — для проверки Q8.
- Каждый файл с autodoc-заголовком (можно сгенерировать через SQLGenerator или написать вручную).

**Тесты `test_pg_sql_parser.py`:**
- `parse_directory` на fixture → ожидаемое число vertices и конкретные рёбра;
- конкретные зависимости: flights → aircrafts (`REFERENCES_BY`), flights_v → flights (`PROVIDE_DATA_TO`), tickets → tickets_id_seq (`SEQUENCE_NEXTVAL_IN`);
- объект с `build: false` включён в граф (Q8);
- дедупликация рёбер: одна и та же связь не дублируется;
- файл без autodoc → fallback-парсинг работает.

**Чек-лист:**
- [ ] generic `ObjectGraphParser` ABC.
- [ ] перенос `normalize.py` + `pg_sql_parser.py` с фиксажами.
- [ ] fixture `codebase_sample/` (6–8 файлов с autodoc).
- [ ] unit-тесты: ожидаемые vertices/edges, Q8, дедупликация, fallback.
- [ ] ruff чист.

---

## S12. Топосортировка (циклы + детерминизм + фильтр build)

**Файлы:** `infrastructure/graph/topological_sort.py`, `tests/unit/test_topological_sort.py`.

**Перенос + фиксы из POC `dflw_topological_sort.py`:**
- `TYPE_PRIORITIES` — расширить (external_table уже есть, добавить type/enum/policy/… при необходимости).
- `topological_sort(vertices, edges) -> list[Vertex]`:
  - **Вернуть проверку циклов** (в POC закомментировано): если `len(result) != len(vertices)` → поднять `CycleError` со списком вершин, оставшихся вне порядка (это и есть цикл или его компонента).
  - **Детерминизм:** соседи добавляются в очередь в отсортированном порядке (по object_key) — устойчивый результат между запусками.
- `sort_by_type_and_topology(vertices, edges) -> list[Vertex]` — Кана/топосорт + сортировка по `(type_priority, order)`.
- `filter_build_true(graph) -> graph` — использует метод из `DependencyGraph` (Q8: граф не фильтруется, фильтр применяется только при деплое).

**Тесты `test_topological_sort.py`:**
- линейный граф → корректный порядок;
- ромб (A→B,A→C,B→D,C→D) → D после B и C;
- **цикл (A→B→A)** → `CycleError` со списком [A, B];
- детерминизм: два прогона дают идентичный порядок;
- `sort_by_type_and_topology`: schema(0) → sequence(1) → table(2) → view(4) → function(5);
- `build:false` вершины участвуют в сортировке (не выбрасываются на этом шаге).

**Чек-лист:**
- [ ] топосорт с проверкой циклов (`CycleError`).
- [ ] детерминированный порядок соседей.
- [ ] сортировка по типам (приоритеты POC).
- [ ] unit-тесты: линия, ромб, цикл, детерминизм, приоритеты.
- [ ] ruff чист.

---

## S13. Graph store (.dbm_graph/) + export

**Файлы:** `infrastructure/graph/graph_store.py`, `infrastructure/graph/export.py`, `tests/unit/test_graph_store.py`, `tests/unit/test_export.py`, `.gitignore` (+`.dbm_graph/`).

**`graph_store.py`:**
- `write_graph(graph: DependencyGraph, root: Path) -> Path` — пишет в `root/.dbm_graph/`:
  - `vertices.json` — `list[dict]` (pydantic `.model_dump()`);
  - `edges.json`;
  - `graph.json` — `{vertices, edges}` одним файлом для portability;
  - `meta.json` — `{format_version: "1", created_at, codebase_hash, vertex_count, edge_count}`.
- `read_graph(root: Path) -> DependencyGraph` — обратное чтение + валидация `format_version`.
- `codebase_hash(root: Path) -> str` — хэш всех `.sql` файлов в каталоге (для detect stale graph).
- `is_stale(root: Path) -> bool` — сравнение хэша в `meta.json` с текущим хэшем.

**`export.py`:**
- `export_json(graph, output)`;
- `export_graphml(graph, output)` — стандартный XML-формат (Gephi, yEd, Tauri-приложение);
- `export_dot(graph, output)` — Graphviz DOT.
- Опц. `export_format(graph, fmt, output)` диспетчер.

**`.gitignore`:** добавить `/.dbm_graph/` (Q2). Поскольку каталог может быть в любом codebase-dir, правило вида `**/.dbm_graph/` (игнорировать везде).

**Тесты `test_graph_store.py`:**
- roundtrip: `write_graph` → `read_graph` → равенство исходному графу;
- `meta.json` содержит корректный `codebase_hash`;
- `is_stale` корректно меняется после изменения SQL-файла;
- `format_version` проверяется при чтении (несовпадение → ошибка).

**Тесты `test_export.py`:**
- json: parsable, содержит vertices и edges;
- graphml: валидный XML с ns `http://graphml.graphdrawing.org/xmlns`;
- dot: содержит `digraph` и строки `A -> B`.

**Чек-лист:**
- [ ] `write_graph`/`read_graph` roundtrip.
- [ ] `meta.json` с `format_version`, `codebase_hash`.
- [ ] `is_stale`.
- [ ] export json/graphml/dot.
- [ ] `.gitignore` обновлён (`**/.dbm_graph/`).
- [ ] unit-тесты.
- [ ] ruff чист.

---

## S14. Deploy validate (Scenario A)

**Файлы:** `infrastructure/database/base.py` (расширить контракт), `infrastructure/database/postgres/queries.py` (+2 запроса), `infrastructure/database/postgres/adapter.py` (реализация), `application/deploy_service.py`, `tests/unit/test_deploy_service.py` (мок-адаптер).

**Расширение контракта `DatabaseAdapter` (S14a):**
```python
class DatabaseAdapter(ABC):
    # ... существующее ...

    @abstractmethod
    def check_can_create_db(self) -> bool: ...

    @abstractmethod
    def get_server_timestamp_utc(self) -> str: ...

    @abstractmethod
    def create_database(self, name: str) -> None: ...

    @abstractmethod
    def drop_database(self, name: str) -> None: ...

    @abstractmethod
    def execute_script(self, script: str) -> None: ...
```

**`postgres/queries.py`:**
- `GET_CREATEDB_CHECK` — `SELECT rolcreatedb FROM pg_roles WHERE rolname = current_user;`.
- `GET_SERVER_TIMESTAMP_UTC` — `SELECT to_char(now() AT TIME ZONE 'UTC', 'YYYYMMDD"T"HH24MISS');`.

**`postgres/adapter.py`:**
- `check_can_create_db` — выполняет запрос, возвращает bool.
- `get_server_timestamp_utc` — строка `YYYYMMDDTHHMMSS`.
- `create_database` — **с валидацией имени** (whitelist `[A-Za-z0-9_]`, урок `atttypod` — никаких f-string с пользовательским вводом в SQL). Создание через подключение к серверу (не к конкретной БД).
- `drop_database` — аналогично, с валидацией имени.
- `execute_script` — выполняет скрипт объекта в текущем подключении; транзакция управляется вызывающим (deploy_service).

**`application/deploy_service.py` — `DeployValidateService`:**
- `run(conn_cfg, codebase_dir, *, prefix=None, keep_db=False, continue_on_error=False) -> DeployResult`.
- Шаги:
  1. Подключиться к серверу (maintenance connection, не к конкретной БД).
  2. `check_can_create_db()` → если False, поднять `PermissionError` с осмысленным сообщением (Q: см. vision §1.3).
  3. `prefix` = sanitize(имя каталога codebase) или `--prefix`. Sanitizer: lowercase, только `[a-z0-9_]`.
  4. `timestamp = get_server_timestamp_utc()`; `db_name = f"{prefix}_{timestamp}"`. При коллизии (БД существует) — повторить с suffix-счётчиком.
  5. `create_database(db_name)`; переподключиться к новой БД.
  6. Построить граф (`graph_service.build`), отфильтровать `build=True` (Q8), топосортировать.
  7. Выполнение по стратам (Q4):
     - **Ранние DDL** (schema/sequence/table/index/constraint): при ошибке → fail-fast, cleanup, поднятие `DeployError`.
     - **Поздние объекты** (view/materialized_view/function/procedure/trigger): по умолчанию fail-fast; с `--continue-on-error` — собирать ошибки в список, продолжать.
  8. Сборка результата `DeployResult {success, db_name, errors: list[ObjectError], objects_done, objects_total}`.
  9. `finally`: если не `keep_db` → `drop_database(db_name)` (даже при ошибке — cleanup).
- Логирование каждого объекта: «деплой объекта N/M: <key> (<file>)».

**Тесты `test_deploy_service.py` (с FakeAdapter):**
- успешный деплой: все объекты, БД создана и удалена, `success=True`;
- нет прав CREATEDB → `PermissionError` до создания БД;
- ошибка в раннем DDL → fail-fast, cleanup (БД удалена), `success=False`;
- ошибка в позднем объекте с `--continue-on-error` → в отчёте, но `success=False`, остальные поздние выполнены;
- `keep_db=True` → БД не удаляется;
- фильтр `build:false` → объект пропущен.

**Чек-лист:**
- [ ] расширение `DatabaseAdapter` контракта (+5 методов).
- [ ] запросы CREATEDB и server-timestamp.
- [ ] валидация имени БД (`[A-Za-z0-9_]`).
- [ ] `DeployValidateService` с стратификацией ошибок и cleanup.
- [ ] unit-тесты с FakeAdapter.
- [ ] ruff чист.

---

## S15. CLI subapps + GUI deploy action

**Файлы:** `presentation/cli/main.py` (расширить), `presentation/gui/main_window.py` (+кнопка/диалог deploy), `presentation/gui/widgets/workers.py` (+DeployValidateWorker).

**CLI — добавляем подгруппы (Q — typer pattern):**
```
db-pm deploy validate \
    --dir <codebase-dir> \
    --connection-file <conn.yaml> \
    [--prefix <name>] [--keep-db] [--continue-on-error]

db-pm graph build   --dir <codebase-dir>
db-pm graph export  --dir <codebase-dir> --format json|graphml|dot [--output <file>]
db-pm graph show    --dir <codebase-dir> --object <object_key>
db-pm graph validate --dir <codebase-dir>
```
- Использовать `typer.Typer()` подгруппы: `graph_app = typer.Typer(); app.add_typer(graph_app, name="graph")`. Аналогично `deploy`.
- Цветной вывод результата: зелёный успех / красная ошибка, exit codes (0/1/2).

**GUI — deploy action в MainWindow:**
- Новая кнопка «Deploy validate…» → диалог выбора codebase-dir + подключения + чекбокс «оставить БД» (Q3).
- `DeployValidateWorker(QRunnable)` — как ReverseEngineerWorker, без блокировки UI, с прогрессом и статусом.
- Результат показывается в статус-логе + `QMessageBox` (успех/ошибки).

**Чек-лист:**
- [ ] CLI `graph` subapp (build/export/show/validate).
- [ ] CLI `deploy validate` subapp.
- [ ] GUI кнопка + диалог deploy + worker.
- [ ] smoke-тест CLI `--help` для всех подкоманд.
- [ ] ruff чист.

---

## S16. Интеграционные тесты + документация

**Файлы:** `pyproject.toml` (+testcontainers в dev), `tests/integration/conftest.py`, `tests/integration/test_deploy_validate_e2e.py`, `tests/integration/test_graph_build_e2e.py`, `README.md`, `-=tasks=-/phase_00/003_roadmap_migration.md` (обновить).

**`tests/integration/conftest.py`:**
- pytest fixture `pg_container` — `PostgreSqlContainer("postgres:16")` (testcontainers).
- fixture `pg_conn_cfg` — `ConnectionConfig` из контейнера (с CREATEDB пользователем).
- fixture `populated_codebase` — генерирует временный каталог SQL из фикстуры, добавляет autodoc.

**`test_deploy_validate_e2e.py`:**
- полный сценарий A: build graph → deploy validate на testcontainer → проверка, что таблицы созданы (`SELECT count(*) FROM information_schema.tables WHERE table_schema='bookings'`);
- без прав CREATEDB (ограниченный пользователь в контейнере) → `PermissionError`;
- `--keep-db` → БД остаётся (проверка через `pg_database`);
- объект `build:false` пропущен.

**`test_graph_build_e2e.py`:**
- `graph build` на codebase, сгенерированном через `reverse-engineer` из testcontainer PG → проверка ожидаемого числа vertices/edges.

**Документация:**
- `README.md`: разделы Phase 2 (graph build/export, deploy validate), обновление статуса.
- `003_roadmap_migration.md`: отметить Phase 2 завершённой, Phase 3 (миграции) как следующую.
- `LESSONS_LEARNED.md`: добавить уроки Phase 2 (по результатам).

**Чек-лист:**
- [ ] testcontainers в dev-dependencies, `uv sync`.
- [ ] integration fixtures (PG container).
- [ ] e2e тесты: deploy validate успех, нет прав, keep-db, build:false.
- [ ] e2e тест: graph build на реверс-инжиниринге.
- [ ] README + roadmap обновлены.

---

## 4. Метрики приёмки Phase 2 (контрольный список)

- [ ] `db-pm graph build` на `tests/fixtures/codebase_sample` строит ожидаемый граф (vertices/edges) и пишет в `.dbm_graph/`.
- [ ] `db-pm graph validate` находит циклы (синтетический тест) и висячие ссылки.
- [ ] `db-pm graph export --format graphml` создаёт валидный GraphML.
- [ ] `db-pm deploy validate` разворачивает образец на testcontainer PG; успех = exit 0.
- [ ] Без прав CREATEDB — осмысленная ошибка до попытки создания БД.
- [ ] `build:false` пропускается при деплое, присутствует в графе.
- [ ] Стратификация ошибок: упавшая schema → cleanup; упавшая proc с `--continue-on-error` → отчёт продолжается.
- [ ] `.dbm_graph/` в `.gitignore`.
- [ ] Все unit-тесты зелёные (`uv run pytest tests/unit`).
- [ ] Интеграционные тесты зелёные (`uv run pytest tests/integration`).
- [ ] ruff чист.
- [ ] README и roadmap обновлены.

---

## 5. Риски и смягчения

| Риск | Смягчение |
|------|-----------|
| Перенос парсера тянет баги POC | Fixture `codebase_sample/` с предсказуемыми рёбрами + тесты; задокументировать известные ограничения |
| Generic-интерфейс переусложняется | Минимальный контракт (`parse_directory` + `supported_object_types`); расширять по мере Informatica/Airflow |
| testcontainers нестабилен/медленный на CI | Вынести integration-тесты в отдельную метку (`-m integration`), не запускать на каждом локальном `pytest` |
| Топосорт теряет объекты при циклах | `CycleError` с явным списком (S12) |
| Создание БД падает в середине → мусор | Cleanup в `finally` + `--keep-db` (S14) |
| SQL-инъекция в `CREATE DATABASE` | Whitelist `[A-Za-z0-9_]` на имени БД (S14, урок `create_database` из Phase 1 analysis) |
| testcontainers + Docker на Windows | Документировать требование Docker Desktop в PREPAREENV; дать опцию skip-integration |

---

## 6. Порядок коммитов (рекомендуемый)

1. `feat(graph): domain model — Vertex, Edge, DependencyGraph (S10)`
2. `feat(parsing): generic ObjectGraphParser + PG SQL parser port (S11)`
3. `feat(graph): topological sort with cycle detection + determinism (S12)`
4. `feat(graph): .dbm_graph store + json/graphml/dot export (S13)`
5. `feat(db): extend adapter contract — createdb/timestamp/execute/drop (S14a)`
6. `feat(app): DeployValidateService — validation deploy with stratified errors (S14b)`
7. `feat(cli): graph + deploy subapps (S15)`
8. `feat(gui): deploy validate action + worker (S15)`
9. `test(integration): testcontainers PG e2e for graph build + deploy (S16)`
10. `docs: Phase 2 — README + roadmap + lessons (S16)`
