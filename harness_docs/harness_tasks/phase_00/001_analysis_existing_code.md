# Анализ существующего кода POC (Phase 00)

> Источник анализа: `C:\repos\reksoft\db-project-manager` (ветка `dev`, коммит `9f5dbbe`)
> Репозиторий-источник: `https://git.reksoft.com/andrey.potapov/db-project-manager.git`
> Дата анализа: 2026-07-17

---

## 1. Назначение и контекст проекта

**db-project-manager** — настольное приложение (GUI + CLI) для инженерии и сопровождения структуры PostgreSQL/Greenplum баз данных. Основные сценарии:

1. **Реверс-инжиниринг**: подключение к БД → чтение каталога (`pg_catalog` / `information_schema`) → генерация набора SQL-файлов (по одному на объект БД).
2. **Прямой инжиниринг / развёртывание**: сборка каталога SQL-файлов в единый скрипт развёртывания с учётом топологических зависимостей → применение во временную БД для проверки.
3. **Сравнение (diff)**: сопоставление каталога SQL-файлов и фактической схемы БД → формирование разностного скрипта миграции.

Авторы: Andrey Potapov, Sergey Igonin, Sergey Boykov (Reksoft).

---

## 2. Стек технологий

| Слой | Технология | Версия |
|------|-----------|--------|
| Язык | Python | `>=3.13` |
| Менеджер пакетов / сборка | `uv` + `uv_build` | `uv_build>=0.11.6,<0.12` |
| GUI | PySide6 (Qt) | `~=6.8.3` |
| Тема оформления | `darkdetect` | `~=0.8.0` |
| Драйвер БД | `psycopg2` | `~=2.9.9` |
| ORM / работа с БД | SQLAlchemy (core, raw `text()`) | `~=2.0.23` |
| Валидация моделей | `pydantic` | `~=2.11.3` |
| Шаблоны SQL | Jinja2 | `~=3.1.6` |
| Чтение CSV | `polars` | `~=1.31.0` |
| Конфиги | `pyyaml` | `~=6.0.2` |
| Логирование | `loguru` | `~=0.7.3` |
| CLI | `typer` | `~=0.24.1` |
| Шифрование паролей | `cryptography` (Fernet) | `~=44.0.2` |
| Линтеры | `ruff`, `sqlfluff`, `yamllint` | через `pre-commit` |
| Тесты | `pytest` | (в зависимостях явно не зафиксирован) |

---

## 3. Структура каталогов POC

```
db-project-manager/
├── config.yaml                      # Конфиг приложения (пути, тема, активное соединение)
├── pyproject.toml                   # Метаданные, зависимости, точки входа, настройки ruff
├── uv.lock                          # Зафиксированные версии зависимостей
├── .pre-commit-config.yaml          # ruff, sqlfluff, yamllint, защита ветки main
├── .sqlfluff                        # Конфиг SQLFluff (диалект postgres)
├── .yamllint                        # Конфиг yamllint
├── .cursorrules                     # Правила форматирования для Cursor IDE
├── connections/                     # YAML-файлы подключений (с зашифрованными паролями)
│   ├── Reksoft_Greenplum_cis.yaml
│   ├── postgres-local-cis.yaml
│   └── ...
├── docs/index.md                    # Краткая документация
├── misc/                            # Вспомогательные CSV (snowflake_db_objects*.csv)
├── samples/aviasales_medium/bookings/   # Демонстрационный набор SQL-файлов
│   ├── tables/ views/ functions/ sequences/ materialized_views/
├── src/
│   ├── cli/main.py                  # Точка входа CLI (typer): list-connections
│   ├── gui/
│   │   ├── main.py                  # Точка входа GUI: db-pm-gui
│   │   ├── main_window.py           # Главное окно (1112 строк) — вся бизнес-логика UI
│   │   └── widgets/
│   │       ├── connection_dialog.py # Диалог подключения (НЕ ИСПОЛЬЗУЕТСЯ в main_window)
│   │       └── connection_list.py   # Кастомная модель списка (НЕ ИСПОЛЬЗУЕТСЯ в main_window)
│   └── db_project_manager/          # ОСНОВНОЙ ПАКЕТ (куда идёт рефакторинг)
│       ├── common/
│       │   ├── config.py            # Простой обёртка над YAML (НЕ ИСПОЛЬЗУЕТСЯ GUI)
│       │   ├── crypto.py            # Шифрование/дешифрование паролей (Fernet)
│       │   ├── dflw_files.py        # Работа с файлами/JSON (dataflow)
│       │   ├── dflw_parser_pg_sql.py # Парсер SQL-файлов: извлечение объектов и связей
│       │   ├── dflw_topological_sort.py # Топологическая сортировка объектов деплоя
│       │   ├── exceptions.py        # Иерархия исключений (НЕ ИСПОЛЬЗУЕТСЯ)
│       │   └── logger.py            # Настройка loguru (консоль + app.log + error.log)
│       ├── connections/base.py      # ABC BaseConnection (НЕ ИСПОЛЬЗУЕТСЯ, заглушка)
│       ├── core/
│       │   ├── config.py            # ConfigManager — активный менеджер конфига/подключений
│       │   ├── database.py          # PGDatabaseModel — чтение структуры БД (1080 строк)
│       │   ├── database_service.py  # Вторая экспериментальная модель объектов (заглушки ABC)
│       │   ├── sql_generator.py     # SQLGenerator — рендер шаблонов в файлы
│       │   └── templates/*.sql.j2   # 9 Jinja2-шаблонов
│       └── utils/create_sf_db_from_csv.py  # Утилита импорта объектов из CSV (Snowflake)
└── tests/test_database.py           # Тесты (НЕ АКТУАЛЬНЫ: импортируют src.core, ожидают др. API)
```

---

## 4. Архитектура и поток данных

### 4.1. Две конкурирующие модели данных

В коде сосуществуют **две реализации одних и тех же сущностей** — признак незавершённого рефакторинга:

**Модель A (активная, `core/database.py`) — процедурная, на SQLAlchemy:**
- `ConnectionConfig` (pydantic) — параметры подключения.
- `PGDatabaseModel` — god-class, читает структуру БД набором методов `get_*`, каждый выполняет сырой SQL через `connection.execute(text(...))`. Возвращает словари (`list[dict[str, Any]]`).

**Модель B (черновая, `core/database_service.py`) — ООП, на psycopg2:**
- `DatabaseInterface`, `DatabaseModelInterface`, `DatabaseObjectInterface` (ABC).
- `PGDatabaseObject` — объект БД с концепцией `autodoc` (встроенный YAML-блок с метаданными в начале SQL-файла).
- `Schema`, `Table`, `PostgresDatabase`, `PGDBModel`.
- **Внимание:** содержит баги — `autodoc` getter возвращает `self._auto_doc` (атрибут `_auto_doc`), который нигде не инициализируется (`__init__` задаёт `_autodoc_template`); свойство `schemas` в интерфейсе объявлено дважды.
- **Не используется** ни GUI, ни CLI.

### 4.2. Поток «Реверс-инжиниринг (БД → файлы)»

```
GUI: _generate_sql()
  └─ db_model.connect(config)                    # SQLAlchemy engine, AUTOCOMMIT
  └─ DatabaseWorker(QRunnable).run()             # фоновый поток
       ├─ db_model.get_database_structure()      # обходит схемы→таблицы→колонки→...
       │     └─ get_schemas / get_tables / get_columns / get_constraints /
       │        get_pk_constraint / get_indexes / get_sequences /
       │        get_views / get_materialized_views / get_functions / get_procedures
       │     ⇒ {"schemas": [ {name, comment, sequences[], tables[], views[], ...} ]}
       └─ SQLGenerator().generate_scripts(structure, output_path)
            └─ по каждому объекту: render(template.j2) → запись "<type> <name>.sql"
            ⇒ дерево каталогов: <schema>/<type>s/<type> <name>.sql
```

Поддерживается **Greenplum**: `get_sequences` сначала пробует запрос через `pg_sequence` (PG10+), при ошибке откатывается на упрощённый запрос через `pg_class` (данные по start/increment/last_value недоступны).

### 4.3. Поток «Сборка скрипта развёртывания (файлы → единый SQL)»

```
GUI: _collect_files()
  ├─ process_pg_sql_files(config_parser)         # common/dflw_parser_pg_sql.py
  │    ├─ extract_object_from_file(path)         # токенизация SQL → тип/имя объекта
  │    │     (распознаёт CREATE [OR REPLACE] {PROCEDURE|VIEW|TABLE|FUNCTION|SCHEMA|SEQUENCE|MATERIALIZED VIEW|READABLE/WRITABLE TABLE})
  │    └─ search_edges_in_pg_sql_file(...)       # поиск связей по ключевым словам
  │          (FROM/JOIN/INSERT INTO/UPDATE/DELETE/TRUNCATE/MERGE/REFERENCES/NEXTVAL)
  │    ⇒ vertices[] (объекты) + edges[] (связи)
  ├─ save_results_as_json(...)                   # промежуточные vertices_*.json / edges_*.json
  ├─ sort_by_type_and_topology(vertices, edges)  # Кана + приоритет типов
  │     TYPE_PRIORITIES: schema(0)→sequence(1)→table(2)→index(3)→view(4)→function(5)→policy(6)
  └─ конкатенация файлов в <dir>_<timestamp>.sql с обёрткой DO $$ ... RAISE NOTICE ... $$
```

### 4.4. Поток «Сравнение каталога и БД (diff)»

```
GUI: _compare_directory_and_db()
  ├─ _generate_sql_files_from_db()              # генерит файлы из целевой БД во временный каталог
  ├─ _compare_directories(src, target)          # filecmp.cmp → статусы new/obsolete/equal/changed
  └─ _generate_difference_script(diff)
       ├─ obsolete view/function/procedure → DROP ...
       ├─ new/changed view/function/procedure   → тело из файла
       ├─ changed table                         → комментарий «требуется ручной скрипт»
       └─ changed materialized_view             → CREATE→ALTER (regex замена)
```

### 4.5. Параллельная работа с событийным циклом Qt

В GUI применяется антипаттерн: фоновый `QRunnable` запускается в `QThreadPool`, после чего **создаётся локальный `QEventLoop` и блокируется** `loop.exec_()` до сигнала `finished`. Это делает «асинхронность» синхронной — UI зависает на время операции, а смысл пула потоков теряется.

---

## 5. Поддерживаемые объекты БД

| Объект | Чтение из БД | Шаблон SQL | Распознавание в парсере |
|--------|:---:|:---:|:---:|
| Schema | ✅ | ✅ | ✅ |
| Table | ✅ | ✅ | ✅ |
| Column | ✅ | (внутри table) | — |
| Primary Key | ✅ | (внутри table) | — |
| Constraints (FK/UQ/CK) | ✅ | ✅ | — |
| Index | ✅ | ❌ (нет рендера) | — |
| Sequence | ✅ | ✅ | ✅ |
| View | ✅ | ✅ | ✅ |
| Materialized View | ✅ | ✅ | ✅ |
| Function | ✅ | ✅ | ✅ |
| Procedure | ✅ | ✅ | ✅ |
| External Table (GP) | ❌ | ❌ | ✅ (парсер) |
| Trigger | ❌ | ⚠️ шаблон есть, генератор — заглушка | ❌ |
| Extension | ❌ | ⚠️ шаблон есть, генератор — заглушка | ❌ |
| Enum/Type | ❌ (поле есть в структуре, пустое) | ❌ | ❌ |
| Policy | ❌ | ❌ | (есть в приоритетах) |

---

## 6. Конфигурация и безопасность

- **`config.yaml`** (активный, `ConfigManager`): `default_output_dir`, `file_overwrite`, `ui_theme`, `db_directory`, `db_connection`.
- **Подключения** `connections/<name>.yaml`: host/port/database/username/`password` (зашифрован).
- **Шифрование паролей**: Fernet (`cryptography`). Ключ хранится в переменной окружения `ENVOS_CRYPTO_01`. Формат значения: `crypto__ENVOS_CRYPTO_01__<base64>`.
  - ⚠️ `crypto.py` на импорте читает `os.environ[ENV_VARIABLE_NAME]` на верхнем уровне модуля — **импорт падает, если переменная не задана**, даже если шифрование не используется.
- **`.gitignore`** не исключает `connections/`, `config.yaml`, `misc/`, `samples/` — в репозитории фактически лежат файлы подключений с зашифрованными паролями к корпоративным стендам (Greenplum `10.22.112.155`).

---

## 7. Точки входа (entry points)

Из `pyproject.toml`:
- `db-pm-gui` → `gui.main:main` (PySide6 приложение).
- `db-pm` → `cli.main:app` (typer, пока только команда `list-connections`).

⚠️ Обе точки входа ссылаются на пакеты верхнего уровня (`gui`, `cli`), а не на `db_project_manager.*`. Для работы необходим `src` в `sys.path` (через `uv`/editable install).

---

## 8. Тесты

`tests/test_database.py` (75 строк) — **полностью устарели**:
- `from src.core.database import ...` — путь не существует (нет `src/core/`, пакет теперь `db_project_manager.core`).
- Ожидают API, которого нет в текущей реализации: `structure["tables"]` на верхнем уровне (актуально: `structure["schemas"][i]["tables"]`), `db_model.generate_sql_scripts(...)`, ключи `"triggers"`, `"extensions"`, `"roles"`.
- Требуют живого подключения к `localhost:5432/test_db`.

**Вывод:** покрытие тестами фактически отсутствует; `pytest` не входит в `dependencies`.

---

## 9. Качество кода — выявленные проблемы

### Архитектура
1. **Дублирование моделей** (`database.py` vs `database_service.py`, `common/config.py` vs `core/config.py`) — незавершённый рефакторинг.
2. **God-class `PGDatabaseModel`** (1080 строк) смешивает подключение, чтение, агрегацию и DDL-операции.
3. **Бизнес-логика в `MainWindow`** (1112 строк): топологическая сортировка, конкатенация файлов, diff-логика живут прямо в обработчиках кнопок.
4. **Мёртвый код:** `widgets/connection_dialog.py`, `widgets/connection_list.py`, `common/config.py`, `common/exceptions.py`, `connections/base.py` нигде не импортируются активным кодом.

### Корректность
5. **SQL-инъекция** в `create_database`: `text(f"CREATE DATABASE {db_name};")` — `db_name` формируется из `file_name + uuid`, но `file_name` берётся из пути пользователя без санитизации.
6. **Топологическая сортировка**: проверка на циклы отключена (`if len(result) != len(vertices)` закомментировано) — при циклических зависимостях часть объектов тихо теряется.
7. **Дедупликация рёбер** в `process_pg_sql_files`: `set(tuple(sorted(d.items())))` не работает корректно с разными типами значений (сравнение `str`/`None`).
8. **Шаблон `table.sql.j2`**: на чек-ограничениях и внешних ключах есть расхождения; в FK-секции используется неопределённая `{{ c.comment }}` (должно быть `f.comment`).
9. **Парсер связей** `search_edges_in_pg_sql_file`: O(n×m) перебор слов по всем объектам, порядок `elif` содержит недостижимые ветки (`words[i-1]=="join"` стоит раньше проверки `left join`), учитывается только предыдущее слово — много ложных срабатываний/пропусков.

### Безопасность
10. **Учётные данные стендов в git** (см. п. 6).
11. **Пароль передаётся в строку подключения** `_build_connection_string` в открытом виде (`postgresql://user:password@...`) — попадает в логи SQLAlchemy при ошибке.

### Эксплуатация
12. **Блокировка UI** из-за `QEventLoop().exec_()` (п. 4.5).
13. **Жёстко зашитые пути** в `utils/create_sf_db_from_csv.py` (`C:\\-=Dump=-\\Danone-KZ\\...`) и в `__main__` блоках парсеров.
14. **`setup_logger`** вызывается на импорте почти каждого модуля и пишет логи в относительный `logs/` — зависит от рабочей директории запуска.

### Стиль / консистентность
15. Смешение русской и английской документации; f-strings в логах вместо lazy `%` (loguru принимает, но производительность/безопасность аргументов страдает).
16. `pyproject.toml` декларирует `requires-python = ">=3.13"`, но `docs/index.md` указывает Python 3.10+, а README — 3.13+.

---

## 10. Сильные стороны (что стоит перенести)

- ✅ Глубокое знание предметной области: богатый набор корректных SQL-запросов к `pg_catalog` с учётом Greenplum.
- ✅ Разумная идея **autodoc** — встраивание YAML-метаданных объекта прямо в SQL-файл (реализована в `database_service.py`, но не доведена).
- ✅ Концепция **топологической сортировки с приоритетами типов** для корректного порядка деплоя.
- ✅ Разделение «объект = отдельный файл с типизированным префиксом» (`table <name>.sql`, `view <name>.sql`) — удобный для diff и версионирования формат.
- ✅ Шаблоны Jinja2 вынесены отдельно — хорошая основа для кастомизации.
- ✅ Зрелая конфигурация линтеров (ruff + sqlfluff + yamllint + pre-commit).

---

## 11. Метрики

| Метрика | Значение |
|---------|---------|
| Строк Python-кода в `src/` | ~4 173 |
| Самый большой файл | `gui/main_window.py` — 1 112 строк |
| Второй по размеру | `core/database.py` — 1 080 строк |
| Модулей Python | 19 |
| Jinja2-шаблонов | 9 |
| Тестов | 5 (все устарели / нерабочие) |
| Внешних зависимостей | 12 (runtime) |

---

## 12. Рекомендации для новой реализации

1. **Единая доменная модель:** оставить одну объектную модель БД (ООП, как в `database_service.py`), удалить процедурный дубликат `PGDatabaseModel`.
2. **Слои архитектуры:** разделить `domain` (модели), `infrastructure` (DB-доступ, файлы), `application` (сервисы: generate/collect/diff), `presentation` (GUI/CLI). Убрать бизнес-логику из `MainWindow`.
3. **Рефакторинг парсера SQL:** заменить наивный токенайзер на регулярки/AST (например, `sqlglot`) — корректность связей и производительность.
4. **Безопасность:** убрать `connections/` и `config.yaml` из git, добавить в `.gitignore`; пароли — из отдельного хранилища (ключи OS / env), не хардкодить `ENVOS_CRYPTO_01` на уровне импорта.
5. **Параметризованные запросы:** `CREATE DATABASE` через валидацию идентификатора; не вставлять пути в SQL.
6. **Асинхронность GUI:** заменить блокирующий `QEventLoop` на корректные сигналы/потоки без nested event loop.
7. **Тесты:** переписать с нуля против финального API; ввести фикстуры с testcontainers/in-memory; добавить в `pyproject` как `dev`-зависимость.
8. **Логирование:** единая точка инициализации, настраиваемый путь, lazy-форматирование.
9. **Расширяемость:** типы объектов как реестр/плагины, чтобы добавить trigger/extension/type/enum без правки god-class.
10. **Точки входа:** вынести в пакет `db_project_manager` (`db_project_manager.gui.main:main`, `db_project_manager.cli.main:app`), убрать зависимость от `sys.path` хаков.
