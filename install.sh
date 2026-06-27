#!/usr/bin/env bash
# Instala o pre-commit hook de PHPCS + IA e o monitor de plugins para Moodle.

set -e

TOOLS_DIR="$HOME/.moodle-dev-tools"
HOOKS_DIR="$HOME/.githooks"

echo "Instalando moodle-dev-tools..."

# Copia os arquivos de suporte
mkdir -p "$TOOLS_DIR"
cp phpcs-ai-call.py "$TOOLS_DIR/phpcs-ai-call.py"
cp phpcs-bootstrap.php "$TOOLS_DIR/phpcs-bootstrap.php"
chmod +x "$TOOLS_DIR/phpcs-ai-call.py"

# Instala os hooks globais como symlinks (edições no repo entram em vigor imediatamente)
mkdir -p "$HOOKS_DIR"
ln -sf "$(pwd)/pre-commit"          "$HOOKS_DIR/pre-commit"
ln -sf "$(pwd)/prepare-commit-msg"  "$HOOKS_DIR/prepare-commit-msg"

# Ferramentas de bancada — symlinks em ~/.local/bin (no PATH)
BIN_DIR="$HOME/.local/bin"
mkdir -p "$BIN_DIR"
chmod +x "$(pwd)/coverage.sh" "$(pwd)/check-schema.sh" "$(pwd)/upgrade.sh" \
    "$(pwd)/mirror.sh" "$(pwd)/phpstan.sh"
ln -sf "$(pwd)/coverage.sh"     "$BIN_DIR/moodle-coverage"
ln -sf "$(pwd)/check-schema.sh" "$BIN_DIR/moodle-check-schema"
ln -sf "$(pwd)/upgrade.sh"      "$BIN_DIR/moodle-upgrade"
ln -sf "$(pwd)/mirror.sh"       "$BIN_DIR/moodle-mirror"
ln -sf "$(pwd)/phpstan.sh"      "$BIN_DIR/moodle-phpstan"

# PHPStan + extensão Moodle ficam num projeto Composer isolado em phpstan/ (não no Moodle).
if command -v composer >/dev/null 2>&1; then
    (cd "$(pwd)/phpstan" && composer install --no-interaction >/dev/null 2>&1) \
        && echo "PHPStan (+ extensão Moodle) instalado em phpstan/vendor" \
        || echo "aviso: 'composer install' em phpstan/ falhou — rode manualmente"
else
    echo "aviso: composer não encontrado — moodle-phpstan precisa de 'composer install' em phpstan/"
fi

# Configura o git globalmente
git config --global core.hooksPath "$HOOKS_DIR"

# Cria o arquivo de chaves se ainda não existir
if [ ! -f "$HOME/.phpcs-ai.env" ]; then
    cp .phpcs-ai.env.example "$HOME/.phpcs-ai.env"
    chmod 600 "$HOME/.phpcs-ai.env"
    echo ""
    echo "Arquivo ~/.phpcs-ai.env criado."
    echo "Edite-o e preencha ao menos uma chave de API para ativar a revisão IA."
else
    echo "~/.phpcs-ai.env já existe — não foi sobrescrito."
fi

# Monitores de plugins (opcionais — requerem Telegram configurado em ~/.phpcs-ai.env)
echo ""
read -r -p "Instalar monitor de novos plugins Moodle? [s/N] " _reply
if [[ "$_reply" =~ ^[Ss]$ ]]; then
    cp plugins-monitor.py "$TOOLS_DIR/plugins-monitor.py"
    chmod +x "$TOOLS_DIR/plugins-monitor.py"

    if crontab -l 2>/dev/null | grep -q 'plugins-monitor.py'; then
        echo "Cron do monitor de novos plugins já configurado — não alterado."
    else
        (crontab -l 2>/dev/null; echo "0 6 * * * /usr/bin/python3 $TOOLS_DIR/plugins-monitor.py >> $HOME/.moodle-plugins-monitor.log 2>&1") | crontab -
        echo "Cron configurado: execução diária às 6h."
    fi

    if ! grep -q 'TELEGRAM_TOKEN=.' "$HOME/.phpcs-ai.env" 2>/dev/null; then
        echo ""
        echo "  Preencha TELEGRAM_TOKEN e TELEGRAM_CHAT_ID em ~/.phpcs-ai.env"
        echo "  para ativar as notificações. Veja o README para instruções."
    fi
fi

echo ""
read -r -p "Instalar monitor de atualizações de plugins específicos? [s/N] " _reply
if [[ "$_reply" =~ ^[Ss]$ ]]; then
    cp plugins-watch.py "$TOOLS_DIR/plugins-watch.py"
    chmod +x "$TOOLS_DIR/plugins-watch.py"

    if crontab -l 2>/dev/null | grep -q 'plugins-watch.py'; then
        echo "Cron do monitor de atualizações já configurado — não alterado."
    else
        (crontab -l 2>/dev/null; echo "15 6 * * * /usr/bin/python3 $TOOLS_DIR/plugins-watch.py >> $HOME/.moodle-plugins-monitor.log 2>&1") | crontab -
        echo "Cron configurado: execução diária às 6h15."
    fi

    echo ""
    echo "  Edite WATCH_PLUGINS em $TOOLS_DIR/plugins-watch.py para ajustar"
    echo "  a lista de plugins monitorados."
fi

echo ""
echo "Instalação concluída."
echo ""
echo "Pré-requisitos necessários (verifique manualmente):"
echo "  - PHP 8.x          : $(php --version 2>/dev/null | head -1 || echo 'não encontrado')"
echo "  - PHPCS             : $(phpcs --version 2>/dev/null | head -1 || echo 'não encontrado')"
echo "  - Moodle CS         : $(phpcs --config-show 2>/dev/null | grep default_standard || echo 'não configurado')"
echo "  - Python 3          : $(python3 --version 2>/dev/null || echo 'não encontrado')"
