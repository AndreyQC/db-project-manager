# Phase 7 (GUI): Панель действий для подключений — vision (draft)

> Контекст:
> - `-=docs=-/-=CHECKPOINTS=-/20260720_004_checkpoint.md` — текущий снапшот проекта
> - `-=docs=-/-=tasks=-/TASK_CONVENTIONS.md` §2.2 — цикл draft → final
> - `LESSONS_LEARNED.md` §11 (сетевые/UI-действия — в QRunnable), §9 (typer мультикомандность)
> - `src/db_project_manager/presentation/gui/main_window.py` — текущее окно
> - `src/db_project_manager/presentation/cli/main.py` — CLI-команды (источник для «Копировать CLI»)

Дата: 2026-07-24
Статус: DRAFT — открытые вопросы помечены `USER_INPUT` (с рекомендацией ИИ).

---

## 1. Цель

Перестроить главное окно GUI вокруг концепции «действие над проектом/подключением»:

- сохранить существующий функционал управления подключениями (add/edit/delete);
- добавить **dropdown с реестром действий** приложения (расширяемый — со временем
  действий будет больше);
- унифицировать для всех действий один контейнер: кнопки **«Настроить…»** и
  **«Выполнить»**, текстовый блок с текущими настройками, **копирование CLI-команды**;
- настройки каждого действия **персистировать** (settings.json) и подставлять
  последние значения при повторном открытии диалога.

Стартовый список действий:

| # | Действие | CLI-эквивалент |
|---|----------|----------------|
| 1 | Создать проект базы по подключению PG | `db-pm reverse-engineer` |
| 2 | Выполнить тестовый деплой из проекта базы | `db-pm deploy validate` |
| 3 | Подготовить граф для просмотра в приложении | `db-pm graph build` (+ `graph export`) |

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
  «Действие» (см. USER_INPUT U2 про per-row вариант).
- При выборе пункта dropdown контейнер ниже переключается на это действие:
  показывает его последние сохранённые настройки (или дефолты) и его CLI-команду.
- Кнопки «Настроить…» / «Выполнить» / «Копировать CLI» — **общие для всех действий**;
  поведение параметризуется выбранным действием.

### 3.2. Сценарии

1. **Выбор действия** → контейнер показывает сохранённые настройки действия.
   Если настроек нет — показываются дефолты (подключение = выбранное в списке,
   каталог = `cfg.paths.default_output_dir`).
2. **«Настроить…»** → модальный диалог действия, поля предзаполнены последними
   настройками. По **OK** настройки сохраняются в settings.json и обновляют
  label + CLI-строку. По **Cancel** — ничего не меняется.
3. **«Выполнить»** → если настройки неполные (не выбрано подключение/каталог),
   сначала открывается тот же диалог «Настроить…»; иначе запускается worker
   действия в `QThreadPool` (прогресс/статус — в существующие виджеты).
4. **«Копировать CLI»** → сформированная CLI-команда кладётся в clipboard
   (`QGuiApplication.clipboard()`), в status log пишется подтверждение.

---

## 4. Реестр действий (расширяемость)

Ключевое требование: «со временем действий будет больше». Поэтому действия не
хардкодятся в `MainWindow`, а описываются декларативно:

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

### 6.1. Действие 1 — «Создать проект базы по подключению PG» (`reverse_engineer`)

Диалог «Настроить…»:

| Поле | Контрол | Дефолт/prefill |
|------|---------|----------------|
| Подключение | `QComboBox` из `ConnectionStore.list_names()` | последнее сохранённое; иначе выбранное в списке |
| Каталог вывода | `QLineEdit` + «Выбрать…» (`QFileDialog.getExistingDirectory`) | последнее сохранённое; иначе `cfg.paths.default_output_dir` |

Worker: существующий `ReverseEngineerWorker`.

### 6.2. Действие 2 — «Выполнить тестовый деплой из проекта базы» (`deploy_validate`)

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

### 6.3. Действие 3 — «Подготовить граф для просмотра в приложении» (`graph_prepare`)

| Поле | Контрол | Дефолт/prefill |
|------|---------|----------------|
| Каталог кодовой базы | `QLineEdit` + «Выбрать…» | последнее; иначе `default_output_dir` |
| Формат экспорта | `QComboBox`: `json` / `graphml` / `dot` / «только build» | последнее; дефолт `json` |
| Проверить граф (циклы, висячие ссылки) | `QCheckBox` | дефолт on |

Worker: новый `GraphBuildWorker` — `BuildGraphService.build_and_store(dir)`,
опционально `export_graph(...)` и toposort-валидация (см. CLI `graph validate`).
См. USER_INPUT U5 про «просмотр в приложении».

---

## 7. Персистентность настроек (`gui_settings.json`)

Новый `GuiSettingsStore` (`infrastructure/config/gui_settings.py`).

- **Формат** — JSON (не YAML: настройки UI пишет только приложение, human-editing
  не требуется; пользователь явно назвал settings.json).
- **Расположение** — `./gui_settings.json` рядом с `config.yaml` (см. USER_INPUT U3).
- **Запись** — по OK диалога «Настроить…» и после успешного «Выполнить»
  (чтобы успешный запуск фиксировал настройки даже без явного сохранения).
- **Устойчивость** — битый/отсутствующий файл → дефолты + warning в лог,
  приложение не падает.

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
      "format": "json",
      "validate": true
    }
  }
}
```

Секретов в файле нет (только имена подключений и пути) — шифрование не нужно.

---

## 8. Копирование CLI-команды

`build_cli(settings, store)` — чистая функция на действие; результат показывается
в read-only строке контейнера и копируется кнопкой.

| Действие | Пример CLI-строки |
|----------|-------------------|
| reverse_engineer | `db-pm reverse-engineer --connection-file connections\qr_pamyat.yaml --output C:\work\qr_codebase` |
| deploy_validate | `db-pm deploy validate --dir C:\work\qr_codebase --connection-file connections\test_server.yaml --prefix qr --keep-db --continue-on-error` |
| graph_prepare | `db-pm graph build --dir C:\work\qr_codebase && db-pm graph export --dir C:\work\qr_codebase --format json` |

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
- **Per-row dropdown.** Если потребуется dropdown в каждой строке списка
  (`QListView.setIndexWidget`), это усложнит `ConnectionListWidget` — в MVP
  не делаем (U2).
- **Смена выбранного подключения в списке** не переписывает сохранённые
  настройки действия: сохранённое значение приоритетнее; выбранное в списке —
  только дефолт при пустых настройках.

---

## 13. USER_INPUT — открытые вопросы

### U1. Номер фазы
Checkpoint 20260720_004 резервирует «Phase 7» за overload resolution
(BACKLOG P1). Эта GUI-работа пришла раньше.
**Рекомендация ИИ:** назначить GUI-панель **Phase 7** (выполняется сейчас), а
overload resolution сдвинуть в Phase 8; при закрытии фазы обновить checkpoint.
Альтернатива: оформить GUI как Phase 8, оставив 7 зарезервированной.

### U2. Расположение dropdown
ТЗ: «справа от элемента списка с подключениями».
**Рекомендация ИИ:** dropdown на уровне панели **справа от всего списка**
(вариант из §3.1), а не per-row в каждой строке: действия контекстно связаны с
подключением, но диалоги всё равно содержат свой выбор подключения; per-row
вариант требует `setIndexWidget` и усложняет модель списка без выигрыша в UX.
Альтернатива: per-row dropdown в строке выбранного элемента — отложить в BACKLOG.

### U3. Расположение и имя файла настроек
**Рекомендация ИИ:** `./gui_settings.json` в корне проекта (рядом с
`config.yaml`), добавить в `.gitignore`. Альтернативы: `%APPDATA%/db-pm/`
(теряется переносимость вместе с проектом), `connections/gui_settings.json`
(смешение зон ответственности).

### U4. «Подготовить граф для просмотра в приложении» — что такое просмотр
Сейчас в приложении нет graph viewer'а (есть только `ProjectViewer` для файлов).
**Рекомендация ИИ (MVP):** действие = `graph build` + `export` в выбранный
формат + опциональная валидация; результат — путь к `.dbm_graph/graph.<fmt>` в
status log. Встроенный просмотрщик графа в GUI — отдельная фаза/BACKLOG.
Альтернатива: уже в этой фазе добавить простой viewer (например, дерево из
`graph show` в QTextEdit) — рост объёма фазы.

### U5. Дефолт чекбокса «Проверить граф» в graph_prepare
**Рекомендация ИИ:** on по умолчанию (валидация дешёвая, ловит циклы до deploy).

### U6. Формат label настроек в контейнере
**Рекомендация ИИ:** многострочный read-only `QPlainTextEdit` (3–4 строки,
ключ: значение) + отдельная однострочная `QLineEdit` для CLI — CLI может быть
длинной и должна выделяться/скроллиться независимо. Альтернатива: одна
сводная строка — плохо читается при длинных путях.

### U7. «Выполнить» без предварительной настройки
**Рекомендация ИИ:** разрешить — выполняет с текущими (последними/дефолтными)
настройками; если обязательные поля пусты (`required_fields`) — сначала
автоматически открывается диалог «Настроить…». Альтернатива: требовать явного
«Настроить…» перед первым запуском — лишний клик при валидных дефолтах.

---

## 14. Критерии готовности (для будущего _final)

1. Dropdown действий показывает 3 действия из реестра; реестр расширяем
   добавлением одного `ActionSpec`.
2. «Настроить…» → диалог с prefill из settings.json; OK сохраняет настройки.
3. «Выполнить» запускает соответствующий worker в фоне; UI не блокируется.
4. «Копировать CLI» кладёт в clipboard команду, идентичную ручному вызову CLI
   (проверено тестом через CliRunner).
5. Старые кнопки run/deploy и глобальная «Папка вывода» удалены; add/edit/delete
   подключений работают как раньше.
6. `uv run pytest tests/unit/ -q` — зелёный (baseline 278 + новые);
   `ruff check` — чисто.
