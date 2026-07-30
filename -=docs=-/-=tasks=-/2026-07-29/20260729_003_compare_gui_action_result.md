# Результат — GUI action для compare

> Дата: 2026-07-30 (закрытие); заведено 2026-07-29
>
> Контекст:
> - `-=docs=-/-=tasks=-/2026-07-29/20260729_002_compare_gui_action_final.md` — нормативный дизайн
> - `-=docs=-/-=tasks=-/2026-07-29/20260729_001_compare_gui_action_draft.md` — история обсуждения
> - `-=docs=-/-=tasks=-/phase_09/002_result_phase_09.md` — результат CLI-реализации compare (Phase 9)

Статус: реализация завершена, ручной тест пользователем пройден.

---

## 1. Что сделано

| Что | Коммит |
|-----|--------|
| `_final` дизайн GUI action | `6fafe9a` |
| Реализация: 4-е действие в реестре Phase 7 (`CompareSettings`, `build_cli_compare`, `CompareDialog`, `CompareWorker`, `_side_spec`, `ActionSpec`, метки, ветка `compare` в `_on_action_finished`) | `c26ecd7` |
| BACKLOG: помечен ВЫПОЛНЕНО | `22f783c` |
| Hotfix (ручной тест): плейсхолдер `(каталог вместо подключения)` — выбор каталога без нарушения XOR | `1d261db` |

Видение из `_final` выполнено: 4 поля с неявным XOR (решение 1), `required_fields=("output_dir",)` (решение 2), после выполнения — открыть отчёт + сводка (решение 3).

---

## 2. Отклонения от плана / доработки по ручному тесту

1. **Плейсхолдер в combo подключений (hotfix `1d261db`).** `_final` предполагал неявное XOR
   «пользователь заполняет одно из двух». На практике при наличии подключений в `store`
   `_connections_combo` выставлял индекс 0 (первое подключение) → заполнение каталога
   рядом давало ошибку «указаны и подключение, и каталог». Решение: общий хелпер
   `_connections_combo(allow_empty=True)` получает ведущий пункт
   `(каталог вместо подключения)` с `userData=""`, выбранный по умолчанию; `CompareDialog`
   читает `currentData()`. Существующие диалоги (`allow_empty=False`) не изменились.

---

## 3. Проверки

```bash
unset SSL_CERT_FILE REQUESTS_CA_BUNDLE CURL_CA_BUNDLE && uv run pytest tests/unit/ -q
# 380 passed (369 + 11 новых за GUI-задачу) — OK
uv run ruff check src/ tests/
# All checks passed!
```

| Тест-файл | Изменение | Кол-во |
|-----------|-----------|--------|
| `test_action_registry.py` | `test_expected_actions_present` → 4 действия | +0 (правка) |
| `test_action_cli.py` | `build_cli_compare`: DIR-DIR, DB-DB, mixed, keep вкл/выкл, пробелы, omitted side + CliRunner-контракт | +8 |
| `test_action_panel_smoke.py` | offscreen: кнопки последние в `CompareDialog`; worker error при отсутствии манифеста; **плейсхолдер combo (регрессионный)** | +3 |

Offscreen smoke: `MainWindow` создаётся, в dropdown 4 действия, compare — последнее;
`CompareDialog` открывается, кнопки внизу, roundtrip настроек работает.

**Ручной тест пользователем:** пройден. Замечание (XOR-ошибка при выборе каталога)
закрыто hotfix'ом `1d261db`. Сформирован отчёт `source.json`/`target.json`/`diff_report.json`,
каталог отчёта открыт в viewer, показана сводка.

---

## 4. Известные ограничения

- **Markdown-отчёт** не реализован — viewer показывает `diff_report.json` как текст
  (BACKLOG P3, отдельная задача).
- **Валидация XOR прямо в диалоге** (disable одного поля при заполнении другого) — не
  делается; правило в `_side_spec`/CLI + плейсхолдер combo. UX-улучшение → BACKLOG.
- **Progress-бар детерминированный** — индикатор неопределённый, как у существующих действий.

---

## 5. Файлы (все изменения в существующих модулях)

| Файл | Что |
|------|-----|
| `presentation/gui/actions/models.py` | + `CompareSettings` |
| `presentation/gui/actions/cli.py` | + `build_cli_compare`, `_side_cli` |
| `presentation/gui/actions/dialogs.py` | + `CompareDialog`; `_connections_combo(allow_empty=)`, `CONNECTION_EMPTY_LABEL` |
| `presentation/gui/widgets/workers.py` | + `CompareWorker` |
| `presentation/gui/actions/registry.py` | + фабрики, `_side_spec`, запись в `ACTIONS` |
| `presentation/gui/widgets/action_panel.py` | + метки полей, `ACTION_FIELD_LABELS["compare"]` |
| `presentation/gui/main_window.py` | + `elif "compare"`, `_compare_summary`; `ValueError` в `_on_execute` |

Новых файлов нет — по паттерну Phase 7 (расширение реестра, не новая подсистема).

---

## 6. Следующие шаги

- BACKLOG P2: edge diff (сравнение рёбер графа).
- BACKLOG P3: Markdown-отчёт; валидация XOR в диалоге (disable полей); настраиваемый фильтр типов.
- Phase 8: overload resolution in edge detection (BACKLOG P1).
