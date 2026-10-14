"""Runs started from the panel: strict validation, files on disk, one at a time.

The panel never builds a command from free text. A request is checked against what exists (the
plugin against the discovered ones, the groups against the plugin's config.json, the reference
against its git tags) and only then turned into an argument list, with no shell involved.

Every run is a JSON file in `.panel/runs/`. The server starts a detached runner (panel_runner.py)
for it, so a run survives a restart of the server, and the server finds it again by reading the
files. The quota is shared by everything that calls Claude, so only one run is active at a time.
"""

import json
import os
import secrets
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
OPTIONS = ('plugin', 'focus', 'only', 'since', 'full', 'with_tests', 'no_verify')
FLAGS = ('full', 'with_tests', 'no_verify')
FLAG_ARGS = {'full': '--full', 'with_tests': '--with-tests', 'no_verify': '--no-verify'}
ACTIVE = ('queued', 'running')
MAX_GROUPS = 20


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
    groups = audit_scope.load_config(directory)['groups']
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

    return {'plugin': plugin['path'], 'component': plugin['component'], 'focus': focus,
            'only': list(only), 'since': since, **flags}


def build_argv(options):
    """The argument list of an audit for validated options (no shell, no free text)."""
    argv = [audit_command(), options['plugin'], '--focus', options['focus']]
    if options['only']:
        argv += ['--only', *options['only']]
    if options['since']:
        argv += ['--since', options['since']]
    for flag in FLAGS:
        if options[flag]:
            argv.append(FLAG_ARGS[flag])
    return argv


# ---- run files ------------------------------------------------------------ #

def _path(run_id, runs_dir=None):
    return (runs_dir or RUNS_DIR) / f'{run_id}.json'


def write_run(run, runs_dir=None):
    path = _path(run['id'], runs_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
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


def enqueue(options, runs_dir=None, now=None):
    """Create a queued run for validated options."""
    stamp = time.strftime('%Y%m%d-%H%M%S', time.localtime(now or time.time()))
    run = {
        'id': f'{stamp}-{secrets.token_hex(3)}', 'status': 'queued', 'options': options,
        'argv': build_argv(options), 'created': now or time.time(), 'started': None,
        'finished': None, 'pid': None, 'exit_code': None, 'report': None,
    }
    run['log'] = str(_log_path(run['id'], runs_dir))
    write_run(run, runs_dir)
    return run


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


def start(run, runs_dir=None):
    """Start the detached runner of a queued run; the runner records its own pid and its result."""
    folder = runs_dir or RUNS_DIR
    # Written before the runner exists, so the runner's own updates are never overwritten.
    run.update(status='running', started=time.time())
    write_run(run, folder)
    process = subprocess.Popen(
        [sys.executable, str(RUNNER), str(_path(run['id'], folder))],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        cwd=str(TOOLS_DIR), start_new_session=True)
    # Reap the runner when it ends (finished or cancelled): a child nobody waits for stays a
    # zombie, which also keeps its process group "alive". If the server itself dies first, init
    # adopts and reaps it.
    threading.Thread(target=process.wait, daemon=True).start()
    return run


def tick(runs_dir=None):
    """Start the oldest queued run when nothing is running; return the run started, or None."""
    runs = list_runs(200, runs_dir)
    for run in runs:
        reconcile(run, runs_dir)
    if any(r['status'] == 'running' for r in runs):
        return None
    queued = [r for r in runs if r['status'] == 'queued']
    return start(min(queued, key=lambda r: r['created']), runs_dir) if queued else None


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
