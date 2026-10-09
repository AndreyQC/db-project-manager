# Сигнатуры REMOVED-рутин в ObjectSnapshot: DROP по настоящей перегрузке (plan)

> **Date:** 2026-10-09
> **Status:** backlog (уровень 2 к задаче 20261009_002)

> Контекст:
> - `_tasks_/2026-10-09/20261009_002_drop_function_gp_parens_result.md` —
>   минимальный фикс (фолбэк `()`) и его ограничение (тихий no-op).
> - `src/db_project_manager/domain/diff.py:77` — `ObjectSnapshot`.
> - `src/db_project_manager/infrastructure/diff/snapshot.py` — построение
>   снапшотов; `src/db_project_manager/application/delta_service.py` —
>   `_drop_statement`.
> - LESSONS §26 (сигнатура в идентичности), §27 (канонизация).

## Проблема

После фикса 20261009_002 DROP REMOVED-функции валиден на GP 6, но для функций
**с аргументами** остаётся тихим no-op:

1. REMOVED-объект отсутствует в кодовой базе → `vertex is None` →
   `_drop_statement` рендерит фолбэк `()`.
2. `DROP FUNCTION IF EXISTS "s"."f"()` для функции `f(integer)` даёт
   «function does not exist», который `IF EXISTS` молча проглатывает.
3. Функция не удаляется, а план/apply считают операцию выполненной.

Данные для правильного стейтмента существуют — DB-сторона compare строится
через RE, и в графе RE у вершины есть `argument_types`
(`pg_get_function_identity_arguments`-совместимый список из
`queries.py:GET_FUNCTIONS/GET_PROCEDURES`). Но при сборке
`ObjectSnapshot` поле выбрасывается: хранится только `object_signature`
(8-hex хэш), сырой список — нет.

## Решение

1. **Модель**: аддитивное поле `ObjectSnapshot.argument_types: str = ""`
   (raw-список, как в `Vertex`; старые JSON-снапшоты парсятся без изменений).
2. **snapshot.py**: при построении снапшота копировать
   `vertex.argument_types` в объект (обе стороны — DIR и DB).
3. **delta_service**:
   - `_classify_entry`: переносить `argument_types` из снапшота в
     `PlannedOperation` (новое аддитивное поле, дефолт `""`);
   - `_drop_statement`: приоритет источников — `PlannedOperation.argument_types`
     (целевой снапшот) → `vertex.argument_types` (кодовая база) → `()`.
4. **Fail-safe вместо тихого no-op**: если у REMOVED-рутины непустой
   `object_signature` (значит, аргументы были), а список недоступен —
   понижать операцию в BLOCKED с причиной «сигнатура неизвестна — DROP
   вручную» (по образцу ALT-6), а не рендерить заведомо пустой `()`.

## Проверки

- Юнит: REMOVED-функция `f(integer, text)` → артефакт
  `DROP FUNCTION IF EXISTS "s"."f"(integer, text);` (список из target-снапшота);
  перегрузки — отдельные entry → отдельные DROP с разными сигнатурами.
- Юнит fail-safe: `object_signature` непустой, `argument_types` пуст →
  BLOCKED-комментарий, не исполняемый `()`.
- Roundtrip: `StateSnapshot` → JSON → парсинг (старые файлы без поля).
- Прогон на реальном корпусе (cis_zup) руками у пользователя.

## Риски / заметки

- `argument_types` из RE может содержать модификаторы (`numeric(10,2)`) —
  для `DROP` это валидно (identity-аргументы), split не нужен: строка уже
  готовый список. НЕ переиспользовать `canonical_signature` — он для
  хэширования, не для рендера DDL.
- Поле попадёт в `diff_report.json`/`plan.json` — размер артефактов чуть
  вырастет; приемлемо.
- Граница: `object_signature` хэш ≠ гарантия аргументов (у безаргументной
  перегрузки хэш пуст) — условие fail-safe п.4 смотреть именно по
  `object_signature != ""`.
