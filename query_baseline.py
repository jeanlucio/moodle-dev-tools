#!/usr/bin/env python3
"""Deterministic per-test DB query counting for a Moodle plugin, with regression detection.

Standalone tool, run on demand — deliberately NOT part of moodle-security-audit. Where that
tool guesses at N+1 by reading code, this one measures it by running the plugin's existing
PHPUnit suite with $DB->perf_get_queries() instrumented (query_count_extension.php). No test
file needs to change: the instrumentation is external, the same way Xdebug measures line
coverage without the test knowing it is being measured.

The count alone does not say "this is bad" — 40 queries can be a real N+1 or a legitimately
complex operation. The tool's real value is REGRESSION detection: save counts per test, and
flag when a test that always fired 4 queries suddenly fires 40. The baseline is never updated
silently — only --accept persists the current run's numbers, after you have looked at what a
report flagged.

Pipeline:
  A. deterministic instrumentation — run the plugin's PHPUnit suite twice inside the
     container (a discarded warm-up pass, then the measured pass — see WARMUP note below),
     read back the per-test query counts
  B. deterministic diff against the saved baseline (if any)
  C. AI triage of ONLY the flagged tests (not the whole plugin) — cheap, because the
     deterministic step already narrowed down what to look at
  D. Markdown report + optional baseline update (only with --accept)

WARMUP: verified empirically against this exact environment (see moodle-lessons case log)
that the FIRST PHPUnit invocation after the test environment is freshly (re)initialised
fires ~6 extra queries per test, one time only, stabilising from the second invocation
onward (confirmed across 3 consecutive runs: run1->run2 changed 12/22 tests by a flat +6,
run2->run3 changed 0/22). Always discarding one throwaway run before the measured one avoids
reporting phantom regressions caused by cache warm-up rather than a real code change.

Usage: query_baseline.py <plugin_abs_dir> [options]  (normally via moodle-query-baseline)
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

from claude_cli import Clock, call_claude, cached, extract_json, hash_key, run_parallel

TOOLS_DIR = Path(__file__).resolve().parent
EXTENSION_FILE = TOOLS_DIR / 'query_count_extension.php'

# Inherited from ~/.moodle-dev-tools.env via query-baseline.sh (the CLI entry point),
# which sources it with `set -a` before invoking this script — falls back to this
# machine's own values when the wrapper isn't used or the env file doesn't exist.
CONTAINER = os.environ.get('MDT_CONTAINER_51', 'meu-moodle-web-1')
DOCROOT = '/var/www/html/public'
PHPUNIT_BIN = '/var/www/html/vendor/bin/phpunit'
MOODLE_BOOTSTRAP = f'{DOCROOT}/lib/phpunit/bootstrap.php'

BASELINE_DIR = Path.home() / '.moodle-query-baseline'
CACHE_DIR = Path.home() / '.moodle-query-baseline-cache'
PROMPT_VERSION = '1'

REPORT_SUBDIR = '.plans/query-baseline'
GITIGNORE_ENTRY = '.plans'
GITIGNORE_COMMENT = '# AI assistant session/workspace directories, not part of the plugin.'


# --------------------------------------------------------------------------- #
#  Plugin resolution — same tipo/nome convention as coverage.sh/security-audit.sh #
# --------------------------------------------------------------------------- #

def resolve_plugin(plugin_arg):
    """'blocks/playerhud' or 'html/public/filter/playerhud' -> ('filter/playerhud', 'filter_playerhud').

    Also handles Moodle's fixed-subdirectory plugin types, where every instance lives under
    an extra directory level that is NOT part of the Frankenstyle name, e.g.
    'availability/condition/playerhud' -> 'availability_playerhud'.
    """
    plugin = plugin_arg.strip()
    for prefix in ('./', 'html/public/', 'public/'):
        if plugin.startswith(prefix):
            plugin = plugin[len(prefix):]
    plugin = plugin.strip('/')
    typedir, _, name = plugin.partition('/')
    if not name:
        raise ValueError('informe o plugin como tipo/nome (ex.: blocks/playerhud)')
    plugin_type = 'block' if typedir == 'blocks' else typedir
    fixed_subdirs = {'availability': 'condition'}
    subdir = fixed_subdirs.get(typedir)
    if subdir and name.startswith(f'{subdir}/'):
        name = name[len(subdir) + 1:]
    if '/' in name:
        raise ValueError(
            f"caminho de plugin com nivel extra nao reconhecido: '{plugin}' "
            f"(resolve_plugin so conhece tipo/nome e os subdiretorios fixos {sorted(fixed_subdirs)})"
        )
    return plugin, f'{plugin_type}_{name}'


def docker_exec(cmd, **kwargs):
    return subprocess.run(['docker', 'exec', CONTAINER, 'sh', '-c', cmd],
                          capture_output=True, text=True, **kwargs)


def docker_exec_env(env, cmd, **kwargs):
    docker_env_args = []
    for k, v in env.items():
        docker_env_args += ['-e', f'{k}={v}']
    return subprocess.run(['docker', 'exec', *docker_env_args, CONTAINER, 'sh', '-c', cmd],
                          capture_output=True, text=True, **kwargs)


# --------------------------------------------------------------------------- #
#  Phase A — instrumentation                                                   #
# --------------------------------------------------------------------------- #

def build_phpunit_xml(plugin_abs, franken, extension_path):
    """Minimal phpunit.xml: no coverage, bootstrap points at the query-count extension
    (which boots Moodle itself — see query_count_extension.php's own docblock)."""
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<phpunit
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
  xsi:noNamespaceSchemaLocation="https://schema.phpunit.de/10.5/phpunit.xsd"
  bootstrap="{extension_path}"
  processIsolation="false"
  backupGlobals="false"
  cacheResult="false"
  failOnDeprecation="false"
  failOnWarning="false"
  beStrictAboutTestsThatDoNotTestAnything="false"
  beStrictAboutOutputDuringTests="true"
  cacheDirectory="/tmp/.phpunit-cache-querybaseline-{franken}"
  backupStaticProperties="false"
>
  <php>
    <const name="PHPUNIT_SEQUENCE_START" value="157000"/>
  </php>
  <extensions>
    <bootstrap class="core\\tests\\phpunit\\moodle_extension"/>
    <bootstrap class="moodle_dev_tools\\phpunit\\query_count_extension"/>
  </extensions>
  <testsuites>
    <testsuite name="{franken}">
      <directory suffix="_test.php">{plugin_abs}/tests</directory>
      <exclude>{plugin_abs}/tests/fixtures</exclude>
      <exclude>{plugin_abs}/tests/generator</exclude>
    </testsuite>
  </testsuites>
</phpunit>
'''


def run_instrumented_suite(plugin, franken, plugin_abs):
    """Push the extension + phpunit.xml into the container, run the suite TWICE (discarded
    warm-up, then the measured pass — see module docstring), return {test_id: count}.
    """
    work_dir = f'/tmp/moodle-query-baseline-{franken}'
    extension_path = f'{work_dir}/query_count_extension.php'
    xml_path = f'{work_dir}/phpunit.xml'
    output_path = f'{work_dir}/counts.json'

    docker_exec(f'mkdir -p {work_dir}')
    copy = subprocess.run(['docker', 'cp', str(EXTENSION_FILE),
                           f'{CONTAINER}:{extension_path}'], capture_output=True, text=True)
    if copy.returncode != 0:
        raise RuntimeError(f'docker cp da extensão falhou: {copy.stderr.strip()}')

    xml = build_phpunit_xml(f'{DOCROOT}/{plugin}', franken, extension_path)
    write_xml = subprocess.run(
        ['docker', 'exec', '-i', CONTAINER, 'sh', '-c', f'cat > {xml_path}'],
        input=xml, capture_output=True, text=True,
    )
    if write_xml.returncode != 0:
        raise RuntimeError(f'escrita do phpunit.xml falhou: {write_xml.stderr.strip()}')

    run_cmd = (f"cd {DOCROOT} && php -d memory_limit=-1 {PHPUNIT_BIN} "
              f"-c {xml_path} 2>&1")

    try:
        # Discarded warm-up pass — see WARMUP in the module docstring.
        docker_exec_env({'MOODLE_QUERY_BASELINE_OUTPUT': f'{work_dir}/warmup.json'},
                        run_cmd, timeout=900)

        # The measured pass.
        result = docker_exec_env({'MOODLE_QUERY_BASELINE_OUTPUT': output_path},
                                 run_cmd, timeout=900)
        if 'OK' not in result.stdout and 'OK, but' not in result.stdout:
            print(result.stdout[-3000:], file=sys.stderr)
            raise RuntimeError('PHPUnit não terminou OK — veja a saída acima')

        read = docker_exec(f'cat {output_path}')
        if read.returncode != 0:
            raise RuntimeError(f'não achou o JSON de contagem: {read.stderr.strip()}')
        return json.loads(read.stdout), result.stdout
    finally:
        docker_exec(f'rm -rf {work_dir}')


# --------------------------------------------------------------------------- #
#  Phase B — diff against the saved baseline                                   #
# --------------------------------------------------------------------------- #

def load_baseline(franken):
    path = BASELINE_DIR / f'{franken}.json'
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_baseline(franken, counts):
    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    path = BASELINE_DIR / f'{franken}.json'
    path.write_text(json.dumps(counts, ensure_ascii=False, indent=2, sort_keys=True))
    return path


def diff_against_baseline(counts, baseline, min_queries, threshold_pct):
    """Classify every test: novo / sem_mudanca / mudou / suspeito.

    "suspeito" requires BOTH filters to pass — min_queries guards against noise on trivial
    tests (2->3 is +50% but irrelevant), threshold_pct guards against normal test-to-test
    variance for tests that already fire a lot of queries.
    """
    rows = []
    for test_id, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        old = baseline.get(test_id)
        if old is None:
            status = 'novo'
            delta, pct = None, None
        else:
            delta = count - old
            pct = (delta / old * 100) if old else (100.0 if delta else 0.0)
            if delta == 0:
                status = 'sem_mudanca'
            elif delta > 0 and count >= min_queries and pct >= threshold_pct:
                status = 'suspeito'
            else:
                status = 'mudou'
        rows.append({
            'test_id': test_id, 'count': count, 'baseline': old,
            'delta': delta, 'pct': pct, 'status': status,
        })
    return rows


# --------------------------------------------------------------------------- #
#  Phase C — AI triage of only the flagged rows                                #
# --------------------------------------------------------------------------- #

TRIAGE_PROMPT = """Um teste PHPUnit de um plugin Moodle passou a disparar bem mais queries no
banco do que disparava antes (medido via $DB->perf_get_queries(), contagem real, não estimada).

Leia o teste (pelo nome do método, ache o arquivo em tests/) e o código que ele exercita.
Diga se isso parece um N+1 genuíno (uma query dentro de um loop que deveria ser bulk-loaded)
ou se é esperado (funcionalidade nova, teste passou a cobrir mais coisa, etc.).

Seja conservador: sem certeza, classifique como "indeterminado".

Responda APENAS com JSON:
{"verdict": "n_plus_one_provavel|esperado|indeterminado",
 "reason": "uma ou duas frases citando o código, em português"}
"""


def triage_suspects(rows, plugin_dir, franken, model, fallback, jobs, use_cache):
    suspects = [r for r in rows if r['status'] == 'suspeito']
    if not suspects:
        return

    def run_one(row):
        def compute():
            prompt = TRIAGE_PROMPT + (
                f"\nTeste: {row['test_id']}\n"
                f"Contagem anterior (baseline): {row['baseline']}\n"
                f"Contagem atual: {row['count']} ({round(row['pct'], 1)}% de aumento)\n"
            )
            try:
                text = call_claude(prompt, plugin_dir, model, fallback, allow_tools=True)
                result = extract_json(text)
                return result if isinstance(result, dict) else {}
            except Exception as exc:
                return {'verdict': 'indeterminado', 'reason': f'triagem falhou: {exc}'}

        key = hash_key(PROMPT_VERSION, row['test_id'], row['baseline'], row['count'])
        result = cached(CACHE_DIR, franken, 'triage', key, use_cache, compute)
        row['triage_verdict'] = result.get('verdict', 'indeterminado')
        row['triage_reason'] = result.get('reason', '')
        return row

    def detail(row):
        return f'{row.get("triage_verdict")} — {row["test_id"].split("::")[-1][:40]}'

    run_parallel(suspects, jobs, run_one, 'suspeito', detail_fn=detail)


# --------------------------------------------------------------------------- #
#  Phase D — report                                                            #
# --------------------------------------------------------------------------- #

def ensure_report_dir(plugin_dir):
    """Same .plans/ convention as moodle-security-audit, own subfolder."""
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


STATUS_LABEL = {
    'novo': '🆕 novo', 'sem_mudanca': '— sem mudança',
    'mudou': 'mudou', 'suspeito': '⚠️ suspeito',
}


def render_report(franken, rows, has_baseline, accepted):
    out = []
    add = out.append
    add(f'# Contagem de queries por teste — {franken}')
    add('')
    add('Medição determinística via `$DB->perf_get_queries()`, instrumentado por'
        ' `query_count_extension.php` — não é estimativa de IA. O número por teste inclui'
        ' o custo de `setUp()`/fixture, não só o código sob teste isoladamente; por isso o'
        ' que importa é a **variação em relação ao baseline**, não o valor absoluto.')
    add('')
    if not has_baseline:
        add('> Nenhum baseline anterior encontrado — esta rodada estabelece um. Rode com'
            ' `--accept` para persistir; sem isso, nada é gravado.')
    elif accepted:
        add('> Baseline **atualizado** com os números desta rodada.')
    else:
        add('> Baseline **não** foi alterado (rode com `--accept` para persistir estes'
            ' números). Comparado contra o baseline salvo anteriormente.')
    add('')

    add('## Tabela completa')
    add('')
    add('| Teste | Atual | Baseline | Delta | Status |')
    add('|---|---:|---:|---:|---|')
    for row in rows:
        delta = '' if row['delta'] is None else f'{row["delta"]:+d}'
        baseline = '' if row['baseline'] is None else str(row['baseline'])
        name = row['test_id'].split('::')[-1]
        add(f'| `{name}` | {row["count"]} | {baseline} | {delta} |'
            f' {STATUS_LABEL[row["status"]]} |')
    add('')

    suspects = [r for r in rows if r['status'] == 'suspeito']
    if suspects:
        add('## Suspeitos — triagem')
        add('')
        for row in suspects:
            name = row['test_id'].split('::')[-1]
            verdict = row.get('triage_verdict', 'não triado (--no-triage)')
            reason = row.get('triage_reason', '')
            add(f'### `{name}`')
            add('')
            add(f'{row["baseline"]} → {row["count"]} queries'
                f' ({row["pct"]:+.0f}%) — **{verdict}**')
            add('')
            if reason:
                add(reason)
                add('')

    add('---')
    add('')
    add('Gerado por `moodle-query-baseline`. Fase A/B são 100% determinísticas (sem IA);'
        ' a Fase C (triagem) só roda sobre os testes marcados suspeito acima.')
    add('')
    return '\n'.join(out)


# --------------------------------------------------------------------------- #
#  main                                                                        #
# --------------------------------------------------------------------------- #

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('plugin_dir', help='tipo/nome, ex. filter/playerhud')
    parser.add_argument('--min-queries', type=int, default=10)
    parser.add_argument('--threshold-pct', type=float, default=50.0)
    parser.add_argument('--no-triage', action='store_true')
    parser.add_argument('--accept', action='store_true')
    parser.add_argument('--model', default='claude-fable-5')
    parser.add_argument('--fallback-model', default='claude-opus-5')
    parser.add_argument('--jobs', type=int, default=5)
    parser.add_argument('--no-cache', action='store_true')
    args = parser.parse_args()

    try:
        plugin, franken = resolve_plugin(args.plugin_dir)
    except ValueError as exc:
        print(f'erro: {exc}', file=sys.stderr)
        return 1

    moodle_public = os.environ.get('MDT_MOODLE_PUBLIC', '/home/ubuntu/meu-moodle/html/public')
    plugin_abs_host = Path(moodle_public) / plugin
    if not plugin_abs_host.is_dir():
        print(f'erro: {plugin_abs_host} não existe', file=sys.stderr)
        return 1
    if not (plugin_abs_host / 'tests').is_dir():
        print(f'erro: {plugin_abs_host}/tests não existe — nada para medir', file=sys.stderr)
        return 1

    use_triage = not args.no_triage
    if use_triage:
        if shutil.which('claude') is None:
            print('aviso: binário "claude" não encontrado — rodando sem triagem'
                  ' (--no-triage implícito)', file=sys.stderr)
            use_triage = False

    clock = Clock()
    print(f'Medindo queries de {franken} ({plugin})')
    print('')

    clock.phase('A', 'Rodando suíte instrumentada (aquecimento + medição)')
    counts, phpunit_output = run_instrumented_suite(plugin, franken, f'{DOCROOT}/{plugin}')
    clock.done(f'{len(counts)} teste(s) medido(s)')

    clock.phase('B', 'Comparando contra o baseline salvo')
    baseline = load_baseline(franken)
    rows = diff_against_baseline(counts, baseline, args.min_queries, args.threshold_pct)
    suspect_count = sum(1 for r in rows if r['status'] == 'suspeito')
    clock.done(f'{suspect_count} suspeito(s)')

    if use_triage and suspect_count:
        clock.phase('C', f'Triando {suspect_count} suspeito(s)')
        triage_suspects(rows, plugin_abs_host, franken, args.model, args.fallback_model,
                        args.jobs, not args.no_cache)
        clock.done()

    report_dir = ensure_report_dir(plugin_abs_host)
    stem = f'{franken}-{date.today().isoformat()}-{time.strftime("%H%M%S")}'
    report_path = report_dir / f'{stem}.md'
    report_path.write_text(
        render_report(franken, rows, bool(baseline), args.accept), encoding='utf-8')

    if args.accept:
        baseline_path = save_baseline(franken, counts)
        print(f'Baseline atualizado: {baseline_path}')

    print('')
    print(f'{len(rows)} teste(s) — {suspect_count} suspeito(s)')
    print(f'Relatório: {report_path}')
    print(f'Tempo total: {clock.total():.0f}s')
    return 0


if __name__ == '__main__':
    sys.exit(main())
