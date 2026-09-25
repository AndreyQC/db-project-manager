# GP 6: обязательные скобки в DROP FUNCTION при deploy reset (plan)

> Контекст:
> - `_docs_/_tasks_/2026-09-17/20260917_001_deploy_reset_final.md` (Phase 18, D9 content-drop)
> - `_docs_/_checkpoints_/20260917_001_checkpoint.md`
> - `LESSONS_LEARNED.md` §74 (итоговый урок)

Дата: 2026-09-25

## Цель

`db-pm deploy reset` (Phase 18) падал на живом GP-таргете cis_zup_gp_dev
(stop-on-error при content-drop). Найти корневую причину и починить генерацию
routine-DROP в postgres-адаптере.

## Симптом

- Попытка 1 (`make reset-target` в репозитории-потребителе lm_hrdo_2): сброс
  прерван на объекте №139.
- Попытка 2: объект №1 — счётчик сместился (138 объектов уже удалены первым
  прогоном); оба падения — один и тот же объект, alphabetically первый routine
  схемы cis_dmt_zup (`fun_lu_orbit_constructor_zon_del`, zero-arg).

## Гипотезы (хронология)

1. **CASCADE в DROP FUNCTION нелегален на ядрах < PG 13** — первичная,
   опровергнута живым прогоном: убрали CASCADE — ошибка сохранилась и
   сместилась на следующий токен (`syntax error at or near ";"`).
   Инсайт: «syntax error at or near X» называет токен, на котором парсер
   споткнулся, а не виновника; X сразу после имени функции = парсер ждал `(`.
   Смещение X после правки (CASCADE → `;`) — маркер нетронутой причины.
2. **Отсутствие списка аргументов** — подтверждена: ядра < PG 10 (GP 6 = 9.4)
   требуют `name(arg_list)` в грамматике DROP FUNCTION/PROCEDURE/AGGREGATE
   даже пустым; PG 10+ скобки опускает молча, маскируя баг на новых таргетах.
   `pg_get_function_identity_arguments` для zero-arg routine возвращает `''`,
   код рендерил ident без скобок (`if args:`).

## Решения

- D1: routines в `drop_schema_contents` ВСЕГДА рендерят аргументы — `()` для
  zero-arg; валидно на всех ядрах, включая PG 13+.
- D2: CASCADE для routines сохранён (легален на любом ядре, когда скобки на
  месте); заодно закрывает остаточный риск aggregate↔support-функция в одной
  схеме (CASCADE уводит agg, IF EXISTS гасит miss).
- D3: временный версионный гейт по `server_version_num`
  (`_supports_routine_drop_cascade`, введён по гипотезе 1) удалить после
  подтверждения гипотезы 2 — не оставлять код на опровергнутой предпосылке.

## Проверки

- Юнит: генерация DDL адаптера через fake-connection — zero-arg routines
  (`"s"."f"() CASCADE`), routines с identity-списком, реляционные объекты.
- Живая: повторный `deploy reset` на cis_zup_gp_dev → `deploy plan` →
  `deploy apply` (остаток приёмки Phase 18 из чекпойнта 20260917_001).

## Риски

- Кластер отвергнет `name() CASCADE` (не ожидается — грамматика ядра 9.4):
  возврат no-CASCADE для routines одной строкой.
