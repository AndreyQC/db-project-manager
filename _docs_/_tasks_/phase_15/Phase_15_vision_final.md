# Phase 15: GUI deploy plan/apply + Plan Viewer — финальный дизайн (vision final)

> **Дата:** 2026-09-02
> **Ветка:** dev
> **Статус:** final (все `USER_INPUT` PRE-1..PRE-3 закрыты; нормативный документ для `_plan`
> и реализации)
>
> Предшествующий артефакт: `_tasks_/phase_15/Phase_15_vision_draft.md` (не удаляется — остаётся
> для истории обсуждения, по TASK_CONVENTIONS §2.2).
>
> Контекст:
> - `_checkpoints_/20260816_001_checkpoint.md` — текущее состояние (Phase 12 done, 846 unit / 24 integration).
> - `_phases_/Phase_12.md`, `_tasks_/phase_12/Phase_12_vision_final.md` — `deploy plan`/`apply` CLI,
>   `DeployApplyService`, `DeltaPlan`, `ApplyResult`.
> - `_phases_/Phase_14.md`, `presentation/gui/widgets/delta_viewer.py` — образец `DeltaViewerWindow`.
> - `_tasks_/BACKLOG.md` §"P3. GUI-действия deploy plan/apply + рендер плана деплоя" — целевая запись.
> - `presentation/gui/actions/registry.py`, `presentation/gui/widgets/workers.py`,
>   `presentation/gui/main_window.py` — паттерны GUI-действий и воркеров.
> - `LESSONS_LEARNED.md` §39-§43, §47 — уроки GUI-разработки.

---

## 1. Постановка проблемы

Phase 12 дала CLI-команды `deploy plan` (dry-run, артефакты `delta/NNN_*.sql` + `plan.{json,md}`)
и `deploy apply` (мутирует существующую БД: репетиция → pre → повторная дельта → применение →
post → version). GUI из 7 действий (`registry.py`) не имеет к ним доступа — пользователь
вынужден переключаться в терминал, что разрывает полный флоу `analyze → plan → apply`,
который был целью Phase 11–12.

`deploy apply` — первая команда в проекте, которая мутирует существующую БД. Любой UX,
ведущий к apply, должен явно обозначать риск и требовать подтверждения.

## 2. Цель фазы

1. GUI-обёртки для `deploy plan` и `deploy apply` по образцу `deploy_analyze` (Phase 11).
2. `PlanViewerWindow` — отдельное окно для просмотра `plan.json`.
3. Preflight-warning в `DeployApplyDialog`: красный заголовок + обязательный чекбокс.
4. Chain analyze → plan → apply из Plan Viewer: кнопка «Применить» открывает
   `DeployApplyDialog` с предзаполненными полями.
5. Все три CLI-флага доступны из GUI.
6. Контракт-тесты GUI↔CLI и offscreen-smoke (уроки §39, §41–§43, §47).

## 3. Принятые решения (все закрыты)

| ID | Развилка | Решение | Обоснование |
|----|----------|---------|-------------|
| PRE-1 | Plan Viewer — отдельное окно или вкладка | **A: отдельное `PlanViewerWindow`** | Контракты `DeltaPlan` и `DiffReport` разные (PlannedOperation vs DiffEntry), свои фильтры и цветовая схема classification. Чистое разделение по конвенции Phase 14 (один viewer — один формат отчёта). |
| PRE-2 | Apply-gate UX | **A: обязательный чекбокс + красный заголовок** | Чекбокс явнее (не проматывается Enter'ом), интегрируется с архитектурой Qt (`stateChanged → setEnabled`), pattern явный и тестируемый. Двухшаговое подтверждение (вариант B) дублирует внимание; вариант C без чекбокса недопустим для apply на проде. |
| PRE-3 | Связка analyze → plan → apply | **A: кнопка «Применить» в Plan Viewer + кнопки «Открыть план» в диалогах** | Единая точка состояния (Plan Viewer), предзаполнение минимизирует ошибки. В диалогах plan/apply — кнопки «Открыть план» (НЕ авто-открытие — пользователь решает, когда смотреть). |

## 4. Архитектура

### Settings — `presentation/gui/actions/models.py`

```python
class DeployApplySettings(BaseModel):
    model_config = ConfigDict(extra="ignore")
    codebase_dir: str = ""
    target_connection: str = ""   # не путать с BaseModel.validate (урок §40)
    output_dir: str = ""
    include_drops: bool = False
    no_rehearsal: bool = False
    keep_rehearsal_db: bool = False
    confirm_understands_risk: bool = False  # GUI-side gate; CLI-билдер игнорирует
```

Отдельный класс (не переиспользовать `DeployAnalyzeSettings`): у apply три доп. флага + preflight.

### CLI-builders — `presentation/gui/actions/cli.py`

```python
def build_cli_deploy_plan(settings: DeployApplySettings, store: ConnectionStore) -> str:
    conn_file = store.path_for(settings.target_connection)
    cmd = (
        f"db-pm deploy plan "
        f"--dir {_quote(settings.codebase_dir)} "
        f"--target-connection-file {_quote(str(conn_file))} "
        f"--output-dir {_quote(settings.output_dir)}"
    )
    if settings.include_drops:
        cmd += " --include-drops"
    return cmd

def build_cli_deploy_apply(settings: DeployApplySettings, store: ConnectionStore) -> str:
    conn_file = store.path_for(settings.target_connection)
    cmd = (
        f"db-pm deploy apply "
        f"--dir {_quote(settings.codebase_dir)} "
        f"--target-connection-file {_quote(str(conn_file))} "
        f"--output-dir {_quote(settings.output_dir)}"
    )
    if settings.include_drops:
        cmd += " --include-drops"
    if settings.no_rehearsal:
        cmd += " --no-rehearsal"
    if settings.keep_rehearsal_db:
        cmd += " --keep-rehearsal-db"
    # confirm_understands_risk — GUI-side gate, в CLI не передаётся
    return cmd
```

### Registry — `presentation/gui/actions/registry.py`

Два `ActionSpec`:
```python
ActionSpec(
    action_id="deploy_plan",
    title="Сформировать план деплоя на существующую БД (dry-run)",
    settings_model=DeployApplySettings,
    make_dialog=_make_deploy_plan_dialog,
    make_worker=_make_deploy_plan_worker,
    build_cli=build_cli_deploy_plan,
    required_fields=("codebase_dir", "target_connection", "output_dir"),
),
ActionSpec(
    action_id="deploy_apply",
    title="Применить деплой к существующей БД (мутирует данные)",
    settings_model=DeployApplySettings,
    make_dialog=_make_deploy_apply_dialog,
    make_worker=_make_deploy_apply_worker,
    build_cli=build_cli_deploy_apply,
    required_fields=("codebase_dir", "target_connection", "output_dir"),
),
```

### Dialogs — `presentation/gui/actions/dialogs.py`

- **`DeployPlanDialog`:** наследник `BaseActionDialog`; поля `codebase_dir`/`target_connection`/`output_dir`
  (как в `DeployAnalyzeDialog`); чекбокс `Включить DROP-артефакты` (`include_drops`).
- **`DeployApplyDialog`:** то же + `no_rehearsal`/`keep_rehearsal_db` + **preflight-warning**:
  - `QLabel` сверху формы с `setStyleSheet("color: red; font-weight: bold")` и текстом
    «⚠ Изменяет существующую БД. Репетиция обязательна (кроме CI).».
  - `QCheckBox("Я понимаю последствия и хочу применить")`.
  - `button_box.button(QDialogButtonBox.StandardButton.Ok).setEnabled(self._confirm.isChecked())`,
    связь через `self._confirm.stateChanged.connect(lambda _: ...)` или `partial`.
  - `_add_buttons()` — последней строкой `__init__` (урок §43).

### Workers — `presentation/gui/widgets/workers.py`

```python
class DeployPlanWorker(QRunnable):
    def __init__(self, conn_cfg, codebase_dir, output_dir, *, include_drops=False): ...
    def run(self):
        try:
            service = DeployApplyService(service_schema=cfg.deploy.service_schema)
            plan = service.plan(codebase_dir, conn_cfg, output_dir,
                                include_drops=include_drops,
                                progress=lambda m, c, t: self.signals.progress.emit(m, c, t))
        except DeployApplyRejected as e:
            self.signals.error.emit(f"Safety gate отклонил план: {e}")
            self.signals.finished.emit(None); return
        except DeployApplyError as e:
            self.signals.error.emit(f"Plan: {e}")
            self.signals.finished.emit(None); return
        self.signals.status.emit(f"План готов: {len(plan.operations)} операций")
        self.signals.finished.emit(plan)


class DeployApplyWorker(QRunnable):
    def __init__(self, conn_cfg, codebase_dir, output_dir, *,
                 include_drops=False, no_rehearsal=False, keep_rehearsal_db=False): ...
    def run(self):
        try:
            service = DeployApplyService(service_schema=cfg.deploy.service_schema)
            result = service.apply(codebase_dir, conn_cfg, output_dir,
                                   include_drops=include_drops,
                                   rehearsal=not no_rehearsal,
                                   keep_rehearsal_db=keep_rehearsal_db,
                                   progress=...)
        except DeployApplyRejected as e:
            self.signals.error.emit(f"Apply отклонён safety gate: {e}")
            self.signals.finished.emit(None); return
        except DeployApplyError as e:
            self.signals.error.emit(f"Apply: {e}")
            self.signals.finished.emit(None); return
        self.signals.status.emit(f"✓ Apply завершён: {result.applied}/{result.planned}")
        self.signals.finished.emit(result)


class LoadPlanReportWorker(QRunnable):
    def __init__(self, path: Path): ...
    def run(self):
        try:
            plan = load_plan_report(self._path)
        except Exception as e:
            self.signals.error.emit(f"Чтение plan.json: {e}")
            self.signals.finished.emit(None); return
        self.signals.finished.emit(plan)
```

Сильные ссылки: `(worker, action_id, settings)` хранится в `MainWindow._active_workers`
и в `PlanViewerWindow._active_workers` (урок §42).

### Plan Viewer — `presentation/gui/widgets/plan_viewer.py` (новый)

`PlanViewerWindow(QMainWindow)` по образцу `DeltaViewerWindow` (Phase 14, 517 строк).

- **Конструктор:** принимает `connection_store: ConnectionStore | None = None`
  (для открытия `DeployApplyDialog` из кнопки «Применить»); state:
  `_plan: DeltaPlan | None`, `_plan_path: Path | None`,
  `_last_target_connection: str`, `_last_codebase_dir: str`, `_last_output_dir: str`,
  `_active_workers: dict`, `thread_pool: QThreadPool`.
- **Дерево:** `type → schema → object`. Цвета листьев по `_CLASSIFICATION_COLORS`:
  - SAFE → `#a3d9a3` (зелёный)
  - NEEDS_PRE → `#f7c948` (жёлтый)
  - BLOCKED → `#f08080` (красный)
- **Табы при выборе листа:**
  - «Детали»: `QFormLayout` с полями `object_key`, `object_type`, `object_name`,
    `action`, `classification`, `reason`, `estimated_rows`, `covered_by`, `script_file`.
  - «DDL»: `QPlainTextEdit` с моноширинным шрифтом + `SqlHighlighter(document, diff_mode=True)`.
    Читает `<output_dir>/<script_file>` если существует; иначе — placeholder.
- **Summary bar:** «safe: X, needs-pre: Y, blocked: Z» (считается на `_populate_tree`).
- **Тулбар:** «Открыть JSON…» (QFileDialog фильтр `plan.json;*.json`), «Применить» (PRE-3).

### Infra — `infrastructure/deploy/plan_report.py`

Добавить симметрично `DiffReport.model_validate_json`:
```python
def load_plan_report(path: str | Path) -> DeltaPlan:
    """Parse a plan.json produced by write_plan_report back into a DeltaPlan."""
    text = Path(path).read_text(encoding="utf-8")
    return DeltaPlan.model_validate_json(text)
```

### Main Window — `presentation/gui/main_window.py`

- Пункт меню «Вид → Plan Viewer…» (по образцу Delta Viewer).
- `open_plan_viewer(report_path: str | None = None)` (по образцу `open_delta_viewer`).
- `_report_plan_result(plan: DeltaPlan, settings: DeployApplySettings)`: QMessageBox
  со счётчиками + кнопка «Открыть план» (открывает `PlanViewerWindow.load_from_path(
  output_dir / "plan.json")`, если файла нет — error).
- `_report_apply_result(result: ApplyResult, settings: DeployApplySettings)`: то же
  (счётчики `applied/planned`, `applied_version`, `rehearsal_db`) + кнопка «Открыть план».
- В ветке `_on_action_finished` для `deploy_plan`/`deploy_apply` — вызов соответствующих `_report_*`.

### Chain из Plan Viewer — `presentation/gui/widgets/plan_viewer.py`

- Кнопка «Применить» в тулбаре:
  ```python
  def _on_apply(self):
      if not self._last_target_connection:
          QMessageBox.warning(self, "Нет target_connection", "Откройте plan.json через диалог plan/apply.")
          return
      settings = DeployApplySettings(
          codebase_dir=self._last_codebase_dir,
          target_connection=self._last_target_connection,
          output_dir=self._last_output_dir,
      )
      dlg = DeployApplyDialog(self._store, settings, parent=self)
      if dlg.exec() == QDialog.Accepted:
          new_settings = dlg.settings()
          worker = DeployApplyWorker(...)
          self._active_workers[worker.signals] = worker
          worker.signals.finished.connect(self._on_apply_finished_in_viewer)
          self.thread_pool.start(worker)
  ```
- `_on_apply_finished_in_viewer` → `self.load_from_path(Path(self._last_output_dir) / "plan.json")`.

## 5. Релевантные уроки (применяются явно)

| § | Урок | Применение |
|---|------|-----------|
| 39 | shlex на Windows | helper `_argv` в `tests/unit/test_action_cli.py`; `build_cli_*` совместимы |
| 40 | имя `validate` затеняет BaseModel | `target_connection`, не `validate_target_connection` |
| 41 | offscreen smoke | новые тесты в `test_action_panel_smoke.py` — `QT_QPA_PLATFORM=offscreen` |
| 42 | signal → strong ref | `_active_workers` в `MainWindow` и в `PlanViewerWindow` |
| 43 | кнопки диалога последними | `_add_buttons()` — последняя строка `__init__` |
| 47 | combo index | `findData(name)`, не индексы |
| 44 | sqlglot smoke перед кодом | offscreen-прогон диалогов до интеграционных тестов |

## 6. Проверки (финальные)

```bash
unset SSL_CERT_FILE REQUESTS_CA_BUNDLE CURL_CA_BUNDLE
uv run ruff check src/ tests/                          # All checks passed
uv run pytest tests/unit/ -q                           # baseline + новые passed
uv run pytest -m integration                           # baseline + новый e2e passed
```

Покрытие минимум:
- 6+ новых unit-тестов (settings, диалоги, Plan Viewer, contract CLI).
- 1 новый integration-сценарий (analyze → plan → open Plan Viewer → apply).
- `confirm_understands_risk` гейтит «OK» (regression-тест на урок §43 + preflight-pattern).

## 7. Известные ограничения / NOT done

- Авто-генератор seed (BACKLOG P3 «ALT-8b»).
- `deploy analyze` без даунгрейда по ALT-3.
- Multi-statement normalize + comment-level diff.
- Plan Viewer не показывает `safety_gate_report.md`/`safety_gate_report.json`.
- GUI не валидирует `target_connection` против `cfg.deploy.service_schema`.