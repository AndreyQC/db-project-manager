# Phase 16: Greenplum tuning — план и журнал валидации

> **Дата:** 2026-09-09
> **Статус:** в работе (живой документ — журнал §5 дополняется каждым шагом)
> **Результат:** будет оформлен отдельным `Phase_16_result.md` по завершении фазы
> **Фаза в `_phases_`:** `Phase_16.md` будет написан по завершении (PHASES_CONVENTION §1)

> Контекст:
> - `_checkpoints_/20260908_001_checkpoint.md` — состояние до фазы
> - `_tasks_/phase_15/Phase_15.9_result.md` — первый живой GP-деплой (preflight rolsuper)
> - `LESSONS_LEARNED.md` §69, §70
> - `_tasks_/ROADMAP.md` §2 — карта фаз (Phase 17 = post-deploy отчёты CD-16..19)

---

## 1. Цель фазы

Все команды `db-pm` (`reverse-engineer`, `graph`, `deploy validate/analyze/plan/apply`,
`compare run`, `yaml generate/apply`) работают на Greenplum-кластере без PG-only
конструкций. Поведение на PostgreSQL не меняется: каждый фикс верифицируется на обеих
СУБД.

Контекст возникновения: конвейер писался и тестировался против современного
PostgreSQL (testcontainers + локальный PG 18). Первый живой деплой на Greenplum
(IVSD00258, `cis_zup_gp_dev`, роль `gpadmin`) вскрывает несовместимости сериями —
фаза собирает их из прогонов и закрывает по одному.

## 2. Среда кластера (факты, подтверждённые живым пробником 2026-09-09)

| Факт | Значение | Следствие |
|------|----------|-----------|
| Ядро кластера | **PostgreSQL 9.4.26 (Greenplum 6.19.4)** | Нет функций PG 9.5+: `array_position`, jsonb-семейство и т.п. |
| `pg_sequence` | отсутствует | Уже прикрыто фолбэком `GET_SEQUENCES_GREENPLUM` |
| `gp_distribution_policy` | доступен | DISTRIBUTED BY можно извлекать при RE (см. кандидаты §6) |
| Роль `gpadmin` | `rolsuper=True, rolcreatedb=False` | Закрыто Phase 15.9 (`rolsuper OR rolcreatedb`) |
| Подключение | `connections/IVSD00258.reksoft.com_GP__DB__cis_zup_gp_dev__U_gpadmin.yaml` | Для живых пробников |

## 3. Регламент шага валидации

Замечания приходят из живых прогонов пользователя. Каждое замечание = шаг
`16.N` и проходит цикл (образец — 15.5.x…15.9):

1. Зафиксировать в журнале §4: команда, симптом (текст ошибки), дата.
2. Диагноз живым запросом через адаптер проекта (probe-скрипт): `SELECT version()`,
   наличие функций в `pg_proc`, кандидат-конструкция на реальном кластере
   (LESSONS §70.2: stacktrace пользователя — гипотеза, подтверждай каталогом).
3. Фикс + регрессионный тест (контракт на SQL-текст или поведение).
4. Верификация на обеих СУБД: GP-кластер (пробник/прогон) + PG
   (`cis_zup_dev` на IVSD00258, подключение `hrdo_user`) — эквивалентность
   доказывается построчным сравнением, не «запрос выполнился».
5. Отдельный коммит кода `fix(app)/feat(app): Phase 16.N — ...`, затем доковый
   коммит журнала. Код и доки не смешиваются (TASK_CONVENTIONS §6).

Принцип фиксов: **универсальный SQL для PG+GP** (как 16.1); ветвление по
`_is_greenplum` — только когда эквивалента нет (образец: `pg_sequence`).

## 4. Журнал валидации

| # | Дата | Команда | Замечание / симптом | Диагноз → фикс | Верификация | Статус |
|---|------|---------|---------------------|----------------|-------------|--------|
| 16.1 | 2026-09-09 | `reverse-engineer` | `UndefinedFunction: array_position(int2vector, smallint) does not exist` на `__deploy.schema_version` — RE падает на первой таблице | `GET_INDEXES::ORDER BY` использует `array_position` (PG 9.5+), ядро GP 6 = PG 9.4. Заменён на `unnest(idx.indkey::smallint[]) WITH ORDINALITY` (PG 9.4+, универсально). LESSONS §70. Коммит `79bcf11` | GP: пробник OK (функция в `pg_proc` отсутствует, замена работает). PG 18.6: старый/новый ORDER BY идентичны построчно на всех индексах `cis_zup_dev`. Unit: 12 passed; ruff clean | **готово** |
| 16.2 | 2026-09-09 | `yaml generate` / `yaml apply` | (решение пользователя, не прогон) Тип БД требовался руками (`--db-type`, `--target-db-type` обязательны), хотя он уже известен из манифеста RE / самого YAML-проекта | Каскад db_type: подключение → манифест → параметр. `yaml generate`: тип из `dbpm.manifest.json`, флаг опционален, конфликт флага с манифестом = exit 2. `yaml apply`: target по умолчанию = `db_type` проекта, явный флаг — для кросс-типовых конвертаций (15.8) | Unit: 8 новых CliRunner-тестов; полный прогон 974 passed + 1 известный флаки (LESSONS §22, подтверждён изоляцией); ruff clean | **готово** |
| 16.3 | 2026-09-09 | `reverse-engineer` (повторный прогон после 16.1) | На GP-подключении (`greenplum=True` в логе) WARNING «pg_sequence недоступен» с полным дампом SQL — на каждую схему (9 за прогон), хотя фолбэк — штатный путь для GP6 | `_get_sequences` пробовал PG-запрос первым и ветвился только в `except`. Фикс: capability-проба `pg_sequence` кэшируется на подключение (`_pg_sequence_available`), лог — однократный INFO без дампа. Проба, а не ветка по `_is_greenplum`: GP 7 (ядро PG 12) имеет `pg_sequence` и должен сохранять полный запрос | Unit: 4 теста (проба 1 раз, INFO без WARNING, GP7-путь, raise на postgres-подключении); полный прогон 979 passed; ruff clean | **готово** |
| 16.4 | 2026-09-13 | `reverse-engineer` / `deploy analyze` (продолжение серии 16.1–16.3, выявлено аудитом `queries.py` до живого падения) | `GET_FUNCTIONS`/`GET_PROCEDURES` используют `pg_proc.prokind` (PG 11+) — на ядре GP 6 (PG 9.4) это `UndefinedColumn` при RE любой схемы с функциями | Наборы колонок дизъюнктны: GP6 различает функции через `proisagg`/`proiswindow` (удалены в PG 11) — универсальный запрос невозможен, в отличие от 16.1. Фикс: пара `GET_FUNCTIONS_{POSTGRES,GREENPLUM}` + capability-проба `PROKIND_PROBE` кэшируется на подключение (паттерн 16.3); ядри без prokind не имеют `CREATE PROCEDURE` вовсе → процедуры возвращаются пустым списком без запроса. Коммит `4b936a3` | Unit: 4 adapter-теста + контракты (`test_pg_adapter_prokind.py`, `test_queries.py`); живой прогон `deploy analyze` 2026-09-13 на `cis_zup_gp_dev` — функции извлечены legacy-запросом без падения; PG-запрос текстуально не изменён | **готово** |
| 16.5 | 2026-09-13 | `deploy analyze` (прогон пользователя) | Админ-схема GP `gp_toolkit` попала в сравнение: 3 web-таблицы → REMOVED ~1e6 строк «нет покрывающего pre-скрипта» → ложные VIOLATIONS (196), пайплайн остановлен; в снапшот ушли все 52 объекта схемы. Замечание пользователя: «такого вида таблицы не должны никак попадать в сравнение», «это только для greenplum» | Пробник: `gp_toolkit` — единственная не-`pg_%` админ-схема кластера; НЕ extension-owned (`pg_extension`=plpgsql, 0/52 `deptype='e'`) → единственная точка отсечения — список схем. Фикс: `GP_ADMIN_SCHEMAS` (`gp_toolkit`, `gp_statistics`, `gp_statistics_history` — GP 7 forward-compat) исключаются в `_get_schemas()` только при `_is_greenplum` (GP-only по требованию пользователя). LESSONS §71. Коммит `e8234a9` | Unit: 4 теста (`test_gp_admin_schemas.py`: исключение на GP, сохранение на PG, INFO-лог однократно, GP7-схемы в списке); живой RE на GP 19:39 — `gp_toolkit` в снапшоте нет; PG-ветка не выполняется (проверено юнит-тестом) | **готово** |
| 16.6 | 2026-09-13 | `deploy analyze` (тот же прогон) | 211 таблиц CHANGED при совпадающих версиях (source=target=2026.09.08.01) — сравнение кодовая база vs RE-снапшот захлёбывается ложными изменениями | Сравнение файлов пары источник/RE + пробники: RE не читает GP-свойства таблиц — (1) `WITH (appendonly/orientation/compresstype/compresslevel)`, (2) `DISTRIBUTED BY/RANDOMLY`. Пробник: `gp_distribution_policy` в GP 6.19.4 БЕЗ колонки `attrnums` (фактические: `policytype/distkey int2vector/numsegments/distclass`); 196/196 таблиц кластера `policytype='p'` с пустым `distkey` (=RANDOMLY); каталог лежит в `pg_catalog` (квалификация `gp_catalog.*` = UndefinedTable). ext_* таблицы — обычные heap (`relkind='r'`), external-рендер не нужен. Фикс: `GET_TABLE_GP_OPTIONS` (1 запрос на схему, только `_is_greenplum`) + `gp_tail_sql` в рендере таблицы | Живое хеш-сравнение RE vs кодовая база (после 16.6+16.7): таблицы **193/196 equal** | **готово** (коммит `418adf7`, совместно с 16.7 — изменения адаптера пересекаются) |
| 16.7 | 2026-09-13 | там же | Вторая половина классов ложных CHANGED — канонические формы: (3) `bool` (каталог GP) vs `boolean` (кодовая база); (4) DEFAULT `timezone('utc'::text, now())` vs `CURRENT_TIMESTAMP AT TIME ZONE 'utc'`; (5) sqlglot «парсил» GP-DDL в Command-узел — regex-нормализация не выполнялась, сравнение шло по сырому тексту | `normalize_sql`: GP-хвост (WITH/DISTRIBUTED) вырезается до парса, канонизируется отдельно (порядок/регистр опций нечувствительны, алиас `appendonly`≡`appendoptimized`), «голова» — полноценный AST. Новые правила эквивалентности: bool/boolean; timezone-функция ≡ AT TIME ZONE (+лишние скобки); дефолтный `VOLATILE` в шапке функции (GP6 `pg_get_functiondef` опускает); кавычки all-lowercase идентификаторов ≡ без кавычек; избыточный список колонок view + case-only алиасы (`"N1_x" AS n1_x`); serial-детект в RE (`serial4/8` при nextval дефолтно-именованной последовательности; bare `serial` запрещён — sqlglot уводит его в IDENTITY). LESSONS §72 | Unit: 1014 passed + ruff clean; живое сравнение: функции 63/67 equal, view 2/11 (остаток — реальная разница рендера выражений каталогом GP6, см. кандидаты) | **готово** (коммит `418adf7`) |
| 16.8 | 2026-09-14 | `deploy apply` (прогон пользователя) | CD-11: apply отклонён — 3 таблицы `__deploy` заблокированы («columns=None — fail-safe; покрывающего pre-скрипта нет»); доп. мина: 2 сервисные последовательности классифицированы safe-drop (DROP упал бы на dependency serial-колонки) | Анализ (без правок, по артефактам + эксперименту): canonical-сид в кодовой базе (static PG-шаблоны: SERIAL, IF NOT EXISTS, inline PK, без DISTRIBUTED) vs каталог-рендер GP в таргете → hash-mismatch → alter вместо skip; `extract_columns` на DISTRIBUTED деградирует до Command-узла → None → blocked. Решение пользователя: вариант A (исключить сервис-схему из сравнения) + columns-фикс + предупреждение об отсутствии. Фикс: `CompareService._exclude_service_schema` (обе стороны, имя конфигурируемое, summary `ignored_service_schema`), WARNING при отсутствии схемы на любой стороне (каждый прогон), `strip_gp_tail` в `columns.py`. LESSONS §73 | Unit: 5 новых + счётчики фикстуры обновлены; 1019 passed; ruff clean. Живой `deploy plan` на cis_zup_gp_dev: **481 ops, safe 481 / needs-pre 0 / blocked 0** (было 3 blocked), diff: `ignored_service_schema=6`, `__deploy`-записей 0 | **готово** |
| 16.9 | 2026-09-14 | разбор плана 16.8 | 67 функций «расщеплены» на added+removed пары: свежий RE пишет `object_signature` (+`/signature/` в key), файлы кодовой базы старой генерации — нет → identity не совпадает | Парсер реконструирует сигнатуру из SQL-тела, когда autodoc её не несёт: экстрактор аргументов (regex — GP `EXECUTE ON ANY` деградирует AST, §72-1) в typname-форме (DDL-спеллинги схлопнуты, DEFAULT/OUT отброшены, VARIADIC-массивы `_text`), автодок `argument_types` приоритетнее. Попутно: `canonical_type` схлопывает явный нулевой scale (`decimal(9)`≡`numeric(9,0)` — ложный type_changed на lu_limits_on_food_cards). Коммит `ca368ff` | Unit: 10 тестов (`test_function_args.py`) + 2 scale-0; полный прогон зелёный; живой план: added 202→103, removed 69→2, unchanged 195→291 | **готово** |
| 16.10 | 2026-09-14 | там же | Тупик 16.9-остатка: 2 serial-последовательности (`zup_process_log_…_id_seq`) — с `--include-drops` DROP упадёт на владении колонкой (нет CASCADE), без флага — blocked (ALT-6); apply невозможен ни так ни так | pg_dump-модель: имплицитная serial-последовательность (`<table>_<col>_seq` + int-колонка с default None/nextval) сворачивается в колонку и не сравнивается. `CompareService._exclude_serial_sequences` — обе стороны, по данным снапшота (owning в autodoc не пишется — выводимо из имени+колонок), summary `ignored_serial_sequences`. Trade-off: переименованная hand-tuned последовательность под паттерном перестаёт трекаться (как у pg_dump) | Unit: 2 теста (сворачивание: 0 removed; custom-имя не сворачивается); 1033 passed; ruff clean. Живой план **с `--include-drops` и без: 412 ops, safe 412 / needs-pre 0 / blocked 0** | **готово** |

## 5. Кандидаты (риски, ждут подтверждения из прогонов)

Не замечания, а предвиденные пробелы — проверять по мере прохождения конвейера:

- **DISTRIBUTED BY при RE — ЗАКРЫТО шагами 16.6–16.7** (193/196 таблиц
  hash-equal; ext_* оказались обычными heap-таблицами, external-рендер не
  потребовался).
- **16.8 (кандидат): канонизация рендера выражений каталогом GP6.** Остаток
  9 view CHANGED: `CAST(CAST('3000-01-01' AS DATE) AS TIMESTAMP) - CAST('1 day'
  AS INTERVAL)` (pg_get_viewdef GP6) vs `CAST('3000-01-01' AS DATE) - INTERVAL
  '1 day'` (кодовая база) — развёрнутые CAST-формы арифметики дат. Конечные
  пары можно канонизировать по образцу 16.7, если принесут замечание.
- **16.9 (кандидат): serial-последовательности как отдельные объекты.** RE
  пишет `t_c_seq.sql` отдельным файлом, кодовая база в стиле serial4 их не
  декларирует → шум no-counterpart (2 шт. на cis_zup). Вариант: pg_dump-style
  сворачивание (не эмитить файл, если последовательность дефолтно-именованная
  и свернута в колонку).
- **`__deploy` в сравнении (решение пользователя).** Кодовая база держит
  только `schema __deploy.sql`; таблицы/последовательности сервис-схемы
  (`schema_version`, `script_audit_log`, `script_history`, seq) создаёт сам
  деплой-конвейер → постоянный дрейф в diff. Варианты: исключить `__deploy`
  из compare (как gp_toolkit) или сидировать полный набор в кодовой базе.
- **4 функции fun_*: квалификация имён внутри комментариев тела `$$`** —
  реальный дрейф (deploy-time qualify-refs переписал тело, включая
  комментарии). Возможно, qualify-refs не должен трогать комментарии/строки.
- **GP-партиции**: `pg_partition` — партиции могут отдаваться RE как отдельные
  таблицы (`relkind='r'`).
- **reltuples на распределённых таблицах** (safety gate): предсказано
  `adapter.py:53`, не проверялось на живом GP.
- **normalize_sql/sqlglot на GP-клаузах** (`DISTRIBUTED BY`, `WITH`, `LOCATION`):
  `normalize_sql.py:8` это допускает; Phase 13 обходил regex-парсером, но
  deploy-путь (compare/diff) не гонялся по GP DDL.
- **CREATE DATABASE / template0-fallback на GP** (LESSONS §32) — deploy validate
  создаёт temp-БД; поведение на GP не проверено.

## 6. Решения

- **Нумерация:** GP-тюнинг = **Phase 16** (блокирует текущую работу пользователя);
  post-deploy отчёты (CD-16..19) сдвинуты в **Phase 17**. ROADMAP: порядок важнее
  номеров.
- **Каскад разрешения db_type (16.2):** команды определяют тип БД по цепочке
  «подключение → манифест → параметр команды»:
  1. команды с подключением (`reverse-engineer`, `deploy *`, `compare run` DB-side)
     берут тип из `ConnectionConfig.type` (адаптер `_is_greenplum`) — уже было;
  2. файловые команды (`yaml generate`) — из `dbpm.manifest.json` RE-выхода;
  3. явный параметр — фолбэк для каталогов без манифеста; конфликт флага с
     манифестом = ошибка, а не тихое предпочтение (неверный тип выбирает
     неверный SQL-парсер);
  4. `yaml apply`: target по умолчанию = `db_type` проекта; явный флаг — только
     для кросс-типовых конвертаций (15.8).
  GP-специфичный SQL добавляется парами по образцу `GET_SEQUENCES_{POSTGRES,GREENPLUM}`;
  универсальная форма предпочтительнее пары (LESSONS §70-4).
- **Граница фазы:** только несовместимости/тюнинг под GP, вскрываемые живыми
  прогонами. Рефакторинги и новые фичи (YAML-diff, отчёты) — вне фазы.
- **Формат фикс-коммитов:** `Phase 16.N` — сквозная нумерация шагов валидации
  (по образцу 15.x), N растёт с каждым замечанием.

## 7. Проверки (базовые команды шага)

```bash
# unit + lint (обход Device Guard: кэшированный интерпретатор uv, LESSONS §68)
PYTHONPATH="src;.venv/Lib/site-packages" ~/AppData/Roaming/uv/python/cpython-3.13.*/python.exe \
    -m pytest tests/unit/ -q -p no:randomly
.venv/Scripts/ruff.exe check src/ tests/

# живая верификация
#   GP:  connections/IVSD00258.reksoft.com_GP__DB__cis_zup_gp_dev__U_gpadmin.yaml
#   PG:  connections/IVSD00258.reksoft.com_DB__cis_zup_dev__U_hrdo_user.yaml (18.6)
```

## 8. NOT done / границы

- Integration-тесты (testcontainers) не гоняются — Docker не используется (решение
  пользователя); замена — живые прогоны на GP + PG-кластере.
- `_phases_/Phase_16.md` и `Phase_16_result.md` — по завершении фазы.
- Чекпоинт — по завершении фазы или существенной вехи внутри неё.
