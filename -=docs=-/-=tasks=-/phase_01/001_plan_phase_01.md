# План Фазы 1 — Фундамент (MVP PG + CLI + базовый GUI)

> Контекст:
> - `-=tasks=-/phase_00/001_analysis_existing_code.md` — анализ POC
> - `-=tasks=-/phase_00/002_idea.md` — видение
> - `-=tasks=-/phase_00/003_roadmap_migration.md` — целевая архитектура и фазы
>
> Дата: 2026-07-17

---

## 0. Цель фазы

Получить **первый работающий вертикальный срез**:
1. CLI `db-pm reverse-engineer --connection-file conn.yaml --output dir/` → генерирует дерево SQL-файлов из схемы PostgreSQL.
2. GUI на PySide6: управление подключениями (единственное место их создания/редактирования) + запуск reverse-engineer + read-only панель «дерево проекта + просмотр SQL» (QScintilla).
3. Безопасная основа: crypto (ленивая, из `shared_pckg`), pydantic-конфиг, секреты вне git.
4. CLI и GUI работают на одном `application`-слое.

**Критерий «фаза готова»:** из коробки запускаем GUI, добавляем подключение к тестовой PG, генерируем файлы, видим их в дереве с подсветкой SQL; тот же reverse-engineer запускается из CLI с `--connection-file`.

---

## 1. Стартовое состояние целевого репозитория (ВАЖНО)

В репозитории уже лежит **старая заготовка**, не соответствующая roadmap:
- `config/config.py` — старый конфиг-класс на `ruamel.yaml`, с хардкодом Greenplum/Clickhouse и чтением env по именам-константам.
- `config/config.yaml` — модель со встроенными подключениями (greenplum/clickhouse), путями `C:\Temp\...`.
- `src/main.py` — точки входа через `sys.path` хаки и `cfg.Config`.
- `requirements.txt` — «мусорный» дамп (jupyter, opencv, vosk, speechkit, moviepy…), не связанный с проектом.
- `PREPAREENV.md` — инструкция на основе старого `venv` + `requirements.txt`.
- `LICENSE`, `.gitignore` (стандартный Python) — **оставляем**.

**Решение по стартовым артефактам:**
| Файл | Действие |
|------|----------|
| `config/config.py`, `config/config.yaml` | ❌ Удалить — заменяем на pydantic-конфиг в `db_project_manager/infrastructure/config/` |
| `src/main.py` | ❌ Удалить — точки входа переезжают в `db_project_manager/presentation/` |
| `requirements.txt` | ❌ Удалить — переходим на `pyproject.toml` + `uv` |
| `PREPAREENV.md` | ♻️ Переписать под `uv` и новую структуру |
| `LICENSE`, `.gitignore` | ✅ Оставить |
| `src/`, `config/` каталоги | ❌ Удалить (пустые станут частью новой структуры `src/db_project_manager/`) |

Эти удаления — **первый шаг** Фазы 1 (шаг S0 ниже), до написания нового кода.

---

## 2. Целевая структура пакета (что создаём в Фазе 1)

```
db-project-manager/
├── pyproject.toml                        # uv_build, зависимости, ruff, entry points
├── uv.lock                               # генерируется uv sync
├── .gitignore                            # дополнить (см. §3)
├── .pre-commit-config.yaml               # ruff + ruff-format (минимум для старта)
├── PREPAREENV.md                         # переписать под uv
├── README.md                             # краткий quickstart
├── config.example.yaml                   # шаблон конфига приложения (в git)
├── connections/
│   └── example.yaml                      # шаблон файла подключения (в git)
└── src/
    └── db_project_manager/
        ├── __init__.py
        ├── domain/
        │   ├── __init__.py
        │   ├── connection.py             # ConnectionConfig (pydantic)
        │   ├── objects.py                # DbObject, ObjectType, Schema/Table/View/... (минимум для reverse-engineer)
        │   └── project.py                # DbProject — дерево объектов (заглушка, наполнение в Фазе 2)
        ├── infrastructure/
        │   ├── __init__.py
        │   ├── crypto/
        │   │   ├── __init__.py
        │   │   └── crypto_util.py        # ← перенос из shared_pckg (§4)
        │   ├── config/
        │   │   ├── __init__.py
        │   │   ├── app_config.py         # CFG (pydantic) + load_cfg (§5)
        │   │   └── connection_store.py   # чтение/запись connections/*.yaml + расшифровка
        │   ├── database/
        │   │   ├── __init__.py
        │   │   ├── base.py               # ABC DatabaseAdapter (контракт)
        │   │   └── postgres/
        │   │       ├── __init__.py
        │   │       ├── adapter.py        # PGDatabaseAdapter — перенос запросов из database.py
        │   │       └── queries.py        # тексты SQL-запросов к pg_catalog
        │   ├── sql/
        │   │   ├── __init__.py
        │   │   └── sql_generator.py      # перенос SQLGenerator + рендер шаблонов (с фиксажами)
        │   ├── files/
        │   │   ├── __init__.py
        │   │   └── project_io.py         # запись дерева <schema>/<type>s/<type> <name>.sql
        │   ├── templates/
        │   │   ├── table.sql.j2          # перенесены 9 шаблонов (§6 roadmap)
        │   │   ├── view.sql.j2
        │   │   ├── materialized_view.sql.j2
        │   │   ├── sequence.sql.j2
        │   │   ├── schema.sql.j2
        │   │   ├── function.sql.j2
        │   │   ├── procedure.sql.j2
        │   │   ├── trigger.sql.j2        # заглушки из POC
        │   │   └── extension.sql.j2
        │   └── logging_setup.py          # единая настройка loguru
        ├── application/
        │   ├── __init__.py
        │   └── reverse_engineer.py       # ReverseEngineerService: БД → файлы (вызывает adapter + generator)
        └── presentation/
            ├── __init__.py
            ├── cli/
            │   ├── __init__.py
            │   └── main.py               # typer app: reverse-engineer --connection-file
            └── gui/
                ├── __init__.py
                ├── main.py               # точка входа db-pm-gui
                ├── main_window.py        # тонкий MainWindow
                └── widgets/
                    ├── __init__.py
                    ├── connection_dialog.py     # добавление/редактирование/тест
                    ├── connection_list.py       # список + выбор
                    └── project_viewer.py        # QTreeView + QSplitter + QScintilla (read-only)

tests/
├── conftest.py                           # фикстуры: env crypto-ключа, tmp connections/
├── unit/
│   ├── test_crypto_util.py               # шифр/дешифр, вложенные dict, отсутствие env
│   ├── test_connection_store.py          # save/load/roundtrip, расшифровка пароля
│   ├── test_app_config.py                # загрузка CFG из example, defaults
│   └── test_sql_generator.py             # рендер шаблонов на фейковых данных объектов
└── fixtures/
    └── sample_structure.json             # захардкоженная структура (как из get_database_structure)
```

> `domain/graph.py`, `sql/topological_sort.py`, парсер SQL, `collect_deployment`, `compare` — **не входят в Фазу 1**, это Фаза 2.

---

## 3. Безопасность (выполняется на шаге S1)

`.gitignore` — дополнить:
```
config.yaml
config.local.yaml
connections/
!connections/example.yaml
logs/
.env
*.local.yaml
```

Шаблоны в git:
- `config.example.yaml` — конфиг приложения (пути, тема, логирование).
- `connections/example.yaml` — пример файла подключения с зашифрованным паролем-плейсхолдером и комментариями.

Модель угроз (из roadmap §3.2): ключи Fernet живут только на машине/в секретах CI; даже коммит шифртекста безопасен, но `connections/` всё равно игнорируем целиком (кроме example), чтобы не светить метаданные стендов.

---

## 4. Перенос crypto (шаг S2)

Источник: `C:\repos\personal\-=SOURCECRAFT=-\project-watcher-dagster\packages\shared_pckg\src\shared_pckg\crypto_util.py`

- Копируем **без изменений** в `infrastructure/crypto/crypto_util.py`.
- Функции, которые окажутся в пакете:
  - `get_decrypted_text(encrypted_data)` — `crypto__<ENV>__<token>` → plaintext.
  - `get_encrypted_text(plaintext, env_var)` — plaintext → токен.
  - `get_decrypted_nested_dict(data)` — рекурсивная расшифровка dict/list/str.
  - `format_cipher_token(env_var, ciphertext)`, `generate_fernet_key()`, `_is_cipher_token(s)`.
- Ключевое преимущество: **ленивое** чтение env (импорт модуля не падает без env), имя env берётся из самого токена.
- Тесты `test_crypto_util.py`:
  - roundtrip encrypt→decrypt;
  - расшифровка вложенного dict с одним полем-паролем;
  - `KeyError` при отсутствии env;
  - строка не в формате токена возвращается как есть.

---

## 5. Конфигурация (шаг S3)

### 5.1. `app_config.py` — переносим подход из `shared_pckg/config.py`
- Pydantic-модели с `ConfigDict(extra="ignore")`.
- Минимум для Фазы 1:
  ```python
  class PathsConfig(BaseModel):
      default_output_dir: str
      logs_dir: str = "./logs"

  class LoggingConfig(BaseModel):
      level: str = "INFO"
      console: bool = True
      file_name: str = "app.log"

  class CFG(BaseModel):
      paths: PathsConfig
      logging: LoggingConfig = LoggingConfig()
      default_connection_file: str | None = None
  ```
- `load_cfg(source, *, decrypt=True) -> CFG` — YAML/dict → опц. расшифровка → валидация.
- В отличие от старой `config/config.py`: **никаких хардкод-подключений** внутри CFG; подключения — отдельные файлы (§5.2).

### 5.2. `connection_store.py`
- `ConnectionConfig` (в `domain/connection.py`):
  ```python
  class ConnectionConfig(BaseModel):
      name: str | None = None
      type: str = "postgres"          # postgres | greenplum | snowflake | mssql
      host: str
      port: int = 5432
      database: str
      username: str
      password: str                   # может быть crypto__<ENV>__<token> или plaintext
      options: dict[str, str] = {}
  ```
- `ConnectionStore`:
  - `save(path, cfg)` — шифрует `password` через `crypto_util.get_encrypted_text`, пишет YAML.
  - `load(path) -> ConnectionConfig` — читает YAML, расшифровывает пароль.
  - `test(cfg) -> tuple[bool, str]` — пробное подключение через адаптер, возвращает (ok, message). Используется UI-кнопкой «Тест».
- CLI `--connection-file` использует `ConnectionStore.load(path)`.

---

## 6. PG-адаптер (шаг S4)

- `database/base.py` — `DatabaseAdapter` ABC. Минимальный контракт для reverse-engineer:
  ```python
  class DatabaseAdapter(ABC):
      def connect(self, cfg: ConnectionConfig) -> None: ...
      def disconnect(self) -> None: ...
      @abstractmethod
      def get_database_structure(self) -> dict: ...   # формат как в POC
  ```
  (Полный контракт с `list_objects`/`get_ddl`/`execute_script`/`create_temp_database` — Фаза 2.)
- `database/postgres/adapter.py` — перенос `PGDatabaseModel` из POC `database.py`:
  - SQLAlchemy `create_engine`, `AUTOCOMMIT`, методы `get_schemas/get_tables/get_columns/get_constraints/get_pk_constraint/get_indexes/get_sequences/get_views/get_materialized_views/get_functions/get_procedures` + `get_database_structure`.
  - **Исправления по roadmap:**
    - пароль в соединение передаётся аргументами, а не в URL (чтобы не светился в исключениях);
    - Greenplum-фолбэк для `get_sequences` сохраняем.
- `database/postgres/queries.py` — тексты SQL вынесены из методов в константы/отдельный модуль (читаемость + тестируемость).
- Источник переносимых запросов: `C:\repos\reksoft\db-project-manager\src\db_project_manager\core\database.py` (1080 строк).

---

## 7. Генератор SQL и шаблоны (шаг S5)

- `sql/sql_generator.py` — перенос `SQLGenerator` из POC, шаблоны из `infrastructure/templates/`.
- **Шаблоны переносятся с фиксажами** (roadmap §6):
  - `table.sql.j2`: `{{ c.comment }}` → `{{ f.comment }}` в секции FK; унифицировать логику запятых.
  - `view.sql.j2` / `materialized_view.sql.j2`: гарантировать разделитель после `AS`.
- Формат вывода: `<output>/<database>/<schema>/<type>s/<type> <name>.sql` (как в POC).
- `files/project_io.py` — выделенная запись дерева (отдельно от генератора, чтобы UI-дерево и генератор делили логику путей).

---

## 8. Application-слой (шаг S6)

- `application/reverse_engineer.py`:
  ```python
  class ReverseEngineerService:
      def __init__(self, adapter: DatabaseAdapter, generator: SQLGenerator): ...
      def run(self, conn_cfg: ConnectionConfig, output_dir: Path,
              progress: Callable[[int, int, str], None] | None = None) -> Path:
          adapter.connect(conn_cfg)
          structure = adapter.get_database_structure()
          path = generator.generate_scripts(structure, output_dir / conn_cfg.database)
          return path
  ```
- `progress(n_done, n_total, message)` — callback для UI (прогресс-бар) и заглушки в CLI.

---

## 9. CLI (шаг S7)

`presentation/cli/main.py` (typer):
```
db-pm reverse-engineer --connection-file <conn.yaml> --output <dir> [--config <config.yaml>]
```
- Загружает CFG (опц. `--config`, иначе поиск по умолчанию).
- `ConnectionStore.load(--connection-file)`.
- По `type` в ConnectionConfig выбирает адаптер (Фаза 1: только `postgres`; `greenplum` → тот же адаптер).
- Запускает `ReverseEngineerService.run`, пишет прогресс в stdout.
- Возврат: код 0 при успехе, ненулевой + сообщение при ошибке.

Entry point в `pyproject.toml`: `db-pm = "db_project_manager.presentation.cli.main:app"`.

---

## 10. GUI (шаг S8)

Точки входа: `db-pm-gui = "db_project_manager.presentation.gui.main:main"`.

### 10.1. `main_window.py` — тонкий
- Панель «Подключения»: `ConnectionListView` + кнопки Добавить/Изменить/Удалить/Тест.
- Панель «Рабочая директория»: выбор output dir (сохраняется в CFG).
- Кнопка «Сгенерировать скрипты объектов БД» → `QRunnable` в `QThreadPool`, без блокирующего `QEventLoop`.
- **Панель «Проект БД» (read-only):**
  - `QSplitter` горизонтальный: слева `QTreeView` с `QFileSystemModel` (root = output dir), справа просмотрщик.
  - `ProjectViewer` (`widgets/project_viewer.py`): по клику в дереве читает файл и грузит в `QScintilla` (read-only, лексер SQL, номера строк, моноширинный шрифт).
- Прогресс-бар (`n/m` от сервиса) + статус-лог (`QTextEdit`).
- Разделитель дерева/контента (`QSplitter`) сохраняет пропорции в CFG.

### 10.2. `connection_dialog.py`
- Форма: name, type (комбо: postgres/greenplum), host, port, database, username, password.
- Кнопка «Тест» → `ConnectionStore.test(cfg)` → результат в `QMessageBox`.
- ОК → `ConnectionStore.save(path, cfg)` (пароль шифруется).

### 10.3. `connection_list.py`
- `QListView` + модель из списка файлов `connections/*.yaml` (оживляем мёртвый код POC).

### 10.4. Зависимости GUI
- `PySide6 ~= 6.8`
- `PySide6-QScintilla` (или эквивалент для версии PySide6) — проверить совместимость с PySide6 6.8.x на шаге S0 (если пакета для 6.8 нет — рассмотреть `pyqtdarktheme`/свой `QSyntaxHighlighter` как fallback, но цель — QScintilla).

---

## 11. Тесты (шаг S9)

- `tests/conftest.py`: фикстура `crypto_env` (генерирует Fernet-ключ, выставляет env), `tmp_connections_dir`.
- Unit-тесты (без живой БД): crypto, connection_store, app_config, sql_generator (на `fixtures/sample_structure.json`).
- PG-адаптер **не покрываем** unit-тестами в Фазе 1 (нужна живая БД → интеграционные тесты переезжают в Фазу 2 с testcontainers).
- `pytest` — в `[dependency-groups] dev` (через `uv`).
- Запуск: `uv run pytest`.

---

## 12. Порядок выполнения (зависимости шагов)

```
S0  Очистка старой заготовки + pyproject.toml + uv
      ├─► S2  crypto_util + тесты
      ├─► S3  app_config + connection_store + тесты
      │      └─► S5  sql_generator + templates (фиксажи) + тесты
      ├─► S4  PG-адаптер (перенос database.py)
      │      └─► S6  ReverseEngineerService
      │             ├─► S7  CLI
      │             └─► S8  GUI
      └─► S1  .gitignore + example-конфиги (параллельно с S2-S3)
S9  Тесты贯穿но с каждым шагом; финальный прогон в конце
```

**Рекомендуемая последовательность коммитов:**
1. `chore: remove legacy stub, scaffold package + pyproject (uv)` (S0+S1)
2. `feat(crypto): port lazy crypto_util from shared_pckg + tests` (S2)
3. `feat(config): pydantic CFG + ConnectionStore` (S3)
4. `feat(db): PG adapter (port catalog queries)` (S4)
5. `feat(sql): generator + fixed templates` (S5)
6. `feat(app): ReverseEngineerService` (S6)
7. `feat(cli): reverse-engineer --connection-file` (S7)
8. `feat(gui): connections + reverse-engineer + project viewer (QScintilla)` (S8)
9. `test: unit suite for crypto/config/generator` (S9 — если не вливалось по шагам)

---

## 13. Риски Фазы 1

| Риск | Смягчение |
|------|-----------|
| `PySide6-QScintilla` несовместим с PySide6 6.8.x | Проверить на S0 до заложения в GUI; fallback — `QSyntaxHighlighter` (временный, с пометкой TODO) |
| Нет под рукой тестовой PG для ручной проверки адаптера | Использовать образец `samples/aviasales_medium` только для UI-дерева; адаптер проверять на любой доступной PG (или отложить smoke-тест до Фазы 2/testcontainers) |
| Перенос 1080-строчного `database.py` тянет баги | Переносить методами + сразу переносить тексты запросов в `queries.py`; покрыть `sql_generator` тестами на фейковых данных |
| Зеленые тесты создают ложную уверенность в адаптере | Чётко фиксировать: PG-адаптер в Фазе 1 = smoke на живой БД вручную, автотесты — Фаза 2 |

---

## 14. Чек-лист приёмки Фазы 1

- [ ] `config/`, старый `src/main.py`, `requirements.txt` удалены; `pyproject.toml` + `uv.lock` работают (`uv sync`, `uv run pytest`).
- [ ] `connections/` и `config.yaml` в `.gitignore`; `*.example.yaml` в git.
- [ ] `crypto_util` импортируется без env (тест зелёный).
- [ ] `ConnectionStore.save/load` roundtrip с шифрованием пароля (тест зелёный).
- [ ] `db-pm reverse-engineer --connection-file conn.yaml --output dir/` генерирует дерево SQL-файлов на живой PG (smoke).
- [ ] GUI: добавление/редактирование/тест подключения, запуск reverse-engineer без блокировки UI, прогресс.
- [ ] GUI: дерево проекта + read-only просмотр SQL с подсветкой (QScintilla).
- [ ] CLI и GUI используют один `ReverseEngineerService` (ни строчки бизнес-логики в `presentation/`).
- [ ] `uv run pytest` зелёный (unit: crypto/config/generator).
