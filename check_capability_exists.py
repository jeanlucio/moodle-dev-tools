#!/usr/bin/env python3
"""Checks that every literal has_capability()/require_capability() call references a
capability that actually exists in some db/access.php — the reverse direction of
check_capability_strings.py (which checks a plugin's OWN declared capabilities have
lang strings; this checks a REFERENCED capability, possibly someone else's, is real).

Same conservative philosophy as check_get_string.py: only resolves the plugin's own
component, 'moodle' (core capabilities live in lib/db/access.php — verified against the
real tree before writing this), and this ecosystem's known plugin types via
MDT_MOODLE_PUBLIC. An unresolvable component is skipped, never guessed at.
"""

import os
import re
import sys
from pathlib import Path

CAP_CALL_RE = re.compile(
    r"""\b(?:has_capability|require_capability)\s*\(\s*(?P<q>['"])(?P<cap>[a-z][a-z0-9_]*/[a-z][a-z0-9_]*:[a-zA-Z0-9_]+)(?P=q)\s*[,)]"""
)
# "=> [" or the legacy "=> array(" -- core's own lib/db/access.php still uses array()
# throughout (predates the short-array-syntax convention), so both must be accepted when
# a checker needs to parse someone ELSE's access.php, not just a freshly-written one.
CAPABILITY_KEY_RE = re.compile(
    r"""['"](?P<full>[a-z][a-z0-9_]*/[a-z][a-z0-9_]*:[a-zA-Z0-9_]+)['"]\s*=>\s*(?:\[|array\s*\()"""
)

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


def resolve_access_file(cap_type: str, cap_name: str, own_component: str | None,
                         own_dir: Path, moodle_root: Path | None) -> Path | None:
    """Resolves the db/access.php that should declare a 'type/name:cap' capability.

    @param string $cap_type the part before the '/' in the capability name
    @param string $cap_name the part between '/' and ':'
    @param string|None $own_component the plugin being checked's own component
    @param Path $own_dir the plugin being checked's own repository root
    @param Path|None $moodle_root absolute path to the Moodle public docroot, if known
    @return Path|None the access.php path, or None if not confidently resolvable
    """
    if cap_type == 'moodle':
        return moodle_root / 'lib' / 'db' / 'access.php' if moodle_root else None

    if own_component and f'{cap_type}_{cap_name}' == own_component:
        return own_dir / 'db' / 'access.php'

    if moodle_root is None:
        return None

    if cap_type in TYPE_DIRS:
        return moodle_root / TYPE_DIRS[cap_type] / cap_name / 'db' / 'access.php'

    if cap_type == 'format':
        return moodle_root / 'course' / 'format' / cap_name / 'db' / 'access.php'

    return None


def check_file(php_file: Path, own_component: str | None, own_dir: Path,
                moodle_root: Path | None) -> list[str]:
    """Checks one PHP file, returning a list of human-readable problem descriptions.

    @param Path $php_file the file to scan for has_capability()/require_capability() calls
    @param string|None $own_component the plugin being checked's own component
    @param Path $own_dir the plugin being checked's own repository root
    @param Path|None $moodle_root absolute path to the Moodle public docroot, if known
    @return list[str] one entry per call referencing a nonexistent capability
    """
    text = php_file.read_text()
    problems = []
    access_cache: dict[Path, set] = {}

    for match in CAP_CALL_RE.finditer(text):
        full = match.group('cap')
        cap_type, rest = full.split('/', 1)
        cap_name = rest.split(':', 1)[0]

        access_file = resolve_access_file(cap_type, cap_name, own_component, own_dir, moodle_root)
        if access_file is None or not access_file.is_file():
            continue  # Unresolvable or genuinely absent on this host — skip, don't guess.

        if access_file not in access_cache:
            access_cache[access_file] = set(CAPABILITY_KEY_RE.findall(access_file.read_text()))

        if full not in access_cache[access_file]:
            line = text.count('\n', 0, match.start()) + 1
            problems.append(f"{php_file}:{line}: capability '{full}' não existe em {access_file}")

    return problems


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print('uso: check_capability_exists.py <plugin_dir> <arquivo.php> [<arquivo.php> ...]', file=sys.stderr)
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
        print(f'\n{len(all_problems)} capability(ies) referenciada(s) que não existem.', file=sys.stderr)
        return 1

    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
