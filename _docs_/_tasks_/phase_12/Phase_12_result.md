# Phase 12: ALTER + Delta — результат

> **Дата:** 2026-08-16
> **Ветка:** dev
> **Статус:** завершена, все шаги S1..S9 (12 код-коммитов + 3 docs-коммита vision/plan)
>
> Контекст:
> - `_tasks_/phase_12/Phase_12_vision_final.md` — нормативный дизайн (ALT-1..ALT-8)
> - `_tasks_/phase_12/Phase_12_plan.md` — план S1..S9
> - `_checkpoints_/20260815_001_checkpoint.md` — состояние до фазы

## Что сделано

Пользовательский итог: `db-pm deploy plan` (dry-run: gate + классификация
safe/needs-pre/blocked + артефакты `delta/NNN_*.sql`, `plan.json/md`) и
`db-pm deploy apply` — первая в проекте **мутирующая живую БД** команда:
репетиция на temp-аналоге (RE таргета → deploy → seed) → полный пайплайн против
таргета (gate → pre → CD-11 → дельта → post → версия `source='apply'`).
Column-level diff (CD-ALT-1) построен на SQL-теле как единственном источнике
правды (ALT-1b, code-first safe). Ноль новых abstract-методов адаптера (§45).

| Шаг | Коммит | Что |
|-----|--------|-----|
| S1 | `d76d804` | `domain/delta.py` (ColumnSnapshot/Diff, PlannedOperation, DeltaPlan) + columns-поля в diff-моделях |
| S2 | `9357e2f` | `infrastructure/diff/columns.py`: `extract_columns` (sqlglot, синонимы типов) + snapshot-интеграция; hash-инвариант тестом |
| S3 | `3a22729` | `diff_columns` + comparator: `column_diffs`/`columns_unavailable` (CD-ALT-1) |
| S4 | `59c0900` | `infrastructure/deploy/alter_plan.py`: classify (матрица ALT-3) + `render_alter` (whitelist §19, fully-qualified §35) |
| S5 | `4912745` | `application/delta_service.py` + `deploy/plan_report.py`: план по `deploy_order`, артефакты, plan.json/md (CD-12/13) |
| S6 | `d642c47` | `application/deploy_apply_service.py`: `_run_pipeline` + репетиция + seed (CD-11/14/15, ALT-5/8) |
| S7 | `477dfab` | CLI `deploy plan` / `deploy apply` (exit 0/1/2, флаги) |
| S8-fix | `8bc2056` | NULL-маркер RE-DDL = nullable (sqlglot `allow_null`, §50) |
| S8-fix | `e252ed9` | `_validate_deploy_presence` принимает префиксные имена `__deploy`-таблиц |
| S8-fix | `2fd68f4` | compare игнорирует catalog-сегмент object_key (§51; репетиция/cross-env) |
| S8 | `99d00e9` | gate-residual даунгрейд по классификации ALT-3; plan-режим пишет BLOCKED (CLI exit 1) |
| S8 | `f55b5a7` | integration e2e — 8 сценариев (репетиция, CD-11, retry, seed) |

Docs: `22d6de4`/`d2a88c6`/`ab53da3` (draft + закрытие ALT-1..8), `cc9a38b`
(vision final), `f24162c` (plan), `275935e`/`996cfa0`/`a7bcb8f`/`f8533d3`
(README/ROADMAP/BACKLOG/lessons).

## Отклонения от плана

1. **S6:** вместо рефактора `DeployValidateService.deploy_into_new_db` репетиция
   переиспользует `run(keep_db=True)` целиком — RE-кодовая база таргета не
   содержит `__migrations`, pre/post-фазы валидейта в ней — no-op; дроп
   репетиционной БД делает apply. Нулевой риск дрейфа поведения валидейта.
2. **S8 (вне плана):** gate-residual даунгрейд — конфликт табличного gate Phase 11
   с матрицей ALT-3 (safe-alter при данных) разрешён перепроверкой gate-нарушений
   классификацией его же diff-отчёта (§52). Отсутствие отчёта = все нарушения
   действуют (fail-safe).
3. **S8 (вне плана):** `deploy plan` не падает на опасной дельте, а пишет
   артефакты с BLOCKED-операциями (CD-13 review) и даёт exit 1; apply
   реджектится до мутаций.
4. Три продуктовых фикса S8 (NULL-маркер, имена `__deploy`, catalog-identity) —
   «стек причин» §52, каждый отдельным коммитом с регрессией.
5. README «Возможности» не расширялся (раздел CLI исчерпывающ; свод — в
   `_phases_/Phase_12.md`).

## Известные ограничения / NOT done

- **Структурный diff констрейнтов/индексов/partitioning** — unrepresented →
  needs-pre (ALT-2); BACKLOG.
- **Comment-level diff** — `normalize_sql` берёт первый statement → comment-only
  правки = UNCHANGED; BACKLOG (multi-statement normalize, смена хэша).
- **Column rename** — детектируется как drop+add (осознанно, ALT-3).
- **Standalone `deploy analyze`** остался table-level (без ALT-3 даунгрейда) —
  BACKLOG P3 на синхронизацию.
- **Greenplum** — распределённые ALTER не валидировались (нет кластера);
  presence уже fail-safe.
- **AI-трек** (CD-AI-1/2) — prerequisite закрыт (column-diff), реализация —
  overlay.
- **GUI plan/apply + рендер плана** — BACKLOG P3. **Post-deploy отчёты/история**
  (CD-16..18) — Phase 13.

## Проверки

```bash
unset SSL_CERT_FILE REQUESTS_CA_BUNDLE CURL_CA_BUNDLE && uv run pytest tests/unit/ -q
# 846 passed (700 baseline + 146 Phase 12)
uv run ruff check src/ tests/
# All checks passed!
uv run pytest -m integration   # Docker требуется
# 24 passed (16 baseline + 8 Phase 12 e2e)
```

Новые тест-файлы: `test_delta_domain.py` (+16), `test_extract_columns.py` (+27),
`test_column_diff.py` (+18), `test_alter_plan.py` (+51), `test_delta_service.py`
(+9), `test_deploy_apply_service.py` (+12), `test_deploy_plan_apply_cli.py`
(+10), `test_deploy_plan_apply_e2e.py` (+8 integration); расширены
`test_comparator.py`, `test_deploy_service.py`, `test_extract_columns.py`.

## Чеклист по урокам (final §7) — закрыт

- ✓ §3 (presence без COUNT), §12 (`git add --`), §19 (whitelist-кавычки),
  §23 (strip_autodoc), §26/§28 (roundtrip), §31 (mkdir), §34/§35
  (fully-qualified контракт-тесты), §44 (sqlglot smoke до кода), §45 (ноль
  новых abstract-методов; FakeApplyAdapter с полным контрактом), §46
  (обязательные typer-опции первыми), §49 (`has_executable_sql` skip),
  §48-порядок (version/RE в e2e).
- ✓ Hash-инвариант: `normalize_sql`/`sql_hash` не менялись (эталонные хэши
  фиксстур захардкожены тестом).

## Новые уроки

- **§50** — sqlglot: явный NULL-маркер = `NotNullColumnConstraint(allow_null=True)`.
- **§51** — identity без имени БД (catalog-сегмент object_key).
- **§52** — e2e-стек причин; продуктовые фиксы отдельными коммитами;
  gate-даунгрейд через классификацию.

## Где читать дальше

- `_phases_/Phase_12.md` — свод фазы
- `_tasks_/phase_12/Phase_12_vision_final.md` — нормативный дизайн (ALT-1..8)
- `_tasks_/ROADMAP.md` §2 (шаг 5 — Phase 13), §6 (AI-трек, prerequisite закрыт)
- `_tasks_/BACKLOG.md` — новые P3 по итогам фазы
