# db-project-manager

Инструмент для работы со структурой баз данных (PostgreSQL/Greenplum; Snowflake/MSSQL/MySQL — в планах): чтение метаданных каталога, генерация дерева SQL-файлов (по одному на объект), сборка скриптов развёртывания с учётом зависимостей, сравнение каталога и схемы БД.

> Статус: Phase 1 (MVP) — reverse-engineering через CLI и GUI.

## Возможности (Phase 1)

- **Reverse-engineering**: подключение к БД → генерация дерева SQL-файлов (`<schema>/<type>s/<type> <name>.sql`).
- **CLI** `db-pm reverse-engineer --connection-file conn.yaml --output dir/` — для автоматизации/CI/CD.
- **GUI** `db-pm-gui`:
  - управление подключениями (единственное место их создания/редактирования) с шифрованием пароля и кнопкой «Тест соединения»;
  - запуск reverse-engineer без блокировки UI (фоновый поток);
  - read-only просмотр сгенерированных файлов: дерево проекта + SQL с подсветкой синтаксиса.
- **Безопасность**: секреты и `connections/` вне git; пароли шифруются (Fernet, формат `crypto__<ENV>__<token>`).

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
# БД → дерево SQL-файлов
db-pm reverse-engineer --connection-file connections/mydb.yaml --output ./output
```

### GUI

```bash
db-pm-gui
```

Добавьте подключение → выберите папку вывода → «Сгенерировать скрипты объектов БД». Файлы появятся в дереве слева; кликните любой `.sql`, чтобы увидеть содержимое с подсветкой.

## Разработка

```bash
uv sync                 # установить зависимости (включая dev)
uv run pytest           # тесты
uv run pytest --cov=db_project_manager   # с покрытием
uv run ruff check .     # линтер
```

Структура пакетов: `domain` (модели) → `infrastructure` (БД, файлы, crypto) → `application` (сервисы) → `presentation` (CLI/GUI). Подробности: `-=tasks=-/`.

## Лицензия

MIT
