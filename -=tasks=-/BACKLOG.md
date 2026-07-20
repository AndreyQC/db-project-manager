# Backlog — открытые задачи

> Назначение: единое место для задач, не вошедших в завершённые фазы. Каждая запись —
> кандидат в будущую фазу (Phase 5+). Пополняется при закрытии фаз (checkpoint) и по
> мере обнаружения проблем в реальных прогонах.
>
> Связанные документы:
> - `-=CHECKPOINTS=-/20260720_001_checkpoint.md` — последний checkpoint (Phase 4 done)
> - `-=tasks=-/phase_00/003_roadmap_migration.md` — исходный roadmap (частью устарел:
>   Phase 3 в нём описана как «Миграции», фактически сделана как SSH-туннель)
>
> Дата заведения: 2026-07-20

---

## Приоритеты

- **P1 — блокирует пользовательские сценарии.** Без этого инструмент не решает
  основную задачу для реальной БД.
- **P2 — заметное ухудшение UX/читаемости.** Работает, но раздражает.
- **P3 — качество/будущие возможности.**

---

## P1. Поддержка PostgreSQL extensions (end-to-end)

**Симптом:** `db-pm deploy validate` падает на таблице с колонкой типа `citext`:
```
тип "citext" не существует
LINE 13:     "public_email" citext NULL,
```

**Корневая причина:** reverse-engineer тянет тип колонки как сырой `udt_name`
(`information_schema.columns`) и кладёт в DDL как есть. При `deploy validate`
создаётся **пустая** временная БД без `CREATE EXTENSION citext` → тип не определён.

**Текущее состояние support extensions (все слои, кроме шаблона, оборваны):**

| Этап | Поддержка |
|------|-----------|
| `templates/extension.sql.j2` | ✅ есть, синтаксис обновлён в Phase 2 |
| Reverse-engineer (запросы к `pg_extension`) | ❌ нет |
| Структура БД (`get_database_structure`) | ❌ нет ключа `extensions` |
| SQLGenerator (рендер) | ❌ шаблон не вызывается |
| Парсер (`SUPPORTED_TYPES`) | ❌ тип не распознаётся |
| Топосорт (`TYPE_PRIORITIES`) | ❌ нет `extension` (попадёт в UNKNOWN=100) |
| Deploy (pre-step) | ❌ нет |

**Целевой охват (по решению пользователя — «Полный цикл»):**

1. **Reverse-engineer:** запросы к `pg_extension` + `pg_available_extensions`;
   агрегация extension-ов на верхнем уровне `structure` (не внутри schema — extensions
   глобальны, хотя могут иметь `SCHEMA <name>`).
2. **Структура:** `structure["extensions"]` = список `{name, schema, version, cascade, comment}`.
3. **SQLGenerator:** рендер `extension.sql.j2` в `<output>/extensions/extension <name>.sql`
   (вне schema-директорий — extensions не привязаны к schema).
4. **Парсер:** `extension` в `SUPPORTED_TYPES` + `("extension",)` в `_CREATE_KEYWORD_TO_TYPE`.
5. **Топосорт:** `extension` в `TYPE_PRIORITIES` с **приоритетом −1** (раньше `schema`=0),
   т.к. schema может зависеть от extension (например, extension создан в конкретной schema).
6. **Deploy:** extensions деплоятся первыми автоматически (через топосорт).
7. **Граф зависимостей:** ребро `DEPENDS_ON` от таблицы/функции к extension, если объект
   использует тип из этого extension (обнаружение через `pg_type.typtype='e'` или
   `typnamespace` принадлежности extension; для колонок — `atttypid` → `pg_type`).

**Кандидат на Phase 5.** Энд-ту-энд задача, задевает 6 подсистем.

**Документ-источник:** `-=CHECKPOINTS=-/20260720_001_checkpoint.md` (Known gaps).

---

## P2. Разрешение перегруженных вызовов в edge detection

**Контекст:** Phase 4 зафиксировала MVP-ограничение — `_build_names_index`
(`pg_sql_parser.py:212-224`) индексирует объекты по голому `object_name` через
`setdefault`. При перегрузках вызовы в SQL создают рёбра к «первой попавшейся» вершине.

**Цель:** по аргументам вызова функции (`sp_x(123)` → `int4`) определять, к какой
именно перегрузке вести ребро.

**Сложность:** полноценный type inference — нужно определять типы выражений-аргументов
(литералы, колонки, результаты других вызовов). Это объёмная задача.

**MVP-вариант:** покрывать только простые случаи — литералы и прямые ссылки на колонки
с известным типом. Для неразрешимых вызовов — ребро к первой перегрузке (как сейчас),
с warning в лог.

**Кандидат на Phase 6+** (после extensions и миграций). Зафиксировано в
`Phase_4_vision_final.md` §6 (Q3).

---

## P2. Интеграционный тест reverse→graph→deploy на реальной PostgreSQL с перегрузками

**Контекст:** Phase 4 покрыта только синтетическими фикстурами
(`sample_structure.json`, `codebase_sample/app/functions/`). Нет проверки на живой БД.

**Цель:** через testcontainers (harness уже есть из Phase 2) поднять PG, создать
перегруженные функции, прогнать `reverse-engineer → graph build → deploy validate`
на чистой временной БД. Контроль: обе перегрузки деплоятся.

**Кандидат на Phase 5** (вместе с extensions — они тоже потребуют real-DB проверки).

---

## P3. Устаревший roadmap `phase_00/003_roadmap_migration.md`

**Проблема:** §10 «Фазы реализации» описывает Phase 3 как «Миграции и расширение СУБД»,
но фактически Phase 3 сделана как SSH-туннель (`-=tasks=-/phase_03/`). Фразы «Фаза 3 —
MSSQL и MySQL адаптеры» и «Фаза 2 — Snowflake» рассинхронизированы с реальностью.

**Действие:** либо переписать §10 под фактические фазы (1=фундамент, 2=граф+deploy,
3=SSH, 4=перегрузки), либо пометить раздел устаревшим со ссылкой на этот беклог и
каталоги `phase_NN/` как источник правды.

**Кандидат на doc-cleanup коммит** (не блокирует ничего).

---

## P3. Flaky env-тест `test_crypto_util::test_decrypt_nested_dict`

**Симптом:** периодически падает в полном прогоне, в изоляции зелёный.

**Причина:** пересечение `monkeypatch.setenv(TEST_CRYPTO_ENV, ...)` между
`test_crypto_util` и `test_connection_store` (описано в `LESSONS_LEARNED.md` §13, §22).

**Действие:** полностью изолировать env-зависимые тесты (свой ключ, своя переменная,
явная teardown), либо перевести на `monkeypatch.context()` / session-scoped fixture.

**Не критично, но влияет на доверие к CI.**
