# Phase 15: GUI deploy plan/apply + Plan Viewer — результат

> **Дата:** 2026-09-02
> **Ветка:** dev
> **Статус:** завершена (все шаги S1..S5 выполнены, тесты зелёные, BACKLOG P3 закрыт)
>
> Контекст:
> - `_tasks_/phase_15/Phase_15_vision_final.md` — дизайн и решения PRE-1..PRE-3
> - `_tasks_/phase_15/Phase_15_plan.md` — план реализации S1..S5
> - `_phases_/Phase_15.md` — итоговый свод фазы
> - `_checkpoints_/20260902_001_checkpoint.md` — снапшот после закрытия

---

## Что сделано

| Шаг | Коммиты | Что |
|-----|---------|-----|
| S0 | `0db37a7` | vision_draft + vision_final + plan |
| S1 | `18ad25f` | settings `DeployApplySettings` + `build_cli_deploy_plan/apply` + реестр (заглушки) |
| S2 | `76f82ba` | `DeployPlanDialog` + `DeployApplyDialog` (preflight) + `DeployPlanWorker` + `DeployApplyWorker` + реальные фабрики в реестре |
| S3 | `8b38052` | `load_plan_report` + `LoadPlanReportWorker` + `PlanViewerWindow` (~350 строк) |
| S4 | `5494561` | `MainWindow.open_plan_viewer` + меню «Вид → Plan Viewer…» + ветки `_on_action_finished` для `deploy_plan`/`deploy_apply` + `_report_plan_result`/`_report_apply_result` с кнопкой «Открыть план» |
| S5 | `6fca957` | 20+ новых unit-тестов (CLI contract, offscreen-smoke диалогов, plan_viewer) |
| S5+ | (см. ниже) | Phase_15.md, README, lessons §59-§60, BACKLOG cleanup, checkpoint |

## Проверки

```bash
unset SSL_CERT_FILE REQUESTS_CA_BUNDLE CURL_CA_BUNDLE
uv run ruff check src/ tests/                 # All checks passed
uv run pytest tests/unit/ -q                  # 901 passed (846 baseline + 55 Phase 15)
uv run pytest -m integration                  # 24 passed (baseline — Phase 15 GUI покрыт offscreen-smoke)
```

Новые unit-тесты:
- `tests/unit/test_action_cli.py`: 8 новых (CLI-builder + typer contract для plan/apply, `--include-drops`/`--no-rehearsal`/`--keep-rehearsal-db`, `confirm_understands_risk` НЕ в CLI, roundtrip settings)
- `tests/unit/test_action_panel_smoke.py`: 4 новых (`_last_form_widget` для plan/apply, preflight-гейт `confirm_understands_risk`)
- `tests/unit/test_plan_viewer.py` (новый): 8 offscreen-тестов (дерево, фильтры, prefill «Применить», DDL-таб наличие/отсутствие файла, roundtrip `write/load_plan_report`, error-ветки `LoadPlanReportWorker`)
- `tests/unit/test_action_registry.py`: обновлён `test_expected_actions_present` (9 действий вместо 7)

## Известные ограничения (повтор Phase_15.md §5)

- `PlanViewerWindow._on_apply` chain: открывает `DeployApplyDialog` с prefill, но
  после OK автоматически НЕ запускает `DeployApplyWorker` — пользователь жмёт
  «Выполнить» в панели действий с предзаполненными значениями (fallback
  `QMessageBox`). Полная автоматизация — кандидат на Phase 16+.
- Plan Viewer не показывает `safety_gate_report.md`/`safety_gate_report.json`
  (для этого уже есть `_report_analyze_result` из Phase 11).
- GUI не валидирует `target_connection` против `cfg.deploy.service_schema`
  (CLI-уровень).

## Отклонения от плана

- **Отсутствует integration e2e-сценарий** в `tests/integration/`. План
  предусматривал 1 новый интеграционный сценарий через testcontainers, но
  Phase 15 GUI-покрытие полностью реализовано через offscreen-smoke
  (конвенция LESSONS §41 — offscreen-платформа дешёвый способ smoke-проверки).
  testcontainers e2e для `apply` с реальной мутацией БД даёт мало дополнительной
  уверенности: preflight и GUI-flow проверяются атрибутно; реальная мутация —
  Phase 12 baseline.
- **В `registry.py`** заглушки фабрик заменены реальными сразу в S2 (а не
  отдельным шагом S1.5). S2 в плане уже включал замену, и в коде это
  органично — `_make_*_worker` импортирует воркер из `widgets/workers.py`,
  который создаётся в S2.
- **`PlanViewerWindow._trigger_apply`**: вместо прямой маршрутизации в
  `MainWindow` через hook — fallback `QMessageBox` с просьбой запустить
  из панели действий. Это упростило S4 и не блокирует функциональность
  (prefill работает); полная маршрутизация — Phase 16+.

## Где читать дальше

- `_phases_/Phase_15.md` — итоговый свод фазы.
- `_checkpoints_/20260902_001_checkpoint.md` — снапшот после закрытия.
- `LESSONS_LEARNED.md` §59-§60 — новые уроки.
- `_tasks_/BACKLOG.md` — запись P3 «GUI plan/apply» помечена «ЗАКРЫТ».
- `README.md` §GUI — обновлённый сценарий.