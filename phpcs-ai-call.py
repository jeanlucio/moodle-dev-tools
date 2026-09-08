#!/usr/bin/env python3
# AI API caller for the pre-commit hook.
# Usage: python3 phpcs-ai-call.py <provider> <key> [url] [model] < prompt.txt
#        python3 phpcs-ai-call.py claudecli <model> [fallback_model] < prompt.txt
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

# Some providers (e.g. Groq) return 403 for urllib's default
# "Python-urllib/x.y" User-Agent, treating it as bot traffic.
USER_AGENT = 'moodle-dev-tools-phpcs-ai-call/1.0'

# Headless Claude CLI calls include Node/model startup overhead on top of the
# actual generation, so they get a longer budget than the HTTP providers.
CLAUDE_CLI_TIMEOUT = 120

# Transient overload/rate-limit codes worth one short retry (e.g. Gemini's
# "high demand" 503, or an OpenRouter provider momentarily out of capacity).
RETRYABLE_HTTP_CODES = {429, 500, 502, 503, 504}
RETRY_DELAY_SECONDS = 2


def _post_json(url, headers, payload, timeout=30):
    """POST payload and return the parsed JSON body.

    Retries once after a short delay on a transient HTTP error, then
    re-raises the original HTTPError so callers can still branch on e.code
    (e.g. to special-case 402 "no credit").
    """
    req = urllib.request.Request(url, data=payload, headers=headers, method='POST')
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in RETRYABLE_HTTP_CODES and attempt == 0:
                time.sleep(RETRY_DELAY_SECONDS)
                continue
            raise


def _http_error_message(e):
    """Extract the meaningful error message from an HTTPError response body."""
    try:
        body = e.read().decode('utf-8', errors='replace')
        data = json.loads(body)
        return data.get('error', {}).get('message') or body[:400]
    except Exception:
        return str(e)


def _extract_openai_content(data):
    """Return the assistant text from an OpenAI-compatible JSON body.

    Raises RuntimeError with a specific reason when the body carries an error
    object or contains no usable text, so the pre-commit hook can report *why*
    a provider produced nothing instead of the generic "resposta ignorada".
    """
    if isinstance(data.get('error'), dict):
        err = data['error']
        code = err.get('code')
        msg = err.get('message') or json.dumps(err)[:400]
        raise RuntimeError(f'API error{f" {code}" if code else ""}: {msg}')

    choices = data.get('choices')
    if not choices:
        raise RuntimeError('resposta sem "choices" (corpo inesperado)')

    choice = choices[0]
    message = choice.get('message') or {}
    content = message.get('content')
    if content and content.strip():
        return content

    # Empty content. Reasoning models can spend the whole token budget on an
    # internal "reasoning" field and never emit a final answer.
    reason = choice.get('finish_reason') or 'desconhecido'
    if reason == 'length':
        raise RuntimeError(
            'content vazio (finish_reason=length) — max_tokens esgotado antes '
            'da resposta; aumente o limite de tokens ou troque de modelo'
        )
    if reason == 'content_filter':
        raise RuntimeError('content vazio (finish_reason=content_filter)')
    if message.get('reasoning') or message.get('reasoning_content'):
        raise RuntimeError(
            f'content vazio (finish_reason={reason}) — só houve texto de '
            'raciocínio; modelo de reasoning inadequado para este orçamento'
        )
    raise RuntimeError(f'content vazio (finish_reason={reason})')


def call_gemini(key, prompt):
    url = (
        'https://generativelanguage.googleapis.com/v1beta/models'
        f'/gemini-flash-latest:generateContent?key={key}'
    )
    payload = json.dumps({
        'contents': [{'parts': [{'text': prompt}]}],
        'generationConfig': {'temperature': 0.1, 'maxOutputTokens': 1024},
    }).encode()
    headers = {'Content-Type': 'application/json', 'User-Agent': USER_AGENT}
    try:
        data = _post_json(url, headers, payload)
    except urllib.error.HTTPError as e:
        first_line = _http_error_message(e).split('\n')[0]
        raise RuntimeError(f'HTTP {e.code}: {first_line}')

    candidates = data.get('candidates')
    if not candidates:
        block = (data.get('promptFeedback') or {}).get('blockReason')
        raise RuntimeError(
            f'resposta sem candidates (blockReason={block})' if block
            else 'resposta sem candidates (corpo inesperado)'
        )
    parts = (candidates[0].get('content') or {}).get('parts') or []
    text = ''.join(p.get('text', '') for p in parts)
    if not text.strip():
        reason = candidates[0].get('finishReason') or 'desconhecido'
        raise RuntimeError(f'content vazio (finishReason={reason})')
    return text


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
    try:
        data = _post_json(url, headers, body)
    except urllib.error.HTTPError as e:
        first_line = _http_error_message(e).split('\n')[0]
        if e.code == 402:
            raise RuntimeError(f'HTTP 402: sem crédito — {first_line}')
        raise RuntimeError(f'HTTP {e.code}: {first_line}')
    return _extract_openai_content(data)


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
