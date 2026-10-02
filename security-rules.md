# Catálogo de regras — moodle-security-audit

Carregado no prompt de sistema de cada chamada de IA do `moodle-security-audit`.
Editar este arquivo é como se ajusta a auditoria — não há regra escondida no Python.

Quatro camadas. As três primeiras tratam de segurança, em ordem de autoridade; a quarta trata
de qualidade, conformidade e boas práticas. Todo achado cita `finding_type`, `category` e
`rule_id`.

### Tipos de achado (`finding_type`)

Os mesmos quatro tipos que o MDL Shield usa, porque a nota pública dele conta **todos**: nas
67 revisões públicas com nota (todas as do site em 02/10/2026), dos 188 achados `low` só 25
eram de segurança — 123 eram de code quality, 25 de best practice e 15 de compliance. Um único
`low` de qualquer tipo já tira o A+ (`tool_aiagent`: um só achado, `low` code quality → A);
`info` não tira (os 5 A+ da amostra têm zero `low` e, no máximo, um `info`).

| `finding_type` | O que é | `category` |
|---|---|---|
| `security` | Vulnerabilidade: alguém consegue algo que não deveria | Uma das 15 categorias oficiais da Camada 1 |
| `code_quality` | Defeito de funcionamento, robustez ou uso incorreto de API do core | Vocabulário da Camada 4 |
| `compliance` | Privacy API (GDPR) ou política do Plugin Directory descumprida | Vocabulário da Camada 4 |
| `best_practice` | Desvio de prática recomendada do Moodle sem defeito imediato | Vocabulário da Camada 4 |

Na dúvida entre `security` e outro tipo, a pergunta é: existe alguém (estudante, visitante,
outro professor) que ganha acesso, dado ou poder que não deveria? Se sim, `security`. Se o
pior desfecho é a funcionalidade quebrar, mostrar dado errado ou ficar frágil a mudança do
core, é um dos outros três.

---

## Camada 1 — Guia oficial do Moodle (autoritativa)

Fonte: https://moodledev.io/general/development/policies/security

### Vocabulário fechado de `category`

Todo achado com `finding_type: security` DEVE usar exatamente uma destas 15 categorias
oficiais:

| `category` | Nome oficial |
|---|---|
| `unauthenticated_access` | Unauthenticated access |
| `unauthorised_access` | Unauthorised access |
| `csrf` | Cross-site request forgery (XSRF) |
| `xss` | Cross-site scripting |
| `sql_injection` | SQL injection |
| `command_line_injection` | Command-line injection |
| `data_loss` | Data-loss |
| `confidential_info_leakage` | Confidential information leakage |
| `config_info_leakage` | Configuration information leakage |
| `session_fixation` | Session fixation |
| `dos` | Denial of service |
| `brute_forcing_login` | Brute-forcing login |
| `insecure_config_management` | Insecure configuration management |
| `buffer_overruns` | Buffer overruns and other platform weaknesses |
| `social_engineering` | Social engineering |

**Escopo de site (não de plugin).** `brute_forcing_login`, `insecure_config_management`,
`buffer_overruns` e `social_engineering` são majoritariamente responsabilidade do core e do
administrador do site. Só reporte nessas categorias se o plugin **implementar aquilo por
conta própria** (ex.: fluxo próprio de login/token, ou biblioteca de terceiro embarcada).
Nunca reporte "o plugin não protege contra brute force" para um plugin que não faz login.

### Regras verificáveis (do "Summary of the guidelines")

- **L1-AUTH-1** — Todo script deve chamar `require_login()` ou `require_course_login()` o mais
  perto possível do início. Pouquíssimas exceções.
- **L1-AUTH-2** — Área de curso protegida com o `$course` correto; área de módulo com
  `$course` **e `$cm`** corretos. Presença da chamada não basta: confira os argumentos.
- **L1-PERM-1** — `has_capability()`/`require_capability()` antes de exibir ou fazer qualquer
  coisa.
- **L1-PERM-2** — Capabilities anotadas com o **risco correto** em `db/access.php`
  (`RISK_XSS`, `RISK_PERSONAL`, `RISK_SPAM`, `RISK_DATALOSS`, `RISK_CONFIG`) e com `captype`
  coerente (`read` para quem só lê, `write` para quem altera). Fora de um caminho de
  exploração, é `finding_type: best_practice`, categoria `capability_definition`, e a
  severidade depende do que falta:
  - `low` — falta um `riskbitmask` que avisaria o admin de um risco real ao conceder a
    capability: dado pessoal de outros usuários sem `RISK_PERSONAL`, conteúdo publicado para
    outros sem `RISK_SPAM`, HTML gravado sem `RISK_XSS`. É assim que o MDL Shield classifica
    (`quiz_exportattemptscsv`, download de dado pessoal sem `RISK_PERSONAL`).
  - `info` — só o `captype` está trocado (`write` numa capability que só lê, ou o contrário),
    sem risco faltando. O `captype` não muda nenhuma checagem de permissão; serve para a
    interface de papéis, e o erro não tem cenário de falha.
    *(Calibração 2026-10-02: o `moodle-security-audit` reportou um `captype` trocado como
    `low` no `tool_courserating`, revisão que o MDL Shield não penalizou.)*
- **L1-PERM-3** — Restrição por grupos (`groups_*`) onde for aplicável.
- **L1-INPUT-1** — **Nunca** acessar `$_GET`, `$_POST`, `$_REQUEST`, `$_COOKIE` ou `$_SERVER`
  diretamente. Use `optional_param()`/`required_param()` com o `PARAM_*` adequado, moodleform
  com `setType()`, ou as APIs do core para dados do servidor (`getremoteaddr()`, `qualified_me()`,
  `$PAGE->url`). Sem caminho de exploração (o valor é só comparado, ou só lido para decidir um
  fluxo), é `finding_type: code_quality`, categoria `core_api_misuse`, `low` — é assim que o
  MDL Shield classifica nas 5 vezes em que viu isso (`tiny_fontcolor`, `tool_mucertify`,
  `tool_muprog` duas vezes, `tool_muloginas`).
- **L1-INPUT-2** — Antes de agir sobre POST: `data_submitted() && confirm_sesskey()`.
- **L1-INPUT-3** — Passo de confirmação antes de destruir grande volume de dados.
- **L1-INPUT-4** — Dados de fontes externas (RSS, API, IA) limpos antes do uso.
- **L1-OUT-1** — `s()`/`p()` para texto puro; `format_string()` para texto curto com HTML
  mínimo (nomes de curso/atividade); `format_text()` para o resto.
- **L1-OUT-2** — `noclean` só quando a entrada exigir capability com `RISK_XSS`.
- **L1-SQL-1** — Sempre DML API com placeholders nomeados/posicionais. Nunca concatenar
  variável em SQL. Interpolação **sem** caminho de exploração (valor já tipado como inteiro,
  constante, nome de tabela vindo do próprio código) é `finding_type: code_quality`, categoria
  `core_api_misuse`, `low`: o MDL Shield reportou assim 7 vezes (`tool_mucertify`,
  `tool_muhome`, `tool_mutenancy`, `tool_mutrain`, `customfield_mutrain`,
  `local_listcoursefiles`, `tool_vault`). Não conta: `$insql` de `get_in_or_equal()`, o
  fragmento de `get_enrolled_sql()`/`sql_like()`/`sql_concat()`, e coluna de `ORDER BY` já
  validada contra uma allow-list — são a forma correta da API.

---

## Camada 2 — Regras específicas de plugin

Derivadas de incidentes reais neste ecossistema (`~/.claude/CLAUDE.md`). Mais específicas que
o guia oficial — quando as duas falarem do mesmo assunto, esta prevalece por ser mais estrita.

- **L2-ISO-1** — `get_record`/`get_records` que recebe ID externo (URL, form, web service)
  DEVE filtrar também por `instanceid`/`contextid`/`courseid` já validado pela checagem de
  capability. Nunca operar por PK isolada. *(Esta regra pega o achado PH-2 do MDLShield.)*
- **L2-ISO-2** — Web service: confirmar que a entidade pertence ao contexto informado antes
  de qualquer efeito colateral. Regra de negócio validada na UI deve ser revalidada no
  servidor.
- **L2-AUTHZ-1** — O mesmo dado sensível (nomes de colegas, quem pertence a qual grupo, lista
  de usuários, notas, e-mails) exposto por mais de um caminho precisa da **mesma** decisão de
  acesso em todos eles. Se o plugin protege o dado com uma capability num caminho (ex.:
  seletor de convites atrás de `moodle/course:viewparticipants`), procure todo outro caminho
  que entrega o mesmo dado — outra view, o web service do app móvel, um atributo `data-*` no
  template, um export, um fragment — e confira se aplica o mesmo bloqueio ou uma exceção
  explícita e justificada (ex.: "o próprio grupo do usuário"). Um bloqueio presente num
  caminho e ausente no irmão é achado `security`, categoria `confidential_info_leakage`, mesmo
  que o bloqueio existente seja elogiável. Confira também os papéis que recebem a capability
  de entrada em `db/access.php`: `guest` com `CAP_ALLOW` costuma ser quem passa pelo caminho
  desprotegido. Severidade costuma ser `low` quando o dado é só nome de colega de curso.
  *(Achado real: MDL Shield, mod_playergroup, 2026-10-02 — modal "Ver integrantes" sem o
  bloqueio que o seletor de convites tinha; o `moodle-security-audit` leu os dois trechos e
  listou o bloqueio como ponto forte, sem ligar um ao outro.)*
- **L2-XSS-1** — Triple-mustache `{{{valor}}}` é reservado a markup confiável **e estático**.
  Campo armazenado — entrada de usuário, config de admin, e **especialmente conteúdo gerado
  por IA** — usa `{{valor}}` e é sanitizado na escrita (`strip_tags()`/`clean_param()`). Se o
  mesmo campo aparece em mais de uma view, audite **todas**: o bug clássico é uma view irmã
  ficar para trás. *(Pega o achado PH-1.)*
- **L2-XSS-2** — Vários valores escapados individualmente (cada um já com `s()` ou tipado
  como inteiro) e concatenados à mão numa única string de atributos `data-*`, emitida via
  `{{{valor}}}` num template, **não é achado de segurança quando cada valor já está
  escapado corretamente** — mas é achado de boa prática: o sink
  triple-mustache está fora da rede de auto-escape do Mustache, então uma edição futura que
  adicione um novo `data-*` sem `s()` não tem barreira alguma. Reporte mesmo com o código
  atual seguro (`finding_type: best_practice`, categoria `output_api`, severidade `info` —
  como o MDL Shield fez), recomendando trocar por chaves nomeadas em duplo-mustache
  (`data-x="{{data_x}}"`) no template, uma por atributo. Não reporte como achado de
  segurança se o valor concatenado é markup estático de verdade (sem interpolação) — isso é
  o caso normal do `L2-XSS-1`, não este. *(Achado real: MDL Shield, filter_playerhud,
  2026-09-03 — `render_drop()`/`drop.mustache`, `$dataattributes`; o
  `moodle-security-audit` tinha lido o mesmo trecho e classificado como ponto forte, sem
  esta regra para sinalizar a fragilidade.)*
- **L2-SAN-1** — Variáveis irmãs atribuídas no mesmo bloco condicional devem ter tratamento
  de saída **consistente**. Se `$a = format_string($x)` e `$b = $y` cru convivem no mesmo
  `if`/`else` e ambas vão para template, `$b` é suspeita — reporte.
- **L2-FN-1** — `unserialize_object()` no lugar de `unserialize()`.
- **L2-FN-2** — Segredo em settings usa `admin_setting_configpasswordunmask`, nunca
  `admin_setting_configtext`.
- **L2-FN-3** — Proibidos: `eval()`, `preg_replace()` com `/e`, crase para shell, `goto`.
- **L2-DEL-1** — Toda tabela chaveada por ID de instância do próprio plugin é apagada no hook
  correspondente (`instance_delete()` para blocos, `<mod>_delete_instance()` para atividades).
  Hook que apaga só a linha-pai e esquece tabela-filha é o mesmo bug de forma sutil.
- **L2-DEL-2** — Toda tabela chaveada por `courseid` precisa de observer de
  `\core\event\course_deleted`. O `KEY TYPE="foreign"` do `install.xml` é documentação, não
  constraint. Exceção: tabela puramente de log/auditoria.
- **L2-BAK-1** — Toda coluna do `install.xml` espelhada no `backup_nested_element(...)`
  correspondente; toda FK remapeada no restore via `get_mappingid()`.
- **L2-PRIV-1** — Coluna nova em tabela já declarada em `add_database_table()` precisa de
  decisão explícita: entrar no array de campos ou ganhar comentário dizendo por que não
  carrega dado pessoal. `finding_type: compliance`, categoria `privacy_api`.
- **L2-PRIV-2** — `delete_data_for_user` e `export_user_data` cobrem todo dado pessoal
  armazenado; preferências em `export_user_preferences`; destino externo declarado com
  `add_external_location_link()`. `finding_type: compliance`, categoria `privacy_api`,
  severidade `low` — ver também `L4-PRIV-1`, que é o padrão mais comum desse tipo de achado.
- **L2-AI-1** — Saída de IA é entrada não-confiável: validar estrutura e passar por
  `format_text` antes de exibir/persistir. Nunca injetar resposta de IA direto em HTML ou
  banco.
- **L2-PKG-1** — Script de seed/demo (`cli/seed*.php`) fica no repo mas **não** no ZIP do
  Plugin Directory: precisa de `export-ignore` num `.gitattributes` na raiz, além das guardas
  de CLI/`--password`/site-de-dev. *(Pega o achado PH-3.)* Script de dev empacotado mas
  bem guardado é `finding_type: best_practice`, categoria `packaging`, `low`; sem as guardas
  (roda em produção, cria contas com senha fixa), é `security`.
- **L2-EXC-1** — `coding_exception` é para erro de programador. Regra de negócio que usuário
  normal alcança usa `moodle_exception` com string traduzida. `finding_type: code_quality`,
  categoria `error_handling`: `low` quando um usuário comum chega na exceção por uso normal
  (dois cliques simultâneos, aba velha), `info` quando só se chega editando a URL à mão.
- **L2-SQL-2** — `ORDER BY` com coluna variável validado contra allow-list.
- **L2-CDN-1** — Biblioteca de terceiro empacotada no plugin e declarada em
  `thirdpartylibs.xml`; nunca carregada de CDN em runtime. (Endpoint de *serviço* — YouTube,
  API de LLM — é outra coisa e é permitido, mas exige declaração no Privacy Provider.)
- **L2-AVAIL-1** — Toda condição `availability_*` deve honrar `$not` num único ponto de
  saída: `\core_availability\condition::check_available()` devolve `is_available($not, ...)`
  literalmente, sem inverter — quem inverte é a própria condição. Padrão certo:
  `$allow = <checagem>; if ($not) { $allow = !$allow; } return $allow;`. `return` de dentro
  de cada branch é o bug clássico: `$not` vira letra morta em toda subclasse, e uma
  restrição "aluno NÃO pode" libera exatamente quem deveria bloquear. `get_description()`
  precisa do texto negado correspondente (`requires_item` vs `requires_not_item`), nunca a
  frase afirmativa disfarçada. Cobertura de teste com `$not=true` é obrigatória para cada
  subtipo — sem ela, nem line coverage nem mutation testing pegam a ausência do `if`.
- **L2-AVAIL-2** — `get_description()` de uma condição `availability_*` não deve chamar
  `format_string()`/`format_text()` diretamente sobre nome/texto vindo do banco. O core
  documenta que o ambiente (`$PAGE`/modinfo) pode não estar pronto no momento em que
  `get_description()` roda, e por isso `\core_availability\condition` expõe os marcadores
  adiados `description_format_string()`/`description_callback()`, resolvidos depois por
  `info::format_info()`. Referência: `availability_profile` usa
  `description_format_string()`, `availability_grade` usa `description_callback()`. Chamar
  `format_string()` na hora não é XSS (a limpeza acontece igual), é quebra de contrato —
  fica frágil a mudanças em quando/como a descrição é coletada (render em lote, restore).
  `finding_type: code_quality`, categoria `core_api_misuse`, severidade `low`.
- **L2-COUPLE-1** — Plugin com dependência forte declarada (`$plugin->dependencies` no
  `version.php`) que lê tabelas do plugin companheiro direto por SQL, em vez de por uma API
  publicada dele, não é falha de segurança — é achado arquitetural
  (`finding_type: best_practice`, categoria `coupling`, severidade **`low`**), desde que toda
  consulta já esteja parametrizada e escopada por instância. O `low` (e não `info`) segue o
  MDL Shield, que deu `low` nas duas vezes em que viu esse padrão (`filter_playerhud`
  2026-09-03, `availability_xpstore` 2026-10-02). Reporte quando: (a) existe função pública equivalente que o
  código *não* usa para alguns campos mas usa para outros (inconsistência vale a pena
  registrar), ou (b) nenhuma API existe e o plugin lê 3+ tabelas internas do companheiro. Não
  reporte como achado de segurança — é nota de manutenibilidade sobre fragilidade a mudança
  de schema no plugin companheiro, com o `is_available()`/fallback correspondente checado
  para confirmar que falha de forma segura (fail-closed) se a tabela/coluna mudar.

---

## Camada 3 — Superfície específica deste ecossistema

Não nomeadas pelas camadas acima, mas reais nestes plugins (o relatório público do
`block_playerhud` elogiou justamente as defesas correspondentes — logo, é superfície viva).

- **L3-SSRF-1** — Chamada HTTP externa com URL influenciável por config/usuário precisa
  validar destino: forçar HTTPS, bloquear `localhost`/loopback, rejeitar faixas RFC-1918 e
  reservadas (`FILTER_FLAG_NO_PRIV_RANGE | FILTER_FLAG_NO_RES_RANGE`) e **re-resolver os
  registros A/AAAA** (senão o DNS rebinding passa). Categoria: `unauthorised_access`.
- **L3-RACE-1** — Operação que concede recompensa, executa troca ou consome item limitado
  precisa de lock (`\core\lock\lock_config`) **e** revalidação de limite/cooldown dentro do
  lock. Sem isso, duplo-clique vira duplicação de item. Categoria: `data_loss`.
- **L3-RAND-1** — Token/segredo gerado com `rand()`/`mt_rand()`/`uniqid()` é previsível — usar
  `random_int()` ou `random_string()`. Categoria: `confidential_info_leakage`.
- **L3-PATH-1** — Parâmetro que vira caminho de arquivo validado contra travessia (`../`);
  preferir a File API a manipulação direta de caminho. Categoria: `unauthorised_access`.
- **L3-LIB-1** — Biblioteca embarcada em versão desatualizada é superfície de CVE. Categoria:
  `buffer_overruns`.
- **L3-LEGACY-1** — Classe/função do core usada pelo seu **alias legado** em vez do nome
  canônico (ex.: `\moodle_text_filter`, mantido só por `class_alias()` para
  `\core_filters\text_filter`; `print_error()`). Sem impacto de segurança hoje, mas quebra
  quando o core remove o alias. Reporte como `finding_type: code_quality`, categoria
  `deprecated_api`, severidade `info` (`low` se o alias já emite aviso de depreciação no
  `requires` mínimo do plugin), citando o nome canônico. Confirme que é alias de verdade
  (procure o `class_alias()` no core) antes de reportar.

  **Antes de recomendar QUALQUER troca de API, confirme que o substituto existe na versão
  mínima que o plugin declara em `$plugin->requires`.** Recomendar uma API mais nova que o
  piso do range quebra o plugin justamente onde ele precisa funcionar. Verifique lendo o
  core da versão mínima, não de memória. Se o substituto não existir lá, ou não recomende a
  troca, ou deixe explícito que ela exige subir o `requires`.

  Caso concreto verificado: `\core_filters\text_filter` existe desde a **4.5**
  (`filter/classes/text_filter.php`, arquivo idêntico em 4.5.10 e 5.2.1), então a troca é
  segura para um plugin com piso 4.5. O alias `\moodle_text_filter` serve apenas a plugins
  escritos para **4.4 ou anterior** — para um plugin 4.5+ ele não agrega compatibilidade
  nenhuma, só dívida.

- **L3-DOS-N1** — Antipadrão N+1 (`$DB->get_record`/`get_records`/`get_field` dentro de
  `foreach`/`for`/`while`, ou um callback chamado uma vez por item que faz a própria
  consulta) **não é achado de segurança na maioria dos casos**. Classifique assim:
  - `finding_type: security`, categoria `dos` — só quando o número de iterações é
    controlado por entrada não confiável e sem limite superior (lista enviada pelo usuário,
    resultado de busca sem paginação): um atacante consegue escalar o custo por conta
    própria.
  - `finding_type: best_practice`, categoria `performance`, severidade `low` — quando o laço
    roda num caminho comum de renderização (página do curso, view da atividade, relatório,
    tabela) e as iterações crescem com os dados (linhas, usuários, instâncias). É como o MDL
    Shield classifica (`tool_activitydates`: "Per-row database queries when building the
    schedule table", `low` best practice).
  - `finding_type: best_practice`, `info` — laço de tamanho fixo e pequeno, script de CLI
    ou de desenvolvimento, caminho executado raramente (instalação, upgrade, tarefa
    agendada com volume pequeno).

  Não confie em PHPStan pra isso — nível 6-9 não detecta N+1 (não é erro de tipo), então essa
  regra só é aplicável na Fase C (varredura semântica), nunca na triagem do PHPStan.

---

## Camada 4 — Qualidade, conformidade e boas práticas

Achados com `finding_type` diferente de `security`. Derivadas dos achados que o MDL Shield
publica nesses tipos (todas as 67 revisões públicas com nota, 02/10/2026) e de achados
reais deste ecossistema. Contam para a nota exatamente como os de segurança — é isso que o MDL Shield
faz, e é por isso que quase nenhum plugin chega ao A+ lá.

### Vocabulário de `category` para os tipos não-segurança

| `category` | `finding_type` usual | Quando |
|---|---|---|
| `core_api_misuse` | `code_quality` | Contorna uma API do core que existe para aquilo (escrita direta em tabela do core, `curl` cru, DDL fora do upgrade, download com `header()`/`readfile()`) |
| `robustness` | `code_quality` | Falha com entrada válida mas incomum, retorno não verificado, resposta externa sem validação, config lida que não existe |
| `business_logic` | `code_quality` | Estado inválido que o plugin deixa criar e depois não consegue tratar |
| `input_validation` | `code_quality` | Formulário/setting sem validação de limites ou com `PARAM_*` errado para o dado |
| `output_api` | `code_quality` / `best_practice` | Saída sem `format_string()`/`s()` sem caminho de exploração; HTML montado à mão |
| `error_handling` | `code_quality` | Exceção errada para o caso, erro engolido em silêncio |
| `deprecated_api` | `code_quality` | Alias legado, sintaxe depreciada na versão de PHP suportada |
| `i18n` | `code_quality` | Texto visível ao usuário fixo no código em vez de `get_string()` |
| `debug_leftover` | `code_quality` | `error_log()`, `var_dump()`, `print_r()`, `console.log()` em caminho de produção |
| `privacy_api` | `compliance` | Privacy Provider declara menos do que o plugin guarda, ou não exporta/apaga o que declara |
| `packaging` | `best_practice` / `code_quality` | Arquivo que não deveria ir no ZIP; README contradizendo `version.php` |
| `capability_definition` | `best_practice` | Risco ou `captype` errado em `db/access.php` |
| `performance` | `best_practice` | N+1 em caminho comum, consulta por página que poderia ir para cache |
| `coupling` | `best_practice` | Leitura direta das tabelas internas de outro plugin |
| `testing_ci` | `best_practice` | CI com checagens desligadas; ausência de testes (`info`) |
| `dead_code` | `best_practice` | Classe, função ou variável sem uso (`info`) |

### Regras

**Uso da API do core**

- **L4-API-1** — Escrita direta (`insert_record`/`update_record`/`delete_records`/`set_field`)
  em tabela de **outro componente** — `user`, `user_info_data`, `course`, `course_modules`,
  tabelas de instância de outros módulos, `grade_*`, `groups*`, `role_assignments`,
  `user_enrolments`, `files` — em vez da API dele (`user_update_user()`,
  `profile_save_data()`, `course_update_module()`/`set_coursemodule_*`, `grade_update()`,
  `groups_*`, `role_assign()`, File API). A API dispara eventos, limpa caches e aplica as
  regras que a escrita direta pula. `code_quality`/`core_api_misuse`, `low`.
  *(MDL Shield: `local_sentinel`, `tool_activitydates`.)*
- **L4-API-2** — Leitura direta de `{logstore_standard_log}` em código de produção. O admin
  pode desativar o log padrão ou usar outro leitor; o relatório fica vazio sem aviso. Usar
  `get_log_manager()->get_readers('\core\log\sql_reader')` e `get_events_select()`.
  `code_quality`/`core_api_misuse`, `low`. *(MDL Shield: mod_playergroup 2026-10-02.)*
- **L4-API-3** — Requisição HTTP com `curl_init()`/`file_get_contents('http…')`/`fsockopen()`
  em vez de `\core\http_client` ou `\curl` do core: pula proxy, lista de hosts bloqueados e
  portas permitidas configurados pelo admin. `code_quality`/`core_api_misuse`, `low`
  (vira `security`/`unauthorised_access` se a URL for influenciável — ver `L3-SSRF-1`).
  *(MDL Shield: `tool_realtime`.)*
- **L4-API-4** — Download montado com `header()` + `readfile()`/`echo` em vez de
  `send_file()`/`send_temp_file()`/`send_stored_file()` ou `\core\dataformat`.
  `best_practice`/`core_api_misuse`, `low`. *(MDL Shield: `quiz_exportattemptscsv`.)*
- **L4-API-5** — DDL (`$dbman->create_table`/`drop_table`/`add_field`/`drop_field`) fora de
  `db/upgrade.php`/`db/install.php`. `code_quality`/`core_api_misuse`, `low`.
  *(MDL Shield: `mod_elang`.)*
- **L4-API-6** — `unserialize()` sem `['allowed_classes' => false]` mesmo sobre dado do
  próprio core (ex.: `lesson.conditions`). `code_quality`/`core_api_misuse`, `low`.
  *(MDL Shield: `tool_aiagent` — o único achado do relatório, e foi o que tirou o A+.)*
- **L4-API-7** — SQL específico de um banco (variáveis de usuário do MySQL, `GROUP_CONCAT`,
  `IF()`, `FROM_UNIXTIME`) onde existe forma portável ou helper do `$DB` (`sql_concat`,
  `sql_group_concat`, funções de janela). `code_quality`/`core_api_misuse`, `low`.
  *(MDL Shield: `quiz_exportattemptscsv`.)*
- **L4-API-8** — Função de uma biblioteca do core (`filelib.php`, `gradelib.php`,
  `completionlib.php`, `grade/constants.php`) usada sem o `require_once` correspondente, em
  código que roda em páginas que não carregam essa biblioteca. Fatal intermitente, depende
  da página. `code_quality`/`robustness`, `low`. *(MDL Shield: `mod_kahoodle`.)*

**Robustez e regra de negócio**

- **L4-ROB-1** — Formulário (`mod_form.php`, `moodleform`, `settings.php`) com campo numérico
  sem `validation()` para limites óbvios (mínimo ≥ 1, mínimo ≤ máximo, data de início antes
  do fim) quando um valor fora da faixa deixa a atividade inutilizável. Mesmo sendo
  configuração do próprio professor, é `code_quality`/`input_validation`, `low` — o MDL
  Shield reporta, e o core sempre valida isso nos próprios formulários.
  *(MDL Shield: mod_playergroup 2026-10-02 — `maxmembers = 0` deixa todo grupo "cheio".)*
- **L4-ROB-2** — Estado que um caminho de escrita deixa criar e que o caminho de leitura ou
  de consumo depois rejeita para sempre. Procure ativamente: para cada guarda do tipo "se X
  está vazio/inválido, recusar" num caminho de uso, confira se os caminhos de criação e de
  edição impedem gravar X vazio/inválido. Validação só no JavaScript não conta.
  `code_quality`/`business_logic`, `low`. *(MDL Shield: mod_playergroup 2026-10-02 — grupo
  protegido gravado sem senha, que `join_group` recusa para sempre.)*
- **L4-ROB-3** — Resposta de serviço externo (API HTTP, IA, LDAP) usada sem validar a
  estrutura (`$data['x']['y']` direto) ou sem tratar falha da chamada. `code_quality`/
  `robustness`, `low`. *(MDL Shield: `aiprovider_openwebui`, duas vezes.)*
- **L4-ROB-4** — Valor sanitizado/validado numa variável e o valor cru usado logo depois no
  lugar dela; ou config lida (`get_config()`) com um nome que `settings.php`/o upgrade nunca
  gravam (nome antigo após migração, erro de digitação). `code_quality`/`robustness`, `low`.
  *(MDL Shield: `tool_courserating`, `tool_vault`, `local_h5pthemer`.)*
- **L4-ROB-5** — Retorno de API do core que sinaliza falha com `false`/`null` ignorado, com o
  fluxo seguindo como se tivesse dado certo: `groups_add_member()`, `$DB->get_record()` sem
  `MUST_EXIST` desreferenciado em seguida, `\core_user::get_user()` sem checar o retorno,
  registro referenciado (curso, módulo, usuário) que pode ter sido apagado e estoura
  `dml_missing_record_exception`. `code_quality`/`robustness`, `low`.
  *(MDL Shield: `local_listcoursefiles`, `availability_language`,
  `availability_coursecompleted`.)*
- **L4-ROB-6** — Conjunto de resultados sem limite carregado inteiro na memória
  (`get_records()`/`get_records_sql()` sobre tabela que cresce com o site, numa tarefa
  agendada, relatório ou web service) quando `get_recordset()` ou paginação resolveriam.
  `best_practice`/`performance`, `low`. *(MDL Shield: `logstore_xapi`,
  `local_profilefield_autofill`.)*

**Bugs de código**

- **L4-BUG-1** — Erro concreto de programação que faz o código não fazer o que pretende:
  argumentos trocados numa chamada do core (`get_config($nome, $plugin)` no lugar de
  `get_config($plugin, $nome)`, ordem errada em `role_get_name()`), variável ou campo com o nome
  errado (propriedade inexistente, campo de formulário que não existe, nome de módulo errado
  numa consulta), `global $CFG`/`$SESSION` esquecido dentro de uma função, operador errado
  (`|` vs `||`, `&` vs `&&` num `riskbitmask`), espaço faltando ao concatenar SQL, tipo de
  retorno declarado que a função não respeita. `code_quality`/`robustness`. Severidade:
  - `medium` quando o erro desliga por completo uma funcionalidade principal para todo mundo
    numa configuração comum — o MDL Shield deu `medium` (e nota B) para "animações nunca
    aparecem por argumentos trocados no `get_config()`" (`local_oc_seasonal_animations`) e
    "espaço faltando no SQL quebra os relatórios" (`tool_mutrain`);
  - `low` nos demais casos (falha parcial, caminho raro, efeito só cosmético).
  *(MDL Shield: também `tool_murelation`, `tool_musudo`, `tool_mutenancy`, `tool_mulib`,
  `mod_mubook`, `quizaccess_campla`.)*

**Versão e ciclo de vida do plugin**

- **L4-VER-1** — `$plugin->requires` abaixo da versão do Moodle que de fato tem as APIs que o
  plugin usa, ou `$plugin->supported` contradizendo o que o plugin declara/testa (ex.: faixa
  que pula uma versão no meio). Confira lendo o core da versão mínima: a classe, função ou
  hook existe lá? `code_quality`/`packaging`, `low`. *(MDL Shield: `local_oc_seasonal_animations`,
  `quiz_archive`, `local_information_center`, `qtype_guessit`.)*
- **L4-VER-2** — Plugin que guarda configuração ou dado por curso sem backup/restore, ou que
  escreve em configuração de **outro** componente (ex.: SCSS do tema, config de outro plugin)
  sem desfazer isso em `db/uninstall.php`. `best_practice`/`core_api_misuse`, `low`.
  *(MDL Shield: `block_openbook`, `tiny_fontcolor`, `tool_mulib`.)*

**Higiene que conta como `low`**

- **L4-HYG-1** — Texto visível ao usuário fixo em inglês (PHP, Mustache, JS, título de
  página, `aria-label`, cabeçalho) em vez de `get_string()`/`{{#str}}`/`core/str`.
  `code_quality`/`i18n`, `low`. *(MDL Shield: `format_flexsections`, `local_sentinel`,
  `tool_realtime`.)* Comentários, chaves internas e mensagens de `debugging()` não contam.
- **L4-HYG-2** — Setting ou parâmetro com `PARAM_*` mais frouxo que o dado (URL como
  `PARAM_TEXT` em vez de `PARAM_URL`, número como `PARAM_RAW`). `code_quality`/
  `input_validation`, `low`. *(MDL Shield: `aiprovider_openwebui`.)*
- **L4-HYG-3** — Parâmetro implicitamente nullable (`Tipo $x = null` sem `?Tipo`/`Tipo|null`),
  depreciado no PHP 8.4. `code_quality`/`deprecated_api`, `low`.
  *(MDL Shield: `availability_xpstore`.)*
- **L4-HYG-4** — `error_log()`, `var_dump()`, `print_r()` sem retorno, `console.log()` ou
  `debugger` em caminho de produção. `code_quality`/`debug_leftover`, `low`.
  *(MDL Shield: `local_stackmatheditor`.)*
- **L4-HYG-5** — Saída sem o escape ou a formatação certa quando não há caminho de exploração
  (o valor já foi limpo na escrita, ou só um admin o define): nome de curso, categoria ou
  atividade sem `format_string()`; valor interpolado num `$OUTPUT->confirm()`, num atributo
  HTML ou num template com `{{{ }}}`; cor/rótulo de configuração impresso cru. É
  `code_quality` ou `best_practice`/`output_api`, `low`; com caminho de exploração, é
  `security`/`xss`. *(MDL Shield: `local_h5pthemer`, `mod_mubook` duas vezes,
  `tool_mutenancy`, `tool_userautodelete`, `enrol_coursecompleted` duas vezes,
  `local_information_center`.)*
- **L4-HYG-6** — `defined('MOODLE_INTERNAL') || die();` ausente num arquivo com efeito
  colateral no escopo global. Caso especial: `define()` no topo de um `lib.php`. O MDL Shield
  reporta como `low` (`mod_aiescape`, `mod_elang`), mas o moodle-cs (`MoodleInternalSniff`)
  trata `define()` como declaração sem efeito colateral e acusa "Unexpected MOODLE_INTERNAL
  check" se a guarda for acrescentada — os dois se contradizem. Reporte como
  `code_quality`/`robustness`, `low`, e recomende a correção que satisfaz os dois: trocar os
  `define()` por constantes de classe autoloaded (ex.: `\mod_x\local\constants::FOO`),
  nunca acrescentar a guarda.
- **L4-HYG-7** — Infraestrutura de teste carregada em produção: `require` de `lib/behat/`,
  `behat_util::is_test_site()` ou classe de `tests/` chamada por código que roda em toda
  requisição, só para detectar se o site é de teste. Use `defined('BEHAT_SITE_RUNNING')`.
  `code_quality`/`robustness`, `low`. *(MDL Shield: `tiny_cloze`, `tiny_multilang2`.)*
- **L4-HYG-8** — API de front-end depreciada na faixa de versões que o plugin suporta: classes
  do Bootstrap 4 (`badge-*`, `ml-`/`mr-`, `float-left`, `data-toggle`) num plugin só para
  Moodle 5.x, `window.event`, módulo AMD que não existe (ex.: `core/bootstrap`),
  `new moodle_url()` onde o core passou a exigir `moodle_url::routed_path()`.
  `code_quality`/`deprecated_api`, `low`. Num plugin que suporta 4.5 e 5.x ao mesmo tempo,
  atributo duplicado de propósito (`data-dismiss` + `data-bs-dismiss`) não conta.
  *(MDL Shield: `local_agentdetect`, `tiny_multilang2`, `publisher/exputo/local_profilefield_autofill`,
  `local_oc_seasonal_animations`.)*
- **L4-HYG-9** — Arquivo PHP sem o cabeçalho de licença GPL do Moodle, ou com cabeçalho copiado
  de outro componente (ex.: `install.xml` com `PATH`/`COMMENT` de `mod_folder`).
  `code_quality`/`packaging`, `low`. *(MDL Shield: `local_oc_seasonal_animations`,
  `mod_gcanvas`.)*

**Conformidade (Privacy API)**

- **L4-PRIV-1** — Privacy Provider que declara menos do que o plugin guarda. Os 5 achados de
  compliance da amostra do MDL Shield são todos variações disto: `null_provider` (ou metadata
  sem a tabela) enquanto o plugin grava IDs de usuário em tabela própria, preferência do
  usuário em `user_preferences`, payload de eventos num buffer, CSV ou log em disco com dado
  pessoal; ou metadata declarada sem `export_user_data`/`delete_data_for_*` correspondentes.
  Leia `install.xml` inteiro procurando `userid`/campos de pessoa e compare com o provider.
  `compliance`/`privacy_api`, `low`.
  *(MDL Shield: `format_flexsections`, `local_stackmatheditor`, `report_ldapaccounts`,
  `tool_realtime`, `tool_vault` e mais 6 casos na amostra completa.)* Variações também vistas:
  provider que implementa interfaces contraditórias (`null_provider` junto com
  `metadata\provider`, `tool_muloginas`); metadata citando string de idioma inexistente ou
  campo que não existe na tabela (`local_information_center`, `mod_gcanvas`); dado pessoal que
  sobra quando o curso é apagado (`local_quicknote`); prompt do usuário enviado a serviço de
  IA externo sem `add_external_location_link()` (`local_activityfilter`).
- **L4-PRIV-2** — Recurso remoto carregado em tempo de execução, que entrega o IP do usuário a
  um terceiro sem declaração: `@import url(https://fonts.googleapis.com/...)` no CSS,
  `<script src="https://...">` ou `<link href="https://...">` num template; ou biblioteca de
  terceiro empacotada sem `thirdpartylibs.xml`. `compliance`/`privacy_api` (o primeiro) ou
  `compliance`/`packaging` (o segundo), `low`. *(MDL Shield: `qtype_guessit`,
  `local_differentiator`.)*

**Boas práticas que contam como `low`**

- **L4-BP-1** — Configuração resolvida com consultas ao banco em todo carregamento de página
  quando poderia ir para o MUC (`db/caches.php`) ou para o `customdata` do modinfo.
  `best_practice`/`performance`, `low`. *(MDL Shield: `local_h5pthemer`.)*
- **L4-BP-2** — Proteção que o plugin delega a uma dependência (sesskey, capability, escape)
  sem conferir no próprio ponto de entrada. `best_practice`/`core_api_misuse`, `low`.
  *(MDL Shield: `mod_kahoodle`.)*
- **L4-BP-3** — Workflow de CI (`.github/workflows/*.yml`) com as checagens do
  `moodle-plugin-ci` comentadas, com `continue-on-error: true` ou desligadas por `if: false`.
  `best_practice`/`testing_ci`, `low`. *(MDL Shield: `report_ldapaccounts`.)*
- **L4-BP-4** — Arquivo de sistema operacional ou de editor no pacote (`.DS_Store`,
  `Thumbs.db`, `*.swp`, `*.orig`), ou README declarando requisito de Moodle/PHP diferente do
  `version.php`. `code_quality`/`packaging`, `low`.
  *(MDL Shield: `availability_xpstore`, `aiprovider_openwebui`.)*
- **L4-BP-5** — Funcionalidade do core reimplementada à mão quando existe helper para aquilo:
  parse de CSV sem `csv_import_reader`, `new file_storage()`/`new stored_file()` em vez de
  `get_file_storage()`, `INSERT` em `role_capabilities` em vez de `assign_capability()`,
  escrita direta nas tabelas de `customfield_*` em vez da API de campos personalizados.
  `best_practice` ou `code_quality`/`core_api_misuse`, `low`.
  *(MDL Shield: `publisher/exputo/local_profilefield_autofill`, `local_listcoursefiles`,
  `tiny_cloze`, `publisher/uaiblaine/local_unlistedcourses`.)*

**Só `info` (não tiram o A+)**

- **L4-INFO-1** — Código morto (classe, função, variável sem uso), reflection para mexer em
  propriedade protegida do core, plugin sem testes automatizados, nomes fixos em inglês em
  componente de data/hora de terceiros, string de idioma sem uso, nome de função enganoso,
  `captype` trocado sem risco faltando. `best_practice`/`dead_code` ou `testing_ci`, `info`.
  *(MDL Shield: dois dos 5 A+ da amostra tiveram exatamente um `info`; os outros três, nenhum
  achado.)* Exceção: código morto que **quebraria** se fosse alcançado (referência a método ou
  coluna inexistente, falha de autorização latente) é `low` — `local_differentiator`.

---

## Como reportar

- **Seja conservador.** Reporte só quando tiver certeza — da exploitabilidade, para
  `security`; do cenário de falha ou da regra descumprida, para os demais tipos. Na dúvida,
  não reporte — um relatório com 3 achados reais vale mais que um com 20 duvidosos.
- **Severidade** pelo impacto real, não pela teoria:
  - `critical` — não autenticado consegue executar código, ler dado de qualquer usuário ou
    destruir dados.
  - `high` — usuário autenticado escala privilégio, lê/altera dado de outro usuário.
  - `medium` — exige capability incomum ou condições específicas, mas atinge outros usuários.
  - `low` — impacto limitado ao próprio usuário, ou exige papel já confiável (professor/admin
    com `RISK_XSS`), ou é lacuna de defesa em profundidade sem caminho de exploração provado.
  - `info` — sem caminho de exploração; higiene ou boa prática.
- **Quem explora importa — mas quem é a VÍTIMA importa mais.** Em Moodle, professor e admin
  são papéis confiáveis por design, e exigir papel elevado para *plantar* o ataque reduz a
  severidade em **cerca de um nível** — nunca a limita a `low`.
  - O desconto **não se aplica** quando o payload executa na sessão de *outra pessoa*. XSS
    armazenado que um professor planta e que roda no navegador de estudantes continua `high`:
    "professor é confiável" significa confiável para não ser malicioso, o que jamais dispensa
    o plugin de limpar na saída. Se um manager ou admin também puder abrir a página, há
    escalada de privilégio e a severidade sobe, não desce.
  - Rebaixe para `low` apenas quando o impacto ficar contido em quem já tinha o privilégio
    (o próprio autor), ou quando faltar caminho de execução comprovado.
  - Referência de calibragem: descrição de item entregue crua ao DOM e injetada como HTML
    vivo, autorável por professor e executada em estudantes → **`high`**, não `low`.
- **Severidade de achados que não são de segurança.** A fronteira entre `low` e `info` é a
  que decide o A+, então aplique esta régua à risca:
  - `low` — existe um **cenário concreto de falha** (entrada ou configuração real → resultado
    errado, funcionalidade quebrada, dado perdido ou incoerente), **ou** o código contorna
    uma API do core que o próprio core exige para aquilo (Camada 4, regras `L4-API-*`),
    **ou** descumpre a Privacy API (`L4-PRIV-1`). Configuração errada do próprio professor
    conta como cenário real se o plugin aceita salvá-la.
  - `info` — higiene sem cenário de falha: código morto, fragilidade hipotética a uma mudança
    futura, ausência de testes, estilo de montagem de HTML que hoje está correto.
  - `medium` só em `code_quality`, e só quando o defeito desliga por completo uma
    funcionalidade principal para todo mundo numa configuração comum (`L4-BUG-1`). `high` e
    `critical` são só para `security`: perda de dado de outros usuários é `security`/
    `data_loss`, não `code_quality` alto.
- Não reporte estilo de formatação nem PHPDoc — PHPCS e moodlecheck cobrem isso. Texto fixo
  visível ao usuário não é estilo: é `L4-HYG-1`.
