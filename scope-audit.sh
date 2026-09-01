#!/usr/bin/env bash
# moodle-scope-audit — diff mecânico entre a árvore de arquivos do §6 do SCOPE.md de um
# plugin e o que realmente existe no repositório. Pega o que uma checagem "de memória" ao
# final de uma fase deixa passar: um arquivo previsto desde o roteiro e nunca criado.
#
# Nasceu de um incidente real: o SCOPE.md do mod_playercross previa managewords.php,
# ranking.php, ranking_service.php e ai_word_generator.php desde a Fase 3, mas cinco fases
# foram marcadas como concluídas sem nenhum deles existir — a verificação de fase tinha sido
# feita contra um checklist reconstruído de cabeça, não contra a árvore literal do §6.
#
# Uso:
#   moodle-scope-audit <tipo/nome> [--scope caminho/para/SCOPE.md]
#
#   <tipo/nome> : ex. mod/playercross, local/latepenalty (aceita o prefixo html/public/).
#   --scope     : caminho alternativo para o SCOPE.md, se não for <plugin>/SCOPE.md.
#
# Exit 1 se algo do §6 estiver faltando no disco; 0 se completo.

set -euo pipefail

if [ -f ~/.moodle-dev-tools.env ]; then
    set -a
    source ~/.moodle-dev-tools.env
    set +a
fi

MOODLE="${MDT_MOODLE_PUBLIC:-/home/ubuntu/meu-moodle/html/public}"

PLUGIN=""
SCOPE_ARGS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --scope) SCOPE_ARGS=(--scope "${2:?--scope exige um caminho}"); shift 2 ;;
        -h|--help) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) echo "erro: opção desconhecida '$1'" >&2; exit 1 ;;
        *) [ -n "$PLUGIN" ] && { echo "erro: informe um plugin só" >&2; exit 1; }; PLUGIN="$1"; shift ;;
    esac
done

if [ -z "$PLUGIN" ]; then
    echo "uso: moodle-scope-audit <tipo/nome> [--scope caminho/para/SCOPE.md]" >&2
    exit 1
fi
PLUGIN="${PLUGIN#./}"; PLUGIN="${PLUGIN#html/public/}"; PLUGIN="${PLUGIN%/}"
PLUGIN_ABS="$MOODLE/$PLUGIN"

if [ ! -d "$PLUGIN_ABS" ]; then
    echo "erro: '$PLUGIN_ABS' não existe" >&2
    exit 1
fi

python3 "$(dirname "$(readlink -f "$0")")/scope_audit.py" "$PLUGIN_ABS" "${SCOPE_ARGS[@]}"
