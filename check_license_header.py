#!/usr/bin/env python3
"""Checks that the Mustache, CSS and JS files a commit adds or changes carry the Moodle GPL header.

PHP is left out on purpose: PHPCS with the Moodle standard already demands the boilerplate comment
(moodle.Files.BoilerplateComment), so a PHP file without it never reaches this point. Nothing checked
the other three. The Moodle ESLint config has no header rule and Stylelint has none either, and the
Mustache gate only looked for @template: three templates added without the header went through.

Skipped, because the header is not theirs to carry: docs/ (a static GitHub Pages site), minified
files, YUI's yui/build/, node_modules/ and vendor/, and anything under a <location> of thirdpartylibs.xml.
"""

import re
import sys
from pathlib import Path

# How many lines from the top may hold the header: a PHP-style opening comment, a Mustache comment
# block or a CSS docblock all fit well inside this.
HEAD_LINES = 25

MARKERS = ('This file is part of Moodle', 'Moodle is free software')

SKIPPED_DIRS = ('docs/', 'node_modules/', 'vendor/', 'yui/build/')

# Minified or built files: name-min.js (YUI/Shifter) and name.min.js (everything else).
MINIFIED_RE = re.compile(r'[.-]min\.')


def third_party_locations(plugin_dir: Path) -> list[str]:
    """Reads the <location> entries of the plugin's thirdpartylibs.xml.

    @param Path $plugin_dir absolute path to the plugin's repository root
    @return list[str] paths relative to the plugin root, without a trailing slash
    """
    declared = plugin_dir / 'thirdpartylibs.xml'
    if not declared.is_file():
        return []
    found = re.findall(r'<location>\s*([^<]+?)\s*</location>', declared.read_text())
    return [location.strip('/') for location in found if location.strip('/')]


def is_exempt(relative: str, locations: list[str]) -> bool:
    """Whether a file is third-party, generated or not Moodle code.

    @param str $relative the file path relative to the plugin root
    @param list[str] $locations the thirdpartylibs.xml locations
    @return bool
    """
    if relative.startswith(SKIPPED_DIRS) or MINIFIED_RE.search(Path(relative).name):
        return True
    return any(relative == loc or relative.startswith(loc + '/') for loc in locations)


def missing_header(file_path: Path) -> bool:
    """Whether the first lines of a file lack the Moodle license header.

    @param Path $file_path the file to read
    @return bool
    """
    head = '\n'.join(file_path.read_text(errors='replace').splitlines()[:HEAD_LINES])
    return not all(marker in head for marker in MARKERS)


def check_plugin(plugin_dir: Path, files: list[str]) -> list[str]:
    """Checks the given files.

    @param Path $plugin_dir absolute path to the plugin's repository root
    @param list[str] $files paths relative to the plugin root (usually just the staged ones)
    @return list[str] one entry per file without the header; empty if clean
    """
    locations = third_party_locations(plugin_dir)
    problems = []
    for relative in files:
        path = plugin_dir / relative
        if not path.is_file() or is_exempt(relative, locations):
            continue
        if missing_header(path):
            problems.append(f"{relative}: falta o cabeçalho de licença GPL do Moodle")
    return problems


def main() -> int:
    """Entry point: <plugin_dir> <file>... Prints the problems and returns the exit code."""
    if len(sys.argv) < 3:
        return 0
    problems = check_plugin(Path(sys.argv[1]), sys.argv[2:])
    for problem in problems:
        print(f"  {problem}")
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
