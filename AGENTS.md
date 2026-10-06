# AGENTS.md


Инструкция для AI-агентов, работающих в этом репозитории. Контекст сессии
начинать отсюда: `_docs_/REFRESH_CONTEXT.md` → последний чекпойнт
(`ls _docs_/_checkpoints_/`).


## Commands

```bash
# Setup
uv sync

# Development
uv run pytest               # unit tests only (integration skipped by default)
uv run pytest -m integration # integration tests (requires Docker)
uv run ruff check .         # linter

# Run CLI/GUI/MCP
uv run db-pm --help
uv run db-pm-gui
uv run db-pm-mcp        # MCP-сервер (stdio); требует: uv sync --extra mcp
```

## Critical: TLS Proxy

If `uv sync` fails with `invalid peer certificate: UnknownIssuer` (corporate proxy):
```bash
unset SSL_CERT_FILE REQUESTS_CA_BUNDLE CURL_CA_BUNDLE && uv sync
```

## Required Environment

- `ENVOS_CRYPTO_01` — Fernet key for connection password encryption. Generate once:
  ```bash
  uv run db-pm crypto keygen
  # encrypt a secret for connections/*.yaml (prints crypto__ENV__token):
  echo 'secret' | uv run db-pm crypto encrypt ENVOS_CRYPTO_01
  ```

## Package Structure

`src/db_project_manager/`
- `domain/` — models (Vertex, Edge, DeltaPlan, etc.)
- `infrastructure/` — DB adapters, queries, diff, deploy logic
- `application/` — services (ReverseEngineer, GraphService, DeltaService, DeployApplyService, SafetyGateService, MCPQueryService)
- `presentation/` — CLI (`cli/`), GUI (`gui/`), MCP server (`mcp/`, Phase 19)

## Files Outside Git

- `connections/` — connection configs (credentials encrypted)
- `config.yaml` — app config
- `.dbm_graph/` — rebuilt from codebase by `db-pm graph build`

## Integration Tests

Marked `@pytest.mark.integration`, skipped by default. Require Docker Desktop running.

## Conventions

- ruff line-length: 120, indent: 4
- Docstyle: Google (see pyproject.toml for ignores)
- Use `shlex.split(cmd, posix=False)` for Windows path handling
- Always qualify SQL identifiers (schema.name) — bare names break on different search_path
- See `LESSONS_LEARNED.md` for patterns to avoid
- See `_docs_/_checkpoints_/latest_checkpoint.md` for current status

## Next Phase

Phase 17 — post-deploy отчёты (CD-16..19). Phase 18 (deploy reset — сброс
пользовательских схем) реализована 2026-09-17 до Phase 17 по решению
пользователя: `_docs_/_tasks_/2026-09-17/20260917_001_deploy_reset_final.md`.
Phase 19 (MCP-сервер `db-pm-mcp`) — план `_docs_/_tasks_/2026-09-27/20260927_001_mcp_server_plan.md`.
