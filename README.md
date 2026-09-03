# moodle-dev-tools

Ferramentas de automação para desenvolvimento de plugins Moodle:

1. **php -l + PHPCS** — sintaxe e padrão Moodle, rodam localmente (~60ms), sem custo
2. **ESLint + Stylelint + lint Mustache** — gates determinísticos no pre-commit que espelham o CI (JS, CSS e templates)
3. **Revisão IA paralela** — múltiplos modelos em paralelo cobrem o que as ferramentas não detectam
4. **Geração de mensagem de commit** — IA gera o texto do commit a partir do diff; você revisa no editor
5. **Cobertura de testes** — `moodle-coverage`, mede a cobertura de testes de um plugin sob demanda
6. **Validação de schema** — `moodle-check-schema`, detecta drift entre o banco de dev e os `install.xml`
7. **Upgrade + validação** — `moodle-upgrade`, aplica upgrades nos containers configurados e valida o schema no fim
8. **Análise estática** — `moodle-phpstan`, PHPStan com a extensão Moodle (pega bugs de tipo/API)
9. **Auditoria de segurança** — `moodle-security-audit`, lê o plugin inteiro (determinístico + IA) e emite relatório com grade
10. **Monitor de novos plugins** — aviso diário via Telegram quando plugins são publicados no diretório oficial
11. **Monitor de updates de core** — `core-updates-watch.py`, aviso diário via Telegram quando um dos três containers locais tem atualização de core Moodle disponível
12. **Badge de downloads** — `moodle-marketplace-downloads`, gera o `docs/badges/downloads.json` a partir da página de stats do Marketplace (atualização mensal automatizada no companion privado)

> `moodle-mirror` (espelhamento de plugins entre containers) não está mais aqui — é específico
> demais da topologia de um ecossistema multi-versão pra generalizar, e vive no companion privado
> `moodle-dev-tools-private`.

---

## Hook 1 — pre-commit: php -l + PHPCS + ESLint + Stylelint + Mustache + revisão IA

O hook roda **gates determinísticos** (ferramentas locais, sem custo, sem IA) e, por fim, a
revisão IA. Cada gate só roda se houver arquivo do seu tipo no staging — um commit que mexe
só em PHP não dispara ESLint, Stylelint nem o lint Mustache.

### Gates determinísticos (php -l, PHPCS, capability-strings, get_string, capability-exists, template/module-names, duplicate-tables, ESLint, Stylelint, Mustache)

| Gate | Dispara com | O que faz | Bloqueia? |
|---|---|---|---|
| **php -l** | `.php` staged | Sintaxe. PHPCS/moodlecheck checam estilo/PHPDoc, não *parse* — um erro de sintaxe passaria batido até o CI (`moodle-plugin-ci phplint`) | Sim |
| **PHPCS** | `.php` staged | Padrão Moodle completo (~60ms por arquivo) | Sim |
| **capability-strings** | `db/access.php` ou `lang/en/*.php` staged | Toda capability em `db/access.php` tem string de lang correspondente (`mod/x:cap` → `$string['x:cap']`) | Sim |
| **get_string** | `.php` staged | Toda chamada `get_string('chave', 'componente')` com os dois argumentos literais aponta pra uma string que existe de verdade | Sim |
| **capability-exists** | `.php` staged | Toda chamada `has_capability()`/`require_capability()` com argumento literal referencia uma capability que existe de verdade | Sim |
| **ESLint** | `.js` staged | ESLint do Moodle com `--max-warnings 0` (espelha o `--max-lint-warnings 0` do CI) | Sim |
| **Stylelint** | `.css` staged | Stylelint com o `.stylelintrc` do Moodle (espelha o `grunt stylelint:css` do CI) | Sim |
| **Mustache** | `.mustache` staged | `@template` obrigatório; chaves `{{`/`}}` desbalanceadas | `@template` sim; chaves só avisam |
| **template/module-names** | `.mustache` ou `.js` staged | O valor de `@template`/`@module` bate com o caminho real do arquivo (`<component>/<subpath>`) | Sim |
| **duplicate-tables** | `db/install.xml` staged | Nenhum `<TABLE NAME>` repetido dentro do mesmo `install.xml` | Sim |
| **Aviso AMD** | `amd/src/*.js` staged | Lembra de rodar `npx grunt amd` se o `amd/build/*.min.js` correspondente não estiver staged | Não (só avisa) |

Notas:

- **ESLint** e **Stylelint** usam o binário e a config do Moodle (`.eslintrc`/`.stylelintrc`),
  localizados subindo a árvore a partir do repositório. Se o plugin não estiver montado sob uma
  árvore Moodle (sem o binário acessível), o lint correspondente é **pulado sem bloquear** — o
  hook é global e não pode quebrar commits de repositórios fora do ecossistema Moodle.
- **Stylelint** filtra do output os avisos "rule is deprecated" que o `.stylelintrc` do Moodle
  sempre imprime (regras antigas mantidas por compatibilidade, não erros do arquivo em si) —
  mantém a saída de falha focada nos problemas reais.
- **Mustache** faz um check leve, não o validador completo do `moodle-plugin-ci` (que valida
  HTML e contexto de exemplo). O `@template` ausente é o erro que mais quebra o CI; é o que o
  gate garante. A validação de HTML/contexto continua a cargo do CI.
- **capability-strings** (`check_capability_strings.py`) só roda se o repositório tiver
  `version.php` na raiz (identifica um plugin Moodle de verdade — não dispara em repos como
  este próprio `moodle-dev-tools`). Regra que já existia escrita no CLAUDE.md (item 19 do
  checklist de entrega), nunca automatizada antes — PHPCS/moodlecheck/PHPStan não sabem que
  `db/access.php` e `lang/en/*.php` deveriam concordar entre si. Zero falso-positivo testado
  contra os 110 plugins reais montados em `web-1` no dia em que foi criado (01/09/2026) — 6
  achados genuínos, todos no core (`mod_lti`, e mais 5 depois de um ajuste de regex — ver nota
  do `capability-exists` abaixo).
- **get_string** (`check_get_string.py`) resolve o próprio componente do plugin, `core`/`moodle`,
  e os tipos já conhecidos por este ecossistema (`mod`/`local`/`block`/`filter`/`report`/`format`/
  `availability`) via `MDT_MOODLE_PUBLIC`. Um componente desconhecido é **pulado, nunca
  chutado** — falso negativo é aceitável aqui, falso positivo bloqueando commit não é. Só conta
  como "literal" um argumento entre aspas simples, ou aspas duplas sem `$`/`{}` (string
  interpolada não é estática — `"rpg_{$tone}_nome"` vira outra coisa em runtime); qualquer coisa
  depois disso que não seja `,` ou `)` (ex.: concatenação, `'x_' . $var`) invalida o literal
  inteiro. Zero falso-positivo depois desse ajuste, testado contra ~10 plugins reais — achou 2
  bugs genuínos e inéditos no `block_playerhud` (publicado): `get_string('no_items_selected', ...)`
  e `get_string('validate_number', 'core')`, nenhuma das duas strings existe (01/09/2026, já
  corrigido).
- **capability-exists** (`check_capability_exists.py`) é o inverso do `capability-strings`:
  aquele valida as capabilities que o **próprio** plugin declara; este valida uma capability
  **referenciada** (de `has_capability()`/`require_capability()`), possivelmente de outro
  componente. Mesma resolução conservadora do `get_string` (próprio componente, `moodle` via
  `lib/db/access.php`, tipos conhecidos). Achado de bootstrap: a regex de chave de capability
  só cobria `=> [` — `lib/db/access.php` do próprio core ainda usa a sintaxe legada
  `=> array(` em toda parte, dando falso-positivo generalizado até a regex aceitar as duas
  formas (corrigido também no `capability-strings`, que usa a mesma regex). Depois do ajuste,
  zero falso-positivo nos 110 plugins + achou 5 capabilities `portfolioexport` do core
  (`mod_resource`/`folder`/`imscp`/`url`/`page`) sem string, além do `mod_lti` já achado antes.
- **template/module-names** (`check_template_module_names.py`) verifica só o *valor* da tag —
  a presença do `@template` já é gate à parte (acima). Nenhum check existente cobria `@module`
  de jeito nenhum (nem presença, nem valor). Convenção confirmada contra exemplos reais antes de
  escrever (nem template nem module derrubam o prefixo "mod_", diferente do arquivo de lang):
  `mod_playervideo/attempt_summary`, `core_contentbank/bankcontent/navigation`,
  `core_group/comboboxsearch/group`. Zero falso-positivo testado contra 11 plugins do
  ecossistema + uma amostra do core — achou 1 problema cosmético genuíno em `mod_assign`
  (`@module` com um backtick perdido no valor, sem efeito funcional).
- **duplicate-tables** (`check_duplicate_tables.py`) é o mais simples dos cinco — autocontido,
  um `install.xml` comparado só contra ele mesmo, sem resolver componente nenhum. Usa parser de
  XML de verdade (`xml.etree.ElementTree`), não regex, já que `install.xml` é XML de verdade.
  Zero achado nos 110 plugins reais (esperado — é um erro raro), mas o teste com um caso
  quebrado de propósito confirma que pega quando existe.
- Achado real: `mod_playervideo` teve dois commits seguidos quebrarem o CI (`stylelint:css`, a
  leg `--all` do `moodle-plugin-ci grunt`) por regras CSS de uma linha só, sem que o hook local
  acusasse nada — motivou a criação deste gate (01/09/2026).
- Os gates determinísticos **não podem ser pulados** — só a revisão IA aceita `SKIP_AI=1`.

### O que a IA revisa

A revisão cobre **PHP, JS (AMD), Mustache, CSS e XML** — todos os tipos de arquivo de um plugin Moodle. Arquivos minificados (`amd/build/`) são ignorados.

Arquivos dentro de um `docs/` na raiz do repositório também ficam de fora **só da revisão IA**:
esse diretório hospeda o site de documentação de cada plugin no GitHub Pages (Jekyll, CSS, JS,
imagens — conteúdo estático, não código Moodle), e as regras da IA partem do pressuposto de que
o diff é código de plugin (escopo de CSS por `.path-*`, PHPDoc, AMD, etc.), o que gera falso
positivo ali. Os gates determinísticos (PHPCS, ESLint, Stylelint, Mustache) continuam rodando
normalmente em `docs/` quando há arquivo do tipo certo staged — por exemplo, o ESLint ainda pega erro de
estilo num `docs/assets/js/*.js`. Caso de origem: `moodle-mod_playerwords`, commit que adicionou
o site de documentação do plugin.

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

#### Todos os tipos de arquivo

| # | Regra |
|---|---|
| 40 | Comentário escrito em português em qualquer arquivo (`// ...`, `/* ... */`, `{{! ... }}`) — todos os comentários devem estar em inglês; sinalizado apenas quando for claramente prosa em português, não palavras isoladas ou nomes próprios |

### Fluxo

```
git commit
    │
    ▼ (só se há .php staged)
PHPCS (local, ~60ms)
    ├── erros → bloqueia
    └── OK
         │
         ▼ (só se há .js staged)
    ESLint (local, --max-warnings 0)  +  aviso de build AMD dessincronizado
    │    ├── erros → bloqueia
    │    └── OK
         │
         ▼ (só se há .css staged)
    Stylelint (local, .stylelintrc do Moodle)
    │    ├── erros → bloqueia
    │    └── OK
         │
         ▼ (só se há .mustache staged)
    Mustache (local: @template obrigatório)
    │    ├── @template ausente → bloqueia
    │    └── OK
         │
         ▼ (PHP + JS + Mustache + CSS + XML, exclui amd/build/ e docs/)
    IAs em paralelo (~5–15s)
    Gemini, Groq, OpenAI-compatible (até 5 slots), Claude CLI (assinatura)
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

Se uma IA falhar (rate limit, cota, timeout) **ou retornar fora do formato** (1ª linha não é `APROVADO` nem `BLOQUEADO` — o modelo se perdeu na tarefa), o problema é exibido no terminal e ela é ignorada, nunca contada como aprovação. O commit só é bloqueado por resposta explícita `BLOQUEADO`.

**Pular a revisão IA** (falso positivo confirmado):

```bash
SKIP_AI=1 git commit -m "mensagem"
```

Os gates determinísticos (PHPCS, ESLint, Stylelint, Mustache) não podem ser pulados —
`SKIP_AI=1` afeta apenas a revisão IA.

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

A IA tenta os providers em sequência, usando o primeiro que responder, na ordem do `~/.phpcs-ai.env`:

1. Gemini (gratuito — tentado primeiro; se falhar, passa adiante)
2. Groq
3. Slots OpenAI-compatible (`OPENAI_*` a `OPENAI5_*`)

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
- Cria o symlink `~/.local/bin/moodle-coverage` → `coverage.sh`
- Configura `git config --global core.hooksPath ~/.githooks`
- Cria `~/.phpcs-ai.env` a partir do template (se ainda não existir)
- Cria `~/.moodle-dev-tools.env` a partir do template (se ainda não existir)
- Pergunta se deseja instalar o monitor de plugins (opcional)

## Configuração de ambiente (containers, paths)

`coverage.sh`, `check-schema.sh`, `upgrade.sh`, `phpstan.sh`, `scope-audit.sh`,
`security-audit.sh` e `query-baseline.sh` precisam saber os nomes dos containers Docker
e os paths do Moodle no host. Esses valores vêm de `~/.moodle-dev-tools.env` (não é
segredo — nomes de container e paths, não chaves de API, que continuam em
`~/.phpcs-ai.env`):

```bash
MDT_MOODLE_ROOT=/path/to/project
MDT_MOODLE_HTML=/path/to/project/html
MDT_MOODLE_PUBLIC=/path/to/project/html/public
MDT_CONTAINER_51=moodle-web-51
MDT_CONTAINER_45=moodle-web-45
MDT_CONTAINER_52=moodle-web-52
```

Sem esse arquivo, cada script usa os valores padrão da máquina de desenvolvimento
original — funciona, mas aponta para os nomes/paths errados em outro ambiente. Os
nomes das variáveis (`_51`/`_45`/`_52`) são só um rótulo para "os três alvos que este
conjunto de scripts assume" — não precisam ser exatamente essas versões do Moodle.

### Alvos de `moodle-check-schema` e `moodle-upgrade`

Esses dois aceitam um número arbitrário de alvos, não só 51/45/52. Por padrão constroem
a lista a partir das 3 variáveis acima; pra um esquema totalmente diferente (outro
número de containers, outros rótulos), defina `MDT_TARGETS` direto:

```bash
MDT_TARGETS="dev:moodle-web-dev staging:moodle-web-staging"
```

Cada entrada é `rótulo:container`, separadas por espaço. `moodle-check-schema staging` /
`moodle-upgrade staging` passam a mirar nesse container; `all` continua expandindo pra
todos os rótulos da lista.

## Configuração das chaves de API

Edite `~/.phpcs-ai.env` e preencha as chaves que tiver:

```bash
# Google Gemini
GEMINI_KEY=sua-chave

# Groq
GROQ_KEY=sua-chave
GROQ_MODEL=openai/gpt-oss-120b

# OpenAI-compatible (OpenRouter, NVIDIA NIM, OpenAI, etc.)
OPENAI_KEY=sua-chave
OPENAI_URL=https://openrouter.ai/api/v1/chat/completions
OPENAI_MODEL=deepseek/deepseek-v4-flash
```

Slots de `OPENAI` a `OPENAI5` são suportados. Basta adicionar `OPENAI2_KEY`, `OPENAI2_URL`, `OPENAI2_MODEL` e assim por diante.

O arquivo `~/.phpcs-ai.env.example` tem o template completo com comentários.

### Claude CLI como provider (assinatura, não API paga)

Além dos providers via API acima, o hook ativa automaticamente um provider extra se o
binário `claude` (Claude Code) estiver no `PATH` e logado na máquina que faz o commit —
sem precisar de nenhuma chave em `~/.phpcs-ai.env`. A chamada roda em modo headless
(`claude -p`) e é forçada a usar a **assinatura** (Pro/Max) em vez de uma API key: o
processo filho tem `ANTHROPIC_API_KEY` (e as flags de Bedrock/Vertex) removidas do
ambiente antes de rodar, mesmo que essas variáveis existam na sua shell.

Por padrão tenta `claude-fable-5`; se essa chamada falhar por qualquer motivo (ex.:
créditos do Fable esgotados no momento), cai automaticamente para `claude-opus-5` — a
segunda chamada continua contra a assinatura, nunca vira cobrança por token.

```bash
# ~/.phpcs-ai.env — todas opcionais, já vêm com esses padrões
CLAUDE_CLI_MODEL=claude-fable-5
CLAUDE_CLI_FALLBACK_MODEL=claude-opus-5
SKIP_CLAUDE_CLI=1   # desativa esse provider mesmo com o binário disponível
```

Como cada commit passa a gastar cota de uso da assinatura (limite de 5h/semanal do
Claude Code), avalie se vale manter ativo em máquinas onde você comita com muita
frequência — `SKIP_CLAUDE_CLI=1` desliga só esse provider, os demais (Gemini/Groq/
OpenAI-compatible) continuam normalmente.

### Modelos gratuitos testados e aprovados

| Provider | Modelo | Observação |
|---|---|---|
| Groq | `openai/gpt-oss-120b` | Substituto oficial do `llama-3.3-70b-versatile` (ver nota de depreciação abaixo) |
| OpenRouter | `deepseek/deepseek-v4-flash` | Boa relação custo/qualidade |
| OpenRouter | `openai/gpt-oss-120b:free` | Formato de resposta excelente |
| NVIDIA NIM | `meta/llama-3.3-70b-instruct` | Gratuito com conta NVIDIA |

### Depreciação do `llama-3.3-70b-versatile` na Groq

A Groq depreciou o `llama-3.3-70b-versatile` e decomissiona o modelo em **16/08/2026**; o
substituto oficial recomendado é o `openai/gpt-oss-120b`. Basta atualizar `GROQ_MODEL` em
`~/.phpcs-ai.env` — nenhum outro ajuste é necessário, o nome do modelo é passado direto para
o payload da API sem validação de allow-list.

**Se a revisão IA passar a falhar com `[Groq / ...] falhou: ERRO: HTTP 403: HTTP Error 403:
Forbidden` logo após essa troca, não é o nome do modelo.** Um teste direto via `curl` com a
mesma chave/modelo retornava 200; o mesmo request feito pelo `phpcs-ai-call.py` (Python
`urllib`) retornava 403. A causa era o `User-Agent` padrão do `urllib`
(`Python-urllib/3.x`), que a Groq (ou o WAF/Cloudflare na frente dela) rejeita como tráfego de
bot — independente de modelo, chave ou conta. O script agora envia um `User-Agent` explícito
(`moodle-dev-tools-phpcs-ai-call/1.0`) em toda chamada HTTP, o que resolve o 403. Se voltar a
acontecer, teste isolado com:

```bash
source ~/.phpcs-ai.env
echo "responda apenas: ok" | python3 ~/.moodle-dev-tools/phpcs-ai-call.py \
  "groq" "$GROQ_KEY" "https://api.groq.com/openai/v1/chat/completions" "$GROQ_MODEL"
```

## Estrutura dos arquivos instalados

```
~/.githooks/
├── pre-commit              ← symlink → php -l + PHPCS + ESLint + Stylelint + Mustache + IA a cada commit
└── prepare-commit-msg      ← symlink → geração de mensagem de commit com IA

~/.local/bin/
├── moodle-coverage         ← symlink → coverage.sh (cobertura de testes por plugin)
├── moodle-check-schema     ← symlink → check-schema.sh (drift de schema vs install.xml)
├── moodle-upgrade          ← symlink → upgrade.sh (upgrade nos containers configurados + check de schema)
├── moodle-phpstan          ← symlink → phpstan.sh (análise estática com extensão Moodle)
├── moodle-scope-audit      ← symlink → scope-audit.sh (§6 do SCOPE.md vs disco)
└── moodle-security-audit   ← symlink → security-audit.sh (auditoria de segurança determinística + IA)

~/.moodle-dev-tools/
├── phpcs-ai-call.py        ← caller Python (Gemini + OpenAI-compatible)
├── phpcs-bootstrap.php     ← fix de compatibilidade PHP 8.3 / phpcsutils
├── plugins-monitor.py      ← monitor de novos plugins (opcional)
└── plugins-watch.py        ← monitor de atualizações de plugins específicos (opcional)

~/.phpcs-ai.env             ← suas chaves de API (chmod 600, nunca commitar)
~/.moodle-dev-tools.env     ← nomes de container e paths (não é segredo)
```

`moodle-mirror` não tem symlink criado pelo `install.sh` — vem de
[moodle-dev-tools-private](https://github.com/jeanlucio/moodle-dev-tools-private), e o symlink
é manual.

---

## Cobertura de testes — `moodle-coverage`

Mede a cobertura de testes de **um** plugin Moodle de forma repetível, dentro do container de
desenvolvimento (com Xdebug). Substitui o processo manual de montar um `phpunit.xml` à mão,
rodar com `XDEBUG_MODE=coverage` e limpar depois. O `install.sh` cria o symlink
`~/.local/bin/moodle-coverage`.

```bash
moodle-coverage <tipo/nome> [--html] [--filter <subpath>]
```

| Exemplo | Efeito |
|---|---|
| `moodle-coverage blocks/playerhud` | Tabela de cobertura por classe no terminal |
| `moodle-coverage local/playergames --html` | Gera também relatório navegável em `~/coverage-reports/<frankenstyle>/` |
| `moodle-coverage blocks/playerhud --filter classes/controller` | Escopa a medição a uma subpasta |

O script recebe só o `tipo/nome` (ex.: `blocks/playerhud`) e deriva o resto — frankenstyle,
`classes/`, `tests/` — montando o `phpunit.xml` temporário escopado ao plugin. Aceita também o
caminho do host (`html/public/blocks/playerhud`); o prefixo é removido.

### Pré-requisitos e notas de ambiente

- Roda no container de desenvolvimento (`meu-moodle-web-1` por padrão, no topo do script) com
  **Xdebug** disponível e o ambiente PHPUnit inicializado (`admin/tool/phpunit/cli/init.php`).
- Usa `memory_limit=-1`: a instrumentação de cobertura do Xdebug consome muito mais memória
  que uma rodada normal, e o teto padrão do CLI faz suítes grandes **segfaultar**.
- `--filter` ajusta o `<source>` do `phpunit.xml` (não a flag `--coverage-filter`, que em
  PHPUnit 10+ apenas soma ao include em vez de restringir).
- É **ferramenta de bancada**: não vai no ZIP do Plugin Directory e não altera o código-fonte.
  A medição completa mesmo quando a suíte reporta warnings/deprecations inofensivas (ex.:
  doc-comment metadata em plugins 4.5+5.0); uma nota final separa "medição-ok-com-avisos" de
  falha real de teste.

---

## Validação de schema — `moodle-check-schema`

Valida o schema físico do banco contra os `install.xml` e mostra **só as divergências dos seus
plugins** (filtra o ruído do core e de plugins de terceiros). Roda o
`admin/cli/check_database_schema.php` nativo do Moodle dentro do container de dev — onde o site
de produção (`mdl_`) está instalado.

```bash
moodle-check-schema [target] [--all]
```

| Argumento | Efeito |
|---|---|
| (nenhum) | web-1 (Moodle 5.1), só os seus plugins |
| `45` / `52` | web45 / web52 |
| `all` | os três containers |
| `--all` | mostra **todas** as divergências (core e terceiros), não só as suas |

Os rótulos `45`/`52`/`all` vêm de `~/.moodle-dev-tools.env` (veja
[Alvos de moodle-check-schema e moodle-upgrade](#alvos-de-moodle-check-schema-e-moodle-upgrade))
— em outra máquina, com outro conjunto de containers, os rótulos podem ser outros.

Serve para pegar **drift do banco de desenvolvimento**: quando o `install.xml` evolui e o banco
local não acompanha (faltou reinstalar o plugin ou um passo de `upgrade.php`). Os prefixos de
tabela dos seus plugins são derivados automaticamente dos diretórios com repositório `.git`.
Sai com código 1 se houver divergência (serve de gate antes de publicar).

> **Por que não no CI:** o `moodle-plugin-ci` só prepara os ambientes de teste (`phpu_`/`bht_`)
> e nunca instala o site `mdl_`, então `check_database_schema.php` aborta com "Database is not
> yet installed". É, por construção, uma ferramenta local — e é por isso que o template oficial
> do Moodle HQ não a inclui.

---

## Upgrade + validação — `moodle-upgrade`

Acopla "aplicar upgrade" e "validar schema" numa operação atômica — o check de schema vem
sempre junto, impossível esquecer. É o fluxo para testar um `db/upgrade.php` após bumpar o
`version.php` de um plugin.

```bash
moodle-upgrade [target|all]   # padrão: all — rótulos vêm de ~/.moodle-dev-tools.env
```

Para cada container do alvo: roda `admin/cli/upgrade.php`, purga os caches, e no fim dispara o
`moodle-check-schema`. Rodar nos três (`all`) valida o `upgrade.php` em **4.5, 5.1 e 5.2** de
uma vez. O `--allow-unstable` é aplicado como **fallback automático** apenas se o container
estiver em versão beta/dev (e avisa quando isso ocorre — sinal de que aquele Moodle precisa ser
atualizado). Sai com código != 0 se algum upgrade falhar ou o schema divergir.

---

## Análise estática — `moodle-phpstan`

Roda o [PHPStan](https://phpstan.org/) num plugin, com a extensão
[`micaherne/phpstan-moodle`](https://github.com/micaherne/phpstan-moodle) que ensina o
analisador sobre as classes do core e seus aliases legacy. Pega bugs que o PHPCS (estilo) e o
moodlecheck (PHPDoc) não veem: chamada a método/função **inexistente**, tipo errado de
argumento/retorno, acesso a propriedade de algo que pode ser `null`, código morto.

```bash
moodle-phpstan <tipo/nome> [--level N] [--path <subdir>]
```

Por padrão analisa `classes/` + as libs de topo (`lib.php`, etc.), no **nível 2**. Níveis altos
geram ruído no Moodle (`stdClass`/`mixed`) — subir só quando valer.

**Especialmente útil para revisar código gerado por IA:** o erro mais característico da IA é
"alucinar" uma API — inventar um método plausível que não existe. O PHPStan acusa isso de forma
**determinística**, complementando a revisão IA do pre-commit (que é probabilística).

A extensão Moodle é essencial: sem ela, o `scanDirectories` puro descobre as classes do core de
forma inconsistente e o nível 2 afoga em falsos positivos de aliases (`cm_info` etc.). A
extensão bootstrapa o classloader do Moodle a partir de `moodle.rootDirectory` (a raiz com
`lib/components.json` + `vendor/`, que na estrutura `public/` do Moodle 5.x é um nível **acima**
do docroot). O PHPStan e a extensão vivem num projeto Composer isolado em `phpstan/` — não tocam
o Moodle nem os containers. Roda no host (PHP do host, analisando como PHP 8.2).

---

## Auditoria de escopo — `moodle-scope-audit`

Faz o diff mecânico entre a árvore de arquivos do §6 de um `SCOPE.md` e o que realmente existe
no repositório do plugin. Nasceu de um incidente real: o `SCOPE.md` do `mod_playercross`
previa `managewords.php`, `ranking.php`, `ranking_service.php` e `ai_word_generator.php` desde
a Fase 3, mas cinco fases inteiras foram marcadas como concluídas sem nenhum desses arquivos
existir — a verificação de "fase concluída" tinha sido feita contra um checklist reconstruído
de memória a partir do critério de aceite em prosa, não contra a árvore literal do §6.

```bash
moodle-scope-audit <tipo/nome> [--scope caminho/para/SCOPE.md]
```

Lê a árvore ASCII (`├──`/`└──`/`│`) dentro do bloco de código do §6, reconstrói o caminho
relativo de cada entrada a partir da indentação, expande listas em chaves (`{a,b,c}_test.php`,
usadas para compactar vários arquivos de teste parecidos numa linha só — inclusive quando essa
lista foi quebrada em várias linhas físicas para caber no limite de 132 colunas) e confere cada
caminho contra o disco. Roda ao final de cada fase do `§16` como parte do Definition of Done —
não substitui a verificação de que o *comportamento* funciona (PHPUnit, Playwright), só garante
que nenhum arquivo planejado foi esquecido silenciosamente.

Saída limpa esperada no fim do desenvolvimento: apenas `CHANGES.md`, `README.md` e
`COPYING.txt` (artefatos da Fase 6/release, que o próprio `TEMPLATE_SCOPE.md` já documenta como
corretamente vazios/ausentes até a primeira tag).

---

## Auditoria de segurança — `moodle-security-audit`

Lê o plugin **inteiro** procurando vulnerabilidades e emite um relatório com grade, achados
por severidade e correção recomendada. Complementa o pre-commit, que revisa **diffs**.

A diferença não é de grau, é de natureza: um achado como "esta variável recebe
`format_string()` e a irmã ao lado, no mesmo `if`, não recebe" é invisível para revisão de
diff — o diff pode nem conter as duas linhas, e mesmo que contenha, julgar exige seguir a
cadeia de chamada até o template que renderiza o valor. Foi exatamente esse buraco que deixou
passar um bug real no `filter_playerhud` que os 5 providers do pre-commit não pegaram.

```bash
moodle-security-audit <tipo/nome> [opções]
```

Relatório em **`<plugin>/.plans/security-audit/<frankenstyle>-<AAAA-MM-DD>-<HHMMSS>.md`** — junto do
código que descreve. A ferramenta usa apenas ferramentas de leitura sobre o código: é
estruturalmente incapaz de alterar o plugin auditado.

`.plans/` é a mesma pasta que o ecossistema já usa para arquivos de trabalho de assistente
de IA — os relatórios ficam numa subpasta própria (`security-audit/`) dentro dela, mas é a
pasta `.plans/` inteira que é criada se não existir e **garantidamente entra no
`.gitignore` do plugin** — a ferramenta confere a cada rodada, cria o `.gitignore` se faltar
e acrescenta a entrada se estiver ausente. Isso não é conveniência: um relatório que aponta
linha e prova de conceito
de vulnerabilidade não pode virar arquivo versionado num repositório público.

### Pipeline

| Fase | O que faz |
|---|---|
| **A** | Coleta determinística: PHPStan em nível alto, versões de libs empacotadas, drift de schema |
| **B** | **Triagem por IA** das mensagens do PHPStan: `real_bug` / `security_relevant` / `moodle_idiom_noise` |
| **C** | Varredura semântica por IA, em lotes, com o agente lendo o código por conta própria |
| **D** | Verificação: cada candidato precisa ser confirmado explorável ou é descartado |
| **E** | Dedup por IA: consolida achados confirmados que descrevem a mesma causa raiz — os lotes da Fase C e as verificações da Fase D rodam isolados uns dos outros, então a mesma vulnerabilidade vista por ângulos de código diferentes pode sobreviver como dois achados |
| **F** | Grade determinística + relatório Markdown |

### Por que o PHPStan roda no nível 6 aqui e no nível 2 no `moodle-phpstan`

Porque a Fase B muda a economia. Medido no `filter_playerhud` (3.718 linhas): nível 2 → 2
mensagens, nível 5 → 6, **nível 8 → 54**. Cerca de 75% do nível 8 é ruído genérico de idioma
Moodle (`type has no value type specified in iterable type array`), mas o resto é sinal sério
— `Cannot access property $id on core\context\course|false`, `Strict comparison ... will
always evaluate to false`, retorno faltando. Sem triagem isso é impraticável na mão; com
triagem, o nível alto vira utilizável pela primeira vez.

Identificadores puramente de PHPDoc/generics (`missingType.*`) são descartados
**deterministicamente**, antes de qualquer chamada de IA — não faz sentido gastar cota para
rejeitar um por um. Todo o resto vai para triagem, então o filtro permanece conservador.

### Regras verificadas

Catálogo em [`security-rules.md`](security-rules.md) — editar esse arquivo é como se ajusta a
auditoria. Três camadas:

1. **Guia oficial do Moodle** ([policies/security](https://moodledev.io/general/development/policies/security)) — as 15 categorias
   oficiais de vulnerabilidade são o vocabulário fechado do campo `category` de todo achado,
   mais as regras verificáveis do "Summary of the guidelines". Quatro categorias
   (`brute_forcing_login`, `insecure_config_management`, `buffer_overruns`,
   `social_engineering`) são de escopo de site e só são reportadas se o plugin implementar
   aquilo por conta própria.
2. **Regras específicas de plugin** — isolamento por `instanceid`, triple-mustache em campo
   armazenado, cleanup em `delete_instance`/`course_deleted`, column drift de backup e
   privacy, saída de IA como entrada não-confiável.
3. **Superfície deste ecossistema** — SSRF em chamadas de IA, condição de corrida em
   economia/quest, aleatoriedade insegura em tokens, path traversal.

### Grade

A nota é **dominada pelo pior achado**, não por soma de penalidades:

| Pior achado presente | Nota |
|---|---|
| `critical` | **F** |
| `high` | **D** |
| `medium` | **C** |
| 3+ `low` | **B+** |
| 1–2 `low` | **A** |
| nenhum (ou só `info`) | **A+** |

**Só achados de segurança contam** — bugs de código triados do PHPStan vão numa seção
separada e não afetam a nota.

O modelo aditivo original (100 − penalidades) foi abandonado porque distorce um relatório de
segurança: ele diz que oito falhas de higiene são piores que um XSS armazenado. Um relatório
com um `high` não é "quase tudo bem" — o achado principal define a nota.

A curva foi calibrada contra três relatórios publicados do MDL Shield e os reproduz
exatamente: `block_playerhud` (2 low + 1 info → A), `local_information_center` (8 low + 1
info → B+) e `filter_playerhud` (1 high + 1 medium + 1 low + 1 info → D). Repare que oito
`low` mal movem a nota, enquanto um único `high` despenca para D — é essa assimetria que a
fórmula aditiva não conseguia expressar.

### Opções

| Flag | Padrão | Efeito |
|---|---|---|
| `--model` | `claude-fable-5` | Modelo primário |
| `--fallback-model` | `claude-opus-5` | Usado se o primário falhar (ex.: créditos do Fable esgotados) |
| `--phpstan-level N` | `6` | Nível do PHPStan |
| `--batch-lines N` | `10000` | Orçamento de linhas por lote |
| `--jobs N` | `5` | Chamadas de IA em paralelo |
| `--with-moodlecheck` | — | Roda também o `local_moodlecheck` (PHPDoc; release, não segurança) |
| `--no-verify` | — | Pula a Fase D (mais rápido, mais falso positivo) |
| `--no-cache` | — | Ignora o cache de lotes |
| `--json` | — | Grava também o relatório em JSON |
| `--from-json ARQ` | — | Re-renderiza o relatório de um JSON já gerado, sem refazer a análise |

### Estrutura do relatório

Segue a forma de um relatório de revisão de segurança profissional, na ordem em que se lê:

1. **Nota geral** — grade em letra, pontuação e tabela por severidade
2. **Sumário executivo** — postura de segurança e o que os achados significam na prática
3. **Metodologia** — escopo (arquivos/linhas), o que foi examinado, superfície de ataque,
   evidências de rigor (Privacy API, capabilities, backup, testes) e dependências de terceiro
4. **Achados** — cada um com severidade/categoria/regra/quem explora, locais afetados,
   **trecho do código**, descrição, avaliação de impacto, mitigações já presentes, prova de
   conceito e correção recomendada. Quando a Fase E consolida dois ou mais achados
   independentes na mesma causa raiz, o resultado traz uma nota "Consolidado" explicando
   por quê, e os locais afetados de todos os achados originais aparecem juntos
5. **Pontos fortes de segurança** — práticas defensivas verificadas no código
6. **Bugs de código (PHPStan triado)** — separados, não afetam a nota
7. **Descartados na verificação** — candidatos refutados, para transparência e calibragem
8. **Conclusão**

O trecho de código é extraído **do arquivo em disco** pelo Python (linha do achado ± 3 de
contexto, com `>` marcando a linha), não copiado pelo modelo — assim nunca diverge do que
está realmente lá. `--from-json` re-renderiza tudo isso sem refazer a análise, o que torna
barato ajustar o modelo do relatório.

### Custo e cache

Roda contra a **assinatura** Claude Code, nunca a API paga: `ANTHROPIC_API_KEY` e as flags de
Bedrock/Vertex são removidas do ambiente do processo filho, e cada chamada usa `--safe-mode`
(pula `CLAUDE.md`/skills/hooks do usuário — as regras que importam já vão explícitas via
`--append-system-prompt`, então carregar tudo de novo em toda chamada só custava tokens à
toa). Consome cota de forma relevante — um plugin de 70 mil linhas dá ~8-10 lotes de
varredura, mais a triagem do PHPStan e uma verificação por achado candidato. A Fase E
(dedup) soma **uma única chamada por rodada**, não uma por achado — roda sobre a lista já
pequena de achados confirmados, então o custo não cresce com o tamanho do plugin.

O cache em `~/.moodle-security-audit-cache/<frankenstyle>/` cobre **as quatro fases de IA**
(triagem, varredura, verificação e dedup), cada uma chaveada pelo hash do que decide seu
resultado (conteúdo dos arquivos do lote, o achado sendo verificado, ou a lista de achados
confirmados sendo deduplicada). Isso tem dois efeitos: re-rodar
depois de corrigir um arquivo só reprocessa o que mudou, e **uma rodada interrompida no meio
retoma de onde parou** — se a cota acabar na Fase D, rodar o mesmo comando de novo pula A, B e
C inteiras. Uma verificação que falhou por erro transitório (limite de cota, rede) não entra
no cache — a próxima rodada tenta de novo, em vez de herdar um "refutado" que na verdade foi
uma falha de infraestrutura.

### Progresso

Cada fase de IA imprime uma linha por item **conforme ele termina** (não na ordem em que foi
disparado — um lote lento não deixa os outros "mudos" até terminar), com tempo decorrido e uma
estimativa de tempo restante calculada pela vazão observada até ali. A estimativa é pouco
confiável na primeira conclusão de cada fase e se corrige sozinha a partir da segunda. O tempo
total da auditoria aparece ao final.

### O que fica de fora, e por quê

PHPCS, ESLint, stylelint e gherkinlint estão disponíveis nesta máquina mas **não** entram: já
rodam no pre-commit/CI, o plugin que comita já passa neles, e praticamente não têm falso
positivo a triar. Incluí-los viraria "rode todo linter do mundo" e diluiria o relatório. O
critério de inclusão é: a ferramenta produz sinal de segurança, **ou** produz sinal que
precisa de triagem por IA para ser usável.

Validador Mustache completo não está instalado (só existe no `moodle-plugin-ci`); o
pre-commit faz o check leve de `@template` + chaves balanceadas.

---

## Regressão de queries por teste — `moodle-query-baseline`

Complementa o `moodle-security-audit`: a Fase C dele só **adivinha** N+1 lendo código (uma
query dentro de um loop pode disparar 3 vezes, inofensivo, ou 300, real) — este mede de
verdade. Roda a suíte PHPUnit que o plugin **já tem**, sem alterar nenhum teste, instrumentada
para contar `$DB->perf_get_queries()` por teste, e compara contra um baseline salvo. O valor
não está em olhar o número isolado (setUp()/fixture já custam queries, o total absoluto não
diz nada sozinho) — está em **detectar regressão**: um teste que sempre disparou 4 queries e
passa a disparar 40.

```bash
moodle-query-baseline <tipo/nome> [opções]
```

### Mecanismo

A contagem vem de `query_count_extension.php`, uma extensão PHPUnit 11
(`PHPUnit\Runner\Extension\Extension`) registrada via `$facade->registerSubscriber(...)` — o
mesmo mecanismo que o próprio Moodle usa em
`lib/tests/classes/phpunit/moodle_extension.php`. Ela tira um snapshot de
`perf_get_queries()` logo após `Prepared` (já passou do `setUp()`) e outro em `Finished`,
grava o delta por teste num JSON ao final da suíte inteira (`TestRunner\Finished`). Como o
XSD do `<extensions><bootstrap>` só aceita atributo `class` (sem `file` — PHPUnit nunca dá
`require` na classe, só `class_exists()`), o próprio arquivo da extensão faz o `require_once`
do bootstrap do Moodle no topo e é apontado como o `bootstrap=` raiz do `phpunit.xml`
temporário — um arquivo faz as duas coisas.

Cada rodada executa a suíte **duas vezes** e descarta a primeira: a primeira execução depois
de `phpunit init` mostra um aumento fixo de +6 queries em boa parte dos testes comparado à
segunda (artefato de aquecimento de cache, não instabilidade) — a segunda e a terceira rodada
batem exatamente. Sem esse descarte, todo plugin apareceria com falsos suspeitos na primeira
vez que a ferramenta rodasse nele.

### Pipeline

| Fase | O que faz |
|---|---|
| **A** | Instrumentação determinística: roda a suíte (aquecimento + medição), sem IA |
| **B** | Diff determinístico contra o baseline salvo: `novo` / `sem_mudança` / `suspeito` |
| **C** | Triagem por IA — só dos testes que a Fase B marcou `suspeito`, não do plugin inteiro |
| **D** | Relatório com a tabela completa + atualização opcional do baseline |

Um teste só vira `suspeito` quando o aumento passa **os dois** filtros ao mesmo tempo:
`--threshold-pct` (percentual) **e** `--min-queries` (valor absoluto) — evita ruído de teste
pequeno (2→3 queries já é +50%, mas irrelevante).

### O baseline nunca muda sozinho

`~/.moodle-query-baseline/<frankenstyle>.json` só é escrito com `--accept` explícito, depois
de você olhar o relatório. Sem a flag, a rodada é sempre só leitura — inclusive a primeira vez
que a ferramenta roda num plugin sem baseline nenhum ("novo" para todos, nada para comparar
ainda). Esse é o ponto central do desenho: uma regressão lenta (+1 query por commit) nunca
dispara alerta se o baseline se atualiza sozinho a cada rodada.

### Opções

| Flag | Padrão | Efeito |
|---|---|---|
| `--min-queries N` | `10` | Ignora testes abaixo disso, mesmo com % alto |
| `--threshold-pct N` | `50` | % de aumento para marcar como suspeito |
| `--no-triage` | — | Só a tabela de números/deltas, sem gastar cota da assinatura |
| `--accept` | — | Grava os números desta rodada como novo baseline |
| `--model` | `claude-fable-5` | Modelo primário da triagem |
| `--fallback-model` | `claude-opus-5` | Usado se o primário falhar |
| `--jobs N` | `5` | Chamadas de triagem em paralelo |
| `--no-cache` | — | Ignora o cache de triagem |

### Relatório e cache

Relatório em `<plugin>/.plans/query-baseline/<frankenstyle>-<AAAA-MM-DD>-<HHMMSS>.md` — mesma
pasta `.plans/` (com `.gitignore` garantido) do `moodle-security-audit`, subpasta própria. A
tabela completa lista **todo** teste, não só os suspeitos; a seção de triagem só aparece
quando há suspeito, com o veredito (`n_plus_one_provavel` / `esperado` / `indeterminado`) e a
justificativa citando o código lido pela IA. Cache de triagem em
`~/.moodle-query-baseline-cache/`, reaproveitando a mesma infraestrutura de `claude_cli.py`
usada pelo `moodle-security-audit` (mesmo `--safe-mode`, mesma remoção de
`ANTHROPIC_API_KEY` do ambiente do processo filho).

A ferramenta só lê o plugin auditado — a suíte roda no container, sem tocar o repositório — e
não faz parte do ZIP do Plugin Directory, é ferramenta de bancada como as demais desta seção.

---

## Badge de downloads — `moodle-marketplace-downloads`

Gera o `docs/badges/downloads.json` de um plugin (schema *endpoint* da shields.io) somando a
tabela mensal de **12 meses fechados** da página `/stats` do Moodle Marketplace — o mês
corrente nunca entra. O card do plugin no Marketplace só mostra "últimos 90 dias" e a única
API de downloads (`local_plugins_get_maintained_plugins`) exige token de mantenedor, então o
número sai por scraping da própria página (sem login), ancorado no `id` da tabela
(`stats-downloads-monthly-table`), não em posição.

```bash
moodle-marketplace-downloads <tipo/nome> <id-ou-frankenstyle>
# ex.: moodle-marketplace-downloads blocks/playerhud 3583
# ex.: moodle-marketplace-downloads blocks/playerhud block_playerhud
```

O 2º argumento pode ser o id numérico da URL (`marketplace.moodle.com/plugins/<id>/stats`) ou
o nome Frankenstyle — a URL `/stats` aceita os dois. **Não commita nem dá push**: só escreve o
arquivo, revisar e commitar é passo separado. Exige que o plugin já tenha `docs/` (site GitHub
Pages); `docs/` já é `export-ignore` no `.gitattributes`, então o JSON não vai pro ZIP do
Plugin Directory.

A badge não é dinâmica — a shields.io lê o JSON commitado em tempo real, mas o valor só muda
quando alguém roda o gerador de novo e commita. Para não congelar, o companion privado
`moodle-dev-tools-private` tem um `marketplace_downloads_watchdog.py` que, nos primeiros dias
de cada mês (cron `0 6 1-4 * *`), roda o gerador para **todos** os plugins que já têm a badge,
commita e dá push num commit por plugin, e só avisa no Telegram quando algo mudou ou deu erro.

---

## Ferramentas de manutenção de código

### sortlang.php — ordenação de strings de idioma

Ordena as chaves `$string[]` em ordem alfabética em todos os arquivos PHP dentro
de `lang/`. Aplica também a remoção de linhas em branco extras.

```bash
php sortlang.php <caminho_do_plugin>
```

**Limitação:** arquivos com valores de string multilinha são ignorados com aviso.
Moodle core possui strings multilinha por legado, mas arquivos de lang de plugins
não devem tê-las — conteúdo longo pertence a templates Mustache. Se um arquivo
for ignorado, corrija o valor multilinha primeiro.

### addheader.php — injeção do cabeçalho GPL

Adiciona o cabeçalho de licença GPL em todos os arquivos PHP, JS, CSS, SCSS e
Mustache que ainda não o possuem. O nome do pacote Frankenstyle é inferido
automaticamente a partir do caminho do plugin.

```bash
php addheader.php <caminho_do_plugin>
```

Formatos gerados:

| Tipo | Formato |
|---|---|
| PHP / JS | Bloco `//` estilo Moodle |
| CSS / SCSS | Dual `/** */` — GPL + JSDoc com `@package` |
| Mustache | Bloco `{{! ... }}` |

Arquivos em `amd/build/`, `yui/build/`, `vendor/` e arquivos `.min.*` são
ignorados automaticamente.

---

## Monitor de novos plugins Moodle

Script que roda uma vez por dia via cron, detecta plugins recém-publicados no
[Moodle Plugin Directory](https://moodle.org/plugins/) e envia um resumo em
português brasileiro via Telegram.

### Como funciona

- Consulta a API pública `download.moodle.org/api/1.3/pluglist.php` (sem bloqueio de bot)
- Detecta novidades pelo ID auto-incremental dos plugins
- Busca a descrição no repositório GitHub do plugin via GitHub API
- Gera o resumo em PT-BR com fallback chain de IAs na ordem do `~/.phpcs-ai.env`: Gemini → Groq → slots OpenAI-compatible
- Envia a notificação via Telegram
- Acumula cada plugin notificado (`append_weekly()`) em `~/.moodle-plugins-weekly.json` — consumido
  pelo `weekly-digest.py` do repo `jeanlucio-github-io` para gerar o post semanal "Destaques da
  Semana" do blog. O arquivo é uma lista JSON simples (`component`, `name`, `tipo`, `summary`,
  `link`, `detected_at`); o `weekly-digest.py` esvazia o acumulador (`[]`) depois de publicar.

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

> **Fuso horário:** o cron usa o horário do servidor. Se ele estiver em UTC e você quiser
> receber às 6h no Brasil (UTC-3), ajuste para `0 9 * * *`.

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
- Gera resumo PT-BR das mudanças via IA (mesma fallback chain: Gemini → Groq → slots OpenAI-compatible)
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

> **Fuso horário:** mesma observação acima — ajuste para `15 9 * * *` em servidores UTC.

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

---

## Monitor de updates de core Moodle

Script complementar (`core-updates-watch.py`) que checa, para cada um dos três containers
locais (`meu-moodle-web-1`, `meu-moodle-web45-1`, `meu-moodle-web52-1`), se há uma
atualização de core Moodle disponível para o branch daquele container, e notifica via
Telegram quando houver.

### Como funciona

- Não reimplementa a checagem: copia `core_update_probe.php` para dentro de cada
  container (`docker cp` + `docker exec php ...` + remove o arquivo depois) e reaproveita
  a própria classe `\core\update\checker` do Moodle — a mesma usada em Site
  administration > Notifications, que consulta `download.moodle.org/api/1.3/updates.php`.
- Filtra o resultado pelo branch do próprio site (`moodle_major_version(true)`, ex.
  `"5.1"`), então só alerta sobre uma atualização real do branch instalado — não sobre
  branches futuros (ex. sugestão de migrar 5.1 → 5.2).
- Guarda em `~/.moodle-core-updates-seen.json` a última versão já notificada por
  container, para não repetir o aviso todo dia enquanto a mesma atualização seguir
  disponível.

### Instalação manual

```bash
cp core-updates-watch.py core_update_probe.php ~/.moodle-dev-tools/
chmod +x ~/.moodle-dev-tools/core-updates-watch.py

# Registra o cron (roda toda segunda-feira às 9h25)
(crontab -l; echo "25 9 * * 1 /usr/bin/python3 $HOME/.moodle-dev-tools/core-updates-watch.py >> $HOME/.moodle-plugins-monitor.log 2>&1") | crontab -
```

Ajuste a lista `CONTAINERS` no início do script se um container for renomeado ou o
mapeamento de docroot mudar (ver `CLAUDE.md` do projeto § Mapeamento container →
domínio → docroot).
