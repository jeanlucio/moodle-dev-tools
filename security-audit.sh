#!/usr/bin/env bash
# moodle-security-audit — auditoria de segurança de um plugin Moodle: ferramentas
# determinísticas + revisão por IA, num relatório único.
#
# Complementa o pre-commit, que revisa DIFFS. Achado que exige ler o plugin inteiro e seguir
# cadeia de chamada entre arquivos (ex.: uma variável sanitizada com format_string() e a irmã
# ao lado crua) é invisível para revisão de diff — é esse buraco que esta ferramenta cobre.
#
# Roda contra a assinatura Claude Code (nunca API paga) e usa apenas ferramentas de LEITURA:
# é estruturalmente incapaz de alterar o plugin auditado.
#
# Uso:
#   moodle-security-audit <tipo/nome> [opções]
#
#   <tipo/nome>          : ex. filter/playerhud, blocks/playerhud (aceita o prefixo html/public/).
#   --model M            : modelo primário (padrão claude-fable-5).
#   --fallback-model M   : usado se o primário falhar (padrão claude-opus-5).
#   --phpstan-level N    : nível do PHPStan 0..9 (padrão 6). Alto de propósito: a triagem
#                          por IA é o que torna nível alto utilizável em Moodle.
#   --batch-lines N      : orçamento de linhas por lote de varredura (padrão 3500).
#   --jobs N             : chamadas de IA em paralelo (padrão 3).
#   --with-moodlecheck   : roda também o local_moodlecheck (PHPDoc; release, não segurança).
#   --no-verify          : pula o passe de verificação (mais rápido, mais falso positivo).
#   --no-cache           : ignora o cache de lotes.
#   --json               : grava também o relatório em JSON.
#   --from-json ARQ      : re-renderiza o relatório de um JSON já gerado, sem refazer a
#                          análise nem gastar cota (para ajustar o modelo do relatório).
#
# Relatório em <plugin>/.plans/security-audit/<frankenstyle>-<AAAA-MM-DD>-<HHMMSS>.md — junto do
# código que descreve. A pasta é criada se faltar e .plans/ garantidamente entra no
# .gitignore do plugin: um relatório que aponta linhas exploráveis não pode virar arquivo
# versionado num repo público.
# É ferramenta de bancada: NÃO vai no ZIP do Plugin Directory e NÃO altera o código-fonte.

set -euo pipefail

MOODLE="/home/ubuntu/meu-moodle/html/public"

PLUGIN=""
PASSTHRU=()
while [ $# -gt 0 ]; do
    case "$1" in
        --model|--fallback-model|--phpstan-level|--batch-lines|--jobs|--from-json)
            PASSTHRU+=("$1" "${2:?$1 exige um valor}"); shift 2 ;;
        --with-moodlecheck|--no-verify|--no-cache|--json)
            PASSTHRU+=("$1"); shift ;;
        -h|--help) sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) echo "erro: opção desconhecida '$1'" >&2; exit 1 ;;
        *) [ -n "$PLUGIN" ] && { echo "erro: informe um plugin só" >&2; exit 1; }; PLUGIN="$1"; shift ;;
    esac
done

if [ -z "$PLUGIN" ]; then
    echo "uso: moodle-security-audit <tipo/nome> [opções]" >&2
    echo "ex.: moodle-security-audit filter/playerhud" >&2
    exit 1
fi
PLUGIN="${PLUGIN#./}"; PLUGIN="${PLUGIN#html/public/}"; PLUGIN="${PLUGIN#public/}"; PLUGIN="${PLUGIN%/}"
PLUGIN_ABS="$MOODLE/$PLUGIN"

if [ ! -d "$PLUGIN_ABS" ]; then
    echo "erro: '$PLUGIN_ABS' não existe" >&2
    exit 1
fi

if ! command -v claude >/dev/null 2>&1; then
    echo "erro: binário 'claude' não encontrado no PATH" >&2
    echo "  instale com: npm install -g @anthropic-ai/claude-code" >&2
    exit 1
fi

# -u: sem buffer, para o progresso das fases aparecer em tempo real mesmo com a saída
# redirecionada para arquivo ou pipe (a auditoria leva minutos; progresso mudo é ruim).
python3 -u "$(dirname "$(readlink -f "$0")")/security_audit.py" "$PLUGIN_ABS" "${PASSTHRU[@]}"
