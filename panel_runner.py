"""Detached runner of one panel run: starts the audit, waits, records how it ended.

    python3 panel_runner.py <run.json>

It is started by the panel server in its own session and keeps going if the server stops. It
only executes the argument list the server wrote after validating the request.
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

REPORT_LINE_RE = re.compile(r'^Relatório: (.+)$', re.M)
# Exit status of moodle-security-audit when the quota ran out before the plugin was fully read.
INCONCLUSIVE = 2


def _save(path, run):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


def _status(code):
    if code == 0:
        return 'done'
    return 'inconclusive' if code == INCONCLUSIVE else 'failed'


def main(argv):
    path = Path(argv[1])
    run = json.loads(path.read_text(encoding='utf-8'))
    log_path = Path(run['log'])
    # The server finds the runner again after a restart through this pid and the run id.
    run.update(status='running', pid=os.getpid())
    _save(path, run)
    with open(log_path, 'ab') as log:
        try:
            process = subprocess.Popen(run['argv'], stdin=subprocess.DEVNULL, stdout=log,
                                       stderr=subprocess.STDOUT)
        except OSError as exc:
            log.write(f'erro ao iniciar: {exc}\n'.encode())
            run.update(status='failed', finished=time.time(), exit_code=-1)
            _save(path, run)
            return 1
        code = process.wait()

    # The server may have marked the run cancelled while the audit was stopping; that wins.
    try:
        current = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        current = run
    if current.get('status') == 'cancelled':
        return 0
    text = log_path.read_text(encoding='utf-8', errors='replace')
    report = REPORT_LINE_RE.search(text)
    current.update(status=_status(code), finished=time.time(), exit_code=code,
                   report=Path(report.group(1).strip()).name if report else None)
    _save(path, current)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
