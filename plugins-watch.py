#!/usr/bin/env python3
# Monitor de atualizações em plugins Moodle específicos.
# Detecta novas versões via download.moodle.org/api, busca release notes no
# GitHub e envia resumo PT-BR das mudanças via Telegram com fallback de IAs.

import json
import re
import time
import datetime
import urllib.request
import urllib.error
from pathlib import Path

ENV_FILE     = Path.home() / '.phpcs-ai.env'
STATE_FILE   = Path.home() / '.moodle-plugins-watch-state.json'
LOG_FILE     = Path.home() / '.moodle-plugins-monitor.log'
PLUGLIST_URL = 'https://download.moodle.org/api/1.3/pluglist.php'

WATCH_PLUGINS = [
    # Level UP XP — suite completa
    'block_xp',
    'availability_xp',
    'enrol_xp',
    'local_xpstore',
    # Stash — suite completa
    'block_stash',
    'availability_stash',
    'filter_stash',
    'tiny_stash',
    # Trail
    'format_trail',
    # Moove
    'theme_moove',
    # Learning Map
    'mod_learningmap',
    'format_learningmap',
    # Block Game
    'block_game',
    'availability_game',
    # Game
    'mod_game',
    # TinyMCE — seleção dos mais utilizados
    'tiny_c4l',
    'tiny_ai',
    'tiny_fontcolor',
    'tiny_fontsize',
    'tiny_wordimport',
    'tiny_multilang2',
    'tiny_cloze',
    # Sharing Cart
    'block_sharing_cart',
    # Completion Progress
    'block_completion_progress',
]


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
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Pluglist API
# ---------------------------------------------------------------------------

def fetch_pluglist() -> list:
    req = urllib.request.Request(
        PLUGLIST_URL,
        headers={'User-Agent': 'MoodlePluginMonitor/1.0'},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
    return {p['component']: p for p in data.get('plugins', []) if p.get('component')}


def latest_version(plugin: dict) -> tuple[str, str]:
    versions = plugin.get('versions') or []
    if not versions:
        return '', ''
    v = versions[0]
    return v.get('release', ''), v.get('version', '')


# ---------------------------------------------------------------------------
# GitHub release notes
# ---------------------------------------------------------------------------

def github_owner_repo(source_url: str) -> tuple[str, str] | None:
    match = re.search(r'github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$', source_url or '')
    if not match:
        return None
    return match.group(1), match.group(2)


def github_request(url: str) -> dict | list | None:
    req = urllib.request.Request(
        url,
        headers={
            'User-Agent': 'MoodlePluginMonitor/1.0',
            'Accept': 'application/vnd.github.v3+json',
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def fetch_github_releases_since(source_url: str, since_ts: int) -> list[dict]:
    parsed = github_owner_repo(source_url)
    if not parsed:
        return []
    owner, repo = parsed
    data = github_request(f'https://api.github.com/repos/{owner}/{repo}/releases?per_page=10')
    if not data:
        return []

    cutoff = datetime.datetime.fromtimestamp(since_ts, tz=datetime.timezone.utc)
    new_releases = []
    for r in data:
        if r.get('draft') or r.get('prerelease'):
            continue
        published = r.get('published_at', '')
        try:
            pub_dt = datetime.datetime.fromisoformat(published.replace('Z', '+00:00'))
        except Exception:
            continue
        if pub_dt > cutoff:
            new_releases.append({
                'tag': r.get('tag_name', ''),
                'name': r.get('name', ''),
                'body': (r.get('body') or '').strip(),
                'url': r.get('html_url', ''),
                'published_at': published,
            })
    return new_releases


def fetch_github_changelog(source_url: str, version: str) -> str:
    """Busca CHANGES.md ou CHANGELOG.md e extrai a seção da versão informada."""
    parsed = github_owner_repo(source_url)
    if not parsed:
        return ''
    owner, repo = parsed
    for filename in ('CHANGES.md', 'CHANGELOG.md', 'CHANGELOG', 'CHANGES'):
        data = github_request(
            f'https://api.github.com/repos/{owner}/{repo}/contents/{filename}'
        )
        if data and isinstance(data, dict) and data.get('content'):
            import base64
            content = base64.b64decode(data['content']).decode('utf-8', errors='ignore')
            return _extract_version_section(content, version)
    return ''


def _extract_version_section(changelog: str, version: str) -> str:
    """Extrai o bloco da versão mais recente, suportando ATX (## v1.0) e Setext (v1.0 / ----)."""
    lines = changelog.splitlines()
    in_section = False
    section_lines: list[str] = []
    i = 0

    while i < len(lines):
        line = lines[i]
        next_line = lines[i + 1] if i + 1 < len(lines) else ''

        # Setext heading: texto na linha i, underline (=== ou ---) na linha i+1
        is_setext = bool(re.match(r'^[=\-]{3,}\s*$', next_line))
        # ATX heading: ## version
        is_atx = line.startswith('#')

        if is_setext or is_atx:
            if in_section:
                break
            heading_text = line.strip().lstrip('#').strip()
            if re.search(r'\d+\.\d+', heading_text):
                in_section = True
                section_lines.append(line)
                if is_setext:
                    i += 1  # pula o underline
        elif in_section:
            section_lines.append(line)

        i += 1

    return '\n'.join(section_lines).strip()[:2000]


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


def build_prompt(plugin_name: str, component: str, release_tag: str, changelog: str) -> str:
    changelog_section = (
        f'Changelog desta versão:\n{changelog[:2000]}'
        if changelog
        else 'Não há notas de release disponíveis.'
    )
    return (
        'Você é especialista em Moodle. '
        f'O plugin "{plugin_name}" ({component}) lançou a versão {release_tag}. '
        'Com base nas notas de release abaixo, escreva um resumo de 3 a 4 linhas em '
        'português brasileiro destacando as principais mudanças, correções ou novidades '
        'desta versão. Foque no que impacta usuários e administradores Moodle. '
        'Responda APENAS com o resumo em PT-BR, sem introdução ou rótulos.\n\n'
        f'{changelog_section}'
    )


def summarize_gemini(prompt: str, key: str) -> str:
    url = (
        'https://generativelanguage.googleapis.com/v1beta/'
        f'models/gemini-2.0-flash:generateContent?key={key}'
    )
    body = {'contents': [{'parts': [{'text': prompt}]}]}
    result = http_post(url, {}, body)
    return result['candidates'][0]['content']['parts'][0]['text'].strip()


def summarize_openai_compat(prompt: str, key: str, api_url: str, model: str) -> str:
    body = {
        'model': model,
        'messages': [{'role': 'user', 'content': prompt}],
        'max_tokens': 400,
    }
    result = http_post(api_url, {'Authorization': f'Bearer {key}'}, body)
    return (result['choices'][0]['message'].get('content') or '').strip()


def provider_label(api_url: str, model: str) -> str:
    """Build a human-readable label from the endpoint host and the model id."""
    if 'nvidia' in api_url:
        vendor = 'NVIDIA'
    elif 'openrouter' in api_url:
        vendor = 'OpenRouter'
    elif 'groq' in api_url:
        vendor = 'Groq'
    elif 'openai.com' in api_url:
        vendor = 'OpenAI'
    else:
        vendor = 'OpenAI-compat'
    return f'{vendor}/{model}'


def summarize_with_fallback(
    plugin_name: str, component: str, release_tag: str, changelog: str, env: dict,
) -> tuple[str, str]:
    prompt = build_prompt(plugin_name, component, release_tag, changelog)

    providers = []
    # Priority order mirrors ~/.phpcs-ai.env: Gemini → Groq → OpenAI-compatible slots.
    if env.get('GEMINI_KEY'):
        providers.append((
            'Gemini/gemini-2.0-flash',
            lambda p: summarize_gemini(p, env['GEMINI_KEY']),
        ))
    if env.get('GROQ_KEY'):
        groqurl = 'https://api.groq.com/openai/v1/chat/completions'
        groqmodel = env.get('GROQ_MODEL', 'llama-3.3-70b-versatile')
        providers.append((
            provider_label(groqurl, groqmodel),
            lambda p, k=env['GROQ_KEY'], u=groqurl, m=groqmodel:
                summarize_openai_compat(p, k, u, m),
        ))
    for i in ['', '2', '3', '4', '5']:
        key = env.get(f'OPENAI{i}_KEY')
        url = env.get(f'OPENAI{i}_URL')
        if not key or not url:
            continue
        model = env.get(f'OPENAI{i}_MODEL', 'deepseek/deepseek-v4-flash')
        providers.append((
            provider_label(url, model),
            lambda p, k=key, u=url, m=model:
                summarize_openai_compat(p, k, u, m),
        ))

    for name, fn in providers:
        try:
            result = fn(prompt)
            if result:
                return result, name
        except Exception as exc:
            log(f'  [{name}] falhou: {exc}')
            time.sleep(2)

    return f'Nova versão {release_tag} disponível.', 'sem IA'


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


def format_message(
    plugin_name: str,
    component: str,
    release: dict,
    summary: str,
    provider: str,
    directory_link: str,
) -> str:
    release_link = release.get('url') or directory_link
    tag = release.get('tag') or release.get('name', '')
    lines = [
        f"*Atualização de plugin Moodle*",
        f"*{plugin_name}* (`{component}`) — {tag}",
        '',
        summary,
        '',
        f"[Notas da versão]({release_link}) · [Plugin Directory]({directory_link})",
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
        plugmap = fetch_pluglist()
    except Exception as exc:
        log(f'ERRO ao buscar pluglist: {exc}')
        return

    state = load_state()
    updated = False

    for component in WATCH_PLUGINS:
        plugin = plugmap.get(component)
        if not plugin:
            log(f'  [?] {component} não encontrado na pluglist')
            continue

        current_ts  = plugin.get('timelastreleased', 0)
        release_str, _ = latest_version(plugin)
        stored      = state.get(component, {})
        stored_ts   = stored.get('timelastreleased', 0)

        if current_ts <= stored_ts:
            continue

        log(f'Atualização detectada: {component} ({release_str})')

        directory_link = f'https://moodle.org/plugins/view.php?plugin={component}'
        source_url     = plugin.get('source', '')

        releases = fetch_github_releases_since(source_url, stored_ts) if source_url else []

        if releases:
            # Notifica cada release nova (normalmente só uma, mas cobre o caso de múltiplas)
            for rel in reversed(releases):
                log(f'  Release GitHub: {rel["tag"]}')
                summary, provider = summarize_with_fallback(
                    plugin['name'], component, rel['tag'], rel['body'], env,
                )
                msg = format_message(
                    plugin['name'], component, rel, summary, provider, directory_link,
                )
                try:
                    send_telegram(token, chat_id, msg)
                    log(f'  Notificado: {rel["tag"]}')
                except Exception as exc:
                    log(f'  ERRO Telegram: {exc}')
                time.sleep(1)
        else:
            # Sem GitHub Releases — tenta CHANGES.md/CHANGELOG.md
            changelog = fetch_github_changelog(source_url, release_str) if source_url else ''
            if changelog:
                log(f'  Changelog extraído do repositório ({len(changelog)} chars).')
            else:
                log(f'  Sem changelog disponível — notificando versão.')
            summary, provider = summarize_with_fallback(
                plugin['name'], component, release_str, changelog, env,
            )
            rel_stub = {'tag': release_str, 'name': release_str, 'url': directory_link}
            msg = format_message(
                plugin['name'], component, rel_stub, summary, provider, directory_link,
            )
            try:
                send_telegram(token, chat_id, msg)
                log('  Notificado.')
            except Exception as exc:
                log(f'  ERRO Telegram: {exc}')

        state[component] = {
            'timelastreleased': current_ts,
            'release': release_str,
            'name': plugin.get('name', component),
        }
        updated = True
        time.sleep(1)

    if updated:
        save_state(state)
        log('Estado salvo.')
    else:
        log('Nenhuma atualização encontrada.')

    log('Concluído.')


if __name__ == '__main__':
    main()
