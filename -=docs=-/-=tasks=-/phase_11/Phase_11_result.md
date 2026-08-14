# Phase 11: Safety Gate — результат

> **Дата:** 2026-08-14
> **Ветка:** dev
> **Статус:** завершена (все шаги S1..S9 плана выполнены)
>
> Контекст:
> - `-=tasks=-/phase_11/Phase_11_vision_final.md` — нормативный дизайн (SG-0..SG-7, SG-M)
> - `-=tasks=-/phase_11/Phase_11_plan.md` — пошаговый план
> - `-=CHECKPOINTS=-/20260814_001_checkpoint.md` — состояние до фазы (Phase 10 done)

---

## Что сделано

Пользовательский результат — **`db-pm deploy analyze`** (CLI) + **GUI-действие**
(5-е в реестре Phase 7): dry-run safety-gate против **существующей** целевой БД с
данными (первый real-target путь в проекте). Read-only: строит дельту код↔БД
(переиспользован `CompareService`), оценивает наличие данных в тронутых таблицах
(CHANGED/REMOVED) через нормализованный контракт адаптера, сопоставляет с покрытием
pre-скриптов (`project.covers` в autodoc), пишет `safety_gate_report.{md,json}` и
возвращает exit code 0/1/2. Ничего к БД не применяется; pre-скрипты читаются статически.

| Шаг | Коммит | Что |
|-----|--------|-----|
| S1 | `35fc2e7` | `domain/safety.py`: StatsConfidence/TablePresenceStats/DataPresence/TouchedTable/Verdict + `classify_presence` (fail-safe) + `check_version_relation`; 27 тестов |
| S2 | `d2945d2` | `DatabaseAdapter.get_table_presence_stats` (abstract, возвращает domain-модель) + PG: `GET_TABLE_PRESENCE_STATS` (pg_class⋈pg_stat_user_tables) + чистый `map_presence_row`; ВСЕ fakes в одном коммите (§45) |
| S3 | `46d45b8` | `infrastructure/deploy/pre_coverage.py`: парсер `project.covers` (битые записи warn+skip, §36 barewords) |
| S4 | `423f4a0` | `infrastructure/deploy/safety_report.py`: md + json рендер вердикта |
| S5 | `82e3c5a` | `application/safety_gate_service.py`: connect → manifest(db_type fail-fast) → version-check → CompareService → touched → presence (service_schema исключён) → coverage → verdict → отчёт; read-only контракт тестом |
| S6 | `87077aa` | CLI `deploy analyze` (`--dir/--target-connection-file/--output-dir`), exit 0/1/2 |
| S7 | `801ae8a` | GUI: `DeployAnalyzeSettings` + `ActionSpec` + диалог (кнопки последними, §43) + `DeployAnalyzeWorker` (§42 strong-ref) + verdict-отчёт в MainWindow |
| S8 | `9add4ef` | Integration e2e (testcontainers, 5 сценариев CD-6..CD-10 + SG-6) |
| S8-fix | `8ed60ab` | Ремонт ДО-Phase-11 поломки integration-фикстуры (schema-файлы + самодостаточный pre-script) |
| S9 | `1b20480` | Регрессия: objects_total 17→19 после schema-файлов фикстуры |

## Отклонения от плана

1. **Поле `schema` → `object_schema`** в `TablePresenceStats`/`TouchedTable` (S1):
   pydantic-предупреждение о затенении `BaseModel.schema` — переименовано по конвенции
   проекта (`Vertex`, `ObjectSnapshot`) и уроку §40.
2. **Ремонт фикстуры `codebase_sample`** (не было в плане): 10 из 16 integration-тестов
   падали ДО Phase 11 (проверено worktree-бисекцией на `5667eda`): (a) pre-скрипт фикстуры
   не был самодостаточен (CDF-1: создавал таблицу в несуществующей схеме); (b) в ручной
   фикстуре отсутствовали `<schema>/schema <name>.sql`, которые реальный RE всегда
   генерирует → sequence деплоился раньше схемы. Добавлены schema-файлы `bookings`/`app`,
   pre-скрипт дополнен `CREATE SCHEMA IF NOT EXISTS`.
3. **Комментарий кода** `map_presence_row` живёт module-level в adapter.py (не приватный
   `_map_`), чтобы unit-тестировать без БД — соответствует плану.

## Известные ограничения / NOT done

- **Real-target apply** (применение безопасных операций + выполнение pre-скриптов на
  живой БД) — Phase 12.
- **Структурный column-diff** (тип изменения add/drop/alter в отчёте) — Phase 12 (CD-ALT-1);
  отчёт Phase 11 показывает табличный статус changed/removed.
- **Рендер отчёта в GUI-окне/вкладка Delta Viewer** — Phase 14; GUI показывает verdict
  и путь к `safety_gate_report.md`.
- **2 до-Phase-11 integration-падения** (RE→deploy на «чистой» PG: `database settings.sql`
  только с комментариями → `can't execute an empty query`) — не связаны с Phase 11,
  заведены в BACKLOG (P1).
- **Greenplum-валидация** распределённых таблиц в presence-stats — при появлении кластера.
- Flaky crypto-тест (BACKLOG P3) — как был, в изоляции зелёный.

## Проверки

```bash
unset SSL_CERT_FILE REQUESTS_CA_BUNDLE CURL_CA_BUNDLE && uv run pytest tests/unit/ -q
# 687 passed (baseline 609 + 78 Phase 11; flaky crypto в изоляции зелёный)
uv run ruff check src/ tests/
# All checks passed!
uv run pytest -m integration
# 14 passed, 2 failed — оба ДО-Phase-11 (BACKLOG P1, database_setting empty query);
# Phase 11 e2e: 5/5 зелёные
```

Новые тесты: `test_safety_domain.py` (+27), `test_pg_presence_mapping.py` (+9),
`test_pre_coverage.py` (+9), `test_safety_report.py` (+6), `test_safety_gate_service.py`
(+14), `test_deploy_analyze_cli.py` (+5), GUI: расширенные `test_action_cli.py` (+3),
`test_action_panel_smoke.py` (+5), `test_action_registry.py` (обновлён);
`tests/integration/test_deploy_analyze_e2e.py` (+5).

## Чеклист по урокам (final §7) — закрыт

- §3 reltuples без COUNT ✓ (SG-4/запрос S2) · §12 `git add --` ✓ · §18/§45 fakes одним
  коммитом ✓ (S2) · §19 whitelist системных схем ✓ · §23 parse autodoc pre-скриптов ✓ (S3)
- §28 pydantic-roundtrip в json-отчёте ✓ (S4) · §31 mkdir перед записью ✓ ·
  §34/§35 `pg_catalog.`-qualified ✓ (S2)
- §39 CliRunner + shlex posix=False ✓ (S7 контракт) · §40 без коллизий имён ✓ (S1 отклонение
  №1) · §41 offscreen smoke ✓ (S7) · §42 worker strong-ref ✓ (S7) · §43 кнопки последними ✓ (S7)
- §46 обязательные опции первыми ✓ (S6) · TASK_CONVENTIONS §6 код/доки раздельно ✓

## Новые уроки

- §47 (хардкод индексов combo + модальные диалоги в headless — зависание тестов).
- §48 (version-gate: версию цели писать после создания кодовой базы — RE синхронизирует
  source_version).

## Где читать дальше

- `-=PHASES=-/Phase_11.md` — свод фазы
- `-=tasks=-/phase_11/Phase_11_vision_final.md` — решения SG-0..SG-7/SG-M
- `-=tasks=-/ROADMAP.md` §2 (шаг 4 — Phase 12 следующая)
- BACKLOG P1 «database_setting empty query» — до-Phase-11 integration-долг
