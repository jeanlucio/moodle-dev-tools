#!/usr/bin/env bash
# coverage.sh — mede a cobertura de testes de UM plugin Moodle, de forma repetível.
#
# Gera um phpunit.xml temporário escopado ao plugin, roda dentro do container de dev com Xdebug
# em modo coverage e imprime um relatório por arquivo e por função. Opcionalmente gera o HTML.
# É ferramenta de bancada: NÃO vai no ZIP do Plugin Directory e NÃO altera o código-fonte.
#
# Padrões (o que mudou em relação à primeira versão):
#   - Escopo: o plugin INTEIRO, menos exclusões explícitas (tests/, cli/, lang/, JS, templates,
#     bibliotecas de terceiros). Antes só entravam classes/ e alguns arquivos de função, e
#     mod_form.php, view.php, index.php, export.php, db/ e o backup sumiam do relatório.
#   - Tudo o que fica de fora do percentual é LISTADO com o motivo: entry points (que chamam
#     config.php e só o Behat exercita), arquivos declarativos de db/ ("carregado" não é
#     "testado"), pastas excluídas e JS/templates (não medíveis por PHPUnit).
#   - Medição EXECUTADA: o PHPUnit só credita a um teste as linhas que a anotação @covers lista,
#     o que subestima o que a suíte executa de verdade. Por isso os testes rodam numa cópia sem
#     @covers/@uses (feita dentro do container; o repositório não é tocado).
#
# Uso:
#   moodle-coverage <tipo/nome> [--html] [--filter <subpath>] [--credited | --compare]
#
# Exemplos:
#   moodle-coverage mod/playergroup               # escopo completo, medição executada
#   moodle-coverage mod/playergroup --html        # + relatório navegável com as linhas
#   moodle-coverage blocks/playerhud --compare    # executada x creditada por @covers (2 rodadas)
#   moodle-coverage blocks/playerhud --credited   # só a creditada por @covers (o comportamento antigo)
#   moodle-coverage blocks/playerhud --filter classes/controller
#
# --credited  roda os testes como estão: só conta o que cada @covers lista. Útil para ver se a
#             higiene dos @covers está boa, mas subestima a cobertura real.
# --compare   faz as duas rodadas (o dobro do tempo) e mostra, por arquivo, quanto a medição
#             executada tem a mais que a creditada.
# --filter    instrumenta só aquela subpasta e dispensa a classificação dos demais arquivos.
# --html      também gera ~/coverage-reports/<frankenstyle>/index.html (linhas descobertas).
#
# Aceita tanto o caminho relativo ao docroot (blocks/playerhud) quanto o caminho do host
# (html/public/blocks/playerhud) — o prefixo html/public/ é removido.

set -euo pipefail

if [ -f ~/.moodle-dev-tools.env ]; then
    set -a
    source ~/.moodle-dev-tools.env
    set +a
fi

CONTAINER="${MDT_CONTAINER_51:-meu-moodle-web-1}"
DOCROOT="/var/www/html/public"
PHPUNIT="/var/www/html/vendor/bin/phpunit"
BOOTSTRAP="$DOCROOT/lib/phpunit/bootstrap.php"
PUBLIC_DIR="${MDT_MOODLE_PUBLIC:-$HOME/meu-moodle/html/public}"
SCRIPT_DIR="$(dirname "$(readlink -f "$0")")"
REPORT_PY="$SCRIPT_DIR/coverage_report.py"

# ------------------------------------------------------------------ #
#  Parse de argumentos                                               #
# ------------------------------------------------------------------ #
PLUGIN=""
WANT_HTML=0
FILTER=""
MODE="executed"

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
        --credited)
            MODE="credited"
            shift
            ;;
        --compare)
            MODE="compare"
            shift
            ;;
        -h|--help)
            awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"
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
    echo "uso: moodle-coverage <tipo/nome> [--html] [--filter <subpath>] [--credited | --compare]" >&2
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
HOST_DIR="$PUBLIC_DIR/$PLUGIN"

if [ ! -f "$REPORT_PY" ]; then
    echo "erro: $REPORT_PY não encontrado" >&2
    exit 1
fi
if [ ! -d "$HOST_DIR" ]; then
    echo "erro: '$HOST_DIR' não existe no host (MDT_MOODLE_PUBLIC=$PUBLIC_DIR)" >&2
    exit 1
fi

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
if [ -n "$FILTER" ] && ! docker exec "$CONTAINER" test -d "$PLUGIN_ABS/$FILTER"; then
    echo "erro: subpasta de --filter '$FILTER' não existe em $PLUGIN_ABS" >&2
    exit 1
fi

# ------------------------------------------------------------------ #
#  Áreas de trabalho temporárias (container e host)                  #
# ------------------------------------------------------------------ #
WORK="$(mktemp -d)"
CACHEDIR="/tmp/.phpunit-cache-${FRANKEN}"
STRIPPED="/tmp/moodle-coverage-tests-${FRANKEN}"
HTMLDIR="/tmp/coverage-html-${FRANKEN}"

# Limpa tudo ao sair (sucesso ou erro): XMLs, cópia dos testes, cache, HTML e a pasta do host.
cleanup() {
    docker exec "$CONTAINER" sh -c "rm -rf '$CACHEDIR' '$STRIPPED' '$HTMLDIR' \
        /tmp/moodle-coverage-${FRANKEN}-*.xml /tmp/moodle-coverage-xml-${FRANKEN}-*" 2>/dev/null || true
    rm -rf "$WORK"
}
trap cleanup EXIT

PHPUNIT_RC=0

# Copia tests/ para dentro do container sem as anotações de cobertura (@covers, @uses e os
# atributos equivalentes do PHPUnit 10+). Sem elas o PHPUnit credita ao teste tudo o que ele
# executa. O repositório não é alterado: a cópia mora em /tmp do container.
prepare_stripped_tests() {
    docker exec "$CONTAINER" sh -c "rm -rf '$STRIPPED' && cp -r '$PLUGIN_ABS/tests' '$STRIPPED' \
        && find '$STRIPPED' -name '*.php' -exec sed -i -E \
        '/@covers|@uses|#\[(Covers|Uses)(Class|Function|Method|Nothing|Trait)/d' {} +"
}

# measure <rótulo> <diretório de testes no container> <gera HTML: 0|1>
measure() {
    local label="$1" testsdir="$2" html="$3"
    local xmlpath="/tmp/moodle-coverage-${FRANKEN}-${label}.xml"
    local covdir="/tmp/moodle-coverage-xml-${FRANKEN}-${label}"
    local filterargs=()
    [ -n "$FILTER" ] && filterargs=(--filter "$FILTER")

    python3 "$REPORT_PY" prepare \
        --host-dir "$HOST_DIR" --container-dir "$PLUGIN_ABS" --tests-dir "$testsdir" \
        --franken "$FRANKEN" --bootstrap "$BOOTSTRAP" --cache-dir "$CACHEDIR" \
        --scope-out "$WORK/scope.json" "${filterargs[@]}" \
        | docker exec -i "$CONTAINER" sh -c "cat > '$xmlpath'"

    local covargs="--coverage-xml '$covdir'"
    if [ "$html" -eq 1 ]; then
        covargs="$covargs --coverage-html '$HTMLDIR'"
    fi

    # memory_limit=-1: a instrumentação de cobertura do Xdebug consome muito mais memória que
    # uma rodada normal; o teto de 512M do CLI derruba suítes grandes (segfault).
    # O exit code do PHPUnit é capturado (sem abortar via set -e): a medição completa e o
    # relatório sai mesmo quando a suíte reporta warnings/deprecations inofensivas.
    local rc=0
    docker exec -e XDEBUG_MODE=coverage "$CONTAINER" sh -c \
        "cd '$DOCROOT' && php -d memory_limit=-1 '$PHPUNIT' -c '$xmlpath' $covargs" || rc=$?
    [ "$rc" -gt "$PHPUNIT_RC" ] && PHPUNIT_RC=$rc

    docker cp "$CONTAINER:$covdir" "$WORK/xml-$label" >/dev/null
}

echo "Medindo cobertura de $FRANKEN ($PLUGIN) — modo: $MODE"
[ -n "$FILTER" ] && echo "  filtro: $FILTER"
echo ""

REPORT_MODE="executed"
case "$MODE" in
    executed)
        prepare_stripped_tests
        measure executed "$STRIPPED" "$WANT_HTML"
        MAIN_XML="$WORK/xml-executed"
        OTHER_XML=""
        ;;
    credited)
        REPORT_MODE="credited"
        measure credited "$PLUGIN_ABS/tests" "$WANT_HTML"
        MAIN_XML="$WORK/xml-credited"
        OTHER_XML=""
        ;;
    compare)
        prepare_stripped_tests
        measure executed "$STRIPPED" "$WANT_HTML"
        measure credited "$PLUGIN_ABS/tests" 0
        MAIN_XML="$WORK/xml-executed"
        OTHER_XML="$WORK/xml-credited"
        ;;
esac

echo ""
reportargs=(--scope "$WORK/scope.json" --plugin "$FRANKEN" --xml "$MAIN_XML" --mode "$REPORT_MODE")
[ -n "$OTHER_XML" ] && reportargs+=(--credited-xml "$OTHER_XML")
python3 "$REPORT_PY" report "${reportargs[@]}"

# ------------------------------------------------------------------ #
#  Copia o HTML para o host, se pedido (mesmo com issues no PHPUnit) #
# ------------------------------------------------------------------ #
# Sai num diretório dedicado no home (nunca dentro de um repo de plugin, para não sujá-lo).
if [ "$WANT_HTML" -eq 1 ] && docker exec "$CONTAINER" test -d "$HTMLDIR"; then
    OUT="$HOME/coverage-reports/${FRANKEN}"
    rm -rf "$OUT"
    mkdir -p "$(dirname "$OUT")"
    docker cp "$CONTAINER:$HTMLDIR" "$OUT" >/dev/null
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
