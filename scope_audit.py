#!/usr/bin/env python3
"""Diffs the §6 file tree of a plugin's SCOPE.md against what actually exists on disk.

Born from a real incident: mod_playercross's SCOPE.md §6 planned managewords.php,
ranking.php, ranking_service.php and ai_word_generator.php since its very first phase,
but five implementation phases were marked complete without any of them existing — the
phase-completion check had been a re-derived mental checklist, not a literal read of
this tree. This script makes that literal read mechanical and repeatable instead of
relying on memory.

Usage:
    python3 scope_audit.py <plugin_dir> [--scope path/to/SCOPE.md]

<plugin_dir> is the plugin's root directory (the one that directly contains SCOPE.md,
classes/, lang/, etc.). Exits 1 if anything from the tree is missing, 0 otherwise.
"""

import argparse
import os
import re
import sys


def expand_braces(token: str) -> list[str]:
    """Expands a single shell-brace token, e.g. "{a,b,c}_test.php" -> 3 entries.

    SCOPE.md files use this notation to list several similarly-named test files on one
    tree line without repeating the whole tree structure. A token without braces is
    returned as a single-item list unchanged.
    """
    match = re.search(r"\{([^{}]+)\}", token)
    if not match:
        return [token]
    prefix = token[:match.start()]
    suffix = token[match.end():]
    options = match.group(1).split(",")
    return [f"{prefix}{opt.strip()}{suffix}" for opt in options]


def join_wrapped_braces(tree_text: str) -> list[str]:
    """Joins tree lines whose "{a,b,c}" brace list was hand-wrapped across several
    physical lines (done in some SCOPE.md files to keep long test-file lists under the
    132-char soft limit). A line with an unmatched "{" absorbs following lines, each
    stripped of its own tree-drawing prefix, until the brace closes.
    """
    lines = tree_text.splitlines()
    joined: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        while line.count("{") > line.count("}") and i + 1 < len(lines):
            i += 1
            continuation = re.sub(r"^[│\s]*[├└]?─*\s*", "", lines[i])
            line = line.rstrip("\n") + continuation
        joined.append(line)
        i += 1
    return joined


def parse_tree(tree_text: str) -> list[str]:
    """Parses an ASCII tree (├── / └── / │ drawing characters) into relative paths.

    Directories are recognised by a trailing "/" in their own tree line and are pushed
    onto a stack; deeper lines inherit every currently-stacked directory as a prefix.
    Indentation is measured in raw leading characters, since the tree-drawing glyphs
    and plain spaces are used interchangeably for alignment in these documents.
    """
    stack: list[tuple[int, str]] = []  # (indent length, directory name)
    paths: list[str] = []

    for raw_line in join_wrapped_braces(tree_text):
        if not raw_line.strip():
            continue

        leading = re.match(r"^([│\s├└─]*)", raw_line).group(1)
        indent = len(leading)

        stripped = re.sub(r"^[│\s]*[├└]?─*\s*", "", raw_line)
        name_match = re.match(r"^(\S+)", stripped)
        if not name_match:
            continue
        name = name_match.group(1)

        while stack and stack[-1][0] >= indent:
            stack.pop()

        full = "/".join([entry[1] for entry in stack] + [name])

        for expanded in expand_braces(full):
            paths.append(expanded)

        if name.endswith("/"):
            stack.append((indent, name.rstrip("/")))

    return paths


def load_tree_paths(scope_path: str) -> list[str]:
    with open(scope_path, encoding="utf-8") as handle:
        content = handle.read()

    match = re.search(r"## 6\. Estrutura de Diret.*?```\n(.*?)```", content, re.S)
    if not match:
        raise SystemExit(f"error: could not find a '## 6. Estrutura de Diretórios' fenced tree in {scope_path}")

    return parse_tree(match.group(1))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plugin_dir", help="Plugin root directory (contains SCOPE.md)")
    parser.add_argument("--scope", default=None, help="Path to SCOPE.md (default: <plugin_dir>/SCOPE.md)")
    args = parser.parse_args()

    plugin_dir = args.plugin_dir.rstrip("/")
    scope_path = args.scope or os.path.join(plugin_dir, "SCOPE.md")

    if not os.path.isfile(scope_path):
        print(f"error: SCOPE.md not found at {scope_path}", file=sys.stderr)
        return 1

    raw_paths = load_tree_paths(scope_path)

    # The tree's own root entry (e.g. "mod/playercross/") names the plugin directory
    # itself, not a child path — every other entry is relative to it.
    root_prefixes = [p.rstrip("/") + "/" for p in raw_paths if p.endswith("/")][:1]
    root_prefix = root_prefixes[0] if root_prefixes else ""

    entries = []
    for p in raw_paths:
        candidate = p[len(root_prefix):] if root_prefix and p.startswith(root_prefix) else p
        candidate = candidate.rstrip("/")
        if candidate and candidate != "SCOPE.md":
            entries.append(candidate)

    missing = [e for e in entries if not os.path.exists(os.path.join(plugin_dir, e))]

    print(f"SCOPE.md: {scope_path}")
    print(f"Entries checked: {len(entries)}")

    if not missing:
        print("Nothing missing — every §6 entry exists on disk.")
        return 0

    print(f"\nMissing ({len(missing)}):")
    for m in sorted(missing):
        print(f"  {m}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
