#!/usr/bin/env python3
# Monitor de conversas do GitHub aguardando resposta do dono.
# Varre Discussions, Issues abertas e PRs abertos de todos os repositórios da conta
# autenticada no `gh` e avisa via Telegram quando o último a falar numa thread não é o
# dono há mais de N dias. Silencioso quando não há nada pendente.
#
# Motivação: Discussions do GitHub notificam de forma bem mais discreta que Issues — uma
# sugestão de feature ficou quase sem resposta por isso (local_resourcestats, 09/09/2026).

import argparse
import datetime
import html
import json
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

ENV_FILE   = Path.home() / '.phpcs-ai.env'
STATE_FILE = Path.home() / '.github-replies-watch-state.json'
LOG_FILE   = Path.home() / '.moodle-plugins-monitor.log'
USER_AGENT = 'GitHubRepliesWatch/1.0'

# cron runs with a minimal PATH; gh is normally at /usr/bin/gh on Debian/Ubuntu.
GH = shutil.which('gh') or '/usr/bin/gh'

DISCUSSIONS_QUERY = '''
query($owner:String!, $name:String!) {
  repository(owner:$owner, name:$name) {
    discussions(first:50, orderBy:{field:UPDATED_AT, direction:DESC}) {
      nodes {
        number title url createdAt
        author { login }
        comments(first:50) {
          nodes {
            author { login } createdAt
            replies(first:50) { nodes { author { login } createdAt } }
          }
        }
      }
    }
  }
}'''

ISSUES_QUERY = '''
query($owner:String!, $name:String!) {
  repository(owner:$owner, name:$name) {
    issues(first:50, states:OPEN, orderBy:{field:UPDATED_AT, direction:DESC}) {
      nodes { number title url createdAt author { login }
        comments(last:1) { nodes { author { login } createdAt } } }
    }
    pullRequests(first:50, states:OPEN, orderBy:{field:UPDATED_AT, direction:DESC}) {
      nodes { number title url createdAt author { login }
        comments(last:1) { nodes { author { login } createdAt } } }
    }
  }
}'''


# ---------------------------------------------------------------------------
# Utilitários
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{ts}] github-replies-watch: {msg}'
    print(line)
    with LOG_FILE.open('a') as f:
        f.write(line + '\n')


def load_env() -> dict:
    env: dict = {}
    if not ENV_FILE.exists():
        return env
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            key, _, val = line.partition('=')
            env[key.strip()] = val.strip()
    return env


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def gh_json(args: list[str]) -> dict | list | None:
    out = subprocess.run([GH, *args], capture_output=True, text=True)
    if out.returncode != 0:
        log(f'gh falhou ({" ".join(args[:2])}): {out.stderr.strip()[:200]}')
        return None
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return None


def graphql(query: str, owner: str, name: str) -> dict | None:
    data = gh_json(['api', 'graphql', '-f', f'query={query}', '-f', f'owner={owner}', '-f', f'name={name}'])
    if not data or 'data' not in data or not data['data'].get('repository'):
        return None
    return data['data']['repository']


def login(node: dict | None) -> str:
    return (node or {}).get('login') or '?'


def parse_ts(iso: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(iso.replace('Z', '+00:00'))


# ---------------------------------------------------------------------------
# Varredura
# ---------------------------------------------------------------------------

def list_repos(owner: str) -> list[dict]:
    repos = gh_json([
        'repo', 'list', owner, '--limit', '200',
        '--json', 'name,hasDiscussionsEnabled,isArchived',
    ]) or []
    return [r for r in repos if not r.get('isArchived')]


def pending_discussions(owner: str, repo: str) -> list[dict]:
    data = graphql(DISCUSSIONS_QUERY, owner, repo)
    if not data:
        return []
    found = []
    for d in data['discussions']['nodes']:
        events = [(d['createdAt'], login(d['author']))]
        for c in d['comments']['nodes']:
            events.append((c['createdAt'], login(c['author'])))
            for r in c['replies']['nodes']:
                events.append((r['createdAt'], login(r['author'])))
        events.sort()
        lastwhen, lastwho = events[-1]
        if lastwho != owner:
            found.append({
                'key': f'{repo}#discussion-{d["number"]}',
                'repo': repo, 'kind': 'discussion', 'number': d['number'],
                'title': d['title'], 'url': d['url'], 'who': lastwho, 'when': lastwhen,
            })
    return found


def pending_issues_and_prs(owner: str, repo: str) -> list[dict]:
    data = graphql(ISSUES_QUERY, owner, repo)
    if not data:
        return []
    found = []
    for kind, nodes in (('issue', data['issues']['nodes']), ('pr', data['pullRequests']['nodes'])):
        for it in nodes:
            last = it['comments']['nodes']
            if last:
                lastwho, lastwhen = login(last[0]['author']), last[0]['createdAt']
            else:
                lastwho, lastwhen = login(it['author']), it['createdAt']
            if lastwho != owner:
                found.append({
                    'key': f'{repo}#{kind}-{it["number"]}',
                    'repo': repo, 'kind': kind, 'number': it['number'],
                    'title': it['title'], 'url': it['url'], 'who': lastwho, 'when': lastwhen,
                })
    return found


def scan(owner: str) -> tuple[list[dict], int]:
    repos = list_repos(owner)
    pending: list[dict] = []
    for r in repos:
        name = r['name']
        if r.get('hasDiscussionsEnabled'):
            pending.extend(pending_discussions(owner, name))
        pending.extend(pending_issues_and_prs(owner, name))
    return pending, len(repos)


# ---------------------------------------------------------------------------
# Notificação
# ---------------------------------------------------------------------------

KIND_LABEL = {'discussion': 'Discussion', 'issue': 'Issue', 'pr': 'PR'}


def age_days(iso: str, now: datetime.datetime) -> int:
    return (now - parse_ts(iso)).days


def format_message(items: list[dict], now: datetime.datetime) -> str:
    # HTML rather than Markdown: titles, logins and repo names come from GitHub and routinely
    # contain "_" or "*", which Telegram's Markdown treats as formatting and then rejects the
    # whole message when unbalanced. With HTML, escaping is enough.
    e = html.escape
    lines = ['<b>Conversas no GitHub aguardando sua resposta</b>', '']
    byrepo: dict[str, list[dict]] = {}
    for it in items:
        byrepo.setdefault(it['repo'], []).append(it)
    for repo in sorted(byrepo):
        lines.append(f'<b>{e(repo)}</b>')
        for it in sorted(byrepo[repo], key=lambda x: x['when']):
            days = age_days(it['when'], now)
            ago = 'hoje' if days == 0 else f'há {days} dia{"s" if days != 1 else ""}'
            lines.append(f'• {KIND_LABEL[it["kind"]]} #{it["number"]} — {e(it["title"])}')
            lines.append(f'  último: {e(it["who"])}, {ago}')
            lines.append(f'  {e(it["url"])}')
        lines.append('')
    return '\n'.join(lines).rstrip()


def send_telegram(token: str, chat_id: str, text: str) -> None:
    body = json.dumps({
        'chat_id': chat_id,
        'text': text,
        'parse_mode': 'HTML',
        'disable_web_page_preview': True,
    }).encode()
    req = urllib.request.Request(
        f'https://api.telegram.org/bot{token}/sendMessage',
        data=body,
        headers={'Content-Type': 'application/json', 'User-Agent': USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=40) as resp:
        resp.read()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description='Avisa sobre conversas do GitHub sem resposta do dono.')
    parser.add_argument('--days', type=int, default=1,
                        help='só avisa quando a última mensagem alheia tem pelo menos N dias (padrão: 1)')
    parser.add_argument('--remind-days', type=int, default=7,
                        help='repete o aviso de um item ainda pendente a cada N dias (padrão: 7)')
    parser.add_argument('--dry-run', action='store_true',
                        help='imprime a mensagem em vez de enviar, e não grava estado')
    parser.add_argument('--test-telegram', action='store_true',
                        help='envia uma mensagem de teste e sai (valida token, chat e parse_mode)')
    args = parser.parse_args()

    if args.test_telegram:
        env = load_env()
        token, chat = env.get('TELEGRAM_TOKEN'), env.get('TELEGRAM_CHAT_ID')
        if not token or not chat:
            log('TELEGRAM_TOKEN/TELEGRAM_CHAT_ID ausentes em ~/.phpcs-ai.env')
            return 1
        send_telegram(token, chat, '<b>github-replies-watch</b> — mensagem de teste. '
                      'Se você leu isto, o canal está configurado; escape de &lt;html&gt; e _sublinhado_ ok.')
        log('mensagem de teste enviada')
        return 0

    me = gh_json(['api', 'user'])
    if not me or 'login' not in me:
        log('gh não está autenticado — abortando')
        return 1
    owner = me['login']

    now = datetime.datetime.now(datetime.timezone.utc)
    pending, nrepos = scan(owner)
    old_enough = [it for it in pending if age_days(it['when'], now) >= args.days]

    state = load_state()
    pending_keys = {it['key'] for it in pending}
    # An item the owner has since replied to leaves the state, so a future reply on the same
    # thread is treated as new rather than "already alerted".
    state = {k: v for k, v in state.items() if k in pending_keys}

    to_alert = []
    for it in old_enough:
        prev = state.get(it['key'])
        newactivity = prev is None or prev.get('when') != it['when']
        remind = prev is not None and age_days(prev.get('alerted', it['when']), now) >= args.remind_days
        if newactivity or remind:
            to_alert.append(it)

    log(f'{nrepos} repositórios, {len(pending)} pendente(s), {len(old_enough)} com {args.days}+ dia(s), '
        f'{len(to_alert)} a avisar')

    if not to_alert:
        if not args.dry_run:
            save_state(state)
        return 0

    text = format_message(to_alert, now)
    if args.dry_run:
        print(text)
        return 0

    env = load_env()
    token, chat = env.get('TELEGRAM_TOKEN'), env.get('TELEGRAM_CHAT_ID')
    if not token or not chat:
        log('TELEGRAM_TOKEN/TELEGRAM_CHAT_ID ausentes em ~/.phpcs-ai.env — aviso não enviado')
        print(text)
        return 1

    send_telegram(token, chat, text)
    alerted = now.isoformat()
    for it in to_alert:
        state[it['key']] = {'when': it['when'], 'alerted': alerted}
    save_state(state)
    log(f'aviso enviado com {len(to_alert)} item(ns)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
