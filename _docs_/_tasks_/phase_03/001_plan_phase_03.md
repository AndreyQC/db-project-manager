# План Phase 3 — SSH-туннельные подключения к PostgreSQL

> Контекст:
> - `_tasks_/phase_02/001_plan_phase_02.md` — предыдущая фаза (graph + deploy validate)
> - `connections/example.yaml` — текущий формат подключения (только direct PG)
> - `src/db_project_manager/infrastructure/database/` — существующая инфраструктура подключений
>
> Дата: 2026-07-19

---

## 0. Цель фазы

Реализовать два сценария подключения к PostgreSQL:

1. **Direct connection** — существующий режим: `host:port` + `username` + `password` (Fernet-encrypted).
2. **SSH tunnel** — новый режим: сначала SSH-подключение к jump-хосту (с парольной аутентификацией), затем подключение к PostgreSQL на этом хосте через SSH-туннель.

**Критерий «фаза готова»:** `db-pm` работает с обоими типами подключений; интеграционные тесты (testcontainers + SSH mock) зелёные.

---

## 1. Зафиксированные решения

| # | Решение |
|---|---------|
| Q1 | SSH-туннель через `sshtunnel` (Python-библиотека, обёртка над paramiko); локальный порт → remote forward |
| Q2 | Аутентификация по паролю (ssh_pass); SSH-ключи не используются |
| Q3 | `ConnectionConfig` расширяется с `connection_type: ConnectionType` enum + опциональный `ssh_tunnel` блок |
| Q4 | SSH-подключение устанавливается до PG-подключения; закрывается после |
| Q5 | Connection file хранит `ssh_pass` (зашифрованный) |
| Q6 | UI: dropdown `Connection Type` вместо checkbox/tab — масштабируемо для будущих типов |

---

## 2. Целевая структура (новые/изменяемые файлы)

```
src/db_project_manager/
├── domain/
│   └── connection.py                     # ИЗМЕНИТЬ: +SSH_TunnelConfig, расширить ConnectionConfig
├── infrastructure/
│   ├── database/
│   │   ├── base.py                       # ИЗМЕНИТЬ: +ssh_tunnel lifecycle
│   │   ├── postgres/
│   │   │   ├── adapter.py               # ИЗМЕНИТЬ: подключение через туннель
│   │   │   └── queries.py               # (без изменений)
│   │   └── ssh_tunnel.py                 # НОВОЕ: SSH tunnel manager
│   └── crypto.py                         # (без изменений)
└── presentation/
    └── gui/
        └── dialogs/
            └── connection_dialog.py      # ИЗМЕНИТЬ: +SSH tunnel tab

connections/
└── example.yaml                          # ИЗМЕНИТЬ: +SSH tunnel example

tests/
├── unit/
│   └── test_ssh_tunnel.py                # НОВОЕ: unit-тесты на SSH tunnel manager
└── integration/
    ├── conftest.py                       # ИЗМЕНИТЬ: +ssh fixture
    └── test_ssh_tunnel_e2e.py            # НОВОЕ: e2e SSH tunnel connection
```

---

## 3. Модель данных

### ConnectionType Enum:

```python
class ConnectionType(str, Enum):
    DIRECT = "direct"         # Direct PostgreSQL connection
    SSH_TUNNEL = "ssh_tunnel"  # PostgreSQL via SSH tunnel
```

### Расширение `ConnectionConfig` (domain/connection.py):

```python
class SSH_TunnelConfig(BaseModel):
    """SSH tunnel configuration."""
    ssh_host: str                          # Jump host IP (e.g., "192.168.1.100")
    ssh_port: int = 22                      # SSH port
    ssh_user: str                           # SSH username (e.g., "root")
    ssh_pass: Optional[str] = None          # Encrypted SSH password
    remote_bind_host: str = "127.0.0.1"    # PG host as seen from jump host
    remote_bind_port: int = 5432            # PG port as seen from jump host
    local_bind_port: int = 0                # 0 = auto-select

class ConnectionConfig(BaseModel):
    # ... существующие поля (host, port, database, username, password) ...
    connection_type: ConnectionType = ConnectionType.DIRECT
    ssh_tunnel: Optional[SSH_TunnelConfig] = None
    # Валидация: если connection_type == SSH_TUNNEL, то ssh_tunnel обязателен
```

### Connection file (YAML) — два сценария:

```yaml
# Scenario A: Direct connection (существующий)
connection_type: direct
host: localhost
port: 5432
database: mydb
username: myuser
password: crypto__ENVOS_CRYPTO_01__gAAAAABm...
type: postgres

---

# Scenario B: SSH tunnel
connection_type: ssh_tunnel
host: 127.0.0.1           # Игнорируется при connection_type=ssh_tunnel
port: 5432
database: mydb
username: myuser
password: crypto__ENVOS_CRYPTO_01__gAAAAABm...
type: postgres
ssh_tunnel:
  ssh_host: 192.168.1.100
  ssh_port: 22
  ssh_user: root
  ssh_pass: crypto__ENVOS_CRYPTO_01__gAAAAABm...
  remote_bind_host: 127.0.0.1
  remote_bind_port: 5432
```

---

## 4. SSH Tunnel Manager (`infrastructure/database/ssh_tunnel.py`)

```python
class SSHTunnelManager:
    """Manages SSH tunnel lifecycle using sshtunnel library."""
    
    def __init__(
        self,
        ssh_host: str,
        ssh_port: int,
        ssh_user: str,
        ssh_pass: str,                     # Расшифрованный пароль
        remote_bind_host: str = "127.0.0.1",
        remote_bind_port: int = 5432,
        local_bind_port: int = 0,
    ):
        ...

    def start(self) -> int:
        """Start tunnel, return local bind port."""
        # Использует sshtunnel.SSHTunnelForwarder
        # SSH аутентификация по паролю через paramiko
        # Выбирает свободный порт если local_bind_port=0
        # Возвращает actual local port (для подключения PG)
        
    def stop(self) -> None:
        """Stop and cleanup tunnel."""
        
    def is_active(self) -> bool:
        """Check if tunnel is alive."""
        
    def get_local_port(self) -> int:
        """Get the actual local bound port."""
```

**Passphrase handling:**
- `ssh_pass` расшифровывается через Fernet в `start()`.
- paramiko использует расшифрованный пароль для SSH-аутентификации.

---

## 5. Изменения в `DatabaseAdapter` и `PostgresAdapter`

### `base.py` (контракт):

```python
class DatabaseAdapter(ABC):
    # ... существующее ...

    def connect(self, conn_cfg: ConnectionConfig) -> None:
        if conn_cfg.connection_type == ConnectionType.SSH_TUNNEL:
            tunnel = SSHTunnelManager(
                ssh_host=conn_cfg.ssh_tunnel.ssh_host,
                ssh_port=conn_cfg.ssh_tunnel.ssh_port,
                ssh_user=conn_cfg.ssh_tunnel.ssh_user,
                ssh_pass=self._decrypt(conn_cfg.ssh_tunnel.ssh_pass),
                remote_bind_host=conn_cfg.ssh_tunnel.remote_bind_host,
                remote_bind_port=conn_cfg.ssh_tunnel.remote_bind_port,
                local_bind_port=conn_cfg.ssh_tunnel.local_bind_port,
            )
            self.connect_via_ssh_tunnel(tunnel)
        else:
            self._connect_direct(conn_cfg)
```

### `postgres/adapter.py`:

```python
class PostgresAdapter(DatabaseAdapter):
    def connect_via_ssh_tunnel(self, tunnel: SSHTunnelManager) -> None:
        # tunnel.start() → получить local_port
        # Подключаться к 127.0.0.1:<local_port>
        local_port = tunnel.start()
        self._connect(
            host="127.0.0.1",
            port=local_port,
            database=self._conn_cfg.database,
            username=self._conn_cfg.username,
            password=self._conn_cfg.password,
        )
        self._tunnel = tunnel  # сохраняем для close()
        
    def close(self) -> None:
        # Сначала закрываем PG-connection
        super().close()
        # Затем закрываем туннель если есть
        if self._tunnel:
            self._tunnel.stop()
            self._tunnel = None
```

---

## 6. Изменения в GUI connection dialog

**Connection Type Dropdown:**
```
Connection Type: [ Direct PG ▼ ]
                 [ Direct PG     ]
                 [ SSH Tunnel PG ]
```

- При выборе **"Direct PG"** — отображаются только стандартные PG-поля (host, port, database, username, password).
- При выборе **"SSH Tunnel PG"** — отображаются SSH-поля + стандартные PG-поля.

**SSH-поля (visible только при SSH Tunnel PG):**
- `ssh_host` text field (jump host IP)
- `ssh_port` spinbox (default 22)
- `ssh_user` text field
- `ssh_pass` password field
- `remote_bind_host` (default 127.0.0.1)
- `remote_bind_port` (default 5432)

**Обоснование dropdown vs checkbox/tab:**
- Более масштабируемо: легко добавить "MySQL", "Snowflake" и т.д.
- Явнее для пользователя — сразу понятно что есть разные типы.
- Future-proofing без изменения UI.

---

## 7. Порядок выполнения (S17–S20)

```
S17  Domain model — SSH_TunnelConfig + ConnectionConfig extension
       └─► S18  SSH Tunnel Manager (sshtunnel, password auth, lifecycle)
              └─► S19  PostgresAdapter via tunnel + CLI support
                     └─► S20  GUI tunnel tab + integration tests
```

---

## S17. Domain model — ConnectionType + SSH_TunnelConfig

**Файлы:** `domain/connection.py`, `tests/unit/test_connection.py`.

**Изменения `domain/connection.py`:**
- Добавить `ConnectionType` enum (`DIRECT`, `SSH_TUNNEL`).
- Добавить `SSH_TunnelConfig` pydantic model.
- Расширить `ConnectionConfig` полями `connection_type` и `ssh_tunnel`.
- Валидация: если `connection_type == SSH_TUNNEL`, то `ssh_tunnel` обязателен и `ssh_host`, `ssh_user`, `ssh_pass` заполнены.

**Тесты `test_connection.py`:**
- создание `SSH_TunnelConfig` с полным набором полей;
- создание `ConnectionConfig` с `connection_type=DIRECT` и `SSH_TUNNEL`;
- валидация: missing `ssh_tunnel` при `connection_type=SSH_TUNNEL` → ValidationError;
- serialization roundtrip через `model_dump()`.

**Чек-лист:**
- [ ] `ConnectionType` enum.
- [ ] `SSH_TunnelConfig` pydantic model.
- [ ] `ConnectionConfig` расширен с `connection_type` + `ssh_tunnel`.
- [ ] unit-тесты.
- [ ] ruff чист.

---

## S18. SSH Tunnel Manager

**Файлы:** `infrastructure/database/ssh_tunnel.py`, `tests/unit/test_ssh_tunnel.py`.

**`ssh_tunnel.py`:**
- `SSHTunnelManager` класс.
- `start()` — создаёт `SSHTunnelForwarder` с password authentication, запускает в threading.
- `stop()` — останавливает форвардер.
- `is_active()` — проверка.
- `get_local_port()` — возвращает локальный порт.

**`pyproject.toml`:** добавить `sshtunnel` в dependencies.

**Тесты `test_ssh_tunnel.py`:**
- unit-тесты без реального SSH (mock `SSHTunnelForwarder`).
- Проверка lifecycle (start/stop/is_active).
- Проверка параметров (host, port, user, pass передаются корректно).

**Чек-лист:**
- [ ] `SSHTunnelManager` с полным lifecycle.
- [ ] password authentication (paramiko).
- [ ] unit-тесты (mock).
- [ ] ruff чист.
- [ ] `sshtunnel` в dependencies.

---

## S19. PostgresAdapter via tunnel + CLI support

**Файлы:** `infrastructure/database/postgres/adapter.py`, `infrastructure/database/base.py`.

**Изменения в `base.py`:**
```python
def connect(self, conn_cfg: ConnectionConfig) -> None:
    if conn_cfg.connection_type == ConnectionType.SSH_TUNNEL:
        tunnel = SSHTunnelManager.from_config(conn_cfg.ssh_tunnel)
        self.connect_via_ssh_tunnel(tunnel)
    else:
        self._connect_direct(conn_cfg)
```

**Изменения в `adapter.py`:**
- Реализация `connect_via_ssh_tunnel()`.
- Сохранение `_tunnel` reference.
- `close()` — закрыть PG connection, затем tunnel.
- CLI: connection files с `ssh_tunnel` разделом работают без изменений (connection_config читает YAML).

**Тесты:**
- существующие тесты PostgresAdapter работают (direct mode unchanged).
- новые тесты: verify tunnel lifecycle на connect/close.

**Чек-лист:**
- [ ] `connect_via_ssh_tunnel()` реализация.
- [ ] `close()` корректно останавливает туннель.
- [ ] существующие тесты direct mode не сломаны.
- [ ] ruff чист.

---

## S20. GUI connection type dropdown + integration tests

**Файлы:** `presentation/gui/dialogs/connection_dialog.py`, `tests/integration/conftest.py`, `tests/integration/test_ssh_tunnel_e2e.py`, `connections/example.yaml`.

**GUI — Connection Type Dropdown:**
- Connection type dropdown (Direct PG / SSH Tunnel PG).
- При выборе SSH Tunnel PG — динамически показываются SSH-поля.
- Валидация: если выбран SSH Tunnel — ssh_host, ssh_user, ssh_pass обязательны.

**Connection file example (`connections/example.yaml`):**
- Добавить SSH tunnel scenario в comments.

**Integration tests (`test_ssh_tunnel_e2e.py`):**
- Требует SSH jumphost (реальный или mock).
- Проверка: tunnel start → PG query → tunnel stop.
- Skip если нет SSH credentials (label `ssh`).

**Чек-лист:**
- [ ] GUI connection type dropdown.
- [ ] Dynamic SSH fields visibility.
- [ ] Connection dialog валидация (SSH fields required when tunnel selected).
- [ ] `example.yaml` обновлён с SSH tunnel example.
- [ ] integration tests (или skip без jumphost).
- [ ] ruff чист.

---

## 8. Метрики приёмки Phase 3

- [x] `ConnectionConfig` с `ssh_tunnel` сериализуется/десериализуется корректно.
- [x] `SSHTunnelManager.start()` возвращает локальный порт.
- [x] `PostgresAdapter` подключается через SSH туннель (e2e с real SSH).
- [x] Connection dialog позволяет ввести SSH tunnel параметры.
- [x] Direct connection без изменений работает как раньше.
- [x] Все unit-тесты зелёные (`uv run pytest tests/unit`).
- [x] ruff чист.

---

## 9. Реализованные решения (актуальные)

| # | Решение | Статус |
|---|---------|--------|
| Q1 | paramiko 5.x напрямую (НЕ sshtunnel — несовместим с paramiko 5.x) | ✅ |
| Q2 | Аутентификация по паролю (ssh_pass); SSH-ключи не используются | ✅ |
| Q3 | `ConnectionConfig` расширяется с `connection_type: ConnectionType` enum + опциональный `ssh_tunnel` блок | ✅ |
| Q4 | SSH-подключение устанавливается до PG-подключения; закрывается после | ✅ |
| Q5 | Connection file хранит `ssh_pass` (зашифрованный) | ✅ |
| Q6 | UI: dropdown `Connection Type` вместо checkbox/tab — масштабируемо | ✅ |

---

## 10. Известные баги и исправления

### Bug: Зависание PG-подключения через SSH туннель (FIXED)
- **Симптом:** Туннель поднимается, но PG-клиент зависает после отправки startup-пакета
- **Корневая причина:** `_pipe` передавал данные только в одну сторону (client → channel)
- **Ответ сервера** (channel → client) **никто не читал и не писал обратно в сокет**
- **Исправление:** `_pipe` переписан в full-duplex с двумя тредами:
  - `_pipe_client_to_channel` — читает от PG-клиента, пишет в SSH-канал
  - `_pipe_channel_to_client` — читает из SSH-канала, пишет в PG-клиент

---

## 11. Риски и смягчения

| Риск | Смягчение |
|------|-----------|
| SSH library compatibility | paramiko 5.x напрямую (sshtunnel несовместим) |
| Password in memory | Fernet decryption happens once on start; password not stored long-term |
| Tunnel fails mid-query | PostgresAdapter.close() гарантирует tunnel cleanup |
| No real SSH for testing | Интеграционные тесты требуют реального SSH jumphost |

---

## 12. Порядок коммитов (рекомендуемый)

1. `feat(domain): SSH_TunnelConfig + ConnectionConfig extension (S17)`
2. `feat(ssh): SSHTunnelManager with lifecycle + password auth (S18)`
3. `feat(db): PostgresAdapter via SSH tunnel (S19)`
4. `feat(gui): SSH tunnel tab in connection dialog (S20)`
5. `test(integration): SSH tunnel e2e tests (S20)`
6. `docs: update example.yaml with SSH tunnel scenario (S20)`

---

## 13. Следующие шаги (за пределами Phase 3)

- Интеграционные тесты с реальным SSH (testcontainers + SSH mock)
- Реализация миграций на БД с данными (diff, ALTER-план)
- pre/post-deploy hooks
