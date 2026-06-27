#!/usr/bin/env bash
# moodle-phpstan — análise estática de tipos (PHPStan) num plugin Moodle, com o contexto das
# classes do core. Pega bugs que o PHPCS (estilo) e o moodlecheck (PHPDoc) não veem: chamada a
# método/função inexistente, tipo errado de argumento/retorno, acesso a propriedade de algo que
# pode ser null, código morto. É especialmente útil para revisar código gerado por IA, que pode
# "alucinar" uma API (um método plausível que não existe) — o PHPStan acusa isso de forma
# determinística.
#
# Uso:
#   moodle-phpstan <tipo/nome> [--level N] [--path <subdir>]
#
#   <tipo/nome> : ex. blocks/playerhud, local/latepenalty (aceita o prefixo html/public/).
#   --level N   : nível do PHPStan 0..9 (padrão 2). Níveis altos geram ruído no Moodle
#                 (stdClass/mixed); subir só quando valer.
#   --path      : analisa um subdiretório específico em vez de classes/ + libs de topo.

set -euo pipefail

PHPSTAN="/home/ubuntu/moodle-dev-tools/phpstan/vendor/bin/phpstan"
MOODLE="/home/ubuntu/meu-moodle/html/public"
# Raiz do Moodle (com lib/components.json + vendor) que a extensão phpstan-moodle bootstrapa
# para conhecer as classes do core e seus aliases legacy (cm_info etc.). É um nível acima do
# docroot na estrutura public/ do Moodle 5.x.
MOODLE_ROOT="/home/ubuntu/meu-moodle/html"

LEVEL=2
PLUGIN=""
SUBPATH=""
while [ $# -gt 0 ]; do
    case "$1" in
        --level) LEVEL="${2:?--level exige um número}"; shift 2 ;;
        --path)  SUBPATH="${2:?--path exige um subdiretório}"; shift 2 ;;
        -h|--help) sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) echo "erro: opção desconhecida '$1'" >&2; exit 1 ;;
        *) [ -n "$PLUGIN" ] && { echo "erro: informe um plugin só" >&2; exit 1; }; PLUGIN="$1"; shift ;;
    esac
done

if [ -z "$PLUGIN" ]; then
    echo "uso: moodle-phpstan <tipo/nome> [--level N] [--path <subdir>]" >&2
    exit 1
fi
PLUGIN="${PLUGIN#./}"; PLUGIN="${PLUGIN#html/public/}"; PLUGIN="${PLUGIN%/}"
PLUGIN_ABS="$MOODLE/$PLUGIN"

if [ ! -d "$PLUGIN_ABS" ]; then
    echo "erro: '$PLUGIN_ABS' não existe" >&2
    exit 1
fi

# Monta a lista de caminhos a analisar: subdir explícito, ou classes/ + libs de topo do plugin.
paths=""
if [ -n "$SUBPATH" ]; then
    paths="        - $PLUGIN_ABS/$SUBPATH"
else
    [ -d "$PLUGIN_ABS/classes" ] && paths="        - $PLUGIN_ABS/classes"
    for f in lib.php locallib.php renderer.php externallib.php; do
        [ -f "$PLUGIN_ABS/$f" ] && paths="$paths"$'\n'"        - $PLUGIN_ABS/$f"
    done
fi
if [ -z "$paths" ]; then
    echo "erro: nada para analisar em '$PLUGIN_ABS' (sem classes/ nem libs de topo)" >&2
    exit 1
fi

# Config temporária. phpVersion 80200 = analisa como PHP 8.2 (o que o Moodle roda).
# A extensão micaherne/phpstan-moodle (auto-registrada via phpstan/extension-installer)
# bootstrapa o classloader do Moodle a partir de moodle.rootDirectory, resolvendo os aliases
# legacy de classe — sem ela o nível 2 afoga em falsos positivos de classes core não-descobertas.
NEON=$(mktemp --suffix=.neon)
trap 'rm -f "$NEON"' EXIT
cat > "$NEON" <<NEONEOF
parameters:
    level: $LEVEL
    phpVersion: 80200
    paths:
$paths
    moodle:
        rootDirectory: $MOODLE_ROOT
NEONEOF

echo "PHPStan nível $LEVEL em $PLUGIN..."
echo ""
"$PHPSTAN" analyse -c "$NEON" --memory-limit=2G --no-progress
