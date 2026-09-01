#!/usr/bin/env python3
"""Checks that every capability declared in a plugin's db/access.php has a matching
lang string in lang/en/<component>.php.

Moodle's own convention (verified against mod_playervideo and block_playerhud, both
already-published plugins): a capability key like 'mod/playervideo:attempt' maps to the
lang string key 'playervideo:attempt' — everything after the capability's first '/'.
This is already a written rule in the project's CLAUDE.md pre-delivery checklist
(item 19: "Capabilities have corresponding lang strings"), but was never automated —
PHPCS, moodlecheck and PHPStan have no idea db/access.php and lang/en/*.php are
supposed to agree with each other.

Only checks the plugin's OWN capabilities (declared with the plugin's own component
prefix inferred from its db/access.php path) — a capability CLONED from another
component via 'clonepermissionsfrom' is a value, not a new key, and is correctly
never matched by the key-only regex below.
"""

import re
import sys
from pathlib import Path

# Matches a capability array KEY specifically — anchored to "=> [" right after the
# quoted string, so a capability referenced only as a VALUE (e.g. inside
# 'clonepermissionsfrom' => 'moodle/course:view') is never mistaken for a declaration.
CAPABILITY_KEY_RE = re.compile(
    r"""['"](?P<full>[a-z][a-z0-9_]*/[a-z][a-z0-9_]*:[a-zA-Z0-9_]+)['"]\s*=>\s*\["""
)
STRING_KEY_RE = re.compile(r"""\$string\[\s*['"](?P<key>[a-zA-Z0-9_:]+)['"]\s*\]""")


def find_component_lang_file(plugin_dir: Path) -> Path | None:
    """Resolves lang/en/<component>.php from the plugin's own version.php.

    @param Path $plugin_dir absolute path to the plugin's repository root
    @return Path|None the lang file path, or None if version.php/component can't be read
    """
    version_file = plugin_dir / 'version.php'
    if not version_file.is_file():
        return None
    match = re.search(r"""\$plugin->component\s*=\s*['"]([a-z0-9_]+)['"]""", version_file.read_text())
    if not match:
        return None
    component = match.group(1)
    # Frankenstyle component -> lang filename. mod_* is the one type that drops its
    # "mod_" prefix (e.g. "mod_playervideo" -> "playervideo.php", matching core's own
    # mod_quiz -> quiz.php); every other type keeps the full frankenstyle name
    # (e.g. "block_playerhud" -> "block_playerhud.php", "local_aihub" -> "local_aihub.php").
    if component.startswith('mod_'):
        name = component[len('mod_'):]
    else:
        name = component
    lang_file = plugin_dir / 'lang' / 'en' / f'{name}.php'
    return lang_file if lang_file.is_file() else None


def check_plugin(plugin_dir: Path) -> list[str]:
    """Checks one plugin, returning a list of human-readable problem descriptions.

    @param Path $plugin_dir absolute path to the plugin's repository root
    @return list[str] one entry per capability missing its lang string; empty if clean
    """
    access_file = plugin_dir / 'db' / 'access.php'
    if not access_file.is_file():
        return []

    capabilities = CAPABILITY_KEY_RE.findall(access_file.read_text())
    if not capabilities:
        return []

    lang_file = find_component_lang_file(plugin_dir)
    if lang_file is None:
        return [
            f"{access_file}: declara {len(capabilities)} capability(ies) mas não achei "
            f"lang/en/<component>.php pra conferir (version.php ausente ou ilegível)"
        ]

    defined_strings = set(STRING_KEY_RE.findall(lang_file.read_text()))

    problems = []
    for full in capabilities:
        expected_key = full.split('/', 1)[1]
        if expected_key not in defined_strings:
            problems.append(
                f"{access_file}: capability '{full}' não tem string "
                f"\"$string['{expected_key}']\" em {lang_file}"
            )
    return problems


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print('uso: check_capability_strings.py <caminho-do-plugin> [<caminho-do-plugin> ...]', file=sys.stderr)
        return 1

    all_problems = []
    for raw in argv[1:]:
        plugin_dir = Path(raw).resolve()
        if not plugin_dir.is_dir():
            print(f'erro: {plugin_dir} não existe', file=sys.stderr)
            return 1
        all_problems.extend(check_plugin(plugin_dir))

    for problem in all_problems:
        print(problem)

    if all_problems:
        print(f'\n{len(all_problems)} capability(ies) sem string correspondente.', file=sys.stderr)
        return 1

    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
