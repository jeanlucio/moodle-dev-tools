#!/usr/bin/env python3
"""Checks that every literal get_string('key', 'component') call in a PHP file resolves
to a real lang string — the exact class of bug that motivated this script: mod_playervideo's
index.php called get_string('nomodules', 'moodle'), a key that never existed in core.

Deliberately conservative: only checks calls where BOTH arguments are literal quoted
strings (a get_string($var, ...) call is skipped, not guessed at), and only resolves a
component when it's confident about the directory — the plugin's own component, 'core'/
'moodle', or one of the well-known plugin types already used elsewhere in this ecosystem
(mod/local/block/filter/report/format/availability). Anything else is silently skipped
rather than risking a false positive that would erode trust in a blocking gate. A missed
typo in a rare component is an acceptable gap; a false block on a legitimate call is not.
"""

import os
import re
import sys
from pathlib import Path

# Deliberately strict about what counts as "a literal argument, and nothing else": the
# character right after the closing quote must be ',' (another argument follows) or ')'
# (the call ends there). Without that boundary check, 'help_' . $key . '_title' would be
# misread as the literal key 'help_' — the concatenation after it silently ignored instead
# of making the whole call unresolvable. Named backreferences (?P=q1/q2) so each string
# closes with the SAME quote character it opened with.
GET_STRING_RE = re.compile(
    r"""get_string\s*\(\s*
        (?P<q1>['"])(?P<key>[^'"]*)(?P=q1)
        \s*
        (?:
            \)
          |
            ,\s*(?P<q2>['"])(?P<component>[^'"]*)(?P=q2)\s*(?:[,)])
        )""",
    re.VERBOSE,
)
STRING_KEY_RE = re.compile(r"""\$string\[\s*['"](?P<key>[a-zA-Z0-9_:]+)['"]\s*\]""")


def is_interpolated(quote: str, value: str) -> bool:
    """A double-quoted PHP string containing '$' or '{' is not a static literal — PHP
    interpolates it at runtime (e.g. "rpg_{$tonekey}_name"). Single-quoted strings never
    interpolate, so they're always safe to treat as literal regardless of content.

    @param string $quote the quote character the string was written with
    @param string $value the string's raw content (between the quotes)
    @return bool true if this is not actually a static value
    """
    return quote == '"' and ('$' in value or '{' in value)

TYPE_DIRS = {
    'mod': 'mod',
    'block': 'blocks',
    'local': 'local',
    'filter': 'filter',
    'report': 'report',
    'availability': 'availability/condition',
}


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


def lang_filename_for_component(component: str) -> str:
    """Frankenstyle component -> lang filename, honouring mod_*'s dropped prefix.

    @param string $component full frankenstyle component name
    @return string filename (without path), e.g. "playervideo.php" or "block_playerhud.php"
    """
    if component.startswith('mod_'):
        return component[len('mod_'):] + '.php'
    return component + '.php'


def resolve_lang_file(component: str, own_component: str | None, own_dir: Path,
                       moodle_root: Path | None) -> Path | None:
    """Resolves a component to its lang/en/<name>.php file, or None if not confidently
    resolvable — callers must treat None as "skip", never as "missing".

    @param string $component frankenstyle component from the get_string() call
    @param string|None $own_component the plugin being checked's own component
    @param Path $own_dir the plugin being checked's own repository root
    @param Path|None $moodle_root absolute path to the Moodle public docroot, if known
    @return Path|None the lang file path, or None
    """
    if component in ('core', 'moodle', ''):
        return moodle_root / 'lang' / 'en' / 'moodle.php' if moodle_root else None

    if own_component and component == own_component:
        return own_dir / 'lang' / 'en' / lang_filename_for_component(component)

    if moodle_root is None:
        return None

    for type_, dirname in TYPE_DIRS.items():
        prefix = f'{type_}_'
        if component.startswith(prefix):
            name = component[len(prefix):]
            return moodle_root / dirname / name / 'lang' / 'en' / f'{lang_filename_for_component(component)}'

    if component.startswith('format_'):
        name = component[len('format_'):]
        return moodle_root / 'course' / 'format' / name / 'lang' / 'en' / f'{component}.php'

    return None


def check_file(php_file: Path, own_component: str | None, own_dir: Path,
                moodle_root: Path | None) -> list[str]:
    """Checks one PHP file, returning a list of human-readable problem descriptions.

    @param Path $php_file the file to scan for get_string() calls
    @param string|None $own_component the plugin being checked's own component
    @param Path $own_dir the plugin being checked's own repository root
    @param Path|None $moodle_root absolute path to the Moodle public docroot, if known
    @return list[str] one entry per call resolving to a missing key; empty if clean
    """
    text = php_file.read_text()
    problems = []
    lang_cache: dict[Path, set] = {}

    for match in GET_STRING_RE.finditer(text):
        key = match.group('key')
        if is_interpolated(match.group('q1'), key):
            continue

        component_raw = match.group('component')
        if component_raw is not None and is_interpolated(match.group('q2'), component_raw):
            continue
        component = component_raw or 'core'

        lang_file = resolve_lang_file(component, own_component, own_dir, moodle_root)
        if lang_file is None or not lang_file.is_file():
            continue  # Unresolvable or genuinely absent on this host — skip, don't guess.

        if lang_file not in lang_cache:
            lang_cache[lang_file] = set(STRING_KEY_RE.findall(lang_file.read_text()))

        if key not in lang_cache[lang_file]:
            line = text.count('\n', 0, match.start()) + 1
            problems.append(
                f"{php_file}:{line}: get_string('{key}', '{component}') — chave não existe "
                f"em {lang_file}"
            )
    return problems


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print('uso: check_get_string.py <plugin_dir> <arquivo.php> [<arquivo.php> ...]', file=sys.stderr)
        return 1

    plugin_dir = Path(argv[1]).resolve()
    if not plugin_dir.is_dir():
        print(f'erro: {plugin_dir} não existe', file=sys.stderr)
        return 1

    own_component = read_own_component(plugin_dir)
    moodle_root_env = os.environ.get('MDT_MOODLE_PUBLIC')
    moodle_root = Path(moodle_root_env).resolve() if moodle_root_env else None

    all_problems = []
    for raw in argv[2:]:
        php_file = Path(raw).resolve()
        if not php_file.is_file():
            continue
        all_problems.extend(check_file(php_file, own_component, plugin_dir, moodle_root))

    for problem in all_problems:
        print(problem)

    if all_problems:
        print(f'\n{len(all_problems)} chamada(s) de get_string() com chave inexistente.', file=sys.stderr)
        return 1

    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
