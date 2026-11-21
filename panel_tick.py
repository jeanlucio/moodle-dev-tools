"""One pass of the panel's scheduler, for cron: queue what is due and start the next run.

    python3 panel_tick.py

The panel server runs the same `panel_runs.tick()` every few seconds while it is open. This
script exists so scheduled audits also start while it is closed: cron calls it every minute (see
`panel_cron.py`). Prints a line only when it starts a run.
"""

import sys

import panel_runs


def main():
    run = panel_runs.tick()
    if run:
        print(f'iniciada {run["id"]} ({run["options"]["plugin"]})')
    return 0


if __name__ == '__main__':
    sys.exit(main())
