"""Runs a plugin's URL validators against known bypass inputs (rule L3-SSRF-1).

Reading a validator is how a review missed that `is_safe_url()` approved `[::1]`, `127.1` and
`::ffff:127.0.0.1`: each line looked right, and a test named "blocks DNS rebinding" passed.
Running the method settles it. Every method that takes one URL-like string and lives in a
file that filters IP ranges is called, inside the Moodle container, with a public control URL
and a list of addresses that must be refused. Whatever it accepts is reported.

The method is called through reflection on an instance built without its constructor, so a
protected helper is reachable and nothing the constructor does (network, database) runs. The
probe writes nothing. A method whose answer to the public control is not truthy is skipped:
its return value means something else (an error string, a normalised URL), so acceptance
cannot be read from it.
"""

import base64
import json
import os
import re
import subprocess
from pathlib import Path

from audit_static_checks import URL_VALIDATION_RE, _finding, _production_php, _read

CONTAINER = os.environ.get('MDT_CONTAINER_51', 'meu-moodle-web-1')
HOST_PUBLIC = Path(os.environ.get('MDT_MOODLE_PUBLIC', '/home/ubuntu/meu-moodle/html/public'))
CONTAINER_DOCROOT = '/var/www/html/public'

CONTROL_URL = 'https://8.8.8.8/v1'
BYPASS_URLS = [
    'https://127.1/v1', 'https://2130706433/v1', 'https://0x7f000001/v1',
    'https://0177.0.0.1/v1', 'https://0.0.0.0/v1', 'https://localhost./v1',
    'https://LOCALHOST/v1', 'https://[::1]/v1', 'https://[::ffff:127.0.0.1]/v1',
    'https://[::7f00:1]/v1', 'https://[64:ff9b::a00:1]/v1', 'https://[fd00::1]/v1',
    'https://[fe80::1]/v1', 'https://169.254.169.254/v1', 'https://100.100.100.200/v1',
    'https://198.18.0.1/v1', 'https://10.0.0.1/v1', 'https://192.168.0.1/v1',
    'https://unresolvable-host.invalid/v1',
]

NAMESPACE_RE = re.compile(r'^\s*namespace\s+([\w\\]+)\s*;', re.M)
CLASS_RE = re.compile(r'^\s*(?:(?:abstract|final|readonly)\s+)*class\s+(\w+)', re.M)
METHOD_RE = re.compile(
    r'(?P<mods>(?:(?:public|protected|private|static|final)\s+)+)function\s+(?P<name>\w+)\s*'
    r'\(\s*(?:\??string\s+)?\$(?P<param>\w+)\s*\)\s*(?::\s*(?P<ret>\??[\w|\\]+))?')
URL_PARAM_RE = re.compile(r'url|uri|endpoint|link|host', re.I)
SKIPPED_RETURNS = {'string', 'int', 'float', 'void', 'never', '?string'}

PROBE_PHP = r'''<?php
define('CLI_SCRIPT', true);
require('%(docroot)s/config.php');
$input = json_decode(base64_decode('%(input)s'), true);
$out = [];
foreach ($input['methods'] as $m) {
    $key = $m['class'] . '::' . $m['method'];
    try {
        $rc = new ReflectionClass($m['class']);
        $rm = $rc->getMethod($m['method']);
        $rm->setAccessible(true);
        $object = $rm->isStatic() ? null : $rc->newInstanceWithoutConstructor();
        if (empty($rm->invoke($object, $input['control']))) {
            $out[$key] = ['skipped' => 'control not accepted'];
            continue;
        }
        $accepted = [];
        foreach ($input['bypass'] as $url) {
            try {
                if (!empty($rm->invoke($object, $url))) {
                    $accepted[] = $url;
                }
            } catch (Throwable $e) {
                continue;
            }
        }
        $out[$key] = ['accepted' => $accepted];
    } catch (Throwable $e) {
        $out[$key] = ['skipped' => get_class($e) . ': ' . $e->getMessage()];
    }
}
echo "\n@@PROBE@@" . json_encode($out);
'''


def find_url_validators(plugin_dir, scan_files):
    """Methods taking one URL-like string, in production class files that filter IP ranges."""
    validators = []
    for rel in _production_php(scan_files):
        if not rel.startswith('classes/'):
            continue
        content = _read(plugin_dir / rel)
        if not URL_VALIDATION_RE.search(content):
            continue
        namespace, klass = NAMESPACE_RE.search(content), CLASS_RE.search(content)
        if not namespace or not klass:
            continue
        fqcn = namespace.group(1) + '\\' + klass.group(1)
        for match in METHOD_RE.finditer(content):
            ret = (match.group('ret') or '').lower()
            if ret in SKIPPED_RETURNS or not URL_PARAM_RE.search(match.group('param')):
                continue
            line = content.count('\n', 0, match.start()) + 1
            validators.append({'class': fqcn, 'method': match.group('name'), 'rel': rel,
                               'line': line})
    return validators


def _container_plugin_dir(plugin_dir):
    try:
        rel = Path(plugin_dir).resolve().relative_to(HOST_PUBLIC.resolve())
    except ValueError:
        return None
    return f'{CONTAINER_DOCROOT}/{rel}'


def run_probe(validators):
    """{class::method: {accepted: [...]} | {skipped: reason}}, or None when it cannot run."""
    payload = json.dumps({
        'control': CONTROL_URL, 'bypass': BYPASS_URLS,
        'methods': [{'class': v['class'], 'method': v['method']} for v in validators],
    })
    script = PROBE_PHP % {'docroot': CONTAINER_DOCROOT,
                          'input': base64.b64encode(payload.encode()).decode()}
    try:
        proc = subprocess.run(['docker', 'exec', '-i', CONTAINER, 'php'], input=script,
                              capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError):
        return None
    marker = proc.stdout.rfind('@@PROBE@@')
    if marker < 0:
        return None
    try:
        return json.loads(proc.stdout[marker + len('@@PROBE@@'):])
    except ValueError:
        return None


def check_url_validators(plugin_dir, scan_files):
    """Candidate findings (Phase D verifies them) plus a list of notes for the console."""
    validators = find_url_validators(plugin_dir, scan_files)
    if not validators:
        return [], []
    if _container_plugin_dir(plugin_dir) is None:
        return [], [f'validadores de URL não executados: {plugin_dir} fora de {HOST_PUBLIC}']
    results = run_probe(validators)
    if results is None:
        return [], [f'validadores de URL não executados: container {CONTAINER} indisponível']

    findings, notes = [], []
    for v in validators:
        key = f"{v['class']}::{v['method']}"
        result = results.get(key, {})
        if 'skipped' in result:
            notes.append(f"{key}: não aplicável ({result['skipped']})")
            continue
        accepted = result.get('accepted', [])
        notes.append(f'{key}: {len(accepted)} de {len(BYPASS_URLS)} entradas de bypass aceitas')
        if not accepted:
            continue
        findings.append(_finding({
            'title': f"Validador de URL aceita endereços internos ({v['method']})",
            'finding_type': 'security', 'severity': 'medium',
            'category': 'unauthorised_access', 'rule_id': 'L3-SSRF-1',
            'description': f'O método {key} foi EXECUTADO no container contra entradas de '
                           'bypass e aprovou endereços que precisa recusar. É fato, não '
                           'leitura de código: falta julgar se a entrada chega ao método por '
                           'um caminho real (PARAM_URL antes dele?) e se o helper de segurança '
                           'do core barra depois.',
            'recommendation': 'Normalizar o host (colchetes, ponto final), julgar IPv6 com IPv4 '
                              'embutido pelo IPv4, bloquear as faixas que os flags do PHP não '
                              'cobrem e recusar quando a resolução de DNS voltar vazia.',
        }, [(v['rel'], v['line'])], 'Entradas aceitas: ' + ', '.join(accepted) + '.'))
    return findings, notes
