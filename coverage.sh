#!/usr/bin/env bash
# coverage.sh — mede a cobertura de testes de UM plugin Moodle, de forma repetível.
#
# Reproduz, num único comando, a rodada de cobertura que antes era manual e descartável
# (montar phpunit.xml na mão, rodar com XDEBUG_MODE=coverage, limpar depois). Gera um
# phpunit.xml temporário escopado ao plugin, roda dentro do container de dev com Xdebug
# em modo coverage e imprime a tabela por classe. Opcionalmente gera relatório HTML.
#
# É ferramenta de bancada: NÃO vai no ZIP do Plugin Directory e NÃO altera o código-fonte.
#
# Uso:
#   moodle-coverage <tipo/nome> [--html] [--filter <subpath>]
#
# Exemplos:
#   moodle-coverage blocks/playerhud
#   moodle-coverage local/playergames --html
#   moodle-coverage blocks/playerhud --filter classes/controller
#
# Aceita tanto o caminho relativo ao docroot (blocks/playerhud) quanto o caminho do
# host (html/public/blocks/playerhud) — o prefixo html/public/ é removido.
#
# Com --html, o relatório navegável sai em ~/coverage-reports/<frankenstyle>/index.html.
# Com --filter <subpath>, instrumenta só aquela subpasta (ex.: classes/controller) —
# útil para suítes grandes e para focar numa área.

set -euo pipefail

CONTAINER="meu-moodle-web-1"
DOCROOT="/var/www/html/public"
PHPUNIT="/var/www/html/vendor/bin/phpunit"
BOOTSTRAP="$DOCROOT/lib/phpunit/bootstrap.php"

# ------------------------------------------------------------------ #
#  Parse de argumentos                                               #
# ------------------------------------------------------------------ #
PLUGIN=""
WANT_HTML=0
FILTER=""

while [ $# -gt 0 ]; do
    case "$1" in
        --html)
            WANT_HTML=1
            shift
            ;;
        --filter)
            FILTER="${2:-}"
            if [ -z "$FILTER" ]; then
                echo "erro: --filter exige um subpath (ex.: classes/controller)" >&2
                exit 1
            fi
            shift 2
            ;;
        -h|--help)
            sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        -*)
            echo "erro: opção desconhecida '$1'" >&2
            exit 1
            ;;
        *)
            if [ -n "$PLUGIN" ]; then
                echo "erro: informe apenas um plugin por vez" >&2
                exit 1
            fi
            PLUGIN="$1"
            shift
            ;;
    esac
done

if [ -z "$PLUGIN" ]; then
    echo "uso: moodle-coverage <tipo/nome> [--html] [--filter <subpath>]" >&2
    echo "ex.: moodle-coverage blocks/playerhud" >&2
    exit 1
fi

# Normaliza: remove prefixos do host e barras sobrando.
PLUGIN="${PLUGIN#./}"
PLUGIN="${PLUGIN#html/public/}"
PLUGIN="${PLUGIN#public/}"
PLUGIN="${PLUGIN%/}"

# ------------------------------------------------------------------ #
#  Deriva frankenstyle (apenas para mensagens) e valida o plugin     #
# ------------------------------------------------------------------ #
TYPEDIR="${PLUGIN%%/*}"
NAME="${PLUGIN##*/}"

if [ "$TYPEDIR" = "$PLUGIN" ] || [ -z "$NAME" ]; then
    echo "erro: informe o plugin como tipo/nome (ex.: blocks/playerhud)" >&2
    exit 1
fi

# O diretório 'blocks' mapeia para o tipo 'block'; os demais coincidem.
case "$TYPEDIR" in
    blocks) TYPE="block" ;;
    *) TYPE="$TYPEDIR" ;;
esac
FRANKEN="${TYPE}_${NAME}"

PLUGIN_ABS="$DOCROOT/$PLUGIN"

# Container no ar?
if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
    echo "erro: container '$CONTAINER' não está rodando" >&2
    exit 1
fi

# Plugin tem classes/ e tests/?
if ! docker exec "$CONTAINER" test -d "$PLUGIN_ABS/classes"; then
    echo "erro: '$PLUGIN_ABS/classes' não existe no container" >&2
    exit 1
fi
if ! docker exec "$CONTAINER" test -d "$PLUGIN_ABS/tests"; then
    echo "erro: '$PLUGIN_ABS/tests' não existe — nada a medir" >&2
    exit 1
fi

# Monta o bloco <source><include> que define o que é instrumentado/reportado.
# Em PHPUnit 10+ o <source> é a fonte de verdade da cobertura (a flag --coverage-filter
# apenas SOMA ao include, não restringe) — por isso --filter ajusta o próprio <source>.
if [ -n "$FILTER" ]; then
    if ! docker exec "$CONTAINER" test -d "$PLUGIN_ABS/$FILTER"; then
        echo "erro: subpasta de --filter '$FILTER' não existe em $PLUGIN_ABS" >&2
        exit 1
    fi
    SOURCE_INCLUDE="      <directory suffix=\".php\">$PLUGIN_ABS/$FILTER</directory>"
else
    # classes/ inteiro + arquivos de função no topo do plugin, se existirem.
    EXTRA_FILES=$(docker exec "$CONTAINER" sh -c "
        for f in lib.php locallib.php renderer.php externallib.php; do
            [ -f \"$PLUGIN_ABS/\$f\" ] && echo \"      <file>$PLUGIN_ABS/\$f</file>\"
        done
        true
    ")
    SOURCE_INCLUDE="      <directory suffix=\".php\">$PLUGIN_ABS/classes</directory>
$EXTRA_FILES"
fi

# ------------------------------------------------------------------ #
#  Monta o phpunit.xml temporário (schema PHPUnit 10/11, igual core) #
# ------------------------------------------------------------------ #
XMLPATH="/tmp/moodle-coverage-${FRANKEN}.xml"
CACHEDIR="/tmp/.phpunit-cache-${FRANKEN}"

# failOnDeprecation/failOnWarning ficam false: esta é uma rodada de MEDIÇÃO, não um gate.
# Plugins 4.5+5.0 com doc-comment emitem deprecations inofensivas que não devem abortá-la.
XML="<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<phpunit
  xmlns:xsi=\"http://www.w3.org/2001/XMLSchema-instance\"
  xsi:noNamespaceSchemaLocation=\"https://schema.phpunit.de/10.5/phpunit.xsd\"
  bootstrap=\"$BOOTSTRAP\"
  processIsolation=\"false\"
  backupGlobals=\"false\"
  cacheResult=\"false\"
  failOnDeprecation=\"false\"
  failOnWarning=\"false\"
  beStrictAboutTestsThatDoNotTestAnything=\"false\"
  beStrictAboutOutputDuringTests=\"true\"
  cacheDirectory=\"$CACHEDIR\"
  backupStaticProperties=\"false\"
>
  <php>
    <const name=\"PHPUNIT_SEQUENCE_START\" value=\"157000\"/>
  </php>
  <extensions>
    <bootstrap class=\"core\\tests\\phpunit\\moodle_extension\"/>
  </extensions>
  <testsuites>
    <testsuite name=\"$FRANKEN\">
      <directory suffix=\"_test.php\">$PLUGIN_ABS/tests</directory>
      <exclude>$PLUGIN_ABS/tests/fixtures</exclude>
      <exclude>$PLUGIN_ABS/tests/generator</exclude>
    </testsuite>
  </testsuites>
  <source>
    <include>
$SOURCE_INCLUDE
    </include>
  </source>
</phpunit>"

# Limpa o XML, o cache e qualquer HTML temporário ao sair (sucesso ou erro).
cleanup() {
    docker exec "$CONTAINER" rm -rf \
        "$XMLPATH" "$CACHEDIR" "/tmp/coverage-html-${FRANKEN}" 2>/dev/null || true
}
trap cleanup EXIT

printf '%s\n' "$XML" | docker exec -i "$CONTAINER" sh -c "cat > '$XMLPATH'"

# ------------------------------------------------------------------ #
#  Monta os argumentos de cobertura e executa                       #
# ------------------------------------------------------------------ #
COVARGS="--coverage-text"
HTMLDIR=""
if [ "$WANT_HTML" -eq 1 ]; then
    HTMLDIR="/tmp/coverage-html-${FRANKEN}"
    COVARGS="$COVARGS --coverage-html '$HTMLDIR'"
fi

echo "Medindo cobertura de $FRANKEN ($PLUGIN)..."
[ -n "$FILTER" ] && echo "  filtro: $FILTER"
echo ""

# memory_limit=-1: a instrumentação de cobertura do Xdebug consome muito mais memória que
# uma rodada normal; o teto de 512M do CLI derruba suítes grandes (segfault).
# O exit code do PHPUnit é capturado (sem abortar via set -e): a medição completa e o
# relatório é gerado mesmo quando a suíte reporta warnings/deprecations inofensivas.
set +e
docker exec -e XDEBUG_MODE=coverage "$CONTAINER" sh -c \
    "cd '$DOCROOT' && php -d memory_limit=-1 '$PHPUNIT' -c '$XMLPATH' $COVARGS"
PHPUNIT_RC=$?
set -e

# ------------------------------------------------------------------ #
#  Copia o HTML para o host, se pedido (mesmo com issues no PHPUnit) #
# ------------------------------------------------------------------ #
# Sai num diretório dedicado no home (nunca dentro de um repo de plugin, para não sujá-lo).
if [ "$WANT_HTML" -eq 1 ] && docker exec "$CONTAINER" test -d "$HTMLDIR"; then
    OUT="$HOME/coverage-reports/${FRANKEN}"
    rm -rf "$OUT"
    mkdir -p "$(dirname "$OUT")"
    docker cp "$CONTAINER:$HTMLDIR" "$OUT" >/dev/null
    docker exec "$CONTAINER" rm -rf "$HTMLDIR" 2>/dev/null || true
    echo ""
    echo "Relatório HTML: $OUT/index.html"
fi

# ------------------------------------------------------------------ #
#  Nota sobre o exit code — separa medição-ok-com-avisos de falha    #
# ------------------------------------------------------------------ #
if [ "$PHPUNIT_RC" -ne 0 ]; then
    echo ""
    echo "Nota: PHPUnit terminou com código $PHPUNIT_RC. Se acima só há 'Deprecations'/'Warnings'"
    echo "(ex.: doc-comment metadata, @covers inválido), a MEDIÇÃO está correta — esses avisos são"
    echo "inofensivos em plugins 4.5+5.0. Se houver FAILURES/ERRORS, há teste quebrado a corrigir."
fi

exit "$PHPUNIT_RC"
