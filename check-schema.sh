#!/usr/bin/env bash
# moodle-check-schema — valida o schema físico do banco contra os install.xml e mostra só
# as divergências dos SEUS plugins (filtra o ruído do core e de plugins de terceiros).
#
# Roda o admin/cli/check_database_schema.php nativo do Moodle dentro do container de dev,
# onde o site de produção (mdl_) está instalado. É a ferramenta para pegar "drift" do banco
# de desenvolvimento: install.xml evoluiu e o banco não acompanhou (faltou reinstalar/upgrade).
#
# Além disso, roda um segundo check independente: round-trip canônico do install.xml. Carrega
# cada install.xml na engine XMLDB do Moodle (xmldb_file) e serializa de volta (xmlOutput());
# se o resultado não bater byte-a-byte com o arquivo, é sinal de que o install.xml foi escrito/
# editado à mão (ou por IA) em vez de gerado pelo editor XMLDB embutido — não é drift de banco
# (a tabela criada é idêntica), mas pode reprovar o teste core\db\plugin_checks_test do core e
# checagens estruturais parecidas do Plugin Directory.
#
# NÃO existe equivalente no CI: o moodle-plugin-ci só prepara ambientes de teste
# (phpu_/bht_), nunca instala o site mdl_ — por isso o check é uma ferramenta local.
#
# Uso:
#   moodle-check-schema [target] [--all]
#
#   target : 51 (web-1, padrão) | 45 (web45) | 52 (web52) | all (os três)
#   --all  : mostra TODAS as divergências de banco (core e terceiros), não só as dos seus
#            plugins. Não afeta o check de formato canônico, que já roda só nos seus.
#
# Exit 1 se houver divergência (banco ou formato canônico) nos seus plugins (ou em qualquer
# um, com --all no check de banco); 0 se limpo.

set -euo pipefail

if [ -f ~/.moodle-dev-tools.env ]; then
    set -a
    source ~/.moodle-dev-tools.env
    set +a
fi

HOST_ROOT="${MDT_MOODLE_PUBLIC:-/home/ubuntu/meu-moodle/html/public}"

# ------------------------------------------------------------------ #
#  Parse de argumentos                                               #
# ------------------------------------------------------------------ #
TARGET="51"
SHOW_ALL=0
while [ $# -gt 0 ]; do
    case "$1" in
        --all) SHOW_ALL=1; shift ;;
        51|45|52|all) TARGET="$1"; shift ;;
        -h|--help) sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "erro: argumento desconhecido '$1'" >&2; exit 1 ;;
    esac
done

C51="${MDT_CONTAINER_51:-meu-moodle-web-1}"
C45="${MDT_CONTAINER_45:-meu-moodle-web45-1}"
C52="${MDT_CONTAINER_52:-meu-moodle-web52-1}"
case "$TARGET" in
    51)  CONTAINERS="$C51" ;;
    45)  CONTAINERS="$C45" ;;
    52)  CONTAINERS="$C52" ;;
    all) CONTAINERS="$C51 $C45 $C52" ;;
esac

# ------------------------------------------------------------------ #
#  Deriva os prefixos de tabela e os componentes Frankenstyle dos    #
#  SEUS plugins (diretórios com .git), por tipo de plugin:           #
#    mod/X              → prefixo ""              · componente mod_X#
#    local/X            → prefixo local_          · componente local_X
#    blocks/X           → prefixo block_           · componente block_X
#    filter/X           → prefixo filter_          · componente filter_X
#    course/format/X    → prefixo format_          · componente format_X
#    report/X           → prefixo report_          · componente report_X
#    availability/condition/X → prefixo availability_ · componente availability_X
#  (availability/condition/ é um nível fixo do Moodle, não faz parte do nome)
# ------------------------------------------------------------------ #
TYPE_ENTRIES="mod: local:local_ blocks:block_ filter:filter_ course/format:format_ report:report_ availability/condition:availability_"

PREFIXES=""
COMPONENTS=""
for entry in $TYPE_ENTRIES; do
    dir="${entry%%:*}"
    pfx="${entry#*:}"
    [ -d "$HOST_ROOT/$dir" ] || continue
    while IFS= read -r gitdir; do
        [ -z "$gitdir" ] && continue
        name=$(basename "$(dirname "$gitdir")")
        PREFIXES="$PREFIXES ${pfx}${name}"
        if [ "$dir" = "mod" ]; then
            COMPONENTS="$COMPONENTS mod_${name}"
        else
            COMPONENTS="$COMPONENTS ${pfx}${name}"
        fi
    done < <(find "$HOST_ROOT/$dir" -mindepth 2 -maxdepth 2 -name .git 2>/dev/null)
done

# ------------------------------------------------------------------ #
#  Roda os checks em cada container e filtra a saída por bloco       #
# ------------------------------------------------------------------ #
RC=0
for cont in $CONTAINERS; do
    if ! docker ps --format '{{.Names}}' | grep -qx "$cont"; then
        echo "erro: container '$cont' não está rodando" >&2
        RC=1
        continue
    fi

    script=$(docker exec "$cont" sh -c \
        'find /var/www/html -maxdepth 4 -path "*/admin/cli/check_database_schema.php" 2>/dev/null | head -1')
    docroot=$(dirname "$(dirname "$(dirname "$script")")")
    output=$(docker exec "$cont" sh -c "php '$script' 2>&1" || true)

    echo "############## $cont ##############"
    # O check imprime blocos separados por linhas de '---', cada um iniciado pela linha do
    # nome da tabela. Filtra (em Python) os blocos cujo nome casa um prefixo seu.
    filtered=$(SHOW_ALL="$SHOW_ALL" PREFIXES="$PREFIXES" python3 - "$output" <<'PY'
import os, re, sys
output = sys.argv[1]
show_all = os.environ.get("SHOW_ALL") == "1"
prefixes = os.environ.get("PREFIXES", "").split()

# Quebra a saída em blocos: cada bloco começa numa linha de nome (sem indentação)
# e segue com linhas ' * ...'. Linhas de '---' são separadores.
blocks = []
current = None
for line in output.splitlines():
    if set(line.strip()) == {"-"} and line.strip():
        continue
    if line.startswith(" ") or line.startswith("*"):
        if current:
            current["lines"].append(line)
    elif line.strip():
        current = {"name": line.strip(), "lines": []}
        blocks.append(current)

def mine(name):
    return any(name == p or name.startswith(p + "_") for p in prefixes)

shown = [b for b in blocks if b["lines"] and (show_all or mine(b["name"]))]
for b in shown:
    print("-" * 60)
    print(b["name"])
    for l in b["lines"]:
        print(l)
if shown:
    print("-" * 60)
sys.exit(1 if shown else 0)
PY
) && fres=0 || fres=1
    if [ -n "$filtered" ]; then
        echo "$filtered"
    fi
    if [ "$fres" -ne 0 ]; then
        RC=1
    else
        echo "  sem divergências$([ "$SHOW_ALL" -eq 1 ] || echo ' nos seus plugins')."
    fi

    # -------------------------------------------------------------- #
    #  Check de formato canônico do install.xml (round-trip XMLDB)   #
    # -------------------------------------------------------------- #
    canon_output=$(docker exec -i -e DOCROOT="$docroot" -e COMPONENTS="$COMPONENTS" "$cont" php <<'PHP' 2>&1
<?php
define("CLI_SCRIPT", true);
require(getenv("DOCROOT") . "/config.php");

function load_xml(string $xml): DOMDocument {
    $dom = new DOMDocument();
    $dom->preserveWhiteSpace = false;
    libxml_use_internal_errors(true);
    $ok = $dom->loadXML($xml);
    libxml_use_internal_errors(false);
    if (!$ok) {
        throw new \RuntimeException("XML malformado");
    }
    return $dom;
}

// Compara dois nós XML de forma semântica: atributos como conjunto (ordem não importa,
// só espaço em branco insignificante em XML), elementos filhos em ordem, texto sem espaço
// de borda. Réplica o que assertXmlStringEqualsXmlString do PHPUnit já faz — string bruta
// ou DOMDocument::saveXML() não bastam porque preservam a ordem original dos atributos.
function xml_nodes_equal(DOMNode $a, DOMNode $b): bool {
    if ($a->nodeType !== $b->nodeType) {
        return false;
    }
    if ($a->nodeType === XML_TEXT_NODE) {
        return trim($a->nodeValue) === trim($b->nodeValue);
    }
    if ($a->nodeType !== XML_ELEMENT_NODE) {
        return true;
    }
    if ($a->nodeName !== $b->nodeName) {
        return false;
    }
    $attrsa = [];
    foreach ($a->attributes ?? [] as $attr) {
        $attrsa[$attr->nodeName] = $attr->nodeValue;
    }
    $attrsb = [];
    foreach ($b->attributes ?? [] as $attr) {
        $attrsb[$attr->nodeName] = $attr->nodeValue;
    }
    ksort($attrsa);
    ksort($attrsb);
    if ($attrsa !== $attrsb) {
        return false;
    }
    $childrena = [];
    foreach ($a->childNodes as $c) {
        if ($c->nodeType === XML_TEXT_NODE && trim($c->nodeValue) === "") {
            continue;
        }
        $childrena[] = $c;
    }
    $childrenb = [];
    foreach ($b->childNodes as $c) {
        if ($c->nodeType === XML_TEXT_NODE && trim($c->nodeValue) === "") {
            continue;
        }
        $childrenb[] = $c;
    }
    if (count($childrena) !== count($childrenb)) {
        return false;
    }
    foreach ($childrena as $i => $childa) {
        if (!xml_nodes_equal($childa, $childrenb[$i])) {
            return false;
        }
    }
    return true;
}

$components = array_filter(preg_split("/\s+/", trim((string) getenv("COMPONENTS"))));
$bad = [];
foreach ($components as $component) {
    $dir = core_component::get_component_directory($component);
    if ($dir === null) {
        continue;
    }
    $file = $dir . "/db/install.xml";
    if (!file_exists($file)) {
        continue;
    }
    $raw = file_get_contents($file);
    try {
        $xmldb = new xmldb_file($file);
        $xmldb->loadXMLStructure();
        $canon = $xmldb->getStructure()->xmlOutput();
        $domraw = load_xml($raw);
        $domcanon = load_xml($canon);
        $diverges = !xml_nodes_equal($domraw->documentElement, $domcanon->documentElement);
    } catch (\Throwable $e) {
        $bad[$component] = "erro ao carregar: " . $e->getMessage();
        continue;
    }
    if ($diverges) {
        $bad[$component] = $file;
    }
}
foreach ($bad as $component => $info) {
    echo "$component|$info\n";
}
PHP
) || true
    echo "----- install.xml: formato canônico (round-trip XMLDB) -----"
    if [ -n "$canon_output" ]; then
        while IFS='|' read -r comp info; do
            [ -z "$comp" ] && continue
            echo "  * $comp: não está no formato que o editor XMLDB geraria ($info)"
        done <<< "$canon_output"
        RC=1
    else
        echo "  sem divergências de formato nos seus plugins."
    fi
done

exit "$RC"
