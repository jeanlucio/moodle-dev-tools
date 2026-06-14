#!/usr/bin/env bash
# Instala o pre-commit hook de PHPCS + IA para desenvolvimento Moodle.

set -e

TOOLS_DIR="$HOME/.moodle-dev-tools"
HOOKS_DIR="$HOME/.githooks"

echo "Instalando moodle-dev-tools..."

# Copia os arquivos de suporte
mkdir -p "$TOOLS_DIR"
cp phpcs-ai-call.py "$TOOLS_DIR/phpcs-ai-call.py"
cp phpcs-bootstrap.php "$TOOLS_DIR/phpcs-bootstrap.php"
chmod +x "$TOOLS_DIR/phpcs-ai-call.py"

# Instala o hook global como symlink (edições no repo entram em vigor imediatamente)
mkdir -p "$HOOKS_DIR"
ln -sf "$(pwd)/pre-commit" "$HOOKS_DIR/pre-commit"

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

echo ""
echo "Instalação concluída."
echo ""
echo "Pré-requisitos necessários (verifique manualmente):"
echo "  - PHP 8.x          : $(php --version 2>/dev/null | head -1 || echo 'não encontrado')"
echo "  - PHPCS             : $(phpcs --version 2>/dev/null | head -1 || echo 'não encontrado')"
echo "  - Moodle CS         : $(phpcs --config-show 2>/dev/null | grep default_standard || echo 'não configurado')"
echo "  - Python 3          : $(python3 --version 2>/dev/null || echo 'não encontrado')"
