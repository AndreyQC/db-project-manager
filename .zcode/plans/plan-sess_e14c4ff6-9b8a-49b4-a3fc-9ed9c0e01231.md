# Phase 6 — QualifyRefs: автоматическая квалификация bare-ссылок в codebase

## Контекст

После Phase 5 (extensions + db settings) и hotfix про `_qualify_default_schema` (lesson 34, 35) осталась проблема: **внутри тел функций/процедур/views** reverse-engineer не трогает идентификаторы. Разработчик мог написать `WHERE p.company_id = sp_company_id_for_user(p_user_id)` — без `qr.` префикса. На временной БД с дефолтным `search_path` это падает с `UndefinedFunction`.

Phase 6 закрывает это пост-процессором: после reverse-engineer пройти все `.sql` файлы, найти bare-ссылки на известные объекты (по графу зависимостей) и квалифицировать их схемой.

**Все design-решения уже собраны через AskUserQuestion:**
- Охват: **functions/procedures + tables/views в FROM/JOIN**
- Триггер: **оба** (auto после reverse + ручная команда `db-pm qualify-refs`)
- Ambiguous имена: **skip + warning + отдельный файл-протокол**
- Парсер: **regex + токенайзер** (обобщение `_qualify_default_schema`)
- Протокол: **`_qualify_report.md` в корне codebase**

## Архитектура

```
codebase/                         ← после reverse-engineer
├── _qualify_report.md            ← НОВЫЙ: протокол изменений/пропусков
├── extensions/...
├── settings/...
└── <schema>/<kind>/file.sql      ← изменён: bare refs → schema.qualified
                                    autodoc += qualify_report: [...]
```

Новый сервис: `application/qualify_refs_service.py` (`QualifyRefsService`).
Новый CLI: `db-pm qualify-refs --dir <path> [--dry-run]`.
Новый autodoc helper: `update_header` в `infrastructure/sql/autodoc.py`.

## Шаги (P6.S01–P6.S07)

### P6.S01. `update_header` helper в autodoc + тесты

**Файлы:** `src/db_project_manager/infrastructure/sql/autodoc.py`, `tests/unit/test_autodoc.py`

Разрыв gap из разведки: `ensure_header` early-return'ит при наличии header'а. Нужна функция, которая **обновляет** существующий header:

```python
def update_header(script: str, mutator: Callable[[dict[str, Any]], None]) -> str:
    """Apply *mutator* to the parsed autodoc metadata and re-render in place.
    
    No-op if no header present. Mutator mutates the dict in place.
    """
    if MARKER_OPEN not in script or MARKER_CLOSE not in script:
        return script
    metadata = extract_header(script)
    if metadata is None:
        return script
    mutator(metadata)
    # Re-render only the header block, preserving the comment wrapper + body.
    new_header_yaml = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False)
    # Replace the YAML body between markers.
    open_idx = script.index(MARKER_OPEN) + len(MARKER_OPEN)
    close_idx = script.index(MARKER_CLOSE)
    return script[:open_idx] + new_header_yaml + script[close_idx:]
```

**Тесты:** mutate existing header (add `qualify_report: [...]`), preserve body SQL, no-op when no header.

**Коммит:** `feat(autodoc): update_header helper to mutate existing header (P6.S01)`

### P6.S02. `QualifyRefsService` — core logic

**Файлы:** `src/db_project_manager/application/qualify_refs_service.py` (новый), `tests/unit/test_qualify_refs_service.py` (новый)

Ключевые компоненты:

1. **Индекс объектов** — `dict[bare_name, object_schema]`, но только для **уникальных** имён (имя → ровно одна схема). Имена, встречающиеся в нескольких схемах, → `ambiguous: set[bare_name]` (попадают в протокол как warning, не квалифицируются).

```python
def build_name_to_schema_index(graph) -> tuple[dict[str, str], set[str]]:
    """Return (unique_names, ambiguous_names)."""
    by_name: dict[str, set[str]] = {}
    for v in graph.vertices.values():
        if v.object_name and v.object_type in QUALIFIABLE_TYPES:
            by_name.setdefault(v.object_name, set()).add(v.object_schema or "public")
    unique = {n: schemas.pop() for n, schemas in by_name.items() if len(schemas) == 1}
    ambiguous = {n for n, schemas in by_name.items() if len(schemas) > 1}
    return unique, ambiguous
```

`QUALIFIABLE_TYPES = {"table", "view", "materialized_view", "function", "procedure", "sequence"}` — исключаем schema/extension/database_setting (они либо глобальны, либо не вызываются).

2. **Regex для детекта bare refs на raw text** (обобщение `_qualify_default_schema`):

```python
# Function/procedure call: name(  (но не после точки)
_FUNC_CALL_RE = re.compile(r"(?<![.\w])(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\(")

# Table/view in FROM/JOIN: FROM name | JOIN name
_FROM_JOIN_RE = re.compile(
    r"\b(?:from|join)\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\b",
    re.IGNORECASE,
)
```

3. **Алгоритм per-file:**
   - Прочитать raw text
   - Strip autodoc header (работать только на SQL body)
   - Найти все match'и обоих regex'ов
   - Для каждого match: проверить, что `name` ∈ unique index, **не** в reserved keywords (`token.upper() in get_reserved()`), не self-reference
   - Заменить `name` → `schema.name` **только** если следующий/предыдущий символ не `.` (уже qualified)
   - Собрать `qualify_report: list[str]` уникальных имён, которые были квалифицированы

4. **Запись:** raw text с подстановками + `update_header(script, lambda m: m.setdefault("qualify_report", []).extend(changes))`.

**Тесты (parametrized, по образцу `test_qualify_default_schema`):**
- function call bare → qualified (`sp_x()` → `qr.sp_x()`)
- function call already qualified → unchanged (`qr.sp_x()`)
- reserved keyword → skip (`SELECT *`, `FROM WHERE` edge cases)
- table in FROM → qualified (`FROM users` → `FROM qr.users`)
- table aliased (`FROM users u` → `FROM qr.users u`)
- ambiguous name → skip + tracked in ambiguous set
- self-reference → skip
- reserved keyword as identifier → skip (`count(` не квалифицируется)

**Коммит:** `feat(qualify-refs): QualifyRefsService core logic (P6.S02)`

### P6.S03. `_qualify_report.md` протокол

**Файл:** `src/db_project_manager/application/qualify_refs_service.py` (часть), `tests/unit/test_qualify_refs_service.py`

Протокол — markdown-таблица в корне codebase:

```markdown
# Qualify Refs Report

Сгенерировано: 2026-07-20T19:30:00
Codebase: D:/.../qr_pamyat

## Изменено (qualified)

| Файл | Тип объекта | Qualified refs |
|------|------------|----------------|
| qr/functions/function sp_x.sql | function | sp_company_id_for_user, sp_validate |
| qr/views/view active_products.sql | view | company_products, users |

## Пропущено: ambiguous (одинаковые имена в разных схемах)

| Имя | Схемы | Файлы |
|-----|-------|-------|
| users | public, app | qr/functions/function sp_x.sql, app/views/view list.sql |

## Пропущено: reserved keyword / self-ref

(только summary count; детали в логе loguru)
```

Через `dataclass QualifyReport` собирается в сервисе, рендерится в markdown. Логирование warning'ов через `loguru` (по образцу проекта) + отдельный файл.

**Тесты:** report generation из mock-данных; пустой codebase → минимальный report; ambiguous и reserved → в правильных секциях.

**Коммит:** `feat(qualify-refs): _qualify_report.md protocol with ambiguous/reserved tracking (P6.S03)`

### P6.S04. Hook в `ReverseEngineerService.run` (auto)

**Файлы:** `src/db_project_manager/application/reverse_engineer.py`, `tests/unit/test_reverse_engineer.py`

В `ReverseEngineerService.run` между `generate_scripts` и `"Готово"`:

```python
self._emit(progress, f"Генерация SQL-скриптов в: {target}", 2, 5)
result = self.generator.generate_scripts(structure, target, object_catalog=conn_cfg.database)
self._emit(progress, "Квалификация ссылок...", 3, 5)
self.qualify_service.run(result)
self._emit(progress, "Готово", 5, 5)
```

Конструктор `ReverseEngineerService.__init__` получает опциональный `qualify_service: QualifyRefsService | None = None` (default — инстанс; для тестов — mock/None для отключения).

**Тесты:** `test_reverse_engineer.py` расширяется: после `run()` проверяем, что `_qualify_report.md` создан и хотя бы один файл модифицирован (если в `sample_structure.json` есть bare ref).

**Коммит:** `feat(reverse): auto-qualify bare refs after generation (P6.S04)`

### P6.S05. CLI команда `db-pm qualify-refs`

**Файл:** `src/db_project_manager/presentation/cli/main.py`

```python
@app.command("qualify-refs")
def qualify_refs(
    dir: Annotated[Path, typer.Option("--dir", help="Codebase root")] = Path("."),
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Report only, no writes")] = False,
) -> None:
    """Найти bare refs и квалифицировать их схемой (per объектный граф)."""
    ...
```

В dry-run: сервис только собирает report и печатает в stdout, не пишет в `.sql` файлы (но `_qualify_report.md` всё равно пишет — это read-only относительно исходников, но не относительно report'а).

**Тесты:** через Typer's `CliRunner` (если есть; иначе просто вызов функции).

**Коммит:** `feat(cli): db-pm qualify-refs command with --dry-run (P6.S05)`

### P6.S06. Integration test

**Файл:** `tests/integration/test_qualify_refs_e2e.py` (новый)

Сценарий: reverse-engineer → graph build → проверить что bare refs в функциях квалифицированы и автодок помечен. Использует `codebase_sample` или новый fixture с intentional bare refs.

**Коммит:** `test(integration): qualify-refs end-to-end (P6.S06)`

### P6.S07. Документация и закрытие фазы

- `LESSONS_LEARNED.md` §36 — урок про пост-процессор и tradeoff regex-vs-AST
- `_tasks_/BACKLOG.md` — отметить follow-up: AST-based qualify через sqlglot (если regex даст false positives в проде)
- `_checkpoints_/20260720_003_checkpoint.md` — Phase 6 complete
- Vision/plan docs минимально (один `phase_06/001_plan_phase_06.md` без draft/final цикла, т.к. все design-решения уже собраны)

**Коммиты:** `docs(...)` раздельно.

## Зависимости шагов

```
S01 (update_header) → S02 (service) → S03 (report) → S04 (reverse hook)
                                                  → S05 (CLI)
                                                  → S06 (integration)
S01–S06 → S07 (docs)
```

## Критерий готовности

- `uv run pytest tests/unit/ -q` зелёный (+~25 новых тестов)
- `uv run ruff check src/ tests/` чисто
- `db-pm reverse-engineer` на реальной БД `qr_pamyat` → `_qualify_report.md` создан, `sp_company_id_for_user` квалифицирован в `qr.sp_company_id_for_user`
- `db-pm deploy validate` проходит на ранее падавшей функции

## Известные ограничения (в §36 уроках)

- Regex не понимает SQL-синтаксис → возможны false positives/negatives в сложных кейсах (CTE, подзапросы, dynamic SQL). Для MVP приемлемо; warning'и в `_qualify_report.md` подсветят.
- Не квалифицируем внутри строковых литералов и `$function$ ... $function$` тел (сложно regex'ом) — TODO через sqlglot.
- Ambiguous имена остаются bare (пользователь решает руками).