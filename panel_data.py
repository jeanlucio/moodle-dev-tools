"""What the panel shows about a plugin: discovery, last audit, groups, estimate and reports.

Everything here only reads. The plugins come from the Moodle public directory (own plugins are
the ones that are git repositories), the grades from the audit reports in each plugin's
`.plans/security-audit/`, the groups from its `config.json` and the history from `state.json`.
"""

import functools
import os
import re
import subprocess
from pathlib import Path

import audit_focus
import audit_scope

PUBLIC = Path(os.environ.get('MDT_MOODLE_PUBLIC', '/home/ubuntu/meu-moodle/html/public'))
# Where each plugin type lives under the Moodle public directory.
TYPE_DIRS = {
    'mod': 'mod', 'block': 'blocks', 'local': 'local', 'filter': 'filter', 'report': 'report',
    'availability': 'availability/condition', 'format': 'course/format',
    'tiny': 'lib/editor/tiny/plugins', 'theme': 'theme', 'qtype': 'question/type',
}
REPORT_DIR = '.plans/security-audit'
REPORT_NAME_RE = re.compile(r'^[A-Za-z0-9_]+-(\d{4}-\d\d-\d\d)(?:-(\d{6}))?\.md$')
SCOPE_RE = re.compile(r'Escopo parcial \((.*?)\): (\d+) de (\d+) arquivos')
SAFE_FILE_RE = re.compile(r'^[A-Za-z0-9_.\-]+\.md$')
SAFE_REF_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._/\-]{0,59}$')
BATCH_LINES = 10000


@functools.lru_cache(maxsize=1)
def owner():
    """The GitHub owner of the ecosystem: MDT_GITHUB_OWNER, else the owner of this repository."""
    configured = os.environ.get('MDT_GITHUB_OWNER')
    if configured:
        return configured
    result = subprocess.run(['git', '-C', str(Path(__file__).resolve().parent), 'remote',
                             'get-url', 'origin'], capture_output=True, text=True)
    match = re.search(r'github\.com[:/]([^/]+)/', result.stdout)
    return match.group(1) if match else ''


def is_own(directory):
    """A plugin is mine when its origin belongs to the owner, or when it has no origin yet.

    Third-party plugins cloned into the tree are git repositories too; they are not audited here.
    """
    result = subprocess.run(['git', '-C', str(directory), 'remote', 'get-url', 'origin'],
                            capture_output=True, text=True)
    url = result.stdout.strip()
    if result.returncode != 0 or not url:
        return True
    return bool(owner()) and re.search(r'github\.com[:/]' + re.escape(owner()) + r'/', url) is not None


def discover_plugins(public=None):
    """The ecosystem's own plugins: git repositories with a version.php, ordered by component."""
    public = Path(public or PUBLIC)
    found = []
    for plugintype, rel in TYPE_DIRS.items():
        base = public / rel
        if not base.is_dir():
            continue
        for directory in sorted(base.iterdir()):
            if not (directory / '.git').exists() or not (directory / 'version.php').is_file():
                continue
            if not is_own(directory):
                continue
            version = _read_version(directory / 'version.php')
            found.append({
                'path': f'{rel}/{directory.name}',
                'type': plugintype,
                'component': version.get('component') or f'{plugintype}_{directory.name}',
                'version': version.get('version', ''),
                'release': version.get('release', ''),
            })
    return sorted(found, key=lambda p: p['component'])


def find_plugin(path, public=None):
    """The discovered plugin whose path is exactly `path`, else None (never a free-form path)."""
    return next((p for p in discover_plugins(public) if p['path'] == path), None)


def plugin_dir(plugin, public=None):
    return Path(public or PUBLIC) / plugin['path']


def _read_version(path):
    text = path.read_text(encoding='utf-8', errors='replace')
    out = {}
    for key in ('component', 'release'):
        match = re.search(r"\$plugin->" + key + r"\s*=\s*'([^']*)'", text)
        if match:
            out[key] = match.group(1)
    match = re.search(r'\$plugin->version\s*=\s*(\d+)', text)
    if match:
        out['version'] = match.group(1)
    return out


def report_files(directory):
    """Audit reports of a plugin, newest first (name order is date order)."""
    folder = directory / REPORT_DIR
    if not folder.is_dir():
        return []
    return sorted((p for p in folder.iterdir() if REPORT_NAME_RE.match(p.name)),
                  key=lambda p: p.name, reverse=True)


def parse_report(path):
    """Grade, security grade, focus and scope of one report, read from its header."""
    text = path.read_text(encoding='utf-8', errors='replace')
    head = text[:6000]
    grade = re.search(r'## Nota geral\s+# (\S+)', head)
    security = re.search(r'Nota contando só os achados de segurança: \*\*(\S+?)\*\*', head)
    focus = re.search(r'\*\*Foco: (segurança|qualidade)\.\*\*', head)
    only, since = [], None
    scope = SCOPE_RE.search(head)
    if scope:
        for part in scope.group(1).split('; '):
            if part.startswith('apenas '):
                only = [name.strip() for name in part[len('apenas '):].split(',')]
            elif part.startswith('alterados desde '):
                since = part[len('alterados desde '):].strip()
    return {
        'file': path.name,
        'date': REPORT_NAME_RE.match(path.name).group(1),
        'grade': grade.group(1) if grade else '',
        'security_grade': security.group(1) if security else '',
        'focus': {'segurança': 'security', 'qualidade': 'quality'}.get(
            focus.group(1) if focus else '', 'all'),
        'partial': 'Escopo parcial' in head,
        'only': only,
        'since': since,
        'tests_read': 'Testes (`tests/`): lidos a fundo' in head,
        # A verification that failed leaves candidates unchecked, so a clean-looking grade is not
        # a result (the tool now marks such runs inconclusive; older reports did not).
        'verification_failed': 'verificação falhou' in text,
    }


def last_audit(directory):
    files = report_files(directory)
    return parse_report(files[0]) if files else None


def reports(directory):
    return [parse_report(path) for path in report_files(directory)]


def read_report(directory, name):
    """The text of a report, only if `name` is a plain report file name of this plugin."""
    if not SAFE_FILE_RE.match(name or '') or not REPORT_NAME_RE.match(name):
        return None
    path = directory / REPORT_DIR / name
    return path.read_text(encoding='utf-8', errors='replace') if path.is_file() else None


def scan_scope(directory, with_tests=False, only=None, since=None):
    """(files in scope, all analysable files, ignored files) as the audit would read them."""
    import security_audit
    config = audit_scope.load_config(directory)
    include_tests = with_tests or audit_scope.targets_tests(only, config['groups'],
                                                            security_audit.TESTS_DIR)
    scan, _ = security_audit.collect_files(directory, include_tests=include_tests)
    scan, ignored = audit_scope.apply_ignore(scan, config['ignore'])
    full = scan
    if only:
        scan = audit_scope.select_only(scan, only, config['groups'])
    if since:
        scan = audit_scope.select_since(scan, audit_scope.changed_since(directory, since))
    return scan, full, ignored


def _ignored_reason(report, reads_tests):
    """Why a report cannot count as having audited a group, or '' when it can."""
    if report['grade'].lower().startswith('inconclusiva'):
        return 'inconclusiva (cota)'
    if report['verification_failed']:
        return 'a verificação falhou'
    if report['since']:
        return f'só leu o que mudou desde {report["since"]}'
    if reads_tests and not report['tests_read']:
        return 'não leu tests/'
    return ''


def group_coverage(directory, names, test_groups=()):
    """For each group: the newest report that audited it, and the newer one that was set aside.

    A report covers a group when it read the whole plugin, or when it was a partial run that named
    the group. It does not count when the run was inconclusive, when its verification failed, when
    it was narrowed by `--since`, or, for a group that reads tests/, when it did not read tests/.
    `ignored` is the newest report set aside before a usable one, so the panel can say why a
    recent run does not show up.
    """
    parsed = [parse_report(path) for path in report_files(directory)]
    found = {}
    for name in names:
        entry = {'last': None, 'ignored': None}
        for report in parsed:
            reason = _ignored_reason(report, name in test_groups)
            covers = not report['partial'] or name in report['only']
            if not covers:
                continue
            if reason:
                if entry['ignored'] is None:
                    entry['ignored'] = {'date': report['date'], 'file': report['file'], 'reason': reason}
                continue
            entry['last'] = {**report, 'scope': 'group' if report['partial'] else 'plugin'}
            break
        found[name] = entry
    return found


def groups_info(directory):
    """The configured groups with their size, last audit and whether the security scope is closed.

    The sizes count tests/ too, so a tests group shows what naming it would read; `tests` tells the
    reader that the group reads tests/ (which a run skips otherwise, because it costs more quota).
    """
    import security_audit
    config = audit_scope.load_config(directory)
    if not config['groups']:
        return []
    scan, _, _ = scan_scope(directory, with_tests=True)
    assigned, _ = audit_scope.assign_groups(scan, config['groups'])
    test_groups = {name for name in assigned if audit_scope.targets_tests(
        [name], config['groups'], security_audit.TESTS_DIR)}
    coverage = group_coverage(directory, list(assigned), test_groups)
    return [{'name': name, 'files': len(entries), 'lines': sum(e['lines'] for e in entries),
             'closed': audit_focus.is_closed(directory, name), 'tests': name in test_groups,
             'last': coverage[name]['last'], 'ignored': coverage[name]['ignored']}
            for name, entries in assigned.items()]


def groups_summary(directory):
    """(covered, closed, total) over the plugin's groups; None when it has no groups."""
    groups = groups_info(directory)
    if not groups:
        return None
    return {'total': len(groups), 'covered': sum(1 for g in groups if g['last']),
            'closed': sum(1 for g in groups if g['closed'])}


def tags(directory, limit=25):
    result = subprocess.run(['git', '-C', str(directory), 'tag', '--sort=-creatordate'],
                            capture_output=True, text=True)
    return [t for t in result.stdout.split() if SAFE_REF_RE.match(t)][:limit]


def ref_exists(directory, ref):
    """Is `ref` a commit-ish of the plugin repository? `ref` must already match SAFE_REF_RE."""
    if not SAFE_REF_RE.match(ref or ''):
        return False
    result = subprocess.run(['git', '-C', str(directory), 'rev-parse', '--verify', '--quiet',
                             f'{ref}^{{commit}}'], capture_output=True, text=True)
    return result.returncode == 0


def estimate(directory, options):
    """How much a run with these options would read: files, lines and AI calls (batches)."""
    import security_audit
    since = options.get('since')
    note = ''
    if (options['focus'] == 'quality' and not options.get('full') and not options.get('only')
            and not since):
        since = audit_scope.last_tag(directory)
        note = f'foco qualidade lê só o que mudou desde {since}' if since else ''
    scan, full, ignored = scan_scope(directory, options.get('with_tests', False),
                                     options.get('only') or None, since)
    batches = security_audit.build_batches(scan, BATCH_LINES) if scan else []
    return {
        'files': len(scan), 'lines': sum(e['lines'] for e in scan), 'batches': len(batches),
        'plugin_files': len(full), 'plugin_lines': sum(e['lines'] for e in full),
        'since': since, 'note': note,
    }
