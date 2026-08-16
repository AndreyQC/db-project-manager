# db-project-manager

Инструмент для работы со структурой баз данных (PostgreSQL/Greenplum; Snowflake/MSSQL/MySQL — в планах): чтение метаданных каталога, генерация дерева SQL-файлов (по одному на объект), построение графа зависимостей, валидация деплоя на пустую временную БД, и (в будущих фазах) миграции.

> Статус: Phase 12 — ALTER + Delta готов: reverse-engineering, граф зависимостей, validation deploy, compare, safety gate (deploy analyze) и controlled apply к существующей БД с репетицией (CLI). Post-deploy отчёты — Phase 13.

## Возможности

### Phase 1 — Reverse-engineering
- **Reverse-engineering**: подключение к БД → генерация дерева SQL-файлов (`<schema>/<type>s/<type> <name>.sql`) с autodoc-заголовком (YAML с метаданными объекта).
- **GUI** `db-pm-gui`: управление подключениями (единственное место их создания/редактирования) с шифрованием пароля и кнопкой «Тест»; запуск reverse-engineer без блокировки UI; read-only просмотр сгенерированных файлов (дерево + SQL с подсветкой).

### Phase 2 — Граф зависимостей + Validation deploy
- **Граф зависимостей**: построение по кодовой базе (чтение autodoc-блоков + извлечение зависимостей FK/SELECT/JOIN/nextval), хранение в `.dbm_graph/`, экспорт в JSON / GraphML / Graphviz DOT. Циклы и висячие ссылки детектируются.
- **Validation deploy**: развёртывание кодовой базы в пустую временную БД (`<prefix>_<server-UTC-timestamp>`) с проверкой прав `CREATEDB`, топосортировкой, фильтром `project.build`, стратифицированной стратегией ошибок (ранние DDL — fail-fast + cleanup; views/процедуры — пообъектный лог с `--continue-on-error`) и удалением временной БД по умолчанию.
- **CLI** для автоматизации/CI/CD: `reverse-engineer`, `graph {build,export,show,validate}`, `deploy validate`.

### Безопасность
- Секреты и `connections/` вне git; пароли шифруются (Fernet, формат `crypto__<ENV>__<token>`); `.dbm_graph/` тоже вне git (детерминированно перестраивается из кодовой базы).

## Требования

- Python 3.13+
- [uv](https://docs.astral.sh/uv/) (менеджер пакетов/окружения)
- Доступ к PostgreSQL/Greenplum (для reverse-engineering)

## Установка

```bash
git clone <repo-url> db-project-manager
cd db-project-manager
uv sync
```

## Настройка

1. **Конфиг приложения**: скопируйте `config.example.yaml` → `config.yaml` (в `.gitignore`) и задайте пути.
2. **Ключ шифрования**: задайте переменную окружения с Fernet-ключом (сгенерируйте однажды):
   ```bash
   # Linux/macOS
   export ENVOS_CRYPTO_01=$(uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
   # Windows (PowerShell)
   $env:ENVOS_CRYPTO_01 = ...ключ...
   ```
3. **Подключение**: создайте его в GUI (рекомендуется) — пароль зашифруется автоматически. Либо вручную по образцу `connections/example.yaml`.

## Использование

### CLI

```bash
# БД → дерево SQL-файлов (reverse-engineering)
db-pm reverse-engineer --connection-file connections/mydb.yaml --output ./output

# Граф зависимостей по кодовой базе
db-pm graph build   --dir ./output/mydb                 # построить, записать в .dbm_graph/
db-pm graph validate --dir ./output/mydb                # проверить циклы и висячие ссылки
db-pm graph export  --dir ./output/mydb --format graphml # экспорт для Gephi / yEd / внешнего приложения
db-pm graph show    --dir ./output/mydb --object <object_key>

# Валидация деплоя на пустой временной БД (нужны права CREATEDB)
db-pm deploy validate \
    --dir ./output/mydb \
    --connection-file connections/server.yaml \
    [--prefix myapp] [--keep-db] [--continue-on-error]

# Safety gate: dry-run анализ деплоя на СУЩЕСТВУЮЩУЮ БД с данными — Phase 11.
# Read-only: дельта код↔БД, оценка данных в тронутых таблицах (reltuples, без
# COUNT; stale-статистика = «есть данные»), сопоставление с pre-скриптами
# (project.covers в autodoc), отчёт-рекомендация.
# Exit codes: 0 — нарушений нет; 1 — нарушения (пайплайн остановлен); 2 — ошибка.
db-pm deploy analyze \
    --dir ./output/mydb \
    --target-connection-file connections/prod.yaml \
    --output-dir ./sg_report
# Отчёты: safety_gate_report.md, safety_gate_report.json + diff_report.json

# Дельта деплоя на СУЩЕСТВУЮЩУЮ БД — Phase 12 (dry-run).
# Column-level diff, классификация операций safe / needs-pre / blocked,
# артефакты для review: delta/NNN_*.sql, plan.json, plan.md.
# Exit codes: 0 — ok; 1 — BLOCKED-операции (нужны pre-скрипты/решения); 2 — ошибка.
db-pm deploy plan \
    --dir ./output/mydb \
    --target-connection-file connections/prod.yaml \
    --output-dir ./sg_report \
    [--include-drops]

# Применение дельты к СУЩЕСТВУЮЩУЮ БД (изменяет её!) — Phase 12.
# По умолчанию: репетиция — состояние таргета воспроизводится в temp-БД,
# прогоняется seed (__migrations/seed/ — только в репетиции) и весь пайплайн;
# затем против таргета: pre-скрипты -> повторная дельта (CD-11, только SAFE)
# -> применение с stop-on-error -> post-скрипты -> запись schema_version
# (source='apply'). Восстановление после сбоя — повторным apply (дельта
# пересчитывается, исполненные pre/post скипаются).
# Флаги: --include-drops (REMOVED-объекты), --no-rehearsal (CI),
# --keep-rehearsal-db (отладка).
db-pm deploy apply \
    --dir ./output/mydb \
    --target-connection-file connections/prod.yaml \
    --output-dir ./sg_report

# Сравнение двух состояний (БД или каталог reverse-engineer) — Phase 9
db-pm compare run \
    --output-dir ./diff_report \
    (--source-dir ./output/mydb | --source-connection-file connections/dev.yaml) \
    (--target-dir ./output/prod | --target-connection-file connections/prod.yaml) \
    [--keep-model-dir]
# Отчёт: source.json, target.json, diff_report.json (added/removed/changed/unchanged)
```

### GUI

```bash
db-pm-gui
```

Добавьте подключение → выберите папку вывода → «Сгенерировать скрипты объектов БД». Файлы появятся в дереве слева; кликните любой `.sql`, чтобы увидеть содержимое с подсветкой. Кнопка «Deploy validate…» запускает валидационный деплой с диалогом опций (префикс, чекбокс «оставить БД», continue-on-error). Действие «Safety gate…» (Phase 11) — dry-run анализ деплоя на существующую БД: вердикт CLEAN/VIOLATIONS + ссылка на отчёт.

## Разработка

```bash
uv sync                 # установить зависимости (включая dev)
uv run pytest           # unit-тесты (integration по умолчанию пропускаются)
uv run pytest -m integration   # integration-тесты (требуется запущенный Docker)
uv run pytest --cov=db_project_manager   # с покрытием
uv run ruff check .     # линтер
```

Структура пакетов: `domain` (модели) → `infrastructure` (БД, файлы, crypto) → `application` (сервисы) → `presentation` (CLI/GUI). Подробности: `_tasks_/`.

## Лицензия

MIT
