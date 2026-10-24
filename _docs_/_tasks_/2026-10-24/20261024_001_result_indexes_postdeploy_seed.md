# Индекс как объект деплоя + постдеплойный засев (result)

> **Date:** 2026-10-24
> **Status:** выполнена
> **План:** `20261024_001_plan_indexes_postdeploy_seed.md`
> **Коммиты db-pm:** `d93af9b` (план), `2be7bf5` (этап 1), `4413da3` (identity),
> `c1237d7` (mcp entry point), `56a651d` (delta: empty schemas + index),
> `504fb4b` (compare qualify_refs)
> **Коммиты qr-проекта:** `70cd894` (identity + граф), `9fdeccf` (сиды + README)

---

## Что сделано

### Этап 1 — индекс как объект деплоя (`2be7bf5`)

- Парсер: `index` в `SUPPORTED_TYPES`, разбор `CREATE [UNIQUE] INDEX`
  (autodoc-путь + token-fallback), ребро `index → table`
  (`DEPENDS_ON`, action `index on table`).
- RE: `GET_SCHEMA_INDEXES` (`pg_get_indexdef`, constraint-backed исключены),
  `_get_indexes`, `SQLGenerator` kind `indexes`, шаблон `index.sql.j2`
  → `<schema>/indexes/index <name>.sql`.
- MCP: `OBJECT_TYPE_KEYS += index`; CH-адаптер отдаёт `indexes: []` по контракту.
- Автогенерация индексов под FK — отклонена (решение пользователя).

### Этап 2 — приёмка на qr-проекте

- 123 ручных индексных файла парсятся без правок (autodoc канонический).
- Граф: 465 → 588 вершин (+123 `index`), 1579 → 1702 рёбер;
  `graph validate` без циклов.

### Этап 3 — постдеплойный засев (`4413da3`, `70cd894`, `9fdeccf`)

- `__migrations/post/2026-10-24_001_seed_reference_data.sql` (справочники,
  2 демо-юзера, 3 мемориала) и `..._002_seed_ru_geography.sql`
  (84 региона, 1118 городов); идемпотентность источников сохранена
  (`ON CONFLICT` / `NOT EXISTS`), снят `SET search_path`.
- `__migrations/README.md` — конвенции pre/post/seed (в т.ч. ответ
  «бэкап таблицы перед сменой типа колонки» → `pre/`).
- **Попутный фикс RE (критический):** identity-колонки терялись
  (`GENERATED ALWAYS AS IDENTITY` → голый `int8 NOT NULL`); сид падал
  `NotNullViolation` на `memorials.memorial_number`. Фикс: `GET_COLUMNS`
  + `is_identity/identity_generation`, рендер в `table.sql.j2`,
  identity-последовательности (`deptype 'i'`) исключены из выгрузки.
  В qr-кодовой базе: 4 таблицы исправлены, 4 identity-sequence файла удалены.

### Этап 4 — deploy apply через MCP в чистую БД (`c1237d7`, `56a651d`, `504fb4b`)

Целевая БД: `qr_apply_20261024_165050` (timestamp-постфикс, чистая).

Попутные фиксы db-pm (все найдены живым прогоном):

1. `db-pm-mcp` entry point `main:main` → `main:app` — CLI-аргументы
   (`--connections-dir`) игнорировались.
2. RE рендерит пустые схемы — без этого дельта на частично-задеплоенном
   таргете пересоздаёт схемы (`DuplicateSchema`), применить повторно
   было нельзя.
3. `DIFFED_TYPES += index` — дельта-путь (apply) не создавал индексы вовсе
   (validate-путь создавал).
4. `CompareService` рендерит DB-снапшоты через `build_default_service()`
   (с qualify_refs) — иначе 150 ложных CHANGED функций (разница только
   в квалификации вызовов).

Итоговая верификация `qr_apply_20261024_165050`:

| Проверка | Результат |
|---|---|
| MCP `deploy_apply` (rehearsal=false) | status ok |
| Объекты | 87 таблиц, 316 функций, 7 схем, **123 автономных индекса** |
| Сиды (post-скрипты) | 1118 городов, 84 региона, 2 юзера, 3 мемориала (identity №1–3) |
| `__deploy.script_history` | 2 записи post, success |
| `__deploy.schema_version` | `2026.10.23.01` (source `apply`) |
| Повторный apply | planned=533, applied=0 (идемпотентен) |
| compare vs `qr_memory_dev` | **added=0, removed=0, changed=0, unchanged=533** |

## Проверки

- [x] `uv run pytest` — 1320 unit зелёные (+18 новых за задачу), ruff clean.
- [x] Живой RE round-trip на `qr_memory_dev`: 123 индекса → 123 файла →
      123 вершины → 123 ребра.
- [x] `graph-validate` qr-проекта без циклов.
- [x] MCP `deploy_apply` в чистую БД — полная схема + сиды + индексы.
- [x] Идемпотентность повторного apply.
- [x] compare DB-vs-DB: 0 расхождений на 533 объектах.

## Известные ограничения / backlog (найдено в этой задаче)

1. **Serial-последовательности**: standalone sequence-файл + serial-колонка
   дают дубль при validate-деплое (`bid_statuses_id_seq` + `bid_statuses_id_seq1`
   с переименованным дефолтом). Нужен аналог serial-folding в граф-пути
   (validate) или отказ от выгрузки owned-последовательностей.
2. **`normalize_sql` не хэширует хвостовые ALTER TABLE** — FK вне CREATE TABLE
   невидимы для compare (два взаимных FK qr-проекта не видны в diff).
3. **Extensions не в дельте** — apply в чистую БД требует предварительной
   установки расширений (`CREATE EXTENSION` вручную; сделано для qr_apply:
   plpgsql, citext, pgcrypto).
4. **PG pre-flight журнала**: `ensure_deploy_tables` — только CH (Phase 6.1);
   для PG перед первым apply нужен `deploy init-service-schema`
   (симметричный авто-bootstrap — кандидат).
5. **Репетиция на пустом таргете** отклоняется: RE пустой БД штампует свежий
   calver → forward-only reject. Обход: `rehearsal=false` для чистых БД.
6. Из задачи 20261023_588 (не здесь): 17 триггеров `build: false`
   (trigger раньше function), 2 взаимных FK, гранты.

## Связанные документы

- План: `20261024_001_plan_indexes_postdeploy_seed.md`
- qr-проект: `_docs_/_tasks_/2026-10-26/20261023_588_dbpm_constraint_index_support_plan.md`
- Отчёт сравнения: qr-репо `deploy_artefact/compare_20261024/chilling-crimson-humble-serpent-57/`
- Ранее: `20261017_001_checkpoint.md` (Phase 6.2 CHProfiler)
