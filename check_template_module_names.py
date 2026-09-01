#!/usr/bin/env python3
"""Checks that a template's @template tag (Mustache) or a module's @module tag (AMD JS)
names the file correctly: <frankenstyle>/<path-under-templates-or-amd/src>, extension
stripped. Verified against real core/ecosystem examples before writing this (unlike the
lang-file convention, neither tag drops a "mod_" prefix — mod_playervideo/attempt_summary,
core_contentbank/bankcontent/navigation, core_group/comboboxsearch/group).

The existing pre-commit hook already checks that a .mustache file HAS an @template tag at
all (moodle-plugin-ci's mustache linter fails hard without one) — this script checks the
VALUE is correct, and extends the same check to AMD modules' @module tag, which had no
check of any kind before (presence or value).
"""

import re
import sys
from pathlib import Path

TEMPLATE_TAG_RE = re.compile(r"""@template\s+(?P<name>\S+)""")
MODULE_TAG_RE = re.compile(r"""@module\s+(?P<name>\S+)""")


def read_own_component(plugin_dir: Path) -> str | None:
    """Reads $plugin->component from the plugin's own version.php.

    @param Path $plugin_dir absolute path to the plugin's repository root
    @return str|None the frankenstyle component, or None if unreadable
    """
    version_file = plugin_dir / 'version.php'
    if not version_file.is_file():
        return None
    match = re.search(r"""\$plugin->component\s*=\s*['"]([a-z0-9_]+)['"]""", version_file.read_text())
    return match.group(1) if match else None


def check_one(file_path: Path, base_dir: Path, tag_re: re.Pattern, tag_name: str,
              component: str) -> str | None:
    """Checks a single template or module file against its expected tag value.

    @param Path $file_path the .mustache or .js file to check
    @param Path $base_dir the "templates" or "amd/src" directory this file lives under
    @param re.Pattern $tag_re regex matching the relevant docblock tag
    @param string $tag_name "@template" or "@module", for the error message
    @param string $component the plugin's own frankenstyle component
    @return string|None a problem description, or None if the file is clean
    """
    relative = file_path.relative_to(base_dir).with_suffix('')
    expected = f"{component}/{relative.as_posix()}"

    match = tag_re.search(file_path.read_text())
    if not match:
        return None  # Presence is already covered elsewhere for templates; modules have
        # no such requirement yet — a missing tag isn't this script's problem to raise.

    actual = match.group('name')
    if actual != expected:
        return f"{file_path}: {tag_name} diz '{actual}', deveria ser '{expected}'"
    return None


def check_plugin(plugin_dir: Path, files: list[Path]) -> list[str]:
    """Checks the given files (a subset of a plugin's templates/*.mustache and
    amd/src/*.js) against their expected @template/@module values.

    @param Path $plugin_dir absolute path to the plugin's repository root
    @param list[Path] $files the specific files to check (usually just the staged ones)
    @return list[str] one entry per mismatch; empty if clean
    """
    component = read_own_component(plugin_dir)
    if component is None:
        return []

    templates_dir = plugin_dir / 'templates'
    amd_src_dir = plugin_dir / 'amd' / 'src'

    problems = []
    for file_path in files:
        if file_path.suffix == '.mustache' and templates_dir in file_path.parents:
            problem = check_one(file_path, templates_dir, TEMPLATE_TAG_RE, '@template', component)
        elif file_path.suffix == '.js' and amd_src_dir in file_path.parents:
            problem = check_one(file_path, amd_src_dir, MODULE_TAG_RE, '@module', component)
        else:
            continue
        if problem:
            problems.append(problem)
    return problems


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print('uso: check_template_module_names.py <plugin_dir> <arquivo> [<arquivo> ...]', file=sys.stderr)
        return 1

    plugin_dir = Path(argv[1]).resolve()
    if not plugin_dir.is_dir():
        print(f'erro: {plugin_dir} não existe', file=sys.stderr)
        return 1

    files = [Path(raw).resolve() for raw in argv[2:] if Path(raw).is_file()]
    problems = check_plugin(plugin_dir, files)

    for problem in problems:
        print(problem)

    if problems:
        print(f'\n{len(problems)} @template/@module com nome incorreto.', file=sys.stderr)
        return 1

    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
