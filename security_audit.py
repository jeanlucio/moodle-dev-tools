#!/usr/bin/env python3
"""Security audit of a single Moodle plugin: deterministic tools + AI review.

Pipeline (see README "Auditoria de segurança"):
  A. deterministic collection (PHPStan at a high level, bundled-library versions,
     schema drift, optionally moodlecheck/coverage)
  B. AI triage of the deterministic output — separates real bugs from Moodle-idiom
     noise, which is what makes a high PHPStan level usable at all
  C. AI semantic scan, batched, with read-only tools so the agent can follow call
     chains beyond its own batch
  D. AI verification pass — every candidate must be confirmed exploitable or refuted
  E. deterministic grade + Markdown report

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

from claude_cli import (
    Clock, cache_path, cached, call_claude, extract_json, fmt_duration, hash_key,
    run_parallel,
)

TOOLS_DIR = Path(__file__).resolve().parent
RULES_FILE = TOOLS_DIR / 'security-rules.md'
PHPSTAN_BIN = TOOLS_DIR / 'phpstan' / 'vendor' / 'bin' / 'phpstan'
MOODLE_ROOT = Path('/home/ubuntu/meu-moodle/html')
MOODLE_DOCROOT = MOODLE_ROOT / 'public'
CACHE_DIR = Path.home() / '.moodle-security-audit-cache'

# Reports live inside the plugin, next to the code they describe, in a security-audit/
# subfolder of .plans/ — the directory this ecosystem already uses for AI assistant
# workspace files. Gitignoring .plans/ as a whole (not just the subfolder) keeps the rule
# aligned with every other tool that already writes there, and still covers this one.
REPORT_SUBDIR = '.plans/security-audit'
GITIGNORE_ENTRY = '.plans'
GITIGNORE_COMMENT = '# AI assistant session/workspace directories, not part of the plugin.'

# Bumped whenever a prompt changes, so cached results from an older prompt are not reused.
# v2: scan prompt gained extra_locations/mitigations; severity calibration rewritten.
PROMPT_VERSION = '2'

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

# Directories that never carry security signal worth a deep read. Their presence is still
# recorded in the inventory (test count is evidence of rigour), they are just not scanned.
SKIP_DIRS = {'amd/build', 'node_modules', 'vendor', '.git', 'docs'}
METADATA_ONLY_DIRS = {'tests', 'lang'}

SCAN_EXTENSIONS = {'.php', '.js', '.mustache', '.xml', '.css'}


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
        if not path.is_file() or path.suffix not in SCAN_EXTENSIONS:
            continue
        rel = str(path.relative_to(plugin_dir))
        if _is_skipped(rel):
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

    test_files = [e['rel'] for e in metadata_only if e['rel'].startswith('tests/')]
    return {
        'files_scanned': len(scan),
        'lines_scanned': sum(e['lines'] for e in scan),
        'entry_points': entry_points,
        'external_ws': external_ws,
        'has_privacy': (plugin_dir / 'classes' / 'privacy').is_dir(),
        'has_backup': (plugin_dir / 'backup' / 'moodle2').is_dir(),
        'has_access': (plugin_dir / 'db' / 'access.php').is_file(),
        'has_thirdparty': (plugin_dir / 'thirdpartylibs.xml').is_file(),
        'test_files': len(test_files),
        'behat_features': len([f for f in test_files if f.endswith('.feature')]),
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


def triage_phpstan(messages, plugin_dir, franken, model, fallback, rules, jobs, use_cache):
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
                return []
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
                           detail_fn=lambda r: f'{len(r)} classificado(s)')
    triaged = []
    for result in results:
        triaged.extend(result)
    return triaged


# --------------------------------------------------------------------------- #
#  Phase C — AI semantic scan                                                  #
# --------------------------------------------------------------------------- #

SCAN_PROMPT = """Você é um auditor de segurança de plugins Moodle. Analise os arquivos listados
abaixo procurando VULNERABILIDADES DE SEGURANÇA, seguindo o catálogo de regras do seu prompt
de sistema.

Leia cada arquivo por completo com a ferramenta Read. Você PODE e DEVE ler outros arquivos do
plugin (Grep/Glob/Read) quando precisar confirmar se algo é realmente explorável — seguir a
cadeia de chamada é o que separa achado real de suposição.

Seja conservador: só reporte o que tiver certeza. Nada de estilo, PHPDoc ou i18n.

Enquanto lê, também sinalize antipadrão N+1 ($DB->get_record/get_records/get_field dentro de
foreach/for/while) — mas classifique com cuidado, seguindo a regra L3-DOS-N1 do catálogo:
- Na IMENSA maioria dos casos é "finding_type": "code_quality" — não é achado de segurança,
  vai para a seção de performance do relatório e NÃO afeta a nota.
- Vira "finding_type": "security", "category": "dos" (achado de verdade, com severidade)
  APENAS quando o número de iterações do loop é controlado por entrada não confiável e sem
  limite superior (ex.: lista enviada pelo usuário, resultado de uma busca sem paginação) —
  ou seja, quando um atacante consegue fazer o custo escalar por conta própria, não só
  quando o código é ineficiente.

Responda APENAS com um array JSON (vazio se nada encontrado):
[{
  "title": "título curto",
  "finding_type": "security ou code_quality — code_quality só para N+1 não-escalável",
  "severity": "critical|high|medium|low|info",
  "category": "uma das 15 categorias oficiais do catálogo, ou \"n_plus_one\" quando finding_type=code_quality",
  "rule_id": "id da regra do catálogo, ex. L2-XSS-1 ou L3-DOS-N1",
  "file": "caminho/relativo.php",
  "line": 123,
  "extra_locations": [{"file": "outro.mustache", "line": 95}],
  "description": "o que está errado e por quê",
  "exploitable_by": "quem consegue explorar (não autenticado / estudante / professor / admin)",
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


def scan_batches(batches, plugin_dir, franken, model, fallback, rules, jobs, use_cache):
    def run_batch(batch):
        def compute():
            listing = '\n'.join(f'- {e["rel"]} ({e["lines"]} linhas)' for e in batch)
            try:
                text = call_claude(SCAN_PROMPT + listing, plugin_dir, model, fallback, rules)
                findings = extract_json(text)
                return findings if isinstance(findings, list) else []
            except Exception as exc:
                print(f'  aviso: um lote falhou ({exc})', file=sys.stderr)
                return []

        return cached(CACHE_DIR, franken, 'scan', _batch_key(plugin_dir, batch),
                     use_cache, compute)

    results = run_parallel(batches, jobs, run_batch, 'lote',
                           detail_fn=lambda r: f'{len(r)} candidato(s)')
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

Responda APENAS com JSON:
{"verdict": "confirmed|refuted", "severity": "critical|high|medium|low|info",
 "reason": "por que confirma ou refuta, citando o código",
 "poc": "passo a passo da exploração, se confirmado"}

Achado:
"""


def verify_findings(findings, plugin_dir, franken, model, fallback, rules, jobs, use_cache):
    """Confirm or refute each candidate independently.

    Deliberately one call per candidate rather than batched: the sceptical, focused reading
    is what produces the conservative tone, and bundling several findings into one prompt
    dilutes it. The cost is bounded by caching instead.
    """
    def run_one(finding):
        def compute():
            payload = json.dumps(finding, ensure_ascii=False, indent=2)
            try:
                text = call_claude(VERIFY_PROMPT + payload, plugin_dir, model,
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
        key = hash_key(PROMPT_VERSION, finding.get('title'), finding.get('file'),
                       finding.get('line'), finding.get('description'))
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
        if result.get('severity') in SEVERITY_ORDER:
            finding['severity'] = result['severity']
        return finding

    def detail(finding):
        title = (finding.get('title') or '')[:40]
        return f'{finding.get("verdict")} — {title}'

    return run_parallel(findings, jobs, run_one, 'achado', detail_fn=detail)


# --------------------------------------------------------------------------- #
#  Phase E — grade and report                                                  #
# --------------------------------------------------------------------------- #

LANG_BY_SUFFIX = {'.php': 'php', '.js': 'javascript', '.mustache': 'html',
                  '.css': 'css', '.xml': 'xml'}


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

    An additive score misrepresents a security report: eight low-severity hygiene gaps are
    not "worse" than one stored XSS, yet any penalty-sum model says exactly that. So the
    worst severity present sets a ceiling, and only the count of low findings refines it.

    Calibrated against three published MDL Shield reviews, which this reproduces exactly:
      block_playerhud        2 low  + 1 info -> A
      local_information_center 8 low + 1 info -> B+
      filter_playerhud       1 high + 1 medium + 1 low + 1 info -> D
    """
    counts = severity_counts(findings)
    if counts['critical']:
        return 'F', 'achado crítico presente'
    if counts['high']:
        return 'D', 'achado de severidade alta presente'
    if counts['medium']:
        return 'C', 'achado de severidade média presente'
    if counts['low'] >= 3:
        return 'B+', f'{counts["low"]} achados de severidade baixa'
    if counts['low']:
        return 'A', f'{counts["low"]} achado(s) de severidade baixa'
    return 'A+', 'nenhum achado de segurança'


def _render_finding(add, index, finding):
    """One finding, in the order a reader needs it: what, where, why, proof, fix."""
    severity = finding.get('severity', 'info')
    add(f'### {index}. {finding.get("title", "(sem título)")}')
    add('')
    add(f'| | |')
    add('|---|---|')
    add(f'| **Severidade** | `{severity}` |')
    add(f'| **Categoria** | `{finding.get("category", "?")}` |')
    add(f'| **Regra** | `{finding.get("rule_id", "?")}` |')
    add(f'| **Explorável por** | {finding.get("exploitable_by", "?")} |')
    add('')

    add('**Local afetado**')
    add('')
    add(f'1. `{finding.get("file")}:{finding.get("line")}`')
    extras = [e for e in (finding.get('extra_locations') or []) if isinstance(e, dict)]
    for offset, extra in enumerate(extras, start=2):
        add(f'{offset}. `{extra.get("file")}:{extra.get("line")}`')
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
        add('**Prova de conceito**')
        add('')
        add(finding['poc'])
        add('')
    if finding.get('recommendation'):
        add('**Correção recomendada**')
        add('')
        add(finding['recommendation'])
        add('')


def render_report(ctx):
    """Assemble the Markdown report, ordered the way a security review is normally read."""
    inv, confirmed, refuted = ctx['inventory'], ctx['confirmed'], ctx['refuted']
    narrative = ctx.get('narrative') or {}
    counts = severity_counts(confirmed)
    grade, grade_reason = ctx['grade'], ctx.get('grade_reason', '')
    version = ctx['version']

    out = []
    add = out.append

    add(f'# Relatório de auditoria de segurança — {ctx["franken"]}')
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
    add('| Severidade | Achados |')
    add('|---|---|')
    for sev in SEVERITY_ORDER:
        mark = f'**{counts[sev]}**' if counts[sev] else '0'
        add(f'| {sev} | {mark} |')
    add('')
    add('> A nota é **dominada pelo pior achado**, não por soma de penalidades: um `critical`'
        ' resulta em `F`, um `high` em `D`, um `medium` em `C`; só de `low` a nota é `A`'
        ' (até 2) ou `B+` (3 ou mais); sem achados, `A+`. Oito falhas de higiene não são'
        ' piores que um XSS armazenado, e um modelo aditivo diria que são. Só achados de'
        ' segurança contam — bugs de código ficam em seção própria.')
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
    add(f'| Arquivos de teste | {inv["test_files"]} |')
    add('')

    add('**Evidências de rigor**')
    add('')

    def check(flag):
        return '✅' if flag else '—'

    add(f'- {check(inv["has_privacy"])} Privacy API implementada')
    add(f'- {check(inv["has_access"])} Capabilities declaradas (`db/access.php`)')
    add(f'- {check(inv["has_backup"])} Backup/restore (`backup/moodle2/`)')
    add(f'- {check(inv["test_files"] > 0)} Testes automatizados ({inv["test_files"]} arquivos)')
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
        add('Nenhum achado de segurança confirmado.')
        add('')
    else:
        ordered = sorted(confirmed,
                         key=lambda f: SEVERITY_ORDER.index(f.get('severity', 'info')))
        for index, finding in enumerate(ordered, 1):
            _render_finding(add, index, finding)

    # ---- Pontos fortes ----------------------------------------------------
    strengths = [s for s in (narrative.get('strengths') or []) if isinstance(s, dict)]
    if strengths:
        add('## Pontos fortes de segurança')
        add('')
        add('Práticas defensivas verificadas no código durante a auditoria.')
        add('')
        for index, item in enumerate(strengths, 1):
            add(f'{index}. **{item.get("title", "")}** — {item.get("detail", "")}')
        add('')

    # ---- Bugs de código ---------------------------------------------------
    real_bugs = [m for m in ctx['phpstan']
                 if m.get('verdict') in ('real_bug', 'security_relevant')]
    add('## Bugs de código (PHPStan triado)')
    add('')
    add('Seção separada de propósito: **não afetam a nota de segurança**. São achados'
        ' determinísticos do PHPStan que sobreviveram à triagem por IA — o ruído de idioma'
        ' Moodle foi descartado.')
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
        add('## Achados de performance')
        add('')
        add('Seção separada de propósito: **não afetam a nota de segurança**. Vêm direto da'
            ' varredura semântica (Fase C), sem passar pelo passe de verificação da Fase D'
            ' — esse passe é sobre exploitabilidade, que não se aplica a uma observação de'
            ' performance. Trate como sinal a conferir, não como confirmado.')
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
    add('Gerado por `moodle-security-audit` — ferramentas determinísticas (PHPStan) +'
        ' revisão por IA, com passe de verificação. Catálogo de regras em'
        ' `security-rules.md`.')
    add('')
    return '\n'.join(out)


NARRATIVE_PROMPT = """Escreva as seções narrativas do relatório de uma auditoria de segurança de
plugin Moodle. Você pode ler o código para embasar o que afirmar — não invente nada.

Tudo em português do Brasil. Factual, sem elogio vazio e sem marketing.

Responda APENAS com JSON:
{
  "purpose": "1-2 frases dizendo o que o plugin faz (contexto para quem lê o relatório)",
  "executive_summary": "3-5 frases: postura geral de segurança, o que os achados significam \
na prática e o que NÃO foi encontrado. Se não houver achado grave, diga com clareza.",
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
        'findings': [{k: f.get(k) for k in ('title', 'severity', 'category', 'file')}
                     for f in ctx['confirmed']],
        'grade': ctx['grade'],
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

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('plugin_dir')
    parser.add_argument('--model', default='claude-fable-5')
    parser.add_argument('--fallback-model', default='claude-opus-5')
    parser.add_argument('--phpstan-level', type=int, default=6)
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
        ctx['confirmed'] = attach_snippets(ctx.get('confirmed', []), plugin_dir)
        # .get(..., []): older JSON files predate the quality_findings split and simply
        # don't have the key — treat that as "none", not an error.
        ctx['quality_findings'] = attach_snippets(ctx.get('quality_findings', []), plugin_dir)
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
    clock = Clock()

    # Phase A
    clock.phase('A', f'PHPStan nível {args.phpstan_level}')
    phpstan_msgs, phpstan_err = run_phpstan(plugin_dir, args.phpstan_level)
    if phpstan_err:
        print(f'  aviso: {phpstan_err}', file=sys.stderr)
    clock.done(f'{len(phpstan_msgs)} mensagem(ns) após filtro de ruído')
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
                                 args.fallback_model, rules, args.jobs, use_cache)
        real = sum(1 for m in triaged if m['verdict'] in ('real_bug', 'security_relevant'))
        clock.done(f'{real} bug(s) real(is), {len(triaged) - real} idioma Moodle')

    # Phase C
    batches = build_batches(scan_files, args.batch_lines)
    clock.phase('C', f'Varredura semântica em {len(batches)} lote(s)')
    all_candidates = scan_batches(batches, plugin_dir, franken, args.model,
                                  args.fallback_model, rules, args.jobs, use_cache)
    # code_quality candidates (N+1 that doesn't scale with attacker input) skip Phase D
    # entirely: verify_findings()'s VERIFY_PROMPT is framed around exploitability, which
    # doesn't apply to a performance observation, and it would be quota spent asking an
    # adversarial-exploit question about something that was never a security claim.
    candidates = [f for f in all_candidates if f.get('finding_type') != 'code_quality']
    quality_findings = [f for f in all_candidates if f.get('finding_type') == 'code_quality']
    clock.done(f'{len(candidates)} candidato(s) de segurança, {len(quality_findings)} de performance')

    # Phase D
    if candidates and not args.no_verify:
        clock.phase('D', f'Verificando {len(candidates)} candidato(s)')
        candidates = verify_findings(candidates, plugin_dir, franken, args.model,
                                     args.fallback_model, rules, args.jobs, use_cache)
        confirmed = [f for f in candidates if f.get('verdict') == 'confirmed']
        refuted = [f for f in candidates if f.get('verdict') != 'confirmed']
        clock.done(f'{len(confirmed)} confirmado(s), {len(refuted)} descartado(s)')
    else:
        confirmed, refuted = candidates, []

    # Phase E
    confirmed = attach_snippets(confirmed, plugin_dir)
    quality_findings = attach_snippets(quality_findings, plugin_dir)
    grade, grade_reason = compute_grade(confirmed)
    ctx = {
        'franken': franken, 'version': version, 'inventory': inventory,
        'confirmed': confirmed, 'refuted': refuted, 'phpstan': triaged,
        'quality_findings': quality_findings,
        'libs': libs, 'grade': grade, 'grade_reason': grade_reason,
    }
    clock.phase('E', 'Gerando relatório')
    ctx['narrative'] = generate_narrative(ctx, plugin_dir, args.model,
                                          args.fallback_model, rules)

    report_dir = ensure_report_dir(plugin_dir)
    # Includes the time, not just the date: two audits run the same day would otherwise
    # share a filename and the second write_text() would silently clobber the first.
    stem = f'{franken}-{date.today().isoformat()}-{time.strftime("%H%M%S")}'
    report_path = report_dir / f'{stem}.md'
    report_path.write_text(render_report(ctx), encoding='utf-8')
    if args.json:
        (report_dir / f'{stem}.json').write_text(
            json.dumps(ctx, ensure_ascii=False, indent=2), encoding='utf-8')

    counts = severity_counts(confirmed)
    print('')
    print(f'Grade: {grade} — {grade_reason}')
    print('  ' + ' · '.join(f'{s}: {counts[s]}' for s in SEVERITY_ORDER))
    print('')
    print(f'Relatório: {report_path}')
    print(f'Tempo total: {fmt_duration(clock.total())}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
