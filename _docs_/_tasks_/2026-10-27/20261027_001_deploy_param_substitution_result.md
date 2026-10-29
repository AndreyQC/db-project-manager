# Подстановка параметров при деплое — result (20261027_001)

> Контекст:
> - `_tasks_/2026-10-27/20261027_001_deploy_param_substitution_plan.md` — согласованный план
> - `_checkpoints_/20261024_001_checkpoint.md` — предыдущий контекст инструмента

**Дата:** 2026-10-27 · **Статус:** Выполнено (инструментальная часть); этап
«потребитель hrdo_ods» — ждёт значений воркспейсов от пользователя.

---

## Что сделано

Механизм подстановки `{{param_name}}` при деплое — по схеме, согласованной в
диалоге (см. план). Коммит `3cd89c0`.

### Механизм

1. **Плейсхолдеры** `{{name}}` (jinja-стиль, пробелы внутри допускаются) в теле
   SQL-файлов заменяются значениями **до парсинга** — граф, дельта и
   исполняемые скрипты строятся уже на развёрнутых именах (параметризация имён
   схем не ломает `object_key` и рёбра).
2. **Источники значений** (приоритет по возрастанию):
   `dbpm.params.yaml` кодовой базы (секция `parameters:`) →
   `params:` в `connections/*.yaml` → `--param name=value` CLI
   (повторяемый, на `graph build` / `deploy validate` / `deploy plan` /
   `deploy apply`). Семантика проброса: переданный словарь — всегда
   **переопределения поверх файла** (файл — базовый слой даже при пустых
   переопределениях; найдено живым прогоном CLI).
3. **Крипта**: значение вида `crypto__<ENV_VAR>__<token>` расшифровывается
   готовым `crypto_util` (Fernet, `ENVOS_CRYPTO_01`) последним шагом; обычное
   значение берётся как есть. Расшифровка строгая: отсутствующий env-ключ —
   ошибка деплоя, не молчаливый токен. В логи/ошибки значения не попадают —
   только имена параметров и файлы.
4. **Fail-fast**: неразрешённый плейсхолдер валит `graph build` /
   `deploy validate` / `plan` / `apply` с агрегированным списком
   «параметр → файлы» (`UnresolvedParamError`) — один прогон показывает ВСЕ
   проблемы; сбор после цикла разбора, поэтому пер-файловый try/except
   парсера пропуски не глотает.
5. **Autodoc-заголовок из подстановки исключён** (identity уже развёрнута RE);
   `dbpm.manifest.json` не затронут.

### Карта кода

| Область | Файл | Что |
|---|---|---|
| Резолвер | `infrastructure/params/resolver.py` | load/effective, крипта, ошибки |
| Подстановка | `infrastructure/params/substitution.py` | `ParamSubstitutor`, regex, autodoc-span |
| Парсер | `infrastructure/parsing/pg_sql_parser.py`, `base.py` | подстановка в `_parse_file`, fail-fast после цикла вершин |
| Граф | `application/graph_service.py` | `build/build_and_store/deploy_order(..., params)` |
| Снапшот | `infrastructure/diff/snapshot.py` | тела для sql_hash подставляются теми же значениями |
| Compare | `application/compare_service.py` | `run(..., codebase_params)` → DIR-стороны |
| Дельта | `application/delta_service.py` | `build_plan/write_artifacts(..., params)`, тела артефактов |
| Validate | `application/deploy_service.py` | `run(..., params)`, `_deploy_object` подставляет перед исполнением |
| Apply | `application/deploy_apply_service.py` | `plan/apply(..., params)`, rehearsal-pipeline, `_fresh_compare` |
| Подключение | `domain/connection.py` | `params: dict[str, str]` (крипта расшифровывается ConnectionStore'ом) |
| CLI | `presentation/cli/main.py` | `--param` (4 команды), `_parse_cli_params` (режет по первому `=`), обработка `UnresolvedParamError` (exit 2) |

GUI/MCP изменений не требуют: `params=None` → файловые значения по умолчанию.

## Решения, принятые при реализации

- **Семантика проброса** — «переопределения поверх файла», а не «готовый
  словарь как есть»: иначе пустой `--param`-набор из CLI подавлял файловые
  значения (найдено на живом CLI-смоуке, до коммита).
- Подстановка применяется и к файлам с `build: false` — строгий fail-fast
  единообразен (пользователь управляет значениями, ложных блокировок нет:
  нет `{{...}}` — нет и требований).
- Seed/`__migrations/pre|post` **не параметризуются** (MVP): это пользовательские
  скрипты данных; решено не смешивать схемную подстановку с данными.
- `{{9x}}`, `{{a.b}}` плейсхолдерами не считаются — остаются в тексте как есть
  (регресс в тесте фиксирует).

## Проверки

- `uv run pytest` — **1341 passed** (было 1320; +21 новый в
  `tests/unit/test_param_substitution.py`: резолвер, крипта, приоритеты,
  autodoc-exclusion, агрегированный fail-fast, рёбра по развёрнутым именам,
  snapshot-тела, `_deploy_object`, ConnectionConfig), ruff clean.
- Обновлены стабы трёх существующих тест-файлов под новые сигнатуры
  (`params`/`codebase_params`).
- Живой CLI-смоук (tmp-кодовая база с внешней таблицей GP):
  значения из `dbpm.params.yaml` → деплой-граф строится; `--param
  pxf_server=ws42` переопределяет; без значений — fail-fast с сообщением и
  exit 2; `--param broken` — понятная ошибка формата.

## Не сделано / следующее

1. **Потребитель `hrdo_ods`**: параметризовать LOCATION внешних таблиц
   (`{{hdfs_schema}}`, `{{pxf_server}}`) и прогнать деплой в две схемы —
   нужны от пользователя списки значений по воркспейсам.
2. CLI-флаг `--param` для `compare run` — не добавлялся (снапшот DIR-стороны
   и так подставляет файловые значения; переопределения для compare — по
   запросу).
3. Реквизит `db-pm params init` (генерация заготовки `dbpm.params.yaml`) —
   опционально, не делалось.
4. Открытые вопросы плана (identity при параметризации схем, sql_hash от
   значений) — не закрыты, сознательно; compare-картина зафиксирована в
   BACKLOG (P3, renamed-объекты между средами).

---

*Файл: `_docs_/_tasks_/2026-10-27/20261027_001_deploy_param_substitution_result.md`*
