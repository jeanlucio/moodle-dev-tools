#!/usr/bin/env bash
# moodle-marketplace-downloads — gera a badge de downloads (últimos 12 meses fechados) a
# partir da página de stats do Moodle Marketplace, e grava em docs/badges/downloads.json.
#
# O Marketplace não tem API pública de downloads (a única, local_plugins_get_maintained_plugins,
# exige token de mantenedor autenticado), e o card do plugin só mostra "last 90 days". A página
# de /stats de cada plugin tem uma tabela mensal mais rica (12 meses fechados, sem o mês
# corrente) atrás de um "Show chart data" — renderizada no HTML da própria página, sem login.
# Este script soma essa tabela e escreve o resultado no schema de badge "endpoint" da
# shields.io (mesmo mecanismo já usado pelo MDL Shield).
#
# docs/badges/downloads.json fica dentro de docs/, que já é export-ignore no .gitattributes de
# todo plugin que tem site de documentação — não vai pro ZIP do Plugin Directory.
#
# NÃO commita nem dá push — só escreve o arquivo. Revisar e commitar é passo separado.
#
# Uso:
#   moodle-marketplace-downloads <tipo/nome> <id-marketplace>
#
#   <tipo/nome>       : ex. blocks/playerhud (aceita o prefixo html/public/).
#   <id-marketplace>  : id numérico da URL marketplace.moodle.com/plugins/<id>/stats
#
# Exige que o plugin já tenha docs/ (site GitHub Pages) — plugins sem isso ainda não têm
# onde colocar o JSON fora do ZIP.

set -euo pipefail

MOODLE="/home/ubuntu/meu-moodle/html/public"

if [ $# -ne 2 ]; then
    if [ $# -eq 1 ] && { [ "$1" = "-h" ] || [ "$1" = "--help" ]; }; then
        sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
        exit 0
    fi
    echo "uso: moodle-marketplace-downloads <tipo/nome> <id-marketplace>" >&2
    echo "ex.: moodle-marketplace-downloads blocks/playerhud 3583" >&2
    exit 1
fi

PLUGIN="$1"
PLUGIN="${PLUGIN#./}"; PLUGIN="${PLUGIN#html/public/}"; PLUGIN="${PLUGIN#public/}"; PLUGIN="${PLUGIN%/}"
PLUGIN_ABS="$MOODLE/$PLUGIN"
MARKETPLACE_ID="$2"

if [ ! -d "$PLUGIN_ABS" ]; then
    echo "erro: '$PLUGIN_ABS' não existe" >&2
    exit 1
fi

python3 "$(dirname "$(readlink -f "$0")")/marketplace_downloads.py" "$PLUGIN_ABS" "$MARKETPLACE_ID"
