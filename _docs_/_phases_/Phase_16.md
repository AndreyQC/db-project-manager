# Phase 16: Greenplum tuning

> **Дата:** 2026-09-09 — 2026-09-15
> **Статус:** завершена (ветка dev; в main не влита)
> **План/журнал:** `_tasks_/phase_16/Phase_16_plan.md`
> **Результат:** `_tasks_/phase_16/Phase_16_result.md`

## Цель фазы

Все команды `db-pm` (`reverse-engineer`, `graph`, `deploy validate/analyze/plan/apply`,
`compare run`, `yaml generate/apply`) работают на Greenplum-кластере без
PG-only конструкций; поведение на PostgreSQL не меняется — каждый фикс
верифицировался на обеих СУБД. Источник замечаний — живые прогоны пользователя
на `cis_zup_gp_dev` (GP 6.19.4, ядро PG 9.4.26).

## Что сделано

Каталог-слой (`infrastructure/database/postgres/`):

| Файл | Что изменилось |
|------|----------------|
| `queries.py` | `GET_INDEXES` без `array_position` (PG 9.5+); пара `GET_FUNCTIONS_{POSTGRES,GREENPLUM}` + `PROKIND_PROBE`; `GET_TABLE_GP_OPTIONS` (gp_distribution_policy + reloptions, 1 запрос на схему) |
| `adapter.py` | кэшированные capability-пробы (pg_sequence, prokind); `GP_ADMIN_SCHEMAS` исключаются из `_get_schemas()` только на GP; `_get_gp_table_options` (distribution + storage); serial-детект в `_build_table` (serial4/8 по дефолтному имени nextval) |

Рендер и сравнение:

| Файл | Что изменилось |
|------|----------------|
| `sql/sql_generator.py` + `templates/table.sql.j2` | `gp_tail_sql` — WITH/DISTRIBUTED после закрывающей скобки |
| `diff/normalize_sql.py` | GP-tail вырезается до парса и канонизируется (порядок/регистр опций, appendonly≡appendoptimized); правила: bool/boolean, timezone(...)≡AT TIME ZONE (+скобки), дефолтный VOLATILE, кавычки lowercase-идентификаторов, избыточный список колонок view, case-only алиасы; `hash_normalized` (без повторного парса); Command→regex-путь |
| `diff/columns.py` | `strip_gp_tail` перед собственным parse; `numeric(9,0)`≡`numeric(9)` |
| `diff/snapshot.py` | хеш уже-нормализованной строки (была двойная нормализация — 1176 предупреждений за прогон) |
| `application/compare_service.py` | `_exclude_service_schema` (`__deploy`, +WARNING при отсутствии на стороне) и `_exclude_serial_sequences` (pg_dump-модель); summary-ключи `ignored_service_schema`/`ignored_serial_sequences` |

Identity и CLI:

| Файл | Что изменилось |
|------|----------------|
| `parsing/function_args.py` (новый) + `parsing/pg_sql_parser.py` | реконструкция `/signature/<hash>` из SQL-тела функции, когда autodoc старой генерации его не несёт (снял расщепление 67 функций на added+removed) |
| `presentation/cli/main.py` | отклонённые `deploy plan`/`apply` печатают путь к run-каталогу артефактов |

Прочее: каскад db_type для `yaml generate/apply` (манифест → флаг);
README `uv sync --system-certs`.

## Ключевые решения

- GP-only знание (админ-схемы, gp-каталоги, свёртки) живёт за
  `_is_greenplum`/capability-пробами — PG-путь текстуально не меняется
  (LESSONS §70-4, §71-3).
- Системные/сервисные схемы не сравниваются вовсе: `gp_toolkit` (16.5),
  `__deploy` (16.8) — canonical-сид по построению не hash-совпадает с
  каталог-рендером; «unchanged→skip» сервис-объектов нельзя строить на
  hash-равенстве (§73-1).
- Канонизации — конечный перечень пар «каталог ↔ рукописный DDL»,
  подтверждённых построчным сравнением живых пар (§72-4).
- serial: pg_dump-модель — колонка сворачивается, последовательность не
  диффится как отдельный объект (16.10).

## Проверки

```bash
.venv/Scripts/python.exe -m pytest tests/unit/ -q -p no:randomly   # 1036 passed
.venv/Scripts/ruff.exe check src/ tests/                           # All checks passed
```

Живая верификация (GP `cis_zup_gp_dev` + PG 18.6 `cis_zup_dev`):
хеш-сравнение RE↔кодовая база — таблицы 193/196 (было ~0); `deploy plan` —
blocked 0 (с `--include-drops` и без, после ANALYZE пустых таблиц);
предупреждения sqlglot за прогон — 1176 → 0.

## Известные ограничения / NOT done

- 9 view «CHANGED» — рендер выражений каталогом GP6 (развёрнутые CAST-формы
  арифметики дат) — канонизация отложена до замечания (BACKLOG).
- 4 функции «CHANGED» — qualify-refs квалифицирует имена в комментариях тел
  (`$$`-строки) — кандидат на фикс qualify (BACKLOG).
- Post-deploy ANALYZE / подсказка «presence unknown → ANALYZE» в
  safety-отчёте (BACKLOG; свежие GP-таблицы без статистики fail-safe-блокируют
  свои будущие дропы — разбиралось в 16.11).
- `codebase doctor` (проверка/дозаполнение autodoc старой генерации) —
  P2 BACKLOG.
- Integration-тесты (Docker) не гонялись — замена живыми прогонами (решение
  пользователя).

## Где читать дальше

1. `_tasks_/phase_16/Phase_16_plan.md` §4 — журнал шагов с диагнозами.
2. `_tasks_/phase_16/Phase_16_result.md` — коммиты и верификация по шагам.
3. `LESSONS_LEARNED.md` §70–§73.
4. `_checkpoints_/20260915_001_checkpoint.md` — текущее состояние.
5. `_tasks_/ROADMAP.md` §2 — далее Phase 17 (post-deploy отчёты, CD-16..19).
