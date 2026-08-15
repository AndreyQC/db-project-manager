# Phase 3 Vision Draft — SSH-туннельные подключения

> Статус: **черновик** — требует решения открытых вопросов
> Дата: 2026-07-19

---

## 1. Зачем SSH tunnel?

**Сценарий:** база данных PostgreSQL недоступна напрямую из локальной сети. Доступ возможен только через jump-сервер по SSH.

**Без SSH tunnel:** пользователь вручную делает `ssh -L 5433:localhost:5432 bastion.host` и работает через локальный порт 5433.

**С db-pm:** connection file содержит SSH-параметры; db-pm автоматически поднимает туннель перед подключением к PG и закрывает его после.

---

## 2. Открытые вопросы (требуют решения)

| # | Вопрос | Варианты |
|---|--------|----------|
| Q1 | SSH-библиотека | `sshtunnel` (pure Python) vs `paramiko` напрямую vs `subprocess ssh` |
| Q2 | SSH-аутентификация | Пароль (password) |
| Q3 | Local bind port | Фиксированный vs auto-select (0) |
| Q4 | Lifecycle ownership | Tunnel создаётся в `connect()` и живёт до `close()` |
| Q5 | UI для типа подключения | Dropdown (Direct / SSH Tunnel) — масштабируемо |

---

## 3. Фиксированные решения

### SSH-аутентификация — пароль

```
host - host_ip          # Jump host IP
user - root             # SSH username
port - 22               # SSH port
pass - password         # SSH password (encrypted, same as PG password)
```

### Приоритет: Нет ключей, только пароль

- `ssh_pass` — зашифрованный пароль (Fernet, как PG password).

### Q1: sshtunnel
- Простая библиотека, обёртка над `paramiko`.
- `SSHTunnelForwarder` — thread-based, удобно.

### Q3: Auto-select
- `local_bind_port=0` → `sshtunnel` выбирает свободный порт.
- Предсказуемость не критична (PG host всегда 127.0.0.1).

---

## 4. Доменная модель (предварительно)

```python
class ConnectionType(Enum):
    DIRECT = "direct"         # Direct PostgreSQL connection
    SSH_TUNNEL = "ssh_tunnel" # PostgreSQL via SSH tunnel

class SSH_TunnelConfig(BaseModel):
    ssh_host: str                          # Jump host IP
    ssh_port: int = 22
    ssh_username: str                      # SSH username (e.g., "root")
    ssh_password: Optional[str] = None    # Encrypted SSH password
    remote_bind_host: str = "127.0.0.1"   # PG host as seen from jump host
    remote_bind_port: int = 5432          # PG port as seen from jump host
    local_bind_port: int = 0              # 0 = auto-select

class ConnectionConfig(BaseModel):
    connection_type: ConnectionType = ConnectionType.DIRECT
    # PG connection fields (host, port, database, username, password)...
    ssh_tunnel: Optional[SSH_TunnelConfig] = None  # only if connection_type == SSH_TUNNEL
```

---

## 4a. UI — Connection Type Dropdown

**Dropdown в GUI connection dialog:**

```
Connection Type: [ Direct PG ▼ ]
                 [ Direct PG     ]
                 [ SSH Tunnel PG ]

```

- При выборе **"Direct PG"** — отображаются стандартные PG-поля (host, port, database, username, password).
- При выборе **"SSH Tunnel PG"** — отображаются SSH-поля + стандартные PG-поля.

**Обоснование dropdown vs checkbox:**
- Более масштабируемо: легко добавить "MySQL", "Snowflake", "SSH + Kerberos" и т.д.
- Явнее для пользователя — сразу понятно что есть разные типы подключений.
- Позволяет future-proofing без изменения UI.

---

## 5. Workflow

```
User: db-pm deploy validate --connection mydb.yaml --dir ./sql
  │
  ├─► ConnectionConfig.load("mydb.yaml")
  │     ├─► ssh_tunnel.enabled == False
  │     │     └─► connect(host, port, user, password)
  │     │
  │     └─► ssh_tunnel.enabled == True
  │           ├─► SSHTunnelManager(ssh_tunnel).start() → local_port
  │           ├─► connect("127.0.0.1", local_port, user, password)
  │           │
  │           └─► (на close) → disconnect PG → tunnel.stop()
```

---

## 6. Что не входит в Phase 3

- [ ] SSH-ключи (только пароль)
- [ ] ProxyJump / Bastion chain (пока один hop)
- [ ] SSH-agent forwarding
- [ ] Connection pooling
- [ ] SSH tunnel через менеджер паролей (1Password, etc.)

---

## 7. Следующие шаги

1. Зафиксировать ответы на Q1–Q4 (этот документ или отдельный PR).
2. Создать `001_plan_phase_03.md`.
3. Начать с S17 (domain model).
