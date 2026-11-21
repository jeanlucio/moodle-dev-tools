"""The crontab line that lets scheduled audits start while the panel is closed.

The line is recognised by the script name, so installing twice changes nothing and the other
entries of the crontab are never touched.
"""

import subprocess
from pathlib import Path

TICK_SCRIPT = Path(__file__).resolve().parent / 'panel-tick.sh'
MARKER = 'panel-tick.sh'
LOG = '$HOME/.moodle-panel-tick.log'


def cron_line():
    return f'* * * * * {TICK_SCRIPT} >/dev/null 2>> {LOG}'


def _read():
    """The current crontab text; '' when there is none; None when cron is not installed."""
    try:
        result = subprocess.run(['crontab', '-l'], capture_output=True, text=True)
    except FileNotFoundError:
        return None
    return result.stdout if result.returncode == 0 else ''


def installed():
    """Does the crontab already run the tick script? None when cron is not available."""
    text = _read()
    if text is None:
        return None
    return any(MARKER in line and not line.lstrip().startswith('#') for line in text.splitlines())


def install():
    """Add the line to the crontab unless it is there. Returns True when it changed anything."""
    text = _read()
    if text is None:
        raise RuntimeError('crontab não encontrado')
    if installed():
        return False
    body = text.rstrip('\n') + ('\n' if text.strip() else '') + cron_line() + '\n'
    subprocess.run(['crontab', '-'], input=body, text=True, check=True)
    return True
