# Переименование каталогов харнеса: `_docs_` → `harness_docs` (result)

> Контекст:
> - `harness_docs/harness_tasks/2026-11-03/20261103_001_harness-dir-rename_plan.md`
>   — план и замер.
> - lm_hrdo_2, задача 20261102_940 — эталонная миграция
>   (`harness_docs/harness_tasks/2026-11-02/20261102_940_harness-dir-rename_plan.md`
>   в репо lm_hrdo_2).

**Дата:** 2026-11-03
**Статус:** Выполнено

---

## Что сделано

1. **git mv** (порядок из плана): `_docs_/_checkpoints_` → `harness_checkpoints`,
   `_phases_` → `harness_phases`, `_tasks_` → `harness_tasks`, затем
   `_docs_` → `harness_docs`. Без «Permission denied» — обводка из плана
   не понадобилась.
2. **Правка `misc/harness_dir_rename.py`** относительно эталонной копии:
   - `SKIP_DIRS`: убран `src` (код перезаписывается наравне с доками),
     добавлен `dbpm-portable` — портативная сборка вне git, в вендорённом
     коде pydantic есть идентификаторы `_docs_for_groups` / `_docs_extraction`
     (ложные совпадения по подстроке, портить нельзя);
   - `SKIP_FILES`: добавлены исторические `move_docs_dirs.py` /
     `rename_docs_dirs.py` (память о прошлом переезде — не трогать);
   - добавлены backslash-варианты правил (`_docs_\_checkpoints_` и пр.):
     в этом репо доки ссылаются Windows-путями, эталонный скрипт знает
     только `/`;
   - добавлено bare-правило внутренних имён в **путевом** употреблении
     (`_tasks_/…`, `_checkpoints_\…`) через regex с guard'ом слева:
     в `harness_checkpoints/` подстрока `_checkpoints_/` присутствует,
     простой replace дал бы `harness_harness_` (двойной префикс).
     Bare-проза без слэша скриптом не переписывается.
3. **Перезапись** (`python -I misc/harness_dir_rename.py .`, preview →
   `--apply`): итог **505 замен в 118 файлах** (первый проход 230/81,
   второй после расширения правил 275/37). Двойных префиксов нет.
4. **Ручные правки** (bare-проза без слэша): `CHECKPOINTS_CONVENTION.md`,
   `TASK_CONVENTIONS.md` (включая дерево структуры §1), `PHASES_CONVENTION.md`.

## Отклонения от замера плана

- Замер «~80 файлов» оказался занижен: эталонные правила не покрывали
  bare-употребления (`_tasks_/BACKLOG.md` без префикса `_docs_/`) и
  backslash-пути — с ними фактические 118 файлов. Кодовых файлов в `src/`
  ровно 8, как в замере (5 с `_docs_`-префиксом: domain/{safety, deploy,
  delta, diff}, parsing/overload_resolution; 3 bare: diff/markdown_report,
  gui/widgets/{delta_viewer, plan_viewer} — все ссылки на дизайн-доки в
  докстрингах).

## Решения

- `.zcode/plans/*` перезаписаны: трекаются git и несут живые ссылки на
  пути харнеса.
- `output/` не тронут (SKIP_DIRS): в diff-артефактах `_docs_`-подстроки —
  это имена БД-индексов вида `idx_company_verification_docs_company`,
  не ссылки харнеса.
- Bare-проза в исторических доках (старые чекпойнты, `.zcode/plans`) не
  правилась — история; актуальные конвенции правлены руками.
- Исторический раздел §8 `TASK_CONVENTIONS.md` (про исходную версию
  регламента) оставлен с `_tasks_` намеренно.

## Проверки

- `uv run pytest` — **1341 passed, 45 deselected** (как до переименования).
- `uv run ruff check .` — чисто.
- `uv run db-pm --help` — работает.
- `git grep -n "_docs_\|_archives_"` вне misc/ и output/ — пусто
  (в misc/: сам скрипт-ренеймер по дизайну self-skip и два исторических
  скрипта; в output/: БД-имена, ложные совпадения).
- `harness_docs/REFRESH_CONTEXT.md` и
  `harness_docs/harness_tasks/HOW_TO_START_NEW_SESSION.md` читают новые
  пути (backslash-ссылки починены вторым проходом).
- Идемпотентность скрипта: повторный прогон preview после apply даёт 0 замен.

## Известные ограничения

- Скрипт остаётся одноразовым инструментом переезда (self-skip, байт-точная
  замена); для следующего репо по ранбуку требуется та же правка SKIP/правил.
- `misc/rename_docs_dirs.py`, `misc/move_docs_dirs.py` содержат старые имена
  намеренно (история v1 переезда).
