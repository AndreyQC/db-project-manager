# Переименование каталогов харнеса: `_docs_` → `harness_docs` (plan)

> Контекст:
> - lm_hrdo_2, задача 20261102_940 — эталонная миграция выполнена 02.11
>   (коммиты `78467a17` feat(infra), `06dac8f8` docs(harness), push в dev);
>   ранбук — `harness_docs/harness_tasks/2026-11-02/20261102_940_harness-dir-rename_plan.md`
>   в репо lm_hrdo_2 (раздел «Приложение · ранбук адаптации»).
> - `misc/harness_dir_rename.py` — скрипт перезаписи, скопирован из lm_hrdo_2
>   (байт-точная замена, self-skip, preview по умолчанию).
> - Причина: агенты систематически путаются в каталогах вида `_{каталог}_`
>   (нормализуют до `docs/`, роняют подчёркивания) — единая конвенция для
>   всех harness-репозиториев: `harness_{каталог}` без подчёркиваний.

**Дата:** 2026-11-02 (создано) · **Запланировано к работе:** 2026-11-03
**Статус:** Не начат

---

## Контекст

lm_hrdo_2 02.11.2026 переименовал каталоги харнеса: `_docs_` → `harness_docs`
(+ внутренние `_checkpoints_`/`_tasks_`/`_plans_`/`_domains_`/`_phases_`/
`_sessions_`/`_index` → `harness_*`), `_archives_` → `harness_archives`;
3206+ замен ссылок/футеров/реестра. Воркер Day Cycle (Temporal) переведён на
новые пути (`fd1a660` в asisay-automation-worker-temporal). Настал черёд
db-project-manager — здесь своё дерево `_docs_/` (REFRESH_CONTEXT.md,
`_checkpoints_`, `_phases_`, `_tasks_`; harness_index/реестра нет).

Замер 02.11: **8 кодовых файлов** в `src/db_project_manager/…` (domain:
safety, deploy, delta, diff; infrastructure: parsing/overload_resolution,
diff/markdown_report; presentation/gui/widgets: delta_viewer, plan_viewer),
**72 `.md`**, корневые AGENTS.md, ONBOARDING.md, LESSONS_LEARNED.md,
PREPAREENV.md. В `misc/` исторические скрипты `move_docs_dirs.py` /
`rename_docs_dirs.py` (переезды `-=…=-` → `_…_`) — не трогать, только
память о прошлом переезде.

## Намерение

1. **Правка скрипта** `misc/harness_dir_rename.py`: из `SKIP_DIRS` убрать
   `"src"` — код db-pm с путями `_docs_` должен переписаться; добавить
   `".venv"`, если нет. Итоговый набор: `.git, node_modules, dist, build,
   vendor, targets, .venv, __pycache__, .idea, .vscode, output, logs`.
2. **git mv** (порядок: внутренние, затем корень):
   ```bash
   git mv _docs_/_checkpoints_ _docs_/harness_checkpoints
   git mv _docs_/_phases_     _docs_/harness_phases
   git mv _docs_/_tasks_      _docs_/harness_tasks
   git mv _docs_              harness_docs
   ```
   Возможен «Permission denied» на каталоге (watcher держит handle) —
   обход: создать новый каталог, `git mv` файлы поштучно, старый удалить.
3. **Перезапись**: `python -I misc/harness_dir_rename.py .` (preview,
   сверить с замером ~80 файлов) → `--apply`.
4. **Хвосты**: `git grep -n "_docs_\|_archives_" -- . ':(exclude)misc/rename_docs_dirs.py' ':(exclude)misc/move_docs_dirs.py'`
   — легитимны только упоминания в исторических скриптах misc/ и слаги
   задач (`*_docs_*_plan.md`).
5. **Проверка**: `pytest` (8 файлов кода затронуты), `db-pm --help`,
   перечитать REFRESH_CONTEXT.md (пути обновлены скриптом).
6. **Коммит**: один `docs(harness)` — mv + перезапись; конвенции в
   AGENTS.md — обновить упоминания `_docs_` → `harness_docs`.

## Критерии приёмки

- [ ] `harness_docs/` (+3 внутренних harness_*) существуют, `_docs_` нет
- [ ] Код src/ перезаписан (0 ссылок на `_docs_` вне misc-архивов)
- [ ] `pytest` зелёный
- [ ] REFRESH_CONTEXT.md читает новые пути
- [ ] git grep хвостов — только легитимные упоминания

## Связанные документы

- lm_hrdo_2 `_docs_…/20261102_940_harness-dir-rename_plan.md` — ранбук и
  эталонный порядок (эталонный репо: `C:/repos/reksoft/locomoko/lm_hrdo_2`).
- `misc/harness_dir_rename.py` — скрипт (уже скопирован в этот репозиторий).
- `misc/rename_docs_dirs.py` — исторический шаблон (v1 переезда).
