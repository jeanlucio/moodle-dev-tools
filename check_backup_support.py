#!/usr/bin/env python3
"""Checks that an activity's FEATURE_BACKUP_MOODLE2 declaration agrees with its backup files.

Two ways to get it wrong, both silent until a real backup:

- <modname>_supports() returns true but backup/moodle2/ lacks the activity task classes: a course
  backup fails, and so does deleting the activity while the recycle bin is enabled, since the bin
  backs it up first ("Class backup_<modname>_activity_task not found"). mod_quickpoll had this
  (15/10/2026), found by Eduardo Kraus's moodle-plugin-validate;
- the classes exist but the feature is not declared: the activity is left out of every course
  backup and duplicate, with no error at all.

Only activity modules (mod_*) are checked: the feature belongs to their supports() callback. The
declaration is read in the three shapes this ecosystem uses — a switch where the case may fall
through to a shared return, an array of features, and a match — and anything that is not a plain
true/false/null (a variable, a function call) is skipped rather than guessed.
"""

import re
import sys
from pathlib import Path

FEATURE = 'FEATURE_BACKUP_MOODLE2'
# `FEATURE_BACKUP_MOODLE2 => value` in an array or a match arm, possibly after other conditions.
ARROW_RE = re.compile(FEATURE + r'\s*=>\s*([^,\]\n;]+)')
# `$features[FEATURE_BACKUP_MOODLE2] = value;`
ASSIGN_RE = re.compile(r'\[\s*' + FEATURE + r'\s*\]\s*=\s*([^;]+);')
CASE_RE = re.compile(r'case\s+' + FEATURE + r'\s*:')
# Any further `case X:` labels that fall through, then the `return value;` they share.
FALLTHROUGH_RE = re.compile(r'(?:\s*case\s+[A-Za-z0-9_\\:]+\s*:)*\s*return\s+([^;]+);')


def strip_comments(source: str) -> str:
    """PHP source without comments, so a commented-out declaration is not read.

    @param str $source the PHP source
    @return str the source with block and line comments removed
    """
    source = re.sub(r'/\*.*?\*/', ' ', source, flags=re.S)
    return re.sub(r'(?m)(?<![:\'"])//.*$|^\s*#.*$', ' ', source)


def declared(lib_source: str) -> str | None:
    """What supports() answers for FEATURE_BACKUP_MOODLE2.

    @param str $lib_source the source of the module's lib.php
    @return str|None 'true', 'false' (also when the feature is never mentioned: core treats the
        missing answer as unsupported), or None when the value cannot be read statically
    """
    source = strip_comments(lib_source)
    if FEATURE not in source:
        return 'false'

    value = None
    for regex in (ARROW_RE, ASSIGN_RE):
        match = regex.search(source)
        if match:
            value = match.group(1)
            break
    if value is None:
        match = CASE_RE.search(source)
        if match:
            following = FALLTHROUGH_RE.match(source, match.end())
            value = following.group(1) if following else None
    if value is None:
        return None

    value = value.strip().lower()
    if value == 'true':
        return 'true'
    if value in ('false', 'null'):
        return 'false'
    return None


def check_plugin(plugin_dir: Path) -> list[str]:
    """Checks one plugin, returning a list of human-readable problem descriptions.

    @param Path $plugin_dir absolute path to the plugin's repository root
    @return list[str] one entry per disagreement; empty if clean, not an activity or unreadable
    """
    version = plugin_dir / 'version.php'
    lib = plugin_dir / 'lib.php'
    if not version.is_file() or not lib.is_file():
        return []
    match = re.search(r"""\$plugin->component\s*=\s*['"]mod_([a-z0-9_]+)['"]""", version.read_text(errors='replace'))
    if not match:
        return []
    modname = match.group(1)

    support = declared(lib.read_text(encoding='utf-8', errors='replace'))
    if support is None:
        return []

    required = [
        plugin_dir / 'backup' / 'moodle2' / f'backup_{modname}_activity_task.class.php',
        plugin_dir / 'backup' / 'moodle2' / f'restore_{modname}_activity_task.class.php',
    ]
    missing = [path for path in required if not path.is_file()]

    if support == 'true' and missing:
        names = ', '.join(path.relative_to(plugin_dir).as_posix() for path in missing)
        return [
            f"{lib}: {modname}_supports() declara {FEATURE}, mas falta {names}. O backup do curso falha, e "
            f"apagar a atividade com a lixeira ativa também. Implemente o backup ou devolva false até lá."
        ]
    if support == 'false' and not missing:
        return [
            f"{lib}: existe backup/moodle2/ completo, mas {modname}_supports() não declara {FEATURE}. "
            f"A atividade fica fora de todo backup e duplicação de curso, sem erro nenhum."
        ]
    return []


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print('uso: check_backup_support.py <caminho-do-plugin> [<caminho-do-plugin> ...]', file=sys.stderr)
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
        print(f'\n{len(all_problems)} atividade(s) com backup declarado e arquivos em desacordo.', file=sys.stderr)
        return 1

    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
