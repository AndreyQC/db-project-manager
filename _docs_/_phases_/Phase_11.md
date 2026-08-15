# Phase 11: Safety Gate (`deploy analyze`)

> **Дата:** 2026-08-14
> **Статус:** завершена
> **План/дизайн:** `_tasks_/phase_11/Phase_11_vision_final.md` (SG-0..SG-7, SG-M)
> **Результат:** `_tasks_/phase_11/Phase_11_result.md`
> **Коммиты:** `35fc2e7`…`1b20480` (10 код-коммитов, S1..S9)

---

## Цель фазы

1. **CD-6** — pre-analysis: из дельты код↔БД выделить тронутые таблицы (табличная
   гранулярность; column-diff — Phase 12).
2. **CD-7** — оценка наличия данных через метаданные (`reltuples`, без `COUNT`);
   stale/unknown → «есть данные» (fail-safe).
3. **CD-8** — сопоставление тронутых таблиц с данными с pre-скриптами (`project.covers`).
4. **CD-9** — при нарушении: отчёт-рекомендация (md+json) + non-zero exit; пайплайн стоит.
5. **CD-10** — жёсткое правило «таблицы с данными без pre-скрипта = блок»; новых объектов,
   пустых таблиц и не-табличных объектов не касается.
6. Первый real-target путь: подключение к существующей БД (read-only).

## Что сделано

| Слой | Файл | Что изменилось |
|------|------|----------------|
| Domain | `src/db_project_manager/domain/safety.py` (новый) | `StatsConfidence`, `TablePresenceStats` (нормализованный сигнал адаптера), `DataPresence` + `classify_presence` (fail-safe), `TouchedTable`, `SafetyGateVerdict`, `check_version_relation` (SG-6) |
| Adapter | `infrastructure/database/base.py`, `postgres/{adapter,queries}.py` | abstract `get_table_presence_stats -> list[TablePresenceStats]` (14-й метод контракта); PG: `GET_TABLE_PRESENCE_STATS` (`pg_class` ⋈ `pg_stat_user_tables`) + чистый `map_presence_row` (never-analyzed/drift/−1 → STALE) |
| Infra | `infrastructure/deploy/pre_coverage.py` (новый) | парсер `project.covers` из autodoc pre-скриптов; битые записи warn+skip |
| Infra | `infrastructure/deploy/safety_report.py` (новый) | `safety_gate_report.{md,json}`: таблица тронутых, секция нарушений с рекомендацией |
| Application | `application/safety_gate_service.py` (новый) | `analyze()`: manifest (db_type fail-fast, SG-M) → version-check → CompareService (дельта) → touched → presence (service_schema исключён) → coverage → verdict → отчёт; read-only |
| CLI | `presentation/cli/main.py` | `db-pm deploy analyze --dir --target-connection-file --output-dir`; exit 0/1/2 |
| GUI | `presentation/gui/actions/{models,registry,cli,dialogs}.py`, `widgets/workers.py`, `main_window.py` | 5-е действие `deploy_analyze`: диалог, CLI-билдер, фоновый worker (§42), verdict CLEAN/VIOLATIONS + отчёт |

## Ключевые решения

- **SG-0/SG-A:** dry-run `deploy analyze`; табличный уровень gating (column-diff → Phase 12).
  Для блокировки достаточно «тронута ∧ есть данные ∧ нет pre-скрипта».
- **SG-3:** покрытие — явное объявление `project.covers` в autodoc pre-скрипта
  (явность > магия; gate не проверяет семантику, только факт объявления).
- **SG-4/SG-5:** БД-агностичная классификация по нормализованной модели
  (`estimated_rows` + `confidence: FRESH/STALE/UNKNOWN`); DB-specific знание — в адаптере;
  адаптер без сигнала свежести → UNKNOWN → fail-safe (SG-M: будущие Snowflake/MSSQL/MySQL
  безопасны по умолчанию).
- **SG-7:** GUI-действие run-only (по образцу `deploy_validate`); рендер отчёта — Phase 14.
- Дельта — через существующий `CompareService` (никакого своего diff); отчёт compare
  (`diff_report.json`) — сопутствующий артефакт.

## Проверки

- Unit: **687 passed** (baseline 609 + 78 Phase 11); ruff чистый.
- Integration (testcontainers): **Phase 11 e2e 5/5** (violation/covers/empty/added/version);
  полный набор: 14 passed, 2 failed — оба **до-Phase-11** (BACKLOG P1:
  `database_setting` empty query), починены остальные 8 до-Phase-11 падений фикстурой
  (`8ed60ab`: schema-файлы + самодостаточный pre-script).
- Read-only контракт: тесты фиксируют отсутствие вызовов мутирующих методов адаптера.

## Известные ограничения / NOT done

- Real-target apply + выполнение pre-скриптов на живой БД — Phase 12.
- Структурный column-diff (тип изменения в отчёте) — Phase 12 (CD-ALT-1).
- Повторный анализ после pre-скриптов (CD-11) — Phase 12.
- Рендер отчёта в GUI/вкладка Delta Viewer — Phase 14.
- Greenplum-валидация распределённых таблиц; flaky crypto-тест (BACKLOG P3).

## Где читать дальше

- `_tasks_/phase_11/Phase_11_vision_final.md` — норматив (решения)
- `_tasks_/phase_11/Phase_11_result.md` — результат (коммиты, отклонения)
- `_tasks_/ROADMAP.md` §2 шаг 4 — **Phase 12 (ALTER + Delta)** следующая
- `LESSONS_LEARNED.md` §47, §48 (Phase 11), §40-§43 (GUI, применены)
