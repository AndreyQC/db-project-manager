# Phase 15.7 (draft): run-каталоги с timeline-именами + сохранение target-snapshot + build=false в analyze

> **Дата:** 2026-09-04
> **Статус:** draft — USER_INPUT закрыты 2026-09-04 (решения в §7); к реализации
> **Контекст:**
> - `_docs_/_tasks_/BACKLOG.md` §P3 «Сохранение target-RESULT в `output_dir/target/`» (было Phase 15.5.6)
> - `_docs_/_checkpoints_/20260904_001_checkpoint.md` — «To resolve in next session»
> - `LESSONS_LEARNED.md` §63, §64 (PG round-trip эквивалентности)
> - Код пользователя: `TimelineNameGenerator` (человекочитаемые ID, кодирующие время)

## 1. Цель фазы

1. **Run-каталоги.** Каждый прогон отчётной команды пишет артефакты в уникальный
   подкаталог `<output_dir>/<timeline-name>/` (имя из `TimelineNameGenerator`,
   напр. `dancing-red-crazy-godzilla-45`), а не впрямую в `output_dir`.
2. **Сохранение DB-side RE-snapshot.** Вместо удаления tempdir
   (`dbpm_compare_<label>_*`, `dbpm_rehearsal_re_*`) — копировать snapshot в
   run-каталог: `target/` (или `source/` для DB-источника, `rehearsal_re/` для
   apply). Это закрывает BACKLOG P3 «keep-target-dir».
3. **`build=false` в analyze/compare.** Объекты с `project.build: false` в
   autodoc больше не попадают в отчёты как changed (сейчас — попадают, см. §3).
4. **Диагностика cis_zup.** Понять, почему `cis_dmt_zup.zup_process_log` и
   `cis_dmt_zup.zup_api_sourcedata_load_log` всё ещё `[changed, ~-1 строк]`
   в safety_gate_report после фиксов Phase 15.5.3–15.5.5 — используя новые
   target-снапшоты из п.2.

## 2. Найденные факты (по коду, 2026-09-04)

| # | Факт | Где |
|---|------|-----|
| F1 | `build_snapshot_from_dir` включает ВСЕ вершины с типом из `DIFFED_TYPES` — фильтра `vertex.build` НЕТ | `src/db_project_manager/infrastructure/diff/snapshot.py:74-77` |
| F2 | `deploy plan`/`apply` build=false УЖЕ пропускают (`deploy_order(build_only=True)` + skip в цикле) | `src/db_project_manager/application/delta_service.py:95,106-120` |
| F3 | Значит: `deploy analyze` (safety gate) и `compare run` видят build=false объекты → репортят changed → violation «нет покрывающего pre-скрипта». **Это и есть ответ, почему build: false не помог** | `application/safety_gate_service.py:259-283` (нет проверки build) |
| F4 | DB-side RE пишет в `tempfile.mkdtemp(prefix="dbpm_compare_<label>_")`, удаляется в `finally` (если не `keep_model_dir`, причём флаг есть только у `compare run` и оседает в %TEMP%) | `application/compare_service.py:132-135,176` |
| F5 | Rehearsal RE в apply — тоже tempdir, тоже удаляется | `application/deploy_apply_service.py:224,274` |
| F6 | `--output-dir` есть у 4 команд: `compare run` (main.py:298), `deploy analyze` (:453), `deploy plan` (:593), `deploy apply` (:666). `reverse-engineer --output` — НЕ трогаем (это кодовая база, не отчёты) | `presentation/cli/main.py` |
| F7 | GUI читает плоские пути: `output_dir/safety_gate_report.md` (main_window.py:334), `output_dir/plan.json` (:388), root viewer (:66) — потребуется резолв run-подкаталога | `presentation/gui/main_window.py` |
| F8 | `~-1 строк` = `reltuples = -1` (PG 13+: таблица никогда не ANALYZEd/vacuumed). Gate уже трактует это fail-safe как «данные есть», но отображение `-1` нечитаемо | `infrastructure/database/postgres/queries.py:349`, `deploy/safety_report.py` |

Важно (симметрия фильтра): если исключить build=false только из source-стороны,
diff покажет объект как REMOVED (в target-БД он есть) — станет хуже. Исключение
должно действовать на ОБЕ стороны по объединению identity_key.

## 3. Гипотезы: почему две zup_* таблицы всё ещё CHANGED (проверить через п.2)

| # | Гипотеза | Как проверяется |
|---|----------|-----------------|
| H1 | Прогон был до коммитов 15.5.3–15.5.5 (старая версия) | дата запуска vs `e379cb0/cff8634/e63e209` |
| H2 | `columns_unavailable=True` у одной из сторон → 15.5.5 fail-safe оставляет CHANGED | `diff_report.json` → `column_diffs_unavailable` у этих таблиц; почему extract_columns вернул None |
| H3 | Обе стороны имеют default в РАЗНЫХ лексических формах (`NEXTVAL(CAST('...' AS REGCLASS))` vs `nextval('...'::regclass)`): компенсация 15.5.4 работает только для пары None↔nextval, а `_canonicalize_text_casts` канонизирует только `AS TEXT`, не `AS REGCLASS` | сравнить `target/.../zup_*.sql` с source-файлами глазами + column_diffs в diff_report |
| H4 | Реальная разница не в колонках (comment, storage options), а column-extract недоступен → hash решает | как H2/H3 |

Если вскроется новый класс PG-эквивалентности — по правилу LESSONS §64 (урок 6):
canonicalization + regression-тест + запись в LESSONS.

## 4. Предлагаемое решение

### S1. Модуль имён run-каталогов
`src/db_project_manager/infrastructure/files/run_naming.py` — адаптация
`TimelineNameGenerator` пользователя:
- словари как в оригинале (дефензивный `.sort()` оставить), `random.Random(seed)`;
- **инъекция времени** (`now_fn: Callable[[], datetime]`) и seed — для тестов;
- API: `create_run_dir(root: Path) -> Path` (создаёт `<root>/<name>/`,
  коллизия → перегенерация, затем суффикс `-2`);
  `decode_run_dir_name(name) -> datetime | None` (для «найти последний прогон»;
  decode терточен для краевых месяцев — fallback: mtime каталога);
  `latest_run_dir(root) -> Path | None`.

### S2. Run-каталог у сервисов
`CompareService.run`, `SafetyGateService.analyze`, `deploy plan` (DeltaService
вызов из CLI), `DeployApplyService` — создают run-каталог в начале и пишут ВСЁ
туда (`source.json`, `target.json`, `diff_report.json`,
`safety_gate_report.{md,json}`, `delta/`, `plan.{json,md}`, `target/`...).
Сервисы возвращают фактический путь; CLI печатает его.

### S3. Copy-instead-of-delete (BACKLOG P3)
`CompareService._build_db_side`: после snapshot — `shutil.copytree(temp_root,
<run_dir>/<label>/)` (label = `target` | `source`), затем rmtree temp как сейчас.
`DeployApplyService`: rehearsal RE → `<run_dir>/rehearsal_re/`.
`--keep-model-dir` у `compare run` становится избыточным (копия теперь всегда
в run-каталоге) — оставить как no-op/deprecated чтобы не ломать скрипты.

### S4. build=false в analyze/compare (семантика — USER_INPUT-1)
Исключить build=false объекты из diff ОБЕИХ сторон (объединение identity_key);
в отчёте — счётчик/секция «ignored (build=false)». Задаётся в snapshot-слое или
в CompareService перед compare.

### S5. Отображение `~-1 строк`
`estimated_rows = -1` → «нет статистики (таблица не ANALYZEd)»; логика
fail-safe (UNKNOWN → «есть данные») НЕ меняется.

### S6. GUI
Диалоги получают фактический run-dir из результата воркера; Plan Viewer
(standalone) — `latest_run_dir(output_dir)` при открытии; отображение имени
прогона в статусе.

## 5. Риски / заметки

- **Ломается привычная раскладка output_dir** (плоские `plan.json` и т.п.) —
  скрипты пользователя, ожидающие `output_dir/plan.json`, перестанут находить
  файл. Вопрос про escape-hatch (`--no-run-subdir`?) — USER_INPUT-1b.
- TimelineNameGenerator: имена не сортируются лексикографически по времени
  (слово-года не монотонно) — «последний прогон» только через decode/mtime.
- Диск: run-каталоги накапливаются (RE-snapshot = текстовые SQL, дёшево, но
  ротация — USER_INPUT-2).
- Тесты: гонять генератор на фиксированном `now_fn`/seed; decode-roundtrip;
  коллизия имён; exclusion build=false (обе стороны); copytree в run-dir;
  CLI печатает run-dir; GUI-контракт «открылся последний прогон».

## 6. Проверки

```bash
unset SSL_CERT_FILE REQUESTS_CA_BUNDLE CURL_CA_BUNDLE
uv run pytest tests/unit/ -q -p no:randomly   # базлайн 939 passed
uv run ruff check src/ tests/
# e2e на cis_zup (если USER_INPUT-3 = да):
db-pm deploy analyze --dir <cis_zup> \
  --target-connection-file connections/local-PG-18_DB__cis_zup_dev_U_postgres.yaml \
  --output-dir <reports_root>
# ожидание: run-dir с target/-снапшотом; zup_* либо исчезли из отчёта
# (build=false / канонизация), либо видна конкретная причина CHANGED
```

## 7. USER_INPUT (открытые вопросы)

### USER_INPUT-1: какие команды переводим на run-каталоги?
**Рекомендация ИИ:** все 4 (`compare run`, `deploy analyze`, `deploy plan`,
`deploy apply`) — единый аудит-трейл; сервисы возвращают run-dir, CLI печатает.
Альтернатива: только read-only диагностика (analyze/plan/compare), apply
пишет плоско. Подвопрос: нужен ли `--no-run-subdir` для старых скриптов?
**Ответ (2026-09-04):** все 4 команды. Escape-hatch `--no-run-subdir`
добавляем для обратной совместимости (старые скрипты, ждущие
`output_dir/plan.json`).

### USER_INPUT-2: ротация старых run-каталогов?
**Рекомендация ИИ:** не чистить ничего автоматически (прозрачность), максимум —
документировать; при желании позже добавить `--keep-runs N`.
**Ответ (2026-09-04):** хранить все, без авто-чистки. Добавить backlog-задачу
на ротацию: имена run-каталогов декодируются в дату → чистка по имени
возможна без mtime (см. BACKLOG P3 «Ротация run-каталогов»).

### USER_INPUT-3: диагностика cis_zup в этой же сессии?
**Рекомендация ИИ:** да — после реализации прогнать `deploy analyze` против
локальной cis_zup и разобрать `target/`-снапшоты двух таблиц (это главная цель
пользователя). Требует доступной локальной PG-БД.
**Ответ (2026-09-04):** да, локальная БД доступна — диагностика в этой же
сессии после реализации.

### USER_INPUT-4: семантика build=false в отчётах
**Рекомендация ИИ:** исключить из diff обеих сторон + счётчик «ignored
(build=false)» в отчёте (не молча — иначе непонятно, почему объекта нет).
Альтернативы: исключить молча; оставить в diff, но не считать нарушением gate.
**Ответ (2026-09-04):** исключить из обеих сторон + показывать счётчик
«ignored (build=false)» в отчётах.

## 8. План работ (после закрытия USER_INPUT)

1. `run_naming.py` + unit-тесты (генерация/decode/коллизия/последний прогон).
2. Run-каталог в CompareService (+ copy target/source), SafetyGate, plan, apply.
3. build=false exclusion (snapshot/compare) + тесты; счётчик в отчётах.
4. `~-1 строк` → «нет статистики».
5. CLI: печать run-dir; GUI: резолв последнего прогона.
6. Документация: BACKLOG P3 закрыт, checkpoint, LESSONS (если новый класс
   эквивалентности по итогам диагностики §3).
