# DROP FUNCTION/PROCEDURE на Greenplum 6: обязательный список аргументов (plan)

> **Date:** 2026-10-09
> **Status:** реализовано в тот же день — см. `20261009_002_drop_function_gp_parens_result.md`

> Контекст:
> - Сообщение Сергея Игонина (пользователь программы, БД cis_zup, Greenplum):
>   deploy apply упал на шаге 6, функция `fun_lu_zup_accountgroups_etl`,
>   «синтаксическая ошибка `DROP FUNCTION IF EXISTS ...;`». Артефакты:
>   `deploy_artefact/jumping-crimson-cal` (машина Сергея).
> - `src/db_project_manager/infrastructure/database/postgres/adapter.py`
>   (`drop_schema_contents`, Phase 18) — прецедент и документация грамматики.
> - `LESSONS_LEARNED.md` §61 (пользовательское описание ошибки = гипотеза).

## Что случилось

`deploy apply` с `--include-drops` на Greenplum упал на REMOVED-функции
`fun_lu_zup_accountgroups_etl` с синтаксической ошибкой. Сгенерированный
артефакт:

```sql
DROP FUNCTION IF EXISTS "cis_dmt_zup"."fun_lu_zup_accountgroups_etl";
```

Ошибка PostgreSQL: `syntax error at or near ";"` — парсер указывает на точку
с запятой, поэтому в отчёте пользователя фигурировала «лишняя `;`».

## Корневая причина

Не точка с запятой, а **отсутствие списка аргументов**. Ядро Greenplum 6 —
PostgreSQL 9.4, а до PG 10 грамматика `DROP FUNCTION/PROCEDURE` требует
`(<список аргументов>)` после имени: без скобок парсер встречает `;` там, где
ждет `(`, и ругается именно на `;`.

Цепочка в `DeltaService._drop_statement`
(`src/db_project_manager/application/delta_service.py`):

1. REMOVED-объект по определению отсутствует в кодовой базе → в графе его
   вершины нет → `vertex is None` при генерации артефакта.
2. Старый код при `vertex is None` / пустых `argument_types` рендерил
   стейтмент **без скобок вовсе** — инвалидный на GP 6.

Прецедент в этом же репо: `adapter.drop_schema_contents` (Phase 18) всегда
пишет список аргументов, `()` для безаргументных, с комментарием «kernels
< PG 10 (Greenplum 6) make it mandatory».

## Почему «правка уже была», а баг вернулся

Хотфикс (фолбэк `args = "()"`) существовал только в локальной копии Сергея.
Git-археология: pickaxe по вариантам без `;` и с `args = "()"` не находит ни
одного коммита, reflog чист, `delta_service.py` трогал один-единственный
коммит `4912745` (Phase 12 S5) — файл «родился» с багом. Портативный бандл
`dbpm-portable/site/...` несёт ту же старую версию (пересобран из репо
2026-10-07) — именно через него баг и пришёл к пользователю.

## Решение (уровень 1 — минимальный фикс)

В `_drop_statement` фолбэк по умолчанию `args = "()"` для FUNCTION/PROCEDURE
(вариант Сергея), трейлинг-`;` сохраняется (легален везде; таблицы его
ассертят тестами):

```python
args = "()"
if kind not in ("FUNCTION", "PROCEDURE"):
    args = ""
elif vertex is not None and vertex.argument_types:
    args = "(" + ", ".join(...) + ")"
```

## Проверки

- Юнит: REMOVED-функция → `DROP FUNCTION IF EXISTS "app"."fun_old_etl"();`;
  REMOVED-процедура без `--include-drops` → BLOCKED-комментарий со скобками;
  рутина с `argument_types` → рендер сигнатуры из вершины.
- Полный прогон `uv run pytest` + `uv run ruff check .`.

## Известные ограничения (не входит в этот фикс)

Фолбэк `()` для REMOVED-функции **с аргументами** даёт «function does not
exist», который `IF EXISTS` молча проглатывает, — функция не удаляется, а
план считает операцию выполненной (тихий no-op). Правильное решение —
прокинуть сигнатуру в `ObjectSnapshot`: оформлено отдельной задачей
`20261009_003_removed_routine_signature_plan.md`.

## Бандл

`dbpm-portable/` **не правится точечно** (решение пользователя: пересобирается
по мере требований). Внимание: бандл несёт старый код — до пересборки из
фикснутого репо деплой через бандл на GP будет падать так же.
