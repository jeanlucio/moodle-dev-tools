#!/usr/bin/env bash
# moodle-query-baseline — contagem determinística de query por teste, com detecção de
# regressão contra um baseline salvo.
#
# Complementa o moodle-security-audit, que só *adivinha* N+1 lendo código. Este mede de
# verdade: roda a suíte PHPUnit que o plugin já tem, instrumentada com
# $DB->perf_get_queries() (query_count_extension.php) — nenhum teste precisa mudar. O
# número por teste sozinho não diz "isso é ruim" (pode ser operação legitimamente cara);
# o valor real é comparar contra o baseline salvo e avisar quando um teste que sempre
# disparou 4 queries passa a disparar 40.
#
# O baseline NUNCA é atualizado silenciosamente — só com --accept, depois de você olhar
# o relatório.
#
# Uso:
#   moodle-query-baseline <tipo/nome> [opções]
#
#   <tipo/nome>            : ex. filter/playerhud (aceita o prefixo html/public/).
#   --min-queries N        : ignora testes com menos que isso de queries (padrão 10).
#   --threshold-pct N      : % de aumento pra marcar como suspeito (padrão 50).
#   --no-triage            : só mostra números/deltas, sem gastar cota da assinatura.
#   --accept               : grava os números desta rodada como novo baseline.
#   --model M              : modelo primário da triagem (padrão claude-fable-5).
#   --fallback-model M     : usado se o primário falhar (padrão claude-opus-5).
#   --jobs N                : chamadas de triagem em paralelo (padrão 5).
#   --no-cache              : ignora o cache de triagem.
#
# Relatório em <plugin>/.plans/query-baseline/<frankenstyle>-<data>-<hora>.md
# Baseline em ~/.moodle-query-baseline/<frankenstyle>.json — só muda com --accept.
# É ferramenta de bancada: NÃO vai no ZIP do Plugin Directory e NÃO altera o código-fonte
# do plugin (só lê; a suíte PHPUnit roda no container, sem tocar o repositório).

set -euo pipefail

MOODLE="/home/ubuntu/meu-moodle/html/public"

PLUGIN=""
PASSTHRU=()
while [ $# -gt 0 ]; do
    case "$1" in
        --min-queries|--threshold-pct|--model|--fallback-model|--jobs)
            PASSTHRU+=("$1" "${2:?$1 exige um valor}"); shift 2 ;;
        --no-triage|--accept|--no-cache)
            PASSTHRU+=("$1"); shift ;;
        -h|--help) sed -n '2,31p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) echo "erro: opção desconhecida '$1'" >&2; exit 1 ;;
        *) [ -n "$PLUGIN" ] && { echo "erro: informe um plugin só" >&2; exit 1; }; PLUGIN="$1"; shift ;;
    esac
done

if [ -z "$PLUGIN" ]; then
    echo "uso: moodle-query-baseline <tipo/nome> [opções]" >&2
    echo "ex.: moodle-query-baseline filter/playerhud --no-triage" >&2
    exit 1
fi
PLUGIN="${PLUGIN#./}"; PLUGIN="${PLUGIN#html/public/}"; PLUGIN="${PLUGIN#public/}"; PLUGIN="${PLUGIN%/}"
PLUGIN_ABS="$MOODLE/$PLUGIN"

if [ ! -d "$PLUGIN_ABS" ]; then
    echo "erro: '$PLUGIN_ABS' não existe" >&2
    exit 1
fi
if [ ! -d "$PLUGIN_ABS/tests" ]; then
    echo "erro: '$PLUGIN_ABS/tests' não existe — nada para medir" >&2
    exit 1
fi

if ! docker ps --format '{{.Names}}' | grep -qx "meu-moodle-web-1"; then
    echo "erro: container 'meu-moodle-web-1' não está rodando" >&2
    exit 1
fi

python3 -u "$(dirname "$(readlink -f "$0")")/query_baseline.py" "$PLUGIN" "${PASSTHRU[@]}"
