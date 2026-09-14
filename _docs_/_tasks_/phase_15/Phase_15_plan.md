# Phase 15: GUI deploy plan/apply + Plan Viewer — план реализации

> **Дата:** 2026-09-02
> **Ветка:** dev
> **Статус:** план (нормативный документ реализации, по vision_final)
>
> Контекст:
> - `_tasks_/phase_15/Phase_15_vision_final.md` — дизайн и закрытые решения PRE-1..PRE-3
> - `_tasks_/phase_12/Phase_12_plan.md` — образец плана Phase 12 (S-шаги, проверки)
> - `_tasks_/phase_15/Phase_15_vision_draft.md` §4 — таблица архитектуры

---

## Шаги

| Шаг | Содержание | Проверки | Коммиты |
|-----|-----------|----------|---------|
| S1 | Settings `DeployApplySettings`, CLI-builders `build_cli_deploy_plan`/`build_cli_deploy_apply`, реестр `ActionSpec` (фабрики — заглушки, поднимают `NotImplementedError`) | `uv run ruff check`; unit-тест settings (`extra="ignore"`, дефолты); `pytest tests/unit/test_action_cli.py::test_deploy_plan_settings_extra_ignored -q` | `feat(app): phase 15 S1 — settings + CLI-builders + реестр (заглушки)` |
| S2 | Диалоги `DeployPlanDialog`, `DeployApplyDialog` (preflight-warning, чекбокс гейтит OK), воркеры `DeployPlanWorker`, `DeployApplyWorker`, фабрики в реестре | `uv run pytest tests/unit/test_action_panel_smoke.py -q` (offscreen: конструктор диалога, `_last_form_widget is QDialogButtonBox`, `confirm_understands_risk` гейтит `OK`); `pytest tests/unit/test_action_cli.py::test_contract_deploy_plan -q`; `pytest tests/unit/test_action_cli.py::test_contract_deploy_apply -q` | `feat(app): phase 15 S2 — диалоги plan/apply + воркеры + preflight` |
| S3 | `PlanViewerWindow` (новый файл `widgets/plan_viewer.py`), `load_plan_report` в `infrastructure/deploy/plan_report.py`, `LoadPlanReportWorker`, `LoadPlanReportWorker`-тест | offscreen-смоук `test_plan_viewer.py`: фикстура `DeltaPlan` (3 операции разных classification), `show_plan(plan)`, проверить дерево, фильтры, рендер DDL | `feat(app): phase 15 S3 — PlanViewerWindow + load_plan_report` |
| S4 | `MainWindow`: меню «Вид → Plan Viewer…», `open_plan_viewer()`, ветки `_report_plan_result`/`_report_apply_result` с кнопкой «Открыть план»; chain из Plan Viewer (`_on_apply` → `DeployApplyDialog` → `DeployApplyWorker` → reload plan.json) | `test_deploy_plan_apply_gui_e2e.py` (integration): analyze → plan → open Plan Viewer → apply (после preflight-чекбокса) → verify `applied_version` + артефакты; offscreen-smoke `_on_plan_viewer_apply_btn` (mock диалога) | `feat(app): phase 15 S4 — MainWindow интеграция + chain analyze→plan→apply` |
| S5 | Документация: `Phase_15.md` (свод фазы), checkpoint `<YYYYMMDD>_NNN_checkpoint.md`, README GUI-секция, `LESSONS_LEARNED.md` §59+ (если вскрылись), BACKLOG cleanup (удалить «P3. GUI-действия deploy plan/apply») | `uv run pytest tests/unit/ -q` (baseline + новые), `uv run pytest -m integration` (baseline + новый e2e), `uv run ruff check src/ tests/` | `docs(phase_15): …` + `docs(checkpoint): …` + `docs(readme): …` + `docs(lessons): …` + `docs(tasks): backlog cleanup` |

## Детализация шагов

### S1 — Settings + CLI-builders + реестр (заглушки)

**Файлы:**
- `src/db_project_manager/presentation/gui/actions/models.py` — добавить `DeployApplySettings`.
- `src/db_project_manager/presentation/gui/actions/cli.py` — добавить `build_cli_deploy_plan`,
  `build_cli_deploy_apply`.
- `src/db_project_manager/presentation/gui/actions/registry.py` — импорты + два `ActionSpec`,
  фабрики — `lambda`s, поднимающие `NotImplementedError("phase 15 S2")`.

**Критерии приёмки:**
- `DeployApplySettings` импортируется без ошибок; `model_validate({})` даёт дефолты; `extra="ignore"`
  работает (`{"unknown": 1}` → нет ошибки).
- `build_cli_deploy_plan(...)` возвращает корректную строку с `--include-drops` если флаг True.
- `build_cli_deploy_apply(...)` возвращает строку с тремя флагами (`--include-drops`,
  `--no-rehearsal`, `--keep-rehearsal-db`) по соответствующим булевым; `confirm_understands_risk`
  НЕ включается в CLI.
- `registry.py:ACTIONS` теперь содержит 9 элементов; `get_action("deploy_plan")`/`get_action("deploy_apply")`
  возвращают `ActionSpec` с правильными `required_fields`.

### S2 — Диалоги + воркеры (preflight)

**Файлы:**
- `src/db_project_manager/presentation/gui/actions/dialogs.py` — `DeployPlanDialog`,
  `DeployApplyDialog` (preflight).
- `src/db_project_manager/presentation/gui/widgets/workers.py` — `DeployPlanWorker`,
  `DeployApplyWorker`.
- `src/db_project_manager/presentation/gui/actions/registry.py` — заменить заглушки фабрик
  на реальные `_make_deploy_plan_dialog`, `_make_deploy_apply_dialog`,
  `_make_deploy_plan_worker`, `_make_deploy_apply_worker`.

**`DeployApplyDialog` детали:**
```python
class DeployApplyDialog(BaseActionDialog):
    def __init__(self, store, settings, parent=None):
        super().__init__("Применить деплой к существующей БД", parent=parent)
        # ... поля codebase_dir, target_connection, output_dir, include_drops,
        #     no_rehearsal, keep_rehearsal_db (по образцу DeployAnalyzeDialog)
        # preflight
        warning = QLabel("⚠ Изменяет существующую БД. Репетиция обязательна (кроме CI).")
        warning.setStyleSheet("color: red; font-weight: bold")
        self._form.insertRow(0, warning)  # первая строка
        self._confirm = QCheckBox("Я понимаю последствия и хочу применить")
        self._form.addRow(self._confirm)
        # OK button gating
        ok_button = self.button_box.button(QDialogButtonBox.StandardButton.Ok)
        ok_button.setEnabled(False)
        self._confirm.stateChanged.connect(lambda _: ok_button.setEnabled(self._confirm.isChecked()))
        self._add_buttons()  # урок §43 — последней строкой
```

**Воркеры:** см. `Phase_15_vision_final.md` §4 «Workers».

**Критерии приёмки:**
- Offscreen `_last_form_widget(dlg) is QDialogButtonBox` для обоих диалогов.
- `ok_button.isEnabled() == False` после конструктора `DeployApplyDialog`;
  `True` после `self._confirm.setChecked(True)`.
- `DeployApplyWorker.run` при `DeployApplyRejected` эмитит `error` + `finished(None)`;
  при успехе — `finished(apply_result)`. Проверяется моком сервиса через monkeypatch.
- Contract-тест `test_contract_deploy_apply`: собранная `build_cli_deploy_apply` строка
  вызывает CLI без ошибок (exit 0 при моке успеха), exit 2 при `DeployApplyError`.

### S3 — Plan Viewer

**Файлы:**
- `src/db_project_manager/infrastructure/deploy/plan_report.py` — добавить `load_plan_report(path)`.
- `src/db_project_manager/presentation/gui/widgets/plan_viewer.py` (новый) — `PlanViewerWindow`.
- `src/db_project_manager/presentation/gui/widgets/workers.py` — `LoadPlanReportWorker`.

**`PlanViewerWindow` детали:** см. `Phase_15_vision_final.md` §4 «Plan Viewer».
- `_populate_tree`: группировка `type → schema → object` через `_group_by_classification`.
- Цвета: `_CLASSIFICATION_COLORS = {SAFE: "#a3d9a3", NEEDS_PRE: "#f7c948", BLOCKED: "#f08080"}`.
- `_show_ddl`: читает `Path(self._plan_path).parent / op.script_file` (если файл существует);
- иначе placeholder («файл не найден»).

**Критерии приёмки:**
- `test_plan_viewer.py::test_show_plan_populates_tree`: фикстура `DeltaPlan` (3 операции
  разных classification) → дерево содержит 3 листа, summary «safe: 1, needs-pre: 1, blocked: 1».
- `test_plan_viewer.py::test_ddl_tab_reads_script_file`: при наличии `script_file` — DDL-таб
  показывает содержимое файла.
- `test_plan_viewer.py::test_filters_hide_by_classification`: фильтр safe=false скрывает safe-операции.

### S4 — MainWindow интеграция + chain

**Файлы:**
- `src/db_project_manager/presentation/gui/main_window.py` — `open_plan_viewer()`,
  меню «Вид → Plan Viewer…», ветки `_on_action_finished` для `deploy_plan`/`deploy_apply`,
  `_report_plan_result`/`_report_apply_result`.

**Детали:**
```python
def open_plan_viewer(self, report_path: str | None = None) -> None:
    window = PlanViewerWindow(connection_store=self.connection_store, parent=self)
    window.destroyed.connect(lambda _obj=None: self._on_child_window_closed(window))
    self._child_windows.append(window)
    if report_path is not None:
        window.load_from_path(report_path)
    window.show()
```

**Chain в Plan Viewer:** кнопка «Применить» в тулбаре (см. `Phase_15_vision_final.md` §4 «Chain»).

**Критерии приёмки:**
- `test_deploy_plan_apply_gui_e2e.py::test_analyze_plan_open_viewer_apply` (integration):
  analyze → plan → open Plan Viewer → apply (с preflight-чекбоксом) → verify
  `applied_version == "..."`, `applied > 0`, `<output_dir>/plan.json` существует,
  `<output_dir>/safety_gate_report.md` существует.
- Offscreen-smoke `_on_plan_viewer_apply_btn`: monkeypatch `DeployApplyDialog.exec()` →
  `Accepted`, `DeployApplyWorker.run` → `finished(apply_result)`; проверить, что viewer
  перезагружает `plan.json`.

### S5 — Документация + закрытие

**Файлы:**
- `_docs_/_phases_/Phase_15.md` — свод фазы (по образцу `Phase_12.md`).
- `_docs_/_checkpoints_/<YYYYMMDD>_NNN_checkpoint.md` — снапшот (по образцу `20260816_001_checkpoint.md`).
- `README.md` — секция GUI: «Deploy plan / apply на существующую БД».
- `LESSONS_LEARNED.md` — новые §59+ если вскрылись (например, про preflight-чекбокс в `setEnabled` race,
  про Plan Viewer `QFileDialog` фильтр).
- `_docs_/_tasks_/BACKLOG.md` — удалить запись «P3. GUI-действия deploy plan/apply + рендер плана деплоя»
  (закрыта).

**Критерии приёмки:**
- `Phase_15.md` содержит метаблок (Дата, Статус, План/Дизайн/Результат, Коммиты), раздел «Цель»,
  таблицу «Что сделано» (по слоям), «Ключевые решения», «Проверки», «Известные ограничения»,
  «Где читать дальше».
- Чекпойнт содержит: Header (date, branch, last commit, current phase, what's next),
  «Что это за проект», «Architecture map (дополнения Phase 15)», «Implemented features»,
  «Known gaps / NOT done», «Key decisions & constraints», «Test status», «Where to read more»,
  «Next planned work».
- README обновлён: пример GUI-флоу «Plan → открыть Plan Viewer → применить (с preflight)».
- BACKLOG.md — запись P3 удалена или помечена «ЗАКРЫТ».

## Проверки (финальные)

```bash
unset SSL_CERT_FILE REQUESTS_CA_BUNDLE CURL_CA_BUNDLE
uv run ruff check src/ tests/                          # All checks passed
uv run pytest tests/unit/ -q                           # baseline (846+) + новые passed
uv run pytest -m integration                           # baseline (24+) + новый e2e passed
```

## Коммиты (ожидаемые)

1. `docs(tasks): phase 15 vision_draft + vision_final + plan`
2. `feat(app): phase 15 S1 — settings + CLI-builders + реестр (заглушки)`
3. `feat(app): phase 15 S2 — диалоги plan/apply + воркеры + preflight`
4. `feat(app): phase 15 S3 — PlanViewerWindow + load_plan_report`
5. `feat(app): phase 15 S4 — MainWindow интеграция + chain analyze→plan→apply`
6. `test(app): phase 15 — unit smoke + contract CLI + plan_viewer + integration e2e`
7. `docs(phase_15): …` (свод фазы)
8. `docs(readme): …` (GUI-секция)
9. `docs(lessons): …` (§59+ если применимо)
10. `docs(tasks): backlog cleanup — P3 GUI plan/apply закрыт`
11. `docs(checkpoint): add <YYYYMMDD>_NNN — Phase 15 done`