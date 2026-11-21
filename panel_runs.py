"""Runs started from the panel: strict validation, files on disk, one at a time.

The panel never builds a command from free text. A request is checked against what exists (the
plugin against the discovered ones, the groups against the plugin's config.json, the reference
against its git tags) and only then turned into an argument list, with no shell involved.

Every run is a JSON file in `.panel/runs/`. The server starts a detached runner (panel_runner.py)
for it, so a run survives a restart of the server, and the server finds it again by reading the
files. The quota is shared by everything that calls Claude, so only one run is active at a time.
"""

import fcntl
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import audit_focus
import audit_scope
import panel_data

TOOLS_DIR = Path(__file__).resolve().parent
PANEL_DIR = TOOLS_DIR / '.panel'
RUNS_DIR = PANEL_DIR / 'runs'
RUNNER = TOOLS_DIR / 'panel_runner.py'
OPTIONS = ('plugin', 'focus', 'only', 'since', 'full', 'with_tests', 'no_verify', 'no_phpstan',
           'phpstan_level')
FLAGS = ('full', 'with_tests', 'no_verify', 'no_phpstan')
FLAG_ARGS = {'full': '--full', 'with_tests': '--with-tests', 'no_verify': '--no-verify',
             'no_phpstan': '--no-phpstan'}
ACTIVE = ('scheduled', 'queued', 'running')
# How far ahead a run may be scheduled, and how much clock skew between browser and server is
# tolerated when the chosen time is "now".
MAX_SCHEDULE_SECONDS = 366 * 24 * 3600
SCHEDULE_SKEW_SECONDS = 30
TICK_BATCH = 1000
MAX_GROUPS = 20
PHPSTAN_LEVELS = range(0, 10)
DEFAULT_PHPSTAN_LEVEL = 6


class ValidationError(ValueError):
    """The request asks for something the panel does not allow."""


def audit_command():
    """The program that runs an audit; tests point MDT_PANEL_AUDIT_CMD at a stand-in."""
    return os.environ.get('MDT_PANEL_AUDIT_CMD') or str(TOOLS_DIR / 'security-audit.sh')


def validate(payload, public=None):
    """Clean options from a request body, or ValidationError. Unknown keys are refused."""
    if not isinstance(payload, dict):
        raise ValidationError('corpo inválido')
    unknown = sorted(set(payload) - set(OPTIONS))
    if unknown:
        raise ValidationError(f'opção desconhecida: {", ".join(unknown)}')

    plugin = panel_data.find_plugin(str(payload.get('plugin', '')), public)
    if plugin is None:
        raise ValidationError('plugin desconhecido')
    directory = panel_data.plugin_dir(plugin, public)

    focus = payload.get('focus', 'all')
    if focus not in audit_focus.FOCI:
        raise ValidationError('foco inválido')

    only = payload.get('only') or []
    if not isinstance(only, list) or len(only) > MAX_GROUPS:
        raise ValidationError('grupos inválidos')
    groups = panel_data.configured_or_suggested(directory)[0]
    for name in only:
        if not isinstance(name, str) or name not in groups:
            raise ValidationError(f'grupo inexistente: {name!r}')

    since = payload.get('since') or None
    if since is not None and (not isinstance(since, str) or not panel_data.ref_exists(directory, since)):
        raise ValidationError('referência (tag ou branch) inexistente')

    flags = {}
    for flag in FLAGS:
        value = payload.get(flag, False)
        if not isinstance(value, bool):
            raise ValidationError(f'{flag} deve ser verdadeiro ou falso')
        flags[flag] = value
    if flags['full'] and focus != 'quality':
        raise ValidationError('--full só vale no foco qualidade')

    level = payload.get('phpstan_level', DEFAULT_PHPSTAN_LEVEL)
    # bool is an int in Python, so it has to be refused by hand.
    if isinstance(level, bool) or not isinstance(level, int) or level not in PHPSTAN_LEVELS:
        raise ValidationError('nível do PHPStan deve ser um inteiro de 0 a 9')

    return {'plugin': plugin['path'], 'component': plugin['component'], 'focus': focus,
            'only': list(only), 'since': since, 'phpstan_level': level, **flags}


def parse_run_at(value, now=None):
    """The epoch a run is scheduled for, or ValidationError. It must lie in the future."""
    now = now or time.time()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError('horário inválido')
    if value < now - SCHEDULE_SKEW_SECONDS:
        raise ValidationError('o horário programado já passou')
    if value > now + MAX_SCHEDULE_SECONDS:
        raise ValidationError('programe para no máximo um ano à frente')
    return float(value)


def build_argv(options):
    """The argument list of an audit for validated options (no shell, no free text)."""
    # --json keeps the report's data next to it, so it can be re-rendered later without a new audit.
    argv = [audit_command(), options['plugin'], '--focus', options['focus'], '--json']
    if options['only']:
        argv += ['--only', *options['only']]
    if options['since']:
        argv += ['--since', options['since']]
    for flag in FLAGS:
        if options[flag]:
            argv.append(FLAG_ARGS[flag])
    if options['phpstan_level'] != DEFAULT_PHPSTAN_LEVEL:
        argv += ['--phpstan-level', str(options['phpstan_level'])]
    return argv


# ---- run files ------------------------------------------------------------ #

def _path(run_id, runs_dir=None):
    return (runs_dir or RUNS_DIR) / f'{run_id}.json'


def write_run(run, runs_dir=None):
    path = _path(run['id'], runs_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique per call: the server, the cron tick and the runner may write the same run at once.
    temp = path.with_suffix(f'.{os.getpid()}.{secrets.token_hex(2)}.tmp')
    temp.write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


def read_run(run_id, runs_dir=None):
    if not run_id or not all(c.isalnum() or c in '-_' for c in run_id):
        return None
    try:
        return json.loads(_path(run_id, runs_dir).read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return None


def list_runs(limit=40, runs_dir=None):
    folder = runs_dir or RUNS_DIR
    if not folder.is_dir():
        return []
    runs = []
    for path in sorted(folder.glob('*.json'), reverse=True)[:limit]:
        try:
            runs.append(json.loads(path.read_text(encoding='utf-8')))
        except (OSError, json.JSONDecodeError):
            continue
    return runs


def enqueue(options, runs_dir=None, now=None, run_at=None):
    """Create a queued run for validated options, or a `scheduled` one when `run_at` is given.

    A scheduled run waits in `scheduled` until `tick()` finds its time has come, then joins the
    queue like any other. It remembers the PATH it was scheduled with: cron starts `tick()` with a
    bare PATH that does not reach `claude` (installed under nvm), see `start()`.
    """
    stamp = time.strftime('%Y%m%d-%H%M%S', time.localtime(now or time.time()))
    run = {
        'id': f'{stamp}-{secrets.token_hex(3)}', 'status': 'scheduled' if run_at else 'queued',
        'options': options,
        'argv': build_argv(options), 'created': now or time.time(), 'started': None,
        'finished': None, 'pid': None, 'exit_code': None, 'report': None,
    }
    if run_at:
        run.update(run_at=run_at, path=os.environ.get('PATH', ''))
    run['log'] = str(_log_path(run['id'], runs_dir))
    write_run(run, runs_dir)
    return run


def list_for_panel(limit=40, runs_dir=None):
    """The newest runs plus every scheduled one, which may be older than the newest `limit`
    because it waits for its time while newer runs pile up."""
    runs = list_runs(limit, runs_dir)
    seen = {r['id'] for r in runs}
    waiting = [r for r in list_runs(TICK_BATCH, runs_dir)
               if r['status'] == 'scheduled' and r['id'] not in seen]
    return sorted(runs + waiting, key=lambda r: r['id'], reverse=True)


def resume(run_id, public=None, runs_dir=None):
    """Queue a new run with the options of an inconclusive one; None when the run does not exist.

    The audit caches every batch and verification it finished, so the new run only pays for what
    the quota cut off. The options are validated again: the plugin or its groups may have changed
    since the run was created. The old run keeps its record and points at its successor, so it
    can be resumed only once.
    """
    run = read_run(run_id, runs_dir)
    if run is None:
        return None
    if run['status'] != 'inconclusive':
        raise ValidationError('só uma execução inconclusiva pode ser retomada')
    if run.get('resumed_by'):
        raise ValidationError('esta execução já foi retomada')
    options = validate({k: v for k, v in run['options'].items() if k != 'component'}, public)
    successor = enqueue(options, runs_dir)
    run['resumed_by'] = successor['id']
    write_run(run, runs_dir)
    return successor


def _log_path(run_id, runs_dir=None):
    return (runs_dir or RUNS_DIR) / f'{run_id}.log'


def read_log(run, offset=0, limit=65536):
    """(text, next offset) of the log from `offset` on."""
    try:
        with open(run['log'], 'rb') as handle:
            handle.seek(max(offset, 0))
            data = handle.read(limit)
    except OSError:
        return '', offset
    return data.decode('utf-8', errors='replace'), max(offset, 0) + len(data)


# ---- process control ------------------------------------------------------ #

def runner_alive(run):
    """Is the runner of this run still there? Checked against its command line, not just the pid."""
    pid = run.get('pid')
    if not pid:
        return False
    try:
        cmdline = Path(f'/proc/{int(pid)}/cmdline').read_bytes().decode(errors='replace')
    except (OSError, ValueError):
        return False
    return 'panel_runner.py' in cmdline and run['id'] in cmdline


GRACE_SECONDS = 15


def reconcile(run, runs_dir=None, now=None):
    """A run marked running whose runner is gone and never wrote its end is marked lost.

    The runner writes its own pid a moment after it starts, so a run younger than GRACE_SECONDS is
    never judged.
    """
    young = (now or time.time()) - (run.get('started') or 0) < GRACE_SECONDS
    if run['status'] == 'running' and not young and not runner_alive(run):
        run.update(status='lost', finished=time.time())
        write_run(run, runs_dir)
    return run


def _runner_env(run):
    """The environment of the runner: the current one, with the PATH of the scheduling session
    only when the current PATH cannot find `claude` (a run started by cron)."""
    env = dict(os.environ)
    if shutil.which('claude') is None and run.get('path'):
        env['PATH'] = run['path']
    return env


def start(run, runs_dir=None):
    """Start the detached runner of a queued run; the runner records its own pid and its result."""
    folder = runs_dir or RUNS_DIR
    # Written before the runner exists, so the runner's own updates are never overwritten.
    run.update(status='running', started=time.time())
    write_run(run, folder)
    process = subprocess.Popen(
        [sys.executable, str(RUNNER), str(_path(run['id'], folder))],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        cwd=str(TOOLS_DIR), start_new_session=True, env=_runner_env(run))
    # Reap the runner when it ends (finished or cancelled): a child nobody waits for stays a
    # zombie, which also keeps its process group "alive". If the server itself dies first, init
    # adopts and reaps it.
    threading.Thread(target=process.wait, daemon=True).start()
    return run


def promote_due(runs, runs_dir=None, now=None):
    """Move the scheduled runs whose time has come into the queue."""
    now = now or time.time()
    for run in runs:
        if run['status'] == 'scheduled' and (run.get('run_at') or 0) <= now:
            run['status'] = 'queued'
            write_run(run, runs_dir)


def tick(runs_dir=None, now=None):
    """Queue the scheduled runs that are due, then start the oldest queued run if nothing runs.

    Returns the run started, or None. Both the panel's scheduler thread and the cron job
    (`panel_tick.py`) call it, so it holds a lock on the runs folder: two callers at the same
    moment would otherwise both see nothing running and start two audits on one quota.
    """
    folder = runs_dir or RUNS_DIR
    if not folder.is_dir():
        return None
    with open(folder / '.tick.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        runs = list_runs(TICK_BATCH, runs_dir)
        for run in runs:
            reconcile(run, runs_dir)
        promote_due(runs, runs_dir, now)
        if any(r['status'] == 'running' for r in runs):
            return None
        queued = [r for r in runs if r['status'] == 'queued']
        # A promoted run waits from its scheduled time, not from the day it was created.
        order = min(queued, key=lambda r: r.get('run_at') or r['created']) if queued else None
        return start(order, runs_dir) if order else None


def cancel(run_id, runs_dir=None):
    """Cancel a queued or running run; a running one has its whole process group stopped."""
    run = read_run(run_id, runs_dir)
    if run is None:
        return None
    if run['status'] == 'running' and runner_alive(run):
        try:
            os.killpg(int(run['pid']), signal.SIGTERM)
        except (OSError, ValueError):
            pass
    if run['status'] in ACTIVE:
        run.update(status='cancelled', finished=time.time())
        write_run(run, runs_dir)
    return run
