#!/usr/bin/env bash
# moodle-check-schema — valida o schema físico do banco contra os install.xml e mostra só
# as divergências dos SEUS plugins (filtra o ruído do core e de plugins de terceiros).
#
# Roda o admin/cli/check_database_schema.php nativo do Moodle dentro do container de dev,
# onde o site de produção (mdl_) está instalado. É a ferramenta para pegar "drift" do banco
# de desenvolvimento: install.xml evoluiu e o banco não acompanhou (faltou reinstalar/upgrade).
#
# NÃO existe equivalente no CI: o moodle-plugin-ci só prepara ambientes de teste
# (phpu_/bht_), nunca instala o site mdl_ — por isso o check é uma ferramenta local.
#
# Uso:
#   moodle-check-schema [target] [--all]
#
#   target : 51 (web-1, padrão) | 45 (web45) | 52 (web52) | all (os três)
#   --all  : mostra TODAS as divergências (core e terceiros), não só as dos seus plugins
#
# Exit 1 se houver divergência nos seus plugins (ou em qualquer um, com --all); 0 se limpo.

set -euo pipefail

HOST_ROOT="/home/ubuntu/meu-moodle/html/public"

# ------------------------------------------------------------------ #
#  Parse de argumentos                                               #
# ------------------------------------------------------------------ #
TARGET="51"
SHOW_ALL=0
while [ $# -gt 0 ]; do
    case "$1" in
        --all) SHOW_ALL=1; shift ;;
        51|45|52|all) TARGET="$1"; shift ;;
        -h|--help) sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "erro: argumento desconhecido '$1'" >&2; exit 1 ;;
    esac
done

case "$TARGET" in
    51)  CONTAINERS="meu-moodle-web-1" ;;
    45)  CONTAINERS="meu-moodle-web45-1" ;;
    52)  CONTAINERS="meu-moodle-web52-1" ;;
    all) CONTAINERS="meu-moodle-web-1 meu-moodle-web45-1 meu-moodle-web52-1" ;;
esac

# ------------------------------------------------------------------ #
#  Deriva os prefixos de tabela dos SEUS plugins (diretórios .git)   #
#  mod/X → "X" · local/X → "local_X" · blocks/X → "block_X" · etc.   #
# ------------------------------------------------------------------ #
PREFIXES=""
for type in mod local blocks filter; do
    [ -d "$HOST_ROOT/$type" ] || continue
    while IFS= read -r gitdir; do
        [ -z "$gitdir" ] && continue
        name=$(basename "$(dirname "$gitdir")")
        case "$type" in
            mod)    PREFIXES="$PREFIXES $name" ;;
            blocks) PREFIXES="$PREFIXES block_$name" ;;
            *)      PREFIXES="$PREFIXES ${type}_$name" ;;
        esac
    done < <(find "$HOST_ROOT/$type" -mindepth 2 -maxdepth 2 -name .git 2>/dev/null)
done

# ------------------------------------------------------------------ #
#  Roda o check em cada container e filtra a saída por bloco         #
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
done

exit "$RC"
