#!/usr/bin/env python3

"""
dump_project.py — дамп проекта в один TXT файл (дерево + содержимое файлов).

Примеры:
  python dump_project.py
  python dump_project.py -o dump.txt --exclude-dirs .git node_modules venv --exclude-ext .png .jpg .zip
  python dump_project.py --include-ext .py .md .txt
  python dump_project.py --exclude "*/migrations/*" "*/__pycache__/*"
  python dump_project.py --no-hidden --max-file-size 200000
"""

from __future__ import annotations

import argparse
import fnmatch
import os
from pathlib import Path
from datetime import datetime


DEFAULT_EXCLUDE_DIRS = {
    ".git", ".hg", ".svn",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".idea", ".vscode",
    "node_modules",
    "venv", ".venv", "env",
    "dist", "build", ".tox",
}

DEFAULT_EXCLUDE_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp",
    ".zip", ".tar", ".gz", ".7z", ".rar",
    ".pdf", ".mp4", ".mov", ".avi", ".mkv", ".mp3", ".wav",
    ".exe", ".dll", ".so", ".dylib",
    ".bin", ".dat",
}


def is_hidden(path: Path) -> bool:
    # На Unix "скрытые" начинаются с точки; на Windows тоже часто так.
    return any(part.startswith(".") for part in path.parts if part not in (".", ".."))


def should_exclude_by_patterns(rel_posix: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(rel_posix, pat) for pat in patterns)


def build_tree(root: Path, files: list[Path]) -> str:
    # Строим дерево только по включённым файлам (без исключённых).
    # Для вывода используем ASCII дерево.
    from collections import defaultdict

    children = defaultdict(set)
    nodes = set()

    root = root.resolve()
    for f in files:
        rp = f.resolve()
        # Собираем все директории по пути к файлу
        parts = rp.relative_to(root).parts
        cur = root
        nodes.add(cur)
        for p in parts[:-1]:
            nxt = cur / p
            children[cur].add(nxt)
            nodes.add(nxt)
            cur = nxt
        children[cur].add(rp)
        nodes.add(rp)

    def sort_key(p: Path):
        # Директории выше файлов, затем по имени
        return (p.is_file(), p.name.lower())

    lines = [str(root.name) + "/"]
    def walk(dirpath: Path, prefix: str = ""):
        items = sorted(children.get(dirpath, []), key=sort_key)
        for i, item in enumerate(items):
            last = (i == len(items) - 1)
            branch = "└── " if last else "├── "
            if item.is_dir():
                lines.append(prefix + branch + item.name + "/")
                walk(item, prefix + ("    " if last else "│   "))
            else:
                lines.append(prefix + branch + item.name)

    walk(root)
    return "\n".join(lines)


def read_text_safely(path: Path, max_bytes: int) -> tuple[str, str | None]:
    """
    Возвращает (text, warning). Пытаемся читать как UTF-8 (с заменой), если не вышло — fallback.
    """
    size = path.stat().st_size
    if max_bytes is not None and size > max_bytes:
        return ("", f"SKIPPED: file too large ({size} bytes > {max_bytes})")

    data = path.read_bytes()
    # Пробуем декодировать; если бинарь — будет много нулей/непечатаемых.
    try:
        text = data.decode("utf-8", errors="replace")
        # эвристика "похоже на бинарь"
        if b"\x00" in data:
            return ("", "SKIPPED: looks like binary (NUL byte found)")
        return (text, None)
    except Exception as e:
        return ("", f"SKIPPED: decode error: {e!r}")


def main():
    ap = argparse.ArgumentParser(description="Dump project to one text file.")
    ap.add_argument(
        "-r", "--root",
        default=".",
        help="Project root (default: current directory)."
    )
    ap.add_argument(
        "-o", "--output",
        default=None,
        help="Output .txt file path (default: project_dump_YYYYmmdd_HHMMSS.txt in root)."
    )

    ap.add_argument(
        "--include-ext",
        nargs="*",
        default=None,
        help="Only include these extensions (e.g. .py .md .txt). If set, others are ignored."
    )
    ap.add_argument(
        "--exclude-ext",
        nargs="*",
        default=None,
        help="Exclude these extensions (e.g. .png .jpg .zip)."
    )
    ap.add_argument(
        "--exclude-dirs",
        nargs="*",
        default=None,
        help="Exclude directories by name (e.g. .git venv node_modules)."
    )
    ap.add_argument(
        "--exclude",
        nargs="*",
        default=[],
        help="Exclude glob patterns (relative posix), e.g. '*/migrations/*' 'secrets.*'."
    )
    ap.add_argument(
        "--include",
        nargs="*",
        default=[],
        help="Include glob patterns (relative posix). If provided, only matching files are kept (after other checks)."
    )

    ap.add_argument(
        "--no-hidden",
        action="store_true",
        help="Do not include hidden files/dirs (starting with dot)."
    )
    ap.add_argument(
        "--max-file-size",
        type=int,
        default=500_000,
        help="Max file size in bytes to dump (default: 500000). Larger files are skipped."
    )
    ap.add_argument(
        "--no-tree",
        action="store_true",
        help="Do not print directory tree in the dump."
    )
    ap.add_argument(
        "--follow-symlinks",
        action="store_true",
        help="Follow symlinks (default: no)."
    )

    args = ap.parse_args()

    root = Path(args.root).resolve()
    if not root.exists() or not root.is_dir():
        raise SystemExit(f"Root directory not found: {root}")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    output = Path(args.output) if args.output else (root / f"project_dump_{ts}.txt")
    output = output.resolve()

    exclude_dirs = set(args.exclude_dirs) if args.exclude_dirs is not None else set(DEFAULT_EXCLUDE_DIRS)
    exclude_exts = set(e.lower() for e in (args.exclude_ext or [])) if args.exclude_ext is not None else set(DEFAULT_EXCLUDE_EXTS)
    include_exts = set(e.lower() for e in (args.include_ext or [])) if args.include_ext else None

    included_files: list[Path] = []

    # Обходим проект
    for dirpath, dirnames, filenames in os.walk(root, followlinks=args.follow_symlinks):
        dpath = Path(dirpath)

        # Фильтруем директории на месте (чтобы os.walk не заходил внутрь)
        kept_dirnames = []
        for dn in dirnames:
            if dn in exclude_dirs:
                continue
            p = dpath / dn
            if args.no_hidden and is_hidden(p.relative_to(root)):
                continue
            relp = p.relative_to(root).as_posix()
            if should_exclude_by_patterns(relp, args.exclude):
                continue
            kept_dirnames.append(dn)
        dirnames[:] = kept_dirnames

        for fn in filenames:
            p = dpath / fn

            # Не дампим сам output, если он внутри root
            try:
                if p.resolve() == output:
                    continue
            except OSError:
                # Битая ссылка или недоступный путь: файл всё равно пропускаем.
                continue

            if args.no_hidden and is_hidden(p.relative_to(root)):
                continue

            rel_posix = p.relative_to(root).as_posix()

            # exclude patterns
            if should_exclude_by_patterns(rel_posix, args.exclude):
                continue

            # include patterns (если заданы — оставляем только совпадающие)
            if args.include:
                if not should_exclude_by_patterns(rel_posix, args.include):  # reuse fnmatch helper
                    # helper returns True if matches any; name is generic, but ok.
                    continue

            ext = p.suffix.lower()

            # include-ext
            if include_exts is not None and ext not in include_exts:
                continue

            # exclude-ext
            if ext in exclude_exts:
                continue

            # только обычные файлы
            try:
                if not p.is_file():
                    continue
            except OSError:
                continue

            included_files.append(p)

    included_files.sort(key=lambda x: x.relative_to(root).as_posix().lower())

    # Пишем дамп
    with output.open("w", encoding="utf-8", newline="\n") as out:
        out.write("PROJECT DUMP\n")
        out.write(f"Root: {root}\n")
        out.write(f"Generated: {datetime.now().isoformat(timespec='seconds')}\n")
        out.write(f"Files included: {len(included_files)}\n")
        out.write("\n")

        if not args.no_tree:
            out.write("=== TREE ===\n")
            out.write(build_tree(root, included_files))
            out.write("\n\n")

        out.write("=== FILES ===\n\n")

        for p in included_files:
            rel = p.relative_to(root).as_posix()
            out.write(f"\n----- FILE: {rel} -----\n")
            try:
                size = p.stat().st_size
            except OSError:
                out.write("SKIPPED: cannot stat file\n")
                continue

            out.write(f"SIZE: {size} bytes\n")
            text, warning = read_text_safely(p, args.max_file_size)
            if warning:
                out.write(warning + "\n")
                continue

            out.write("\n")
            out.write(text)
            if not text.endswith("\n"):
                out.write("\n")

    print(f"Dump written to: {output}")


if __name__ == "__main__":
    main()