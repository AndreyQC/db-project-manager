# Phase 7: GUI — панель действий для подключений

> Дата: 2026-07-25
> Статус: завершена
> План: `-=docs=-/-=tasks=-/phase_07/001_plan_phase_07.md`
> Результат: `-=docs=-/-=tasks=-/phase_07/002_result_phase_07.md`
> Vision (норматив): `-=docs=-/-=tasks=-/phase_07/Phase_7_vision_final.md`

---

## 1. Цель фазы

- Перестроить главное окно GUI вокруг расширяемого **реестра действий**
  (dropdown), вместо жёстко зашитых кнопок run/deploy.
- Единый контейнер для всех действий: «Настроить…» / «Выполнить» / сводка
  настроек / копирование CLI-команды.
- Персистентность настроек действий (`gui_settings.json`) с prefill последних
  значений.
- Сохранить функционал управления подключениями (add/edit/delete) без изменений.

---

## 2. Что сделано

| Категория | Файл → Что изменилось |
|-----------|-----------------------|
| GUI actions | `presentation/gui/actions/registry.py` → `ActionSpec`/`ACTIONS` — 3 действия, ленивые фабрики |
| GUI actions | `presentation/gui/actions/models.py` → pydantic-модели настроек действий |
| GUI actions | `presentation/gui/actions/cli.py` → `build_cli` для копируемой CLI-команды |
| GUI actions | `presentation/gui/actions/dialogs.py` → диалоги «Настроить…» (3 шт.) |
| GUI widgets | `presentation/gui/widgets/action_panel.py` → контейнер действия (dropdown, summary, кнопки, CLI-строка) |
| GUI widgets | `presentation/gui/widgets/workers.py` → +`GraphBuildWorker` |
| GUI | `presentation/gui/main_window.py` → интеграция панели; удалены старые кнопки и глобальная «Папка вывода» |
| Config | `infrastructure/config/gui_settings.py` → `GuiSettingsStore` (JSON, атомарная запись, corrupt-safe) |
| Repo | `.gitignore` → +`gui_settings.json` |
| Tests | `test_gui_settings.py` (8), `test_action_cli.py` (9), `test_action_registry.py` (6) |

Ключевые коммиты: `dda2312`, `3a8ee9a`, `4c3c800`, `a481f10`, `1dee90d`,
`ed199c2`, `1991560`, `0637678`.

---

## 3. Ключевые архитектурные решения

- **Декларативный реестр действий** (`ActionSpec`): новое действие = одна запись
  в `ACTIONS` + модель + диалог + worker; `MainWindow` и панель не меняются.
  Прямое требование пользователя («со временем действий будет больше»).
- **GUI↔CLI контракт тестом**: сгенерированная CLI-строка парсится реальным
  typer-приложением через CliRunner (сервисы замоканы) — страховка от
  рассинхрона флагов (LESSONS §39).
- **`validate_graph` вместо `validate`** в модели — pydantic shadowing
  (LESSONS §40).
- **Gephi-просмотр внешний**: graphml-экспорт уже Gephi-совместим, новый формат
  не нужен; GEXF — BACKLOG (решение U4).
- **Настройки — JSON, не YAML**: пишет только приложение; атомарная запись
  (tmp + `os.replace`), битый файл → дефолты + warning, без падения.
- **«Выполнить» без настройки** — с дефолтами; пустые `required_fields` →
  auto-open диалога (решение U7).

---

## 4. Проверки

```bash
unset SSL_CERT_FILE REQUESTS_CA_BUNDLE CURL_CA_BUNDLE && uv run pytest tests/unit/ -q
# 301 passed (278 baseline + 23 новых) — OK
uv run ruff check src/ tests/
# All checks passed!
```

Headless smoke (`QT_QPA_PLATFORM=offscreen`, LESSONS §41): MainWindow создаётся,
dropdown содержит 3 действия, summary/CLI переключаются, дефолты подставляются — OK.

---

## 5. Известные ограничения / NOT done

- GUI-виджеты без unit-тестов (как и раньше; логика в тестируемых слоях).
- Копируемая CLI — строка для ручного запуска; GUI исполняет через services.
- GEXF-экспорт, per-row dropdown в списке подключений — BACKLOG.
- Overload resolution (бывший «Phase 7» по BACKLOG P1) — перенесён в Phase 8
  (решение U1).
- Живой прогон reverse/deploy из нового UI против реальной БД не выполнялся
  (workers переиспользованы без изменений; smoke — offscreen).

---

## 6. Где читать дальше

1. `-=docs=-/-=tasks=-/phase_07/Phase_7_vision_final.md` — нормативный дизайн, §13 — решения U1–U7.
2. `-=docs=-/-=tasks=-/phase_07/002_result_phase_07.md` — результат, отклонения от плана.
3. `-=docs=-/-=CHECKPOINTS=-/20260725_001_checkpoint.md` — снапшот после фазы.
4. `LESSONS_LEARNED.md` §39-41 — уроки фазы.
5. `-=docs=-/-=tasks=-/phase_07/Phase_7_vision_draft.md` — история обсуждения.
