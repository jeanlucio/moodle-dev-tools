# moodle-dev-tools

Pre-commit hook para desenvolvimento de plugins Moodle com duas camadas de revisão automática antes de cada commit:

1. **PHPCS** — padrão Moodle, roda localmente (~60ms), sem custo
2. **Revisão IA paralela** — múltiplos modelos em paralelo cobrem o que o PHPCS não detecta

## O que a IA revisa

A revisão cobre **PHP, JS (AMD), Mustache, CSS e XML** — todos os tipos de arquivo de um plugin Moodle.

### PHP

| # | Regra |
|---|---|
| 1 | PHPDoc: `@param` sem descrição, `@return` ausente, `@var` ausente em propriedades, tipos errados |
| 2 | Strings `lang/`: chave nova fora da ordem alfabética estrita |
| 3 | `$DB` dentro de loop `foreach/for/while` — antipadrão N+1 |
| 4 | `echo $var` sem `s()` / `format_string()` / `format_text()` |
| 5 | `require_sesskey()` ausente em bloco que processa `$_POST` |
| 6 | Texto hardcoded que deveria usar `get_string()` |
| 7 | Type hints e return types ausentes em funções/métodos novos |
| 8 | SQL com variáveis concatenadas diretamente (risco de injeção SQL) |
| 9 | `require_capability()` ausente antes de ação sensível |
| 10 | `defined('MOODLE_INTERNAL') \|\| die()` ausente onde obrigatório |
| 11 | Script de entrada sem `require_login()` antes de renderizar HTML |
| 12 | `unserialize()` com dado externo — usar `unserialize_object()` |
| 13 | `format_text()` com `['noclean' => true]` — proibido |
| 14 | `require_once` para arquivo em `classes/` — Moodle faz autoload |
| 15 | `print_error()` — depreciado; usar `throw new moodle_exception()` |
| 16 | `$DB->get_record()` filtrando só por `id` externo sem validação de `instanceid`/`contextid` |
| 17 | `style="..."` inline em HTML — criar classe em `styles.css` |
| 18 | Tag `<script>` em PHP ou Mustache — usar AMD via `js_call_amd()` |
| 19 | String em `lang/pt_br/` com "aluno/alunos" — usar "estudante/estudantes" |
| 20 | String adicionada em `lang/en/` sem correspondente em `lang/pt_br/` |

### JavaScript (`amd/src/*.js`)

| # | Regra |
|---|---|
| 21 | `var` declarado — usar `const` ou `let` |
| 22 | `jQuery.ajax()` ou `execCommand()` — proibidos |
| 23 | Import de `core/modal_factory` — removido no Moodle 5.2; usar `core/modal` |
| 24 | `==` ou `!=` — usar `===` / `!==` |
| 25 | Strings de UI hardcoded visíveis ao usuário — usar `core/str` |
| 26 | Cadeia `.then().then()` onde `async/await` é mais legível |

### Mustache (`*.mustache`)

| # | Regra |
|---|---|
| 27 | `@template` ausente no segundo bloco `{{! ... }}` |
| 28 | Heading vazio: `<h1>` a `<h6>` sem conteúdo ou variável |
| 29 | `sr-only` sozinho dentro de `.table` ou `.activity-item` (conflito com Boost) |
| 30 | Classe Bootstrap 4 depreciada: `ml-*`, `mr-*`, `text-right`, `data-dismiss` sem `data-bs-dismiss` |
| 31 | Ícone `<i class="fa-...">` sem texto adjacente e sem `aria-hidden="true"` |
| 32 | `<img>` sem atributo `alt` |
| 33 | Botão/link com só ícone sem `aria-label` ou `<span class="visually-hidden">` |
| 34 | `<th>` sem `scope="col"` ou `scope="row"` |
| 35 | `<input>`, `<select>` ou `<textarea>` sem `<label>` ou `aria-label` |

### CSS (`*.css`)

| # | Regra |
|---|---|
| 36 | `!important` — proibido; aumentar especificidade |
| 37 | Seletor sem escopo de path-class (`.path-*` ou `body.path-*`) |
| 38 | Hex hardcoded fora de `var()` — usar `var(--nome, #fallback)` |

### XML (`db/*.xml`)

| # | Regra |
|---|---|
| 39 | Nome de tabela (sem `mdl_`) com mais de 53 caracteres |
| 40 | Nome de campo com mais de 63 caracteres |

## Fluxo

```
git commit
    │
    ▼ (só se há .php staged)
PHPCS (local, ~60ms)
    ├── erros → bloqueia imediatamente
    └── OK
         │
         ▼ (PHP + JS + Mustache + CSS + XML)
    IAs em paralelo (~5–15s)
    Gemini, Groq, OpenAI-compatible (até 5 slots)
         │
         ├── qualquer uma retorna BLOQUEADO → bloqueia com relatório
         └── todas aprovam → commit acontece
```

Se uma IA falhar (rate limit, sem crédito, timeout), é ignorada silenciosamente. O commit só é bloqueado por resposta explícita de BLOQUEADO.

### Cobertura do diff por arquivo

O orçamento de 2000 linhas é distribuído proporcionalmente entre os arquivos staged, garantindo que nenhum arquivo seja ignorado. O cabeçalho GPL é removido do diff antes do envio (já verificado pelo PHPCS).

| Arquivos staged | Linhas por arquivo | Total enviado |
|---|---|---|
| 1 | 2000 | 2000 |
| 5 | 400 | 2000 |
| 10 | 200 | 2000 |
| 20 | 100 | 2000 |
| 40 | 50 (mínimo) | 2000 |
| 60 | 50 (mínimo) | 3000 |

O mínimo de 50 linhas por arquivo garante que cabeçalho, imports e primeiras declarações — onde a maioria dos problemas ocorre — estejam sempre presentes. Os 2000 linhas representam ~15 000 tokens, bem abaixo do limite de contexto de 128 K de todos os modelos configurados.

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
