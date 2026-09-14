# Phase 15: GUI deploy plan/apply + Plan Viewer — vision draft

> **Дата:** 2026-09-02
> **Ветка:** dev
> **Статус:** draft → открыт `USER_INPUT` PRE-1..PRE-3 (см. §3)

## Контекст

- `_checkpoints_/20260816_001_checkpoint.md` — Phase 12 done (ALTER + Delta).
- `_phases_/Phase_12.md`, `_tasks_/phase_12/Phase_12_vision_final.md` — нормативный дизайн `deploy plan`/`apply` и `DeployApplyService`.
- `_phases_/Phase_14.md`, `presentation/gui/widgets/delta_viewer.py` — образец viewer'а для отчётов.
- `_tasks_/BACKLOG.md` §"P3. GUI-действия deploy plan/apply + рендер плана деплоя" — целевая запись для закрытия.
- `presentation/gui/actions/registry.py` — `ACTIONS` реестр (7 действий; `deploy_plan`/`deploy_apply` отсутствуют).
- `presentation/gui/widgets/workers.py` — паттерн `WorkerSignals` + `QRunnable` (урок §42).
- `LESSONS_LEARNED.md` §39-§43, §47 — уроки GUI-разработки (shlex, offscreen, signal → strong ref, кнопки последними, combo по userData).

## 1. Постановка

Phase 12 дала CLI-команды `deploy plan` (dry-run, артефакты `delta/NNN_*.sql` + `plan.{json,md}`)
и `deploy apply` (мутирует существующую БД: репетиция → pre → повторная дельта → применение →
post → version). GUI из 7 действий (`registry.py`) не имеет к ним доступа — пользователь
вынужден переключаться в терминал, что разрывает полный флоу `analyze → plan → apply`,
который был целью Phase 11–12.

BACKLOG P3 явно указывает: «GUI-действия run-only по образцу Phase 11 `deploy_analyze` …
`deploy_apply` — с явным подтверждением "изменяет целевую БД"». Delta Viewer (Phase 14)
показывает `diff_report.json`; `plan.json` — естественное расширение, но с другим контрактом
(`DeltaPlan` vs `DiffReport`), поэтому нужен отдельный viewer.

**Ключевое ограничение фазы:** `deploy apply` — первая команда в проекте, которая
**мутирует существующую БД** (validation deploy создаёт временную пустую БД и удаляет
её по умолчанию; Phase 11 analyze — read-only). Любой UX, ведущий к apply, должен явно
обозначать риск и требовать подтверждения.

## 2. Цель фазы

1. GUI-обёртки для `deploy plan` и `deploy apply` по образцу `deploy_analyze` (Phase 11):
   settings-модели, диалоги, воркеры, `build_cli_*`, регистрация в `ActionSpec`.
2. `PlanViewerWindow` — отдельное окно для просмотра `plan.json` (по образцу
   `DeltaViewerWindow`): дерево операций с цветами classification, фильтры
   safe/needs-pre/blocked, рендер `delta/NNN_*.sql` с подсветкой.
3. Preflight-warning в `DeployApplyDialog`: красный заголовок + обязательный чекбокс
   «Я понимаю последствия и хочу применить».
4. Chain analyze → plan → apply из Plan Viewer: кнопка «Применить» открывает
   `DeployApplyDialog` с предзаполненными полями.
5. Все три CLI-флага (`--include-drops`, `--no-rehearsal`, `--keep-rehearsal-db`) доступны
   из GUI.
6. Контракт-тесты GUI↔CLI и offscreen-smoke (уроки §39, §41–§43).

**Не входит:** авто-генератор seed (BACKLOG P3 «ALT-8b»); analyze-даунгрейд по ALT-3;
multi-statement normalize; GUI рендер `safety_gate_report.md` (уже есть QMessageBox с
путём к MD).

## 3. Вопросы (`USER_INPUT`)

### PRE-1. Plan Viewer — отдельное окно или вкладка в Delta Viewer?
- **A.** Отдельное `PlanViewerWindow` (по образцу `DeltaViewerWindow`). Контракт
  `DeltaPlan` отличается от `DiffReport` (PlannedOperation vs DiffEntry), свой фильтр,
  своя цветовая схема classification. Чистое разделение, повторное использование
  `SqlHighlighter`.
- **B.** Вкладка «Plan» внутри `DeltaViewerWindow`. Меньше кода, но смешивает два
  разных домена: diff (Phase 9/14) и plan (Phase 12). Усложняет `_populate_tree`.
- **C.** Расширить `DeltaViewerWindow` общим режимом diff/plan с переключателем.
  Максимальное переиспользование, но `DiffReport` и `DeltaPlan` имеют разные
  операции и разные цвета — нужен полиморфизм в дереве.

USER_INPUT PRE-1: A / B / C? **Рекомендация: A** — независимые контракты, чистое
разделение по конвенции Phase 14 (один viewer — один формат отчёта).

### PRE-2. Preflight-warning в `DeployApplyDialog`
- **A.** Обязательный чекбокс-подтверждение + красный заголовок «⚠ Изменяет
  существующую БД». Кнопка «Применить» (`OK`) `setEnabled(False)` пока
  `QCheckBox.isChecked()`. Дополнительное поле `confirm_understands_risk: bool`
  в `DeployApplySettings` (CLI-билдер игнорирует — только GUI-side гейт).
- **B.** Только ярлык-предупреждение в диалоге + финальный `QMessageBox.question`
  при нажатии «Выполнить». Двухшаговое подтверждение (по аналогии с удалением
  подключения в `MainWindow._on_delete_connection`).
- **C.** Только текст в диалоге «Будет изменена целевая БД», без чекбокса.
  Минимальный gate; риск случайного нажатия на проде выше.

USER_INPUT PRE-2: A / B / C? **Рекомендация: A** — чекбокс явнее
(не проматывается Enter'ом), интегрируется с архитектурой Qt (stateChanged → setEnabled),
pattern явный и тестируемый.

### PRE-3. Связка analyze → plan → apply
- **A.** Кнопка «Применить» в `PlanViewerWindow` открывает `DeployApplyDialog`
  с предзаполненными `codebase_dir`/`target_connection`/`output_dir`. После
  успешного apply viewer перезагружается на новый `plan.json`. В диалогах plan/apply
  после успешного выполнения — кнопка «Открыть план».
- **B.** Кнопка «Перейти к apply» в `QMessageBox` после `deploy_analyze`/`deploy_plan`.
  Меньше состояний в viewer'е, но каждый QMessageBox должен знать, что делать.
- **C.** Только ручной выбор через панель действий (без автоматической связки).
  Минимум кода, но пользователь повторяет выбор `target_connection`/`output_dir`.

USER_INPUT PRE-3: A / B / C? **Рекомендация: A** — единая точка состояния
(Plan Viewer), предзаполнение минимизирует ошибки, типичный сценарий «посмотрел
план → решил применить» нативно укладывается.

## 4. Архитектура (предварительная)

| Слой | Файл | Что |
|------|------|-----|
| Settings | `presentation/gui/actions/models.py` | `DeployApplySettings` (новый — НЕ переиспользовать `DeployAnalyzeSettings`: у apply три доп. флага + preflight). Поля: `codebase_dir`, `target_connection`, `output_dir`, `include_drops: bool = False`, `no_rehearsal: bool = False`, `keep_rehearsal_db: bool = False`, `confirm_understands_risk: bool = False` |
| CLI-builder | `presentation/gui/actions/cli.py` | `build_cli_deploy_plan`, `build_cli_deploy_apply` (по образцу `build_cli_deploy_analyze`); флаг `--include-drops` для plan; все три флага для apply; `confirm_understands_risk` игнорируется (GUI-side) |
| Registry | `presentation/gui/actions/registry.py` | два `ActionSpec`: `deploy_plan`, `deploy_apply`, `required_fields=("codebase_dir", "target_connection", "output_dir")` |
| Dialogs | `presentation/gui/actions/dialogs.py` | `DeployPlanDialog` (наследник `BaseActionDialog`); `DeployApplyDialog` с preflight-warning + чекбоксом; `_add_buttons()` — последней строкой `__init__` (урок §43) |
| Workers | `presentation/gui/widgets/workers.py` | `DeployPlanWorker(codebase_dir, conn_cfg, output_dir, *, include_drops=False)` → `DeltaPlan`; `DeployApplyWorker(..., *, include_drops=False, no_rehearsal=False, keep_rehearsal_db=False)` → `ApplyResult`; `LoadPlanReportWorker(path)` → `DeltaPlan` |
| Plan Viewer | `presentation/gui/widgets/plan_viewer.py` (новый) | `PlanViewerWindow` по образцу `DeltaViewerWindow`; дерево `type → schema → object` с цветами classification; табы «Детали» + «DDL» (`SqlHighlighter(diff_mode=True)`) |
| Infra | `infrastructure/deploy/plan_report.py` | `load_plan_report(path) -> DeltaPlan` (симметрично `DiffReport.model_validate_json`) |
| Main Window | `presentation/gui/main_window.py` | `open_plan_viewer(report_path=None)` (по образцу `open_delta_viewer`); пункт меню «Вид → Plan Viewer…»; `_report_apply_result` с кнопкой «Открыть план»; `_report_plan_result` аналогично |
| Tests | `tests/unit/test_action_panel_smoke.py`, `tests/unit/test_action_cli.py`, `tests/unit/test_plan_viewer.py` (новый), `tests/integration/test_deploy_plan_apply_gui_e2e.py` (новый) | offscreen-smoke, contract-тесты GUI↔CLI, integration-сценарий через testcontainers |
| Docs | `_tasks_/phase_15/*`, `_phases_/Phase_15.md`, `_checkpoints_/<…>_NNN_checkpoint.md`, `README.md`, `LESSONS_LEARNED.md` §59+, `_tasks_/BACKLOG.md` (cleanup) | vision_draft, vision_final, plan, result, фаза, чекпойнт, README, lessons, удаление BACKLOG P3 |

## 5. План реализации (предварительный, уточняется в `_plan.md`)

- **S1.** settings + CLI-builders + реестр (заглушки фабрик)
- **S2.** диалоги + воркеры (preflight в `DeployApplyDialog`, `confirm_understands_risk` в settings)
- **S3.** `PlanViewerWindow` + `load_plan_report` + `LoadPlanReportWorker`
- **S4.** MainWindow интеграция: `open_plan_viewer`, меню «Вид», кнопки «Открыть план» в
  `_report_apply_result`/`_report_plan_result`; chain через Plan Viewer
- **S5.** тесты (unit + integration); документация (Phase_15, README, lessons, checkpoint, BACKLOG cleanup)

## 6. Ключевые принципы (из уроков)

- **Урок §42 (signal → strong ref):** все воркеры — `WorkerSignals` + хранение
  `(worker, action_id, settings)` в `MainWindow._active_workers` и в `PlanViewerWindow._active_workers`.
- **Урок §43 (кнопки последними):** `_add_buttons()` — последней строкой `__init__` в каждом диалоге.
- **Урок §40 (имя `validate`):** в settings — `codebase_dir`/`target_connection`/`output_dir` (не `validate`).
- **Урок §39 (shlex):** helper `_argv` остаётся в `tests/unit/test_action_cli.py`; `build_cli_*`
  используют `shlex`-совместимые пути.
- **Урок §41 (offscreen smoke):** новые тесты — `QT_QPA_PLATFORM=offscreen`, `qapp` scope=module.
- **Урок §47 (combo index):** `_connections_combo` возвращает `userData=name` — `findData`,
  не индексы.
- **Урок §44 (smoke перед кодом):** offscreen-прогон диалогов/воркеров до интеграционных тестов.

## 7. Проверки (финальные)

```bash
unset SSL_CERT_FILE REQUESTS_CA_BUNDLE CURL_CA_BUNDLE
uv run ruff check src/ tests/                          # All checks passed
uv run pytest tests/unit/ -q                           # baseline + новые passed
uv run pytest -m integration                           # baseline + новый e2e passed
```

Минимум: 6+ новых unit-тестов (settings, диалоги, Plan Viewer, contract CLI);
1 новый integration-сценарий (analyze → plan → open Plan Viewer → apply).

## 8. Известные ограничения / NOT done

- Авто-генератор seed (BACKLOG P3 «ALT-8b»).
- `deploy analyze` без даунгрейда по ALT-3.
- Multi-statement normalize + comment-level diff.
- Plan Viewer не показывает `safety_gate_report.md`/`safety_gate_report.json` (уже есть в `_report_analyze_result`).
- GUI не валидирует `target_connection` против `cfg.deploy.service_schema` — CLI-уровень.