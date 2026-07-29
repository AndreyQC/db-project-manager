# GUI action для compare — draft

> **Дата:** 2026-07-29
> **Ветка:** dev
> **Статус документа:** draft (дизайн готов к плану→результату; все USER_INPUT закрыты)

> Контекст:
> - `-=docs=-/REFRESH_CONTEXT.md` — точка входа в проект
> - `-=docs=-/-=CHECKPOINTS=-/20260729_001_checkpoint.md` — последний checkpoint (Phase 9 closed)
> - `-=docs=-/-=tasks=-/2026-07-28/20260728_001_compare_db_vs_fs_draft.md` — нормативный дизайн compare (CLI, Phase 9)
> - `-=docs=-/-=tasks=-/phase_07/Phase_7_vision_final.md` — нормативный дизайн action registry
> - `-=docs=-/-=tasks=-/BACKLOG.md` — «GUI action для compare» (P3, из Phase 9)
> - `src/db_project_manager/presentation/gui/actions/registry.py` — `ActionSpec`/`ACTIONS`
> - `src/db_project_manager/presentation/gui/actions/models.py` — модели настроек
> - `src/db_project_manager/presentation/gui/actions/cli.py` — `build_cli_*`
> - `src/db_project_manager/presentation/gui/actions/dialogs.py` — `BaseActionDialog` + подклассы
> - `src/db_project_manager/presentation/gui/widgets/workers.py` — workers
> - `src/db_project_manager/presentation/gui/main_window.py` — `_on_action_finished`
> - `src/db_project_manager/presentation/gui/widgets/action_panel.py` — `FIELD_LABELS`, `_apply_defaults`
> - `src/db_project_manager/application/compare_service.py` — `CompareService`, `SideSpec`, `CompareError`
> - `src/db_project_manager/domain/diff.py` — `SnapshotSourceKind` (DB/DIR)
> - `src/db_project_manager/presentation/cli/main.py` — `compare_run`, `_resolve_side`
> - `LESSONS_LEARNED.md` §42 (PySide6 lambda GC), §43 (кнопки диалога последними)

---

## 1. Постановка задачи

Phase 9 реализовала compare как CLI-only (`db-pm compare run`). BACKLOG P3 ставит
задачу добавить compare как GUI action через существующий action registry (Phase 7),
чтобы пользователь мог запустить сравнение из интерфейса без CLI.

Реестр Phase 7 декларативный: новое действие = одна запись в `ACTIONS` + модель
настроек + диалог + worker + `build_cli`. `MainWindow` и панель не меняются
механически (кроме одной опциональной ветки в `_on_action_finished`, см. §3.6).

**Сложность compare vs существующих действий:** две стороны (source/target), каждая
может быть БД или каталогом. Существующие действия имеют максимум один каталог +
одно подключение (deploy_validate). Compare имеет 2+2 поля + правило XOR на сторону.

---

## 2. Принятые решения (закрытые USER_INPUT)

| # | Параметр | Решение | Обоснование |
|---|---|---|---|
| 1 | UX диалога | **4 поля, неявное XOR** | 2 combo подключений (source/target) + 2 строки каталога (source/target). Пользователь заполняет ровно одно из двух на сторону; правило проверяется при сборке `SideSpec`/CLI. Просто, по образцу deploy_validate, но 4 поля. |
| 2 | `required_fields` | **Только `output_dir`** | Избегает бага авто-дефолтов: `_apply_defaults` заполняет все `_dir` одним дефолтом (footgun — source и target совпали бы). Каталог отчёта авто-подставляется; стороны остаются пустыми, пользователь настраивает через «Настроить…». |
| 3 | После выполнения | **Открыть отчёт + сводка** | Ветка `elif action_id == "compare"` в `_on_action_finished`: `viewer.set_root(result)` (каталог отчёта) + `_append_status` со сводкой `added/removed/changed/unchanged`. Пользователь видит отчёт сразу. |

---

## 3. Архитектурный набросок (для последующего `_plan`/`_result`)

### 3.1. Модель настроек (`models.py`)

```python
class CompareSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # Source side — exactly one of source_connection / source_dir must be set
    # (XOR enforced when building SideSpec / CLI, not by pydantic).
    source_connection: str = ""
    source_dir: str = ""
    # Target side — same XOR rule.
    target_connection: str = ""
    target_dir: str = ""
    # Report destination.
    output_dir: str = ""
    keep_model_dir: bool = False
```

Пустая строка = «не настроено» (конвенция модуля). `required_fields = ("output_dir",)`.

### 3.2. CLI-билдер (`cli.py`)

```python
def build_cli_compare(settings: CompareSettings, store: ConnectionStore) -> str:
    parts = ["db-pm compare run", f"--output-dir {_quote(settings.output_dir)}"]
    parts += _side_cli("source", settings.source_connection, settings.source_dir, store)
    parts += _side_cli("target", settings.target_connection, settings.target_dir, store)
    if settings.keep_model_dir:
        parts.append("--keep-model-dir")
    return " ".join(parts)

def _side_cli(label: str, connection: str, dir_: str, store: ConnectionStore) -> list[str]:
    """Emit exactly one of --<label>-connection-file / --<label>-dir."""
    if connection:
        return [f"--{label}-connection-file {_quote(str(store.path_for(connection)))}"]
    if dir_:
        return [f"--{label}-dir {_quote(dir_)}"]
    return []  # neither set — CLI will exit 2 with a clear message
```

Соответствует CLI-команде `compare run` (`main.py:278-314`) и её `_resolve_side`
(XOR-проверка на стороне CLI, exit code 2 при нарушении).

### 3.3. Диалог (`dialogs.py`)

```python
class CompareDialog(BaseActionDialog):
    def __init__(self, store, settings: CompareSettings, parent=None):
        super().__init__("Сравнение состояний — настройки", parent)
        self._source_connection = self._connections_combo(store, settings.source_connection)
        self._form.addRow("Source: подключение (БД):", self._source_connection)
        self._source_dir = self._dir_row(settings.source_dir, "Source: каталог reverse-engineer:")
        self._target_connection = self._connections_combo(store, settings.target_connection)
        self._form.addRow("Target: подключение (БД):", self._target_connection)
        self._target_dir = self._dir_row(settings.target_dir, "Target: каталог reverse-engineer:")
        self._output_dir = self._dir_row(settings.output_dir, "Каталог для отчётов:")
        self._keep_model_dir = QCheckBox("Сохранить временный каталог reverse-engineer")
        self._keep_model_dir.setChecked(settings.keep_model_dir)
        self._form.addRow(self._keep_model_dir)
        self._add_buttons()   # LESSONS §43 — последней строкой

    def settings(self) -> CompareSettings:
        return CompareSettings(
            source_connection=self._source_connection.currentText().strip(),
            source_dir=self._source_dir.text().strip(),
            target_connection=self._target_connection.currentText().strip(),
            target_dir=self._target_dir.text().strip(),
            output_dir=self._output_dir.text().strip(),
            keep_model_dir=self._keep_model_dir.isChecked(),
        )
```

Подсказка в диалоге о правиле XOR (placeholder или label-текст): «На каждую сторону
укажите **либо** подключение, **либо** каталог». Реализация placeholder — на
усмотрение плана.

### 3.4. Worker (`workers.py`)

```python
class CompareWorker(QRunnable):
    def __init__(self, source: SideSpec, target: SideSpec, output_dir: str,
                 *, keep_model_dir: bool = False):
        super().__init__()
        self.signals = WorkerSignals()
        self._source = source
        self._target = target
        self._output_dir = output_dir
        self._keep_model_dir = keep_model_dir

    def run(self):  # noqa: C901
        from db_project_manager.application.compare_service import CompareError, CompareService
        service = CompareService()

        def progress(msg, cur, total):
            self.signals.progress.emit(msg, cur, total)
            self.signals.status.emit(msg)

        try:
            result = service.run(
                self._source, self._target, self._output_dir,
                keep_model_dir=self._keep_model_dir, progress=progress,
            )
            self.signals.finished.emit(result)
        except CompareError as e:
            self.signals.error.emit(str(e))
            self.signals.finished.emit(None)
        except Exception as e:  # noqa: BLE001
            self.signals.error.emit(f"Непредвиденная ошибка: {e}")
            self.signals.finished.emit(None)
```

Сильная ссылка на worker удерживается `MainWindow._active_workers` (урок §42) —
механика `_on_execute` не меняется.

### 3.5. Фабрика worker'а (`registry.py`)

```python
def _make_compare_worker(store, settings: CompareSettings):
    from db_project_manager.presentation.gui.widgets.workers import CompareWorker
    return CompareWorker(
        _side_spec("source", settings.source_connection, settings.source_dir, store),
        _side_spec("target", settings.target_connection, settings.target_dir, store),
        settings.output_dir,
        keep_model_dir=settings.keep_model_dir,
    )

def _side_spec(label, connection, dir_, store) -> SideSpec:
    """Build a SideSpec from XOR fields; raises ValueError if both/neither set."""
    from db_project_manager.domain.diff import SnapshotSourceKind
    if connection and dir_:
        raise ValueError(f"На сторону {label} указаны и подключение, и каталог — выберите одно.")
    if connection:
        return SideSpec(SnapshotSourceKind.DB, str(store.path_for(connection)),
                        conn_cfg=store.load_by_name(connection))
    if dir_:
        return SideSpec(SnapshotSourceKind.DIR, dir_)
    raise ValueError(f"Сторона {label} не настроена: укажите подключение или каталог.")
```

`ValueError` пробрасывается из `make_worker` → ловится в `_on_execute` (существующий
`try/except ConnectionStoreError` нужно расширить до `except Exception` с понятным
сообщением, ИЛИ ловить `ValueError` отдельно). Это точка дизайна для плана.

### 3.6. `ActionSpec` + `ACTIONS` (`registry.py`)

```python
ActionSpec(
    action_id="compare",
    title="Сравнить состояния (БД или каталог reverse-engineer)",
    settings_model=CompareSettings,
    make_dialog=_make_compare_dialog,
    make_worker=_make_compare_worker,
    build_cli=build_cli_compare,
    required_fields=("output_dir",),
),
```

### 3.7. Метки полей (`action_panel.py`)

```python
FIELD_LABELS += {
    "source_connection": "source: подключение",
    "source_dir": "source: каталог",
    "target_connection": "target: подключение",
    "target_dir": "target: каталог",
    "keep_model_dir": "сохранить reverse-engineer",
}
ACTION_FIELD_LABELS["compare"] = {"output_dir": "каталог для отчётов"}
```

### 3.8. Пост-выполнение (`main_window.py`)

В `_on_action_finished` добавить ветку:
```python
elif action_id == "compare":
    self._viewer.set_root(str(result))   # result — Path к каталогу отчёта
    self._append_status(self._compare_summary(result))
```

`_compare_summary(result)` читает `diff_report.json` из каталога отчёта и формирует
строку: «Сравнение: добавлено N, удалено N, изменено N, без изменений N».
Чтение JSON оборачивается в `try/except` (отчёт может отсутствовать при ошибке) —
fallback на `_append_status(f"Готово: {result}")`.

---

## 4. Изменения в существующих файлах

| Файл | Изменение |
|---|---|
| `presentation/gui/actions/models.py` | + `CompareSettings` (6 полей). |
| `presentation/gui/actions/cli.py` | + `build_cli_compare`, `+ _side_cli` helper; импорт `CompareSettings`. |
| `presentation/gui/actions/dialogs.py` | + `CompareDialog`; импорт `CompareSettings`. |
| `presentation/gui/widgets/workers.py` | + `CompareWorker`. |
| `presentation/gui/actions/registry.py` | + `_make_compare_dialog`, `_make_compare_worker`, `_side_spec`; + запись в `ACTIONS`; импорты. |
| `presentation/gui/widgets/action_panel.py` | + метки в `FIELD_LABELS`, `ACTION_FIELD_LABELS["compare"]`. |
| `presentation/gui/main_window.py` | + `elif action_id == "compare"` в `_on_action_finished`; + `_compare_summary`. |
| `tests/unit/test_action_registry.py` | обновить `test_expected_actions_present` (добавить `"compare"`). |
| `tests/unit/test_action_cli.py` | + точные строки `build_cli_compare` + контракт-тест CliRunner. |

Новые файлы: **нет** — всё в существующих модулях GUI (по паттерну Phase 7).

---

## 5. Тесты

| Файл | Что покрывает |
|---|---|
| `tests/unit/test_action_cli.py` (обновить) | Точные строки `build_cli_compare`: DIR-DIR, DB-DB, DIR-DB; `--keep-model-dir` вкл/выкл; путь с пробелом → кавычки. Контракт CliRunner: сгенерированная команда парсится реальным `compare run` (с замоканным `CompareService` + `load_cfg`). |
| `tests/unit/test_action_registry.py` (обновить) | `test_expected_actions_present` → 4 действия; `test_specs_are_complete` проходит для compare; `test_required_fields_exist_in_model` — `output_dir` есть в `CompareSettings`. |
| `tests/unit/test_action_panel_smoke.py` (обновить) | offscreen: dropdown содержит 4 действия; compare-диалог создаётся, кнопки последниe (§43); `_active_workers` после прогона пуст (нет утечки ссылок, §42). |

GUI-виджеты тестами не покрываются (конвенция Phase 7, vision §11), кроме offscreen
smoke. Логика вынесена в тестируемые слои: `build_cli_compare` (чистая функция),
`_side_spec` (чистая, бросает `ValueError`), `CompareSettings` (pydantic).

---

## 6. NOT done / отложено

- **Markdown-отчёт** в viewer — `diff_report.json` открывается в viewer как файл, но
  человекочитаемого markdown-свода пока нет (BACKLOG P3, отдельная задача). Viewer
  покажет JSON с подсветкой SQL (для `.json` подсветки нет — отобразится как текст).
- **Валидация XOR прямо в диалоге** (disable одного поля при заполнении другого) —
  не делается; правило проверяется при сборке `SideSpec`/CLI. UX-улучшение → BACKLOG.
- **Progress-бар для reverse-engineer стороны БД** — `CompareService` уже эмиттит
  progress-сообщения; worker прокидывает их в `signals.progress`. Индикатор
  неопределённый (`setRange(0, 0)`) — как у существующих действий.

---

## 7. Контрольный список (по `LESSONS_LEARNED.md`)

- [ ] `_add_buttons()` — последняя строка `__init__` `CompareDialog` (§43).
- [ ] Worker удерживается сильной ссылкой через `_active_workers` (§42) — механика
  `_on_execute` не меняется, но убедиться, что `CompareWorker` не собирается GC
  до `finished`.
- [ ] Сигналы подключать к bound-методам (`_on_progress`, `_on_error`,
  `_on_worker_finished`), не к лямбдам (§42).
- [ ] `build_cli_compare` — чистая функция, тестируется без Qt.
- [ ] `_side_spec` — бросает `ValueError` при обоих/ни одного поля; обработать в
  `_on_execute` (понятное сообщение, не падать).
- [ ] Обновить точное-множество `test_expected_actions_present` (иначе тест регрессит).
- [ ] Контракт-тест CliRunner для `compare run` (страховка от рассинхрона GUI↔CLI, §39).
- [ ] `git add -- "-=docs=-/..."` для путей с `-` (§12).

---

## 8. Кросс-ссылки

- `-=docs=-/-=tasks=-/phase_09/002_result_phase_09.md` — результат CLI-реализации compare.
- `-=docs=-/-=tasks=-/phase_07/Phase_7_vision_final.md` §4 — паттерн `ActionSpec`/`ACTIONS`.
- `-=docs=-/-=tasks=-/BACKLOG.md` — «GUI action для compare» (P3).
- `LESSONS_LEARNED.md` §39 (CliRunner-контракт), §42 (PySide6 GC), §43 (кнопки диалога).
- `src/db_project_manager/application/compare_service.py` — `CompareService.run`, `SideSpec`.

---

## 9. Статус USER_INPUT

Все ключевые развилки закрыты через Q&A с пользователем (см. §2). Открытых вопросов
для дизайна нет — документ готов к переходу `_draft` → `_plan`/`_result`.
