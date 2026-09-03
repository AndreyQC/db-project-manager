# Phase 15: GUI deploy plan/apply + Plan Viewer

> **Дата:** 2026-09-02
> **Ветка:** dev
> **Статус:** завершена
> **План:** `_docs_/_tasks_/phase_15/Phase_15_plan.md`
> **Дизайн:** `_docs_/_tasks/phase_15/Phase_15_vision_final.md`
> **Результат:** `_docs_/_tasks/phase_15/Phase_15_result.md`

---

## 1. Цель фазы

1. **GUI-доступ к `deploy plan` и `deploy apply`** по образцу Phase 11
   `deploy_analyze`: settings-модели, диалоги, воркеры, `build_cli_*`,
   регистрация в `ActionSpec`.
2. **`PlanViewerWindow`** — отдельное окно (по образцу `DeltaViewerWindow`):
   дерево операций с цветами `OperationClass`, фильтры
   safe/needs-pre/blocked, просмотр DDL из `delta/NNN_*.sql`.
3. **Preflight-warning** в `DeployApplyDialog` (PRE-2): красный заголовок +
   обязательный чекбокс-подтверждение, гейтящий OK.
4. **Chain analyze → plan → apply** (PRE-3): кнопка «Применить» в Plan Viewer
   открывает `DeployApplyDialog` с предзаполненными полями.
5. Все три CLI-флага (`--include-drops`, `--no-rehearsal`, `--keep-rehearsal-db`)
   доступны из GUI.

Закрывает **BACKLOG P3 «GUI-действия deploy plan/apply + рендер плана деплоя»**.

## 2. Что сделано

| Слой | Файл → Что изменилось |
|------|------------------------|
| Domain / Settings | `presentation/gui/actions/models.py` — `DeployApplySettings` (новый): `codebase_dir`/`target_connection`/`output_dir`/`include_drops`/`no_rehearsal`/`keep_rehearsal_db`/`confirm_understands_risk` |
| CLI-builder | `presentation/gui/actions/cli.py` — `build_cli_deploy_plan`, `build_cli_deploy_apply`; `confirm_understands_risk` намеренно не попадает в CLI (GUI-side gate) |
| Registry | `presentation/gui/actions/registry.py` — два `ActionSpec` (`deploy_plan`, `deploy_apply`) с реальными фабриками `_make_deploy_plan_dialog/worker`, `_make_deploy_apply_dialog/worker` |
| Dialogs | `presentation/gui/actions/dialogs.py` — `DeployPlanDialog` (наследник `BaseActionDialog`, без preflight); `DeployApplyDialog` (preflight: красный `QLabel` + `QCheckBox`, гейтит OK через `_button_box`); оба вызывают `_add_buttons()` последней строкой (урок §43) |
| Workers | `presentation/gui/widgets/workers.py` — `DeployPlanWorker` (dry-run, `DeltaPlan` через `finished`); `DeployApplyWorker` (мутирует БД, `ApplyResult`); `LoadPlanReportWorker` (по образцу `LoadDiffReportWorker`) |
| Infra | `infrastructure/deploy/plan_report.py` — `load_plan_report(path) -> DeltaPlan` (roundtrip для `write_plan_report`) |
| Plan Viewer | `presentation/gui/widgets/plan_viewer.py` (новый, ~350 строк) — `PlanViewerWindow` с деревом `type → schema → object`, цветами `_CLASSIFICATION_COLORS`, табами «Детали» + «DDL» (с `SqlHighlighter(diff_mode=True)`), фильтрами, тулбаром с «Открыть JSON…» и «Применить…» |
| Main Window | `presentation/gui/main_window.py` — пункт меню «Вид → Plan Viewer…», `open_plan_viewer(report_path, target_connection, codebase_dir, output_dir)`, ветки `_on_action_finished` для `deploy_plan`/`deploy_apply`, методы `_report_plan_result`/`_report_apply_result` с предложением «Открыть план» (PRE-3) |
| Tests | `tests/unit/test_action_panel_smoke.py` — `test_buttons_are_last_row_deploy_plan/_apply`, `test_apply_dialog_confirm_checkbox_gates_ok`, `test_apply_dialog_settings_roundtrip_includes_risk_flag` (4 новых) |
| Tests | `tests/unit/test_action_cli.py` — `test_deploy_plan_cli_string_basic/_with_include_drops`, `test_deploy_apply_cli_string_all_three_flags/_flags_off_omitted`, `test_deploy_apply_settings_extra_ignored`, `test_contract_deploy_plan`, `test_contract_deploy_apply` (8 новых); `_patch_common` адаптирован под `DeployApplyService` |
| Tests | `tests/unit/test_plan_viewer.py` (новый, ~200 строк) — 8 offscreen-тестов: дерево, фильтры, preflight-гейт «Применить», DDL-таб (наличие/отсутствие файла), roundtrip `write_plan_report` ↔ `load_plan_report`, error-ветки `LoadPlanReportWorker` |
| Tests | `tests/unit/test_action_registry.py` — `test_expected_actions_present` дополнен `deploy_plan`/`deploy_apply` |

## 3. Ключевые архитектурные решения

### PRE-1: Plan Viewer — отдельное окно
- **Чистое разделение доменов**: `DeltaPlan` (Phase 12) и `DiffReport` (Phase 9/14) —
  разные контракты (PlannedOperation vs DiffEntry), разные фильтры/цвета.
- Шаблон повторно использует существующие `WorkerSignals`, `LoadDiffReportWorker`-pattern,
  `SqlHighlighter(diff_mode=True)`. Реализация по образцу `DeltaViewerWindow` (Phase 14,
  517 строк).

### PRE-2: Apply-gate — обязательный чекбокс + красный заголовок
- Чекбокс `Я понимаю последствия и хочу применить` гейтит `OK` через
  `stateChanged → setEnabled`. Это явнее `QMessageBox.question` (двухшагового
  варианта): один шаг, не проматывается Enter'ом, тестируется напрямую
  (`ok_button.isEnabled()`).
- `confirm_understands_risk: bool` хранится в settings (round-trip через
  `settings()`), но `build_cli_deploy_apply` его игнорирует — поле
  GUI-side-only (Phase 15, конвенция «настройка ↔ контракт CLI»).
- Regression-тест `test_apply_dialog_confirm_checkbox_gates_ok` фиксирует контракт.

### PRE-3: Chain через Plan Viewer
- После успешного `deploy plan`/`deploy apply` `_report_plan_result`/
  `_report_apply_result` показывают `QMessageBox.question` с предложением
  «Открыть план». Если пользователь соглашается — `open_plan_viewer` с
  prefill `target_connection`/`codebase_dir`/`output_dir` (PRE-3).
- В Plan Viewer кнопка «Применить» в тулбаре включена только при наличии
  `target_connection` и `connection_store`. Дальнейшая маршрутизация — через
  `_on_apply_accepted` hook (зарезервировано для расширения в Phase 16+;
  сейчас fallback — `QMessageBox` с просьбой запустить «Применить» из панели
  действий).

### Не-функциональные
- **Урок §42 (signal → strong ref):** `PlanViewerWindow._active_workers` и
  `MainWindow._active_workers` хранят worker'ов до `finished`.
- **Урок §43 (кнопки диалога последними):** `_add_buttons()` — последняя строка
  `__init__`; `_button_box` сохраняется на `self` для preflight-гейта.
- **Урок §40 (имя `validate`):** в settings используется `target_connection`,
  не `validate_target_connection`.
- **Урок §44 (smoke перед кодом):** offscreen-прогон диалогов/воркеров до
  integration-тестов; 4 offscreen-smoke теста на диалоги + 8 на Plan Viewer.
- **`QAction.trigger()` без click-контекста:** `setChecked(False)` не вызывает
  `triggered` сигнал сам по себе. Offscreen-тест `_on_filter_changed` запускает
  handler напрямую, документируя контракт.

## 4. Проверки

```bash
unset SSL_CERT_FILE REQUESTS_CA_BUNDLE CURL_CA_BUNDLE
uv run ruff check src/ tests/                 # All checks passed
uv run pytest tests/unit/ -q                  # 901 passed (846 baseline + 55 Phase 15)
uv run pytest -m integration                  # baseline 24 — integration e2e не добавлялся (Phase 15 GUI-покрытие через offscreen-smoke, по конвенции §41)
```

Новые unit-тесты:
- **8 contract-тестов** `test_action_cli.py` (CLI-builder + typer contract для plan/apply).
- **4 offscreen-smoke** `test_action_panel_smoke.py` (кнопки, preflight-гейт, roundtrip settings).
- **8 offscreen-тестов** `test_plan_viewer.py` (дерево, фильтры, DDL-таб, LoadPlanReportWorker error-ветки, roundtrip `write/load_plan_report`).

## 5. Известные ограничения / NOT done

- **Авто-генератор seed** (BACKLOG P3 «ALT-8b») — отдельная фаза.
- **`deploy analyze` без даунгрейда по ALT-3** (BACKLOG P3 «analyze: даунгрейд») — отдельная фаза.
- **Multi-statement normalize + comment-level diff** (BACKLOG P3).
- **`PlanViewerWindow._on_apply` chain:** сейчас открывает `DeployApplyDialog`
  с prefill, но результат `settings()` пока не передаётся в `DeployApplyWorker`
  автоматически — пользователь должен нажать «Выполнить» в панели действий с
  предзаполненными значениями (fallback `QMessageBox`). Полная автоматизация
  (Plan Viewer → запустить воркер) — естественное расширение в Phase 16+
  (BACKLOG).
- **Plan Viewer не показывает `safety_gate_report.md`/`safety_gate_report.json`** —
  для этого уже есть `_report_analyze_result` (Phase 11 QMessageBox).
- **GUI не валидирует `target_connection` против `cfg.deploy.service_schema`** —
  CLI-уровень (Phase 12 `DeployApplyError`).

## 7. Phase 15.5 — PG round-trip false-positive fixes (cis_zup feedback 2026-09-03/04)

После закрытия Phase 15 на реальной cis_zup (272 таблицы) в `safety_gate`
появилась **серия false-positive CHANGED**: PG хранит семантически
одинаковые DDL в разных лексических формах. Закрыто тремя последовательными
фиксами:

### 15.5.3 — normalize_sql: `CAST('x' AS TEXT)` ↔ `'x'` (commit `e379cb0`)
- **Файл:** `src/db_project_manager/infrastructure/diff/normalize_sql.py`
- **Fix:** post-AST regex `_canonicalize_text_casts` срабатывает после
  sqlglot-нормализации. Сводит `CAST('utc' AS TEXT)`/`'utc'::text` к `'utc'`.
- **Coverage:** `DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'literal')`,
  любые DEFAULT-выражения с AT TIME ZONE.
- **Limitation:** только string literal (`CAST(<quoted-string> AS TEXT)` /
  `'<str>'::text`). numeric/identifier cast **не** трогаются.

### 15.5.4 — columns.py: type aliases + nextval compensation (commit `cff8634`)
- **Файл:** `src/db_project_manager/infrastructure/diff/columns.py`
- **Fix:**
  1. `_TYPE_ALIASES` расширен: `serial4/int4 → int`, `serial8/int8 → bigint`,
     `serial2/int2 → smallint`, `float4 → real`, `float8 → double precision`.
  2. `diff_columns` теперь скрывает default-difference, если одна сторона
     `default=None` (serialN неявно), а другая — `default=NEXTVAL(...)`
     (RE-БД round-trip): PG хранит одинаково.
- **Limitation:** обе стороны default-less (`serial4 NOT NULL` vs `int NOT NULL`)
  дают diff=[]. На практике не возникает (RE всегда читает DEFAULT).

### 15.5.5 — comparator: dual-signal (commit `e63e209`)
- **Файл:** `src/db_project_manager/infrastructure/diff/comparator.py`
- **Проблема:** comparator использовал только `sql_hash` для UNCHANGED/CHANGED,
  не видел column-level compensation из 15.5.3/15.5.4.
- **Fix:** dual-signal решение — `hash_agrees OR columns_match → UNCHANGED`,
  columns_unavailable=True сохраняет fail-safe (только hash).

### Итог Phase 15.5
- **Tests:** 901 → 939 unit passed (38 новых).
- **Кейс cis_zup:** violations 272+ → ожидаемо 0 после `git pull` + rerun.
- **LESSONS §63, §64:** подробные уроки.
- **BACKLOG (Phase 15.5.6, приоритет 1):**
  `keep-target-dir` для отладки — сейчас RE-target snapshot удаляется
  из tempdir после compare, невозможно посмотреть, что RE прочитал из БД.
  Это **первый** приоритет на следующую сессию (BACKLOG `db900be`,
  пользователь предложил).
- **BACKLOG (Phase 15.5.6, приоритет 2):** YAML-diff source vs target —
  архитектурное переосмысление, hash слишком хрупкий.
- **Другие PG-эквивалентности** (Phase 15.5.6++, приоритет 3) — см.
  LESSONS §64 урок 4 (VARCHAR vs TEXT, TIMESTAMP vs TIMESTAMP WITHOUT TIME ZONE,
  autogen constraint names и т.д.).

### **To resolve in next session**

**Перед любой новой работой прочитать `_checkpoints_/20260904_001_checkpoint.md`!**

В частности:

1. **Запустить** `db-pm deploy analyze` после `git pull` — должно быть
   `safety_gate.violations=0` или minor (если есть **реальные** отличия).
2. **Если есть остаточные violations** — прислать
   `safety_gate_report.json` в чат. В `_docs_/_tasks_/BACKLOG.md` есть
   раздел «PG-различие serialN vs int+DEFAULT NEXTVAL(...) в extract_columns»
   с детальным описанием известного ограничения.
3. **Phase 15.5.6 — первый приоритет:**
   Реализовать `keep-target-dir` для `db-pm compare run` и
   `db-pm deploy analyze` (BACKLOG `db900be`). Это ~1 час работы и резко
   ускоряет диагностику следующих false-positive.
4. **Phase 16+ (post-deploy отчёты, ROADMAP §2 шаг 5+)** — может
   стартовать независимо после Phase 15.5.6.

## 6. Где читать дальше

- `_tasks_/phase_15/Phase_15_vision_final.md` — нормативный дизайн (PRE-1..PRE-3 закрыты).
- `_tasks_/phase_15/Phase_15_plan.md` — шаги S1..S5, критерии приёмки.
- `_tasks_/phase_15/Phase_15_result.md` — коммиты, отклонения.
- `_tasks_/BACKLOG.md` — запись P3 «GUI-действия deploy plan/apply + рендер плана деплоя» удалена (закрыта).
- `_phases_/Phase_12.md` — `DeployApplyService`, `DeltaPlan` (контекст).
- `_phases_/Phase_14.md` — `DeltaViewerWindow` (образец).
- `_phases_/Phase_11.md` — `SafetyGateService` (предыдущий шаг chain).
- `LESSONS_LEARNED.md` §42, §43, §40, §44 — применённые уроки.
- `README.md` §GUI — обновлённый сценарий «Deploy plan / apply на существующую БД».
- `_checkpoints_/<YYYYMMDD>_NNN_checkpoint.md` — закрытие фазы.