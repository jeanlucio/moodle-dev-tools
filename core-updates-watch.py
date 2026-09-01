#!/usr/bin/env python3
# Monitora atualizacoes de core disponiveis para os containers Moodle locais.
# Reaproveita o proprio \core\update\checker do Moodle (mesma fonte usada em
# Site administration > Notifications, download.moodle.org/api/1.3/updates.php)
# via um probe PHP copiado para dentro de cada container. Notifica via
# Telegram (mesmo bot dos outros monitores) so quando ha versao nova.

import datetime
import json
import subprocess
import urllib.request
from pathlib import Path

ENV_FILE   = Path.home() / '.phpcs-ai.env'
STATE_FILE = Path.home() / '.moodle-core-updates-seen.json'
LOG_FILE   = Path.home() / '.moodle-plugins-monitor.log'
PROBE_SRC  = Path(__file__).parent / 'core_update_probe.php'
USER_AGENT = 'MoodleCoreUpdatesWatch/1.0'

CONTAINERS = [
    {
        'name': 'meu-moodle-web-1',
        'label': 'educainfo.duckdns.org (5.1)',
        'docroot': '/var/www/html/public',
    },
    {
        'name': 'meu-moodle-web45-1',
        'label': 'educainfo2.duckdns.org (4.5)',
        'docroot': '/var/www/html',
    },
    {
        'name': 'meu-moodle-web52-1',
        'label': 'educainfo3.duckdns.org (5.2)',
        'docroot': '/var/www/html/public',
    },
]


def log(msg: str) -> None:
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f'[core-updates-watch] [{ts}] {msg}'
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
    STATE_FILE.write_text(json.dumps(state))


def send_telegram(token: str, chat_id: str, text: str) -> None:
    url = f'https://api.telegram.org/bot{token}/sendMessage'
    data = json.dumps({
        'chat_id': chat_id,
        'text': text,
        'parse_mode': 'Markdown',
        'disable_web_page_preview': False,
    }).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={'Content-Type': 'application/json', 'User-Agent': USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=40) as resp:
        resp.read()


def check_container(container: dict) -> dict:
    """Copies the probe into the container, runs it, cleans up, returns the parsed JSON."""
    name = container['name']
    remote_path = f"{container['docroot']}/core_update_probe_tmp.php"

    subprocess.run(
        ['docker', 'cp', str(PROBE_SRC), f'{name}:{remote_path}'],
        check=True, capture_output=True,
    )
    try:
        result = subprocess.run(
            ['docker', 'exec', name, 'php', remote_path],
            check=True, capture_output=True, text=True,
        )
        return json.loads(result.stdout)
    finally:
        subprocess.run(
            ['docker', 'exec', name, 'rm', '-f', remote_path],
            check=False, capture_output=True,
        )


def main() -> None:
    env     = load_env()
    token   = env.get('TELEGRAM_TOKEN', '')
    chat_id = env.get('TELEGRAM_CHAT_ID', '')

    if not token or not chat_id:
        log('ERRO: TELEGRAM_TOKEN ou TELEGRAM_CHAT_ID ausentes em ~/.phpcs-ai.env')
        return

    state = load_state()
    alerts = []

    for container in CONTAINERS:
        name = container['name']
        try:
            info = check_container(container)
        except Exception as exc:
            log(f'{name}: erro ao consultar o checker do Moodle ({exc})')
            continue

        available = info.get('available')
        if not available:
            log(f"{name}: em dia ({info.get('currentrelease')})")
            continue

        newversion = str(available['version'])
        if state.get(name) == newversion:
            log(f"{name}: atualizacao {available['release']} ja notificada, pulando")
            continue

        log(f"{name}: atualizacao disponivel -> {available['release']}")
        alerts.append((container, info, available))
        state[name] = newversion

    if alerts:
        lines = ['*Atualizacoes de core Moodle disponiveis*', '']
        for container, info, available in alerts:
            lines.append(f"- *{container['label']}*: {info['currentrelease']} -> "
                          f"*{available['release']}*")
        lines.append('')
        lines.append('Peca pro Claude atualizar o core desses containers (git checkout da '
                      'nova build/tag) e rodar moodle-upgrade em seguida.')
        send_telegram(token, chat_id, '\n'.join(lines))
        log(f'Notificado via Telegram ({len(alerts)} container(s)).')

    save_state(state)


if __name__ == '__main__':
    main()
