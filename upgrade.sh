#!/usr/bin/env bash
# moodle-upgrade — aplica os upgrades pendentes do banco nos containers de dev e valida o
# schema no fim, numa operação atômica (o check de schema vem sempre junto, impossível esquecer).
#
# Uso: moodle-upgrade [51|45|52|all]   (padrão: all)
#
# Para cada container do alvo: roda admin/cli/upgrade.php (com --allow-unstable como fallback
# automático se o container estiver em versão beta/dev) e purga os caches. No fim, roda o
# moodle-check-schema no mesmo alvo para confirmar que o resultado bate com os install.xml.
# Sai com código != 0 se algum upgrade falhar ou o schema divergir.

set -euo pipefail

if [ -f ~/.moodle-dev-tools.env ]; then
    set -a
    source ~/.moodle-dev-tools.env
    set +a
fi

C51="${MDT_CONTAINER_51:-meu-moodle-web-1}"
C45="${MDT_CONTAINER_45:-meu-moodle-web45-1}"
C52="${MDT_CONTAINER_52:-meu-moodle-web52-1}"

TARGET="${1:-all}"
case "$TARGET" in
    51)  CONTAINERS="$C51" ;;
    45)  CONTAINERS="$C45" ;;
    52)  CONTAINERS="$C52" ;;
    all) CONTAINERS="$C51 $C45 $C52" ;;
    -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "uso: moodle-upgrade [51|45|52|all]" >&2; exit 1 ;;
esac

RC=0
for cont in $CONTAINERS; do
    if ! docker ps --format '{{.Names}}' | grep -qx "$cont"; then
        echo "erro: container '$cont' não está rodando" >&2
        RC=1
        continue
    fi

    echo "############## $cont — upgrade ##############"
    up=$(docker exec "$cont" sh -c \
        'find /var/www/html -maxdepth 4 -path "*/admin/cli/upgrade.php" 2>/dev/null | head -1')

    set +e
    out=$(docker exec "$cont" sh -c "php '$up' --non-interactive 2>&1")
    urc=$?
    set -e

    # Fallback: se abortou pelo gate de código instável, o próprio Moodle sugere a flag.
    if printf '%s' "$out" | grep -qi "allow-unstable"; then
        echo "  versão beta/dev detectada — re-tentando com --allow-unstable"
        set +e
        out=$(docker exec "$cont" sh -c "php '$up' --non-interactive --allow-unstable 2>&1")
        urc=$?
        set -e
    fi

    if [ "$urc" -ne 0 ]; then
        echo "  UPGRADE FALHOU (exit $urc):"
        printf '%s\n' "$out" | tail -10 | sed 's/^/    /'
        RC=1
        continue
    fi

    # Resumo das linhas relevantes (conclusão ou "nada a fazer").
    summary=$(printf '%s\n' "$out" | grep -iE "completada|completed|Nothing to|no upgrade|atualizaç" | tail -2)
    if [ -n "$summary" ]; then
        printf '%s\n' "$summary" | sed 's/^/  /'
    else
        echo "  upgrade aplicado (sem mensagem de conclusão explícita)"
    fi

    pc=$(docker exec "$cont" sh -c \
        'find /var/www/html -maxdepth 4 -path "*/admin/cli/purge_caches.php" 2>/dev/null | head -1')
    docker exec "$cont" php "$pc" >/dev/null 2>&1 && echo "  caches purgados"
done

echo ""
echo "############## validação de schema ##############"
moodle-check-schema "$TARGET" || RC=1

exit "$RC"
