#!/usr/bin/env bash
# moodle-mirror — garante que cada plugin do dev esteja montado (bind mount) nos containers
# web45 e web52 EM QUE ELE É COMPATÍVEL, espelhando a árvore do web-1. Detecta os faltantes
# compatíveis, adiciona as linhas no docker-compose.yml (com backup), recria os containers
# afetados e roda moodle-upgrade para instalar nos bancos os plugins recém-montados.
#
# Uso: moodle-mirror [--dry-run]
#
# Critério de "plugin do dev": diretório com repositório .git cujo remote contém 'jeanlucio'.
# Plugins de terceiros (codechecker/moodlecheck do Moodle HQ, theme_moove, etc.) são ignorados.
#
# Compatibilidade: um plugin só é espelhado para um container se a versão do core daquele
# container atende ao $plugin->requires E (se houver $plugin->supported) o branch do container
# está no range suportado. Isso evita montar, p.ex., um tema 5.1-only no web45/web52 — um
# plugin incompatível faz o admin/cli/upgrade.php ABORTAR a instalação de TODOS os outros.

set -euo pipefail

ROOT="/home/ubuntu/meu-moodle"
COMPOSE="$ROOT/docker-compose.yml"
DRYRUN=0
[ "${1:-}" = "--dry-run" ] && DRYRUN=1

# Versão (datecode inteiro) e branch do core de cada container alvo.
core_info() {
    docker exec "$1" sh -c \
        'php -r "define(\"CLI_SCRIPT\",1);require(\"/var/www/html/config.php\");echo \$CFG->version.\"|\".\$CFG->branch;"' \
        2>/dev/null
}
i45=$(core_info meu-moodle-web45-1); VER45=${i45%%|*}; VER45=${VER45%%.*}; BR45=${i45##*|}
i52=$(core_info meu-moodle-web52-1); VER52=${i52%%|*}; VER52=${VER52%%.*}; BR52=${i52##*|}

# Compatível? $1=plugin_path $2=core_ver_int $3=core_branch
is_compat() {
    local vf="$ROOT/html/public/$1/version.php" req sup mn mx
    [ -f "$vf" ] || return 1
    req=$(grep -oE '\$plugin->requires[[:space:]]*=[[:space:]]*[0-9]+' "$vf" | grep -oE '[0-9]+' | head -1)
    [ -n "$req" ] && [ "$req" -gt "$2" ] && return 1
    sup=$(grep -oE '\$plugin->supported[[:space:]]*=[[:space:]]*\[[0-9, ]+\]' "$vf" | grep -oE '[0-9]+')
    if [ -n "$sup" ]; then
        mn=$(printf '%s\n' "$sup" | head -1); mx=$(printf '%s\n' "$sup" | tail -1)
        { [ "$3" -lt "$mn" ] || [ "$3" -gt "$mx" ]; } && return 1
    fi
    return 0
}

# 1. Detecta os plugins do dev (remote jeanlucio) em html/public.
cd "$ROOT/html/public"
plugins=$(while IFS= read -r gd; do
    d=$(dirname "$gd")
    git -C "$d" remote get-url origin 2>/dev/null | grep -q jeanlucio && echo "${d#./}"
done < <(find . -maxdepth 6 -name .git -type d 2>/dev/null) | sort -u)

# 2. Faltantes compatíveis por container; e avisa os incompatíveis (pulados de propósito).
miss45=""; miss52=""; skipped=""
while IFS= read -r p; do
    [ -z "$p" ] && continue
    if is_compat "$p" "$VER45" "$BR45"; then
        grep -qF "./html/public/$p:/var/www/html/$p'" "$COMPOSE" || miss45="${miss45}${p}"$'\n'
    else
        grep -qF "./html/public/$p:/var/www/html/$p'" "$COMPOSE" || skipped="${skipped}  $p — incompatível com web45 (branch $BR45)"$'\n'
    fi
    if is_compat "$p" "$VER52" "$BR52"; then
        grep -qF "./html/public/$p:/var/www/html/public/$p'" "$COMPOSE" || miss52="${miss52}${p}"$'\n'
    else
        grep -qF "./html/public/$p:/var/www/html/public/$p'" "$COMPOSE" || skipped="${skipped}  $p — incompatível com web52 (branch $BR52)"$'\n'
    fi
done <<< "$plugins"
miss45=$(printf '%s' "$miss45" | grep -v '^$' || true)
miss52=$(printf '%s' "$miss52" | grep -v '^$' || true)
skipped=$(printf '%s' "$skipped" | grep -v '^$' || true)

echo "=== a espelhar no web45 ==="; printf '%s\n' "${miss45:-(nenhum)}"
echo "=== a espelhar no web52 ==="; printf '%s\n' "${miss52:-(nenhum)}"
[ -n "$skipped" ] && { echo "=== pulados por incompatibilidade ==="; printf '%s\n' "$skipped"; }

if [ -z "$miss45" ] && [ -z "$miss52" ]; then
    echo "Nada compatível a espelhar."
    exit 0
fi
if [ "$DRYRUN" -eq 1 ]; then
    echo "(dry-run) nenhuma alteração feita."
    exit 0
fi

# 3. Backup + inserção das linhas (antes do mount do xdebug.ini de cada serviço).
cp "$COMPOSE" "$COMPOSE.bak.$(date +%Y%m%d%H%M%S)"
MISS45="$miss45" MISS52="$miss52" python3 - "$COMPOSE" <<'PY'
import os, re, sys
path = sys.argv[1]
lines = open(path).readlines()
miss = {
    'web45': [p for p in os.environ.get('MISS45', '').split('\n') if p],
    'web52': [p for p in os.environ.get('MISS52', '').split('\n') if p],
}
def mount(svc, p):
    dest = ('/var/www/html/%s' % p) if svc == 'web45' else ('/var/www/html/public/%s' % p)
    return "      - './html/public/%s:%s'\n" % (p, dest)
out, cur = [], None
for line in lines:
    m = re.match(r'^  ([a-z0-9_-]+):\s*$', line)
    if m:
        cur = m.group(1)
    if cur in miss and miss[cur] and 'xdebug.ini' in line:
        out.extend(mount(cur, p) for p in miss[cur])
        miss[cur] = []
    out.append(line)
open(path, 'w').writelines(out)
print('docker-compose.yml atualizado')
PY

# 4. Valida o YAML antes de recriar nada.
python3 -c "import yaml; yaml.safe_load(open('$COMPOSE')); print('YAML válido')"

# 5. Recria os containers afetados (os bancos não são tocados).
echo "=== recriando web45 e web52 ==="
cd "$ROOT"
docker compose up -d web45 web52 2>&1 | tail -6
sleep 8

# 6. Instala os plugins recém-montados nos bancos e valida o schema.
echo ""
echo "=== upgrade + validação de schema ==="
moodle-upgrade all
