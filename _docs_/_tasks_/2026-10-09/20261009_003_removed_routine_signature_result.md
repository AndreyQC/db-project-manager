# Сигнатуры REMOVED-рутин в ObjectSnapshot: DROP по настоящей перегрузке (result)

> **Date:** 2026-10-11
> **Status:** done

> Контекст:
> - План: `_tasks_/2026-10-09/20261009_003_removed_routine_signature_plan.md`
> - Уровень 1 (контекст бага): `_tasks_/2026-10-09/20261009_002_drop_function_gp_parens_result.md`
> - Коммит кода: `da59f2d` (feat(app), код и документы раздельно).

## Что сделано

Цепочка переноса сигнатуры реализована ровно по плану:

| Слой | Файл | Изменение |
|---|---|---|
| Домен | `domain/diff.py` | `ObjectSnapshot.argument_types: str = ""` (аддитивно; старые JSON парсятся) |
| Домен | `domain/delta.py` | `PlannedOperation.argument_types: str = ""` (попадает в plan.json автоматически — pydantic dump) |
| Снапшот | `infrastructure/diff/snapshot.py` | `argument_types=vertex.argument_types` при построении обеих сторон |
| Классификация | `infrastructure/deploy/alter_plan.py` | `classify()`: перенос `snap.argument_types` в base; **fail-safe** — REMOVED-рутина с непустым `object_signature` и пустым `argument_types` → BLOCKED «DROP вручную» (ALT-6), а не тихий `()`-no-op |
| Рендер | `application/delta_service.py` | `_drop_statement`: приоритет `op.argument_types` (целевой снапшот) → `vertex.argument_types` (кодовая база) → `()` |

`classify()` используется и `deploy plan`, и повторной дельтой в `deploy apply`
(deploy_apply_service.py:554), а также MCP `deploy_plan` — fail-safe покрывает
все три пути автоматически.

## Проверки

- `uv run pytest` — **1280 passed**, 40 deselected (integration; Docker не
  поднимался). +6 тестов:
  - `test_removed_function_drop_uses_snapshot_signature` — REMOVED-функция
    с `argument_types="integer, text"` → исполняемый артефакт
    `DROP FUNCTION IF EXISTS "app"."fun_old_etl"(integer, text);`;
  - `test_removed_overloads_drop_with_distinct_signatures` — две REMOVED-
    перегрузки `sp_old` → два артефакта, каждый со своей сигнатурой;
  - `test_removed_routine_unknown_signature_blocked` — `object_signature`
    непустой, списка нет → BLOCKED + закомментированный `()` в артефакте;
  - `test_drop_statement_prefers_op_argument_types` — приоритет op → vertex;
  - `test_snapshot_carries_argument_types` — фиксtures `sp_x(text)` /
    `sp_x(int4)` попадают в снапшот дословно; не-рутины — `""`;
  - `test_snapshot_argument_types_json_roundtrip` — JSON round-trip
    сохраняет поле; легаси-словарь без поля парсится в `""`.
- `uv run ruff check .` — чисто.

## Известные ограничения

- Прогон на живом Greenplum (cis_zup, перегрузки с реальными сигнатурами) —
  за пользователем; integration-контейнер — postgres, GP-грамматика
  зафиксирована юнит-тестами уровня генерации стейтмента (20261009_002).
- `argument_types` пишется в `diff_report.json`/`plan.json` (чуть крупнее
  артефакты) — осознанное решение плана.
- Старые `diff_report.json` без поля: REMOVED-перегрузки с сигнатурой
  уйдут в BLOCKED (fail-safe) — это желаемое поведение, DROP вручную.
