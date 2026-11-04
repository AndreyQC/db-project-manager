# Триггеры как объекты деплоя — порядок выше function (result)

> Контекст:
> - `20261028_001_trigger_deploy_order_plan.md` — план, варианты, находки.
> - `harness_checkpoints/20261027_001_checkpoint.md` — находки сессии 2026-10-27.
> - qr-репо `qr-db-miroslav-asisay`, задача 20261023_588 (этап 3 — триггеры).

**Дата:** 2026-11-03 (выполнено; план создан 2026-10-28)
**Статус:** Выполнено

---

## Что сделано

### db-pm (вариант 1 из плана)

- `infrastructure/graph/topological_sort.py`: `TYPE_PRIORITIES["trigger"]`
  **3 → 6** — триггер деплоится после function/procedure (5), которые
  выполняет, и после table (2) / view (4), к которым прикреплён. Делит полосу
  с `policy`/`grant` (6): type-констант между ними нет, ничья разбирается
  `topo_index`. Обратная сторона (deploy reset, drop в обратном порядке)
  тоже корректна: триггер дропается раньше функции.
- `tests/unit/test_topological_sort.py`: +2 регресс-теста —
  `test_trigger_after_function_table_view_by_type_priority` (смешанный граф
  schema/table/view/function/procedure/trigger + явное ребро trigger→function;
  при старом приоритете 3 падало бы) и `test_trigger_drop_order_reversed_…`;
  в `test_type_priority_constants` добавлены инварианты
  `function/procedure/view < trigger`.

### qr-репо `C:/repos/personal/qr-db-miroslav-asisay` (dev)

- С 17 файлов триггеров (qr_commerce/qr_identity/qr_marketplace/qr_memorial)
  снят `build: false` → `build: true`, удалён коммент «временно: db-pm
  сортирует trigger раньше function» (задача 20261026_*).
- qr-repo ходит в `DB_PM_HOME=C:/repos/personal/GITHUB/db-project-manager`
  (`.venv/Scripts/db-pm.exe`) — правка приоритета подхватилась из рабочего
  дерева без релиза.

## Проверки

| Проверка | Результат |
|---|---|
| `make graph-validate` (qr) | ✓ циклов и висячих ссылок нет |
| `make deploy-validate` (qr, чистая временная БД `qr_20261103T160814`) | ✓ **584/584 объектов**, все 17 триггеров в хвосте плана (позиции 568–584, после функций/процедур), post-seed ×2 выполнен, БД удалена |
| `uv run pytest` (db-pm) | **1343 passed, 45 deselected** (+2 новых теста) |
| `uv run ruff check .` (db-pm) | чисто |

## Решения / отклонения от плана

- Вариант 1 выполнен как рекомендовано; вариант 2 (приоритетная очередь Кана)
  остаётся в `BACKLOG.md` (P3) — возвращаться при первом конфликте порядка,
  который нельзя починить правкой приоритета (например, выраженные индексы
  с функциями: `index=3` раньше `function=5`).
- Пункт плана 3 (браться ли за этап 1 плана 588 — constraint-объекты,
  взаимные FK) **не решён** — вне этой задачи.
- RE по-прежнему не выгружает триггеры (попутная находка плана) — отдельно.

## Известные ограничения

- Деплой-валидация прогнана на локальном PG (`localhost:5432/postgres`,
  `PG_DEMO_CONN`); GP/CH не затронуты — правка общего порядка объектов.
  GP-регресс рекомендуется при следующем плановом прогоне (триггеры в GP
  тоже после функций — семантика та же).
- В `Makefile` qr-репо ничего не менялось; `build: true` теперь явно в
  autodoc — при следующем RE-рерайне файлов формат сохранится.

---

*Файл: `harness_docs/harness_tasks/2026-10-28/20261028_001_trigger_deploy_order_result.md`*
