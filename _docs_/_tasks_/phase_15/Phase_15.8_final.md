# Phase 15.8 (final): yaml apply — конвертация external tables в обычные

> Контекст:
> - `_docs_/_tasks_/phase_15/Phase_15.8_draft.md` — драфт с обсуждением USER_INPUT-1..4 (история)
> - `_docs_/_tasks_/phase_13/Phase_13.md` — базовая функциональность `yaml generate` / `yaml apply` (CD-13b)
> - `LESSONS_LEARNED.md` §53–§58 (Phase 13), §55 (конвенция раскладки в одном месте)
> - `_docs_/_checkpoints_/20260908_001_checkpoint.md` — текущий статус проекта

**Дата:** 2026-09-08
**Статус:** final (все USER_INPUT закрыты 2026-09-08, подтверждены пользователем)
**Заказчик сценария:** legacy Greenplum-корпус `Legacy_dp_dev.yml` (LM.HRDO-2.DEV, 2026-09-08_legacy_greenplum)

---

## 1. Задача

`db-pm yaml apply` получает новый режим «конвертировать внешние таблицы в
обычные» — CLI-флаг + чекбокс GUI. При включении все `external_tables` из
YAML-проекта превращаются в обычные таблицы: колонки 1:1, имя сохраняется,
external-специфичные поля (`location` / `format_type` / `format_options` /
`encoding`) отбрасываются, в SQL остаётся комментарий-провенанс.

## 2. Принятые решения (закрытые USER_INPUT)

### Р-1. DISTRIBUTED RANDOMLY для всех таблиц без ключа дистрибуции

При `--target-db-type greenplum` и пустом `distributed_by` в DDL пишется явный
`DISTRIBUTED RANDOMLY` — **не только для конвертированных external-таблиц, но
и для любых обычных таблиц, у которых ключ дистрибуции не указан** (решение
пользователя, расширившее изначальный вопрос только про конвертированные).

- Behavior change: таблицы, раньше рендерившиеся без clause (GP default —
  hash по первой колонке), теперь получают явный RANDOMLY.
- target=postgres не затронут: clause GP-only, не пишется.
- Существующий assert t2 в `TestApplyLayout` (`tests/unit/test_yaml_project.py`)
  обновляется: `)\nWITH (orientation=COLUMN)\nDISTRIBUTED RANDOMLY;`.

### Р-2. Имя объекта — сохранить 1:1

Конвертированная таблица остаётся под исходным именем (`ext_w_staging_...`).
Трассируемость, отсутствие коллизий, детерминированный compare с источником.

### Р-3. Флаг снимает запрет GP→postgres

`--convert-external-to-tables --target-db-type postgres` на GP-YAML с
external-таблицами: конвертация вместо прежней `YamlApplyError`. Без флага —
прежняя ошибка (регрессия фиксируется тестом).

### Р-4. Провенанс — SQL-комментарий

Над CREATE TABLE конвертированной таблицы комментарий вида:

```sql
-- converted from external table; source LOCATION: pxf://...; FORMAT: CUSTOM
```

Не влияет на DDL/деплой; только для конвертированных таблиц.

## 3. Дизайн реализации

### S1. Конвертация — in-memory трансформация YamlProject до валидации

- `YamlApplyService._convert_external_tables(project) -> tuple[YamlProject, dict]`:
  чистая функция (пересборка pydantic-моделей, без мутации входа). Для каждой
  схемы каждый `YamlExternalTable` → `YamlTable(name, columns,
  distributed_by=[], with_options={})`; `external_tables` схемы очищается.
  Возвращает также map `(schema_name, table_name) -> provenance-строка` для Р-4.
- Порядок в `run()`: parse → **convert (если флаг)** → `_validate` → write.
  Конвертация до валидации бесплатно даёт Р-3: external-списки пусты, запрет
  не срабатывает.
- Guard: коллизия имени ext-таблицы с обычной таблицей в одной схеме →
  `YamlApplyError`.

### S2. DDL и запись

- Запись через существующий `_write_table_sql` (раскладка
  `<schema>/tables/table <name>.sql`, autodoc `object_type: table`) — конвенция
  раскладки в одном месте (LESSONS §55).
- `_write_table_sql`, ветка greenplum: `if table.distributed_by: ...` получает
  else — `DISTRIBUTED RANDOMLY` (Р-1).
- Провенанс (Р-4): комментарий вставляется в начало sql_body (после autodoc-
  заголовка, до первого стейтмента) только для таблиц из provenance-map.

### S3. CLI

```bash
db-pm yaml apply --yaml <f> --target-db-type <t> --output <dir> \
    --convert-external-to-tables
```

bool-флаг, default off. Итоговая строка дополняется:
`Applied: schemas=..., objects=..., converted_external=N, output=...`.

### S4. GUI

| Файл | Изменение |
|---|---|
| `presentation/gui/actions/models.py` | `YamlApplySettings.convert_external_to_tables: bool = False` |
| `presentation/gui/actions/dialogs.py` | `YamlApplyDialog`: QCheckBox «Конвертировать внешние таблицы в обычные» после combo; кнопки последними (LESSONS §43) |
| `presentation/gui/actions/cli.py` | `build_cli_yaml_apply` добавляет `--convert-external-to-tables` при True |
| `presentation/gui/actions/registry.py` | `_make_yaml_apply_worker` пробрасывает флаг |
| `presentation/gui/widgets/workers.py` | `YamlApplyWorker(convert_external_to_tables=...)` → `service.run(...)` |

### S5. Результат

`YamlApplyResult` += `converted_external_tables: int` (0 при выключенном
флаге). `skipped_external_tables` сохраняет смысл для postgres без флага.

## 4. Проверки

Тесты в `tests/unit/test_yaml_project.py` + GUI-контракты в
`tests/unit/test_action_cli.py`:

1. Флаг ON + greenplum: `<schema>/tables/table ext_x.sql` существует;
   `external_tables/` пуст; autodoc `object_type: table`; в SQL нет
   `LOCATION`/`FORMAT`; все колонки на месте (полнота по полям, LESSONS §55).
2. Флаг ON + greenplum: провенанс-комментарий с LOCATION/FORMAT (Р-4).
3. Флаг ON + postgres: ошибки нет, таблица сконвертирована (Р-3).
4. Флаг OFF + postgres: прежняя `YamlApplyError` (регрессия).
5. Флаг OFF + greenplum: `external_tables/external_table x.sql` как раньше.
6. `converted_external_tables`: N при флаге, 0 без флага.
7. Коллизия имён ext/table в одной схеме → `YamlApplyError`.
8. Р-1 greenplum: пустой `distributed_by` → явный `DISTRIBUTED RANDOMLY`
   (обычная таблица и конвертированная); непустой → `DISTRIBUTED BY (...)`;
   postgres → clause отсутствует.
9. CLI-контракт (CliRunner): флаг доходит до сервиса.
10. GUI: settings round-trip; `build_cli_yaml_apply` содержит флаг при True,
    не содержит при False.
11. Обновление существующего assert t2 в `TestApplyLayout` (Р-1).

```bash
# Окружение: Device Guard блокирует .venv\Scripts\python.exe (LESSONS §68)
PYTHONPATH="src;.venv/Lib/site-packages" ~/AppData/Roaming/uv/python/cpython-3.13.*/python.exe \
    -m pytest tests/unit/ -q -p no:randomly
.venv/Scripts/ruff.exe check src/ tests/
```

## 5. Риски и ограничения

- **Behavior change (Р-1):** DDL существующих корпусов с пустым
  `distributed_by` меняется (явный RANDOMLY вместо молчаливого GP default) —
  осознанное решение пользователя.
- Roundtrip `yaml generate` → `yaml apply --convert...` → `yaml generate`:
  тип объекта меняется один раз; повторная конвертация нечего конвертировать.
- Колонки переносятся как есть; из LOCATION ничего не синтезируется.
- `yaml generate` флагом не затрагивается.

## 6. NOT in scope

- Обратная конвертация (table → external), загрузка данных из внешних источников.
- Эвристики синтеза `distributed_by` / `with_options` (RANDOMLY — не эвристика,
  а явный выбор пользователя).
- Изменение формата YAML (провенанс только в SQL-комментарии).

## 7. Затрагиваемые файлы

| Файл | Изменение |
|---|---|
| `src/db_project_manager/application/yaml_apply_service.py` | `_convert_external_tables`, параметр `run()`, счётчик, Р-1 else-ветка, провенанс |
| `src/db_project_manager/presentation/cli/main.py` | `--convert-external-to-tables`, вывод |
| `src/db_project_manager/presentation/gui/actions/models.py` | поле settings |
| `src/db_project_manager/presentation/gui/actions/dialogs.py` | чекбокс |
| `src/db_project_manager/presentation/gui/actions/cli.py` | флаг в `build_cli_yaml_apply` |
| `src/db_project_manager/presentation/gui/actions/registry.py` | проброс в worker |
| `src/db_project_manager/presentation/gui/widgets/workers.py` | параметр `YamlApplyWorker` |
| `tests/unit/test_yaml_project.py`, `tests/unit/test_action_cli.py` | тесты (раздел 4) |
| `README.md` | документация флага в секции yaml apply |

## 8. Куда дальше

Реализация по этому final → `Phase_15.8_result.md` (что сделано, проверки,
коммиты) → README. Драфт не удаляется (история обсуждения).
