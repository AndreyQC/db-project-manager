# Phase 10: CD Foundation

> **Дата:** 2026-08-14
> **Статус:** завершена
> **План:** `_tasks_/phase_10/Phase_10_plan.md`
> **Результат:** `_tasks_/phase_10/Phase_10_result.md`
> **Норматив-дизайн:** `_tasks_/phase_10/Phase_10_vision_final.md`

---

## 1. Цель фазы

Построить **фундамент controlled-deployment** (CD-ядра 10→13): служебную схему
`__deploy`, calver-версионирование и идемпотентный pre/post runner как
**механику**, тестируемую в рамках validate-flow. Real-target deploy, safety-gate
и ALTER оставлены на Phase 11/12.

Зависимости: Phase 8 (✓ done — overload resolution чинит топосорт деплоя).
Развязка для Phase 11 (Safety Gate).

## 2. Что сделано

| Категория | Файл | Что изменилось |
|-----------|------|----------------|
| Domain | `domain/deploy.py` (новый) | `CALVER_RE`, `validate_calver`, `calver_seed`, `ScriptRecord`, `canonical_normalize`, `script_checksum` |
| Domain | `domain/diff.py` | `CodebaseManifest.source_version` (calver, required при v2) |
| Domain | `domain/graph.py` | `Vertex.immutable: bool = False` (additive) |
| Infra: SQL | `infrastructure/sql/autodoc.py` | `strip_autodoc` (public, вынесен из deploy_service); `build_metadata`/`ensure_header` + `immutable` |
| Infra: templates | `infrastructure/templates/deploy/{3 файла}.sql.j2` | seed-шаблоны `__deploy`-таблиц |
| Infra: deploy | `infrastructure/deploy/canonical_ddl.py` (новый) | `canonical_deploy_ddl`, `canonical_deploy_checksums`, `validate_deploy_ddl` (warning-only) |
| Infra: DB | `infrastructure/database/base.py` | +4 abstract methods (Phase 10 section) |
| Infra: DB | `infrastructure/database/postgres/{adapter,queries}.py` | PG-реализация (UPSERT+транзакция для `record_script_execution`) |
| Infra: config | `infrastructure/config/app_config.py` | `DeployConfig.service_schema` |
| Infra: config | `infrastructure/config/codebase_manifest.py` | `MANIFEST_FORMAT_VERSION=2`, strict read/write |
| Infra: parsing | `infrastructure/parsing/pg_sql_parser.py` | `project.immutable` → Vertex; `__migrations` в `_SKIP_DIRS` |
| Application | `application/deploy_service.py` | расширенный `run()` (validate-presence, canonical-warning, split-loop, pre/post-runner, record version) |
| Application | `application/script_runner.py` (новый) | `ScriptRunner.run_phase`, 5-way decision |
| Application | `application/reverse_engineer.py` | `_seed_or_sync_deploy` — замыкает цикл БД ↔ codebase |
| Fixture | `tests/fixtures/codebase_sample/` | +`__deploy/`, +`__migrations/{pre,post}/`, +`dbpm.manifest.json` v2 |

Ключевые коммиты: `3d5694c` (S1) → `15002ad` (S8+S9) → `1a3afb9` (S10 integration).

## 3. Ключевые архитектурные решения

- **`__deploy` — обычная схема в кодовой базе** (CDF-10), не adapter-magic.
  Reverse-engineer seed'ит из canonical templates; deploy применяет через
  `EARLY_DDL_TYPES`. `immutable: true` в autodoc `project` (omit при false).
- **Calver `YYYY.MM.DD.NN` в manifest** (CDF-2/9). Git полностью убран из
  versioning. Версия explicit, MR-controlled.
- **State + history разделены** (CDF-11): `script_history` (PK name+type,
  UPSERT, для runner lookup'а) и `script_audit_log` (append-only, с
  `deploy_version`/`deploy_source` для отчётов Phase 13).
- **Checksum без autodoc** (CDF-6) — `script_checksum(strip_autodoc(text))`.
  Унифицировано для pre/post и canonical-DDL-валидации.
- **5-way runner decision**: new → EXECUTE; same+success → SKIP;
  same+failed → ERROR; **different+failed → EXECUTE** (retry);
  different+success → ERROR (CD-4).
- **Split deploy loop**: `__deploy` schema+tables применяются ПЕРВЫМИ (до
  pre-runner, которому нужна `script_history`).
- **Canonical-DDL-валидация: warning-only** (CDF-10 подход b).

## 4. Проверки

```bash
unset SSL_CERT_FILE REQUESTS_CA_BUNDLE CURL_CA_BUNDLE && uv run pytest tests/unit/ -q
# 609 passed (516 baseline + 93 Phase 10)
uv run ruff check src/ tests/
# All checks passed!
uv run pytest -m integration  # опционально, требует Docker Desktop
# +4 Phase 10 e2e на реальном PG (testcontainers)
```

## 5. Известные ограничения / NOT done

- **Real-target deploy** (deploy в существующую БД, не temp) — Phase 11+.
- **Safety-gate** (pre-analysis, оценка данных) — Phase 11 (CD-6..CD-10).
- **Структурный column-diff + ALTER-план** — Phase 12.
- **`script_watermark`** — отложено (CDF-5).
- **CLI `--reseed-deploy`** для RE — backlog.
- **Жёсткая canonical-DDL-валидация** (hard-block) — backlog.
- **Фильтр служебных схем в compare** — backlog.

## 6. Где читать дальше

- `_tasks_/phase_10/Phase_10_result.md` — результат (детально, коммиты).
- `_tasks_/phase_10/Phase_10_vision_final.md` — нормативный дизайн (CDF-1..11).
- `_checkpoints_/20260814_001_checkpoint.md` — текущее состояние проекта.
- `_tasks_/ROADMAP.md` §2 (порядок), §4 (CD-1..CD-5), §9 (Q3/Q4/Q5 закрыты).
- `LESSONS_LEARNED.md` §12, §18, §19, §23, §28, §32, §34, §35, §45.
