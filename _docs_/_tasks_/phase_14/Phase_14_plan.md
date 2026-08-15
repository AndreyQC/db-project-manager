# Phase 14: План реализации — Delta Viewer

> **Дата:** 2026-08-04
> **Ветка:** dev
> **Статус:** plan (нормативный документ для пошаговой реализации; на основе `_final`)
>
> Норматив-дизайн: `_tasks_/phase_14/Phase_14_vision_final.md`.
> Предшественник (история обсуждения): `_tasks_/phase_14/Phase_14_vision_draft.md`.
> Контекст: чекпойнт 20260731_001; ROADMAP §5; BACKLOG P2 (edge diff) + P3 (markdown);
> Phase_09.md; LESSONS §35, §41, §42, §43, §45.

---

## Принцип разбиения коммитов

Один логический шаг — один коммит (TASK_CONVENTIONS §6). **Код и документы не смешиваются.**
Шаги идут «снизу-вверх»: сначала чистая функция markdown (без зависимостей от UI/Phase 9
контракта), затем CLI-обёртка, затем GUI-виджет, интеграция в MainWindow, и наконец edge
diff (расширяет контракт Phase 9 — самым «рискованным» шагом в конце). Тесты пишутся в том
же коммите, что и код юнит-функции.

Глобальные команды проверок (урок §1 — снять TLS-переменные перед uv):
```bash
unset SSL_CERT_FILE REQUESTS_CA_BUNDLE CURL_CA_BUNDLE && uv run pytest tests/unit/ -q
uv run ruff check src/ tests/
```

---

## Шаг P14.S1 — Markdown-генератор (чистая функция)

**Цель:** `render_diff_markdown(report: DiffReport) -> str` — изолированно тестируемая
функция без I/O. Закрывает BACKLOG P3 «Markdown-отчёт сравнения» (частично — сама функция;
запись на диск + CLI в S2).

**Новый файл:** `src/db_project_manager/infrastructure/diff/markdown_report.py`

Содержание:
- `def render_diff_markdown(report: DiffReport) -> str:` — возвращает полный markdown-текст.
  Структура по _final §4.2:
  1. Заголовок `# Diff report` + `generated_at`.
  2. Строка источника/цели: `source_ref (source_kind) → target_ref (source_kind)`,
     `db_type`.
  3. **Summary** — таблица `| status | count |` из `report.summary` (4 строки, порядок
     added/removed/changed/unchanged).
  4. Секции **Added / Removed / Changed** (пустые — опускаются). Внутри каждой: группировка
     по `object_type` (порядок `DIFFED_TYPES`), внутри — таблица
     `| schema | object | signature | rows |`.
     - `signature` — `object_signature`, если есть (для перегрузок); иначе `—`.
     - `rows` — `estimated_rows`, если `object_type == "table"` и значение не None; иначе
       `—`.
  5. Для `changed` — под каждой таблицей объекта collapsible-блок
     `<details><summary>DDL diff</summary>\n\n```diff\n ... \n```\n\n</details>` с unified
     diff из `_unified_diff(source_snap, target_snap)`.
  6. `unchanged` — только в summary, секции нет (шум).
- `def _unified_diff(src: ObjectSnapshot, tgt: ObjectSnapshot) -> str:` —
  `difflib.unified_diff` на `sql_normalized.splitlines(keepends=True)` (урок _final §4.4);
  `fromfile=f"source/{name}"`, `tofile=f"target/{name}"`, `lineterm=""`. Возвращает строку
  (пустую, если `sql_normalized` обеих сторон пуст/равен — unchanged не вызовет эту функцию).
- Группировщик-хелпер: `def _group_entries(entries, status) -> dict[str, dict[str|None, list[DiffEntry]]]:`
  возвращает `{object_type: {schema: [entries]}}` для запрошенного статуса.

**Импорты:** `difflib`, `domain.diff.{DiffReport, DiffStatus, ObjectSnapshot}`,
`infrastructure.diff.snapshot.DIFFED_TYPES` (для порядка типов).

**Тесты (этот же коммит):** `tests/unit/test_diff_markdown.py` — переиспользуем паттерн
хелперов `_obj`/`_state` из `test_comparator.py:14-36` (скопировать в новый файл или вынести
в `tests/unit/conftest.py` — _решение: оставить локально в новом файле_, чтобы не трогать
существующий conftest).
- `test_markdown_has_summary_table`: синтетический report → содержит `| status | count |` и
  все 4 статуса с ожидаемыми числами.
- `test_markdown_added_section_lists_object`: один added-объект → секция `## Added`,
  строки со schema/name.
- `test_markdown_omits_empty_sections`: report только с unchanged → нет секций
  Added/Removed/Changed (только summary).
- `test_markdown_changed_has_collapsible_diff`: changed-объект с разным `sql_normalized` →
  блок `<details>` и маркеры `+`/`-`/`@@`.
- `test_markdown_tables_show_rows_and_signature`: таблица с `object_type="table"`,
  `estimated_rows=1234`, `object_signature="abc123"` → строка содержит `1234` и `abc123`.
- `test_markdown_overloads_distinct`: две перегрузки (разные `object_key`) → обе в отчёте,
  не склеены.
- `test_markdown_source_target_header`: содержит `source_ref`, `target_ref`, оба `source_kind`.

**NOT done тут:** запись на диск и CLI — появляются в P14.S2.

---

## Шаг P14.S2 — CLI-команда `db-pm compare report`

**Цель:** пользователь генерирует `diff_report.md` из готового `diff_report.json` офлайн.

**Файлы:**
- `src/db_project_manager/infrastructure/diff/markdown_report.py` — добавить
  `def write_diff_markdown(report_or_path, output: Path | None = None) -> Path:`
  (тонкая I/O-обёртка над `render_diff_markdown`): читает JSON если передан путь, пишет
  `diff_report.md` рядом (или по `output`), возвращает путь. Использует
  `DiffReport.model_validate_json`.
- `src/db_project_manager/presentation/cli/main.py` — новая команда в подгруппе `compare`
  (рядом с `compare_run`, ~строка 320):
  ```python
  @compare_app.command("report")
  def compare_report(
      from_path: Annotated[Path, typer.Option("--from", help="Путь к diff_report.json.")],
      output: Annotated[
          Optional[Path],
          typer.Option("--output", "-o", help="Куда писать diff_report.md. По умолчанию рядом с --from."),
      ] = None,
  ) -> None:
      """Сгенерировать markdown-отчёт из готового diff_report.json (офлайн)."""
      configure_logging()
      ...
  ```
  - Проверка `from_path.is_file()` → exit code 2, если нет (по образцу `_resolve_side`).
  - Вызов `write_diff_markdown`, обработка ошибок (битый JSON → `CompareError`-стиль: красная
    строка, exit 2).
  - Успех: `✓ Markdown-отчёт: <path>` зелёным (по образцу строки 319).

**Тесты (этот же коммит):** расширить `tests/unit/test_compare_cli.py` (стиль —
`runner.invoke(cli_main.app, ["compare", "report", ...])`):
- `test_compare_report_help_lists_command`: `compare --help` содержит `report`.
- `test_compare_report_writes_markdown`: во `tmp_path` кладём синтетический `diff_report.json`
  (через `DiffReport(...).model_dump_json`) → `compare report --from <path>` → exit 0,
  файл `diff_report.md` создан, содержит `# Diff report`.
- `test_compare_report_custom_output`: `--output <path>` пишет по указанному пути.
- `test_compare_report_missing_file_exits_2`: несуществующий `--from` → exit 2.
- `test_compare_report_invalid_json_exits_2`: `--from` с мусором → exit 2.

---

## Шаг P14.S3 — GUI: `DeltaViewerWindow` (дерево + детали + diff)

**Цель:** отдельное окно просмотра `diff_report.json`.

**Новый файл:** `src/db_project_manager/presentation/gui/widgets/delta_viewer.py`

Класс `DeltaViewerWindow(QMainWindow)`. Компоновка по _final §4.3:
- toolbar: «Открыть JSON» (QFileDialog), «Экспорт markdown» (вызывает
  `write_diff_markdown`), разделитель, чекбоксы статусов (4), строка поиска
  (`QLineEdit` с `textChanged`).
- `summaryBar` (`QLabel`): `source_ref → target_ref · added/removed/changed/unchanged`.
- central `QSplitter(Horizontal)`:
  - left: `QTreeWidget` — колонки `[status_icon, object]`; верхний уровень — `object_type`,
    внутри — `object_schema` (если не None), внутри — объекты. На узлах-объектах чекбокс
    (DV-3). `itemClicked` → обновить правую панель.
  - right: `QTabWidget` — «Детали» (`QFormLayout`: schema/name/type/signature/rows + ключ),
    «Diff» (`QPlainTextEdit` read-only + `SqlHighlighter` + простой `DiffMarkerHighlighter`
    поверх для `+ - @@`).
- `_active_workers: dict` для воркера загрузки (урок §42 — сильная ссылка).

Методы:
- `load_report(self, path: str | Path) -> None:` — стартует `LoadDiffReportWorker`
  (см. S3-worker ниже); по `finished` вызывает `_populate_tree` + `_populate_summary`.
- `_populate_tree(self, report: DiffReport) -> None:` — строит дерево по `report.entries`;
  `QTreeWidgetItem` хранит `data(0, UserRole) = object_key` и статус. Группировка через тот
  же хелпер группировки, что в S1 (вынести `_group_entries` в общий модуль или продублировать
  локально — _решение: продублировать локально, GUI не должен зависеть от infrastructure-модуля
  отчёта_; см. OPEN в §«Риски»).
- `_populate_details(self, entry: DiffEntry) -> None:` — заполняет «Детали» (берёт
  «имеющуюся» сторону: для added — source, removed — target, changed/unchanged — source).
- `_populate_diff(self, entry: DiffEntry) -> None:` — если status не CHANGED →
  `(нет изменений / объект добавлен / объект удалён)`; иначе `_unified_diff` (переиспользуем
  из S1 — вынести в общий хелпер, см. ниже) в `QPlainTextEdit`.
- `_apply_filters(self) -> None:` — проходит по дереву, `setHidden(True/False)` по
  активным статусам и по строке поиска (`object_name` contains, case-insensitive).
- `_on_save_selection(self) -> None:` — собирает `object_key` всех отмеченных →
  `selection.json` рядом с открытым отчётом (список `{"selected": [key, ...]}`).

**Новый файл:** `src/db_project_manager/presentation/gui/widgets/diff_highlighter.py`
- `class DiffMarkerHighlighter(QSyntaxHighlighter):` — поверх `SqlHighlighter`-раскрашенного
  блока подсвечивает unified-строки: `^+` зелёным фоном, `^-` красным фоном, `^@@` серым.
  Простой regex по `highlightBlock`. (Альтернатива — один комбинированный highlighter; для
  MVP — отдельный класс, применяемый после SQL-хайлайтера к тем же блокам через второй pass
  не работает — QSyntaxHighlighter один на документ. _Решение: расширить SqlHighlighter
  опциональным diff-режимом_, см. §«Риски» — уточнить в реализации.)

**Новый воркер (в существующий `widgets/workers.py`):**
```python
class LoadDiffReportWorker(QRunnable):
    """Load + parse diff_report.json off the UI thread."""
    def __init__(self, path: str | Path):
        ...
        self.signals = WorkerSignals()
    def run(self):
        try:
            text = Path(self.path).read_text(encoding="utf-8")
            report = DiffReport.model_validate_json(text)
            self.signals.finished.emit(report)
        except Exception as e:
            self.signals.error.emit(f"Не удалось прочитать отчёт: {e}")
            self.signals.finished.emit(None)
```
По образцу `CompareWorker` (`workers.py:172`): `QRunnable`, `self.signals` в `__init__`,
`try/except` с `finished.emit(None)` при ошибке. Сигналы подключаются к bound-методам окна,
сильная ссылка в `_active_workers` до `finished` (урок §42 — критично).

**Общий хелпер unified-diff:** вынести `_unified_diff` из `markdown_report.py` (S1) в
`infrastructure/diff/markdown_report.py` как публичную `unified_diff_text(src, tgt) -> str`
(используется и markdown, и GUI). Оба вызывают одну функцию — нет дублирования логики diff.

**Тесты (этот же коммит):** `tests/unit/test_delta_viewer.py` — offscreen-smoke (LESSONS §41):
- `QT_QPA_PLATFORM=offscreen` (через `monkeypatch.setenv` в фикстуре или `autouse`) —
  создать `QApplication`, `DeltaViewerWindow`, загрузить синтетический `diff_report.json`
  (через `tmp_path`), выбрать узел дерева, прочитать текст detail/diff-вкладок.
- `test_window_loads_report_populates_tree`: после load — верхний уровень дерева содержит
  ожидаемые `object_type`, summary-bar содержит ожидаемый текст.
- `test_selecting_changed_object_shows_diff`: выбор changed-узла → diff-вкладка содержит
  `+`/`-`/`@@`.
- `test_selecting_unchanged_shows_no_diff_message`.
- `test_filter_hides_unchecked_statuses`: снимаем чекбокс «unchanged» → unchanged-узлы
  скрыты (`isHidden()`).
- `test_search_filters_by_name`: поиск по подстроке → неподходящие скрыты.
- `test_save_selection_writes_json`: отметить 2 узла → «Сохранить выбор» → файл
  `selection.json` создан, содержит ожидаемые `object_key`.
- `test_bound_methods_only_no_lambda`: (статический assert через inspect, или просто
  документируется в чеклисте) — регрессия урока §42.

**Риски / OPEN (решить в реализации):**
1. **Двойной highlighter (SQL + diff-маркеры):** `QSyntaxHighlighter` один на документ.
   Варианты: (a) расширить `SqlHighlighter` опциональным diff-режимом (флаг в конструкторе,
   добавляет правила для `^+`/`^-`/`^@@`); (b) отдельный `DiffHighlighter` без SQL. Для
   diff-вкладки SQL-синтаксис полезен (показываем DDL) → вариант (a). _Принято: расширить
   `SqlHighlighter`_ (параметр `diff_mode: bool = False`).
2. **Хелпер группировки `_group_entries`** нужен и в markdown (S1), и в GUI (S3). Чтобы GUI
   не зависел от `infrastructure.diff.markdown_report` — вынести в
   `infrastructure/diff/grouping.py` (чистая функция, без I/O). Используется обоими.

---

## Шаг P14.S4 — Интеграция в `MainWindow`

**Цель:** пользователь открывает Delta Viewer из главного окна.

**Файл:** `src/db_project_manager/presentation/gui/main_window.py`
- В `_init_ui` (после `viewer_group`, ~строка 107) добавить toolbar или кнопку «Delta
  Viewer…» в `action_panel`/отдельной группой. Минимально: пункт меню «Вид → Delta Viewer»
  (`QMenuBar`) + кнопка в toolbar главного окна.
- Обработчик `_on_open_delta_viewer(self):` — `QFileDialog.getOpenFileName` с фильтром
  `diff_report.json` → создаёт `DeltaViewerWindow(path, parent=self)` (или передаёт путь в
  `load_report`), `show()`. Окно немодальное (живёт независимо, _final §3 DV-4).
- Сильная ссылка на открытое окно в `self._viewer_windows: list` (чтобы GC не убил до
  закрытия — аналог `_active_workers`, урок §42 для top-level окон).
- Обновить `_on_action_finished` для `compare` (~строка 190): после успешного compare,
  **помимо** текущего `_viewer.set_root` (filesystem), предложить открыть Delta Viewer на
  свежем `diff_report.json` (кнопка в status или автозапрос). _Решение: НЕ открывать
  автоматически (может быть нежелательно) — только добавить кнопку/пункт._ Оставить как есть
  + документировать, что Delta Viewer доступен через меню.

**Тесты (этот же коммит):** расширить offscreen-smoke тесты MainWindow (если есть —
`test_action_panel_smoke.py` по уроку §42; иначе новый `test_main_window_smoke.py`):
- `test_main_window_has_delta_viewer_menu_action`: меню/кнопка присутствует.
- `test_open_delta_viewer_creates_window`: вызвать обработчик с синтетическим
  `diff_report.json` (mock `QFileDialog` через `monkeypatch`) → `DeltaViewerWindow` создан,
  в `_viewer_windows`.

**NOT done тут:** edge diff (вкладка «Рёбра») — отдельный шаг S5.

---

## Шаг P14.S5 — Edge diff (вкладка «Рёбра»)

**Цель:** DV-2 — сравнение рёбер графа между состояниями. Расширяет Phase 9 контракт.

**Это самый «рискованный» шаг** — меняет домен Phase 9. Выполнять строго после S1–S4, чтобы
MVP (markdown + object tree) был уже залит и работоспособен независимо от исхода S5.

**Домен** (`src/db_project_manager/domain/diff.py`):
- Новая модель `EdgeSnapshot` (поля из `Edge.dedup_key()`:
  `source_object_key, destination_object_key, relation, action`).
- `StateSnapshot` — добавить опц. поле `edges: list[EdgeSnapshot] = []` (default пустой,
  обратная совместимость: старые JSON без `edges` парсятся, поле = `[]`).
- `DiffReport` — добавить `edge_summary: dict[str, int] = {"added": 0, "removed": 0}` и
  `edge_entries: list[EdgeDiffEntry] = []`. `EdgeDiffEntry`: `source_edge | target_edge` +
  `status` (added/removed — рёбра не «меняются», они появляются/исчезают).

**Snapshot** (`infrastructure/diff/snapshot.py`):
- `build_snapshot_from_dir` — после сбора вершин собрать рёбра из `graph.edges` в
  `EdgeSnapshot` (через `Edge.dedup_key()`), положить в `StateSnapshot.edges`.

**Comparator** (`infrastructure/diff/comparator.py`):
- После `compare` вершин — сравнить множества `dedup_key` рёбер source vs target:
  added = src − tgt, removed = tgt − src. Заполнить `edge_summary`, `edge_entries`.

**Markdown** (`infrastructure/diff/markdown_report.py`):
- Добавить секцию **Edges** (если есть added/removed рёбра): таблицы `| source → dest |
  relation | action |` для added/removed.

**GUI** (`delta_viewer.py`):
- Третья вкладка «Рёбра» в `QTabWidget` — таблица (`QTableWidget`) добавленных/удалённых
  рёбер; те же фильтры статусов (added/removed рёбер — отдельно или общие с объектами?
  _решение: отдельные чекбоксы для рёбер_, чтобы не путать).

**Урок §45 (критично):** расширение `DiffReport`/`StateSnapshot` — проверить ВСЕ тесты Phase
9, которые конструируют эти модели вручную (`test_comparator.py`, `test_diff_models.py`,
`test_compare_service.py`). Новые поля с default не сломают существующие конструкторы, но
assertion'ы на `report.edge_entries` нужно добавить. Поля аддитивные → старые `diff_report.json`
(без `edges`) парсятся (pydantic default) → обратная совместимость без бампа версии формата.

**Тесты (этот же коммит):**
- `test_comparator.py` — `test_edge_diff_added_removed`: два snapshots с разными рёбрами →
  `edge_summary` корректен, `edge_entries` содержат ожидаемые.
- `test_edge_diff_empty_when_identical`.
- `test_backward_compat_old_json_without_edges`: `DiffReport.model_validate_json` на JSON
  без `edges`/`edge_entries` → поля = `[]`/`{"added":0,"removed":0}`.
- `test_markdown_has_edges_section` (если есть diff рёбер).
- GUI smoke: вкладка «Рёбра» populated.

**Решающая проверка перед S5:** если объём S5 окажется слишком большим или начнёт дестабилизировать MVP — **откатить S5 в отдельный follow-up** (BACKLOG), оставив S1–S4 как завершённую Phase 14. В _final §6 edge diff зафиксирован как часть Phase 14, но в плане — явно помечено «рискованный шаг последним».

---

## Шаг P14.S6 — Регрессия и итоговые проверки

**Цель:** Phase 14 не ломает существующее; новые фичи работают.

- Прогнать полный suite:
  ```bash
  unset SSL_CERT_FILE REQUESTS_CA_BUNDLE CURL_CA_BUNDLE && uv run pytest tests/unit/ -q
  uv run ruff check src/ tests/
  ```
  Цель: 473 baseline (чекпойнт 20260731_001) + новые тесты Phase 14 зелёные; ruff чисто.
- Smoke CLI: `db-pm compare --help` показывает `run` и `report`; `db-pm compare report
  --from <синтетический.json> --output /tmp/x.md` → файл создан.
- Smoke GUI (опц., требует дисплей): `db-pm-gui` → меню Delta Viewer → окно открывается,
  дерево строится, выбор узла показывает детали/diff. (В CI — offscreen-варианты из S3/S4.)
- Проверить обратную совместимость: реальный `diff_report.json` из последнего compare-прогона
  открывается в новом GUI и конвертируется в markdown без ошибок.

---

## Чеклист по урокам (для самопроверки перед каждым коммитом)

- [ ] §35: показываемый DDL — `sql_normalized` (могут быть bare refs); не «лечим» в DV.
- [ ] §36: regex-vs-AST не нужен (нет модификации SQL).
- [ ] §41: offscreen-smoke для `DeltaViewerWindow` и обновлённого `MainWindow`.
- [ ] §42: сигнал → только bound-method; `LoadDiffReportWorker` — сильная ссылка в
  `_active_workers` до `finished`; `DeltaViewerWindow` — сильная ссылка в `_viewer_windows`;
  лямбды в `connect` — никогда.
- [ ] §43: кнопки окна/диалога — после построения основного UI.
- [ ] §18/§45: шаги S1–S4 **не** расширяют контракт Phase 9 → fakes/тесты Phase 9 целы.
  Шаг S5 расширяет домен — проверить ВСЕ наследников/тестов Phase 9 в одном коммите.
- [ ] §12: `git add -- "_docs_/_tasks_/phase_14/..."` (дефис в имени каталога).
- [ ] §1: снимать TLS-переменные перед `uv`.

---

## NOT done в Phase 14 (явно, для `Phase_14.md` и чекпойнта)

- Side-by-side diff (QTableWidget 2 колонки) — follow-up, если unified-diff неудобочитаем.
- Опция `--markdown` в `compare run` (сахар поверх `compare report`) — follow-up.
- Выбор объектов → pipeline CD — отложено до Phase 12 (контракт selection).
- AST-точный diff DDL — работа Phase 12 (структурный column-diff), не DV.
- `QAbstractItemModel` вместо `QTreeWidget` — если производительность недостаточна.

---

## Где читать дальше

- `_tasks_/phase_14/Phase_14_vision_final.md` — нормативный дизайн
- `_tasks_/phase_14/Phase_14_vision_draft.md` — история обсуждения
- `_phases_/Phase_09.md` — compare (источник данных DV)
- `_tasks_/phase_09/001_plan_phase_09.md`, `002_result_phase_09.md` — детали Phase 9
- `LESSONS_LEARNED.md` §35, §41, §42, §43, §45
- `src/db_project_manager/presentation/gui/widgets/workers.py:172` — образец воркера
- `src/db_project_manager/presentation/gui/widgets/project_viewer.py:30` — образец
  splitter-виджета (дерево + детали)
- `tests/unit/test_comparator.py:14-36` — образцы хелперов `_obj`/`_state`
