#!/usr/bin/env python3
# Monitor de novos plugins no Moodle Plugin Directory.
# Fonte: download.moodle.org/api/1.3/pluglist.php (sem Cloudflare).
# Detecta plugins novos por ID auto-incremental, busca descrição no GitHub
# e envia resumo PT-BR via Telegram com fallback chain de IAs.

import json
import re
import time
import datetime
import urllib.request
import urllib.error
from pathlib import Path

ENV_FILE      = Path.home() / '.phpcs-ai.env'
STATE_FILE    = Path.home() / '.moodle-plugins-seen.json'
LOG_FILE      = Path.home() / '.moodle-plugins-monitor.log'
PLUGLIST_URL  = 'https://download.moodle.org/api/1.3/pluglist.php'
GITHUB_API    = 'https://api.github.com/repos/{owner}/{repo}'


# ---------------------------------------------------------------------------
# Utilitários
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{ts}] {msg}'
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
    return {'max_id': 0, 'seen_ids': []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state))


# ---------------------------------------------------------------------------
# Fonte de dados: Plugin Directory API
# ---------------------------------------------------------------------------

def fetch_pluglist() -> dict:
    req = urllib.request.Request(
        PLUGLIST_URL,
        headers={'User-Agent': 'MoodlePluginMonitor/1.0'},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def find_new_plugins(plugins: list, state: dict) -> list:
    seen_ids = set(state.get('seen_ids', []))
    max_id   = state.get('max_id', 0)
    new_ones = [p for p in plugins if p['id'] > max_id and p['id'] not in seen_ids]
    return sorted(new_ones, key=lambda p: p['id'])


# ---------------------------------------------------------------------------
# Enriquecimento: descrição via GitHub API
# ---------------------------------------------------------------------------

def github_owner_repo(source_url: str) -> tuple[str, str] | None:
    match = re.search(r'github\.com/([^/]+)/([^/]+?)(?:\.git)?$', source_url or '')
    if not match:
        return None
    return match.group(1), match.group(2)


def fetch_github_description(source_url: str) -> str:
    parsed = github_owner_repo(source_url)
    if not parsed:
        return ''
    owner, repo = parsed
    url = GITHUB_API.format(owner=owner, repo=repo)
    req = urllib.request.Request(
        url,
        headers={
            'User-Agent': 'MoodlePluginMonitor/1.0',
            'Accept': 'application/vnd.github.v3+json',
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            return data.get('description', '') or ''
    except Exception:
        return ''


# ---------------------------------------------------------------------------
# Chamadas de IA
# ---------------------------------------------------------------------------

def http_post(url: str, headers: dict, body: dict) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={**headers, 'Content-Type': 'application/json'},
    )
    with urllib.request.urlopen(req, timeout=40) as resp:
        return json.loads(resp.read())


def component_type_label(component: str) -> str:
    prefix_map = {
        'mod_': 'atividade/recurso',
        'block_': 'bloco',
        'local_': 'plugin local',
        'theme_': 'tema',
        'auth_': 'método de autenticação',
        'enrol_': 'método de matrícula',
        'report_': 'relatório',
        'filter_': 'filtro de texto',
        'qtype_': 'tipo de questão',
        'format_': 'formato de curso',
        'admin_': 'plugin de administração',
        'tool_': 'ferramenta administrativa',
        'gradereport_': 'relatório de notas',
        'gradeimport_': 'importação de notas',
        'gradeexport_': 'exportação de notas',
        'repository_': 'repositório de arquivos',
        'plagiarism_': 'detecção de plágio',
        'tiny_': 'plugin do editor TinyMCE',
        'atto_': 'plugin do editor Atto',
    }
    for prefix, label in prefix_map.items():
        if component.startswith(prefix):
            return label
    return 'plugin'


def build_prompt(plugin: dict, github_desc: str) -> str:
    tipo  = component_type_label(plugin['component'])
    extra = f'\nDescrição no repositório: {github_desc}' if github_desc else ''
    return (
        'Você é um especialista em Moodle. '
        'Com base nas informações abaixo, escreva um resumo de 3 linhas em português brasileiro '
        'explicando: o que esse plugin faz, para quem é útil e qual problema resolve. '
        'Responda APENAS com o resumo em PT-BR, sem introdução, rótulos ou marcadores.\n\n'
        f'Nome: {plugin["name"]}\n'
        f'Componente: {plugin["component"]} (tipo: {tipo})\n'
        f'Link: https://moodle.org/plugins/view.php?plugin={plugin["component"]}'
        f'{extra}'
    )


def summarize_gemini(plugin: dict, github_desc: str, key: str) -> str:
    url = (
        'https://generativelanguage.googleapis.com/v1beta/'
        f'models/gemini-2.0-flash:generateContent?key={key}'
    )
    body = {'contents': [{'parts': [{'text': build_prompt(plugin, github_desc)}]}]}
    result = http_post(url, {}, body)
    return result['candidates'][0]['content']['parts'][0]['text'].strip()


def summarize_openai_compat(
    plugin: dict, github_desc: str, key: str, api_url: str, model: str,
) -> str:
    body = {
        'model': model,
        'messages': [{'role': 'user', 'content': build_prompt(plugin, github_desc)}],
        'max_tokens': 400,
    }
    result = http_post(api_url, {'Authorization': f'Bearer {key}'}, body)
    return result['choices'][0]['message']['content'].strip()


def summarize_with_fallback(plugin: dict, github_desc: str, env: dict) -> tuple[str, str]:
    providers = []

    if env.get('GEMINI_KEY'):
        providers.append((
            'Gemini',
            lambda p, g: summarize_gemini(p, g, env['GEMINI_KEY']),
        ))
    if env.get('OPENAI_KEY') and env.get('OPENAI_URL'):
        providers.append((
            f'OpenRouter/{env.get("OPENAI_MODEL", "deepseek")}',
            lambda p, g: summarize_openai_compat(
                p, g, env['OPENAI_KEY'], env['OPENAI_URL'],
                env.get('OPENAI_MODEL', 'deepseek/deepseek-v4-flash'),
            ),
        ))
    if env.get('OPENAI2_KEY') and env.get('OPENAI2_URL'):
        providers.append((
            f'OpenRouter/{env.get("OPENAI2_MODEL", "gpt-oss")}',
            lambda p, g: summarize_openai_compat(
                p, g, env['OPENAI2_KEY'], env['OPENAI2_URL'],
                env.get('OPENAI2_MODEL', 'openai/gpt-oss-120b:free'),
            ),
        ))

    for name, fn in providers:
        try:
            result = fn(plugin, github_desc)
            if result:
                return result, name
        except Exception as exc:
            log(f'  [{name}] falhou: {exc}')
            time.sleep(2)

    tipo = component_type_label(plugin['component'])
    fallback_text = (
        f'{plugin["name"]} é um {tipo} para Moodle '
        f'(componente: {plugin["component"]}). '
        f'{github_desc or "Sem descrição disponível."}'
    )
    return fallback_text[:600], 'sem IA'


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram(token: str, chat_id: str, text: str) -> None:
    url = f'https://api.telegram.org/bot{token}/sendMessage'
    body = {
        'chat_id': chat_id,
        'text': text,
        'parse_mode': 'Markdown',
        'disable_web_page_preview': False,
    }
    http_post(url, {}, body)


def format_message(plugin: dict, summary: str, provider: str) -> str:
    tipo  = component_type_label(plugin['component'])
    link  = f'https://moodle.org/plugins/view.php?plugin={plugin["component"]}'
    lines = [
        f"*Novo plugin no Moodle* — {tipo}",
        f"*{plugin['name']}* (`{plugin['component']}`)",
        '',
        summary,
        '',
        f"[Ver no Plugin Directory]({link})",
        f"_via {provider}_",
    ]
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    env     = load_env()
    token   = env.get('TELEGRAM_TOKEN', '')
    chat_id = env.get('TELEGRAM_CHAT_ID', '')

    if not token or not chat_id:
        log('ERRO: TELEGRAM_TOKEN ou TELEGRAM_CHAT_ID ausentes em ~/.phpcs-ai.env')
        return

    log('Buscando lista de plugins em download.moodle.org...')
    try:
        data = fetch_pluglist()
    except Exception as exc:
        log(f'ERRO ao buscar pluglist: {exc}')
        return

    plugins  = data.get('plugins', [])
    state    = load_state()
    new_ones = find_new_plugins(plugins, state)

    log(f'{len(plugins)} plugins no diretório — {len(new_ones)} novo(s) desde a última verificação.')

    if not new_ones:
        log('Nenhum plugin novo. Encerrando.')
        return

    seen_ids = set(state.get('seen_ids', []))

    for plugin in new_ones:
        log(f'Processando: [{plugin["id"]}] {plugin["component"]}')

        github_desc = ''
        if plugin.get('source'):
            github_desc = fetch_github_description(plugin['source'])
            if github_desc:
                log(f'  GitHub: {github_desc[:80]}')

        summary, provider = summarize_with_fallback(plugin, github_desc, env)
        log(f'  Resumo via {provider}')

        msg = format_message(plugin, summary, provider)
        try:
            send_telegram(token, chat_id, msg)
            seen_ids.add(plugin['id'])
            log('  Notificado via Telegram.')
        except Exception as exc:
            log(f'  ERRO ao enviar Telegram: {exc}')

        time.sleep(1)

    new_max = max(p['id'] for p in plugins)
    save_state({'max_id': new_max, 'seen_ids': list(seen_ids)})
    log(f'Estado salvo. max_id={new_max}')
    log('Concluído.')


if __name__ == '__main__':
    main()
