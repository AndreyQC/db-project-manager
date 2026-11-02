#!/usr/bin/env python3
"""
Rename harness documentation directories and rewrite references.

Задача 20261102_940 (lm_hrdo_2): каталоги вида `_{каталог}_` переносятся на
`harness_{каталог}`. Скрипт делает ТОЛЬКО текстовую перезапись ссылок/путей;
сами каталоги переименовываются отдельно через `git mv` (см. ранбук плана).

- Переименование каталогов: git mv (вне этого скрипта), порядок в ранбуке.
- Перезапись ссылок: во всех текстовых файлах под root, кроме skip-каталогов
  (.git, src, node_modules, dist, vendor, ...), Taskfile.yml и самого скрипта.
- Fenced-блоки переписываются наравне с телом: живые инструкции (команды
  make/ls) тоже лежат в фенсах; от порчи карту уберегает self-skip.
- Байт-точная запись: перезаписываются только файлы с реальными заменами,
  без нормализации переводов строк и кодировки (UTF-8 strict, иначе binary).

Usage:
    python harness_dir_rename.py <root_dir>            # preview (default)
    python harness_dir_rename.py <root_dir> --apply    # выполнить замену
"""

import argparse
import sys
from pathlib import Path

# Порядок важен: сначала длинные префиксы, catch-all _docs_ — последним.
RENAME_MAP = [
    (b"_docs_/_checkpoints_", b"harness_docs/harness_checkpoints"),
    (b"_docs_/_tasks_", b"harness_docs/harness_tasks"),
    (b"_docs_/_plans_", b"harness_docs/harness_plans"),
    (b"_docs_/_domains_", b"harness_docs/harness_domains"),
    (b"_docs_/_phases_", b"harness_docs/harness_phases"),
    (b"_docs_/_sessions_", b"harness_docs/harness_sessions"),
    (b"_docs_/_index", b"harness_docs/harness_index"),
    # опечаточный вариант без закрывающего подчёркивания (исторический мусор)
    (b"_docs/_checkpoints_", b"harness_docs/harness_checkpoints"),
    (b"_docs_", b"harness_docs"),
    (b"_archives_", b"harness_archives"),
]

SKIP_DIRS = {
    ".git", "node_modules", "src", "dist", "build", "vendor", "targets",
    ".venv", "__pycache__", ".idea", ".vscode", "output", "logs",
}
SKIP_FILES = {"Taskfile.yml"}


def is_text(data: bytes) -> bool:
    try:
        data.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def rewrite(data: bytes) -> tuple[bytes, int]:
    """Переписать все вхождения; вернуть (новые байты, число замен)."""
    changed = data
    total = 0
    for old, new in RENAME_MAP:
        n = changed.count(old)
        if n:
            changed = changed.replace(old, new)
            total += n
    return changed, total


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"нет каталога: {root}")
        return 2
    self_path = Path(__file__).resolve()

    files_total = 0
    files_hit: list[tuple[Path, int]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts[:-1]):
            continue
        if rel.name in SKIP_FILES or path.name in SKIP_FILES:
            continue
        if path == self_path:
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if not is_text(data):
            continue
        files_total += 1
        new, n = rewrite(data)
        if n:
            files_hit.append((path, n))
            if args.apply:
                path.write_bytes(new)

    verb = "ПЕРЕЗАПИСАНО" if args.apply else "найдено замен"
    print(f"просмотрено текстовых файлов: {files_total}")
    print(f"{verb}: {sum(n for _, n in files_hit)} в {len(files_hit)} файлах")
    for path, n in files_hit:
        print(f"  {n:4d}  {path.relative_to(root)}")
    if not args.apply:
        print("preview — ничего не изменено; применение: --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
