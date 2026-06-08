#!/usr/bin/env python3
# AI API caller for the pre-commit hook.
# Usage: python3 phpcs-ai-call.py <provider> <key> [url] [model] < prompt.txt
import json
import sys
import urllib.request


def call_gemini(key, prompt):
    url = (
        'https://generativelanguage.googleapis.com/v1beta/models'
        f'/gemini-2.0-flash:generateContent?key={key}'
    )
    body = json.dumps({
        'contents': [{'parts': [{'text': prompt}]}],
        'generationConfig': {'temperature': 0.1, 'maxOutputTokens': 1024},
    }).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={'Content-Type': 'application/json', 'User-Agent': 'curl/7.88.1'},
        method='POST'
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
        return data['candidates'][0]['content']['parts'][0]['text']


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
        'User-Agent': 'curl/7.88.1',
    }
    req = urllib.request.Request(url, data=body, headers=headers, method='POST')
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
        return data['choices'][0]['message']['content']


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
