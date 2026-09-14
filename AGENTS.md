# AGENTS.md

## Commands

```bash
# Setup
uv sync

# Development
uv run pytest               # unit tests only (integration skipped by default)
uv run pytest -m integration # integration tests (requires Docker)
uv run ruff check .         # linter

# Run CLI/GUI
uv run db-pm --help
uv run db-pm-gui
```

## Critical: TLS Proxy

If `uv sync` fails with `invalid peer certificate: UnknownIssuer` (corporate proxy):
```bash
unset SSL_CERT_FILE REQUESTS_CA_BUNDLE CURL_CA_BUNDLE && uv sync
```

## Required Environment

- `ENVOS_CRYPTO_01` — Fernet key for connection password encryption. Generate once:
  ```bash
  uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  ```

## Package Structure

`src/db_project_manager/`
- `domain/` — models (Vertex, Edge, DeltaPlan, etc.)
- `infrastructure/` — DB adapters, queries, diff, deploy logic
- `application/` — services (ReverseEngineer, GraphService, DeltaService, DeployApplyService, SafetyGateService)
- `presentation/` — CLI (`cli/`) and GUI (`gui/`)

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

Phase 13 — Post-deploy + отчёты (CD-16..19)