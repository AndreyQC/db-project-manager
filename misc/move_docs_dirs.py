#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Перемещение документационных каталогов в _docs_ и обновление ссылок.

Что делает скрипт:
  _checkpoints_  ->  _docs_/_checkpoints_
  _phases_       ->  _docs_/_phases_
  _tasks_        ->  _docs_/_tasks_

  1. Во всех текстовых файлах репозитория заменяет упоминания этих каталогов
     на новые пути (добавляет префикс ``_docs_/``).
  2. (Опционально) физически переносит три каталога внутрь ``_docs_``.

Безопасность:
  * По умолчанию работает в режиме dry-run (только отчёт, ничего не меняет).
  * ``--apply``       — записать замены в файлы.
  * ``--move-dirs``   — переместить каталоги (выполнять ПОСЛЕ ``--apply``,
                        иначе внутренние относительные ссылки в перемещённых
                        файлах на момент замены ещё не обновлены — впрочем,
                        замены идёмпотентны, так что порядок не критичен).
  * ``--show-diff``   — показать построчные примеры замен в отчёте.

Идемпотентность: замена использует negative lookbehind ``(?<!_docs_/)``,
поэтому повторный запуск не дублирует префикс ``_docs_/_docs_/...``.

Использование::

    python misc/move_docs_dirs.py                 # сухой прогон + сводка
    python misc/move_docs_dirs.py --show-diff     # сухой прогон с примерами строк
    python misc/move_docs_dirs.py --apply         # применить замены в файлах
    python misc/move_docs_dirs.py --move-dirs     # переместить каталоги
    python misc/move_docs_dirs.py --apply --move-dirs   # всё сразу

Корень репозитория определяется автоматически (по расположению .git вверх от
этого файла); переопределить через ``--root PATH``.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# --- Конфигурация -----------------------------------------------------------

# Перемещаемые каталоги и целевой родитель.
DIRS_TO_MOVE: tuple[str, ...] = ("_checkpoints_", "_phases_", "_tasks_")
NEW_PARENT = "_docs_"

# Каталоги, которые не сканируем при поиске упоминаний (подстраховка поверх
# учёта .gitignore через ``git ls-files``). node_modules/target/dist могут
# содержать десятки тысяч файлов — обход их делает скрипт «зависшим».
EXCLUDE_DIRS: set[str] = {".git", "node_modules", "target", "dist", "dist-ssr", ".pytest_cache", ".ruff_cache", ".venv", "plans"}

# Расширения файлов, которые не имеет смысла открывать как текст.
EXCLUDE_SUFFIXES: set[str] = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".svg",
    ".zip", ".gz", ".tar", ".lock", ".woff", ".woff2", ".ttf", ".eot",
    ".mp3", ".mp4", ".wav", ".pdf", ".exe", ".dll", ".so", ".dylib",
    ".pdb", ".wasm",
}

# Файлы, которые лежат в .gitignore (git ls-files их не вернёт), но в которых
# тем не менее нужно обновить ссылки. Относительно корня репозитория.
EXPLICIT_EXTRA_FILES: tuple[str, ...] = (
    ".zcode/plans/plan-sess_5a3877c7-1aa6-4aef-be1d-e62dbdbe9edf.md",
    ".zcode/plans/plan-sess_6fb22274-102a-4600-ab3b-a67f7bbf5507.md",
)

# Сколько изменённых строк показывать на один файл в режиме --show-diff.
DIFF_LINES_PER_FILE = 5

# Размер порции для эвристики «бинарный/текстовый».
TEXT_SNIFF_BYTES = 4096


# --- Утилиты ----------------------------------------------------------------

def detect_repo_root(start: Path) -> Path:
    """Идём вверх от ``start``, пока не найдём каталог с ``.git``."""
    current = start.resolve()
    for candidate in [current, *current.parents]:
        if (candidate / ".git").exists():
            return candidate
    return start.resolve()


def looks_like_text(path: Path) -> bool:
    """Эвристика: файл считается бинарным, если в начале есть NUL-байт."""
    try:
        with path.open("rb") as fh:
            chunk = fh.read(TEXT_SNIFF_BYTES)
    except OSError:
        return False
    if not chunk:
        return True  # пустой файл — текстовый, но замен в нём не будет
    return b"\x00" not in chunk


def is_special_or_binary_path(path: Path) -> bool:
    """Пропускаем не-регулярные файлы (FIFO/socket/device) и нерелевантные
    расширения. Открытие FIFO/device через open() могло бы зависнуть."""
    try:
        if not path.is_file():  # заодно разрешает симлинки на файлы
            return True
        # is_file() следует по symlink; отсекаем ссылки на каталоги/устройства
        if path.is_dir() or path.is_block_device() or path.is_char_device() \
                or path.is_fifo() or path.is_socket():
            return True
    except OSError:
        return True
    if path.suffix.lower() in EXCLUDE_SUFFIXES:
        return True
    return False


def iter_candidate_files(root: Path):
    """Источник списка файлов — ``git ls-files`` (быстро и учитывает .gitignore).

    Если git недоступен или репозиторий не инициализирован, используем обход
    с пропуском EXCLUDE_DIRS. Сам этот скрипт исключаем намеренно: он содержит
    ``DIRS_TO_MOVE`` с «голыми» именами каталогов и описания их в docstring.
    Если переписать их на ``_docs_/<name>``, логика поиска источников/целей
    перемещения сломается (``root / "_docs_/_checkpoints_"`` — двойной путь).

    Файлы, которые ``git ls-files`` не вернул (например, игнорируемые), не
    сканируем: наши ссылки живут в трекаемых исходниках и документации.
    """
    self_path = Path(__file__).resolve()

    seen: set[Path] = set()
    for rel in git_tracked_files(root):
        path = (root / rel)
        try:
            resolved = path.resolve()
            if resolved == self_path or resolved in seen:
                continue
        except OSError:
            continue
        if is_special_or_binary_path(path):
            continue
        if any(part in EXCLUDE_DIRS for part in path.parts):
            continue
        seen.add(resolved)
        yield path

    # Старые планы .zcode лежат в .gitignore и не возвращаются git'ом, но мы
    # договорились обновлять ссылки в них тоже. Добавляем явно.
    for plan in EXPLICIT_EXTRA_FILES:
        path = (root / plan)
        try:
            resolved = path.resolve()
            if resolved == self_path or resolved in seen:
                continue
        except OSError:
            continue
        if not path.is_file() or is_special_or_binary_path(path):
            continue
        seen.add(resolved)
        yield path


def git_tracked_files(root: Path):
    """Возвращает относительные пути из ``git ls-files`` или [] при неудаче.

    Обрабатывает два случая: из ``git ls-files`` приходит либо ``<path>`` для
    файлов, либо ``<path>/\t<stage>\t<hash>`` для конфликтующих стадий индекса
    (``git ls-files --unmerged``). Берём только первый столбец.
    """
    import subprocess
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "ls-files"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(root),
        )
    except (FileNotFoundError, OSError):
        return []
    if proc.returncode != 0:
        return []
    for line in proc.stdout.splitlines():
        rel = line.split("\t", 1)[0].strip()
        if rel:
            yield rel


def make_pattern(name: str) -> re.Pattern[str]:
    """
    Паттерн, совпадающий с ``name``, только если ему НЕ предшествует
    ``NEW_PARENT + "/"``. lookbehind фиксированной длины — валиден в Python re.
    """
    lookbehind = re.escape(NEW_PARENT + "/")
    return re.compile(r"(?<!" + lookbehind + r")" + re.escape(name))


def apply_replacements(text: str, patterns: list[tuple[str, re.Pattern[str]]]):
    """
    Возвращает (new_text, total_count, per_name_counts, per_name_examples).

    examples: {name: [старая_строка, ...]} — примеры строк, где были замены
    (без дубликатов, до DIFF_LINES_PER_FILE на имя).
    """
    total = 0
    per_name_counts: dict[str, int] = {}
    per_name_examples: dict[str, list[str]] = {}
    current = text
    for name, pat in patterns:
        replacement = NEW_PARENT + "/" + name
        new_text, n = pat.subn(replacement, current)
        if n:
            per_name_counts[name] = n
            total += n
            # Соберём примеры изменённых строк.
            seen: list[str] = []
            for line in new_text.splitlines():
                if replacement in line and len(seen) < DIFF_LINES_PER_FILE:
                    seen.append(line.strip())
            per_name_examples[name] = seen
        current = new_text
    return current, total, per_name_counts, per_name_examples


def read_text(path: Path) -> str | None:
    """Читаем UTF-8 без трансляции переносов строк (сохраняем CRLF/LF)."""
    try:
        with path.open("r", encoding="utf-8", newline="") as fh:
            return fh.read()
    except (UnicodeDecodeError, OSError):
        return None


def write_text(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def move_directory(src: Path, dest_parent: Path) -> str:
    """
    Переносим ``src`` в ``dest_parent / src.name``.
    Возвращает человекочитаемый статус.
    """
    dest_parent.mkdir(parents=True, exist_ok=True)
    dest = dest_parent / src.name
    if dest.exists():
        if src.resolve() == dest.resolve():
            return f"уже на месте: {dest}"
        return f"ПРОПУСК (цель занята): {dest}"
    src.rename(dest)
    return f"перемещено: {src.name} -> {dest}"


# --- Основные шаги ----------------------------------------------------------

def step_update_files(root: Path, apply: bool, show_diff: bool) -> int:
    """Обновляем упоминания в файлах. Возвращает кол-во изменённых файлов."""
    patterns = [(name, make_pattern(name)) for name in DIRS_TO_MOVE]

    changed_files = 0
    total_replacements = 0
    grand_counts: dict[str, int] = {name: 0 for name in DIRS_TO_MOVE}

    print("=" * 78)
    print(" ШАГ 1: обновление упоминаний в файлах")
    print(f"   режим: {'APPLY (запись)' if apply else 'DRY-RUN (только отчёт)'}")
    print("=" * 78)

    for path in iter_candidate_files(root):
        text = read_text(path)
        if text is None:
            continue
        new_text, total, counts, examples = apply_replacements(text, patterns)
        if total == 0:
            continue

        rel = path.relative_to(root).as_posix()
        changed_files += 1
        total_replacements += total
        for name, n in counts.items():
            grand_counts[name] += n

        breakdown = ", ".join(f"{name}={n}" for name, n in counts.items())
        print(f"  [{total:3d}] {rel}   ({breakdown})")

        if show_diff:
            for name, lines in examples.items():
                replacement = f"{NEW_PARENT}/{name}"
                for line in lines:
                    # покажем обрезанную строку с маркером новой ссылки
                    snippet = line if len(line) <= 140 else line[:137] + "..."
                    print(f"        • {snippet}")
                print()

    print("-" * 78)
    print(f" Файлов с заменами: {changed_files}")
    print(f" Всего замен:       {total_replacements}")
    for name in DIRS_TO_MOVE:
        print(f"   - {name:<16} {grand_counts[name]}")
    if not apply and changed_files:
        print("\n Это был DRY-RUN. Примените: python misc/move_docs_dirs.py --apply")
    elif apply and changed_files:
        print("\n Замены ЗАПИСАНЫ в файлы.")
    elif changed_files == 0:
        print("\n Упоминаний не найдено — обновление не требуется.")
    print()
    return changed_files


def step_move_dirs(root: Path, do_move: bool) -> None:
    """Перемещаем физические каталоги."""
    print("=" * 78)
    print(" ШАГ 2: перемещение каталогов")
    print(f"   режим: {'MOVE (перенос)' if do_move else 'DRY-RUN (план)'}")
    print("=" * 78)

    dest_parent = root / NEW_PARENT
    if do_move:
        dest_parent.mkdir(parents=True, exist_ok=True)

    for name in DIRS_TO_MOVE:
        src = root / name
        dest = dest_parent / name
        if not src.exists():
            status = f"нет источника: {src.name}"
        elif not do_move:
            status = f"план: {name} -> {NEW_PARENT}/{name}"
        else:
            status = move_directory(src, dest_parent)
        print(f"  {name:<16} {status}")

    if not do_move:
        present = [n for n in DIRS_TO_MOVE if (root / n).exists()]
        if present:
            print("\n Это был DRY-RUN. Перенесите: "
                  "python misc/move_docs_dirs.py --move-dirs")
        else:
            print("\n Источников уже нет — переносить нечего.")
    print()


# --- CLI --------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Перенос _checkpoints_/_phases_/_tasks_ в _docs_ "
                    "и обновление ссылок.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="По умолчанию — сухой прогон. Используйте --apply / --move-dirs.",
    )
    p.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Корень репозитория (по умолчанию автоопределение по .git).",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Записать замены в файлы (без флага — dry-run).",
    )
    p.add_argument(
        "--move-dirs",
        action="store_true",
        help="Физически переместить каталоги в _docs_ (без флага — dry-run).",
    )
    p.add_argument(
        "--show-diff",
        action="store_true",
        help="Показать построчные примеры замен в отчёте.",
    )
    p.add_argument(
        "--only",
        choices=("files", "dirs"),
        default=None,
        help="Выполнить только один шаг: files (замены) или dirs (перенос).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    script_dir = Path(__file__).resolve().parent
    root = (args.root or detect_repo_root(script_dir)).resolve()
    print(f"Корень репозитория: {root}")

    only = args.only
    run_files = only in (None, "files")
    run_dirs = only in (None, "dirs")

    if run_files:
        step_update_files(root, apply=args.apply, show_diff=args.show_diff)
    if run_dirs:
        step_move_dirs(root, do_move=args.move_dirs)

    return 0


if __name__ == "__main__":
    sys.exit(main())
