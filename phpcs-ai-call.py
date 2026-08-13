#!/usr/bin/env python3
# AI API caller for the pre-commit hook.
# Usage: python3 phpcs-ai-call.py <provider> <key> [url] [model] < prompt.txt
#        python3 phpcs-ai-call.py claudecli <model> [fallback_model] < prompt.txt
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

# Some providers (e.g. Groq) return 403 for urllib's default
# "Python-urllib/x.y" User-Agent, treating it as bot traffic.
USER_AGENT = 'moodle-dev-tools-phpcs-ai-call/1.0'

# Headless Claude CLI calls include Node/model startup overhead on top of the
# actual generation, so they get a longer budget than the HTTP providers.
CLAUDE_CLI_TIMEOUT = 120


def _http_error_message(e):
    """Extract the meaningful error message from an HTTPError response body."""
    try:
        body = e.read().decode('utf-8', errors='replace')
        data = json.loads(body)
        return data.get('error', {}).get('message') or body[:400]
    except Exception:
        return str(e)


def call_gemini(key, prompt):
    url = (
        'https://generativelanguage.googleapis.com/v1beta/models'
        f'/gemini-flash-latest:generateContent?key={key}'
    )
    payload = json.dumps({
        'contents': [{'parts': [{'text': prompt}]}],
        'generationConfig': {'temperature': 0.1, 'maxOutputTokens': 1024},
    }).encode()
    req = urllib.request.Request(
        url, data=payload,
        headers={'Content-Type': 'application/json', 'User-Agent': USER_AGENT},
        method='POST'
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r)
            return data['candidates'][0]['content']['parts'][0]['text']
    except urllib.error.HTTPError as e:
        first_line = _http_error_message(e).split('\n')[0]
        raise RuntimeError(f'HTTP {e.code}: {first_line}')


def call_openai(url, key, model, prompt, max_tokens=1024):
    body = json.dumps({
        'model': model,
        'messages': [{'role': 'user', 'content': prompt}],
        'temperature': 0.1,
        'max_tokens': max_tokens,
    }).encode()
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {key}',
        'User-Agent': USER_AGENT,
    }
    req = urllib.request.Request(url, data=body, headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r)
            return data['choices'][0]['message']['content']
    except urllib.error.HTTPError as e:
        msg = _http_error_message(e)
        first_line = msg.split('\n')[0]
        raise RuntimeError(f'HTTP {e.code}: {first_line}')


def _run_claude_cli(model, prompt):
    """Run one headless Claude CLI query and return its stdout text."""
    if shutil.which('claude') is None:
        raise RuntimeError('binário "claude" não encontrado no PATH')

    # Strip vars that would make the CLI bill against a pay-per-token API key
    # or a cloud-provider account instead of the logged-in subscription.
    env = {
        k: v for k, v in os.environ.items()
        if k not in ('ANTHROPIC_API_KEY', 'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX')
    }

    result = subprocess.run(
        ['claude', '-p', '--model', model],
        input=prompt, capture_output=True, text=True,
        timeout=CLAUDE_CLI_TIMEOUT, env=env,
    )
    output = result.stdout.strip()
    if result.returncode != 0 or not output:
        stderr = result.stderr.strip().split('\n')[0] if result.stderr.strip() else ''
        raise RuntimeError(stderr or f'exit {result.returncode}')
    return output


def call_claude_cli(primary_model, fallback_model, prompt):
    """Try primary_model first; fall back to fallback_model only if that call fails."""
    try:
        return _run_claude_cli(primary_model, prompt)
    except subprocess.TimeoutExpired:
        primary_error = f'timeout ({CLAUDE_CLI_TIMEOUT}s)'
    except RuntimeError as e:
        primary_error = str(e)

    if not fallback_model or fallback_model == primary_model:
        raise RuntimeError(f'{primary_model}: {primary_error}')

    try:
        return _run_claude_cli(fallback_model, prompt)
    except subprocess.TimeoutExpired:
        fallback_error = f'timeout ({CLAUDE_CLI_TIMEOUT}s)'
    except RuntimeError as e:
        fallback_error = str(e)

    raise RuntimeError(
        f'{primary_model}: {primary_error}; {fallback_model}: {fallback_error}'
    )


def main():
    if len(sys.argv) < 3:
        print('uso: phpcs-ai-call.py <provider> <key> [url] [model]', file=sys.stderr)
        sys.exit(1)

    provider = sys.argv[1]
    prompt = sys.stdin.read()

    try:
        if provider == 'gemini':
            key = sys.argv[2]
            print(call_gemini(key, prompt))
        elif provider in ('groq', 'openai'):
            key = sys.argv[2]
            url = sys.argv[3] if len(sys.argv) > 3 else ''
            model = sys.argv[4] if len(sys.argv) > 4 else ''
            max_tokens = int(sys.argv[5]) if len(sys.argv) > 5 else 1024
            print(call_openai(url, key, model, prompt, max_tokens))
        elif provider == 'claudecli':
            primary_model = sys.argv[2]
            fallback_model = sys.argv[3] if len(sys.argv) > 3 else ''
            print(call_claude_cli(primary_model, fallback_model, prompt))
        else:
            print(f'provider desconhecido: {provider}', file=sys.stderr)
            sys.exit(1)
    except Exception as e:
        print(f'ERRO: {e}', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
