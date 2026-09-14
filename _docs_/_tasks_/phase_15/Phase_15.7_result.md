# Phase 15.7 (result): run-каталоги + target-snapshot + build=false + финальные PG-эквивалентности

> **Дата:** 2026-09-08
> **Статус:** завершена (код + тесты; не закоммичена — ждёт решения о коммите)
> **План:** `_docs_/_tasks_/phase_15/Phase_15.7_draft.md`
> **Коммиты:** ещё не созданы (изменения в рабочем дереве)

## 1. Цель

1. Отчётные команды пишут артефакты в уникальный run-подкаталог с
   человекочитаемым timeline-именем; DB-side RE-snapshot сохраняется вместо
   удаления (BACKLOG P3 «keep-target-dir»).
2. `project.build: false` уважается в compare/safety gate (объекты «вне деплоя»
   не должны попадать в diff).
3. Закрыть оставшиеся false-positive CHANGED для таблиц cis_zup
   (`zup_process_log`, `zup_api_sourcedata_load_log`) и системных `__deploy`.

## 2. Что сделано

| Файл | Изменение |
|------|-----------|
| `src/db_project_manager/infrastructure/files/run_naming.py` | Новый модуль: `TimelineNameGenerator` (адаптация кода пользователя) + `create_run_dir` / `decode_run_dir_name` / `latest_run_dir` / `resolve_report_dir` |
| `src/db_project_manager/application/compare_service.py` | `_exclude_build_false` (исключение build=false из обеих сторон + рёбра + счётчик `ignored_build_false`); `_copy_snapshot_dir` — копия DB-side RE в `<run>/target/`/`source/` |
| `src/db_project_manager/domain/diff.py` | `ObjectSnapshot.build` (additive, default True); константа `IGNORED_BUILD_FALSE_KEY` |
| `src/db_project_manager/infrastructure/diff/snapshot.py` | `build=vertex.build` при построении снапшота |
| `src/db_project_manager/application/safety_gate_service.py` | `SafetyGateVerdict.ignored_build_false` из `report.summary` |
| `src/db_project_manager/domain/safety.py` | поле `ignored_build_false: int = 0` |
| `src/db_project_manager/infrastructure/deploy/safety_report.py` | `rows_phrase`/`_fmt_rows` для `reltuples=-1` → «нет статистики»; примечание ignored |
| `src/db_project_manager/application/deploy_apply_service.py` | копия rehearsal RE → `<run>/rehearsal_re/` |
| `src/db_project_manager/infrastructure/diff/columns.py` | `_default_expression` канонизирует text-cast (§63); `_TYPE_ALIASES` += `serial/bigserial/smallserial`; `_has_not_null` считает serial NOT NULL |
| `src/db_project_manager/presentation/cli/main.py` | run-каталог + `--no-run-subdir` у `compare run`/`deploy analyze`/`deploy plan`/`deploy apply`; печать run-dir; `rows_phrase` |
| `src/db_project_manager/presentation/gui/widgets/workers.py` | воркеры создают run-каталог через `create_run_dir` |
| `src/db_project_manager/presentation/gui/main_window.py` | пути отчётов через `resolve_report_dir`; `rows_phrase`; ignored-note |
| `src/db_project_manager/infrastructure/diff/markdown_report.py` | строка `ignored (build=false)` в Summary |

Тесты:
- `tests/unit/test_run_naming.py` — 6 новых (generate/decode/latest/resolve/no-subdir/collision).
- `tests/unit/test_compare_service.py` — копия target/source; исключение build=false обеих сторон.
- `tests/unit/test_safety_report.py` — формат `-1`; ignored-note.
- `tests/unit/test_deploy_analyze_cli.py` — run-подкаталог + `--no-run-subdir`.
- `tests/unit/test_extract_columns.py` — text-cast в DEFAULT; bare `SERIAL` + NOT NULL; `__deploy` roundtrip.

## 3. Ключевые решения

- **Run-каталог создаётся на границе команды (CLI/GUI), не в сервисах.** Сервисы
  получают готовый каталог — сигнатуры не меняются, вложенности run-каталогов
  при вызове compare из analyze/plan/apply нет; unit-тесты, зовущие сервисы
  напрямую, не ломаются.
- **build=false исключается из ОБЕИХ сторон по identity_key**, а не из source —
  иначе объект, присутствующий в БД, стал бы REMOVED.
- **Канонизация text-cast дублируется в пути извлечения колонок** (не только в
  hash тела) — это тот же класс §63, применённый ко второму пути сравнения.
- **serial-алиасы расширены до bare `SERIAL`/`BIGSERIAL`/`SMALLSERIAL` + неявный
  NOT NULL** — системная `__deploy` пишет `SERIAL`, а не `serial4`.

## 4. Проверки

```bash
# 955 passed (базово 939 + 16 новых Phase 15.7)
uv run pytest tests/unit/ -q -p no:randomly
uv run ruff check src/ tests/   # All checks passed!
```

E2E на cis_zup (локальная PG-18):

| Сценарий | До | После |
|----------|-----|-------|
| `build:false` на zup_* (deploy analyze) | 2 violations (`changed`) | CLEAN, `ignored_build_false: 2` |
| `build:true` (deploy analyze) | 2 violations (`changed`) | CLEAN, `unchanged` +2 |
| `__deploy.schema_version`/`script_audit_log` (deploy apply) | blocked `type_changed` | no diff |

## 5. Известные ограничения / NOT done

- `--keep-model-dir` у `compare run` оставлен как deprecated no-op (снапшот
  копируется всегда).
- Ротация run-каталогов не реализована (BACKLOG P3 «Ротация run-каталогов»).
- `deploy analyze` без даунгрейда ALT-3; YAML-diff как альтернатива hash-diff —
  остаются в BACKLOG (Phase 16+).
- Вне git не проверялась целевая БД `IVSD00258.reksoft.com` — фикс `__deploy`
  подтверждён на сохранённом target-снапшоте прогона пользователя.

## 6. Где читать дальше

| Док | Зачем |
|-----|-------|
| `Phase_15.7_draft.md` | план + закрытые USER_INPUT |
| `LESSONS_LEARNED.md` §65–§68 | новые уроки (build-фильтр, text-cast в колонках, bare SERIAL, Device Guard) |
| `_checkpoints_/20260908_001_checkpoint.md` | снапшот состояния после фазы |
| `BACKLOG.md` §P3 keep-target-dir / ротация | закрытая и открытая backlog-задачи |
