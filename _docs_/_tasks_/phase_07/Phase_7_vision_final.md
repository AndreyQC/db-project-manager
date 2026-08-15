# Phase 7 (GUI): Панель действий для подключений — vision (final)

> Контекст:
> - `_docs_/_tasks_/phase_07/Phase_7_vision_draft.md` — история обсуждения (USER_INPUT U1–U7)
> - `_docs_/_checkpoints_/20260720_004_checkpoint.md` — снапшот проекта на старте фазы
> - `LESSONS_LEARNED.md` §11 (сетевые/UI-действия — в QRunnable), §9 (typer мультикомандность)
> - `src/db_project_manager/presentation/gui/main_window.py` — текущее окно
> - `src/db_project_manager/presentation/cli/main.py` — CLI-команды (источник для «Копировать CLI»)

Дата: 2026-07-24
Статус: FINAL — все USER_INPUT draft'а закрыты (см. §13 draft'а). Существенные
изменения — только через новый draft (`..._v2_draft.md`).

---

## 1. Цель

Перестроить главное окно GUI вокруг концепции «действие над проектом/подключением»:

- сохранить существующий функционал управления подключениями (add/edit/delete);
- добавить **dropdown с реестром действий** приложения (расширяемый — со временем
  действий будет больше);
- унифицировать для всех действий один контейнер: кнопки **«Настроить…»** и
  **«Выполнить»**, текстовый блок с текущими настройками, **копирование CLI-команды**;
- настройки каждого действия **персистировать** (`gui_settings.json`) и подставлять
  последние значения при повторном открытии диалога.

Стартовый список действий:

| # | action_id | Действие | CLI-эквивалент |
|---|-----------|----------|----------------|
| 1 | `reverse_engineer` | Создать проект базы по подключению PG | `db-pm reverse-engineer` |
| 2 | `deploy_validate` | Выполнить тестовый деплой из проекта базы | `db-pm deploy validate` |
| 3 | `graph_prepare` | Подготовить граф для просмотра в Gephi | `db-pm graph build` (+ `graph export`) |

---

## 2. As-is (текущее состояние)

`main_window.py`:

- Группа «Подключения» — `ConnectionListWidget` (список + кнопки
  «Добавить…»/«Изменить…»/«Удалить», double-click = edit). **Остаётся без изменений.**
- Группа «Действия» — глобальное поле «Папка вывода», кнопка
  «Сгенерировать скрипты объектов БД» (`_on_run`), кнопка «Deploy validate…» (`_on_deploy`).
- `_on_deploy` собирает ad-hoc `QDialog` inline (`main_window.py:236-253`) —
  подход не масштабируется на новые действия.
- Настройки диалогов не сохраняются: при каждом открытии deploy-диалога prefix
  и чекбоксы сброшены.
- Workers: `ReverseEngineerWorker`, `DeployValidateWorker`
  (`widgets/workers.py`) — уже на QRunnable/QThreadPool (lesson 11).

---

## 3. To-be UX

### 3.1. Раскладка главного окна

```
+-------------------------------------------------------------------+
| Подключения                 | Действие                            |
| +-------------------------+ | +---------------------------------+ |
| | qr_pamyat               | | | [ Действие: (dropdown)      v ] | |
| | test_db                 | | +---------------------------------+ |
| +-------------------------+ | +---------------------------------+ |
| [Добавить..][Изменить..]  | | | Настройки: (read-only текст)  | |
| [Удалить]                 | | |   подключение: qr_pamyat      | |
|                           | | |   каталог вывода: C:/out/qr   | |
|                           | | +---------------------------------+ |
|                           | | [Настроить..] [Выполнить]         | |
|                           | | +---------------------------------+ |
|                           | | | CLI: db-pm reverse-engineer ..  | |
|                           | | +----------------------[Копировать]| |
|                           | +---------------------------------+ |
+-------------------------------------------------------------------+
| [progress]                                                        |
| [status log]                                                      |
| Проект базы данных (ProjectViewer)                                |
+-------------------------------------------------------------------+
```

- Dropdown (`QComboBox`) располагается **справа от списка подключений**, в группе
  «Действие» (решение U2: на уровне панели, не per-row).
- При выборе пункта dropdown контейнер ниже переключается на это действие:
  показывает его последние сохранённые настройки (или дефолты) и его CLI-команду.
- Кнопки «Настроить…» / «Выполнить» / «Копировать CLI» — **общие для всех действий**;
  поведение параметризуется выбранным действием.

### 3.2. Сценарии

1. **Выбор действия** → контейнер показывает сохранённые настройки действия.
   Если настроек нет — показываются дефолты (подключение = выбранное в списке,
   каталог = `cfg.paths.default_output_dir`).
2. **«Настроить…»** → модальный диалог действия, поля предзаполнены последними
   настройками. По **OK** настройки сохраняются в `gui_settings.json` и обновляют
   label + CLI-строку. По **Cancel** — ничего не меняется.
3. **«Выполнить»** → если обязательные поля настроек пусты (`required_fields`),
   сначала автоматически открывается диалог «Настроить…» (решение U7); иначе
   запускается worker действия в `QThreadPool` (прогресс/статус — в существующие
   виджеты).
4. **«Копировать CLI»** → сформированная CLI-команда кладётся в clipboard
   (`QGuiApplication.clipboard()`), в status log пишется подтверждение.

---

## 4. Реестр действий (расширяемость)

Действия не хардкодятся в `MainWindow`, а описываются декларативно:

```python
# presentation/gui/actions/registry.py
@dataclass(frozen=True)
class ActionSpec:
    action_id: str                 # "reverse_engineer"
    title: str                     # "Создать проект базы по подключению PG"
    settings_model: type[BaseModel]  # pydantic-модель настроек действия
    make_dialog: Callable[..., QDialog]      # фабрика диалога «Настроить…»
    make_worker: Callable[..., QRunnable]    # фабрика worker'а для «Выполнить»
    build_cli: Callable[[BaseModel, ConnectionStore], str]  # CLI-строка
    required_fields: tuple[str, ...]         # что проверить перед «Выполнить»

ACTIONS: list[ActionSpec] = [...]  # dropdown заполняется из этого списка
```

Добавление нового действия = новый `ActionSpec` + модель настроек + диалог +
worker. `MainWindow` и `ActionPanelWidget` не меняются.

---

## 5. Компонентная архитектура (новые/изменяемые файлы)

| Файл | Статус | Что внутри |
|------|--------|-----------|
| `presentation/gui/actions/__init__.py` | new | пакет действий |
| `presentation/gui/actions/registry.py` | new | `ActionSpec`, `ACTIONS`, поиск по `action_id` |
| `presentation/gui/actions/models.py` | new | pydantic-модели настроек: `ReverseEngineerSettings`, `DeployValidateSettings`, `GraphPrepareSettings` |
| `presentation/gui/actions/dialogs.py` | new | диалоги «Настроить…» (по одному на действие) |
| `presentation/gui/actions/cli.py` | new | чистые функции `build_cli(settings, store) -> str` |
| `presentation/gui/widgets/action_panel.py` | new | `ActionPanelWidget`: dropdown + label настроек + кнопки + CLI-строка + «Копировать» |
| `infrastructure/config/gui_settings.py` | new | `GuiSettingsStore` — чтение/запись `gui_settings.json` |
| `presentation/gui/widgets/workers.py` | edit | +`GraphBuildWorker` (build + опционально export/validate) |
| `presentation/gui/main_window.py` | edit | удалить «Папка вывода»/run/deploy кнопки; вставить `ActionPanelWidget`; `_on_deploy` inline-диалог удалить |
| `tests/unit/test_gui_settings.py` | new | roundtrip settings.json, дефолты, битый JSON |
| `tests/unit/test_action_cli.py` | new | соответствие CLI-строк реальным typer-командам |
| `tests/unit/test_action_registry.py` | new | уникальность `action_id`, полнота `ACTIONS` |

---

## 6. Настройки действий и диалоги

### 6.1. Действие 1 — `reverse_engineer`

Диалог «Настроить…»:

| Поле | Контрол | Дефолт/prefill |
|------|---------|----------------|
| Подключение | `QComboBox` из `ConnectionStore.list_names()` | последнее сохранённое; иначе выбранное в списке |
| Каталог вывода | `QLineEdit` + «Выбрать…» (`QFileDialog.getExistingDirectory`) | последнее сохранённое; иначе `cfg.paths.default_output_dir` |

Worker: существующий `ReverseEngineerWorker`.

### 6.2. Действие 2 — `deploy_validate`

Диалог «Настроить…» (переносит inline-диалог из `_on_deploy`, добавляя выбор
каталога и подключения, чтобы не плодить окна):

| Поле | Контрол | Дефолт/prefill |
|------|---------|----------------|
| Каталог кодовой базы | `QLineEdit` + «Выбрать…» | последнее; иначе `default_output_dir` |
| Подключение (куда) | `QComboBox` | последнее; иначе выбранное в списке |
| Префикс имени БД | `QLineEdit` (placeholder: имя каталога кодовой базы) | последнее; пусто = дефолт сервиса |
| Оставить временную БД | `QCheckBox` (`keep_db`) | последнее; дефолт off |
| Продолжать при ошибках views/functions/procedures | `QCheckBox` (`continue_on_error`) | последнее; дефолт off |

Worker: существующий `DeployValidateWorker`.

### 6.3. Действие 3 — `graph_prepare`

| Поле | Контрол | Дефолт/prefill |
|------|---------|----------------|
| Каталог кодовой базы | `QLineEdit` + «Выбрать…» | последнее; иначе `default_output_dir` |
| Формат экспорта | `QComboBox`: `graphml` / `json` / `dot` / «только build» | последнее; дефолт `graphml` (Gephi) |
| Проверить граф (циклы, висячие ссылки) | `QCheckBox` | дефолт on (решение U5) |

Worker: новый `GraphBuildWorker` — `BuildGraphService.build_and_store(dir)`,
опционально `export_graph(...)` и toposort-валидация (см. CLI `graph validate`).
Просмотр — во внешнем **Gephi** (решение U4): существующий экспортер
(`infrastructure/graph/export.py`) уже пишет GraphML «for Gephi / yEd»
с атрибутами узлов (`object_type`, `object_schema`, `build`) и рёбер
(`relation`, `action`), поэтому новый формат не нужен. Нативный GEXF —
опционально, BACKLOG.

---

## 7. Персистентность настроек (`gui_settings.json`)

Новый `GuiSettingsStore` (`infrastructure/config/gui_settings.py`).

- **Формат** — JSON (настройки UI пишет только приложение, human-editing не
  требуется).
- **Расположение** — `./gui_settings.json` в корне проекта, рядом с
  `config.yaml` (решение U3); добавить в `.gitignore` (как `connections/*`,
  см. lesson 12 про negation-правила).
- **Запись** — по OK диалога «Настроить…» и после успешного «Выполнить»
  (успешный запуск фиксирует настройки даже без явного сохранения).
- **Устойчивость** — битый/отсутствующий файл → дефолты + warning в лог,
  приложение не падает.
- Секретов в файле нет (только имена подключений и пути) — шифрование не нужно.

Схема:

```json
{
  "version": 1,
  "last_action": "reverse_engineer",
  "actions": {
    "reverse_engineer": {
      "connection": "qr_pamyat",
      "output_dir": "C:/work/qr_codebase"
    },
    "deploy_validate": {
      "codebase_dir": "C:/work/qr_codebase",
      "connection": "test_server",
      "prefix": "",
      "keep_db": false,
      "continue_on_error": false
    },
    "graph_prepare": {
      "codebase_dir": "C:/work/qr_codebase",
      "format": "graphml",
      "validate": true
    }
  }
}
```

---

## 8. Копирование CLI-команды

`build_cli(settings, store)` — чистая функция на действие; результат показывается
в отдельной однострочной read-only `QLineEdit` контейнера (решение U6) и
копируется кнопкой «Копировать».

| Действие | Пример CLI-строки |
|----------|-------------------|
| reverse_engineer | `db-pm reverse-engineer --connection-file connections\qr_pamyat.yaml --output C:\work\qr_codebase` |
| deploy_validate | `db-pm deploy validate --dir C:\work\qr_codebase --connection-file connections\test_server.yaml --prefix qr --keep-db --continue-on-error` |
| graph_prepare | `db-pm graph build --dir C:\work\qr_codebase && db-pm graph export --dir C:\work\qr_codebase --format graphml` |

- Путь к файлу подключения — через `ConnectionStore.path_for(name)`
  (каталог `connections/` уже существует и gitignore'нут, CLI читает именно его).
- Невыбранные bool-флаги в строку не включаются; пустой `prefix` опускается.
- Тест `test_action_cli.py` сверяет, что сгенерированная строка парсится
  реальным typer-приложением (CliRunner) — защита от рассинхрона GUI и CLI.

---

## 9. Выполнение (workers)

- Все запуски — через `QThreadPool` (lesson 11). Существующие
  `ReverseEngineerWorker`/`DeployValidateWorker` переиспользуются без изменений.
- Новый `GraphBuildWorker` по тому же шаблону `WorkerSignals`.
- Панель действий блокирует «Выполнить» на время работы worker'а
  (аналог существующего `_set_running`).
- Результат/ошибки — в существующий status log; для deploy — сохранить текущее
  сводное QMessageBox-оповещение.

---

## 10. Что удаляется/мигрирует из текущего UI

| Элемент | Судьба |
|---------|--------|
| Глобальное поле «Папка вывода» | удаляется; каталог — per-action настройка (дефолт из `cfg.paths.default_output_dir`) |
| Кнопка «Сгенерировать скрипты объектов БД» | заменяется действием `reverse_engineer` |
| Кнопка «Deploy validate…» + inline-диалог | заменяется действием `deploy_validate` |
| `ConnectionListWidget` (add/edit/delete) | без изменений |
| `ProjectViewer`, status log, progress bar | без изменений; `ProjectViewer.set_root` дёргается после успешного reverse-engineer |

---

## 11. Тесты

- `test_gui_settings.py` — roundtrip save/load; отсутствующий файл → дефолты;
  битый JSON → дефолты + warning; частичное обновление одного действия не затирает
  другие.
- `test_action_cli.py` — для каждого действия: построенная CLI-строка
  валидируется typer CliRunner'ом (без реального запуска, мок сервисов).
- `test_action_registry.py` — уникальность `action_id`; каждый `ActionSpec`
  имеет settings_model/dialog/worker/build_cli; `required_fields` ⊆ полей модели.
- GUI-виджеты — без тестов (как сейчас; PySide6-виджеты в проекте не покрыты).

Команды проверки:

```bash
unset SSL_CERT_FILE REQUESTS_CA_BUNDLE CURL_CA_BUNDLE && uv run pytest tests/unit/ -q
uv run ruff check src/ tests/
```

---

## 12. Риски и ограничения

- **Рассинхрон GUI↔CLI.** Копируемая команда должна точно соответствовать
  typer-интерфейсу; закрывается тестом §11, но при добавлении флагов в CLI нужно
  помнить про `build_cli`.
- **settings.json в git.** Пути локальные, файл не должен попадать в репо —
  добавить в `.gitignore` (как `connections/*`, см. lesson 12 про negation).
- **Смена выбранного подключения в списке** не переписывает сохранённые
  настройки действия: сохранённое значение приоритетнее; выбранное в списке —
  только дефолт при пустых настройках.
- **Номер фазы.** Phase 7 назначена GUI-панели (решение U1); overload resolution
  (BACKLOG P1) переносится в Phase 8 — отразить при следующем checkpoint.

---

## 13. Принятые решения (сводка закрытых USER_INPUT)

| # | Вопрос | Решение |
|---|--------|---------|
| U1 | Номер фазы | GUI-панель = Phase 7; overload resolution → Phase 8 |
| U2 | Расположение dropdown | Панель справа от всего списка, не per-row |
| U3 | Файл настроек | `./gui_settings.json` в корне проекта, в .gitignore |
| U4 | Просмотр графа | Во внешнем Gephi через существующий graphml-экспорт; GEXF — BACKLOG |
| U5 | Чекбокс «Проверить граф» | On по умолчанию |
| U6 | Label настроек | Многострочный `QPlainTextEdit` + отдельная `QLineEdit` для CLI |
| U7 | «Выполнить» без настройки | Разрешено; пустые required_fields → auto-open диалога |

---

## 14. Критерии готовности

1. Dropdown действий показывает 3 действия из реестра; реестр расширяем
   добавлением одного `ActionSpec`.
2. «Настроить…» → диалог с prefill из `gui_settings.json`; OK сохраняет настройки.
3. «Выполнить» запускает соответствующий worker в фоне; UI не блокируется.
4. «Копировать CLI» кладёт в clipboard команду, идентичную ручному вызову CLI
   (проверено тестом через CliRunner).
5. Старые кнопки run/deploy и глобальная «Папка вывода» удалены; add/edit/delete
   подключений работают как раньше.
6. `uv run pytest tests/unit/ -q` — зелёный (baseline 278 + новые);
   `ruff check` — чисто.
