"""moodle-static-checks: the deterministic schema checks of static_schema, outside the audit.

    moodle-static-checks <tipo/nome|caminho> [--check a,b] [--json] [--list]

Exit status is 1 when something is reported, so the same command can gate a commit.
"""

import argparse
import json
import os
import sys
from pathlib import Path

import static_schema

PUBLIC = Path(os.environ.get('MDT_MOODLE_PUBLIC', '/home/ubuntu/meu-moodle/html/public'))


def resolve(plugin):
    """A plugin directory from `tipo/nome` (under the Moodle public dir) or a path."""
    plugin = plugin.removeprefix('./').removeprefix('html/public/').removeprefix('public/')
    path = Path(plugin)
    if not path.is_absolute():
        path = PUBLIC / plugin
    return path.resolve()


def run(plugin_dir, names):
    """{check: [violation, ...]} for the requested checks."""
    return {name: static_schema.CHECKS[name](plugin_dir) for name in names}


def main(argv=None):
    parser = argparse.ArgumentParser(prog='moodle-static-checks', description=__doc__.split('\n')[0])
    parser.add_argument('plugin', nargs='?', help='tipo/nome (ex.: mod/reflect) ou caminho')
    parser.add_argument('--check', help='checagens separadas por vírgula (padrão: todas)')
    parser.add_argument('--json', action='store_true', help='saída em JSON')
    parser.add_argument('--list', action='store_true', help='lista as checagens e sai')
    args = parser.parse_args(argv)

    if args.list:
        for name, function in static_schema.CHECKS.items():
            print(f'{name:18s} {(function.__doc__ or "").strip().splitlines()[0]}')
        return 0
    if not args.plugin:
        parser.error('informe o plugin')
    names = [n.strip() for n in args.check.split(',')] if args.check else list(static_schema.CHECKS)
    unknown = [n for n in names if n not in static_schema.CHECKS]
    if unknown:
        parser.error(f"checagem desconhecida: {', '.join(unknown)}")
    plugin_dir = resolve(args.plugin)
    if not plugin_dir.is_dir():
        print(f"erro: '{plugin_dir}' não existe", file=sys.stderr)
        return 2

    try:
        results = run(plugin_dir, names)
    except ValueError as exc:
        print(f'erro: {exc}', file=sys.stderr)
        return 2
    total = sum(len(v) for v in results.values())
    if args.json:
        print(json.dumps({'plugin': plugin_dir.name, 'results': results}, ensure_ascii=False, indent=2))
    else:
        for name, violations in results.items():
            for violation in violations:
                print(f'[{name}] {static_schema.describe(name, violation)}')
        print(f'{plugin_dir.name}: {total} ocorrência(s)' if total else f'{plugin_dir.name}: limpo')
    return 1 if total else 0


if __name__ == '__main__':
    sys.exit(main())
