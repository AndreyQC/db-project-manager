# Phase 14: Delta Viewer

> Дата: 2026-08-04
> Статус: завершена
> План: `-=docs=-/-=tasks=-/phase_14/Phase_14_plan.md`
> Результат: `-=docs=-/-=tasks=-/phase_14/Phase_14_result.md`
> Норматив-дизайн: `-=docs=-/-=tasks=-/phase_14/Phase_14_vision_final.md`

---

## 1. Цель фазы

- Просмотрщик `diff_report.json` (результата Phase 9 `compare`) для review-сценария:
  дерево объектов с цветовой индикацией статусов, построчный diff DDL, фильтры, выбор.
- Markdown-отчёт из того же JSON (закрывает BACKLOG P3 «Markdown-отчёт сравнения»).
- Сравнение рёбер графа между состояниями (закрывает BACKLOG P2 «Edge diff») — вкладка
  «Рёбра» в GUI + секция в markdown.

ROADMAP §5 ставил DV как шаг 6, опирающийся только на готовую Phase 9 (✓).

---

## 2. Что сделано

| Категория | Файл → Что изменилось |
|-----------|-----------------------|
| Diff infra | `infrastructure/diff/grouping.py` (новый) — grouping-хелперы (status → type → schema), общий для markdown и GUI |
| Diff infra | `infrastructure/diff/markdown_report.py` (новый) — `render_diff_markdown`, `write_diff_markdown`, `unified_diff_text` |
| Domain | `domain/diff.py` — `EdgeSnapshot`, `EdgeDiffEntry`; `StateSnapshot.edges`, `DiffReport.edge_summary`/`edge_entries` (аддитивно) |
| Diff infra | `infrastructure/diff/snapshot.py` — `_collect_edges` (граф → `EdgeSnapshot`) |
| Diff infra | `infrastructure/diff/comparator.py` — edge set-diff (added/removed) |
| CLI | `presentation/cli/main.py` — подкоманда `compare report` (`--from`, `--output`) |
| GUI | `presentation/gui/widgets/delta_viewer.py` (новый) — `DeltaViewerWindow` |
| GUI | `presentation/gui/widgets/sql_highlighter.py` — `diff_mode=True` |
| GUI | `presentation/gui/widgets/workers.py` — `LoadDiffReportWorker` |
| GUI | `presentation/gui/main_window.py` — меню «Вид», `open_delta_viewer` |
| Tests | 4 новых/расширенных файла (+43 теста: 516 всего) |

Ключевые коммиты: `3649b0c` (S1), `211a402` (S2), `b9b3fd1` (S3), `c9b8bb3` (S4),
`141e9fc` (S5).

---

## 3. Ключевые архитектурные решения

- **Markdown сначала, GUI потом** (DV-1): markdown — чистая функция, закрывает BACKLOG P3
  независимо от GUI; CLI `compare report` работает офлайн (читает готовый JSON).
- **`diff_report.json` самодостаточен**: обе стороны (`report.source`/`report.target`) +
  `entries` + `summary` в одном файле; `source.json`/`target.json` избыточны.
- **Diff DDL через `difflib.unified_diff`** (DV-5): Phase 9 не хранит diff-блок, только
  `sql_normalized`; `unified_diff_text` — единый источник для markdown и GUI.
- **Отдельное окно, не dock** (DV-4): dock'ов в MainWindow нет (рефактор вне scope);
  `_child_windows` — сильная ссылка на top-level окна (урок §42).
- **Edge diff — часть Phase 14** (DV-2): расширяет Phase 9 контракт **аддитивными**
  полями с defaults → старые JSON парсятся без ошибок (проверено тестами на двух уровнях).
  Identity — `Edge.dedup_key()` (урок §16).
- **Status в QTreeWidgetItem — `status.value` (str)**: PySide6 не сохраняет enum-identity
  через `setData` round-trip; сравнение по строке стабильно.

---

## 4. Проверки

```bash
unset SSL_CERT_FILE REQUESTS_CA_BUNDLE CURL_CA_BUNDLE && uv run pytest tests/unit/ -q
# 516 passed (473 baseline + 43 Phase 14)
# (test_crypto_util::test_decrypt_nested_dict — известный flaky BACKLOG P3, в изоляции зелёный)
uv run ruff check src/ tests/
# All checks passed!
```

Smoke CLI: `db-pm compare --help` → `run` + `report`; `db-pm compare report --from
<json>` → `diff_report.md` создан (Summary + Changed collapsible diff + Edges).

Smoke GUI (offscreen): дерево строится, выбор узла → детали/diff, фильтры скрывают,
выбор → `selection.json`; интеграция в MainWindow (меню + удержание окна).

---

## 5. Известные ограничения / NOT done

- **Side-by-side diff** (2 колонки) — follow-up, если unified-diff неудобочитаем.
- **Опция `--markdown` в `compare run`** (сахар поверх `compare report`) — follow-up.
- **Выбор объектов → pipeline CD** — отложено до Phase 12 (контракт selection); MVP
  сохраняет только локальный `selection.json` (DV-3).
- **AST-точный diff DDL** — работа Phase 12 (CD-ALT-1), не DV.
- **`QAbstractItemModel` вместо `QTreeWidget`** — если производительность на больших
  схемах недостаточна.
- **Ручной тест пользователем** на реальном `diff_report.json` живой БД — не выполнен
  (требует подключения); offscreen-smoke покрывает контракты.

---

## 6. Где читать дальше

1. `-=docs=-/-=tasks=-/phase_14/Phase_14_vision_final.md` — нормативный дизайн (DV-1..DV-7)
2. `-=docs=-/-=tasks=-/phase_14/Phase_14_plan.md` — пошаговый план
3. `-=docs=-/-=tasks=-/phase_14/Phase_14_result.md` — результат (детали, коммиты)
4. `-=PHASES=-/Phase_09.md` — compare (источник данных DV)
5. `LESSONS_LEARNED.md` §16, §35, §41, §42, §43
