# DROP FUNCTION/PROCEDURE на Greenplum 6: обязательный список аргументов (result)

> **Date:** 2026-10-09
> **Status:** done

> Контекст:
> - План: `_tasks_/2026-10-09/20261009_002_drop_function_gp_parens_plan.md`
> - Коммит кода: `65abe4c` (fix(app), код и документы раздельно).

## Что сделано

`src/db_project_manager/application/delta_service.py`, `_drop_statement`:

- фолбэк `args = "()"` по умолчанию для `FUNCTION`/`PROCEDURE` — стейтмент
  валиден на ядрах < PG 10 (Greenplum 6), где список аргументов обязателен в
  грамматике `DROP FUNCTION/PROCEDURE`;
- при наличии вершины с `argument_types` рендерится настоящая сигнатура
  (как и раньше);
- трейлинг-`;` сохранён (легален; контракт таблицных DROP-тестов);
- комментарий в коде объясняет GP-специфику и ссылается на прецедент
  `drop_schema_contents` (Phase 18).

Тесты (`tests/unit/test_delta_service.py`, +3):

- `test_removed_function_drop_carries_empty_arg_list` — REMOVED-функция,
  GP, `--include-drops` → `DROP FUNCTION IF EXISTS "app"."fun_old_etl"();`;
- `test_removed_function_blocked_drop_commented_with_parens` — REMOVED-
  процедура без флага → BLOCKED-комментарий со скобками;
- `test_drop_statement_uses_vertex_argument_types` — вершина с
  `argument_types="integer, varchar(50)"` → сигнатура в стейтменте.

## Проверки

- `uv run pytest tests/unit/test_delta_service.py -q` — 12 passed.
- `uv run pytest` — **1274 passed**, 40 deselected (integration; Docker не
  поднимался).
- `uv run ruff check .` — чисто.

## Подтверждение диагностики

- Ошибка пользователя «лишняя `;`» — симптом: PG < 10 при отсутствии `(...)`
  репортит `syntax error at or near ";"` (парсер ждёт `(` после имени).
  Повторение LESSONS §61 Урок #3: пользовательское описание = гипотеза.
- Правка, которую «уже делали», в историю репо не попадала (pickaxe/reflog
  это доказали) — существовала только в копии Сергея; бандл, пересобранный
  из репо 2026-10-07, вернул баг.

## Известные ограничения

- Тихий no-op: REMOVED-функция **с аргументами** получает `f()` →
  «function does not exist» → `IF EXISTS` молча пропускает. Сигнатуры
  REMOVED-рутин надо прокидывать через `ObjectSnapshot` — задача
  `20261009_003_removed_routine_signature_plan.md` (уровень 2).
- Интеграционного теста на живом GP нет (Docker-контейнер — postgres, не
  greenplum); GP-грамматика зафиксирована юнит-тестом на уровне
  генерации стейтмента.

## Бандл (не правится)

`dbpm-portable/` пересобирается пользователем по мере требований. До
пересборки из фикснутого репо деплой через бандл на GP 6 упадёт так же.
**При следующей сборке бандла этот фикс попадёт автоматически.**
