#!/usr/bin/env bash
# panel-tick — uma passada do agendador do painel (chamado pelo cron a cada minuto).
# Instalação: moodle-panel --install-cron

set -euo pipefail

if [ -f ~/.moodle-dev-tools.env ]; then
    set -a
    source ~/.moodle-dev-tools.env
    set +a
fi

cd "$(dirname "$(readlink -f "$0")")"
exec python3 panel_tick.py
