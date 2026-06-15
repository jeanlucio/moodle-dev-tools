#!/usr/bin/env python3
# AI API caller for the pre-commit hook.
# Usage: python3 phpcs-ai-call.py <provider> <key> [url] [model] < prompt.txt
import json
import re
import sys
import time
import urllib.error
import urllib.request


def _http_error_message(e):
    """Extract the meaningful error message from an HTTPError response body."""
    try:
        body = e.read().decode('utf-8', errors='replace')
        data = json.loads(body)
        return data.get('error', {}).get('message') or body[:400]
    except Exception:
        return str(e)


def _retry_delay(e, msg):
    """Return seconds to wait before retrying a 429, or None to not retry."""
    header = e.headers.get('Retry-After') if e.headers else None
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    m = re.search(r'retry in (\d+(?:\.\d+)?)s', msg)
    if m:
        return float(m.group(1)) + 1
    return 30


def call_gemini(key, prompt):
    url = (
        'https://generativelanguage.googleapis.com/v1beta/models'
        f'/gemini-flash-latest:generateContent?key={key}'
    )
    payload = json.dumps({
        'contents': [{'parts': [{'text': prompt}]}],
        'generationConfig': {'temperature': 0.1, 'maxOutputTokens': 1024},
    }).encode()

    for attempt in range(2):
        req = urllib.request.Request(
            url, data=payload,
            headers={'Content-Type': 'application/json'},
            method='POST'
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.load(r)
                return data['candidates'][0]['content']['parts'][0]['text']
        except urllib.error.HTTPError as e:
            msg = _http_error_message(e)
            if e.code == 429 and attempt == 0:
                delay = _retry_delay(e, msg)
                if delay <= 65:
                    print(f'Gemini: rate limit, retrying in {delay:.0f}s...', file=sys.stderr)
                    time.sleep(delay)
                    continue
            first_line = msg.split('\n')[0]
            raise RuntimeError(f'HTTP {e.code}: {first_line}')

    raise RuntimeError('Gemini: rate limit persists after retry')


def call_openai(url, key, model, prompt):
    body = json.dumps({
        'model': model,
        'messages': [{'role': 'user', 'content': prompt}],
        'temperature': 0.1,
        'max_tokens': 1024,
    }).encode()
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {key}',
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


def main():
    if len(sys.argv) < 3:
        print('uso: phpcs-ai-call.py <provider> <key> [url] [model]', file=sys.stderr)
        sys.exit(1)

    provider = sys.argv[1]
    key = sys.argv[2]
    url = sys.argv[3] if len(sys.argv) > 3 else ''
    model = sys.argv[4] if len(sys.argv) > 4 else ''
    prompt = sys.stdin.read()

    try:
        if provider == 'gemini':
            print(call_gemini(key, prompt))
        elif provider in ('groq', 'openai'):
            print(call_openai(url, key, model, prompt))
        else:
            print(f'provider desconhecido: {provider}', file=sys.stderr)
            sys.exit(1)
    except Exception as e:
        print(f'ERRO: {e}', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
