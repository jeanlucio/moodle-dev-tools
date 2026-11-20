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

A plugin that is big enough and has no groups of its own gets suggested ones (`suggest_groups`),
so nobody has to write the file by hand; `save_groups` turns the suggestion into the config.
"""

import fnmatch
import json
import subprocess

CONFIG_REL = '.plans/security-audit/config.json'

# Calibrated on block_playerhud, whose hand-made groups each fit one quota window: the source
# groups hold 6-9.5k lines and the tests groups 18k (tests are cheaper per line to read as a block).
GROUP_BUDGET_LINES = 10000
TESTS_GROUP_BUDGET_LINES = 20000
# Below this many source lines (tests/ excluded) a plugin is read whole: two batches or less.
AUTO_GROUPS_MIN_LINES = 20000
TESTS_DIR = 'tests'

# The slices of block_playerhud's config, which is the one that was proven in use, so the names
# mean the same in every plugin. Order matters (first match wins): the catch-all C and T1 come
# after the narrower groups. A root-level file is always an entry point or metadata, hence "*".
SEMANTIC_GROUPS = {
    'A': ['*', 'classes/controller/', 'classes/form/', 'admin/', 'ajax/', 'cli/'],
    'B': ['classes/external/'],
    'D': ['classes/ai/', 'classes/privacy/', 'backup/', 'db/', '.github/'],
    'E1': ['classes/output/'],
    'E2': ['templates/'],
    'F': ['amd/src/', 'yui/src/'],
    'C': ['classes/'],
    'T2': ['tests/external/', 'tests/output/', 'tests/controller/', 'tests/behat/'],
    'T1': ['tests/'],
}
TESTS_GROUPS = ('T2', 'T1')


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


def _is_tests(rel):
    return rel == TESTS_DIR or rel.startswith(TESTS_DIR + '/')


def _units(files, depth, limit):
    """Split files into (pattern, lines) units no bigger than `limit`, refining oversized dirs."""
    by_key = {}
    for entry in files:
        parts = entry['rel'].split('/')
        key = '/'.join(parts[:depth]) + ('/' if len(parts) > depth else '')
        by_key.setdefault(key, []).append(entry)
    units = []
    for key in sorted(by_key):
        members = by_key[key]
        lines = sum(e['lines'] for e in members)
        if key.endswith('/') and len(members) == 1:
            units.append((members[0]['rel'], lines))
        elif key.endswith('/') and lines > limit:
            units.extend(_units(members, depth + 1, limit))
        else:
            units.append((key, lines))
    return units


def _pack(units, limit):
    """First-fit in path order: each unit goes to the first part with room, so a big directory in
    the middle does not leave a tiny part at the end. Returns lists of units under `limit`."""
    parts, totals = [], []
    for unit in units:
        for index, total in enumerate(totals):
            if total + unit[1] <= limit:
                parts[index].append(unit)
                totals[index] += unit[1]
                break
        else:
            parts.append([unit])
            totals.append(unit[1])
    return parts


def _build_groups(plan, explicit=()):
    """Turn a plan {bucket: (patterns, [file lists])} into an ordered {name: patterns} dict.

    A bucket that fits stays whole and keeps its readable patterns; one that does not becomes
    `<name>-1`, `<name>-2`... Buckets named in `explicit` list their files one by one.
    """
    groups, intended = {}, {}
    for name, (patterns, parts, whole) in plan.items():
        if len(parts) == 1 and name not in explicit:
            groups[name] = patterns
            intended[name] = {e['rel'] for e in whole}
            continue
        for number, part in enumerate(parts, 1):
            label = f'{name}-{number}' if len(parts) > 1 else name
            files = [e['rel'] for e in part['files']]
            groups[label] = files if name in explicit else part['patterns']
            intended[label] = set(files)
    return groups, intended


def suggest_groups(scan, budget=GROUP_BUDGET_LINES, tests_budget=TESTS_GROUP_BUDGET_LINES,
                   min_lines=AUTO_GROUPS_MIN_LINES):
    """Groups for a plugin that has none: {} when it is small enough to be read whole.

    The files fall into the semantic slices of SEMANTIC_GROUPS; a slice over its line budget is
    split along directory boundaries into `<name>-1`, `<name>-2`... The result is checked against
    `assign_groups`, because a directory pattern can claim files of a later group; a slice whose
    patterns do not reproduce the intended files falls back to listing them one by one.
    """
    if sum(e['lines'] for e in scan if not _is_tests(e['rel'])) < min_lines:
        return {}
    templates = {name: list(patterns) for name, patterns in SEMANTIC_GROUPS.items()}
    _, leftover = assign_groups(scan, templates)
    templates['C'] += sorted({e['rel'].split('/')[0] + '/' for e in leftover})
    assigned, _ = assign_groups(scan, templates)

    plan = {}
    for name, entries in assigned.items():
        if not entries:
            continue
        limit = tests_budget if name in TESTS_GROUPS else budget
        patterns = [p for p in templates[name] if any(matches(e['rel'], p) for e in entries)]
        if sum(e['lines'] for e in entries) <= limit:
            plan[name] = (patterns, [{'files': entries, 'patterns': patterns}], entries)
            continue
        parts = []
        for packed in _pack(_units(entries, 1, limit), limit):
            cover = [pattern for pattern, _ in packed]
            files = [e for e in entries if any(matches(e['rel'], p) for p in cover)]
            parts.append({'files': files, 'patterns': cover})
        plan[name] = (patterns, parts, entries)

    groups, intended = _build_groups(plan)
    actual, _ = assign_groups(scan, groups)
    wrong = {label for label, entries in actual.items()
             if {e['rel'] for e in entries} != intended[label]}
    if wrong:
        explicit = {label.rsplit('-', 1)[0] if label not in plan else label for label in wrong}
        groups, intended = _build_groups(plan, explicit)
        actual, _ = assign_groups(scan, groups)
        if any({e['rel'] for e in entries} != intended[label] for label, entries in actual.items()):
            return {}
    return groups


def save_groups(plugin_dir, groups):
    """Write `groups` into the plugin's config.json, keeping its `ignore` list.

    Refuses to overwrite groups somebody already wrote: those carry history in state.json.
    """
    config = load_config(plugin_dir)
    if config['groups']:
        raise ValueError('o plugin já tem grupos em ' + CONFIG_REL)
    if not groups:
        raise ValueError('não há grupos para salvar')
    path = plugin_dir / CONFIG_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'ignore': config['ignore'], 'groups': groups},
                               ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
