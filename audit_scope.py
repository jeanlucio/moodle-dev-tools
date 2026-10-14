"""Which files an audit reads: per-plugin ignore list, named groups, --only and --since.

A full scan of a large plugin no longer fits one quota window, so the audit can be narrowed:
  - `ignore`: files that carry no signal (dev seed scripts, the stylesheet) are never read;
  - `groups`: named sets of paths, so a plugin can be audited one slice per session;
  - `--only`: read only the named groups or paths;
  - `--since REF`: read only what changed since a tag, branch or commit.

The configuration lives in `<plugin>/.plans/security-audit/config.json`, next to the reports:
that folder is gitignored in every plugin, so it never ships in the Plugin Directory zip.

    {
      "ignore": ["cli/seed*.php", "styles.css"],
      "groups": {"A": ["*.php", "classes/controller/"], "B": ["classes/external/"]}
    }

Group order matters: a file belongs to the FIRST group that matches it, so a catch-all such
as "classes/" goes last.
"""

import fnmatch
import json
import subprocess

CONFIG_REL = '.plans/security-audit/config.json'


def load_config(plugin_dir):
    """The plugin's audit configuration; an absent file means no ignores and no groups."""
    path = plugin_dir / CONFIG_REL
    if not path.is_file():
        return {'ignore': [], 'groups': {}}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f'{CONFIG_REL} inválido: {exc}')
    ignore = data.get('ignore', [])
    groups = data.get('groups', {})
    if not isinstance(ignore, list) or not isinstance(groups, dict) \
            or not all(isinstance(v, list) for v in groups.values()):
        raise ValueError(f'{CONFIG_REL}: "ignore" deve ser lista e "groups" um objeto de listas')
    return {'ignore': [str(p) for p in ignore],
            'groups': {str(k): [str(p) for p in v] for k, v in groups.items()}}


def matches(rel, pattern):
    """Does the plugin-relative path `rel` fall under `pattern`?

    - "dir/"            : everything below that directory;
    - "name.php"/"*.php": no slash in the pattern means root-level files only;
    - "a/b.php", "a/b*" : that path, a directory of that name, or a glob over the path.
    """
    if pattern.endswith('/'):
        return rel.startswith(pattern)
    if '/' not in pattern:
        return '/' not in rel and fnmatch.fnmatch(rel, pattern)
    return rel == pattern or rel.startswith(pattern + '/') or fnmatch.fnmatch(rel, pattern)


def apply_ignore(scan, patterns):
    """Split `scan` into (kept, ignored) by the ignore patterns."""
    kept, ignored = [], []
    for entry in scan:
        (ignored if any(matches(entry['rel'], p) for p in patterns) else kept).append(entry)
    return kept, ignored


def assign_groups(scan, groups):
    """Map each group name to its files (first matching group wins) plus the unassigned ones."""
    assigned = {name: [] for name in groups}
    unassigned = []
    for entry in scan:
        for name, patterns in groups.items():
            if any(matches(entry['rel'], p) for p in patterns):
                assigned[name].append(entry)
                break
        else:
            unassigned.append(entry)
    return assigned, unassigned


def select_only(scan, selectors, groups):
    """Files named by `--only`: each selector is a group name or a path/glob."""
    assigned, _ = assign_groups(scan, groups)
    wanted = set()
    for selector in selectors:
        if selector in assigned:
            wanted.update(e['rel'] for e in assigned[selector])
        else:
            wanted.update(e['rel'] for e in scan if matches(e['rel'], selector))
    return [e for e in scan if e['rel'] in wanted]


def changed_since(plugin_dir, ref):
    """Plugin-relative paths changed since `ref`, including uncommitted and untracked files."""
    def git(*args):
        result = subprocess.run(['git', '-C', str(plugin_dir), *args],
                                capture_output=True, text=True)
        if result.returncode != 0:
            raise ValueError(f'git {" ".join(args)}: {result.stderr.strip() or "falhou"}')
        return [line for line in result.stdout.splitlines() if line]

    changed = set(git('diff', '--name-only', '--relative', ref, '--', '.'))
    changed.update(git('ls-files', '--others', '--exclude-standard'))
    return changed


def last_tag(plugin_dir):
    """The most recent tag reachable from HEAD, or None (no git, no tags)."""
    result = subprocess.run(['git', '-C', str(plugin_dir), 'describe', '--tags', '--abbrev=0'],
                            capture_output=True, text=True)
    tag = result.stdout.strip()
    return tag if result.returncode == 0 and tag else None


def targets_tests(selectors, groups, tests_dir='tests'):
    """Does `--only` name something under tests/? Those files are not read unless asked for."""
    for selector in selectors or []:
        patterns = groups.get(selector, [selector])
        if any(p == tests_dir or p.startswith(tests_dir + '/') for p in patterns):
            return True
    return False


def select_since(scan, changed):
    return [e for e in scan if e['rel'] in changed]


def describe(only, since, full_files, scoped_files):
    """One line for the report header when the audit did not read the whole plugin."""
    parts = []
    if only:
        parts.append('apenas ' + ', '.join(only))
    if since:
        parts.append(f'alterados desde {since}')
    return (f'escopo parcial ({"; ".join(parts)}): {len(scoped_files)} de {len(full_files)} '
            'arquivos analisáveis')
