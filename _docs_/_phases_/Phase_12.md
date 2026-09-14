# Phase 12: ALTER + Delta (`deploy plan` / `deploy apply`)

> **Дата:** 2026-08-16
> **Статус:** завершена
> **План/дизайн:** `_tasks_/phase_12/Phase_12_vision_final.md` (решения ALT-1..ALT-8)
> **Результат:** `_tasks_/phase_12/Phase_12_result.md`
> **Коммиты:** `d76d804`…`f55b5a7` (12 код-коммитов, S1..S8 + 3 фикса)

## Цель фазы

1. Структурный column-level diff (CD-ALT-1): из «хэши различаются» — к списку
   изменений уровня колонок.
2. ALTER-план с классификацией safe / needs-pre / blocked (CD-ALT-2..4,
   консервативная матрица).
3. Артефакты дельты для review: `delta/NNN_*.sql`, `plan.json`, `plan.md`
   (CD-12/CD-13).
4. Apply к существующей БД: gate → pre → повторная дельта (CD-11, только SAFE) →
   применение → post → запись `schema_version` с `source='apply'` (CD-14/CD-15);
   перед таргетом — репетиция на temp-аналоге с данными.
5. Всё детерминированно, без AI (CD-AI-3 = Won't); ноль новых abstract-методов
   адаптера.

## Что сделано

| Слой | Файл | Что изменилось |
|------|------|----------------|
| Domain | `domain/delta.py` (новый) | `ColumnSnapshot`, `ColumnDiff`/`ColumnChangeKind`, `OperationClass`, `PlannedOperation`, `DeltaPlan` (properties violations/needs_pre_ops/safe_ops) |
| Domain | `domain/diff.py` | additive: `ObjectSnapshot.columns`, `DiffEntry.column_diffs`/`columns_unavailable` |
| Infra/diff | `infrastructure/diff/columns.py` (новый) | `extract_columns` из SQL-тела (sqlglot; синонимы типов `integer`≡`int4`, alias-карта `bpchar`→`char`), `diff_columns`; fail-safe `None` |
| Infra/diff | `infrastructure/diff/comparator.py` | column-diff для CHANGED-таблиц; `identity_key` — сравнение без catalog-сегмента (`pg_database/<имя_БД>/`) |
| Infra/deploy | `infrastructure/deploy/alter_plan.py` (новый) | `classify` (матрица ALT-3 + presence/`covers`/`--include-drops`), `render_alter` (SAFE-подмножество, whitelist §19, `"s"."t"` §35), `is_volatile_default` |
| Infra/deploy | `infrastructure/deploy/plan_report.py` (новый) | `plan.json` (roundtrip §28) + `plan.md` (счётчики, reasons, «Требуют pre-скриптов») |
| Application | `application/delta_service.py` (новый) | план по `deploy_order` (REMOVED в конец, build=false исключены); артефакты `delta/NNN_*` (rerender-таблица с `DROP TABLE IF EXISTS`, BLOCKED-drop закомментирован, comment-only → skip §49) |
| Application | `application/deploy_apply_service.py` (новый) | `_run_pipeline` (gate → pre → CD-11 → артефакты → stop-on-error применение → post → версия) + репетиция (RE → `DeployValidateService.run(keep_db=True)` → seed `__migrations/seed/` → пайплайн → дроп); `DeployApplyError`/`DeployApplyRejected` |
| Application | `application/deploy_service.py` | `_validate_deploy_presence` принимает сид- и генераторные имена `__deploy`-файлов |
| CLI | `presentation/cli/main.py` | `deploy plan` (dry-run; BLOCKED → exit 1) и `deploy apply` (`--include-drops/--no-rehearsal/--keep-rehearsal-db`) |

## Ключевые решения

- **ALT-1b: SQL-тело — единственный источник правды для колонок.** Ничего не
  храним в autodoc → дрейф «метаданные ↔ SQL» невозможен (code-first safe);
  обе стороны compare идут через один экстрактор; `normalize_sql`/хэши не
  тронуты (инвариант фазы, эталонные хэши тестом).
- **ALT-5: репетиция вместо mega-транзакции.** Состояние таргета (RE → deploy)
  воспроизводится в temp-БД, сеется пользователем, полный пайплайн прогоняется
  на аналоге; сбой = отладка, таргет не тронут. Восстановление на таргете —
  повторным apply (дельта пересчитывается; pre/post идемпотентны по
  `script_history`).
- **ALT-3: консервативная матрица классификации.** ADD nullable/literal-default —
  safe даже при данных (PG11+); drop/type-change/narrowing/unrepresented →
  needs-pre; без `covers` → blocked; авто-DROP никогда (кроме явного флага и
  пустых/не-табличных).
- **Gate-residual даунгрейд (§52).** Табличный gate Phase 11 уточняется
  колоночной классификацией его же diff-отчёта: safe-дельта по таблице с
  данными пропускается, прочее — блок. Нет отчёта — все нарушения действуют.
- **Identity без имени БД (§51).** Compare работает между средами с разными
  именами БД (репетиция, dev↔prod): `identity_key` без catalog-сегмента.

## Проверки

- Unit: **846 passed** (700 базовых + 146), ruff чистый.
- Integration (testcontainers): **24 passed** — 16 базовых + 8 e2e Phase 12
  (safe-alter при данных; drop без pre → blocked; `covers`-pre → CD-11 чист;
  сбой репетиции → таргет не тронут; retry после midway; CTAS-fail-safe;
  `int4`≡`integer`; seed только в репетиции).
- Hash-инвариант: эталонные `sql_hash` фикстур захардкожены регрессией.

## Известные ограничения / NOT done

- Diff констрейнтов/индексов/partitioning — unrepresented → needs-pre (BACKLOG).
- Comment-level diff — `normalize_sql` первый-statement (BACKLOG: multi-statement
  normalize + переход хэшей).
- Column rename = drop+add (осознанно).
- Standalone `deploy analyze` без ALT-3 даунгрейда — BACKLOG P3.
- Greenplum распределённые ALTER — при появлении кластера.
- Post-deploy отчёты/история (CD-16..18) — Phase 13; AI-трек — overlay
  (prerequisite закрыт); GUI plan/apply — BACKLOG P3.

## Где читать дальше

- `_tasks_/phase_12/Phase_12_vision_final.md` — дизайн и решения ALT-1..8
- `_tasks_/phase_12/Phase_12_result.md` — шаги, коммиты, отклонения
- `_tasks_/ROADMAP.md` §2 (шаг 5 — Phase 13), §6 (AI-трек)
- `LESSONS_LEARNED.md` §50-§52
