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

## 5. Кандидаты (риски, ждут подтверждения из прогонов)

Не замечания, а предвиденные пробелы — проверять по мере прохождения конвейера:

- **DISTRIBUTED BY при RE**: `queries.py` не читает `gp_distribution_policy` →
  раскладка теряется; deploy validate создаёт таблицы с distribution по умолчанию.
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
