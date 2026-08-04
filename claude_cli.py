#!/usr/bin/env python3
"""Shared Claude CLI plumbing, extracted from security_audit.py so a second tool
(query_baseline.py) does not duplicate ~150 lines of proven, tested infrastructure.

Every call runs through the local `claude` CLI against the Claude Code subscription;
API keys are stripped from the child environment so a stray ANTHROPIC_API_KEY can never
turn this into per-token billing. Nothing here is security-audit-specific — callers own
their own prompts, cache directory and prompt-version constant.
"""

import concurrent.futures
import hashlib
import json
import os
import re
import subprocess
import threading
import time

# Headless CLI calls include Node/model startup overhead on top of the actual generation.
CLAUDE_TIMEOUT = 900


def fmt_duration(seconds):
    """Compact mm:ss / h:mm:ss, for a run that can last an hour."""
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f'{hours}h{minutes:02d}m{secs:02d}s'
    return f'{minutes}m{secs:02d}s'


class ProgressWriter:
    """Writes live progress state to a JSON file for progress.html to poll.

    Best-effort and silent on failure: a broken progress file must never break the tool it
    is reporting on. Writes are atomic (temp file + rename) so the dashboard never reads a
    half-written file, and thread-safe since run_parallel() ticks from worker threads.
    """

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()

    def write(self, **fields):
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(self.path.suffix + '.tmp')
                payload = {'updated_epoch': time.time(), **fields}
                tmp.write_text(json.dumps(payload, ensure_ascii=False))
                tmp.replace(self.path)
            except OSError:
                pass


class Clock:
    """Elapsed-time reporting. Purely local — costs nothing, and a run can last long
    enough that silence is indistinguishable from a hang.

    When `progress` (a ProgressWriter) is given, every phase boundary and every
    run_parallel() completion also writes live state to a JSON file — this is what lets
    progress.html show a real ticking clock even while a single long AI call (which prints
    nothing until it returns) is in flight, instead of going silent between print()s.
    """

    def __init__(self, progress=None):
        self.start = time.monotonic()
        self.start_epoch = time.time()
        self.phase_start = self.start
        self.phase_start_epoch = self.start_epoch
        self.progress = progress
        self.tag = ''
        self.description = ''

    def total(self):
        return time.monotonic() - self.start

    def phase(self, tag, description):
        print(f'[{tag}] {description}... (decorrido {fmt_duration(self.total())})')
        self.phase_start = time.monotonic()
        self.phase_start_epoch = time.time()
        self.tag = tag
        self.description = description
        self.tick()

    def done(self, note=''):
        elapsed = time.monotonic() - self.phase_start
        suffix = f' — {note}' if note else ''
        print(f'  concluída em {fmt_duration(elapsed)}{suffix}')
        self.tick(note=note)

    def tick(self, done=None, total=None, eta_seconds=None, note=''):
        """Refreshes the progress file with the current phase state, if one is configured.

        Called on every phase() and done() boundary, and by run_parallel() on every item
        completion — so done/total/eta_seconds stay fresh mid-phase, not just at its start
        and end.
        """
        if not self.progress:
            return
        self.progress.write(
            phase=self.tag,
            description=self.description,
            total_start_epoch=self.start_epoch,
            phase_start_epoch=self.phase_start_epoch,
            done=done,
            total=total,
            eta_seconds=eta_seconds,
            note=note,
            finished=False,
        )

    def finish(self):
        """Marks the run as finished, so the dashboard shows a final state and stops
        expecting further ticks."""
        if not self.progress:
            return
        self.progress.write(
            phase=self.tag,
            description=self.description,
            total_start_epoch=self.start_epoch,
            phase_start_epoch=self.phase_start_epoch,
            done=None, total=None, eta_seconds=None, note='',
            finished=True,
        )


def _subscription_env():
    """Environment for the CLI child, with anything that would bill per token removed."""
    return {
        k: v for k, v in os.environ.items()
        if k not in ('ANTHROPIC_API_KEY', 'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX')
    }


def _run_claude(prompt, cwd, model, system_prompt, allow_tools):
    """One headless CLI call. Returns the assistant's text, or raises RuntimeError."""
    cmd = [
        'claude', '-p',
        '--model', model,
        '--output-format', 'json',
        '--no-session-persistence',
        # Skips CLAUDE.md, skills, hooks and MCP servers in the child session. The user's
        # global CLAUDE.md is ~18k tokens and would be re-sent on every single call, for no
        # benefit: the rules a caller needs are passed explicitly via --append-system-prompt.
        # Unlike --bare, this leaves auth untouched, so subscription billing still applies.
        '--safe-mode',
    ]
    if allow_tools:
        # Read-only tool set: callers are structurally incapable of modifying the plugin.
        cmd += ['--tools', 'Read,Grep,Glob']
    else:
        cmd += ['--tools', '']
    if system_prompt:
        cmd += ['--append-system-prompt', system_prompt]

    result = subprocess.run(
        cmd, input=prompt, capture_output=True, text=True,
        timeout=CLAUDE_TIMEOUT, cwd=str(cwd), env=_subscription_env(),
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or '').strip().split('\n')[0]
        raise RuntimeError(err or f'exit {result.returncode}')

    try:
        envelope = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise RuntimeError('resposta do CLI não é JSON')

    if envelope.get('is_error'):
        raise RuntimeError(str(envelope.get('result'))[:200])
    text = envelope.get('result')
    if not text:
        raise RuntimeError('resposta vazia')
    return text


def call_claude(prompt, cwd, model, fallback_model, system_prompt='', allow_tools=True):
    """Run a call on `model`, falling back to `fallback_model` on ANY failure.

    The CLI's own --fallback-model only covers "overloaded or not available"; exhausted
    model credits surface as a plain failure, so the retry is handled here instead.
    """
    try:
        return _run_claude(prompt, cwd, model, system_prompt, allow_tools)
    except Exception as first:
        if not fallback_model or fallback_model == model:
            raise RuntimeError(f'{model}: {first}')
        try:
            return _run_claude(prompt, cwd, fallback_model, system_prompt, allow_tools)
        except Exception as second:
            raise RuntimeError(f'{model}: {first}; {fallback_model}: {second}')


# --------------------------------------------------------------------------- #
#  Parallel execution with completion-order progress                           #
# --------------------------------------------------------------------------- #

def run_parallel(items, jobs, fn, label, detail_fn=None, clock=None):
    """Run fn(item) over items in a thread pool, printing progress AS EACH ONE FINISHES.

    pool.map() yields in submission order — if item 1 was slow, items 2 and 3 sat
    done-but-silent until it caught up, and a slow batch looked indistinguishable from a
    hang. as_completed() reports true completion order instead.

    The ETA is throughput extrapolated from what has completed so far (elapsed / done),
    scaled by remaining work under the same parallelism. It is a moving estimate, not a
    guarantee: it is unreliable on the very first completion (n=1 sample) and self-corrects
    as more items finish, exactly like any progress bar built on observed throughput.

    Results are returned in the ORIGINAL item order regardless of completion order, since
    callers (report ordering, cache bookkeeping) depend on it — only the printed progress
    is completion-ordered.

    `clock` (optional): when given, its tick() is called on every completion with the same
    done/total/eta_seconds, so progress.html sees per-item progress mid-phase, not just the
    phase boundaries.
    """
    n = len(items)
    if n == 0:
        return []

    results = [None] * n
    start = time.monotonic()
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        future_to_index = {pool.submit(fn, item): i for i, item in enumerate(items)}
        for future in concurrent.futures.as_completed(future_to_index):
            index = future_to_index[future]
            result = future.result()
            results[index] = result
            done += 1

            elapsed = time.monotonic() - start
            eta = ''
            eta_seconds = None
            if done < n:
                avg_per_item = elapsed / done
                remaining_wall = (n - done) * avg_per_item / min(jobs, n)
                eta = f', ETA ~{fmt_duration(remaining_wall)}'
                eta_seconds = remaining_wall
            detail = f' — {detail_fn(result)}' if detail_fn else ''
            print(f'  {label} {done}/{n}{detail} (decorrido {fmt_duration(elapsed)}{eta})')
            if clock:
                clock.tick(done=done, total=n, eta_seconds=eta_seconds)

    return results


def extract_json(text):
    """Pull the first JSON array/object out of a model response.

    Defensive on purpose: responses arrive bare, fenced, or with a sentence in front,
    and a whole batch should not be lost to a stray "Here are the findings:".

    Tries whichever of '[' / '{' appears FIRST in the text, not array-then-object in a
    fixed order. A fixed array-first order silently mis-parses a top-level OBJECT that
    contains a nested array (e.g. {"strengths": [...], ...}): the first '[' found belongs
    to the nested array, and the text's last ']' happens to close that same array, so
    json.loads() succeeds — just on the wrong, inner value, deserialising a list where a
    dict was expected. Every call site that needs a specific top-level shape (list vs dict)
    already checks isinstance() on the result, so silently returning the wrong container was
    never caught — this fixes the extraction itself instead of relying on that check to
    paper over it.
    """
    text = text.strip()
    fenced = re.search(r'```(?:json)?\s*(.*?)```', text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    candidates = []
    for opener, closer in (('[', ']'), ('{', '}')):
        start = text.find(opener)
        if start != -1:
            candidates.append((start, opener, closer))
    for _, opener, closer in sorted(candidates):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError('nenhum JSON válido na resposta')


# --------------------------------------------------------------------------- #
#  Caching — content-hash keyed, resumable                                     #
# --------------------------------------------------------------------------- #

def hash_key(prompt_version, *parts):
    """Stable short hash of the inputs that decide a call's result.

    `prompt_version` is the caller's own bump counter (their PROMPT_VERSION constant) —
    passed in rather than read from a module global, since each tool that shares this
    module owns its prompts and must be able to invalidate its cache independently.
    """
    digest = hashlib.sha256()
    digest.update(str(prompt_version).encode())
    for part in parts:
        digest.update(repr(part).encode('utf-8', errors='replace'))
    return digest.hexdigest()[:16]


def cache_path(cache_dir, franken, phase, key):
    return cache_dir / franken / phase / f'{key}.json'


def cached(cache_dir, franken, phase, key, use_cache, compute):
    """Run `compute` unless a cached result for this exact input already exists.

    Applied to every AI phase: a run of 20+ calls that dies partway (spend limit, network,
    Ctrl-C) must resume rather than re-pay for work already done. Anything not cached here
    is money spent twice on the next attempt. `cache_dir` is the caller's own cache root
    (each tool keeps a separate directory, so clearing one never touches the other's cache).
    """
    if not use_cache:
        return compute()
    path = cache_path(cache_dir, franken, phase, key)
    if path.is_file():
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            pass
    result = compute()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False))
    except OSError:
        pass
    return result
