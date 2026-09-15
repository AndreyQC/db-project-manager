# Phase 15.8 (draft): yaml apply — конвертация external tables в обычные

> Контекст:
> - `_docs_/_tasks_/phase_13/Phase_13.md` — базовая функциональность `yaml generate` / `yaml apply` (CD-13b)
> - `LESSONS_LEARNED.md` §53–§58 — уроки Phase 13 (GP-парсинг, roundtrip-инварианты, раскладка)
> - `_docs_/_checkpoints_/20260908_001_checkpoint.md` — текущий статус проекта

**Дата:** 2026-09-08
**Статус:** draft — USER_INPUT-1..4 закрыты ответами пользователя 2026-09-08 (см. раздел 4); решения интегрированы в S2 / разделы 5–6
**Заказчик сценария:** legacy Greenplum-корпус `Legacy_dp_dev.yml` (LM.HRDO-2.DEV, 2026-09-08_legacy_greenplum)

---

## 1. Контекст и проблема

Пользователь переносит legacy Greenplum-базу в управляемую кодовую базу:

```bash
db-pm yaml apply \
    --yaml .../Legacy_dp_dev.yml \
    --target-db-type greenplum \
    --output .../greenplum/current_dev
```

В YAML-проекте внешние таблицы (PXF/JDBC-источники, пример
`ext_w_staging_tr_zup_headcount_calc_result`) описаны как `external_tables`
с полями `location` / `format_type` / `format_options` / `encoding`. Для целевой
кодовой базы они не нужны как external — нужны обычные таблицы.

Текущее поведение `YamlApplyService`
(`src/db_project_manager/application/yaml_apply_service.py`):

| target_db_type | external_tables из YAML | Поведение |
|---|---|---|
| greenplum | есть | рендер `CREATE EXTERNAL TABLE ... LOCATION (...) FORMAT ...` в `external_tables/` |
| postgres | есть | жёсткая `YamlApplyError` («Postgres does not support...») |

Нужен опциональный режим «конвертировать внешние таблицы в обычные» — и в CLI,
и в GUI. При включении все `external_tables` превращаются в обычные таблицы
(структура колонок 1:1, external-специфичные поля отбрасываются).

## 2. Цели

1. CLI-флаг `--convert-external-to-tables` для `db-pm yaml apply`.
2. Чекбокс «Конвертировать внешние таблицы в обычные» в GUI-диалоге
   YAML apply (settings round-trip + CLI-builder + worker).
3. Сгенерированные объекты: `tables/table <name>.sql`, autodoc
   `object_type: table`, существующий путь `_write_table_sql`.
4. Счётчик сконвертированных в `YamlApplyResult` и в выводе CLI/GUI.
5. Без флага поведение не меняется (регрессии на оба существующих пути).

## 3. Решения (design)

### S1. Конвертация — in-memory трансформация YamlProject до валидации

- Новый метод `YamlApplyService._convert_external_tables(project) -> YamlProject`
  (чистая функция: пересборка pydantic-моделей, без мутации входа):
  для каждой схемы каждый `YamlExternalTable` → `YamlTable(name=ext.name,
  columns=ext.columns, distributed_by=[], with_options={})`;
  `external_tables` схемы очищается.
- Порядок в `run()`: parse → **convert (если флаг)** → `_validate` → write.
  Конвертация до валидации закрывает кейс GP→postgres без спец-условий:
  после конвертации external-списки пусты, запрет не срабатывает
  (см. USER_INPUT-3).
- Guard: коллизия имён ext-таблицы с обычной таблицей в одной схеме →
  `YamlApplyError` (в живом GP namespace общий и коллизия невозможна, но
  hand-made YAML может содержать).

### S2. DDL конвертированной таблицы

- Запись через существующий `_write_table_sql`: раскладка
  `<schema>/tables/table <name>.sql` — конвенция раскладки живёт в одном
  месте (LESSONS §55, урок 2).
- Autodoc: `object_type: table`, `object_key .../type/table/name/<n>`;
  граф строится штатно по файлам.
- `location` / `format_type` / `format_options` / `encoding` — отбрасываются
  (encoding у обычной таблицы — свойство БД, не DDL).
- DISTRIBUTED BY (решение USER_INPUT-1): при target=greenplum и пустом
  `distributed_by` пишется явный `DISTRIBUTED RANDOMLY`. Правило действует
  для ВСЕХ таблиц с незаданным ключом дистрибуции (не только конвертированных
  из external): в `_write_table_sql` ветка `if table.distributed_by:`
  дополняется else-веткой `DISTRIBUTED RANDOMLY`. Для target=postgres clause
  GP-only и по-прежнему не пишется.
- Провенанс (решение USER_INPUT-4): комментарий над CREATE TABLE —
  `-- converted from external table; source LOCATION: pxf://...; FORMAT: CUSTOM`.
  Влияет только на конвертированные таблицы.

### S3. CLI

```bash
db-pm yaml apply --yaml <f> --target-db-type <t> --output <dir> \
    --convert-external-to-tables
```

- bool-флаг, default off; имя длинное и самоописательное — стиле existing
  (`--no-run-subdir`, `--keep-rehearsal-db`).
- Итоговая строка: `Applied: schemas=..., objects=..., converted_external=N,
  output=...`.

### S4. GUI

| Файл | Изменение |
|---|---|
| `presentation/gui/actions/models.py:96` | `YamlApplySettings.convert_external_to_tables: bool = False` |
| `presentation/gui/actions/dialogs.py:377` | `YamlApplyDialog`: QCheckBox после combo «Целевой тип БД:»; кнопки — последними (LESSONS §43) |
| `presentation/gui/actions/cli.py:118` | `build_cli_yaml_apply`: добавляет `--convert-external-to-tables` при True |
| `presentation/gui/actions/registry.py:193` | `_make_yaml_apply_worker`: проброс в конструктор |
| `presentation/gui/widgets/workers.py:449` | `YamlApplyWorker(convert_external_to_tables=...)` → `service.run(...)` |

### S5. Результат

- `YamlApplyResult` += `converted_external_tables: int`.
- Существующее поле `skipped_external_tables` сохраняет смысл для postgres
  без флага.

## 4. USER_INPUT — открытые вопросы

### USER_INPUT-1: DISTRIBUTED BY для конвертированных таблиц (greenplum)

В YAML у external таблиц поля `distributed_by` нет; у обычных таблиц корпуса
оно часто `[]` — тогда код не пишет clause вовсе.



Ответ пользователя: 
  Использовать не только для конвертирующих таблиц, но и для таблиц, где ключ дистрибуция не указан.
- явный `DISTRIBUTED RANDOMLY` — 
- **→ Решение (принято):** явный `DISTRIBUTED RANDOMLY`. Область действия
  расширена пользователем: правило применяется не только к конвертированным
  external-таблицам, но и к ЛЮБЫМ таблицам с незаданным ключом дистрибуции
  (`distributed_by: []`) при target=greenplum.
- **Следствие (behavior change):** DDL ранее генерировавшихся таблиц с пустым
  `distributed_by` меняется — вместо отсутствия clause (GP default: hash по
  первой колонке) пишется явный `DISTRIBUTED RANDOMLY`. Существующий assert t2
  в `TestApplyLayout` (`)\nWITH (orientation=COLUMN);`) обновляется на
  `)\nWITH (orientation=COLUMN)\nDISTRIBUTED RANDOMLY;`.

### USER_INPUT-2: имя объекта

- **Рекомендация ИИ:** сохранить исходное имя 1:1 (`ext_w_staging_...`).
  Трассируемость, отсутствие коллизий, детерминированный compare с источником.
- Альтернатива: срезать префикс `ext_` / `ext_w_` — риски коллизий и
  расхождений при compare.
- **Ответ пользователя:**  принимаю рекомендацию
- **→ Решение (принято):** сохранить исходное имя 1:1 (`ext_w_staging_...`).

### USER_INPUT-3: GP→postgres с включённым флагом

Сейчас `--target-db-type postgres` на GP-YAML с external tables — ошибка.

- **Рекомендация ИИ:** флаг снимает запрет: конвертировать вместо ошибки.
  Это осмысленный сценарий портирования legacy GP-корпуса на PG (S1 уже даёт
  это бесплатно). Флаг OFF — прежняя ошибка.
- Альтернатива: флаг действует только при greenplum-таргете.
- **Ответ пользователя:**  принимаю рекомендацию
- **→ Решение (принято):** флаг снимает запрет GP→postgres — конвертация вместо
  ошибки; без флага прежняя `YamlApplyError`.

### USER_INPUT-4: след происхождения в SQL

- **Рекомендация ИИ:** комментарий над CREATE TABLE, например
  `-- converted from external table; source LOCATION: pxf://...; FORMAT: CUSTOM`.
  Не влияет на DDL/деплой (комментарий уходит в БД безвредно), но при review
  кодовой базы видно происхождение.
- Альтернатива: чистый DDL без следов external-прошлого.
- **Ответ пользователя:**  принимаю рекомендацию
- **→ Решение (принято):** комментарий провенанса над CREATE TABLE (source
  LOCATION + FORMAT) — только для конвертированных таблиц.

## 5. Проверки

Новые тесты в `tests/unit/test_yaml_project.py` (там уже живут
`TestYamlApplyValidation` / `TestApplyLayout` / `TestDeploySchemaSeeding`) +
GUI-контракты:

1. Флаг ON + greenplum: существует `<schema>/tables/table ext_x.sql`;
   `external_tables/` пуст; autodoc `object_type: table`; в SQL нет
   `LOCATION` / `FORMAT`; все колонки на месте (полнота по полям — LESSONS §55).
2. Флаг ON + postgres (при USER_INPUT-3 = да): ошибки нет, таблица
   сконвертирована.
3. Флаг OFF + postgres: прежняя `YamlApplyError` (регрессия).
4. Флаг OFF + greenplum: `external_tables/external_table x.sql` как раньше
   (регрессия).
5. `YamlApplyResult.converted_external_tables` корректен; флаг OFF → 0.
6. Коллизия имён ext/table → `YamlApplyError`.
7. Граф после конвертации содержит вершину table (через существующий путь
   `build_and_store` в `run()`).
8. CLI-контракт (CliRunner): флаг доходит до сервиса.
9. GUI: settings round-trip; `build_cli_yaml_apply` содержит флаг при True,
   не содержит при False (`tests/unit/test_action_cli.py`).
10. Расширенное правило USER_INPUT-1 (greenplum): обычная таблица с пустым
    `distributed_by` → в DDL явный `DISTRIBUTED RANDOMLY`; с непустым —
    прежний `DISTRIBUTED BY (...)`; target=postgres — clause отсутствует.
11. Обновить существующий assert t2 в `TestApplyLayout`
    (`tests/unit/test_yaml_project.py:813-814`) — см. USER_INPUT-1.

```bash
# Окружение: Device Guard блокирует .venv\Scripts\python.exe (LESSONS §68)
PYTHONPATH="src;.venv/Lib/site-packages" ~/AppData/Roaming/uv/python/cpython-3.13.*/python.exe \
    -m pytest tests/unit/ -q -p no:randomly
.venv/Scripts/ruff.exe check src/ tests/
```

## 6. Риски и ограничения

- **Behavior change (USER_INPUT-1):** все таблицы с пустым `distributed_by`
  при target=greenplum получают явный `DISTRIBUTED RANDOMLY` — DDL
  существующих корпусов (у которых раньше clause отсутствовал) меняется;
  GP default (hash по первой колонке) больше не используется молча.
- Roundtrip `yaml generate` → `yaml apply --convert...` → `yaml generate`:
  тип объекта меняется один раз (external → table); повторная конвертация
  уже нечего конвертировать — ожидаемо, не баг.
- У external-колонок в модели нет `default`-полей, переносимых из LOCATION —
  колонки переносятся как есть, ничего не синтезируется.
- `yaml generate` флагом не затрагивается (external остаются external).
- Hand-made YAML с коллизией имён — guard из S1.

## 7. NOT in scope

- Обратная конвертация (table → external).
- Копирование/загрузка данных из внешних источников.
- Эвристики синтеза `distributed_by` / `with_options` для конвертированных.
- Изменение формата YAML (поле «converted_from» и т.п. — провенанс только
  в SQL-комментарии, USER_INPUT-4).

## 8. Затрагиваемые файлы

| Файл | Изменение |
|---|---|
| `src/db_project_manager/application/yaml_apply_service.py` | `_convert_external_tables`, параметр `run()`, счётчик, провенанс-комментарий |
| `src/db_project_manager/presentation/cli/main.py` | `--convert-external-to-tables` |
| `src/db_project_manager/presentation/gui/actions/models.py` | поле settings |
| `src/db_project_manager/presentation/gui/actions/dialogs.py` | чекбокс |
| `src/db_project_manager/presentation/gui/actions/cli.py` | флаг в `build_cli_yaml_apply` |
| `src/db_project_manager/presentation/gui/actions/registry.py` | проброс в worker |
| `src/db_project_manager/presentation/gui/widgets/workers.py` | параметр `YamlApplyWorker` |
| `tests/unit/test_yaml_project.py`, `tests/unit/test_action_cli.py` | тесты |
| `README.md` | документация флага в секции yaml apply |

## 9. Куда дальше

- Закрыть USER_INPUT-1..4 → `Phase_15.8_final.md` (или сразу план+реализация).
- После реализации: `Phase_15.8_result.md`, обновить README и
  `_docs_/_phases_/` при закрытии фазы 15.
