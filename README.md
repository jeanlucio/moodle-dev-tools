# moodle-dev-tools

Pre-commit hook para desenvolvimento de plugins Moodle com duas camadas de revisão automática antes de cada commit:

1. **PHPCS** — padrão Moodle, roda localmente (~60ms), sem custo
2. **Revisão IA paralela** — múltiplos modelos em paralelo cobrem o que o PHPCS não detecta

## O que a IA revisa

| # | Regra | Por que PHPCS não pega |
|---|---|---|
| 1 | PHPDoc incompleto (`@param` sem descrição, `@return` ausente, tipos errados) | PHPCS verifica presença, não qualidade |
| 2 | Strings `lang/` fora de ordem alfabética | Requer leitura semântica do arquivo |
| 3 | `$DB` dentro de loop — antipadrão N+1 | Análise de fluxo de controle |
| 4 | `echo $var` sem `s()` / `format_string()` / `format_text()` | Rastreamento de origem da variável |
| 5 | `require_sesskey()` ausente em código que processa POST | Rastreamento de fluxo HTTP |
| 6 | Texto hardcoded que deveria usar `get_string()` | Detecção semântica de literais |
| 7 | Type hints e return types ausentes em funções/métodos novos | PHPCS exige só em alguns contextos |
| 8 | SQL com variáveis concatenadas (risco de injeção) | Análise de interpolação |
| 9 | `require_capability()` ausente antes de ação sensível | Rastreamento de contexto de permissão |
| 10 | `defined('MOODLE_INTERNAL') \|\| die()` ausente | Requer contexto do tipo de arquivo |

## Fluxo

```
git commit
    │
    ▼
PHPCS (local, ~60ms)
    ├── erros → bloqueia imediatamente
    └── OK
         │
         ▼
    IAs em paralelo (~5–15s)
    Gemini, Groq, OpenAI-compatible (até 5 slots)
         │
         ├── qualquer uma retorna BLOQUEADO → bloqueia com relatório
         └── todas aprovam → commit acontece
```

Se uma IA falhar (rate limit, sem crédito, timeout), é ignorada silenciosamente. O commit só é bloqueado por resposta explícita de BLOQUEADO.

## Pré-requisitos

- PHP 8.x
- [PHPCS](https://github.com/squizlabs/PHP_CodeSniffer) instalado globalmente (`/usr/local/bin/phpcs`)
- [moodle-cs](https://github.com/moodlehq/moodle-cs) configurado como padrão padrão do PHPCS
- Python 3 (biblioteca padrão apenas, sem dependências externas)
- Ao menos uma chave de API de IA configurada

### Instalando PHPCS + moodle-cs

```bash
# Instala o PHPCS globalmente
composer global require squizlabs/php_codesniffer

# Clona e configura o moodle-cs
git clone https://github.com/moodlehq/moodle-cs ~/moodle-cs
cd ~/moodle-cs && composer install

# Define o padrão e os caminhos
phpcs --config-set default_standard moodle
phpcs --config-set installed_paths ~/moodle-cs,~/moodle-cs/vendor/phpcsstandards/phpcsextra,~/moodle-cs/vendor/phpcsstandards/phpcsutils
```

> **Nota PHP 8.3:** o `phpcsutils 1.1+` referencia constantes do PHP 8.4 em tempo de compilação.
> O arquivo `phpcs-bootstrap.php` resolve isso automaticamente — o `install.sh` cuida da configuração.

## Instalação

```bash
git clone https://github.com/jeanlucio/moodle-dev-tools.git
cd moodle-dev-tools
bash install.sh
```

O script:
- Copia `phpcs-ai-call.py` e `phpcs-bootstrap.php` para `~/.moodle-dev-tools/`
- Instala o hook em `~/.githooks/pre-commit`
- Configura `git config --global core.hooksPath ~/.githooks`
- Cria `~/.phpcs-ai.env` a partir do template (se ainda não existir)

## Configuração das chaves de API

Edite `~/.phpcs-ai.env` e preencha as chaves que tiver:

```bash
# Google Gemini
GEMINI_KEY=sua-chave

# Groq
GROQ_KEY=sua-chave
GROQ_MODEL=llama-3.3-70b-versatile

# OpenAI-compatible (OpenRouter, NVIDIA NIM, OpenAI, etc.)
OPENAI_KEY=sua-chave
OPENAI_URL=https://openrouter.ai/api/v1/chat/completions
OPENAI_MODEL=deepseek/deepseek-v4-flash
```

Slots de `OPENAI` a `OPENAI5` são suportados. Basta adicionar `OPENAI2_KEY`, `OPENAI2_URL`, `OPENAI2_MODEL` e assim por diante.

O arquivo `~/.phpcs-ai.env.example` tem o template completo com comentários.

### Modelos gratuitos testados e aprovados

| Provider | Modelo | Observação |
|---|---|---|
| Groq | `llama-3.3-70b-versatile` | Rápido, consistente |
| OpenRouter | `deepseek/deepseek-v4-flash` | Boa relação custo/qualidade |
| OpenRouter | `openai/gpt-oss-120b:free` | Formato de resposta excelente |
| NVIDIA NIM | `meta/llama-3.3-70b-instruct` | Gratuito com conta NVIDIA |

## Uso

O hook roda automaticamente a cada `git commit`. Nenhuma ação necessária.

**Pular a revisão IA** (falso positivo confirmado):

```bash
SKIP_AI=1 git commit -m "mensagem"
```

O PHPCS não pode ser pulado — é obrigatório.

## Estrutura dos arquivos instalados

```
~/.githooks/
└── pre-commit          ← hook git global

~/.moodle-dev-tools/
├── phpcs-ai-call.py    ← caller Python (Gemini + OpenAI-compatible)
└── phpcs-bootstrap.php ← fix de compatibilidade PHP 8.3 / phpcsutils

~/.phpcs-ai.env         ← suas chaves de API (chmod 600, nunca commitar)
```
