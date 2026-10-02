#!/usr/bin/env python3
"""Calibrate moodle-security-audit against MDL Shield's published reviews.

Each public MDL Shield review names the plugin's GitHub repository and the exact commit it
reviewed (every finding links to github.com/<owner>/<repo>/blob/<sha>/<file>#L<line>). That
makes it ground truth: clone that commit, run the audit on it, and compare finding by finding.
It is the only objective way to tell whether a rule change brought the local audit closer to
MDL Shield, instead of judging from one plugin at a time.

Subcommands:
  list                 public reviews on mdlshield.com/reviews (and which are fetched/run)
  fetch <review>       download one review and store its findings as ground truth
  run <review>         clone the reviewed commit, audit it, compare (spends AI quota)
  compare <review>     re-compare using the latest audit JSON of that review, no AI calls
  summary              one line per compared review: grade match, recall per finding type

<review> is a slug from `list` (e.g. tool_courserating_2026-10-02 or
publisher/exputo/mod_profilefield), a full mdlshield.com URL, or the path to a Markdown
export downloaded from the MDL Shield dashboard (how private reviews of your own plugins are
compared).

Everything is stored under ~/.moodle-security-audit-cache/calibration/ — review pages are
third-party content and do not belong in this repository.

Usage: security_audit_calibrate.py <subcommand> [args]  (normally via
moodle-security-audit-calibrate)
"""

import argparse
import html
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
AUDIT_SCRIPT = TOOLS_DIR / 'security_audit.py'
CALIBRATION_DIR = Path.home() / '.moodle-security-audit-cache' / 'calibration'
REVIEWS_URL = 'https://mdlshield.com/reviews'
# Without a browser User-Agent the site's WAF answers 403 to every request.
USER_AGENT = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) '
              'Chrome/128.0 Safari/537.36')

SEVERITIES = ['critical', 'high', 'medium', 'low', 'info']
# MDL Shield's own labels, mapped to the finding_type values security_audit.py uses.
TYPE_BY_LABEL = {
    'security': 'security',
    'code quality': 'code_quality',
    'compliance': 'compliance',
    'best practice': 'best_practice',
}
BLOB_RE = re.compile(r'https://github\.com/([\w.-]+/[\w.-]+)/blob/([0-9a-f]{40})/'
                     r'([^"#\s)]+)(?:#L(\d+))?')

# A finding counts as found when the audit reports one in the same file within this many
# lines of any location MDL Shield cites. Generous on purpose: the two tools often anchor
# the same defect on different lines of the same function.
LINE_WINDOW = 30


# --------------------------------------------------------------------------- #
#  Fetching and parsing                                                        #
# --------------------------------------------------------------------------- #

def http_get(url):
    request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read().decode('utf-8', errors='replace')


def slug_of(review):
    """Storage key for a review: the URL path after /reviews/, slashes flattened."""
    review = review.strip()
    if review.startswith('http'):
        review = review.split('/reviews/', 1)[-1]
    return review.strip('/').replace('/', '__')


def html_to_text(page):
    """Page text, one element per line, with GitHub source links kept as @@LOC markers.

    The links are the only place the file, line and commit of a finding appear, so they are
    turned into text before the tags are stripped instead of being lost with them.
    """
    page = re.sub(r'<script.*?</script>|<style.*?</style>', '', page, flags=re.S)
    # The whole opening <a> tag is replaced, not just the URL: a marker left inside the tag
    # would be stripped together with it a line below.
    page = re.sub(r'<a\b[^>]*href="(https://github\.com/[^"]+/blob/[^"]+)"[^>]*>',
                  lambda m: f'\n@@LOC {m.group(1)}@@\n', page)
    text = html.unescape(re.sub(r'<[^>]+>', '\n', page))
    return re.sub(r'\s*\n\s*', '\n', text)


PLAIN_LOCATION_RE = re.compile(
    r'\n([\w./-]+\.(?:php|mustache|js|xml|css|json|yml|md))(?::(\d+))?\n')


def _locations(segment):
    """File/line citations of one finding: GitHub links, or plain "path:line" text.

    Some reviews cite files without linking them (no commit in the page at all); the plain
    form keeps those findings matchable by file and line all the same.
    """
    locations, seen = [], set()
    for match in BLOB_RE.finditer(segment):
        repo, sha, path, line = match.groups()
        key = (path, line)
        if key in seen:
            continue
        seen.add(key)
        locations.append({'repo': repo, 'sha': sha, 'file': path,
                          'line': int(line) if line else None})
    if locations:
        return locations
    for match in PLAIN_LOCATION_RE.finditer(segment):
        path, line = match.groups()
        if '/' not in path and not re.search(r'\.(php|mustache)$', path):
            # A bare "form.js" is as likely a word in the prose as a file path.
            continue
        key = (path, line)
        if key in seen:
            continue
        seen.add(key)
        locations.append({'repo': None, 'sha': None, 'file': path,
                          'line': int(line) if line else None})
    return locations


def parse_review_html(page):
    """Ground truth from a public review page: grade, counts, source and findings."""
    text = html_to_text(page)
    truth = {'grade': None, 'counts': {}, 'repo': None, 'sha': None, 'findings': []}

    counts = re.search(r'\n([A-F]\+?)\nGrade\n(\d+)\nCritical\n(\d+)\nHigh\n(\d+)\nMedium\n'
                       r'(\d+)\nLow\n(\d+)\nInfo', text)
    if counts:
        truth['grade'] = counts.group(1)
        truth['counts'] = dict(zip(SEVERITIES, map(int, counts.groups()[1:])))
    for field in ('Version', 'Release', 'Reviewed for'):
        match = re.search(rf'\n{field}:\n([^\n]+)', text)
        if match:
            truth[field.lower().replace(' ', '_')] = match.group(1).strip()

    start = text.find('\nFindings\n')
    body = text[start:] if start >= 0 else text
    header = re.compile(r'\n(security|code quality|compliance|best practice)\n'
                        r'(Critical|High|Medium|Low|Info)\n([^\n]{5,200})')
    matches = list(header.finditer(body))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        truth['findings'].append({
            'finding_type': TYPE_BY_LABEL[match.group(1)],
            'severity': match.group(2).lower(),
            'title': match.group(3).strip(),
            'locations': _locations(body[match.start():end]),
        })
    _fill_source(truth)
    if not truth['repo']:
        header_repo = re.search(r'\nGitHub\n([\w.-]+/[\w.-]+)\n', text)
        if header_repo:
            truth['repo'] = header_repo.group(1).removesuffix('.git')
    return truth


def parse_review_markdown(content):
    """Ground truth from a Markdown export of the MDL Shield dashboard."""
    truth = {'grade': None, 'counts': {}, 'repo': None, 'sha': None, 'findings': []}
    grade = re.search(r'\*\*Grade:\s*([A-F]\+?)', content)
    if grade:
        truth['grade'] = grade.group(1)
    for severity in SEVERITIES:
        match = re.search(rf'(\d+)\s+{severity}\b', content.split('\n## ', 1)[0])
        if match:
            truth['counts'][severity] = int(match.group(1))
    version = re.search(r'\|\s*Version\s*\|\s*(\d+)\s*\(release\s*([^)]+)\)', content)
    if version:
        truth['version'], truth['release'] = version.group(1), version.group(2).strip()

    matches = list(re.finditer(r'^### \d+\. \[([A-Z]+)\] (.+)$', content, re.M))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        segment = content[match.start():end]
        label = re.search(r'\*\*Severity:\*\*\s*\w+\s*\(([^)]+)\)', segment)
        truth['findings'].append({
            'finding_type': TYPE_BY_LABEL.get((label.group(1) if label else '').lower(),
                                              'security'),
            'severity': match.group(1).lower(),
            'title': match.group(2).strip(),
            'locations': _locations(segment),
        })
    _fill_source(truth)
    return truth


def _fill_source(truth):
    """Repository and commit, taken from the first finding location that has them."""
    for finding in truth['findings']:
        for location in finding['locations']:
            if location['repo'] and location['sha']:
                truth['repo'], truth['sha'] = location['repo'], location['sha']
                return


def review_dir(slug):
    return CALIBRATION_DIR / slug


def load_truth(slug):
    path = review_dir(slug) / 'ground-truth.json'
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding='utf-8'))


def fetch(review):
    """Download (or read) one review, parse it and store the ground truth."""
    path = Path(review).expanduser()
    if path.is_file():
        slug = path.stem
        truth = parse_review_markdown(path.read_text(encoding='utf-8'))
        truth['source'] = str(path.resolve())
    else:
        slug = slug_of(review)
        url = review if review.startswith('http') else f'{REVIEWS_URL}/{review.strip("/")}'
        truth = parse_review_html(http_get(url))
        truth['source'] = url
    truth['slug'] = slug
    if not truth['findings'] and not truth['grade']:
        raise RuntimeError(f'nada reconhecível em {review} — o formato da página mudou?')
    target = review_dir(slug)
    target.mkdir(parents=True, exist_ok=True)
    (target / 'ground-truth.json').write_text(
        json.dumps(truth, ensure_ascii=False, indent=2), encoding='utf-8')
    return slug, truth


def list_reviews():
    page = http_get(REVIEWS_URL)
    slugs = sorted(set(re.findall(r'href="/reviews/([^"]+)"', page)))
    return [s for s in slugs if not s.startswith('publisher/') or s.count('/') == 2]


# --------------------------------------------------------------------------- #
#  Running the audit on the reviewed commit                                    #
# --------------------------------------------------------------------------- #

def checkout(truth, slug):
    """Clone the reviewed repository at the reviewed commit; reuse an existing clone.

    When the review links no code (so there is no commit), the release tag stated in the
    page is tried instead, with and without a leading "v"; failing that, the clone stays on
    the default branch and a warning says the comparison may be against different code.
    """
    if not truth.get('repo'):
        raise RuntimeError('a revisão não indica o repositório')
    source = review_dir(slug) / 'src'
    if not (source / '.git').is_dir():
        source.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(['git', 'clone', '--quiet', '--filter=blob:none',
                        f'https://github.com/{truth["repo"]}.git', str(source)], check=True)
    refs = [truth['sha']] if truth.get('sha') else []
    release = (truth.get('release') or '').split()[0] if truth.get('release') else ''
    if release:
        refs += [release, f'v{release.lstrip("v")}', release.lstrip('v')]
    for ref in refs:
        done = subprocess.run(['git', '-C', str(source), 'checkout', '--quiet', '--detach',
                               ref], capture_output=True)
        if done.returncode == 0:
            truth['checked_out'] = ref
            return source
    print('aviso: commit/tag da revisão não encontrados — comparando com o branch padrão,'
          ' que pode não ser o código revisado', file=sys.stderr)
    truth['checked_out'] = 'default branch'
    return source


def latest_audit_json(source):
    reports = sorted((source / '.plans' / 'security-audit').glob('*.json'))
    return reports[-1] if reports else None


def run(slug, extra_args):
    truth = load_truth(slug)
    if truth is None:
        slug, truth = fetch(slug)
    source = checkout(truth, slug)
    command = [sys.executable, '-u', str(AUDIT_SCRIPT), str(source), '--json'] + extra_args
    print('$ ' + ' '.join(command))
    subprocess.run(command, check=True)
    return compare(slug)


# --------------------------------------------------------------------------- #
#  Comparison                                                                  #
# --------------------------------------------------------------------------- #

def _audit_locations(finding):
    locations = [(finding.get('file'), finding.get('line'))]
    for extra in finding.get('extra_locations') or []:
        if isinstance(extra, dict):
            locations.append((extra.get('file'), extra.get('line')))
    return [(f, ln) for f, ln in locations if f]


def _words(text):
    """Crude stems (first 6 letters), so "validate" and "validation" count as one word."""
    return {w[:6] for w in re.findall(r'[a-z0-9_]{4,}', (text or '').lower())}


def _score(truth_finding, audit_finding):
    """Sortable match strength; its first element is 2 (same file, nearby line), 1 (same
    file only) or 0 (unrelated).

    Proximity alone ties far too often — one view.php can carry three different findings
    within 30 lines of each other — so ties are broken by finding type, then by how many
    words the MDL title shares with the local title (the local titles are in Portuguese,
    but identifiers such as logstore_standard_log or validation survive), and only then by
    line distance, which inside the window is a weak signal.
    """
    proximity, distance = 0, 10 ** 6
    for location in truth_finding['locations']:
        for file, line in _audit_locations(audit_finding):
            if file != location['file']:
                continue
            if isinstance(line, int) and location['line'] is not None:
                gap = abs(line - location['line'])
                if gap <= LINE_WINDOW:
                    proximity = 2
                distance = min(distance, gap)
            proximity = max(proximity, 1)
    if not proximity:
        return (0, 0, 0, 0)
    same_type = int(truth_finding['finding_type'] ==
                    audit_finding.get('finding_type', 'security'))
    overlap = len(_words(truth_finding['title']) & _words(audit_finding.get('title')))
    return (proximity, same_type, overlap, -distance)


def match_findings(truth_findings, audit_findings):
    """Greedy one-to-one matching, strongest pairs first."""
    pairs = sorted(
        ((_score(t, a), ti, ai) for ti, t in enumerate(truth_findings)
         for ai, a in enumerate(audit_findings)),
        key=lambda pair: pair[0], reverse=True)
    matched_truth, matched_audit, result = set(), set(), {}
    for score, ti, ai in pairs:
        if score[0] == 0:
            break
        if ti in matched_truth or ai in matched_audit:
            continue
        matched_truth.add(ti)
        matched_audit.add(ai)
        result[ti] = (ai, score[0])
    extras = [ai for ai in range(len(audit_findings)) if ai not in matched_audit]
    return result, extras


def compare(slug):
    truth = load_truth(slug)
    if truth is None:
        raise RuntimeError(f'{slug}: rode fetch primeiro')
    source = review_dir(slug) / 'src'
    report = latest_audit_json(source)
    if report is None:
        raise RuntimeError(f'{slug}: nenhuma auditoria em {source} — rode run primeiro')
    ctx = json.loads(report.read_text(encoding='utf-8'))
    audit = [f for f in ctx.get('confirmed', []) if isinstance(f, dict)]

    matches, extras = match_findings(truth['findings'], audit)
    lines = [f'# Calibração — {slug}', '',
             f'Revisão MDL Shield: {truth.get("source")}',
             f'Commit: `{truth.get("repo")}@{(truth.get("sha") or "")[:7]}`',
             f'Auditoria local: `{report}`', '',
             '| | MDL Shield | moodle-security-audit |', '|---|---|---|',
             f'| Nota | **{truth.get("grade")}** | **{ctx.get("grade")}** |',
             '', '## Achados do MDL Shield', '',
             '| Sev. | Tipo | Achado MDL | Local | Encontrado? | Achado local |',
             '|---|---|---|---|---|---|']
    stats = {}
    for index, finding in enumerate(truth['findings']):
        location = finding['locations'][0] if finding['locations'] else {}
        where = f'`{location.get("file")}:{location.get("line")}`' if location else '—'
        key = finding['finding_type']
        stats.setdefault(key, [0, 0])
        counts = finding['severity'] != 'info'
        if counts:
            stats[key][1] += 1
        if index in matches:
            ai, score = matches[index]
            local = audit[ai]
            status = 'sim' if score == 2 else 'mesmo arquivo'
            if counts and score == 2:
                stats[key][0] += 1
            local_text = (f'{local.get("title")} (`{local.get("severity")}`, '
                          f'{local.get("finding_type", "security")})')
        else:
            status, local_text = '**não**', '—'
        lines.append(f'| {finding["severity"]} | {key} | {finding["title"]} | {where} | '
                     f'{status} | {local_text} |')

    lines += ['', '## Achados só da auditoria local', '']
    if extras:
        lines += ['| Sev. | Tipo | Achado | Local |', '|---|---|---|---|']
        for ai in extras:
            finding = audit[ai]
            lines.append(f'| {finding.get("severity")} | '
                         f'{finding.get("finding_type", "security")} | '
                         f'{finding.get("title")} | '
                         f'`{finding.get("file")}:{finding.get("line")}` |')
    else:
        lines.append('Nenhum.')
    lines += ['', '## Recall (achados low ou acima do MDL encontrados no mesmo local)', '',
              '| Tipo | Encontrados | Total |', '|---|---|---|']
    for key, (found, total) in sorted(stats.items()):
        if total:
            lines.append(f'| {key} | {found} | {total} |')
    lines.append('')

    output = review_dir(slug) / 'comparison.md'
    output.write_text('\n'.join(lines), encoding='utf-8')
    result = {
        'slug': slug, 'mdl_grade': truth.get('grade'), 'local_grade': ctx.get('grade'),
        'recall': stats, 'extras': len(extras),
    }
    (review_dir(slug) / 'comparison.json').write_text(json.dumps(result, indent=2),
                                                      encoding='utf-8')
    print('\n'.join(lines))
    print(f'Comparação: {output}')
    return result


def summary():
    rows = []
    for path in sorted(CALIBRATION_DIR.glob('*/comparison.json')):
        rows.append(json.loads(path.read_text(encoding='utf-8')))
    if not rows:
        print('Nenhuma comparação ainda. Rode: moodle-security-audit-calibrate run <revisão>')
        return
    print('| Revisão | MDL | Local | Recall (encontrados/total low+) | Extras |')
    print('|---|---|---|---|---|')
    totals = {}
    for row in rows:
        found = sum(v[0] for v in row['recall'].values())
        total = sum(v[1] for v in row['recall'].values())
        for key, (f, t) in row['recall'].items():
            totals.setdefault(key, [0, 0])
            totals[key][0] += f
            totals[key][1] += t
        agree = '' if row['mdl_grade'] == row['local_grade'] else ' ≠'
        print(f'| {row["slug"]} | {row["mdl_grade"]} | {row["local_grade"]}{agree} | '
              f'{found}/{total} | {row["extras"]} |')
    print('')
    print('Recall agregado por tipo: ' + ', '.join(
        f'{k} {f}/{t}' for k, (f, t) in sorted(totals.items()) if t))


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('list')
    fetch_parser = sub.add_parser('fetch')
    fetch_parser.add_argument('review')
    run_parser = sub.add_parser('run')
    run_parser.add_argument('review')
    run_parser.add_argument('--with-phpstan', action='store_true',
                            help='roda também o PHPStan (fora da árvore do Moodle, gera ruído)')
    run_parser.add_argument('audit_args', nargs=argparse.REMAINDER,
                            help='opções repassadas ao security_audit.py (após --)')
    compare_parser = sub.add_parser('compare')
    compare_parser.add_argument('review')
    sub.add_parser('summary')
    args = parser.parse_args()

    try:
        if args.command == 'list':
            fetched = {p.name for p in CALIBRATION_DIR.glob('*') if p.is_dir()}
            for slug in list_reviews():
                state = ''
                if (review_dir(slug_of(slug)) / 'comparison.json').is_file():
                    state = '  [comparada]'
                elif slug_of(slug) in fetched:
                    state = '  [baixada]'
                print(f'{slug}{state}')
        elif args.command == 'fetch':
            slug, truth = fetch(args.review)
            print(f'{slug}: nota {truth["grade"]}, {len(truth["findings"])} achado(s), '
                  f'{truth.get("repo")}@{(truth.get("sha") or "?")[:7]}')
            for finding in truth['findings']:
                where = finding['locations'][0] if finding['locations'] else {}
                print(f'  [{finding["severity"]}/{finding["finding_type"]}] '
                      f'{finding["title"]} — {where.get("file")}:{where.get("line")}')
        elif args.command == 'run':
            extra = [a for a in args.audit_args if a != '--']
            if not args.with_phpstan and '--no-phpstan' not in extra:
                extra.append('--no-phpstan')
            run(slug_of(args.review) if not Path(args.review).is_file()
                else fetch(args.review)[0], extra)
        elif args.command == 'compare':
            compare(slug_of(args.review) if not Path(args.review).is_file()
                    else Path(args.review).stem)
        elif args.command == 'summary':
            summary()
    except (RuntimeError, subprocess.CalledProcessError, OSError) as exc:
        print(f'erro: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
