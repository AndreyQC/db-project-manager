# Roadmap миграции (Phase 00 → релиз)

> Связанные документы:
> - `001_analysis_existing_code.md` — анализ POC в `C:\repos\reksoft\db-project-manager`
> - `002_idea.md` — видение продукта
>
> Источник crypto-реализации: `C:\repos\personal\-=SOURCECRAFT=-\project-watcher-dagster\packages\shared_pckg\src\shared_pckg\crypto_util.py`, `config.py`
> Дата: 2026-07-17

---

## 0. Принципы миграции

1. **Не «переписать всё», а перенести ценное.** SQL-запросы к `pg_catalog`, шаблоны Jinja2, формат «объект = типизированный файл», идея autodoc и топосортировки проверены в проде — их сохраняем.
2. **Чистая архитектура слоями.** Presentation (GUI/CLI) → Application (сервисы) → Domain (модели) → Infrastructure (DB-драйверы, файлы, crypto). Бизнес-логика больше не живёт в обработчиках кнопок.
3. **Расширяемость СУБД через адаптеры.** Postgres, Greenplum, Snowflake, MySQL — реализуют общий контракт. Парсеры SQL — подключаемые.
4. **Безопасность по умолчанию:** секреты и `connections/` не в git; crypto берётся из проверенной реализации (см. §4).
5. **Тестируемость с первого дня:** покрытие доменных и application-слоёв, фикстуры без живой БД.

---

## 1. Целевая архитектура

```
db_project_manager/
├── domain/                      # Чистые модели, не зависят от БД/UI
│   ├── objects.py               # DbObject, Schema, Table, View, ... (dataclass/pydantic)
│   ├── connection.py            # ConnectionConfig
│   ├── graph.py                 # DependencyGraph (vertices + edges)
│   └── project.py               # DbProject — каталог объектов как дерево
│
├── infrastructure/
│   ├── crypto/                  # ← переносим crypto_util.py (см. §4)
│   │   └── crypto_util.py
│   ├── config/
│   │   ├── app_config.py        # CFG (pydantic) — переносим подход из shared_pckg
│   │   └── connection_store.py  # YAML подключений + расшифровка
│   ├── database/
│   │   ├── base.py              # ABC DatabaseAdapter (connect/list_objects/get_ddl/...)
│   │   ├── postgres/            # PGDatabaseAdapter — переносим запросы из database.py
│   │   │   ├── adapter.py
│   │   │   └── queries/         # get_schemas.sql, get_tables.sql, ... (вынести из кода)
│   │   ├── greenplum/           # подмножество PG + внешние таблицы
│   │   ├── snowflake/           # позже
│   │   └── registry.py          # реестр адаптеров по типу/диалекту
│   ├── sql/
│   │   ├── parser.py            # парсер объектов из .sql (замена dflw_parser_pg_sql)
│   │   ├── dependency_extractor.py  # построение рёбер (FROM/JOIN/INSERT/...)
│   │   └── topological_sort.py  # перенос + исправления (см. §5)
│   ├── files/
│   │   └── project_io.py        # чтение/запись дерева «<schema>/<type>s/<type> <name>.sql»
│   └── logging.py               # единая настройка loguru, настраиваемый путь
│
├── application/                 # Сценарии (use cases) — вызываются и CLI, и GUI
│   ├── reverse_engineer.py      # БД → файлы (generate_scripts)
│   ├── collect_deployment.py    # файлы → единый скрипт деплоя (+ валидация на temp БД)
│   ├── compare.py               # diff каталога и БД → миграционный скрипт
│   └── migrations.py            # (фаза 3) пред-/пост-деплой, учёт наличия данных
│
├── presentation/
│   ├── cli/                     # typer-приложение, делегирует в application/*
│   └── gui/                     # PySide6, тонкий слой, только сигналы/потоки
│
├── templates/                   # Jinja2-шаблоны (переносим 9 шт., дорабатываем)
└── tests/                       # unit + integration (testcontainers)
```

### Соответствие слоёв и сценариев из видения (002_idea)

| Сценарий из видения | Слой application | Источник в POC для переноса |
|---------------------|------------------|-----------------------------|
| Подключения к PG/GP/MSSQL/Snowflake | `infrastructure/database/registry.py` | `database.py` (PG/GP-запросы) + новый MSSQL/Snowflake |
| БД → файловая иерархия объектов | `reverse_engineer.py` | `DatabaseWorker` + `SQLGenerator` |
| Граф зависимостей | `sql/topological_sort.py` + `domain/graph.py` | `dflw_parser_pg_sql.py`, `dflw_topological_sort.py` |
| Скрипт деплоя в правильной последовательности | `collect_deployment.py` | `MainWindow._collect_files` |
| Валидация: деплой на пустую БД | `collect_deployment.py` (deploy to temp DB) | `MainWindow._apply_file` + `DatabaseDeployment` |
| Включение в CI/CD | CLI entrypoint `db-pm deploy --validate` | новый |
| Проект БД из дампа и актуального состояния | `compare.py` | `MainWindow._compare_directory_and_db` |
| Миграции + pre/post-deploy + учёт данных | `migrations.py` | новый (фаза 3) |

---

## 2. Что именно переносим из POC, а что — переписываем

### ✅ Переносим почти как есть (с очисткой)
- **SQL-запросы к `pg_catalog`** из `database.py`: `get_schemas`, `get_tables`, `get_columns`, `get_constraints`, `get_pk_constraint`, `get_indexes`, `get_sequences` (включая Greenplum-фолбэк), `get_views`, `get_materialized_views`, `get_functions`, `get_procedures`. Переносим как методы адаптера; тексты запросов — в отдельные `.sql`/константы для читаемости и тестирования.
- **Шаблоны Jinja2** `templates/*.sql.j2` (9 шт.) — с исправлениями багов (см. §6).
- **Формат файлов** `<schema>/<type>s/<type> <name>.sql` и конкатенацию с обёртками `DO $$ ... RAISE NOTICE ... $$`.
- **`TYPE_PRIORITIES`** и общую идею топосортировки.
- **Настройки линтеров** (`ruff`, `sqlfluff`, `yamllint`, pre-commit).

### ♻️ Переписываем / серьёзно дорабатываем
- **Парсер SQL** (`dflw_parser_pg_sql.py`) — заменяем наивный токенайзер на надёжный: `sqlglot` (или pglast) для AST, чтобы корректно извлекать объекты и связи. Старая логика — как фолбэк/референс.
- **Топосортировка** — добавляем обратно проверку циклов, детерминированный порядок (сортировка соседей), корректную дедупликацию рёбер.
- **GUI** — тонкий слой: `MainWindow` только отображает и прокидывает события; вся логика в `application/*`. Убираем блокирующий `QEventLoop` (§7).
- **Модели объектов** — одна доменная иерархия (из `database_service.py` берём идею, но без багов: `autodoc` getter, дублированное свойство `schemas`).

### ❌ Не переносим (мёртвый код / антипаттерны)
- `common/config.py` (заменён на pydantic `CFG`).
- `common/exceptions.py` — пересоздаём минимально под реальные ошибки.
- `connections/base.py` ABC и `database_service.py` как черновик — после извлечения полезных идей (autodoc, ООП-модель).
- `gui/widgets/connection_dialog.py`, `gui/widgets/connection_list.py` — не использовались.
- `tests/test_database.py` — устарели полностью, переписываем.
- Хардкод путей в `utils/create_sf_db_from_csv.py` и `__main__` блоках.

---

## 3. Безопасность и секреты

### 3.1. `.gitignore` — добавляем
```
config.yaml
config.local.yaml
connections/
logs/
.env
*.local.yaml
```
Создаём `config.example.yaml` и `connections/example.yaml` с плейсхолдерами — они в git.

### 3.2. Учётные данные
- Пароли в `connections/*.yaml` хранятся **только** в формате `crypto__<ENV_VAR>__<fernet_token>`.
- Согласно твоей заметке: переменные окружения (ключи Fernet) живут только на твоей машине, поэтому даже при случайном коммите зашифрованного значения расшифровка на чужой стороне невозможна без `ENV_VAR`. Это принимаем как модель угроз — **но** всё равно не коммитим `connections/`, чтобы не светить даже шифртексты и метаданные стендов (хосты).

### 3.3. SQL-безопасность
- `CREATE DATABASE <name>` — валидация идентификатора по whitelist `[A-Za-z0-9_]`, никакого f-string с пользовательским вводом.
- Строку подключения не собираем с паролем в URL; используем именованные аргументы драйвера, чтобы пароль не попадал в логи/исключения SQLAlchemy.

---

## 4. Crypto — переносим твою реализацию

Берём **как есть** из `shared_pckg` (это уже исправленная версия старой `crypto.py`):

| Проблема старой `crypto.py` (POC) | Решение в переносимой `crypto_util.py` |
|-----------------------------------|----------------------------------------|
| `cipher_key = os.environ[ENV_VARIABLE_NAME]` **на верхнем уровне модуля** → импорт падает, если env не задан | Ключ читается **лениво** внутри `get_decrypted_text`/`get_encrypted_text` только при реальной операции |
| Жёстко зашито имя env `ENVOS_CRYPTO_01` | Имя env-переменной берётся из **самого шифртекста** (`crypto__<ENV>__<token>`), ключ может быть любым |
| Только дешифрование строки; шифрование писалось в `__main__` | Есть `get_encrypted_text(plaintext, env_var)` и `generate_fernet_key()` как полноценный API |
| `get_decrypted_nested_dict` глотал любые исключения молча | Чёткие исключения: `KeyError` при отсутствии env, `ValueError` при ошибке Fernet; nested-обход оставляет нерасшифрованную строку с warning |
| Смешение ASCII/bytes при `decrypt` | Двойная попытка `encode("ascii")` → fallback на `bytes` |

### Что переносим
- `crypto_util.py` → `infrastructure/crypto/crypto_util.py` без изменений (либо минимальная адаптация импортов).
- Подход `config.py` из `shared_pckg`: **pydantic-модели + `load_cfg(source, decrypt=True)`** — это образец и для `app_config.py`, и для `connection_store.py`.

### Дополнительно для нового проекта
- Команда CLI `db-pm crypto encrypt <env_var>` / `db-pm crypto decrypt` — обёртка над `crypto_util` для ручного шифрования пароля при добавлении подключения (заменяет `__main__` блок старой версии).
- Утилита `db-pm crypto genkey` → `generate_fernet_key()`.

---

## 5. Граф зависимостей — что исправляем

Переносим идею, чиним конкретные баги POC:

1. **Проверка циклов возвращается** (`len(result) != len(vertices)` → ясная ошибка со списком вершин цикла, а не тихая потеря объектов).
2. **Детерминизм:** соседи сортируются перед добавлением в очередь (в POC порядок зависел от порядка итерации dict).
3. **Дедупликация рёбер:** вместо `set(tuple(sorted(d.items())))` (ломается на разных типах значений) — хешируемый ключ `(source, destination, relation, action)`.
4. **Извлечение связей через AST** (`sqlglot`): корректно учитываем алиасы, квалифицированные имена `schema.table`, CTE. Старый подход «слово перед/после» давал ложные срабатывания и пропуски (например, порядок `elif` делал `left join` недостижимым после общего `join`).
5. **`TYPE_PRIORITIES`** расширен под новые типы (external_table уже есть, добавляем type/enum/policy).
6. **Экспорт графа** в стандартный формат (JSON + опц. GraphML/DOT) — прямая поддержка твоего сценария «анализировать граф в другом приложении».

---

## 6. Шаблоны Jinja2 — исправления

Переносим 9 шаблонов, фиксы по найденным багам:

- **`table.sql.j2`**: в секции FK используется `{{ c.comment }}` при итерации `f` → должно быть `{{ f.comment }}`. Также унифицировать логику запятых между колонками/констрейнтами (сейчас условие `{% if (not loop.last) or primary_keys or constraints%}` хрупкое).
- **`view.sql.j2` / `materialized_view.sql.j2`**: `AS{{ definition }}` без пробела — нормально, если definition начинается с перевода строки, но стоит гарантировать разделитель.
- Добавить **пропущенные генераторы**: `index.sql.j2` (данные для индексов уже читаются из БД, но рендера нет), и дописать `trigger.sql.j2` / `extension.sql.j2` (шаблоны есть, генераторы — заглушки).
- Поддержать **autodoc-блок** в начале каждого файла (идея из `database_service.py`): стандартный YAML-заголовок с метаданными объекта — будет использоваться парсером и diff-логикой.

---

## 7. GUI — как упрощаем

- `MainWindow` тонкий: только построение UI, чтение полей, вызов `application/*` сервисов, отображение прогресса.
- **Убираем антипаттерн `QEventLoop().exec_()`.** Вместо блокировки: запускаем `QRunnable` в `QThreadPool`, по `finished`/`error` обновляем UI через сигналы; кнопка действия отключается на время операции, статус и прогресс — в `QTextEdit`/`QProgressBar`.
- Виджеты подключения (`ConnectionDialog`, `ConnectionListView`) — переносим в активное использование (в POC они были мёртвым кодом, а в `MainWindow` диалог дублировался инлайн).
- Прогресс-бар действительно отражает этапы (сейчас всегда скрыт): `reverse_engineer` репортит `n/m` по схемам/объектам.

---

## 8. CLI и CI/CD

**Принцип разделения ответственности между UI и CLI:**
- **UI (PySide6) — единственное место для создания/редактирования подключений.** Форма с валидацией полей, выбор типа СУБД, кнопка «Тест соединения», шифрование пароля через `crypto_util` при сохранении в файл (по умолчанию `connections/<name>.yaml`). Так убираем неудобство CLI-ввода секретов (история оболочки, отсутствие интерактива) и даём наглядную обратную связь.
- **CLI (typer) — потребитель подключений** для автоматизации/CI/CD: принимает **путь к файлу подключения** через `--connection-file <path>`, читает YAML, расшифровывает пароль из env. CLI не управляет подключениями (нет add/edit/test) — он работает с файлом, созданным в UI. Это делает CLI stateless и удобным для пайплайнов: файл подключения кладётся в репозиторий CI (или секреты CI) и явно передаётся аргументом.
- Вариант указания подключения в CLI: только `--connection-file path/to/conn.yaml`. Никакого глобального реестра/имя-по-умолчанию команда не ищет — явный путь = предсказуемость в CI.

Команды (typer), покрывающие сценарии из видения:

```
db-pm reverse-engineer --connection-file <conn.yaml> --output <dir>
    # БД → дерево SQL-файлов

db-pm build --dir <dir> --output deploy.sql
    # файлы → единый скрипт с топосортировкой (подключение не нужно)

db-pm deploy --script deploy.sql --connection-file <conn.yaml> [--temp-db] [--validate]
    # деплой; с --temp-db создаёт временную БД и проверяет (для CI/CD)

db-pm diff --dir <dir> --connection-file <conn.yaml> --output migration.sql
    # сравнение каталога и фактической схемы

db-pm graph export --dir <dir> --format json|graphml
    # экспорт графа зависимостей

db-pm crypto encrypt <env_var>      # шифрует пароль из stdin
db-pm crypto decrypt <token>
db-pm crypto genkey
```

**CI/CD-пайплайн** (фаза 2) собирается из CLI:
```yaml
# CI secret: путь к файлу подключения (создан в UI, пароль зашифрован, ключ ENV_VAR — в секретах CI)
- db-pm reverse-engineer --connection-file $CI_CONN_FILE --output artifacts/schema
- db-pm diff --dir repo/db --connection-file $CI_CONN_FILE --output artifacts/migration.sql
- db-pm deploy --script artifacts/migration.sql --connection-file $CI_CONN_FILE --temp-db --validate
```

---

## 9. Поддержка нескольких СУБД

Контракт адаптера (`infrastructure/database/base.py`):
```python
class DatabaseAdapter(ABC):
    def connect(self, cfg: ConnectionConfig) -> None: ...
    def list_schemas(self) -> list[Schema]: ...
    def list_objects(self, schema: str, object_type: ObjectType) -> list[DbObject]: ...
    def get_object_ddl(self, obj: DbObject) -> str: ...
    def execute_script(self, script: str) -> None: ...
    def create_temp_database(self, base_name: str) -> str: ...
```

- **PostgreSQL** — фаза 1 (полный перенос из POC).
- **Greenplum** — фаза 1 (тот же адаптер PG + переопределённый `get_sequences`/внешние таблицы).
- **Snowflake** — фаза 2 (есть зачаток в `utils/create_sf_db_from_csv.py` — импорт объектов из CSV-дампа; нужно подключение через `snowflake-connector`/`snowpark`).
- **MSSQL / MySQL** — фаза 3.
- Реестр `registry.py`: `get_adapter(db_type: str) -> DatabaseAdapter`. Тип хранится в `connections/<name>.yaml` (`type: postgres`).

---

## 10. Фазы реализации (milestones)

### Фаза 1 — Фундамент (MVP PG + CLI + базовый GUI)
- [ ] Скелет пакетов по целевой архитектуре (§1).
- [ ] Перенос `crypto_util.py` + pydantic `app_config.py` / `connection_store.py` (§4).
- [ ] `.gitignore`, `config.example.yaml`, `connections/example.yaml` (§3).
- [ ] `infrastructure/database/postgres` — перенос запросов из `database.py`, как адаптер.
- [ ] `domain/objects.py`, `domain/graph.py`.
- [ ] `application/reverse_engineer.py` + перенос шаблонов Jinja2 (с фиксажами §6).
- [ ] CLI: `reverse-engineer --connection-file <path>` (управление подключениями — только в UI, см. §8).
- [ ] **Базовый GUI (PySide6)** — единственное место для управления подключениями (§8) и клиент `reverse_engineer`-сервиса (§7):
      - список подключений + диалог добавления/редактирования (перенос `ConnectionDialog`/`ConnectionListView` из мёртвого кода в активное использование) с валидацией полей, выбором типа СУБД и кнопкой «Тест соединения»; сохранение шифрует пароль через `crypto_util`;
      - выбор выходной директории;
      - кнопка «Сгенерировать скрипты объектов БД» → неблокирующий запуск через `QRunnable`/`QThreadPool` с прогрессом и статусом.
      Цель — UI создаёт подключения и запускает reverse-engineer на едином application-слое с CLI.
- [ ] Базовые тесты (mock-адаптер + фикстуры SQL-файлов из `samples/`).

### Фаза 2 — Деплой и граф
- [ ] `application/collect_deployment.py` (файлы → скрипт, с топосортировкой §5).
- [ ] `application/compare.py` (diff каталога и БД).
- [ ] Переписанный парсер SQL на `sqlglot`.
- [ ] CLI: `build`, `deploy --temp-db --validate`, `diff`, `graph export`.
- [ ] **Расширение GUI:** панель «Операции с Базами Данных» (build, deploy, validate), просмотр/экспорт графа зависимостей.
- [ ] Greenplum-адаптер полностью; Snowflake — исследование/импорт из дампа.
- [ ] Интеграционные тесты через testcontainers (PostgreSQL в контейнере).
- [ ] Пример CI/CD пайплайна (§8).

### Фаза 3 — Миграции и расширение СУБД
- [ ] `application/migrations.py`: pre/post-deploy скрипты, учёт наличия данных в таблицах (через `pg_class.reltuples` / sampling) — генерация безопасных миграционных скриптов.
- [ ] **GUI: секция миграций** (diff/миграции с предпросмотром).
- [ ] MSSQL и MySQL адаптеры.
- [ ] Полноценное покрытие тестами; пакетирование и релиз (через `uv build`).

---

## 11. Критерии приёмки (что считаем «правильным решением»)

1. **CLI и GUI работают на одном application-слое** — ни одной строчки бизнес-логики в `presentation/`.
2. **Импорт любого модуля не падает** без установленных env-переменных (в отличие от старой `crypto.py`).
3. **`pytest` зелёный** на unit + integration; CI reproduces.
4. **Ни одного секрета/подключения в git** (проверка: `connections/` и `config.yaml` в `.gitignore`, `git log -p` чист).
5. **`db-pm deploy --temp-db --validate`** успешно разворачивает образец `samples/aviasales_medium` на пустую временную БД — это и есть «валидация, что всё работает» из видения.
6. **Граф зависимостей** корректно строится и экспортируется в JSON.
7. **Добавление новой СУБД** = реализация одного адаптера без правки остальных слоёв.

---

## 12. Риски и смягчение

| Риск | Смягчение |
|------|-----------|
| `sqlglot` не покроет специфичные конструкции GP/Snowflake | Сохранить старый парсер как фолбэк; расширять диалекты постепенно |
| Топосортировка «теряет» объекты при циклах | Обязательная проверка циклов + явный отчёт (§5.1) |
| Различия поведения PG и GP (нет `pg_sequence`, нет процедур) | Адаптер Greenplum наследует PG и переопределяет только нужное |
| Блокировка UI если снова вернёмся к nested event loop | Архитектурное правило: ни одного `QEventLoop.exec_()` в коде (lint-проверка/ревью) |
| Случайный коммит секрета | pre-commit hook на наличие строк `crypto__` вне `*.example.yaml`; `.gitignore` |
