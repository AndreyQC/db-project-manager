# План Phase 7 — GUI: панель действий для подключений

> Дата: 2026-07-24
>
> Контекст:
> - `_docs_/_tasks_/phase_07/Phase_7_vision_final.md` — нормативный дизайн (источник решений, §1-14)
> - `_docs_/_tasks_/TASK_CONVENTIONS.md` — правила коммитов, цикл plan → result
> - `LESSONS_LEARNED.md` — §1 (uv/TLS), §11 (QRunnable), §12 (git add для `-`-каталогов, .gitignore negation)
> - `src/db_project_manager/presentation/gui/main_window.py` — текущее окно
> - `src/db_project_manager/presentation/cli/main.py` — typer-команды (контракт для build_cli)

Шаги `P7.S01…P7.S08`. Каждый шаг — отдельный коммит реализации
(`feat(gui):` / `test(...)` / `docs(...)`). Документы и код не смешивать
в одном коммите. `git add -- "_docs_/..."` (LESSONS §12).

После каждого шага зелёные (LESSONS §1):
```bash
unset SSL_CERT_FILE REQUESTS_CA_BUNDLE CURL_CA_BUNDLE && uv run pytest tests/unit/ -q
uv run ruff check src/ tests/
```
Baseline на старте: **278 unit-тестов** (checkpoint 20260720_004).

---

## P7.S01. GuiSettingsStore — персистентность настроек действий

**Файл:** `src/db_project_manager/infrastructure/config/gui_settings.py` (new)

Класс `GuiSettingsStore` (vision §7):

1. `__init__(path: str | Path = "gui_settings.json")` — дефолт в корне проекта
   (решение U3).
2. `load() -> dict` — читает JSON; отсутствующий файл → `{}`; битый JSON или
   не-dict корень → warning в лог + `{}` (приложение не падает).
3. `get_action_settings(action_id: str) -> dict` — секция `actions[action_id]`
   или `{}`.
4. `save_action_settings(action_id: str, settings: dict) -> None` — читает
   текущее состояние файла, обновляет только одну секцию (не затирает другие
   действия), пишет атомарно (temp-file + replace), `ensure_ascii=False`,
   `indent=2`.
5. `get_last_action() / set_last_action(action_id)` — поле `last_action`.
6. Формат файла: `{"version": 1, "last_action": ..., "actions": {...}}`
   (схема — vision §7). При записи `version` выставляется всегда.

Без шифрования (секретов нет — только имена подключений и пути).

**Тесты:** `tests/unit/test_gui_settings.py` (new):
- roundtrip save/load для двух действий — секции независимы;
- отсутствующий файл → `{}` без исключений;
- битый JSON → `{}` + warning (caplog), не падает;
- не-dict корень (`[1,2]`) → `{}`;
- `last_action` roundtrip;
- частичное обновление: save одного действия не затирает другое;
- `version: 1` присутствует после save.

**Коммит:** `feat(gui): add gui_settings.json store for action settings (P7.S01)`

---

## P7.S02. Модели настроек и build_cli — чистая логика без Qt

**Файлы:**
- `src/db_project_manager/presentation/gui/actions/__init__.py` (new, пустой)
- `src/db_project_manager/presentation/gui/actions/models.py` (new)
- `src/db_project_manager/presentation/gui/actions/cli.py` (new)

1. **Модели** (pydantic v2, `extra="ignore"` — устойчивость к будущим полям;
   LESSONS §30: None для dict-полей не передавать):
   ```python
   class ReverseEngineerSettings(BaseModel):
       connection: str = ""
       output_dir: str = ""

   class DeployValidateSettings(BaseModel):
       codebase_dir: str = ""
       connection: str = ""
       prefix: str = ""
       keep_db: bool = False
       continue_on_error: bool = False

   class GraphPrepareSettings(BaseModel):
       codebase_dir: str = ""
       format: str = "graphml"          # graphml|json|dot|none ("только build")
       validate: bool = True
   ```
   Пустая строка = «не настроено» (используется в `required_fields` и для
   дефолтов из выбранного подключения / `cfg.paths.default_output_dir`).

2. **CLI-билдеры** — чистые функции (vision §8):
   ```python
   def build_cli_reverse_engineer(s: ReverseEngineerSettings, store: ConnectionStore) -> str
   def build_cli_deploy_validate(s: DeployValidateSettings, store: ConnectionStore) -> str
   def build_cli_graph_prepare(s: GraphPrepareSettings, store: ConnectionStore) -> str
   ```
   - путь подключения — `store.path_for(name)` (позиционная часть
     `--connection-file`); имя файла показываем как `connections\<name>.yaml`;
   - bool-флаги (`--keep-db`, `--continue-on-error`) включаются только при True;
   - пустой `prefix` опускается;
   - `graph_prepare`: одна команда `db-pm graph build --dir <dir>` при
     `format="none"`, иначе `build && export --format <fmt>`; при
     `validate=True` третья команда `&& db-pm graph validate --dir <dir>`;
   - пути с пробелами — в двойных кавычках.

**Тесты:** `tests/unit/test_action_cli.py` (new):
- точные строки для каждого действия (fake `ConnectionStore` на tmp_path);
- флаги off → отсутствуют в строке; пустой prefix → нет `--prefix`;
- путь с пробелом → закавычен;
- **контракт с typer**: сгенерированные argv парсятся реальным приложением
  через `typer.testing.CliRunner` с замоканными сервисами
  (`build_default_service`, `DeployValidateService`, `BuildGraphService`,
  `graph_store`, `export_graph` — monkeypatch), exit code == 0 — защита от
  рассинхрона GUI↔CLI (vision §12).

**Коммит:** `feat(gui): action settings models and CLI builders (P7.S02)`

---

## P7.S03. Реестр действий

**Файл:** `src/db_project_manager/presentation/gui/actions/registry.py` (new)

1. `ActionSpec` (frozen dataclass, vision §4):
   `action_id`, `title`, `settings_model`, `make_dialog`, `make_worker`,
   `build_cli`, `required_fields`.
2. `ACTIONS: list[ActionSpec]` — три действия (порядок = порядок в dropdown):
   | action_id | title | required_fields |
   |-----------|-------|-----------------|
   | `reverse_engineer` | `Создать проект базы по подключению PG` | `("connection", "output_dir")` |
   | `deploy_validate` | `Выполнить тестовый деплой из проекта базы` | `("codebase_dir", "connection")` |
   | `graph_prepare` | `Подготовить граф для просмотра в Gephi` | `("codebase_dir",)` |
3. `get_action(action_id) -> ActionSpec` — lookup с понятной ошибкой.
4. `make_dialog` / `make_worker` — ленивые фабрики (импорт dialogs/workers
   внутри функций), чтобы registry можно было импортировать в тестах без
   создания виджетов. Dialogs/workers появятся в S04/S05 — на этом шаге
   фабрики ссылаются на них forward-reference'ом (шаг S03 коммитится вместе
   с интерфейсами; тесты не вызывают фабрики).

**Тесты:** `tests/unit/test_action_registry.py` (new):
- `action_id` уникальны;
- для каждого spec: `settings_model` — подкласс BaseModel, `build_cli`
  callable, `required_fields` ⊆ полей `settings_model`;
- `get_action` для всех трёх + KeyError на неизвестный.

**Коммит:** `feat(gui): action registry with three action specs (P7.S03)`

---

## P7.S04. Диалоги «Настроить…»

**Файл:** `src/db_project_manager/presentation/gui/actions/dialogs.py` (new)

Общий базовый класс + три диалога (поля — vision §6.1-6.3):

1. `BaseActionDialog(QDialog)`: `QFormLayout` + `QDialogButtonBox(Ok|Cancel)`;
   метод `settings() -> BaseModel` (собрать из контролов), `set_settings(model)`
   (prefill). Хелпер `_dir_row()` — `QLineEdit` + кнопка «Выбрать…»
   (`QFileDialog.getExistingDirectory`, стартовый каталог = текущее значение
   или `Path.home()`).
2. `ReverseEngineerDialog` — combo подключений
   (`ConnectionStore.list_names()`), каталог вывода.
3. `DeployValidateDialog` — каталог кодовой базы, combo подключений, prefix
   (`QLineEdit` + placeholder «по умолчанию: имя каталога кодовой базы»),
   чекбоксы `keep_db` / `continue_on_error` (тексты — как в текущем
   `_on_deploy`, main_window.py:241-242).
4. `GraphPrepareDialog` — каталог кодовой базы, combo формата
   (`graphml` / `json` / `dot` / «только build» → `none`), чекбокс validate.
5. Prefill вне диалогов (в панели, S06): сохранённые настройки → дефолты
   (выбранное в списке подключение, `cfg.paths.default_output_dir`).

GUI-виджеты тестами не покрываем (как сейчас, vision §11).

**Коммит:** `feat(gui): per-action settings dialogs (P7.S04)`

---

## P7.S05. GraphBuildWorker

**Файл:** `src/db_project_manager/presentation/gui/widgets/workers.py` (edit)

Новый `GraphBuildWorker(QRunnable)` по шаблону существующих
(`WorkerSignals`, LESSONS §11):

1. `__init__(codebase_dir, *, fmt: str = "graphml", validate: bool = True)`.
2. `run()`:
   - `BuildGraphService().build_and_store(codebase_dir)` → статус
     «вершин=N, рёбер=M»;
   - `validate`: `topological_sort(graph)` + `graph.dangling_edges()` →
     при циклах/висячих ссылках `signals.error` (как CLI `graph validate`);
   - `fmt != "none"`: `export_graph(graph, fmt, graph_store.graph_dir_for(dir)
     / f"graph.{fmt}")` → статус с путём;
   - `signals.finished.emit(path)` — путь к `.dbm_graph/` (или к файлу
     экспорта).
3. Исключения — как в `DeployValidateWorker`: понятное сообщение в
   `signals.error`, `finished.emit(None)`.

**Тесты:** без новых (логика — в покрытых сервисах; worker — тонкая
обвязка, как существующие workers без тестов). При простой возможности —
прямой вызов `run()` со signal-захватом в `test_action_registry.py` не
требуется; оставляем консистентно с проектом.

**Коммит:** `feat(gui): GraphBuildWorker for graph_prepare action (P7.S05)`

---

## P7.S06. ActionPanelWidget — контейнер действия

**Файл:** `src/db_project_manager/presentation/gui/widgets/action_panel.py` (new)

Виджет группы «Действие» (vision §3.1, решения U2/U6/U7):

1. **Состав:** `QComboBox` действий (из `ACTIONS`, userData = `action_id`);
   read-only `QPlainTextEdit` (3-4 строки, настройки `ключ: значение`);
   кнопки «Настроить…» / «Выполнить»; read-only `QLineEdit` с CLI-строкой +
   кнопка «Копировать».
2. **Конструктор:** `ActionPanelWidget(store: ConnectionStore,
   settings_store: GuiSettingsStore, cfg: CFG, get_selected_connection:
   Callable[[], str | None], parent=None)`. Текущее подключение из списка —
   через callback (панель не владеет списком).
3. **Состояние:** при смене действия — загрузить настройки из
   `GuiSettingsStore`, подставить дефолты для пустых полей (подключение =
   `get_selected_connection()`, каталог = `cfg.paths.default_output_dir`),
   обновить label + CLI. Выбор действия восстанавливается из `last_action`.
4. **«Настроить…»** → `spec.make_dialog(...)`; prefill текущими настройками;
   по Accepted — `settings_store.save_action_settings(...)`, обновить
   label/CLI. По Cancel — без изменений.
5. **«Выполнить»** → если `required_fields` пусты (после подстановки
   дефолтов) — сначала открыть диалог (U7); при отмене — прервать. Иначе
   emit `execute_requested(action_id, settings_model_instance)`.
6. **«Копировать»** → `QGuiApplication.clipboard().setText(cli)`; emit
   `status_message("CLI скопирована в буфер обмена")`.
7. **Сигналы:** `execute_requested(str, object)`, `status_message(str)`.
   `set_running(bool)` — блокирует «Выполнить»/«Настроить…»/dropdown на
   время worker'а.
8. После успешного выполнения MainWindow вызывает
   `panel.mark_executed()` → сохранить настройки + `set_last_action`
   (vision §7: успешный запуск фиксирует настройки).

**Коммит:** `feat(gui): ActionPanelWidget with configure/run/copy-cli (P7.S06)`

---

## P7.S07. Интеграция в MainWindow + .gitignore

**Файлы:**
- `src/db_project_manager/presentation/gui/main_window.py` (edit)
- `.gitignore` (edit)

1. **Удалить** (vision §10): глобальное поле «Папка вывода» + кнопку
   «Выбрать…», `run_btn`/`_on_run`, `deploy_btn`/`_on_deploy` (вместе с
   inline-диалогом), `_on_select_output`. Группа «Действия» заменяется на
   `ActionPanelWidget`.
2. **Оставить без изменений:** `ConnectionListWidget` (add/edit/delete,
   double-click), status log, progress bar, `ProjectViewer`.
3. **Связка:**
   - `panel.execute_requested` → `_on_execute(action_id, settings)`:
     `spec.make_worker(...)` (reverse/deploy — существующие workers;
     graph — `GraphBuildWorker`), коннект signals к существующим
     `_on_progress`/`_on_error`/`_append_status`, `thread_pool.start`;
   - finished → `_set_running(False)`, `panel.set_running(False)`,
     `panel.mark_executed()`; для reverse-engineer — обновить
     `ProjectViewer.set_root(output_dir)`; для deploy — сохранить сводное
     QMessageBox-оповещение (успех/список ошибок, main_window.py:282-306);
   - `panel.status_message` → `_append_status`;
   - `_set_running` теперь делегирует в `panel.set_running`.
4. **.gitignore:** добавить `gui_settings.json` (рядом с правилом
   `connections/*`; LESSONS §12 — проверить, что правило не внутри
   игнорируемой директории).
5. `MainWindow.__init__` создаёт `GuiSettingsStore()` и передаёт
   `get_selected_connection=self.connection_list.selected_name`.

**Проверка вручную (smoke):** запуск `db-pm-gui` (или `python -m
...presentation.gui.main`): dropdown показывает 3 действия; «Настроить…»
помнит значения после OK; «Выполнить» без настроек открывает диалог;
«Копировать» → в буфере корректная команда; `gui_settings.json` появляется
в корне; reverse-engineer и deploy validate проходят как раньше.

**Коммиты (раздельно):**
- `feat(gui): integrate action panel into main window (P7.S07)`
- `chore(gitignore): ignore gui_settings.json (P7.S07)`

---

## P7.S08. Документация и закрытие фазы

1. `_docs_/_tasks_/phase_07/002_result_phase_07.md` — что сделано,
   проверки, известные ограничения, ссылки на коммиты (TASK_CONVENTIONS §2.1).
2. `LESSONS_LEARNED.md` — уроки Phase 7 по факту (кандидаты: особенности
   CliRunner-контракта, атомарная запись JSON на Windows, prefill-стратегия).
3. `_docs_/_checkpoints_/<YYYYMMDD>_NNN_checkpoint.md` — «Phase 7
   complete»: обновить architecture map (новый пакет `presentation/gui/actions/`),
   зафиксировать перенос overload resolution в Phase 8 (решение U1), baseline
   тестов.
4. `_docs_/_phases_/Phase_07.md` — свод фазы (PHASES_CONVENTION).
5. Коммиты раздельно: `docs(tasks):`, `docs(lessons):`, `docs(checkpoint):`,
   `docs(phase_07):`.

---

## Зависимости шагов

```
S01 → S02 → S03 → S04 ─┐
                S05 ───┴→ S06 → S07 → S08
(S03 ссылается на фабрики S04/S05 forward-reference'ом;
 S04 и S05 независимы друг от друга, порядок между ними свободный)
```

## Критерий готовности фазы

- Критерии §14 `Phase_7_vision_final.md` выполнены.
- `uv run pytest tests/unit/ -q` зелёный (278 baseline + ~20 новых);
  `uv run ruff check src/ tests/` — чисто.
- Ручной smoke-тест (S07) пройден на реальном подключении.
- В `phase_07/` есть `Phase_7_vision_final.md` (норматив), план и result;
  draft сохранён для истории.
