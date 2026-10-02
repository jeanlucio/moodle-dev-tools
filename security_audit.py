#!/usr/bin/env python3
"""Audit of a single Moodle plugin: deterministic tools + AI review.

Covers the same four finding types MDL Shield grades on — security, code quality,
compliance and best practice — so the grade here tracks the public one there.

Pipeline (see README "Auditoria de segurança"):
  A. deterministic collection (PHPStan at a high level, pattern checks from
     audit_static_checks.py, bundled-library versions, optionally moodlecheck)
  B. AI triage of the PHPStan output — separates real bugs from Moodle-idiom noise, which
     is what makes a high PHPStan level usable at all
  C. AI semantic scan, batched, with read-only tools so the agent can follow call
     chains beyond its own batch
  D. AI verification pass — every candidate from A, B and C must be confirmed or refuted:
     security ones on exploitability, the other types on whether the defect is real
  E. AI dedup pass — batches (C) and per-candidate verification (D) run in isolation
     from each other, so the same root cause found from two different angles (two
     files in different batches, or the same file read twice) survives as two
     separate findings unless something looks at the confirmed list as a whole
  F. deterministic grade + Markdown report

Every AI call runs through the local `claude` CLI against the Claude Code subscription;
API keys are stripped from the child environment so a stray ANTHROPIC_API_KEY can never
turn this into per-token billing.

Usage: security_audit.py <plugin_abs_dir> [options]  (normally via moodle-security-audit)
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

from audit_static_checks import run_static_checks
from claude_cli import (
    Clock, ProgressWriter, Uncached, cache_path, cached, call_claude, extract_json,
    fmt_duration, hash_key, run_parallel, usage,
)

TOOLS_DIR = Path(__file__).resolve().parent
RULES_FILE = TOOLS_DIR / 'security-rules.md'
PHPSTAN_BIN = TOOLS_DIR / 'phpstan' / 'vendor' / 'bin' / 'phpstan'
# Inherited from ~/.moodle-dev-tools.env via security-audit.sh (the CLI entry point),
# which sources it with `set -a` before invoking this script — falls back to this
# machine's own path when the wrapper isn't used or the env file doesn't exist.
MOODLE_ROOT = Path(os.environ.get('MDT_MOODLE_HTML', '/home/ubuntu/meu-moodle/html'))
MOODLE_DOCROOT = MOODLE_ROOT / 'public'
CACHE_DIR = Path.home() / '.moodle-security-audit-cache'

# Lives next to progress.html so the dashboard can poll it with a plain relative fetch()
# when both are served from this same directory (e.g. via VS Code Live Server) — no need
# to expose any path outside what is already being served.
PROGRESS_DIR = TOOLS_DIR / '.progress'

# Reports live inside the plugin, next to the code they describe, in a security-audit/
# subfolder of .plans/ — the directory this ecosystem already uses for AI assistant
# workspace files. Gitignoring .plans/ as a whole (not just the subfolder) keeps the rule
# aligned with every other tool that already writes there, and still covers this one.
REPORT_SUBDIR = '.plans/security-audit'
GITIGNORE_ENTRY = '.plans'
GITIGNORE_COMMENT = '# AI assistant session/workspace directories, not part of the plugin.'

# Bumped whenever a prompt changes, so cached results from an older prompt are not reused.
# v2: scan prompt gained extra_locations/mitigations; severity calibration rewritten.
# v3: four finding types (MDL Shield's taxonomy), separate verification for non-security
#     findings, Layer 4 of the rule catalog.
# v4: L1-PERM-2 split — a wrong captype alone is info, a missing riskbitmask stays low.
#     The catalog is the system prompt but not part of the cache key, so a rule change that
#     moves a severity needs this bump to take effect on a re-run.
PROMPT_VERSION = '4'

CLAUDE_TIMEOUT = 900

# PHPStan identifiers that are pure PHPDoc/generics noise in a Moodle codebase. Dropped
# before AI triage — they carry no security signal and would burn quota to reject one by
# one. Anything NOT listed here goes to triage, so the filter stays conservative.
PHPSTAN_NOISE_IDENTIFIERS = {
    'missingType.iterableValue',
    'missingType.parameter',
    'missingType.property',
    'missingType.return',
    'missingType.generics',
}

SEVERITY_ORDER = ['critical', 'high', 'medium', 'low', 'info']

# The same four finding types MDL Shield labels its findings with. Its public grade counts
# all of them, not just security — in a sample of 18 public reviews (Oct 2026) only 5 of the
# 44 low findings were security ones — so the grade here counts all of them too.
FINDING_TYPES = ['security', 'code_quality', 'compliance', 'best_practice']
FINDING_TYPE_LABELS = {
    'security': 'segurança',
    'code_quality': 'qualidade de código',
    'compliance': 'conformidade',
    'best_practice': 'boa prática',
}

# Directories that never carry security signal worth a deep read. Their presence is still
# recorded in the inventory (test count is evidence of rigour), they are just not scanned.
SKIP_DIRS = {'amd/build', 'node_modules', 'vendor', '.git', 'docs', '.plans'}
METADATA_ONLY_DIRS = {'tests', 'lang'}

SCAN_EXTENSIONS = {'.php', '.js', '.mustache', '.xml', '.css'}
# Scanned only where MDL Shield has been seen to find something in them: a README stating
# requirements that contradict version.php, a CI workflow with its checks switched off.
ROOT_DOC_EXTENSIONS = {'.md'}
WORKFLOW_DIR = '.github/workflows'
WORKFLOW_EXTENSIONS = {'.yml', '.yaml'}


# --------------------------------------------------------------------------- #
#  Phase 0 — inventory                                                         #
# --------------------------------------------------------------------------- #

def _is_skipped(rel):
    """True when the path lives under any never-scanned directory, at any depth.

    Matches on path segments so multi-segment entries like "amd/build" work and a file
    merely named "docs.php" is not mistaken for the docs/ directory.
    """
    parts = Path(rel).parts
    for skip in SKIP_DIRS:
        skip_parts = tuple(skip.split('/'))
        span = len(skip_parts)
        if any(parts[i:i + span] == skip_parts for i in range(len(parts) - span + 1)):
            return True
    return False


def collect_files(plugin_dir):
    """Every candidate file, classified into scan tiers."""
    scan, metadata_only = [], []
    for path in sorted(plugin_dir.rglob('*')):
        if not path.is_file():
            continue
        rel = str(path.relative_to(plugin_dir))
        wanted = (
            path.suffix in SCAN_EXTENSIONS
            or (path.suffix in ROOT_DOC_EXTENSIONS and '/' not in rel)
            or (path.suffix in WORKFLOW_EXTENSIONS and rel.startswith(WORKFLOW_DIR + '/'))
        )
        if not wanted or _is_skipped(rel):
            continue
        try:
            lines = len(path.read_text(encoding='utf-8', errors='replace').splitlines())
        except OSError:
            continue
        entry = {'rel': rel, 'lines': lines}
        if any(rel == d or rel.startswith(d + '/') for d in METADATA_ONLY_DIRS):
            metadata_only.append(entry)
        else:
            scan.append(entry)
    return scan, metadata_only


def read_version_php(plugin_dir):
    """Parse the handful of version.php fields that belong in the report header."""
    path = plugin_dir / 'version.php'
    info = {}
    if not path.is_file():
        return info
    content = path.read_text(encoding='utf-8', errors='replace')
    for field in ('component', 'release'):
        match = re.search(rf"\$plugin->{field}\s*=\s*'([^']*)'", content)
        if match:
            info[field] = match.group(1)
    for field in ('version', 'requires'):
        match = re.search(rf'\$plugin->{field}\s*=\s*(\d+)', content)
        if match:
            info[field] = match.group(1)
    return info


def build_inventory(plugin_dir, scan, metadata_only):
    """Attack-surface facts that the report header states and the prompts rely on."""
    entry_points, external_ws = [], []
    for entry in scan:
        path = plugin_dir / entry['rel']
        if path.suffix != '.php':
            continue
        content = path.read_text(encoding='utf-8', errors='replace')
        if re.search(r'require(_once)?\s*\(?[^;]*config\.php', content):
            entry_points.append(entry['rel'])
        if entry['rel'].startswith('classes/external/'):
            external_ws.append(entry['rel'])

    # Behat .feature files are counted straight from disk: collect_files() keeps only the
    # scannable extensions, so reading them from metadata_only would always find none.
    features = [
        str(p.relative_to(plugin_dir)) for p in sorted((plugin_dir / 'tests').rglob('*.feature'))
        if p.is_file() and not _is_skipped(str(p.relative_to(plugin_dir)))
    ]
    test_files = [e['rel'] for e in metadata_only if e['rel'].startswith('tests/')] + features
    return {
        'files_scanned': len(scan),
        'lines_scanned': sum(e['lines'] for e in scan),
        'entry_points': entry_points,
        'external_ws': external_ws,
        'has_privacy': (plugin_dir / 'classes' / 'privacy').is_dir(),
        'has_backup': (plugin_dir / 'backup' / 'moodle2').is_dir(),
        'has_access': (plugin_dir / 'db' / 'access.php').is_file(),
        'has_thirdparty': (plugin_dir / 'thirdpartylibs.xml').is_file(),
        'tests_dir_files': len(test_files),
        'phpunit_tests': len([f for f in test_files if f.endswith('_test.php')]),
        'behat_features': len(features),
    }


# --------------------------------------------------------------------------- #
#  Phase A — deterministic collection                                          #
# --------------------------------------------------------------------------- #

def run_phpstan(plugin_dir, level):
    """PHPStan with JSON output. Returns (messages, error_or_None).

    The NEON is built here rather than shelling out to phpstan.sh because that wrapper
    prints its human-readable table and offers no --error-format passthrough; the config
    itself is the same five parameters.
    """
    if not PHPSTAN_BIN.is_file():
        return [], 'phpstan não instalado (rode composer install em phpstan/)'

    paths = []
    if (plugin_dir / 'classes').is_dir():
        paths.append(str(plugin_dir / 'classes'))
    for name in ('lib.php', 'locallib.php', 'renderer.php', 'externallib.php'):
        if (plugin_dir / name).is_file():
            paths.append(str(plugin_dir / name))
    if not paths:
        return [], None

    neon = tempfile.NamedTemporaryFile('w', suffix='.neon', delete=False)
    neon.write('parameters:\n')
    neon.write(f'    level: {level}\n')
    neon.write('    phpVersion: 80200\n')
    neon.write('    paths:\n')
    for p in paths:
        neon.write(f'        - {p}\n')
    neon.write('    moodle:\n')
    neon.write(f'        rootDirectory: {MOODLE_ROOT}\n')
    neon.close()

    try:
        proc = subprocess.run(
            [str(PHPSTAN_BIN), 'analyse', '-c', neon.name,
             '--memory-limit=2G', '--no-progress', '--error-format=json'],
            capture_output=True, text=True, timeout=900,
        )
        data = json.loads(proc.stdout or '{}')
    except (subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        return [], f'phpstan falhou: {exc}'
    finally:
        os.unlink(neon.name)

    messages = []
    for filepath, info in data.get('files', {}).items():
        try:
            rel = str(Path(filepath).relative_to(plugin_dir))
        except ValueError:
            rel = filepath
        for msg in info.get('messages', []):
            identifier = msg.get('identifier') or ''
            if identifier in PHPSTAN_NOISE_IDENTIFIERS:
                continue
            messages.append({
                'file': rel,
                'line': msg.get('line'),
                'message': msg.get('message', ''),
                'identifier': identifier,
            })
    return messages, None


def check_thirdparty_libs(plugin_dir):
    """Bundled third-party libraries and their pinned versions.

    Not a vulnerability by itself — it hands the AI triage the facts it needs to judge
    whether a pinned version is old enough to matter. No tool in this repo checked this
    before, and an outdated bundled library is a real CVE surface.
    """
    path = plugin_dir / 'thirdpartylibs.xml'
    if not path.is_file():
        return []
    content = path.read_text(encoding='utf-8', errors='replace')
    libs = []
    for block in re.findall(r'<library>(.*?)</library>', content, re.S):
        def field(name):
            match = re.search(rf'<{name}>(.*?)</{name}>', block, re.S)
            return match.group(1).strip() if match else ''
        libs.append({
            'name': field('name'),
            'version': field('version'),
            'location': field('location'),
        })
    return libs


def run_moodlecheck(plugin_dir):
    """local_moodlecheck (PHPDoc) — opt-in, release-readiness rather than security."""
    cli = MOODLE_DOCROOT / 'local' / 'moodlecheck' / 'cli' / 'moodlecheck.php'
    if not cli.is_file():
        return [], 'local_moodlecheck não instalado'
    try:
        proc = subprocess.run(
            ['php', str(cli), f'--path={plugin_dir}', '--format=text'],
            capture_output=True, text=True, timeout=300,
        )
    except subprocess.TimeoutExpired:
        return [], 'moodlecheck: timeout'
    return [ln for ln in proc.stdout.splitlines() if ln.strip()], None


# --------------------------------------------------------------------------- #
#  Phase B — AI triage of deterministic output                                 #
# --------------------------------------------------------------------------- #

TRIAGE_PROMPT = """Você recebe mensagens do PHPStan sobre um plugin Moodle. Classifique CADA uma.

Contexto que importa: Moodle usa stdClass do $DB em todo lugar, arrays sem generics e APIs
legadas. Muita mensagem de PHPStan é idioma normal de Moodle, não bug. Outras são bug real
sério (API que não existe, comparação sempre falsa, acesso a propriedade de algo que pode
ser false, retorno faltando).

Leia o código ao redor de cada linha citada antes de decidir. Use as ferramentas de leitura.

Classifique cada mensagem como:
- "real_bug": defeito verdadeiro que pode quebrar em produção
- "security_relevant": defeito verdadeiro COM consequência de segurança
- "moodle_idiom_noise": idioma normal de Moodle, PHPStan sendo pedante

Responda APENAS com um array JSON, um objeto por mensagem, na mesma ordem:
[{"index": 0, "verdict": "real_bug", "reason": "uma frase"}]

Mensagens:
"""


def triage_phpstan(messages, plugin_dir, franken, model, fallback, rules, jobs, use_cache,
                    clock=None):
    """Classify PHPStan messages in chunks; returns messages with a 'verdict' key."""
    if not messages:
        return []

    chunks = [messages[i:i + 25] for i in range(0, len(messages), 25)]

    def run_chunk(chunk):
        def compute():
            listing = '\n'.join(
                f'{i}. {m["file"]}:{m["line"]} [{m["identifier"]}] {m["message"]}'
                for i, m in enumerate(chunk)
            )
            try:
                text = call_claude(TRIAGE_PROMPT + listing, plugin_dir, model,
                                   fallback, rules)
                verdicts = extract_json(text)
            except Exception as exc:
                print(f'  aviso: triagem de um bloco falhou ({exc})', file=sys.stderr)
                raise Uncached([])
            out = []
            for item in verdicts:
                idx = item.get('index')
                if not isinstance(idx, int) or not 0 <= idx < len(chunk):
                    continue
                entry = dict(chunk[idx])
                entry['verdict'] = item.get('verdict', 'moodle_idiom_noise')
                entry['reason'] = item.get('reason', '')
                out.append(entry)
            return out

        return cached(CACHE_DIR, franken, 'triage', hash_key(PROMPT_VERSION, chunk),
                     use_cache, compute)

    results = run_parallel(chunks, jobs, run_chunk, 'bloco',
                           detail_fn=lambda r: f'{len(r)} classificado(s)', clock=clock)
    triaged = []
    for result in results:
        triaged.extend(result)
    return triaged


# --------------------------------------------------------------------------- #
#  Phase C — AI semantic scan                                                  #
# --------------------------------------------------------------------------- #

SCAN_PROMPT = """Você é um revisor de plugins Moodle. Analise os arquivos listados abaixo
procurando achados dos QUATRO tipos do catálogo do seu prompt de sistema: vulnerabilidades de
segurança (Camadas 1-3) e defeitos de qualidade, conformidade e boa prática (Camada 4).

Segurança vem primeiro: um XSS ou um acesso indevido pesa mais que qualquer achado de
qualidade. Mas não pare aí — a nota pública do MDL Shield, que esta auditoria imita, conta os
quatro tipos, e um único "low" de qualidade já tira o A+.

Leia cada arquivo por completo com a ferramenta Read. Você PODE e DEVE ler outros arquivos do
plugin (Grep/Glob/Read) quando precisar confirmar um achado — seguir a cadeia de chamada é o
que separa achado real de suposição. Para confirmar como o core se comporta (o que uma API
exige, se uma função existe na versão mínima do plugin), leia o código do core do Moodle.

Procure ativamente, além das regras de segurança:
- o mesmo dado sensível exposto por dois caminhos com decisões de acesso diferentes
  (L2-AUTHZ-1);
- estados que a escrita deixa criar e a leitura rejeita para sempre (L4-ROB-2);
- formulários sem validation() para limites que, violados, quebram a atividade (L4-ROB-1);
- APIs do core contornadas (L4-API-*), Privacy Provider declarando menos do que o plugin
  grava (L4-PRIV-1), texto fixo visível ao usuário (L4-HYG-1).

Seja conservador: só reporte o que tiver certeza. Nada de formatação de código nem PHPDoc.
Para achados que não são de segurança, aplique a régua low/info do catálogo à risca: "low"
exige cenário concreto de falha, contorno de API do core ou descumprimento da Privacy API;
o resto é "info". Severidade "medium" ou acima é só para "security".

Responda APENAS com um array JSON (vazio se nada encontrado):
[{
  "title": "título curto",
  "finding_type": "security | code_quality | compliance | best_practice",
  "severity": "critical|high|medium|low|info",
  "category": "security: uma das 15 categorias oficiais; demais tipos: vocabulário da Camada 4",
  "rule_id": "id da regra do catálogo, ex. L2-XSS-1, L4-ROB-2",
  "file": "caminho/relativo.php",
  "line": 123,
  "extra_locations": [{"file": "outro.mustache", "line": 95}],
  "description": "o que está errado e por quê",
  "exploitable_by": "security: quem consegue explorar; demais tipos: em que situação o defeito aparece",
  "impact": "blast radius concreto",
  "mitigations": "proteções que JÁ existem e limitam o impacto (string vazia se nenhuma)",
  "recommendation": "correção específica"
}]

"file"/"line" apontam a ocorrência PRINCIPAL; use "extra_locations" para as demais ocorrências
do mesmo problema. O trecho de código é extraído automaticamente do arquivo — não o copie.

Arquivos deste lote:
"""


def build_batches(scan_files, batch_lines):
    """Pack files into line-budgeted batches, keeping same-directory files together."""
    ordered = sorted(scan_files, key=lambda e: (str(Path(e['rel']).parent), e['rel']))
    batches, current, total = [], [], 0
    for entry in ordered:
        if current and total + entry['lines'] > batch_lines:
            batches.append(current)
            current, total = [], 0
        current.append(entry)
        total += entry['lines']
    if current:
        batches.append(current)
    return batches


def _batch_key(plugin_dir, batch):
    """Content hash, so fixing one file only invalidates the batches containing it."""
    contents = []
    for entry in batch:
        try:
            contents.append((entry['rel'], (plugin_dir / entry['rel']).read_bytes()))
        except OSError:
            contents.append((entry['rel'], b''))
    return hash_key(PROMPT_VERSION, contents)


def scan_batches(batches, plugin_dir, franken, model, fallback, rules, jobs, use_cache,
                 clock=None):
    def run_batch(batch):
        def compute():
            listing = '\n'.join(f'- {e["rel"]} ({e["lines"]} linhas)' for e in batch)
            try:
                text = call_claude(SCAN_PROMPT + listing, plugin_dir, model, fallback, rules)
                findings = extract_json(text)
                return findings if isinstance(findings, list) else []
            except Exception as exc:
                print(f'  aviso: um lote falhou ({exc})', file=sys.stderr)
                raise Uncached([])

        return cached(CACHE_DIR, franken, 'scan', _batch_key(plugin_dir, batch),
                     use_cache, compute)

    results = run_parallel(batches, jobs, run_batch, 'lote',
                           detail_fn=lambda r: f'{len(r)} candidato(s)', clock=clock)
    all_findings = []
    for result in results:
        all_findings.extend(result)
    return all_findings


# --------------------------------------------------------------------------- #
#  Phase D — verification                                                      #
# --------------------------------------------------------------------------- #

VERIFY_PROMPT = """Verifique se este achado de segurança em um plugin Moodle é REAL e EXPLORÁVEL.

Leia o código citado e o que for necessário ao redor. Seja cético: a maioria dos candidatos
não sobrevive a uma leitura cuidadosa.

Lembre que em Moodle professor e admin são papéis CONFIÁVEIS por design. Falha que só um
professor dispara é no máximo "low", a não ser que atinja dados fora do curso dele.

Se o defeito é real mas NÃO é de segurança (ninguém ganha acesso, dado ou poder indevido —
o pior desfecho é a funcionalidade quebrar ou ficar frágil), confirme reclassificando:
"finding_type" com um dos tipos não-segurança do catálogo e "category" do vocabulário da
Camada 4, e severidade pela régua low/info dele.

Responda APENAS com JSON:
{"verdict": "confirmed|refuted", "severity": "critical|high|medium|low|info",
 "finding_type": "security | code_quality | compliance | best_practice",
 "category": "categoria final",
 "reason": "por que confirma ou refuta, citando o código",
 "poc": "passo a passo da exploração, se confirmado como security"}

Achado:
"""

VERIFY_QUALITY_PROMPT = """Verifique se este achado de qualidade, conformidade ou boa prática
em um plugin Moodle é REAL. Ele não é uma alegação de vulnerabilidade: a pergunta não é "dá
para explorar?", e sim "o defeito existe e importa?".

Leia o código citado, o que for necessário ao redor e, quando o achado depender de como o
core se comporta (o que uma API exige, se uma função existe na versão mínima declarada em
$plugin->requires), o código do core do Moodle. Seja cético: refute quando
- o código já trata o caso em outro lugar (validação no servidor, wrapper que aplica a regra,
  guarda em outra camada);
- o padrão é o recomendado pelo próprio core ou pelo template oficial, ou não existe API do
  core para fazer aquilo;
- o "defeito" depende de uma situação que o plugin não deixa acontecer.

Se confirmar, decida a severidade pela régua do catálogo — é ela que separa A de A+:
- "low": cenário concreto de falha (entrada ou configuração real → resultado errado,
  funcionalidade quebrada, dado perdido ou incoerente), contorno de API do core que existe
  para aquilo, ou descumprimento da Privacy API;
- "info": higiene sem cenário de falha (código morto, fragilidade hipotética a mudança futura,
  ausência de testes).
Nunca acima de "low". Se durante a leitura você perceber que o defeito é na verdade de
segurança (alguém ganha acesso, dado ou poder indevido), confirme com "finding_type":
"security", uma das 15 categorias oficiais e a severidade de segurança correspondente.

Responda APENAS com JSON:
{"verdict": "confirmed|refuted", "severity": "low|info (ou severidade de segurança)",
 "finding_type": "code_quality | compliance | best_practice | security",
 "category": "categoria final",
 "reason": "por que confirma ou refuta, citando o código",
 "poc": "cenário concreto: passos, entrada ou configuração → resultado errado"}

Achado:
"""


def normalize_finding(finding):
    """Coerce the type/severity pair into the combinations the grade understands.

    Anything without a known finding_type is treated as security (every finding was security
    before the four types existed, so older JSON reports stay meaningful), and a non-security
    finding is capped at low — the catalog reserves medium and above for security.
    """
    if finding.get('finding_type') not in FINDING_TYPES:
        finding['finding_type'] = 'security'
    if finding.get('severity') not in SEVERITY_ORDER:
        finding['severity'] = 'info'
    if finding['finding_type'] != 'security' and finding['severity'] in ('critical', 'high',
                                                                         'medium'):
        finding['severity'] = 'low'
    return finding


def verify_findings(findings, plugin_dir, franken, model, fallback, rules, jobs, use_cache,
                    clock=None):
    """Confirm or refute each candidate independently.

    Deliberately one call per candidate rather than batched: the sceptical, focused reading
    is what produces the conservative tone, and bundling several findings into one prompt
    dilutes it. The cost is bounded by caching instead.

    Security candidates get the exploitability question; the other three types get a prompt
    about whether the defect is real and where it falls on the low/info line, since asking
    "can this be exploited?" about a missing form validation refutes everything. Either
    verifier may move a finding to another type. Static-check facts marked `deterministic`
    are taken as confirmed without a call.
    """
    def run_one(finding):
        if finding.get('deterministic'):
            finding['verdict'] = 'confirmed'
            finding['verify_reason'] = 'fato determinístico, sem verificação por IA'
            finding.setdefault('poc', '')
            return normalize_finding(finding)

        prompt = (VERIFY_PROMPT if finding.get('finding_type', 'security') == 'security'
                  else VERIFY_QUALITY_PROMPT)

        def compute():
            payload = json.dumps(finding, ensure_ascii=False, indent=2)
            try:
                text = call_claude(prompt + payload, plugin_dir, model,
                                   fallback, rules)
                result = extract_json(text)
                # extract_json() is shared with the phase C scan, which expects a JSON
                # array; it returns whichever bracket type it finds first in the raw
                # text, so a stray "[" earlier in the model's prose (e.g. a footnote-
                # style reference) can make it return a list here instead of the single
                # object VERIFY_PROMPT asks for. Treat that the same as a parse failure.
                if not isinstance(result, dict):
                    raise ValueError(f'esperava um objeto JSON, recebeu {type(result).__name__}')
                return result
            except Exception as exc:
                # A failed verification must never silently promote an unverified candidate.
                return {'verdict': 'refuted', 'reason': f'verificação falhou: {exc}'}

        # A failed verification is not cached: the next run should retry it, not inherit
        # a refusal caused by a spend limit or a network blip.
        key = hash_key(PROMPT_VERSION, finding.get('finding_type'), finding.get('title'),
                       finding.get('file'), finding.get('line'), finding.get('description'))
        path = cache_path(CACHE_DIR, franken, 'verify', key)
        result = None
        if use_cache and path.is_file():
            try:
                result = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                result = None
        if result is None:
            result = compute()
            transient = str(result.get('reason', '')).startswith('verificação falhou')
            if use_cache and not transient:
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps(result, ensure_ascii=False))
                except OSError:
                    pass

        finding['verdict'] = result.get('verdict', 'refuted')
        finding['verify_reason'] = result.get('reason', '')
        finding['poc'] = result.get('poc', '')
        if result.get('finding_type') in FINDING_TYPES:
            finding['finding_type'] = result['finding_type']
        if result.get('category'):
            finding['category'] = result['category']
        if result.get('severity') in SEVERITY_ORDER:
            finding['severity'] = result['severity']
        return normalize_finding(finding)

    def detail(finding):
        title = (finding.get('title') or '')[:40]
        return f'{finding.get("verdict")} — {title}'

    return run_parallel(findings, jobs, run_one, 'achado', detail_fn=detail, clock=clock)


# --------------------------------------------------------------------------- #
#  Phase E — dedup                                                             #
# --------------------------------------------------------------------------- #

DEDUP_PROMPT = """Você recebe uma lista de achados (de segurança, qualidade, conformidade e boa
prática) de uma auditoria de plugin Moodle. Cada achado veio de um lote de varredura
independente (Fase C), de uma checagem determinística ou do PHPStan, e foi verificado
isoladamente (Fase D) — nenhum lote ou verificação teve visibilidade dos demais achados.
Por isso, o MESMO problema pode ter sido relatado mais de uma vez: uma vez por cada
arquivo que participa do mesmo fluxo (ex.: a rota de leitura e a rota de escrita do mesmo
bug de autorização), ou duas vezes dentro do mesmo arquivo quando duas partes dele exibem o
mesmo sintoma (ex.: uma função que declara metadata incompleta e outra que exporta os dados
de acordo com essa mesma metadata incompleta).

Agrupe os achados que descrevem a MESMA causa raiz — ou seja, corrigir um automaticamente
resolve o outro. NÃO agrupe achados que só compartilham categoria, arquivo ou severidade por
coincidência; eles precisam ser genuinamente o mesmo problema.

Para cada grupo de 2+ achados duplicados, escolha "primary": o índice do achado com a
descrição, prova de conceito e correção recomendada mais completas e específicas — é esse
que sobrevive no relatório final; os demais do grupo são consolidados dentro dele.

Responda APENAS com JSON:
{"groups": [{"indices": [0, 3], "primary": 0, "reason": "uma frase: qual é a causa raiz comum"}]}

Se nenhum achado for duplicado, responda {"groups": []}.

Achados (um por índice):
"""


def dedupe_findings(findings, plugin_dir, franken, model, fallback, rules, use_cache,
                    clock=None):
    """Merges findings that independently describe the same root cause.

    One call over the whole list rather than the O(n^2) alternative of comparing every
    pair — cheap because this runs on the already-small confirmed list (Phase D has
    already filtered out everything that didn't survive verification), not on the raw
    candidate count.
    """
    if len(findings) < 2:
        return findings

    payload = json.dumps([
        {
            'index': i,
            'title': f.get('title'),
            'file': f.get('file'),
            'line': f.get('line'),
            'finding_type': f.get('finding_type'),
            'category': f.get('category'),
            'severity': f.get('severity'),
            # Trimmed: the model only needs enough to judge "same root cause", not the
            # full write-up — keeps this single call cheap regardless of how detailed
            # Phase C's descriptions were.
            'description': (f.get('description') or '')[:400],
        }
        for i, f in enumerate(findings)
    ], ensure_ascii=False, indent=2)

    def compute():
        try:
            text = call_claude(DEDUP_PROMPT + payload, plugin_dir, model, fallback, rules)
            data = extract_json(text)
            groups = data.get('groups') if isinstance(data, dict) else None
            return groups if isinstance(groups, list) else []
        except Exception as exc:
            print(f'  aviso: deduplicação falhou ({exc})', file=sys.stderr)
            raise Uncached([])

    key = hash_key(PROMPT_VERSION, [
        (f.get('title'), f.get('file'), f.get('line'), f.get('description'))
        for f in findings
    ])
    groups = cached(CACHE_DIR, franken, 'dedupe', key, use_cache, compute)

    merged_away = set()
    for group in groups:
        if not isinstance(group, dict):
            continue
        indices = [i for i in group.get('indices', [])
                  if isinstance(i, int) and 0 <= i < len(findings)]
        if len(indices) < 2 or any(i in merged_away for i in indices):
            # A finding already merged into an earlier group is not merged a second
            # time — keeps the result a simple partition instead of chasing
            # overlapping/chained groups the model should not have produced anyway.
            continue

        primary = group.get('primary')
        if primary not in indices:
            primary = min(indices)
        others = [i for i in indices if i != primary]

        survivor = findings[primary]
        extras = list(survivor.get('extra_locations') or [])
        for i in others:
            other = findings[i]
            extras.append({'file': other.get('file'), 'line': other.get('line')})
            extras.extend(other.get('extra_locations') or [])
        survivor['extra_locations'] = extras

        # The more severe of the merged verdicts wins — two independent readings of the
        # same root cause disagreeing on severity is not a reason to under-report it.
        severities = [survivor.get('severity', 'info')]
        severities += [findings[i].get('severity', 'info') for i in others]
        survivor['severity'] = min(
            severities,
            key=lambda s: SEVERITY_ORDER.index(s) if s in SEVERITY_ORDER else len(SEVERITY_ORDER),
        )

        # Same rule for the type: if any reading saw a security impact, the merged finding
        # is a security one.
        if any(findings[i].get('finding_type') == 'security' for i in others):
            survivor['finding_type'] = 'security'
        normalize_finding(survivor)

        reason = (group.get('reason') or '').strip()
        if reason:
            survivor['dedup_note'] = reason

        merged_away.update(others)

    if not merged_away:
        return findings
    return [f for i, f in enumerate(findings) if i not in merged_away]


# --------------------------------------------------------------------------- #
#  Phase F — grade and report                                                  #
# --------------------------------------------------------------------------- #

LANG_BY_SUFFIX = {'.php': 'php', '.js': 'javascript', '.mustache': 'html',
                  '.css': 'css', '.xml': 'xml', '.md': 'markdown', '.yml': 'yaml',
                  '.yaml': 'yaml'}


def extract_snippet(plugin_dir, rel, line, context=3):
    """Pull the offending lines out of the file, with a little context around them.

    Done in Python rather than asking the model to quote the code: the file on disk is the
    source of truth, so the snippet can never drift from what is actually there.
    """
    if not rel or not isinstance(line, int) or line < 1:
        return None
    path = plugin_dir / rel
    if not path.is_file():
        return None
    try:
        lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError:
        return None
    start = max(0, line - 1 - context)
    end = min(len(lines), line + context)
    if start >= end:
        return None
    numbered = []
    for offset, text in enumerate(lines[start:end], start=start + 1):
        marker = '>' if offset == line else ' '
        numbered.append(f'{marker} {offset:>5} | {text}')
    return {
        'lang': LANG_BY_SUFFIX.get(path.suffix, ''),
        'code': '\n'.join(numbered),
    }


def attach_snippets(findings, plugin_dir):
    """Decorate every finding (and each of its extra locations) with its code snippet."""
    for finding in findings:
        finding['snippet'] = extract_snippet(plugin_dir, finding.get('file'),
                                             finding.get('line'))
        for extra in finding.get('extra_locations') or []:
            if isinstance(extra, dict):
                extra['snippet'] = extract_snippet(plugin_dir, extra.get('file'),
                                                   extra.get('line'))
    return findings


def ensure_report_dir(plugin_dir):
    """Return <plugin>/.plans/security-audit, creating it and gitignoring .plans/ when needed.

    The report is written inside the plugin on purpose, but it must never become a tracked
    file: a security report naming exploitable lines does not belong in a public plugin
    repository. So the .gitignore entry is guaranteed here rather than assumed.
    """
    report_dir = plugin_dir / REPORT_SUBDIR
    report_dir.mkdir(parents=True, exist_ok=True)

    gitignore = plugin_dir / '.gitignore'
    if gitignore.is_file():
        existing = gitignore.read_text(encoding='utf-8')
        entries = {line.strip().rstrip('/') for line in existing.splitlines()}
        if GITIGNORE_ENTRY not in entries:
            separator = '' if existing.endswith('\n') else '\n'
            gitignore.write_text(
                f'{existing}{separator}\n{GITIGNORE_COMMENT}\n{GITIGNORE_ENTRY}\n',
                encoding='utf-8')
            print(f'  {GITIGNORE_ENTRY} adicionado ao .gitignore do plugin')
    else:
        gitignore.write_text(f'{GITIGNORE_COMMENT}\n{GITIGNORE_ENTRY}\n', encoding='utf-8')
        print(f'  .gitignore criado com {GITIGNORE_ENTRY}')

    return report_dir


def severity_counts(findings):
    counts = {s: 0 for s in SEVERITY_ORDER}
    for finding in findings:
        severity = finding.get('severity', 'info')
        counts[severity] = counts.get(severity, 0) + 1
    return counts


def compute_grade(findings):
    """Grade dominated by the worst finding, not by a sum of penalties.

    An additive score misrepresents a review: eight low-severity hygiene gaps are not
    "worse" than one stored XSS, yet any penalty-sum model says exactly that. So the worst
    severity present sets a ceiling, and only the count of low findings refines it.

    Every finding type counts, the way MDL Shield's public grade does. Re-calibrated
    (2026-10-02) on 18 public reviews (mdlshield.com/reviews, Sep-Oct 2026) that label each
    finding with its type: 44 lows, of which 26 code quality, 7 best practice, 5 compliance
    and only 5 security. The rule they show:
      - any low, of any type, caps the grade at A: tool_aiagent has a single finding (a low
        code-quality unserialize()) and got A;
      - info does not: mod_profilefield (0 low + 1 info best practice) is the only A+ in the
        sample;
      - one security medium gives B+ (availability_xpstore: 1 medium + 3 low);
      - 1 to 5 lows stayed A in every one of the 17 A reviews.

    Earlier data points (2026-09-04 calibration, kept because they cover the rest of the
    scale): 5 low graded A (tiny_fontcolor) and B+ (local_differentiator); 8 low -> B+
    (quizaccess_campla, local_information_center); 1 high + 1 medium + 1 low + 1 info -> D
    (filter_playerhud 2026-08-02); 1 high + 3 medium + 4 low -> C (block_playerhud
    2026-04-29). MDL Shield's grade is not a pure function of the counts — those two pairs
    prove it weighs each finding's real impact — so expect occasional disagreement at the
    boundaries (a single medium, 5-6 lows) as an inherent limit of a label-only formula.
    """
    counts = severity_counts(findings)
    if counts['critical']:
        return 'F', 'achado crítico presente'
    if counts['high']:
        return 'D', 'achado de severidade alta presente'
    if counts['medium']:
        return 'B+', 'achado de severidade média presente'
    if counts['low'] >= 6:
        return 'B+', f'{counts["low"]} achados de severidade baixa'
    if counts['low']:
        return 'A', f'{counts["low"]} achado(s) de severidade baixa'
    if counts['info']:
        return 'A+', 'só achados informativos, que não tiram o A+'
    return 'A+', 'nenhum achado'


def type_counts(findings):
    """{finding_type: {severity: count}} for the report's type-by-severity table."""
    table = {t: {s: 0 for s in SEVERITY_ORDER} for t in FINDING_TYPES}
    for finding in findings:
        ftype = finding.get('finding_type', 'security')
        severity = finding.get('severity', 'info')
        if ftype in table and severity in table[ftype]:
            table[ftype][severity] += 1
    return table


def phpstan_candidates(triaged):
    """PHPStan messages the triage judged real, as candidates for Phase D.

    MDL Shield reports a real bug (a setting read that does not exist, an always-false
    comparison) as a code-quality finding that counts against the grade, so a triaged real
    bug can no longer live only in a side table. Each one goes through the same verification
    as the AI scan's candidates; the table in the report stays as the raw triage record.
    """
    candidates = []
    for msg in triaged:
        verdict = msg.get('verdict')
        if verdict not in ('real_bug', 'security_relevant'):
            continue
        security = verdict == 'security_relevant'
        candidates.append({
            'title': f'PHPStan: {msg.get("message", "")[:90]}',
            'finding_type': 'security' if security else 'code_quality',
            'severity': 'low',
            'category': 'insecure_config_management' if security else 'robustness',
            'rule_id': f'PHPStan {msg.get("identifier") or ""}'.strip(),
            'file': msg.get('file'),
            'line': msg.get('line'),
            'extra_locations': [],
            'description': f'{msg.get("message", "")}\n\nTriagem: {msg.get("reason", "")}',
            'exploitable_by': '',
            'impact': '',
            'mitigations': '',
            'recommendation': '',
            'source': 'phpstan',
        })
    return candidates


def _render_finding(add, index, finding):
    """One finding, in the order a reader needs it: what, where, why, proof, fix."""
    severity = finding.get('severity', 'info')
    ftype = finding.get('finding_type', 'security')
    add(f'### {index}. {finding.get("title", "(sem título)")}')
    add('')
    add('| | |')
    add('|---|---|')
    add(f'| **Severidade** | `{severity}` |')
    add(f'| **Tipo** | {FINDING_TYPE_LABELS.get(ftype, ftype)} |')
    add(f'| **Categoria** | `{finding.get("category", "?")}` |')
    add(f'| **Regra** | `{finding.get("rule_id", "?")}` |')
    if finding.get('exploitable_by'):
        label = 'Explorável por' if ftype == 'security' else 'Quando aparece'
        add(f'| **{label}** | {finding["exploitable_by"]} |')
    source = {'static': 'checagem determinística', 'phpstan': 'PHPStan'}.get(
        finding.get('source'))
    if source:
        add(f'| **Origem** | {source} |')
    add('')

    add('**Local afetado**')
    add('')
    add(f'1. `{finding.get("file")}:{finding.get("line")}`')
    extras = [e for e in (finding.get('extra_locations') or []) if isinstance(e, dict)]
    for offset, extra in enumerate(extras, start=2):
        add(f'{offset}. `{extra.get("file")}:{extra.get("line")}`')
    add('')

    if finding.get('dedup_note'):
        add(f'> **Consolidado**: a varredura relatou este achado mais de uma vez, de ângulos'
            f' de código diferentes. {finding["dedup_note"]}')
        add('')

    snippet = finding.get('snippet')
    if snippet and snippet.get('code'):
        add('**Código**')
        add('')
        add(f'```{snippet.get("lang", "")}')
        add(snippet['code'])
        add('```')
        add('')
    for extra in extras:
        extra_snippet = extra.get('snippet')
        if extra_snippet and extra_snippet.get('code'):
            add(f'`{extra.get("file")}:{extra.get("line")}`')
            add('')
            add(f'```{extra_snippet.get("lang", "")}')
            add(extra_snippet['code'])
            add('```')
            add('')

    if finding.get('description'):
        add('**Descrição**')
        add('')
        add(finding['description'])
        add('')
    if finding.get('impact'):
        add('**Avaliação de impacto**')
        add('')
        add(finding['impact'])
        add('')
    if finding.get('mitigations'):
        add('**Mitigações já presentes**')
        add('')
        add(finding['mitigations'])
        add('')
    if finding.get('poc'):
        add('**Prova de conceito**' if ftype == 'security' else '**Cenário de falha**')
        add('')
        add(finding['poc'])
        add('')
    if finding.get('recommendation'):
        add('**Correção recomendada**')
        add('')
        add(finding['recommendation'])
        add('')


def render_report(ctx):
    """Assemble the Markdown report, ordered the way a code review is normally read."""
    inv, confirmed, refuted = ctx['inventory'], ctx['confirmed'], ctx['refuted']
    narrative = ctx.get('narrative') or {}
    counts = severity_counts(confirmed)
    grade, grade_reason = ctx['grade'], ctx.get('grade_reason', '')
    version = ctx['version']

    out = []
    add = out.append

    add(f'# Relatório de auditoria — {ctx["franken"]}')
    add('')
    if narrative.get('purpose'):
        add(f'*{narrative["purpose"]}*')
        add('')

    # ---- Nota geral -------------------------------------------------------
    add('## Nota geral')
    add('')
    add(f'# {grade}')
    add('')
    if grade_reason:
        add(f'*{grade_reason}*')
        add('')
    if ctx.get('security_grade'):
        add(f'Nota contando só os achados de segurança: **{ctx["security_grade"]}**')
        add('')
    table = type_counts(confirmed)
    add('| Tipo | ' + ' | '.join(SEVERITY_ORDER) + ' |')
    add('|---|' + '---|' * len(SEVERITY_ORDER))
    for ftype in FINDING_TYPES:
        cells = [f'**{table[ftype][sev]}**' if table[ftype][sev] else '0'
                 for sev in SEVERITY_ORDER]
        add(f'| {FINDING_TYPE_LABELS[ftype]} | ' + ' | '.join(cells) + ' |')
    totals = [f'**{counts[sev]}**' if counts[sev] else '0' for sev in SEVERITY_ORDER]
    add('| **total** | ' + ' | '.join(totals) + ' |')
    add('')
    add('> A nota é **dominada pelo pior achado**, não por soma de penalidades: um `critical`'
        ' resulta em `F`, um `high` em `D`, um `medium` em `B+`; só de `low` a nota é `A`'
        ' (até 5) ou `B+` (6 ou mais); sem `low`, `A+` — achados `info` não tiram o A+.'
        ' **Os quatro tipos contam**, como na nota pública do MDL Shield: um único `low` de'
        ' qualidade de código já limita a nota a `A`. Calibrado sobre 18 revisões públicas do'
        ' MDL Shield — aproximação mais próxima possível, não garantia de nota idêntica: o'
        ' próprio MDL Shield já deu notas diferentes para a mesma contagem de achados.')
    add('')

    # ---- Sumário executivo ------------------------------------------------
    if narrative.get('executive_summary'):
        add('## Sumário executivo')
        add('')
        add(narrative['executive_summary'])
        add('')

    # ---- Metodologia ------------------------------------------------------
    add('## Metodologia')
    add('')
    add('**Escopo analisado**')
    add('')
    add(f'- **{inv["files_scanned"]} arquivos · {inv["lines_scanned"]} linhas** lidos a fundo')
    if version.get('release'):
        add(f'- Versão do plugin: {version.get("release")} (`{version.get("version", "?")}`)')
    if version.get('requires'):
        add(f'- Requer Moodle: `{version["requires"]}`')
    add(f'- Data da auditoria: {date.today().isoformat()}')
    add('')
    if narrative.get('methodology'):
        add('**O que foi examinado**')
        add('')
        add(narrative['methodology'])
        add('')

    add('**Superfície de ataque**')
    add('')
    add('| Item | Quantidade |')
    add('|---|---|')
    add(f'| Entry points (chamam `config.php`) | {len(inv["entry_points"])} |')
    add(f'| Web services (`classes/external/`) | {len(inv["external_ws"])} |')
    add(f'| Arquivos em `tests/` (testes, fixtures, geradores, contextos Behat) | {inv["tests_dir_files"]} |')
    add('')

    add('**Evidências de rigor**')
    add('')

    def check(flag):
        return '✅' if flag else '—'

    add(f'- {check(inv["has_privacy"])} Privacy API implementada')
    add(f'- {check(inv["has_access"])} Capabilities declaradas (`db/access.php`)')
    add(f'- {check(inv["has_backup"])} Backup/restore (`backup/moodle2/`)')
    add(f'- {check(inv["phpunit_tests"] > 0)} Testes PHPUnit ({inv["phpunit_tests"]} arquivos `*_test.php`)')
    add(f'- {check(inv["behat_features"] > 0)} Testes Behat ({inv["behat_features"]} features)')
    add('')

    add('**Dependências de terceiro**')
    add('')
    if ctx['libs']:
        add('| Biblioteca | Versão | Local |')
        add('|---|---|---|')
        for lib in ctx['libs']:
            add(f'| {lib["name"]} | `{lib["version"]}` | `{lib["location"]}` |')
        add('')
        add('> Biblioteca embarcada em versão antiga é superfície de CVE. Confira as versões'
            ' acima contra o upstream — nenhuma ferramenta local faz isso automaticamente.')
    else:
        add('Nenhuma biblioteca de terceiro empacotada.')
    add('')

    # ---- Achados ----------------------------------------------------------
    add('## Achados')
    add('')
    if not confirmed:
        add('Nenhum achado confirmado.')
        add('')
    else:
        ordered = sorted(confirmed, key=lambda f: (
            SEVERITY_ORDER.index(f.get('severity', 'info')),
            FINDING_TYPES.index(f.get('finding_type', 'security'))))
        for index, finding in enumerate(ordered, 1):
            _render_finding(add, index, finding)

    # ---- Pontos fortes ----------------------------------------------------
    strengths = [s for s in (narrative.get('strengths') or []) if isinstance(s, dict)]
    if strengths:
        add('## Pontos fortes')
        add('')
        add('Práticas verificadas no código durante a auditoria.')
        add('')
        for index, item in enumerate(strengths, 1):
            add(f'{index}. **{item.get("title", "")}** — {item.get("detail", "")}')
        add('')

    # ---- Bugs de código ---------------------------------------------------
    real_bugs = [m for m in ctx['phpstan']
                 if m.get('verdict') in ('real_bug', 'security_relevant')]
    add('## Bugs de código (PHPStan triado)')
    add('')
    add('Registro bruto da triagem: mensagens do PHPStan que a IA julgou bug real. Cada uma'
        ' também passou pela verificação da Fase D; as confirmadas aparecem em **Achados**,'
        ' com origem "PHPStan", e contam para a nota como qualidade de código. O ruído de'
        ' idioma Moodle foi descartado.')
    add('')
    if not real_bugs:
        add('Nenhum bug real após triagem.')
        add('')
    else:
        add('| Arquivo:linha | Veredito | Mensagem |')
        add('|---|---|---|')
        for msg in real_bugs:
            text = msg['message'].replace('|', '\\|')[:110]
            add(f'| `{msg["file"]}:{msg["line"]}` | `{msg["verdict"]}` | {text} |')
        add('')
    noise = len(ctx['phpstan']) - len(real_bugs)
    if noise:
        add(f'*{noise} mensagem(ns) classificada(s) como idioma normal de Moodle e'
            ' omitida(s).*')
        add('')

    # ---- Achados de performance ---------------------------------------------
    quality_findings = ctx.get('quality_findings') or []
    if quality_findings:
        add('## Achados de performance (formato antigo)')
        add('')
        add('Relatório gerado antes dos quatro tipos de achado: estas observações de N+1 não'
            ' passaram pela verificação nem entram na nota. Numa auditoria nova, viram achados'
            ' de boa prática verificados.')
        add('')
        for index, finding in enumerate(quality_findings, 1):
            _render_finding(add, index, finding)

    # ---- Descartados ------------------------------------------------------
    if refuted:
        add('## Descartados na verificação')
        add('')
        add('Candidatos que **não** sobreviveram ao passe de verificação. Listados para'
            ' transparência e calibragem — não são achados.')
        add('')
        for finding in refuted:
            add(f'- **{finding.get("title")}** (`{finding.get("file")}:'
                f'{finding.get("line")}`) — {finding.get("verify_reason", "")}')
        add('')

    # ---- Conclusão --------------------------------------------------------
    if narrative.get('conclusion'):
        add('## Conclusão')
        add('')
        add(narrative['conclusion'])
        add('')

    add('---')
    add('')
    add('Gerado por `moodle-security-audit` — ferramentas determinísticas (PHPStan e'
        ' checagens de padrão) + revisão por IA, com passe de verificação. Catálogo de regras'
        ' em `security-rules.md`.')
    add('')
    return '\n'.join(out)


NARRATIVE_PROMPT = """Escreva as seções narrativas do relatório de uma auditoria de plugin Moodle
que cobre segurança, qualidade de código, conformidade (Privacy API) e boas práticas. Você
pode ler o código para embasar o que afirmar — não invente nada.

Tudo em português do Brasil. Factual, sem elogio vazio e sem marketing.

Responda APENAS com JSON:
{
  "purpose": "1-2 frases dizendo o que o plugin faz (contexto para quem lê o relatório)",
  "executive_summary": "3-5 frases: postura geral de segurança, o que os achados de cada tipo \
significam na prática e o que NÃO foi encontrado. Se não houver achado grave, diga com \
clareza. Se a nota geral for menor que a nota só de segurança, explique que a diferença vem \
de achados de qualidade, conformidade ou boa prática.",
  "methodology": "2-3 frases descrevendo concretamente o que foi examinado — cite os \
diretórios e tipos de arquivo reais deste plugin (entry points, classes/external/, \
templates, AMD...).",
  "strengths": [
    {"title": "Nome curto da prática defensiva",
     "detail": "o que o plugin faz de certo, citando função/arquivo concreto"}
  ],
  "conclusion": "1-2 frases de fechamento."
}

De 3 a 8 itens em "strengths", só o que você realmente verificou no código (escopo por
instância, sesskey, locks, validação de URL, allow-list em ORDER BY, privacy provider,
backup/restore, testes...). Se não verificou, não liste.

Dados da auditoria:
"""


def generate_narrative(ctx, plugin_dir, model, fallback, rules):
    """Executive summary, methodology, strengths and conclusion — the prose sections."""
    payload = json.dumps({
        'component': ctx['franken'],
        'inventory': ctx['inventory'],
        'findings': [{k: f.get(k) for k in ('title', 'finding_type', 'severity', 'category',
                                            'file')}
                     for f in ctx['confirmed']],
        'grade': ctx['grade'],
        'security_grade': ctx.get('security_grade'),
    }, ensure_ascii=False, indent=2)
    try:
        text = call_claude(NARRATIVE_PROMPT + payload, plugin_dir, model, fallback,
                           rules, allow_tools=True)
        data = extract_json(text)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        print(f'  aviso: narrativa falhou ({exc})', file=sys.stderr)
        return {}


# --------------------------------------------------------------------------- #
#  main                                                                        #
# --------------------------------------------------------------------------- #

def append_usage_history(franken, stem, run_usage):
    """One JSON line per run, kept beside the cache so runs can be compared over time
    (e.g. before/after a model change) without having kept every report's --json."""
    path = CACHE_DIR / franken / 'usage-history.jsonl'
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a', encoding='utf-8') as fh:
            fh.write(json.dumps({'run': stem, **run_usage}, ensure_ascii=False) + '\n')
    except OSError:
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('plugin_dir')
    parser.add_argument('--model', default='claude-fable-5-1')
    parser.add_argument('--fallback-model', default='claude-opus-5-5')
    parser.add_argument('--phpstan-level', type=int, default=6)
    parser.add_argument('--no-phpstan', action='store_true',
                        help='pula a fase A (PHPStan + triagem) inteiramente')
    # 10k lines is roughly 130k tokens of code — comfortably inside the context window, and
    # 3x fewer calls than the original 3.5k. Each call re-pays the system prompt, so small
    # batches were spending quota on overhead rather than on analysis.
    parser.add_argument('--batch-lines', type=int, default=10000)
    parser.add_argument('--jobs', type=int, default=5)
    parser.add_argument('--with-moodlecheck', action='store_true')
    parser.add_argument('--no-verify', action='store_true')
    parser.add_argument('--no-cache', action='store_true')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--from-json', default=None,
                        help='re-renderiza o relatório de um JSON já gerado, sem refazer '
                             'a análise (para iterar o modelo do relatório sem gastar cota)')
    args = parser.parse_args()

    plugin_dir = Path(args.plugin_dir).resolve()
    if not plugin_dir.is_dir():
        print(f'erro: {plugin_dir} não existe', file=sys.stderr)
        return 1

    if shutil.which('claude') is None:
        print('erro: binário "claude" não encontrado no PATH', file=sys.stderr)
        return 1

    rules = RULES_FILE.read_text(encoding='utf-8') if RULES_FILE.is_file() else ''
    if not rules:
        print(f'erro: catálogo de regras ausente em {RULES_FILE}', file=sys.stderr)
        return 1

    if args.from_json:
        source = Path(args.from_json)
        if not source.is_file():
            print(f'erro: {source} não existe', file=sys.stderr)
            return 1
        ctx = json.loads(source.read_text(encoding='utf-8'))
        # Snippets are re-extracted from disk so the report always matches the current file.
        ctx['confirmed'] = attach_snippets(
            [normalize_finding(f) for f in ctx.get('confirmed', [])], plugin_dir)
        # JSON from before the four finding types kept N+1 observations apart, unverified
        # and outside the grade; they are still rendered in their own section, and the grade
        # is recomputed so an old JSON re-rendered today follows today's grading rule.
        ctx['quality_findings'] = attach_snippets(ctx.get('quality_findings', []), plugin_dir)
        ctx['grade'], ctx['grade_reason'] = compute_grade(ctx['confirmed'])
        ctx['security_grade'], _ = compute_grade(
            [f for f in ctx['confirmed'] if f.get('finding_type') == 'security'])
        if not ctx.get('narrative'):
            print('narrativa ausente no JSON — gerando (1 chamada)...')
            ctx['narrative'] = generate_narrative(ctx, plugin_dir, args.model,
                                                  args.fallback_model, rules)
            source.write_text(json.dumps(ctx, ensure_ascii=False, indent=2),
                              encoding='utf-8')
        report_dir = ensure_report_dir(plugin_dir)
        out_path = report_dir / f'{source.stem}.md'
        out_path.write_text(render_report(ctx), encoding='utf-8')
        print(f'Relatório re-renderizado: {out_path}')
        return 0

    version = read_version_php(plugin_dir)
    franken = version.get('component') or plugin_dir.name
    use_cache = not args.no_cache

    scan_files, metadata_only = collect_files(plugin_dir)
    if not scan_files:
        print('erro: nenhum arquivo analisável encontrado', file=sys.stderr)
        return 1
    inventory = build_inventory(plugin_dir, scan_files, metadata_only)

    print(f'Auditando {franken} — {inventory["files_scanned"]} arquivos, '
          f'{inventory["lines_scanned"]} linhas')
    print('')
    progress = ProgressWriter(PROGRESS_DIR / f'{franken}.json')
    clock = Clock(progress=progress)

    # Phase A
    if args.no_phpstan:
        clock.phase('A', 'PHPStan (pulado via --no-phpstan)')
        phpstan_msgs, phpstan_err = [], None
        clock.done('0 mensagem(ns)')
    else:
        clock.phase('A', f'PHPStan nível {args.phpstan_level}')
        phpstan_msgs, phpstan_err = run_phpstan(plugin_dir, args.phpstan_level)
        if phpstan_err:
            print(f'  aviso: {phpstan_err}', file=sys.stderr)
        clock.done(f'{len(phpstan_msgs)} mensagem(ns) após filtro de ruído')
    static_candidates = run_static_checks(plugin_dir, scan_files, version)
    print(f'  {len(static_candidates)} candidato(s) das checagens determinísticas')
    libs = check_thirdparty_libs(plugin_dir)
    if libs:
        print(f'  {len(libs)} biblioteca(s) de terceiro empacotada(s)')
    if args.with_moodlecheck:
        _, mc_err = run_moodlecheck(plugin_dir)
        if mc_err:
            print(f'  aviso: {mc_err}', file=sys.stderr)

    # Phase B
    triaged = []
    if phpstan_msgs:
        clock.phase('B', 'Triagem das mensagens do PHPStan')
        triaged = triage_phpstan(phpstan_msgs, plugin_dir, franken, args.model,
                                 args.fallback_model, rules, args.jobs, use_cache,
                                 clock=clock)
        real = sum(1 for m in triaged if m['verdict'] in ('real_bug', 'security_relevant'))
        clock.done(f'{real} bug(s) real(is), {len(triaged) - real} idioma Moodle')

    # Phase C
    batches = build_batches(scan_files, args.batch_lines)
    clock.phase('C', f'Varredura semântica em {len(batches)} lote(s)')
    all_candidates = scan_batches(batches, plugin_dir, franken, args.model,
                                  args.fallback_model, rules, args.jobs, use_cache,
                                  clock=clock)
    scan_count = len(all_candidates)
    # Every source feeds the same verification: the AI scan, the static checks (Phase A)
    # and the PHPStan messages the triage judged real (Phase B).
    candidates = [normalize_finding(dict(f, source=f.get('source', 'scan')))
                  for f in all_candidates if isinstance(f, dict)]
    candidates += [normalize_finding(f) for f in static_candidates]
    candidates += [normalize_finding(f) for f in phpstan_candidates(triaged)]
    by_type = {t: sum(1 for f in candidates if f['finding_type'] == t) for t in FINDING_TYPES}
    clock.done(f'{scan_count} da varredura, {len(candidates)} no total — '
               + ', '.join(f'{n} {FINDING_TYPE_LABELS[t]}' for t, n in by_type.items() if n))

    # Phase D
    if candidates and not args.no_verify:
        clock.phase('D', f'Verificando {len(candidates)} candidato(s)')
        candidates = verify_findings(candidates, plugin_dir, franken, args.model,
                                     args.fallback_model, rules, args.jobs, use_cache,
                                     clock=clock)
        confirmed = [f for f in candidates if f.get('verdict') == 'confirmed']
        refuted = [f for f in candidates if f.get('verdict') != 'confirmed']
        clock.done(f'{len(confirmed)} confirmado(s), {len(refuted)} descartado(s)')
    else:
        confirmed, refuted = candidates, []

    # Phase E
    if len(confirmed) > 1:
        clock.phase('E', f'Deduplicando {len(confirmed)} achado(s) confirmado(s)')
        before = len(confirmed)
        confirmed = dedupe_findings(confirmed, plugin_dir, franken, args.model,
                                    args.fallback_model, rules, use_cache, clock=clock)
        clock.done(f'{before - len(confirmed)} duplicata(s) consolidada(s)')

    # Phase F
    confirmed = attach_snippets(confirmed, plugin_dir)
    grade, grade_reason = compute_grade(confirmed)
    security_grade, _ = compute_grade(
        [f for f in confirmed if f.get('finding_type') == 'security'])
    ctx = {
        'franken': franken, 'version': version, 'inventory': inventory,
        'confirmed': confirmed, 'refuted': refuted, 'phpstan': triaged,
        'libs': libs, 'grade': grade, 'grade_reason': grade_reason,
        'security_grade': security_grade,
    }
    clock.phase('F', 'Gerando relatório')
    ctx['narrative'] = generate_narrative(ctx, plugin_dir, args.model,
                                          args.fallback_model, rules)

    report_dir = ensure_report_dir(plugin_dir)
    # Includes the time, not just the date: two audits run the same day would otherwise
    # share a filename and the second write_text() would silently clobber the first.
    stem = f'{franken}-{date.today().isoformat()}-{time.strftime("%H%M%S")}'
    report_path = report_dir / f'{stem}.md'
    report_path.write_text(render_report(ctx), encoding='utf-8')
    ctx['usage'] = {'model': args.model, 'fallback_model': args.fallback_model,
                    'seconds': round(clock.total()), 'phases': usage.as_dict()}
    append_usage_history(franken, stem, ctx['usage'])
    if args.json:
        (report_dir / f'{stem}.json').write_text(
            json.dumps(ctx, ensure_ascii=False, indent=2), encoding='utf-8')

    counts = severity_counts(confirmed)
    print('')
    print(f'Grade: {grade} — {grade_reason}  (só segurança: {security_grade})')
    print('  ' + ' · '.join(f'{s}: {counts[s]}' for s in SEVERITY_ORDER))
    print('')
    print(f'Relatório: {report_path}')
    print(f'Tempo total: {fmt_duration(clock.total())}')
    print('')
    print('Consumo por fase (custo eq. = preço de tabela da API, só para comparar):')
    for line in usage.summary_lines():
        print(line)
    clock.finish()
    return 0


if __name__ == '__main__':
    sys.exit(main())
