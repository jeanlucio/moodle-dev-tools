#!/usr/bin/env python3
"""Builds a shields.io endpoint-badge JSON from the Moodle Marketplace stats page.

The Marketplace plugin page ("last 90 days downloads") only shows a rolling 90-day
window, and there is no public API for anything wider (confirmed against
moodledev.io's Plugins directory API docs — the only download-count field,
`aggdownloads`, is returned by `local_plugins_get_maintained_plugins`, which requires
an authenticated maintainer token).

The `/stats` page for each plugin does have a wider view: a "Show chart data" toggle
reveals a monthly downloads table covering the trailing 12 *closed* months (the
current, still-in-progress month is never included). That table is server-rendered
in the page's own HTML — no login, no separate API call — so it can be scraped
directly. This script sums those months and writes the result as a shields.io
"endpoint" badge JSON (https://shields.io/badges/endpoint-badge), the same mechanism
already used for the MDL Shield badge in this ecosystem.

The anchor used to locate the table is the HTML id the Marketplace assigns to it,
`id="stats-downloads-monthly-table"` — not any specific number, so it keeps working
as the monthly figures change. If Marketplace ever redesigns the stats page this
anchor can silently stop matching; the script fails loudly (non-zero exit, clear
message) rather than writing a stale or wrong badge in that case.

Deliberately does NOT commit or push anything — it only writes
<plugin>/docs/badges/downloads.json. Committing is a separate, reviewed step.
"""

import json
import re
import sys
import urllib.request
from pathlib import Path

TABLE_ANCHOR = 'id="stats-downloads-monthly-table"'
ROW_RE = re.compile(r'<td>(\d{4}-\d{2})</td>\s*<td>(\d+)</td>')


def fetch_stats_html(marketplace_id: str) -> str:
    """Fetch the raw HTML of a plugin's Marketplace stats page.

    @param string $marketplace_id numeric Marketplace plugin id (e.g. "3583")
    @return string raw page HTML
    """
    url = f'https://marketplace.moodle.com/plugins/{marketplace_id}/stats'
    request = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read().decode('utf-8')


def extract_monthly_downloads(html: str) -> list[tuple[str, int]]:
    """Extract the (month, downloads) rows from the trailing-12-months table.

    @param string $html raw stats page HTML
    @return list of (yyyy-mm, downloads) tuples, in the order they appear on the page
    """
    anchor = html.find(TABLE_ANCHOR)
    if anchor == -1:
        raise RuntimeError(
            'anchor "stats-downloads-monthly-table" not found — the Marketplace '
            'page layout may have changed; the parser needs updating, not the badge.'
        )
    table_end = html.find('</table>', anchor)
    if table_end == -1:
        raise RuntimeError('found the table anchor but no closing </table> after it')
    table_html = html[anchor:table_end]
    rows = ROW_RE.findall(table_html)
    if not rows:
        raise RuntimeError('table anchor found, but no month/downloads rows matched')
    return [(month, int(count)) for month, count in rows]


def format_badge_message(total: int) -> str:
    """Format a download total as a compact badge message, e.g. "2.4k/yr".

    @param int $total sum of the trailing closed months
    @return string compact label for the badge's right-hand side
    """
    if total >= 1000:
        return f'{total / 1000:.1f}k/yr'
    return f'{total}/yr'


def resolve_docs_dir(plugin_dir: Path) -> Path:
    """Resolve and validate the docs/ directory for a plugin, since not every
    plugin in this ecosystem has a GitHub Pages docs/ site yet.

    @param Path $plugin_dir absolute path to the plugin's repository root
    @return Path the plugin's docs/ directory
    """
    docs_dir = plugin_dir / 'docs'
    if not docs_dir.is_dir():
        raise RuntimeError(
            f'{docs_dir} does not exist — this plugin has no docs/ GitHub Pages '
            'site yet, so there is nowhere export-ignored to put the badge JSON.'
        )
    return docs_dir


def main(argv: list[str]) -> int:
    """CLI entry point.

    @param list $argv [plugin_dir, marketplace_id]
    @return int process exit code
    """
    if len(argv) != 3:
        print('uso: marketplace_downloads.py <caminho-do-plugin> <id-marketplace>', file=sys.stderr)
        return 1

    plugin_dir = Path(argv[1]).resolve()
    marketplace_id = argv[2]

    if not plugin_dir.is_dir():
        print(f'erro: {plugin_dir} não existe', file=sys.stderr)
        return 1

    try:
        docs_dir = resolve_docs_dir(plugin_dir)
        html = fetch_stats_html(marketplace_id)
        rows = extract_monthly_downloads(html)
    except (RuntimeError, OSError) as exc:
        print(f'erro: {exc}', file=sys.stderr)
        return 1

    total = sum(count for _month, count in rows)
    message = format_badge_message(total)

    badges_dir = docs_dir / 'badges'
    badges_dir.mkdir(exist_ok=True)
    output_path = badges_dir / 'downloads.json'
    output_path.write_text(
        json.dumps({'schemaVersion': 1, 'label': 'downloads', 'message': message, 'color': 'blue'})
        + '\n'
    )

    print(f'meses lidos: {rows[0][0]} a {rows[-1][0]} ({len(rows)} meses)')
    print(f'total: {total} -> "{message}"')
    print(f'gravado em: {output_path}')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
