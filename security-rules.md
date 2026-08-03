# Catálogo de regras — moodle-security-audit

Carregado no prompt de sistema de cada chamada de IA do `moodle-security-audit`.
Editar este arquivo é como se ajusta a auditoria — não há regra escondida no Python.

Três camadas, em ordem de autoridade. Todo achado cita `category` (vocabulário fechado da
Camada 1) e `rule_id`.

---

## Camada 1 — Guia oficial do Moodle (autoritativa)

Fonte: https://moodledev.io/general/development/policies/security

### Vocabulário fechado de `category`

Todo achado DEVE usar exatamente uma destas 15 categorias oficiais:

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
  coerente (`read` para quem só lê, `write` para quem altera).
- **L1-PERM-3** — Restrição por grupos (`groups_*`) onde for aplicável.
- **L1-INPUT-1** — **Nunca** acessar `$_GET`, `$_POST` ou `$_REQUEST` diretamente. Use
  `optional_param()`/`required_param()` com o `PARAM_*` adequado, ou moodleform com
  `setType()`.
- **L1-INPUT-2** — Antes de agir sobre POST: `data_submitted() && confirm_sesskey()`.
- **L1-INPUT-3** — Passo de confirmação antes de destruir grande volume de dados.
- **L1-INPUT-4** — Dados de fontes externas (RSS, API, IA) limpos antes do uso.
- **L1-OUT-1** — `s()`/`p()` para texto puro; `format_string()` para texto curto com HTML
  mínimo (nomes de curso/atividade); `format_text()` para o resto.
- **L1-OUT-2** — `noclean` só quando a entrada exigir capability com `RISK_XSS`.
- **L1-SQL-1** — Sempre DML API com placeholders nomeados/posicionais. Nunca concatenar
  variável em SQL.

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
- **L2-XSS-1** — Triple-mustache `{{{valor}}}` é reservado a markup confiável **e estático**.
  Campo armazenado — entrada de usuário, config de admin, e **especialmente conteúdo gerado
  por IA** — usa `{{valor}}` e é sanitizado na escrita (`strip_tags()`/`clean_param()`). Se o
  mesmo campo aparece em mais de uma view, audite **todas**: o bug clássico é uma view irmã
  ficar para trás. *(Pega o achado PH-1.)*
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
  carrega dado pessoal.
- **L2-PRIV-2** — `delete_data_for_user` e `export_user_data` cobrem todo dado pessoal
  armazenado; preferências em `export_user_preferences`; destino externo declarado com
  `add_external_location_link()`.
- **L2-AI-1** — Saída de IA é entrada não-confiável: validar estrutura e passar por
  `format_text` antes de exibir/persistir. Nunca injetar resposta de IA direto em HTML ou
  banco.
- **L2-PKG-1** — Script de seed/demo (`cli/seed*.php`) fica no repo mas **não** no ZIP do
  Plugin Directory: precisa de `export-ignore` num `.gitattributes` na raiz, além das guardas
  de CLI/`--password`/site-de-dev. *(Pega o achado PH-3.)*
- **L2-EXC-1** — `coding_exception` é para erro de programador. Regra de negócio que usuário
  normal alcança usa `moodle_exception` com string traduzida.
- **L2-SQL-2** — `ORDER BY` com coluna variável validado contra allow-list.
- **L2-CDN-1** — Biblioteca de terceiro empacotada no plugin e declarada em
  `thirdpartylibs.xml`; nunca carregada de CDN em runtime. (Endpoint de *serviço* — YouTube,
  API de LLM — é outra coisa e é permitido, mas exige declaração no Privacy Provider.)

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
  quando o core remove o alias. Reporte como `info`, categoria
  `insecure_config_management`, citando o nome canônico. Confirme que é alias de verdade
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

---

## Como reportar

- **Seja conservador.** Reporte só quando tiver certeza da exploitabilidade. Na dúvida, não
  reporte — um relatório com 3 achados reais vale mais que um com 20 duvidosos.
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
- Não reporte estilo, PHPDoc, i18n ou preferência de código — outras ferramentas cobrem isso.
