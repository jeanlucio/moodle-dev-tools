#!/usr/bin/env bash
# moodle-panel — página local para ver os plugins (notas, relatórios, checagens) e disparar
# auditorias com fila. Escuta só em 127.0.0.1; de outra máquina, encaminhe a porta (aba Portas do
# VS Code, ou `ssh -L 8765:127.0.0.1:8765 host`).
#
# Uso:
#   moodle-panel [--port 8765]
#
# As auditorias disparadas usam o mesmo moodle-security-audit do terminal, na mesma assinatura do
# Claude Code. Rodam uma por vez (a cota é uma só) e continuam se o painel parar.

set -euo pipefail

if [ -f ~/.moodle-dev-tools.env ]; then
    set -a
    source ~/.moodle-dev-tools.env
    set +a
fi

exec python3 -u "$(dirname "$(readlink -f "$0")")/audit_panel.py" "$@"
