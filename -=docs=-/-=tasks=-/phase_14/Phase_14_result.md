# Phase 14: Delta Viewer — результат

> **Дата:** 2026-08-04
> **Ветка:** dev
> **Статус:** завершена
>
> Контекст:
> - План: `-=tasks=-/phase_14/Phase_14_plan.md`
> - Норматив-дизайн: `-=tasks=-/phase_14/Phase_14_vision_final.md`
> - История: `-=tasks=-/phase_14/Phase_14_vision_draft.md`
> - Чекпойнт предыдущий: `-=CHECKPOINTS=-/20260731_001_checkpoint.md` (Phase 8 done)

---

## 1. Что сделано

Все 6 шагов плана реализованы в 5 коммитах (по логическому шагу):

| Шаг | Коммит | Суть |
|-----|--------|------|
| S1 | `3649b0c` | markdown-генератор + grouping.py (чистая функция) |
| S2 | `211a402` | CLI `db-pm compare report --from <json>` (офлайн) |
| S3 | `b9b3fd1` | GUI `DeltaViewerWindow` (дерево + детали + diff + фильтры + выбор) |
| S4 | `c9b8bb3` | интеграция в MainWindow (меню «Вид → Delta Viewer…») |
| S5 | `141e9fc` | edge diff — расширение Phase 9 контракта, вкладка «Рёбра» |

S6 (регрессия) — без отдельного коммита; проверки пройдены (см. §4).

### Файлы

| Категория | Файл → Что изменилось |
|-----------|-----------------------|
| Diff infra | `infrastructure/diff/grouping.py` (новый) — чистые grouping-хелперы (status → type → schema) |
| Diff infra | `infrastructure/diff/markdown_report.py` (новый) — `render_diff_markdown`, `write_diff_markdown`, `unified_diff_text`; секции Summary/Added/Removed/Changed/Edges |
| Domain | `domain/diff.py` — `EdgeSnapshot`, `EdgeDiffEntry`; `StateSnapshot.edges`, `DiffReport.edge_summary`/`edge_entries` (аддитивно) |
| Diff infra | `infrastructure/diff/snapshot.py` — `_collect_edges` (граф → `EdgeSnapshot`, дедуп + сортировка, фильтр по `DIFFED_TYPES`) |
| Diff infra | `infrastructure/diff/comparator.py` — edge set-diff (added/removed по `dedup_key`) |
| CLI | `presentation/cli/main.py` — подкоманда `compare report` (`--from`, `--output`) |
| GUI | `presentation/gui/widgets/delta_viewer.py` (новый) — `DeltaViewerWindow` (QMainWindow, splitter, QTreeWidget, QTabWidget: Детали/Diff/Рёбра, фильтры, поиск, выбор → selection.json) |
| GUI | `presentation/gui/widgets/sql_highlighter.py` — `SqlHighlighter(diff_mode=True)` |
| GUI | `presentation/gui/widgets/workers.py` — `LoadDiffReportWorker` |
| GUI | `presentation/gui/main_window.py` — меню, `open_delta_viewer`, `_child_windows` |
| Tests | `tests/unit/test_diff_markdown.py` (новый, 19) — markdown + backward-compat |
| Tests | `tests/unit/test_compare_cli.py` (+5) — `compare report` CLI |
| Tests | `tests/unit/test_delta_viewer.py` (новый, 13) — offscreen-smoke GUI |
| Tests | `tests/unit/test_delta_viewer_integration.py` (новый, 3) — MainWindow wiring |
| Tests | `tests/unit/test_comparator.py` (+5) — edge diff + backward-compat |

## 2. Проверки

```bash
unset SSL_CERT_FILE REQUESTS_CA_BUNDLE CURL_CA_BUNDLE && uv run pytest tests/unit/ -q
# 516 passed (473 baseline + 43 Phase 14)
# (test_crypto_util::test_decrypt_nested_dict — известный flaky BACKLOG P3,
#  в изоляции зелёный; не связан с Phase 14)
uv run ruff check src/ tests/
# All checks passed!
```

**Smoke CLI** (end-to-end):
- `db-pm compare --help` показывает `run` и `report`.
- `db-pm compare report --from <diff_report.json>` → exit 0, `diff_report.md` создан,
  содержит Summary / Changed с collapsible unified-diff (`+`/`-`/`@@` маркеры) / Edges.

**Smoke GUI** (offscreen, через тесты): `DeltaViewerWindow` конструируется, дерево
строится, выбор узла показывает детали/diff, фильтры скрывают узлы, выбор сохраняется
в `selection.json`. Интеграция в MainWindow: меню присутствует, окно создаётся и
удерживается, освобождается при закрытии.

## 3. Ключевые архитектурные решения

- **Markdown сначала, GUI потом** (DV-1): markdown-генератор — чистая функция, закрывает
  BACKLOG P3 независимо от GUI. CLI `compare report` работает офлайн (не повторяет compare).
- **`diff_report.json` самодостаточен**: DV/markdown читают один файл (`report.source` +
  `report.target` + `entries` внутри); `source.json`/`target.json` избыточны.
- **Diff DDL через `difflib.unified_diff`** (DV-5): Phase 9 не хранит diff-блок, только
  `sql_normalized` обеих сторон; diff считается на лету. `unified_diff_text` — единый
  источник правды для markdown и GUI.
- **Отдельное окно, не dock** (DV-4): dock'ов в MainWindow нет (рефактор вне scope);
  action-паттерн Phase 7 не подходит (диалог закрывается по OK). `_child_windows` —
  сильная ссылка на top-level окна (урок §42).
- **Edge diff — часть Phase 14, не отдельная фаза** (DV-2): расширяет Phase 9 контракт
  аддитивными полями с defaults → старые JSON парсятся без ошибок (проверено тестами на
  уровне и `StateSnapshot`, и `DiffReport`). Identity — `Edge.dedup_key()` (урок §16).
- **Status в QTreeWidgetItem хранится как `status.value` (str)**: PySide6 не сохраняет
  enum-identity через `setData` round-trip (str-Enum возвращается как plain str);
  сравнение по строке — единственный стабильный вариант.
- **`SqlHighlighter(diff_mode=True)`**: один `QSyntaxHighlighter` на документ; diff-маркеры
  (`^+`/`^-`/`^@@`) добавлены как правила поверх SQL.

## 4. NOT done / отложено

- **Side-by-side diff** (QTableWidget 2 колонки) — follow-up, если unified-diff неудобочитаем.
- **Опция `--markdown` в `compare run`** (сахар поверх `compare report`) — follow-up.
- **Выбор объектов → pipeline CD** — отложено до Phase 12 (контракт selection); в MVP
  выбор сохраняется только как `selection.json` (DV-3).
- **AST-точный diff DDL** — работа Phase 12 (структурный column-diff CD-ALT-1), не DV.
- **`QAbstractItemModel` вместо `QTreeWidget`** — если производительность на больших
  схемах недостаточна; MVP на `QTreeWidget`.
- **Ручной тест пользователем** на реальном `diff_report.json` из сравнения живой БД —
  не выполнен в этой сессии (требует подключения); offscreen-smoke покрывает контракты.

## 5. Отклонения от плана

- В `_final`/`_plan` предполагалось, что хелпер `_group_entries` будет продублирован в GUI
  локально (чтобы GUI не зависел от `markdown_report`). В реализации вынесен в отдельный
  модуль `infrastructure/diff/grouping.py`, который используют **оба** (markdown и GUI) —
  чище, без дублирования, и не создаёт лишней связности с markdown-модулем.
- `_render_edges_section` добавлен в markdown (в плане отмечено как часть S5) — реализовано
  по образцу секций объектов.

## 6. Где читать дальше

1. `-=tasks=-/phase_14/Phase_14_vision_final.md` — нормативный дизайн (закрытые DV-1..DV-7)
2. `-=tasks=-/phase_14/Phase_14_plan.md` — пошаговый план
3. `-=PHASES=-/Phase_14.md` — свод фазы
4. `-=PHASES=-/Phase_09.md` — compare (источник данных DV)
5. `LESSONS_LEARNED.md` §16, §35, §41, §42, §43
