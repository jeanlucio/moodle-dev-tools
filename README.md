# moodle-dev-tools

Ferramentas de automação para desenvolvimento de plugins Moodle:

1. **PHPCS** — padrão Moodle, roda localmente (~60ms), sem custo
2. **Revisão IA paralela** — múltiplos modelos em paralelo cobrem o que o PHPCS não detecta
3. **Geração de mensagem de commit** — IA gera o texto do commit a partir do diff; você revisa no editor
4. **Monitor de novos plugins** — aviso diário via Telegram quando plugins são publicados no diretório oficial

---

## Hook 1 — pre-commit: PHPCS + revisão IA

### O que a IA revisa

A revisão cobre **PHP, JS (AMD), Mustache, CSS e XML** — todos os tipos de arquivo de um plugin Moodle. Arquivos minificados (`amd/build/`) são ignorados.

#### PHP

| # | Regra |
|---|---|
| 1 | PHPDoc: `@param` sem descrição, `@return` ausente, `@var` ausente em propriedades, tipos errados |
| 2 | `$DB` dentro de loop `foreach/for/while` — antipadrão N+1 |
| 3 | `echo $var` sem `s()` / `format_string()` / `format_text()` |
| 4 | `require_sesskey()` ausente em bloco que processa `$_POST` |
| 5 | Texto hardcoded que deveria usar `get_string()` |
| 6 | Type hints e return types ausentes em funções/métodos novos |
| 7 | SQL com variáveis concatenadas diretamente (risco de injeção SQL) |
| 8 | `require_capability()` ausente antes de ação sensível |
| 9 | `$PAGE->requires->js_call_amd()` com 3º argumento contendo array grande ou dado de `$DB->get_records*()` — usar `data-*` ou `<script type="application/json">` |
| 10 | Script de entrada sem `require_login()` antes de renderizar HTML |
| 11 | `unserialize()` com dado externo — usar `unserialize_object()` |
| 12 | `format_text()` com `['noclean' => true]` — proibido |
| 13 | `require_once` para arquivo em `classes/` — Moodle faz autoload |
| 14 | `print_error()` — depreciado; usar `throw new moodle_exception()` |
| 15 | `$DB->get_record()` filtrando só por `id` externo sem validação de `instanceid`/`contextid` |
| 16 | `style="..."` inline em HTML — criar classe em `styles.css` |
| 17 | Tag `<script>` em PHP ou Mustache — usar AMD via `js_call_amd()` |
| 18 | String em `lang/pt_br/` com "aluno/alunos" — usar "estudante/estudantes" |
| 19 | String adicionada em `lang/en/` sem correspondente em `lang/pt_br/` |

#### JavaScript (`amd/src/*.js`)

| # | Regra |
|---|---|
| 20 | `var` declarado — usar `const` ou `let` |
| 21 | `jQuery.ajax()` ou `execCommand()` — proibidos |
| 22 | Import de `core/modal_factory` — removido no Moodle 5.2; usar `core/modal` |
| 23 | `==` ou `!=` — usar `===` / `!==` |
| 24 | Strings de UI hardcoded visíveis ao usuário — usar `core/str` |
| 25 | Cadeia `.then().then()` onde `async/await` é mais legível |

#### Mustache (`*.mustache`)

| # | Regra |
|---|---|
| 26 | `@template` ausente no segundo bloco `{{! ... }}` |
| 27 | Heading vazio: `<h1>` a `<h6>` sem conteúdo ou variável |
| 28 | `sr-only` sozinho dentro de `.table` ou `.activity-item` (conflito com Boost) |
| 29 | Classe Bootstrap 4 depreciada: `ml-*`, `mr-*`, `text-right` — só quando sem equivalente BS5 no mesmo elemento. Para dismiss: flagar `data-dismiss` apenas quando `data-bs-dismiss` **estiver ausente**; ter os dois atributos simultaneamente é o padrão correto de compatibilidade BS4+BS5 |
| 30 | Ícone `<i class="fa-...">` sem texto adjacente e sem `aria-hidden="true"` |
| 31 | `<img>` sem atributo `alt` |
| 32 | Botão/link com só ícone sem `aria-label` ou `<span class="visually-hidden">` |
| 33 | `<th>` sem `scope="col"` ou `scope="row"` |
| 34 | `<input>`, `<select>` ou `<textarea>` sem `<label>` ou `aria-label` |

#### CSS (`*.css`)

| # | Regra |
|---|---|
| 35 | `!important` — proibido; aumentar especificidade |
| 36 | Seletor sem escopo de path-class (`.path-*` ou `body.path-*`) |
| 37 | Hex hardcoded fora de `var()` — usar `var(--nome, #fallback)` |

#### XML (`db/*.xml`)

| # | Regra |
|---|---|
| 38 | Nome de tabela (sem `mdl_`) com mais de 53 caracteres |
| 39 | Nome de campo com mais de 63 caracteres |

### Fluxo

```
git commit
    │
    ▼ (só se há .php staged)
PHPCS (local, ~60ms)
    ├── erros → bloqueia imediatamente
    └── OK
         │
         ▼ (PHP + JS + Mustache + CSS + XML, exclui amd/build/)
    IAs em paralelo (~5–15s)
    Gemini, Groq, OpenAI-compatible (até 5 slots)
         │
         ├── qualquer uma retorna BLOQUEADO → bloqueia com relatório
         └── todas aprovam
              │
              ▼ (opcional — só quando sem -m)
         IA gera mensagem de commit (~3–8s)
              │
              ▼
         Editor abre pré-preenchido → revise e salve → commit acontece
```

Se uma IA falhar (rate limit, cota, timeout), o erro é exibido no terminal e ela é ignorada. O commit só é bloqueado por resposta explícita `BLOQUEADO`.

**Pular a revisão IA** (falso positivo confirmado):

```bash
SKIP_AI=1 git commit -m "mensagem"
```

O PHPCS não pode ser pulado — é obrigatório.

### Cobertura do diff por arquivo

O orçamento de 2000 linhas é distribuído proporcionalmente entre os arquivos staged. O cabeçalho GPL é removido do diff antes do envio.

| Arquivos staged | Linhas por arquivo | Total enviado |
|---|---|---|
| 1 | 2000 | 2000 |
| 5 | 400 | 2000 |
| 10 | 200 | 2000 |
| 20 | 100 | 2000 |
| 40 | 50 (mínimo) | 2000 |
| 60 | 50 (mínimo) | 3000 |

---

## Hook 2 — prepare-commit-msg: geração de mensagem com IA

Quando você executa `git commit` **sem** `-m`, a IA analisa o diff staged e gera uma mensagem de commit completa. O editor abre pré-preenchido para você revisar e salvar.

### Fluxo

```
git commit          ← sem -m
    │
    ▼
IA analisa diff staged (~3–8s)
    │
    ▼
Editor abre pré-preenchido com a mensagem gerada
    │
    ├── Revise, edite se necessário → salve → commit acontece
    └── Feche sem salvar → commit é cancelado
```

`git commit -m "..."`, `--amend`, merge e squash pulam o hook automaticamente.

### Regras aplicadas à mensagem gerada

- **Plugin de terceiro** (caso comum): resumo curto, sem prefixo (`MDL-xxx`, `feat:`, `fix:`)
- **Contribuição core Moodle**: `MDL-12345 COMPONENT: resumo` — só quando o diff claramente aponta para core
- Linha 1: máximo 72 caracteres, sem ponto final
- Linha 2: sempre em branco
- Linha 3+: explica o **porquê** da mudança (o diff já mostra o quê)
- Narrativa limpa: sem mencionar ciclos de revisão ou bugs encontrados durante o desenvolvimento
- Sem atribuição de IA (`Co-authored-by`, `Signed-off-by`)
- Sempre em inglês

### Ordem dos providers

A IA tenta os providers em sequência, usando o primeiro que responder:

1. Groq (mais rápido)
2. Slots OpenAI-compatible (`OPENAI_*` a `OPENAI5_*`)
3. Gemini (fallback — sujeito a cota)

---

## Pré-requisitos

- PHP 8.x
- [PHPCS](https://github.com/squizlabs/PHP_CodeSniffer) instalado globalmente (`/usr/local/bin/phpcs`)
- [moodle-cs](https://github.com/moodlehq/moodle-cs) configurado como padrão do PHPCS
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
- Cria symlinks em `~/.githooks/` para `pre-commit` e `prepare-commit-msg`
- Configura `git config --global core.hooksPath ~/.githooks`
- Cria `~/.phpcs-ai.env` a partir do template (se ainda não existir)
- Pergunta se deseja instalar o monitor de plugins (opcional)

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

## Estrutura dos arquivos instalados

```
~/.githooks/
├── pre-commit              ← symlink → revisão PHPCS + IA a cada commit
└── prepare-commit-msg      ← symlink → geração de mensagem de commit com IA

~/.moodle-dev-tools/
├── phpcs-ai-call.py        ← caller Python (Gemini + OpenAI-compatible)
├── phpcs-bootstrap.php     ← fix de compatibilidade PHP 8.3 / phpcsutils
├── plugins-monitor.py      ← monitor de novos plugins (opcional)
└── plugins-watch.py        ← monitor de atualizações de plugins específicos (opcional)

~/.phpcs-ai.env             ← suas chaves de API (chmod 600, nunca commitar)
```

---

## Monitor de novos plugins Moodle

Script que roda uma vez por dia via cron, detecta plugins recém-publicados no
[Moodle Plugin Directory](https://moodle.org/plugins/) e envia um resumo em
português brasileiro via Telegram.

### Como funciona

- Consulta a API pública `download.moodle.org/api/1.3/pluglist.php` (sem bloqueio de bot)
- Detecta novidades pelo ID auto-incremental dos plugins
- Busca a descrição no repositório GitHub do plugin via GitHub API
- Gera o resumo em PT-BR com fallback chain de IAs: Gemini → OpenRouter/DeepSeek → OpenRouter/GPT-OSS
- Envia a notificação via Telegram

### Pré-requisitos

- Python 3 (biblioteca padrão apenas, sem dependências extras)
- Um bot do Telegram (criado via [@BotFather](https://t.me/BotFather))
- Ao menos uma chave de API de IA configurada em `~/.phpcs-ai.env`

### Configurar o bot Telegram

**1. Crie o bot:**

Abra uma conversa com [@BotFather](https://t.me/BotFather), envie `/newbot` e siga
as instruções. Copie o token gerado (formato `123456789:AAFxxx...`).

**2. Descubra seu chat ID:**

Envie qualquer mensagem ao bot e acesse no browser:
```
https://api.telegram.org/bot<SEU_TOKEN>/getUpdates
```
Procure o campo `"id"` dentro de `"chat"` no JSON retornado.

**3. Preencha `~/.phpcs-ai.env`:**

```bash
TELEGRAM_TOKEN=123456789:AAFxxx...
TELEGRAM_CHAT_ID=987654321
```

### Instalação via install.sh

O `install.sh` pergunta durante a instalação se deseja ativar o monitor.
Se confirmar, ele copia o script para `~/.moodle-dev-tools/` e registra o cron:

```
0 6 * * *  python3 ~/.moodle-dev-tools/plugins-monitor.py
```

### Instalação manual

```bash
cp plugins-monitor.py ~/.moodle-dev-tools/
chmod +x ~/.moodle-dev-tools/plugins-monitor.py

# Registra o cron (execução diária às 6h)
(crontab -l; echo "0 6 * * * /usr/bin/python3 $HOME/.moodle-dev-tools/plugins-monitor.py >> $HOME/.moodle-plugins-monitor.log 2>&1") | crontab -
```

### Teste

```bash
python3 ~/.moodle-dev-tools/plugins-monitor.py
```

O log fica em `~/.moodle-plugins-monitor.log`.

---

## Monitor de atualizações em plugins específicos

Script complementar (`plugins-watch.py`) que monitora uma lista configurável de
plugins e notifica quando uma nova versão é publicada, com resumo PT-BR das mudanças.

### Como funciona

- Consulta `download.moodle.org/api/1.3/pluglist.php` e compara `timelastreleased` de cada plugin monitorado com o estado salvo
- Para plugins com GitHub Releases: extrai o `body` da release como changelog
- Para plugins sem GitHub Releases (maioria): busca `CHANGES.md`/`CHANGELOG.md` diretamente no repositório e extrai a seção da versão mais recente
- Gera resumo PT-BR das mudanças via IA (mesma fallback chain: Gemini → OpenRouter/DeepSeek → OpenRouter/GPT-OSS)
- Envia notificação Telegram com link para as notas de release e para o Plugin Directory

### Lista de plugins monitorados

A lista fica em `WATCH_PLUGINS` no início do arquivo `plugins-watch.py`.
Edite após instalar (`~/.moodle-dev-tools/plugins-watch.py`) para adicionar ou remover plugins.
Use o `component` frankenstyle como identificador (ex: `block_xp`, `format_trail`).

Lista padrão incluída:

| Suite | Componentes |
|---|---|
| Level UP XP | `block_xp`, `availability_xp`, `enrol_xp`, `local_xpstore` |
| Stash | `block_stash`, `availability_stash`, `filter_stash`, `tiny_stash` |
| Trail | `format_trail` |
| Moove | `theme_moove` |
| Learning Map | `mod_learningmap`, `format_learningmap` |
| Block Game | `block_game`, `availability_game` |
| Game | `mod_game` |
| TinyMCE plugins | `tiny_c4l`, `tiny_ai`, `tiny_fontcolor`, `tiny_fontsize`, `tiny_wordimport`, `tiny_multilang2`, `tiny_cloze` |
| Sharing Cart | `block_sharing_cart` |
| Completion Progress | `block_completion_progress` |

### Instalação via install.sh

O `install.sh` oferece a instalação separada deste monitor. Se confirmada,
copia o script e registra o cron às 6h15 (15 minutos após o monitor de novos plugins):

```
15 6 * * *  python3 ~/.moodle-dev-tools/plugins-watch.py
```

### Instalação manual

```bash
cp plugins-watch.py ~/.moodle-dev-tools/
chmod +x ~/.moodle-dev-tools/plugins-watch.py

# Inicializa o estado com as versões atuais (evita notificar releases antigas)
python3 ~/.moodle-dev-tools/plugins-watch.py  # roda uma vez para criar o state file

# Registra o cron
(crontab -l; echo "15 6 * * * /usr/bin/python3 $HOME/.moodle-dev-tools/plugins-watch.py >> $HOME/.moodle-plugins-monitor.log 2>&1") | crontab -
```

O estado é salvo em `~/.moodle-plugins-watch-state.json`.
