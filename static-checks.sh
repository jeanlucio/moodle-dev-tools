#!/usr/bin/env bash
# moodle-static-checks — checagens determinísticas de um plugin Moodle (schema x código):
# backup do curso, limpeza ao apagar, limite de coluna, preferências de usuário, aleatoriedade fraca.
# Custo zero de cota; sai com 1 quando acha algo, então também serve de portão de commit.
#
# Uso:
#   moodle-static-checks <tipo/nome> [--check a,b] [--json] [--list]
#   ex.: moodle-static-checks mod/reflect --check backup_columns
#
# Exceções deliberadas ficam em <plugin>/.plans/security-audit/accepted.json, com o motivo
# (ver o cabeçalho de static_schema.py).

set -euo pipefail

if [ -f ~/.moodle-dev-tools.env ]; then
    set -a
    source ~/.moodle-dev-tools.env
    set +a
fi

exec python3 "$(dirname "$(readlink -f "$0")")/static_checks.py" "$@"
