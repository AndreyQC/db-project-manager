# Phase 14: Delta Viewer — финальный дизайн (vision final)

> **Дата:** 2026-08-04
> **Ветка:** dev
> **Статус:** final (все USER_INPUT закрыты; нормативный документ для `_plan` и реализации)
>
> Предыдущий артефакт: `_tasks_/phase_14/Phase_14_vision_draft.md` (не удаляется —
> остаётся для истории обсуждения, по TASK_CONVENTIONS §2.2).
>
> Контекст:
> - `_checkpoints_/20260731_001_checkpoint.md` — текущее состояние (Phase 8 done)
> - `_tasks_/ROADMAP.md` §2, шаг 6 (Phase 14) и §5 (направление A — Delta Viewer)
> - `_phases_/Phase_09.md` — compare (источник данных DV)
> - `src/db_project_manager/domain/diff.py` — `DiffReport`, `DiffEntry`, `ObjectSnapshot`
> - `src/db_project_manager/presentation/gui/` — `main_window.py`, `actions/`,
>   `widgets/project_viewer.py`, `widgets/workers.py`
> - `LESSONS_LEARNED.md` §35, §36, §41, §42, §43

---

## 1. Постановка проблемы

Phase 9 (compare) пишет отчёт только в **machine-readable JSON** (`diff_report.json` +
дублирующие `source.json`/`target.json`). CLI печатает путь к каталогу, содержимое отдаёт
внешнему инструменту (`Phase_09.md` §5). Для review-сценария (DBA смотрит дельту перед
деплоем, выбирает объекты) JSON неудобен: нет дерева, цветовой индикации статусов,
построчного diff DDL, фильтров.

`diff_report.json` **самодостаточен**: содержит обе стороны целиком (`report.source`,
`report.target`) + `entries` (со `status` из 4 значений) + посчитанный `summary`
(`compare_service._write_report`, `domain/diff.py:109-118`). Отдельные `source.json`/
`target.json` — избыточные дубликаты. Значит, DV читает **один файл** и работает офлайн
(без подключения, без повторного compare).

ROADMAP §5 ставит DV как шаг 6, опирающийся только на готовую Phase 9 (✓). Может идти
параллельно CD-ядру (Phase 10–13): CD работает через CLI, связи с DV не требует
(ROADMAP §9 Q6 — связь опциональна).

**Доп. препятствие (подтверждено исследованием Phase 7):** GUI сейчас — одна `QMainWindow`
с центральным `QWidget` + вложенные `QGroupBox` (`main_window.py:40,63-107`), dock-панелей
(`QDockWidget`) нет. Action-паттерн Phase 7 (Settings+Dialog+Worker+build_cli) заточен под
«выполнить действие с настройками», а не под постоянно открытое окно просмотра.
`ProjectViewer` (`widgets/project_viewer.py:30`) — готовый референс «splitter дерево+детали
+ SqlHighlighter», но модель там `QFileSystemModel` по файлам; для дерева объектов дельты
нужна своя модель (`QTreeWidget`).

## 2. Цель фазы

1. **Markdown-отчёт** из `DiffReport` (закрывает BACKLOG P3 «Markdown-отчёт сравнения»):
   читает `diff_report.json`, пишет `diff_report.md` рядом; команда `db-pm compare report`.
2. **GUI-просмотрщик** `diff_report.json`: дерево объектов с группировкой по типу/схеме и
   цветовой индикацией статуса (`added`/`removed`/`changed`/`unchanged`).
3. **Детальный diff объекта** для `changed`: построчный `difflib.unified_diff` на
   `sql_normalized` обеих сторон (Phase 9 отдельных diff-блоков не хранит — _draft §4.3).
4. **Фильтры и поиск**: по статусу, по типу объекта, по имени; summary наверху.
5. **Выбор объектов/типов** чекбоксами → сохранение в локальный артефакт `selection.json`
   (связь с CD отложена до Phase 12).
6. Smoke-проверка GUI через `QT_QPA_PLATFORM=offscreen` (LESSONS §41).

## 3. Принятые решения (все закрыты через Q&A с пользователем)

| Развилка | Решение | Обоснование |
|---|---|---|
| **DV-1** Порядок: markdown сначала или сразу GUI | **Сначала markdown-отчёт, затем GUI** | Markdown закрывает BACKLOG P3, дёшев, самостоятельный артефакт review (в т.ч. для CI). GUI — основная ценность DV, но второго шага. |
| **DV-2** Edge diff (BACKLOG P2) | **Вкладка «Рёбра» в DV**, после MVP объектного дерева | Переиспользуется UI DV, единое окно review. BACKLOG P2 «Edge diff» переносится внутрь Phase 14. |
| **DV-3** «Выбор объектов для деплоя» при неготовом CD | **Чекбоксы есть в UI, сохранение в локальный `selection.json`** | Связь с CD — когда Phase 12 даст контракт selection. В MVP выбор — артефакт DV, не pipeline CD. |
| **DV-4** Размещение в GUI | **Отдельное окно `DeltaViewerWindow`** | Dock'ов нет (рефактор MainWindow вне scope); action-паттерн Phase 7 не подходит (диалог закрывается по OK). Открывается из меню/кнопки MainWindow, живёт пока пользователь не закроет. |
| **DV-5** Diff DDL | **`difflib.unified_diff` (одна колонка)** в MVP | Стандартная библиотека, без сложности двух синхронизированных скролл-ареа. Side-by-side — follow-up по запросу. |
| **DV-6** Markdown: опция или команда | **Отдельная команда `db-pm compare report --from <json>`** | Читает готовый `diff_report.json`, не требует повторного compare (быстрее, офлайн). Опция `--markdown` в `compare run` — follow-up (сахар поверх). |
| **DV-7** Цветовая схема | **Стандартная**: added=зелёный, removed=красный, changed=жёлтый, unchanged=серый | Проверка в обеих темах (проект использует `darkdetect`, `main.py:14`). Оттенки — в `_plan`. |

## 4. Финальная архитектура

### 4.1. Источник данных — `diff_report.json`

DV/markdown-генератор читают **только** `diff_report.json` (один файл). Модель Phase 9:

| Поле (`domain/diff.py`) | Использование |
|---|---|
| `report.summary: dict[str,int]` | Шапка: «added: N · removed: N · changed: N · unchanged: N» |
| `report.source`/`report.target` (`StateSnapshot`) | Шапка: `source_ref`, `target_ref`, `source_kind` (db/dir), `generated_at` |
| `report.entries: list[DiffEntry]` | Дерево объектов / секции markdown |
| `entry.object_key` | Стабильный id (содержит `/signature/<hash>` для перегрузок) |
| `entry.status` (`DiffStatus`) | Цвет/иконка узла |
| `entry.source_snapshot`/`target_snapshot` (`ObjectSnapshot`) | Детали: `object_schema`, `object_name`, `object_type`, `object_signature`, `sql_normalized`, `estimated_rows` (только таблицы) |

> `ObjectSnapshot` для `added` — только source; `removed` — только target; `changed`/
> `unchanged` — оба. Для показа имени/типа берётся «имеющаяся» сторона.

### 4.2. Markdown-отчёт (DV-1, шаг 1)

Чистая функция `render_diff_markdown(report: DiffReport) -> str` (без I/O, тестируется
изолированно). Команда `db-pm compare report --from <diff_report.json> [--output <path>]`:
читает JSON → `DiffReport.model_validate_json` → рендерит → пишет `diff_report.md` рядом
(или по `--output`).

Структура отчёта:
- Заголовок + `generated_at`; строка `source_ref → target_ref` с `source_kind`.
- **Summary**: `| status | count |` таблица из `report.summary`.
- Секции **Added** / **Removed** / **Changed** (пустые секции опускаются), внутри —
  группировка по `object_type` → `object_schema`, таблицы `| object | signature | rows |`
  (`rows` — только таблицы, из `estimated_rows`; `signature` — для перегрузок).
- Для `changed` — unified-diff-блок в `<details><summary>diff</summary>...` (collapsible),
  чтобы отчёт оставался читаемым при больших схемах. Diff считается через
  `difflib.unified_diff` на `sql_normalized`.
- `unchanged` — только счётчик в summary (секция не выводится — шум).

### 4.3. Компоновка GUI (по образцу `ProjectViewer`)

```
DeltaViewerWindow(QMainWindow)  # отдельное окно
├── toolbar: «Открыть JSON», «Экспорт markdown», фильтры статусов, поиск
├── summaryBar (QLabel): source_ref → target_ref · added/removed/changed/unchanged
└── central QSplitter(Horizontal)
    ├── left: QTreeWidget  # тип → [схема →] объект (иконка/цвет статуса)
    └── right: QTabWidget
        ├── «Детали»  # ObjectSnapshot поля + estimated_rows (для таблиц)
        ├── «Diff»    # unified_diff(sql_normalized) в QPlainTextEdit + SqlHighlighter
        └── «Рёбра»   # DV-2: edge diff (BACKLOG P2, второй шаг)
```

- Группировка дерева: верхний уровень — `object_type` (`table`, `view`, ...), внутри — по
  `object_schema` (если не None), внутри — объекты. Порядок типов — `DIFFED_TYPES`.
- Цвета (DV-7): `added`=#080 / зелёный, `removed`=#800 / красный, `changed`=#880 / жёлтый,
  `unchanged`=серый; проверка в dark/light (уточнение оттенков в `_plan`).
- Фильтры: чекбоксы по 4 статусам + по типам; строка поиска по `object_name`
  (case-insensitive substring). Фильтр = скрытие узлов дерева (`setHidden`), не перестроение.
- Чекбоксы выбора (DV-3): на узлах-объектах; «Выбрать всё / Снять всё» по типу; кнопка
  «Сохранить выбор» → `selection.json` (список `object_key`) рядом с открытым JSON.

### 4.4. Diff DDL для `changed` (DV-5)

Phase 9 хранит `sql_normalized` обеих сторон, но **не diff-блок** (`comparator.py:65-80`:
сравнение по `sql_hash`). DV/markdown считают diff на лету через `difflib.unified_diff`:

```python
import difflib
diff = difflib.unified_diff(
    source_snap.sql_normalized.splitlines(keepends=True),
    target_snap.sql_normalized.splitlines(keepends=True),
    fromfile=f"source/{name}", tofile=f"target/{name}", lineterm="",
)
```

Результат — в read-only `QPlainTextEdit` с `SqlHighlighter` (переиспользуем
`widgets/project_viewer.py`) + простой regex-хайлайтер unified-маркеров (`+ - @@`) поверх.

### 4.5. Worker для открытия большого JSON (DV UI)

По образцу `widgets/workers.py` (LESSONS §42: QRunnable + сильная ссылка в
`_active_workers`). Читает JSON, парсит `DiffReport` (`model_validate_json`). Diff'ы для
`changed` считаются **лениво** при выборе узла (не все сразу — крупные схемы). Прогресс — в
statusbar. Сигналы — только к bound-методам (не лямбды, §42).

### 4.6. Edge diff (DV-2, второй шаг — расширяет Phase 9 домен)

Вкладка «Рёбра» требует расширения `DiffReport` секцией `edge_entries` и обновления
`comparator.py`/`snapshot.py` (снимать рёбра через `Edge.dedup_key()`). Это меняет контракт
Phase 9 (урок §18/§45: обновить fakes в одном коммите). Выполняется **после** MVP объектного
дерева, отдельными шагами в `_plan`. Если объём окажется велик — выносится в follow-up; в
финале зафиксировано как часть Phase 14.

## 5. Проверки (для `_plan`)

- **Unit на markdown-генератор**: синтетический `DiffReport` с одним объектом каждого
  статуса → `diff_report.md` содержит ожидаемые секции/строки; пустые секции опускаются;
  changed-блок в `<details>`. Roundtrip через `render_diff_markdown(report)` → строка,
  обычные assert'ы подстрок (не YAML — §28 неприменим).
- **Unit на построение дерева**: фикстура-`DiffReport` → древовидная структура
  (тип → схема → объект) корректна, перегрузки (`/signature/<hash>`) не склеиваются.
- **Unit на unified-diff**: changed-объект → ожидаемые `+`/`-`/`@@`-маркеры; unchanged →
  пустой diff.
- **Offscreen smoke** (LESSONS §41): `QT_QPA_PLATFORM=offscreen` — создать
  `DeltaViewerWindow`, загрузить тестовый `diff_report.json`, выбрать узел, прочитать текст
  detail/diff-вкладок. Регрессия «сигнал к лямбде» (§42) — bound-method only.
- **Контракт-тест CliRunner** для `compare report --from`: флаги принимаются, файл создаётся.
- Существующие тесты Phase 9 не ломаются (compare-логика не меняется в шаге 1; шаг 2 edge
  diff расширяет домен — fakes обновляются в одном коммите, §45).

## 6. NOT done / отложено

- **Side-by-side diff** (QTableWidget 2 колонки) — follow-up, если unified_diff
  неудобочитаем; DV-5 альтернатива.
- **Выбор объектов → pipeline CD** — отложено до Phase 12 (контракт selection); в MVP выбор
  сохраняется только как артефакт DV (`selection.json`).
- **AST-точный diff DDL** — unified_diff текстовый; структурный column-diff (CD-ALT-1,
  Phase 12) даст richer diff, но это работа Phase 12, не DV.
- **`QAbstractItemModel` вместо `QTreeWidget`** — если производительность на больших схемах
  недостаточна; MVP на `QTreeWidget`.
- **Опция `--markdown` в `compare run`** — follow-up (сахар поверх `compare report`);
  DV-6 альтернатива.
- **Редактирование/переименование в дереве** — вне scope (DV = viewer, read-only).

## 7. Чеклист по урокам

- [ ] §35: показываемый DDL — `sql_normalized` (могут быть bare refs, ограничение Phase 9);
  не «лечим» в DV, только показываем. Коррекция — qualify-refs на стороне compare.
- [ ] §36: regex-vs-AST не стоит (нет модификации SQL) — только отображение готового.
- [ ] §41: offscreen-smoke для `DeltaViewerWindow` обязателен.
- [ ] §42: сигнал → только bound-method; воркер загрузки — сильная ссылка в `_active_workers`
  до `finished`; лямбды в `connect` — никогда.
- [ ] §43: кнопки окна (экспорт/закрыть) — после построения основного UI (по образцу
  `_add_buttons`).
- [ ] §18/§45: контракт `DiffReport` **не расширяется** в шаге 1 (markdown + object tree) →
  fakes/тесты Phase 9 не ломаются. Расширение под edge diff (шаг 2) — отдельный коммит с
  обновлением всех наследников/тестов.
- [ ] §12: `git add -- "_docs_/_tasks_/phase_14/..."`.
- [ ] §1: снимать TLS-переменные перед `uv`.
- [ ] Markdown-генератор и GUI — разные логические единицы (TASK_CONVENTIONS §6: код и
  документы не смешивать; здесь код-с-кодом, но по разным коммитам на шаг).

## 8. Где читать дальше

- `_tasks_/phase_14/Phase_14_vision_draft.md` — предшествующий драфт (история обсуждения)
- `_checkpoints_/20260731_001_checkpoint.md` — текущее состояние проекта (Phase 8 done)
- `_phases_/Phase_09.md` — compare, источник данных DV
- `_tasks_/phase_09/001_plan_phase_09.md`, `002_result_phase_09.md` — детали Phase 9
- `_tasks_/ROADMAP.md` §5 (Delta Viewer), §9 (Q1, Q2)
- `_tasks_/BACKLOG.md` — «Edge diff» (P2), «Markdown-отчёт сравнения» (P3)
- `LESSONS_LEARNED.md` §35, §41, §42, §43
