# GUI action для compare — final

> **Дата:** 2026-07-29
> **Ветка:** dev
> **Статус документа:** final (нормативный дизайн; все USER_INPUT закрыты в draft)
> **Предшественник:** `-=docs=-/-=tasks=-/2026-07-29/20260729_001_compare_gui_action_draft.md`
> (draft сохранён для истории обсуждения — TASK_CONVENTIONS §2.2)

> Контекст:
> - `-=docs=-/-=CHECKPOINTS=-/20260729_001_checkpoint.md` — Phase 9 closed (compare CLI)
> - `-=docs=-/-=tasks=-/phase_07/Phase_7_vision_final.md` — action registry (норматив)
> - `-=docs=-/-=tasks=-/2026-07-28/20260728_001_compare_db_vs_fs_draft.md` — compare CLI дизайн
> - `src/db_project_manager/application/compare_service.py` — `CompareService`, `SideSpec`, `CompareError`
> - `src/db_project_manager/domain/diff.py` — `SnapshotSourceKind` (DB/DIR)
> - `src/db_project_manager/presentation/cli/main.py` — `compare run`, `_resolve_side`
> - `LESSONS_LEARNED.md` §39 (CliRunner-контракт), §42 (PySide6 GC), §43 (кнопки диалога)

---

## 1. Цель

Добавить compare как 4-е действие в декларативный реестр Phase 7 (`ACTIONS`), чтобы
пользователь мог запустить сравнение двух состояний (БД или каталог reverse-engineer)
из GUI без CLI. CLI `db-pm compare run` (Phase 9) уже существует — GUI делегирует в
тот же `CompareService`.

---

## 2. Решения (нормативные)

| # | Параметр | Решение |
|---|---|---|
| 1 | UX диалога | **4 поля, неявное XOR**: 2 combo подключений (source/target) + 2 строки каталога (source/target). Ровно одно из connection/dir на сторону; правило проверяется при сборке `SideSpec`/CLI. |
| 2 | `required_fields` | **`("output_dir",)`** — избегает бага `_apply_defaults`, который заполнил бы source/target одним дефолтом. Стороны остаются пустыми, пользователь настраивает через «Настроить…». |
| 3 | После выполнения | **Открыть отчёт + сводка**: ветка `elif action_id == "compare"` в `_on_action_finished` → `viewer.set_root(result)` + `_append_status` со сводкой `added/removed/changed/unchanged`. |

---

## 3. Компоненты

### 3.1. `CompareSettings` (`presentation/gui/actions/models.py`)

```python
class CompareSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # Source side — exactly one of source_connection / source_dir (XOR at build time).
    source_connection: str = ""
    source_dir: str = ""
    # Target side — same XOR rule.
    target_connection: str = ""
    target_dir: str = ""
    # Report destination.
    output_dir: str = ""
    keep_model_dir: bool = False
```

### 3.2. `build_cli_compare` (`presentation/gui/actions/cli.py`)

```python
def build_cli_compare(settings: CompareSettings, store: ConnectionStore) -> str:
    parts = ["db-pm compare run", f"--output-dir {_quote(settings.output_dir)}"]
    parts += _side_cli("source", settings.source_connection, settings.source_dir, store)
    parts += _side_cli("target", settings.target_connection, settings.target_dir, store)
    if settings.keep_model_dir:
        parts.append("--keep-model-dir")
    return " ".join(parts)


def _side_cli(label, connection, dir_, store):
    if connection:
        return [f"--{label}-connection-file {_quote(str(store.path_for(connection)))}"]
    if dir_:
        return [f"--{label}-dir {_quote(dir_)}"]
    return []
```

Соответствует CLI `compare run` (`main.py:278-314`): `_resolve_side` enforcing XOR
(exit 2 при нарушении). CLI-команда опускает сторону, если ни одно поле не заполнено
— это вызовет понятную ошибку в CLI.

### 3.3. `CompareDialog` (`presentation/gui/actions/dialogs.py`)

4 поля + output + чекбокс. `_add_buttons()` — последней строкой (§43).

```python
class CompareDialog(BaseActionDialog):
    def __init__(self, store, settings, parent=None):
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
        self._add_buttons()   # §43 — последней

    def settings(self) -> CompareSettings: ...
```

### 3.4. `CompareWorker` (`presentation/gui/widgets/workers.py`)

Оборачивает `CompareService.run`; ловит `CompareError` → `signals.error`, затем
широкий `Exception`; всегда эмиттит `finished`. Ленивый импорт сервиса внутри `run()`.

```python
class CompareWorker(QRunnable):
    def __init__(self, source: SideSpec, target: SideSpec, output_dir: str,
                 *, keep_model_dir: bool = False): ...
    def run(self):  # noqa: C901
        from db_project_manager.application.compare_service import CompareError, CompareService
        ...
```

### 3.5. Фабрика worker'а + `_side_spec` (`presentation/gui/actions/registry.py`)

```python
def _side_spec(label, connection, dir_, store) -> SideSpec:
    if connection and dir_:
        raise ValueError(f"На сторону {label} указаны и подключение, и каталог — выберите одно.")
    if connection:
        return SideSpec(SnapshotSourceKind.DB, str(store.path_for(connection)),
                        conn_cfg=store.load_by_name(connection))
    if dir_:
        return SideSpec(SnapshotSourceKind.DIR, dir_)
    raise ValueError(f"Сторона {label} не настроена: укажите подключение или каталог.")
```

`ValueError` пробрасывается из `make_worker` → обрабатывается в `_on_execute`
(расширить существующий `try/except`).

### 3.6. `ActionSpec` запись

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

### 3.7. Метки полей (`presentation/gui/widgets/action_panel.py`)

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

### 3.8. Пост-выполнение (`presentation/gui/main_window.py`)

```python
elif action_id == "compare":
    self._viewer.set_root(str(result))   # result — Path к каталогу отчёта
    self._append_status(self._compare_summary(result))
```

`_compare_summary(result)` читает `diff_report.json` (оборачивая в `try/except`),
формирует: «Сравнение: добавлено N, удалено N, изменено N, без изменений N».

---

## 4. Изменения в существующих файлах

| Файл | Изменение |
|---|---|
| `presentation/gui/actions/models.py` | + `CompareSettings`. |
| `presentation/gui/actions/cli.py` | + `build_cli_compare`, `_side_cli`; импорт. |
| `presentation/gui/actions/dialogs.py` | + `CompareDialog`; импорт. |
| `presentation/gui/widgets/workers.py` | + `CompareWorker`. |
| `presentation/gui/actions/registry.py` | + фабрики, `_side_spec`, запись в `ACTIONS`, импорты. |
| `presentation/gui/widgets/action_panel.py` | + метки. |
| `presentation/gui/main_window.py` | + `elif "compare"`, `_compare_summary`. |
| `tests/unit/test_action_registry.py` | `test_expected_actions_present` → 4 действия. |
| `tests/unit/test_action_cli.py` | + точные строки + контракт-тест CliRunner. |
| `tests/unit/test_action_panel_smoke.py` | offscreen: 4 действия в dropdown. |

Новых файлов нет — всё в существующих модулях (по паттерну Phase 7).

---

## 5. Тесты

- `build_cli_compare`: DIR-DIR, DB-DB, DIR-DB, mixed; `--keep-model-dir` вкл/выкл; путь с пробелом.
- Контракт CliRunner: команда парсится реальным `compare run` (замоканный `CompareService` + `load_cfg`).
- `test_expected_actions_present` → 4 действия; specs complete; required_fields в модели.
- offscreen smoke: dropdown содержит 4 действия; compare-диалог создаётся, кнопки последние.

GUI-виджеты unit-тестами не покрываются (конвенция Phase 7 vision §11), кроме offscreen smoke.

---

## 6. NOT done

- Markdown-отчёт в viewer (BACKLOG P3, отдельная задача).
- Валидация XOR прямо в диалоге (disable поля) — не делается; правило в `_side_spec`/CLI.
- Progress-бар детерминированный — индикатор неопределённый, как у существующих действий.

---

## 7. Контрольный список (LESSONS)

- [ ] `_add_buttons()` последняя строка `CompareDialog.__init__` (§43).
- [ ] Worker в `_active_workers` до `finished` (§42) — механика `_on_execute` не меняется.
- [ ] Сигналы к bound-методам, не лямбдам (§42).
- [ ] `build_cli_compare` — чистая функция (тестируется без Qt).
- [ ] `_side_spec` бросает `ValueError` при нарушении XOR — обработать в `_on_execute`.
- [ ] Обновить `test_expected_actions_present`.
- [ ] Контракт-тест CliRunner для `compare run` (§39).
- [ ] `git add -- "-=docs=-/..."` (§12).
