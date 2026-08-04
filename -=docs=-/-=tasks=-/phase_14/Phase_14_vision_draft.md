# Phase 14: Delta Viewer — драфт (vision draft)

> **Дата:** 2026-08-04
> **Ветка:** dev
> **Статус:** draft (после закрытия USER_INPUT → `_final`, затем `_plan` → реализация)
>
> Контекст:
> - `-=CHECKPOINTS=-/20260731_001_checkpoint.md` — текущее состояние (Phase 8 done)
> - `-=tasks=-/ROADMAP.md` §2, шаг 6 (Phase 14) и §5 (направление A — Delta Viewer)
> - `-=tasks=-/ROADMAP.md` §9 — USER_INPUT Q1 (edge diff), Q2 (CLI vs GUI)
> - `-=PHASES=-/Phase_09.md` — compare (источник данных DV)
> - `-=tasks=-/phase_09/001_plan_phase_09.md`, `002_result_phase_09.md` — детали Phase 9
> - `src/db_project_manager/domain/diff.py` — `DiffReport`, `DiffEntry`, `ObjectSnapshot`
> - `src/db_project_manager/presentation/gui/` — `main_window.py`, `actions/`,
>   `widgets/project_viewer.py`, `widgets/workers.py`
> - `LESSONS_LEARNED.md` §35 (fully-qualified DDL), §36 (qualify-refs), §41 (offscreen
>   smoke), §42 (signal-to-lambda = мёртвый слот), §43 (кнопки диалога последними)

---

## 1. Постановка проблемы

Phase 9 (compare) научила инструмент сравнивать состояние БД с файловой системой и
писать отчёт — но только в **machine-readable JSON** (`source.json`, `target.json`,
`diff_report.json`). CLI печатает путь к каталогу отчёта, содержимое отдаёт внешнему
инструменту (`Phase_09.md` §5: «Просмотр содержимого отдан внешнему инструменту — что и
мотивирует Delta Viewer поверх `diff_report.json`»). Для review-сценария (DBA/разработчик
смотрит дельту перед деплоем, выбирает объекты) JSON неудобен: нет дерева, цветовой
индикации статусов, построчного diff DDL, фильтров.

`diff_report.json` при этом **самодостаточен**: содержит обе стороны целиком
(`report.source`, `report.target`) + `entries` (с `status` из 4 значений) + посчитанный
`summary` (исследование: `compare_service._write_report`, `domain/diff.py:109-118`).
Отдельные `source.json`/`target.json` — избыточные дубликаты. Значит, DV читает **один
файл** и может работать офлайн (без подключения, без повторного compare).

ROADMAP §5 ставит DV как шаг 6, опирающийся только на готовую Phase 9 (✓). Может идти
параллельно CD-ядру (Phase 10–13): CD работает через CLI, связи с DV не требует
(ROADMAP §9 Q6 — рекомендация: связь опциональна).

**Доп. препятствие (подтверждено исследованием Phase 7):** GUI сейчас — одна
`QMainWindow` с центральным `QWidget` + вложенные `QGroupBox` (`main_window.py:40,63-107`),
dock-панелей (`QDockWidget`) нет. Action-паттерн Phase 7 (Settings+Dialog+Worker+build_cli)
заточен под «выполнить действие с настройками», а не под постоянно открытое окно просмотра.
`ProjectViewer` (`widgets/project_viewer.py:30`) — готовый референс «splitter дерево+детали
+ SqlHighlighter», но модель там `QFileSystemModel` по файлам; для дерева объектов дельты
нужна своя модель (`QTreeWidget` или `QAbstractItemModel`).

## 2. Цель фазы

1. **Просмотр `diff_report.json`** как дерева объектов с группировкой по типу/схеме и
   цветовой индикацией статуса (`added`/`removed`/`changed`/`unchanged`).
2. **Детальный diff объекта** для `changed`: построчное сравнение
   `source_snapshot.sql_normalized` vs `target_snapshot.sql_normalized` (т.к. Phase 9
   отдельных diff-блоков не хранит — см. §4.3).
3. **Фильтры и поиск**: по статусу, по типу объекта, по имени; summary наверху.
4. **(опц.) Выбор объектов/типов** чекбоксами — MVP без жёсткой связи с CD (CD не готов),
   сохранение выбора в отдельный артефакт (JSON), не в «конфигурацию деплоя» (см. USER_INPUT
   DV-3).
5. Smoke-проверка GUI через `QT_QPA_PLATFORM=offscreen` (LESSONS §41).

## 3. Принятые решения (закрыты через Q&A с пользователем — см. §8)

| Развилка | Решение | Обоснование |
|---|---|---|
| CLI/markdown сначала или сразу GUI (ROADMAP Q2) | **см. USER_INPUT DV-1** | Рекомендация: **сначала markdown-отчёт** (закрывает BACKLOG P3, дёшево, и для CI), затем GUI. GUI — основная ценность DV, но markdown самостоятельный артефакт review. |
| Edge diff — вкладка DV или отдельная фаза (ROADMAP Q1) | **см. USER_INPUT DV-2** | Рекомендация: **вкладка «Рёбра» в DV** (после MVP объектного дерева). Не отдельная фаза. |
| Размещение в GUI | **Отдельное окно** (не dock, не group box в главном) | Dock'ов нет (рефактор MainWindow выходит за scope); постоянно открытое окно просмотра не вписывается в action-паттерн (диалоги закрываются по OK). Открывается из меню/кнопки MainWindow, читает выбранный `diff_report.json`. См. USER_INPUT DV-4. |
| Модель дерева дельты | **`QTreeWidget`** | Объектов обычно сотни (не десятки тысяч); `QTreeWidget` проще `QAbstractItemModel`, хватает для review. Если тормозит на больших схемах — замена на модель в follow-up. |
| Diff DDL (changed) | **`difflib.unified_diff`** в read-only `QPlainTextEdit` + SqlHighlighter | Стандартная библиотека, без новой зависимости. Side-by-side (QTableWidget 2 колонки) — follow-up, если unified_diff неудобочитаем (см. USER_INPUT DV-5). |
| Связь с CD (ROADMAP Q6) | **Нет связи в MVP** | CD не готов; «выбор для деплоя» сохраняется в локальный артефакт DV, не в pipeline CD. Связь — когда Phase 12 (ALTER+Delta) даст контракт selection. |

## 4. Архитектура (для последующего `_plan`)

### 4.1. Источник данных — `diff_report.json`

DV читает **только** `diff_report.json` (один файл). Модель Phase 9 уже несёт всё нужное:

| Поле (`domain/diff.py`) | Использование в DV |
|---|---|
| `report.summary: dict[str,int]` | Шапка: «added: N · removed: N · changed: N · unchanged: N» |
| `report.source`/`report.target` (`StateSnapshot`) | Шапка: `source_ref`, `target_ref`, `source_kind` (db/dir), `generated_at` |
| `report.entries: list[DiffEntry]` | Дерево объектов |
| `entry.object_key` | Стабильный id (содержит `/signature/<hash>` для перегрузок) |
| `entry.status` (`DiffStatus`) | Цвет/иконка узла |
| `entry.source_snapshot`/`target_snapshot` (`ObjectSnapshot`) | Детали: `object_schema`, `object_name`, `object_type`, `object_signature`, `sql_normalized`, `estimated_rows` (только таблицы) |

> `ObjectSnapshot` для `added` — только source; для `removed` — только target; для
> `changed`/`unchanged` — оба. DV должен корректно брать «имеющуюся» сторону для показа
> имени/типа (они совпадают у source/target для common-объектов).

### 4.2. Компоновка GUI (по образцу `ProjectViewer`)

```
DeltaViewerWindow(QMainWindow)  # отдельное окно, открывается из MainWindow
├── toolbar/menu: «Открыть JSON», «Экспорт markdown», фильтры
├── summaryBar (QLabel): source_ref → target_ref · added/removed/changed/unchanged
└── central QSplitter(Horizontal)
    ├── left: QTreeWidget  # дерево: тип → [схема →] объект (статус-иконка/цвет)
    └── right: QTabWidget
        ├── «Детали»  # ObjectSnapshot поля + estimated_rows (для таблиц)
        ├── «Diff»    # unified_diff(sql_normalized) в QPlainTextEdit + SqlHighlighter
        └── (future) «Рёбра»  # DV-2: edge diff (BACKLOG P2)
```

- Группировка дерева: верхний уровень — `object_type` (`table`, `view`, ...), внутри — по
  `object_schema` (если не None), внутри — объекты (`object_name` + signature, если
  перегрузка). Порядок типов — `DIFFED_TYPES` (фиксирован, Phase 9).
- Цветовая индикация: `added`=зелёный, `removed`=красный, `changed`=жёлтый,
  `unchanged`=серый (по образцу типичной diff-цветовой схемы; точные цвета — в `_plan`).
- Фильтры: чекбоксы по 4 статусам + по типам; строка поиска по `object_name` (case-insensitive
  substring). Фильтр = скрытие узлов дерева (`setHidden`), не перестроение модели.

### 4.3. Diff DDL для `changed`

Phase 9 хранит `sql_normalized` обеих сторон, но **не diff-блок** (исследование
`comparator.py:65-80`: сравнение по `sql_hash`, «что изменилось» лежит в двух полных
снапшотах). DV считает diff на лету:

```python
import difflib
diff = difflib.unified_diff(
    source_snap.sql_normalized.splitlines(keepends=True),
    target_snap.sql_normalized.splitlines(keepends=True),
    fromfile=f"source: {source_snap.object_name}",
    tofile=f"target: {target_snap.object_name}",
)
```

Результат — в read-only `QPlainTextEdit` с `SqlHighlighter` (переиспользуем
`widgets/project_viewer.py` highlighter; unified-diff-маркеры `+ - @@` подсветить
дополнительно простым regex-хайлайтером поверх SQL).

### 4.4. Markdown-отчёт (DV-1, если принимаем «markdown сначала»)

Закрывает BACKLOG P3 «Markdown-отчёт сравнения». Генерация из `DiffReport` (чистая функция,
без GUI), пишется рядом с `diff_report.json` как `diff_report.md`:

- Summary наверху: `added/removed/changed/unchanged`, source/target ref.
- Секции **Added** / **Removed** / **Changed**, внутри — группировка по `object_type` →
  `object_schema`, таблицы `| object | signature | rows |`.
- Для `changed` — опционально unified-diff-блок в `<details>` (collapsible), чтобы отчёт
  оставался читаемым при больших схемах.

Точка интеграции: опция `--markdown` в `db-pm compare run` (рядом с существующим JSON-
выводом), либо отдельная команда `db-pm compare report --from <diff_report.json>`. См.
USER_INPUT DV-6.

### 4.5. Worker для открытия большого JSON

Открытие `diff_report.json` на крупной схеме (тысячи объектов) + построчный diff — не
мгновенно. По образцу `widgets/workers.py` (LESSONS §42: QRunnable + сильная ссылка в
`_active_workers`): воркер читает JSON, парсит `DiffReport` (pydantic `model_validate_json`),
считает diff'ы для changed-объектов лениво (при раскрытии узла, не все сразу). Прогресс —
в statusbar окна. Сигналы — только к bound-методам (не лямбды, §42).

## 5. Тесты (для `_plan`)

- **Unit на markdown-генератор** (если DV-1 принят): синтетический `DiffReport` с одним
  объектом каждого статуса → `diff_report.md` содержит ожидаемые секции/строки; roundtrip
  через строковые assert'ы (не YAML — обычный markdown, §28 про YAML-kavычки неприменим).
- **Unit на построение дерева**: фикстура-`DiffReport` → древовидная структура
  (тип → схема → объект) корректна, перегрузки (`/signature/<hash>`) не склеиваются.
- **Unit на unified-diff**: changed-объект → ожидаемые `+`/`-`/`@@`-маркеры; unchanged-объект
  → пустой diff.
- **Offscreen smoke** (LESSONS §41): `QT_QPA_PLATFORM=offscreen` — создать
  `DeltaViewerWindow`, загрузить тестовый `diff_report.json`, выбрать узел, прочитать текст
  detail/diff-вкладок. Регрессия «сигнал к лямбде» (§42) — bound-method only.
- **(опц.) Контракт-тест CliRunner** если добавляем `--markdown` к `compare run`: флаг
  принимается, файл создаётся.
- Существующие тесты Phase 9 не ломаются (compare-логика не меняется — DV только читает
  артефакт).

## 6. NOT done / отложено (явный список в `_plan`)

- **Edge diff (BACKLOG P2)** — если DV-2 принят как вкладка «Рёбра», то это отдельный
  шаг внутри Phase 14 (после MVP), требует расширения `DiffReport` секцией `edge_entries`
  и обновления `comparator.py`/`snapshot.py` (снимать рёбра). Если нет — остаётся отдельным
  BACKLOG P2.
- **Side-by-side diff** (QTableWidget 2 колонки) — follow-up, если unified_diff
  неудобочитаем; DV-5.
- **Выбор объектов → pipeline CD** — отложено до Phase 12 (контракт selection); в MVP
  выбор сохраняется только как артефакт DV (см. USER_INPUT DV-3).
- **AST-точный diff DDL** — unified_diff текстовый; структурный column-diff (CD-ALT-1,
  Phase 12 prerequisite) даст richer diff, но это работа Phase 12, не DV.
- **`QAbstractItemModel` вместо `QTreeWidget`** — если производительность на больших схемах
  окажется недостаточной; MVP на `QTreeWidget`.
- **Редактирование/переименование в дереве** — вне scope (DV = viewer, read-only).

## 7. Чеклист по урокам

- [ ] §35: показываемый DDL — `sql_normalized`, который уже прошел sqlglot-нормализацию;
  идентификаторы в нём могут быть bare (ограничение Phase 9 §5) — не «лечим» в DV, только
  показываем. Коррекция bare refs — qualify-refs на стороне compare, не в viewer.
- [ ] §36: regex-vs-AST здесь не стоит (нет модификации SQL) — только отображение
  готового `sql_normalized`.
- [ ] §41: offscreen-smoke для `DeltaViewerWindow` обязателен (как для MainWindow в Phase 7).
- [ ] §42: сигнал → только bound-method; воркер загрузки JSON — сильная ссылка в
  `_active_workers` до `finished`; лямбды в `connect` — никогда.
- [ ] §43: если в окне/диалоге есть кнопки (экспорт/закрыть) — добавлять после построения
  основного UI (по образцу `_add_buttons`).
- [ ] §18: контракт `DiffReport` (domain) **не расширяется** в MVP → никакие fakes/тесты
  Phase 9 не ломаются. (Расширение под edge diff — только если DV-2 принят, и тогда это
  отдельный шаг с обновлением fakes.)
- [ ] §12: `git add -- "-=docs=-/-=tasks=-/phase_14/..."` (дефис в имени каталога).
- [ ] Markdown-отчёт — отдельный коммит от GUI (TASK_CONVENTIONS §6: код и документы не
  смешивать; здесь — markdown-генератор = код, GUI = код, но разные логические единицы).

## 8. USER_INPUT (рекомендации; закрытие → перенос в `_final`)

> Стиль ROADMAP §9 — рекомендации; если пользователь согласен, переношу в `_final`.

- **DV-1** Markdown-отчёт сначала (закрывает BACKLOG P3, для CI), затем GUI?
  *Рекомендация:* **да** — markdown самостоятельный артефакт review, дёшев; GUI — основная
  ценность, но второго шага. Альтернатива: только GUI (markdown пропустить).
- **DV-2** Edge diff (BACKLOG P2) — вкладка «Рёбра» в DV, или отдельная фаза?
  *Рекомендация:* **вкладка в DV**, после MVP объектного дерева. Переиспользуется UI DV,
  единое окно review. (Перенос BACKLOG P2 внутрь Phase 14.)
- **DV-3** «Выбор объектов для деплоя» (ROADMAP DV-2) — что делать в MVP, раз CD не готов?
  *Рекомендация:* чекбоксы **есть в UI**, но сохранение — в локальный артефакт
  `selection.json` рядом с `diff_report.json` (формат: список `object_key`). Связь с CD —
  когда Phase 12 даст контракт. Альтернатива: убрать чекбоксы из MVP (только просмотр).
- **DV-4** Размещение в GUI — отдельное окно (`DeltaViewerWindow`)?
  *Рекомендация:* **да**. Dock'ов нет (рефактор MainWindow вне scope); action-паттерн Phase 7
  не подходит (диалог закрывается). Отдельное окно открывается из меню MainWindow, живёт
  пока пользователь его не закроет. Альтернатива: group box в центральном виджете
  (перегрузит MainWindow).
- **DV-5** Diff DDL — `unified_diff` (одна колонка) или side-by-side (две колонки)?
  *Рекомендация:* **unified_diff в MVP** (стандартная `difflib`, без сложности двух
  синхронизированных скролл-ареа); side-by-side — follow-up по запросу.
- **DV-6** Markdown-отчёт (DV-1) — опция `--markdown` в `db-pm compare run`, или отдельная
  команда `db-pm compare report --from <json>`?
  *Рекомендация:* **отдельная команда `compare report`** — читает готовый `diff_report.json`,
  не требует повторного compare (быстрее, работает офлайн). `--markdown` в `compare run` —
  опц. сахар поверх (если пользователь хочет всё за один прогон).
- **DV-7** Цветовая схема статусов — стандартная (added=зелёный/removed=красный/
  changed=жёлтый/unchanged=серый) или因地制宜 под dark/light тему?
  *Рекомендация:* стандартная + проверка в обеих темах (проект использует `darkdetect`,
  `main.py:14`). Точные оттенки — в `_plan`.

## 9. Где читать дальше

- `-=CHECKPOINTS=-/20260731_001_checkpoint.md` — текущее состояние проекта (Phase 8 done)
- `-=PHASES=-/Phase_09.md` — compare, источник данных DV
- `-=tasks=-/phase_09/001_plan_phase_09.md`, `002_result_phase_09.md` — детали Phase 9
- `-=tasks=-/ROADMAP.md` §5 (Delta Viewer), §9 (Q1, Q2)
- `-=tasks=-/BACKLOG.md` — «Edge diff» (P2), «Markdown-отчёт сравнения» (P3)
- `LESSONS_LEARNED.md` §35, §41, §42, §43
