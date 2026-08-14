# Phase 10: CD Foundation — результат

> **Дата:** 2026-08-14
> **Ветка:** dev
> **Статус:** завершена
>
> Норматив-дизайн: `-=tasks=-/phase_10/Phase_10_vision_final.md`.
> Предшественник (история обсуждения): `-=tasks=-/phase_10/Phase_10_vision_draft.md`.
> План: `-=tasks=-/phase_10/Phase_10_plan.md`.
>
> Контекст: чекпойнт `20260804_001`; ROADMAP §2/§4/§7/§8/§9; Phase_02.md;
> LESSONS §12, §18/§45, §19, §23, §28, §32, §34/§35.

---

## 1. Что сделано

Сделан **фундамент controlled-deployment** (CD-ядра 10→13): служебная схема
`__deploy`, calver-версионирование через manifest, идемпотентный pre/post
runner. Реализована в рамках validate-flow (temp-БД получает `__deploy` →
end-to-end → удаляется); real-target deploy оставлен на Phase 11+.

### Пошаговые коммиты

| Шаг | Коммит | Что |
|-----|--------|-----|
| **S1** | `3d5694c` | `domain/deploy.py`: calver, ScriptRecord, canonical_normalize, script_checksum. Рефакторинг `strip_autodoc` → public в `autodoc.py`. |
| **S2** | `018d2f1` | `CodebaseManifest.source_version` (calver, required при v2); `MANIFEST_FORMAT_VERSION=2`; read/write валидация. Минимальный seed в RE-flow. |
| **S3** | `df2e409` | `Vertex.immutable: bool = False`; `build_metadata`/`ensure_header` принимают `immutable=False`; парсер читает `project.immutable`. |
| **S4** | `c09f06e` | `DatabaseAdapter` +4 abstract methods (без `ensure_deploy_schema`); PG-реализация (UPSERT+транзакция для `record_script_execution`); 3 fakes обновлены. |
| **S5** | `ea48fbe` | 3 Jinja-шаблона в `infrastructure/templates/deploy/`; `canonical_ddl.py` — render, checksums, `validate_deploy_ddl` (warning-only, CDF-6 invariant). |
| **S6** | `9321f6a` | `ReverseEngineerService._seed_or_sync_deploy`: при RE без `__deploy` — seed из canonical templates; при наличии — sync `source_version` из `schema_version`. `DeployConfig.service_schema` configurable. |
| **S7** | `825c79b` | `application/script_runner.py`: `ScriptRunner.run_phase`, 5-way decision, state UPSERT + audit INSERT, `ScriptExecutionError`, continue_on_error. |
| **S8+S9** | `15002ad` | `DeployValidateService.run()` расширен Phase 10 механикой (validate-presence, canonical-warning, split-loop, pre/post-runner, record version). Fixture обновлён (`__deploy/`, `__migrations/`, manifest v2). Parser: `__migrations` в `_SKIP_DIRS`. |
| **S10** | `1a3afb9` | Integration-тесты (`@pytest.mark.integration`, testcontainers): 4 кейса на реальном PG. |

### Файлы (по категориям)

**Domain (новые)**:
- `src/db_project_manager/domain/deploy.py` — calver, ScriptRecord, checksum/normalize helpers.

**Domain (расширены)**:
- `src/db_project_manager/domain/diff.py` — `CodebaseManifest.source_version`.
- `src/db_project_manager/domain/graph.py` — `Vertex.immutable`.

**Infrastructure**:
- `src/db_project_manager/infrastructure/sql/autodoc.py` — `strip_autodoc` (public), `build_metadata`/`ensure_header` + `immutable`.
- `src/db_project_manager/infrastructure/sql/templates/deploy/` — 3 seed-шаблона (`schema_version`, `script_history`, `script_audit_log`).
- `src/db_project_manager/infrastructure/deploy/` (новый пакет) — `canonical_ddl.py`.
- `src/db_project_manager/infrastructure/database/base.py` — +4 abstract methods (Phase 10 section).
- `src/db_project_manager/infrastructure/database/postgres/{adapter.py, queries.py}` — PG-реализация (5 SQL-констант, `_quote_identifier`, `record_script_execution` через `engine.connect().execution_options(READ_COMMITTED)`).
- `src/db_project_manager/infrastructure/config/app_config.py` — `DeployConfig.service_schema`.
- `src/db_project_manager/infrastructure/config/codebase_manifest.py` — `MANIFEST_FORMAT_VERSION=2`, валидация.
- `src/db_project_manager/infrastructure/parsing/pg_sql_parser.py` — `project.immutable` → Vertex; `__migrations` в `_SKIP_DIRS`.

**Application**:
- `src/db_project_manager/application/deploy_service.py` — расширенный `run()` с Phase 10 циклом.
- `src/db_project_manager/application/script_runner.py` (новый) — pre/post runner.
- `src/db_project_manager/application/reverse_engineer.py` — `_seed_or_sync_deploy`, `_seed_deploy_files`, `_mark_immutable`.

**Tests (новые)**:
- `tests/unit/test_deploy_domain.py` (35) — calver, ScriptRecord, normalize/checksum.
- `tests/unit/test_canonical_ddl.py` (16) — canonical DDL + validator.
- `tests/unit/test_script_runner.py` (15) — pre/post runner.
- `tests/integration/test_deploy_phase10_e2e.py` (4, `@pytest.mark.integration`) — e2e на реальном PG.

**Tests (расширены)**:
- `tests/unit/test_codebase_manifest.py` — +6 (v2 validation).
- `tests/unit/test_autodoc.py` — +8 (immutable).
- `tests/unit/test_deploy_service.py` — +5 (Phase 10 integration), `DeployFakeAdapter` +Phase 10 state.
- `tests/unit/test_reverse_engineer.py` — +8 (seed/sync), `FakeAdapter._schema_version`.
- `tests/unit/test_compare_cli.py`, `test_compare_service.py`, `test_snapshot.py` — assertions под обновлённый fixture.

**Fixture**:
- `tests/fixtures/codebase_sample/__deploy/{schema __deploy.sql, tables/{3}.sql}` — canonical DDL с `immutable:true`.
- `tests/fixtures/codebase_sample/__migrations/{pre,post}/2026-08-11_001_*.sql` — идемпотентные скрипты.
- `tests/fixtures/codebase_sample/dbpm.manifest.json` — v2 с `source_version`.

## 2. Проверки

```bash
unset SSL_CERT_FILE REQUESTS_CA_BUNDLE CURL_CA_BUNDLE && uv run pytest tests/unit/ -q
# 609 passed (516 baseline + 93 Phase 10)
# (test_connection_store::test_save_load_roundtrip — известный flaky BACKLOG P3,
#  в изоляции зелёный)
uv run ruff check src/ tests/
# All checks passed!
```

Integration (требует Docker Desktop, по умолчанию пропущены):
```bash
uv run pytest -m integration
# 4 Phase 10 e2e (testcontainers) + существующие Phase 2 integration
```

## 3. Ключевые архитектурные решения

- **`__deploy` — обычная схема в кодовой базе** (CDF-10), не adapter-magic.
  Применяется через стандартный `EARLY_DDL_TYPES` flow; seed'ится при RE
  из canonical templates. `immutable: true` в autodoc (omit при false).
- **`source_version` calver `YYYY.MM.DD.NN`** в manifest, required при v2
  (CDF-2/9). Git полностью убран из versioning — версия explicit, MR-controlled.
- **`script_history` state + `script_audit_log` history** (CDF-11): первая —
  PK `(name, type)` UPSERT, для runner lookup'а; вторая — append-only с
  `deploy_version`/`deploy_source` для отчётов Phase 13.
- **Checksum без autodoc** (CDF-6) — `script_checksum(strip_autodoc(text))`.
  Унифицировано для pre/post и canonical-DDL-валидации.
- **5-way runner decision** (vision §4.4 + refinement): new → EXECUTE;
  same+success → SKIP; same+failed → ERROR; **different+failed → EXECUTE**
  (retry с фиксом); different+success → ERROR (CD-4).
- **Split deploy loop** — `__deploy` schema+tables применяются ПЕРВЫМИ (до
  pre-runner, которому нужна `script_history`).
- **Canonical-DDL-валидация: warning-only** (CDF-10 подход b). Mismatch не
  блокирует deploy, ловит рассинхрон раньше runtime.

## 4. NOT done / отложено

- **Real-target deploy** (deploy в существующую БД, не temp) — **Phase 11+**.
- **Safety-gate** (pre-analysis, оценка данных) — **Phase 11** (CD-6..CD-10).
- **Структурный column-diff + ALTER-план** — **Phase 12** (CD-ALT-*).
- **`script_watermark`** — отложено (CDF-5).
- **CLI `--reseed-deploy`** для RE (canonical-mismatch fallback) — backlog.
- **Жёсткая canonical-DDL-валидация** (hard-block вместо warning) — backlog.
- **Фильтр служебных схем в compare** — backlog.
- **GUI для истории версий/скриптов** (CD-18) — Phase 18.

## 5. Отклонения от плана

- **`canonical_normalize`/`script_checksum` в domain — без strip autodoc**
  (план говорил strip внутри). Решено: domain остаётся pure, caller стрипает
  через `strip_autodoc`. Зафиксировано тестом
  `test_script_checksum_invariant_under_autodoc_change`.
- **S8 + S9 объединены** в один коммит: strict-проверка `__deploy` (S8)
  ломала regression без fixture update (S9). Объединение даёт atomic commit.
- **5-way decision вместо 4-way** (S7): добавлена ветка «different checksum +
  previous failure → EXECUTE» — иначе легитимный «починил и retry» упирался в
  ERROR. Зафиксировано в docstring `_decide`.

## 6. Где читать дальше

- `-=tasks=-/phase_10/Phase_10_vision_final.md` — нормативный дизайн (CDF-1..11).
- `-=tasks=-/phase_10/Phase_10_vision_draft.md` — история обсуждения.
- `-=tasks=-/phase_10/Phase_10_plan.md` — пошаговый план S1..S10.
- `-=PHASES=-/Phase_10.md` — свод фазы.
- `-=CHECKPOINTS=-/20260814_001_checkpoint.md` — текущее состояние проекта.
- `-=tasks=-/ROADMAP.md` §2 (порядок), §4 (CD-1..CD-5), §9 (Q3/Q4/Q5 закрыты).
- `LESSONS_LEARNED.md` §12, §18, §19, §23, §28, §32, §34, §35, §45.
