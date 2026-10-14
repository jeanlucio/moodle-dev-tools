"""What an audit looks for: security, quality, or both (`--focus`).

The report already tags every finding with one of four types, so splitting the audit is mostly a
matter of choosing which types a run asks for, verifies and grades:

  security  vulnerabilities only (layers 1-3 of the catalog). It is the release gate and has its
            own grade. PHPStan is skipped: it is a quality tool.
  quality   code quality, compliance and best practice (layer 4). The deterministic checks do the
            bulk of it for free; the AI scan reads only what changed since the last tag. A
            security finding seen along the way is still reported, never dropped.
  all       both, graded together. The behaviour before the split, so existing caches stay valid.

A security run also keeps a small history per scope, so "is this scope done?" has an answer:
two complete runs in a row with nothing above `low` close it.
"""

import json
from datetime import date

FOCI = ('all', 'security', 'quality')
SECURITY_TYPE = 'security'
STATE_REL = '.plans/security-audit/state.json'
# Severities that keep a scope open: anything above low.
OPEN_SEVERITIES = ('critical', 'high', 'medium')
CLOSING_RUNS = 2
HISTORY_KEPT = 5

SCAN_PROMPT_SECURITY = """Você é um revisor de segurança de plugins Moodle. Analise os arquivos
listados abaixo procurando SOMENTE vulnerabilidades: alguém (estudante, visitante, outro
professor) que ganha acesso, dado ou poder que não deveria. Use as Camadas 1-3 do catálogo do
seu prompt de sistema. Defeitos de qualidade, conformidade e boa prática ficam de fora desta
rodada: não os reporte.

Leia cada arquivo por completo com a ferramenta Read. Você PODE e DEVE ler outros arquivos do
plugin (Grep/Glob/Read) quando precisar confirmar um achado — seguir a cadeia de chamada é o
que separa achado real de suposição. Para confirmar como o core se comporta (o que uma API
exige, se uma função existe na versão mínima do plugin), leia o código do core do Moodle.

Procure ativamente: o mesmo dado sensível exposto por dois caminhos com decisões de acesso
diferentes (L2-AUTHZ-1); entidade ou curso recebido do cliente sem checar que pertence à
instância; ação que concede, troca ou consome algo sem lock (L3-RACE-1); regra da interface que o
servidor não revalida; saída de IA, de serviço externo ou do banco entrando em HTML sem escape.

Arquivo em tests/ não é superfície de ataque: ignore-o.

Seja conservador: só reporte o que tiver certeza. Em Moodle professor e admin são papéis
CONFIÁVEIS por design: falha que só um professor dispara é no máximo "low", a não ser que
atinja dados fora do curso dele.

Responda APENAS com um array JSON (vazio se nada encontrado):
[{
  "title": "título curto",
  "finding_type": "security",
  "severity": "critical|high|medium|low|info",
  "category": "uma das 15 categorias oficiais de segurança",
  "rule_id": "id da regra do catálogo, ex. L2-XSS-1",
  "file": "caminho/relativo.php",
  "line": 123,
  "extra_locations": [{"file": "outro.mustache", "line": 95}],
  "description": "o que está errado e por quê",
  "exploitable_by": "quem consegue explorar",
  "impact": "blast radius concreto",
  "mitigations": "proteções que JÁ existem e limitam o impacto (string vazia se nenhuma)",
  "recommendation": "correção específica"
}]

"file"/"line" apontam a ocorrência PRINCIPAL; use "extra_locations" para as demais ocorrências
do mesmo problema. O trecho de código é extraído automaticamente do arquivo — não o copie.

Arquivos deste lote:
"""

SCAN_PROMPT_QUALITY = """Você é um revisor de plugins Moodle. Analise os arquivos listados abaixo
procurando SOMENTE defeitos de qualidade de código, conformidade (Privacy API, política do
Plugin Directory) e boa prática: a Camada 4 do catálogo do seu prompt de sistema. As checagens
determinísticas já cobrem coluna fora do backup, tabela que nada apaga, campo sem maxlength,
preferência fora do Privacy Provider e token com uniqid()/rand(): não repita esses.

Leia cada arquivo por completo com a ferramenta Read. Você PODE e DEVE ler outros arquivos do
plugin (Grep/Glob/Read) quando precisar confirmar um achado. Para confirmar como o core se
comporta (o que uma API exige, se uma função existe na versão mínima do plugin), leia o código
do core do Moodle.

Procure ativamente: estados que a escrita deixa criar e a leitura rejeita para sempre
(L4-ROB-2); formulários sem validation() para limites que, violados, quebram a atividade
(L4-ROB-1); APIs do core contornadas (L4-API-*); Privacy Provider declarando menos do que o
plugin grava (L4-PRIV-1); texto fixo visível ao usuário (L4-HYG-1); capability dada a papel de
curso mas checada só no contexto de sistema (L4-ROB-7).

Se ao ler você perceber uma VULNERABILIDADE (alguém ganha acesso, dado ou poder indevido),
reporte-a mesmo assim, com "finding_type": "security". Ela não pode se perder só porque esta
rodada é de qualidade.

Arquivos de tests/ (quando estiverem no lote) seguem a lista "Quais regras valem dentro de
tests/" do catálogo: lá só valem L4-TEST-1, L4-TEST-2, cabeçalho GPL e sintaxe depreciada.
Gravar direto em tabela, texto fixo, $_POST simulado e consulta em laço são normais num teste.

Seja conservador: só reporte o que tiver certeza. Nada de formatação de código nem PHPDoc.
Aplique a régua low/info do catálogo à risca: "low" exige cenário concreto de falha, contorno
de API do core ou descumprimento da Privacy API; o resto é "info". "medium" só para
"code_quality" que desliga por completo uma funcionalidade principal (L4-BUG-1).

Responda APENAS com um array JSON (vazio se nada encontrado):
[{
  "title": "título curto",
  "finding_type": "code_quality | compliance | best_practice | security",
  "severity": "medium|low|info (ou severidade de segurança)",
  "category": "vocabulário da Camada 4 (ou categoria de segurança)",
  "rule_id": "id da regra do catálogo, ex. L4-ROB-2",
  "file": "caminho/relativo.php",
  "line": 123,
  "extra_locations": [{"file": "outro.mustache", "line": 95}],
  "description": "o que está errado e por quê",
  "exploitable_by": "em que situação o defeito aparece",
  "impact": "blast radius concreto",
  "mitigations": "proteções que JÁ existem e limitam o impacto (string vazia se nenhuma)",
  "recommendation": "correção específica"
}]

"file"/"line" apontam a ocorrência PRINCIPAL; use "extra_locations" para as demais ocorrências
do mesmo problema. O trecho de código é extraído automaticamente do arquivo — não o copie.

Arquivos deste lote:
"""


def scan_prompt(focus, all_prompt):
    """The scan prompt of a focus; `all` keeps the original prompt."""
    return {'security': SCAN_PROMPT_SECURITY, 'quality': SCAN_PROMPT_QUALITY}.get(focus, all_prompt)


def keep_for_focus(findings, focus):
    """Candidates a run of this focus verifies.

    Security keeps only security: that is what it is asked for, and it saves the verification of
    the rest. Quality and all keep everything, so a vulnerability seen during a quality run is
    verified and reported like any other.
    """
    if focus == 'security':
        return [f for f in findings if f.get('finding_type', SECURITY_TYPE) == SECURITY_TYPE]
    return list(findings)


def split_by_security(findings):
    """(security findings, the others)."""
    security = [f for f in findings if f.get('finding_type', SECURITY_TYPE) == SECURITY_TYPE]
    others = [f for f in findings if f.get('finding_type', SECURITY_TYPE) != SECURITY_TYPE]
    return security, others


def scope_key(only):
    """The selectors a run covered, one history entry each; `*` for the whole plugin."""
    return list(only) if only else ['*']


def load_state(plugin_dir):
    path = plugin_dir / STATE_REL
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def record_run(plugin_dir, focus, only, highest, today=None):
    """Append a complete, whole-scope run to the history of every selector it covered.

    `highest` is the worst severity the run confirmed ('none' when it confirmed nothing). Only
    the security focus keeps a history: it is the one that has a stopping point.
    """
    if focus != 'security':
        return
    state = load_state(plugin_dir)
    runs = state.setdefault('security', {})
    stamp = (today or date.today()).isoformat()
    for key in scope_key(only):
        history = runs.setdefault(key, [])
        history.append({'date': stamp, 'highest': highest})
        runs[key] = history[-HISTORY_KEPT:]
    path = plugin_dir / STATE_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def is_closed(plugin_dir, key):
    """Did the last CLOSING_RUNS complete security runs of this scope stay at low or below?"""
    history = load_state(plugin_dir).get('security', {}).get(key, [])
    recent = history[-CLOSING_RUNS:]
    return (len(recent) == CLOSING_RUNS
            and all(run.get('highest') not in OPEN_SEVERITIES for run in recent))


def highest_severity(findings, order):
    """The worst severity among findings, 'none' for an empty list. `order` runs worst to best."""
    present = {f.get('severity', 'info') for f in findings}
    for severity in order:
        if severity in present:
            return severity
    return 'none'
