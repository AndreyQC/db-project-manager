# Подготовка окружения

## Установка uv

`uv` — менеджер пакетов и виртуальных окружений (заменяет pip + venv).

### Windows (PowerShell)
```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

### Linux/macOS
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Проверка:
```bash
uv --version
```

## Установка зависимостей проекта

Из корня репозитория:
```bash
uv sync
```

Это создаст виртуальное окружение `.venv`, установит runtime- и dev-зависимости из `pyproject.toml`/`uv.lock` и сам пакет в editable-режиме. Точка входа `db-pm` (CLI) и `db-pm-gui` (GUI) станут доступны через `uv run`.

> Если `uv sync` падает с `invalid peer certificate: UnknownIssuer` в сети с корпоративным TLS-прокси (Kaspersky и т.п.), снимите переменные окружения CA-bundle перед вызовом:
> ```bash
> unset SSL_CERT_FILE REQUESTS_CA_BUNDLE CURL_CA_BUNDLE && uv sync
> ```
> (uv/rustls игнорирует эти переменные; pip их использует, и возникает конфликт.)

## Запуск

```bash
uv run db-pm --help        # CLI
uv run db-pm-gui           # GUI
uv run pytest              # тесты
uv run ruff check .        # линтер
```

## Переменные окружения

- `ENVOS_CRYPTO_01` — Fernet-ключ для шифрования паролей подключений. Сгенерируйте однажды и держите в окружении (не коммитьте):
  ```bash
  uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  ```
- При желании имя env-переменной для шифрования можно переопределить в GUI (по умолчанию `ENVOS_CRYPTO_01`).

## Добавление/изменение зависимостей

```bash
uv add <package>           # добавить runtime-зависимость
uv add --dev <package>     # добавить dev-зависимость
```

После изменения зависимостей коммитьте обновлённый `uv.lock`. Забирающие изменения делают `uv sync` повторно для актуализации окружения.

## Integration-тесты (Phase 2+)

Integration-тесты (`tests/integration/`) поднимают PostgreSQL в Docker-контейнере через `testcontainers`. По умолчанию они **пропускаются** (`addopts = "-m 'not integration'"` в `pyproject.toml`).

```bash
uv run pytest                       # только unit-тесты (быстро)
uv run pytest -m integration        # integration-тесты (нужен Docker)
uv run pytest -m "not integration"  # явно без integration
```

Требования для integration-тестов:
- **Docker Desktop запущен** (на Windows — иконка в трее активна; `docker version` показывает секцию `Server`).
- Если `docker.errors.DockerException: ... CreateFile ...` — daemon не запущен или Python SDK не видит pipe. Запустите Docker Desktop, при необходимости `docker context use desktop-linux`.

Integration-тесты покрывают полный сценарий validation deploy и round-trip reverse-engineer → graph build на реальном PostgreSQL 16.
