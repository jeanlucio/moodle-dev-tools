"""Deterministic checks for moodle-security-audit (Phase A).

Each check looks for one pattern that MDL Shield has reported as a finding in a public review
(the rule ids point at Layer 4 of security-rules.md, where each case is cited). Grep-level
precision is enough to raise a candidate, not to confirm one: `curl_init()` inside a wrapper
that already enforces the egress rules is fine, `error_log()` in a CLI script is fine. So
almost every check yields a *candidate* that goes through the same Phase D verification as
the AI scan's candidates. Only facts that need no judgement (a .DS_Store in the package, an
implicitly nullable parameter) are marked `deterministic` and skip verification.

All occurrences of one check are folded into a single finding with `extra_locations`, the way
MDL Shield reports a pattern once with every place it occurs — it also keeps Phase D at one
call per check rather than one per line.
"""

import re
import subprocess
from pathlib import Path

# Release branching versions, used to turn $plugin->requires into "Moodle X.Y". Taken from
# each release tag's version.php; a requires newer than the last entry is simply not checked.
MOODLE_RELEASES = [
    (2022041900, '4.0'), (2022112800, '4.1'), (2023042400, '4.2'), (2023100900, '4.3'),
    (2024042200, '4.4'), (2024100700, '4.5'), (2025041400, '5.0'), (2025100600, '5.1'),
    (2026042000, '5.2'),
]

JUNK_FILE_NAMES = {'.DS_Store', 'Thumbs.db', 'desktop.ini'}
JUNK_FILE_SUFFIXES = ('.swp', '.swo', '.orig', '.rej', '~')

# Core tables whose rows belong to another component's API. Writing them directly skips the
# events, cache invalidation and validation that API performs (L4-API-1).
CORE_TABLES = (
    'user', 'user_info_data', 'user_preferences', 'course', 'course_modules',
    'course_sections', 'grade_items', 'grade_grades', 'groups', 'groups_members',
    'groupings', 'groupings_groups', 'role_assignments', 'user_enrolments', 'enrol',
    'context', 'files', 'config', 'config_plugins', 'event', 'tag_instance',
)
DB_WRITE_RE = re.compile(
    r"\$DB->(insert_record|insert_records|update_record|delete_records(?:_select|_list)?|"
    r"set_field(?:_select)?)\(\s*['\"](" + '|'.join(CORE_TABLES) + r")['\"]")

FUNCTION_SIGNATURE_RE = re.compile(r'\bfunction\s*&?\s*\w*\s*\((.*?)\)\s*(?::|\{|;|use\b)',
                                   re.S)
IMPLICIT_NULLABLE_RE = re.compile(
    r'(?<![?|\w\\])((?:\\?[A-Za-z_][\w\\]*))\s+&?(?:\.\.\.)?\$\w+\s*=\s*null\b', re.I)


def _git_files(plugin_dir):
    """Files that would go into `git archive` (tracked and not export-ignore), or None."""
    try:
        listed = subprocess.run(['git', '-C', str(plugin_dir), 'ls-files', '-z'],
                                capture_output=True, text=True, timeout=60, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    files = [f for f in listed.stdout.split('\0') if f]
    if not files:
        return files
    try:
        attrs = subprocess.run(
            ['git', '-C', str(plugin_dir), 'check-attr', '-z', 'export-ignore', '--stdin'],
            input='\0'.join(files), capture_output=True, text=True, timeout=60, check=True)
    except (OSError, subprocess.SubprocessError):
        return files
    # -z output is a flat sequence of path, attribute, value triples.
    fields = attrs.stdout.split('\0')
    ignored = {fields[i] for i in range(0, len(fields) - 2, 3) if fields[i + 2] == 'set'}
    return [f for f in files if f not in ignored]


def _read(path):
    try:
        return path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return ''


def _line_of(content, offset):
    return content.count('\n', 0, offset) + 1


def _finding(check, locations, description_extra=''):
    """Fold every location of one check into a single candidate finding."""
    locations = list(dict.fromkeys(locations))
    first, rest = locations[0], locations[1:]
    description = check['description']
    if description_extra:
        description = f'{description}\n\n{description_extra}'
    return {
        'title': check['title'],
        'finding_type': check['finding_type'],
        'severity': check['severity'],
        'category': check['category'],
        'rule_id': check['rule_id'],
        'file': first[0],
        'line': first[1],
        'extra_locations': [{'file': f, 'line': ln} for f, ln in rest[:25]],
        'description': description,
        'exploitable_by': '',
        'impact': check.get('impact', ''),
        'mitigations': '',
        'recommendation': check['recommendation'],
        'source': 'static',
        'deterministic': check.get('deterministic', False),
    }


def _production_php(scan_files):
    """PHP files outside tests/ and cli/ — the code that runs on a live site."""
    for entry in scan_files:
        rel = entry['rel']
        if not rel.endswith('.php'):
            continue
        if rel.startswith(('tests/', 'cli/')):
            continue
        yield rel


def _grep(plugin_dir, rels, pattern, skip_line=None):
    locations = []
    for rel in rels:
        content = _read(plugin_dir / rel)
        lines = content.splitlines()
        for match in pattern.finditer(content):
            line = _line_of(content, match.start())
            text = lines[line - 1] if line <= len(lines) else ''
            if text.lstrip().startswith(('//', '*', '#')):
                continue
            if skip_line and skip_line(text):
                continue
            locations.append((rel, line))
    return locations


def check_junk_files(plugin_dir, scan_files):
    files = _git_files(plugin_dir)
    if files is None:
        files = [str(p.relative_to(plugin_dir)) for p in plugin_dir.rglob('*')
                 if p.is_file() and '.git/' not in str(p.relative_to(plugin_dir))]
    junk = [f for f in files
            if Path(f).name in JUNK_FILE_NAMES or f.endswith(JUNK_FILE_SUFFIXES)]
    if not junk:
        return []
    return [_finding({
        'title': 'Arquivos de sistema operacional ou de editor no pacote do plugin',
        'finding_type': 'code_quality', 'severity': 'low', 'category': 'packaging',
        'rule_id': 'L4-BP-4', 'deterministic': True,
        'description': 'Estes arquivos estão versionados e não têm export-ignore, então vão '
                       'para o ZIP gerado por `git archive` e publicado no Plugin Directory: '
                       + ', '.join(f'`{f}`' for f in junk[:10]) + '.',
        'recommendation': 'Remover do repositório (`git rm --cached`) e acrescentar o padrão '
                          'ao `.gitignore`.',
    }, [(f, 1) for f in junk])]


def check_logstore(plugin_dir, scan_files):
    locations = _grep(plugin_dir, _production_php(scan_files),
                      re.compile(r'\{logstore_standard_log\}'))
    if not locations:
        return []
    return [_finding({
        'title': 'Leitura direta de logstore_standard_log',
        'finding_type': 'code_quality', 'severity': 'low', 'category': 'core_api_misuse',
        'rule_id': 'L4-API-2',
        'description': 'O código consulta a tabela `{logstore_standard_log}` diretamente. Se o '
                       'administrador desativar o log padrão ou usar outro leitor, a consulta '
                       'volta vazia sem aviso, embora os eventos tenham sido registrados.',
        'recommendation': "Obter o leitor com get_log_manager()->get_readers("
                          "'\\core\\log\\sql_reader') e consultar com get_events_select().",
    }, locations)]


def check_raw_http(plugin_dir, scan_files):
    pattern = re.compile(r"\b(curl_init|curl_exec|fsockopen)\s*\(|"
                         r"\bfile_get_contents\s*\(\s*['\"]https?://")
    locations = _grep(plugin_dir, _production_php(scan_files), pattern)
    if not locations:
        return []
    return [_finding({
        'title': 'Requisição HTTP sem o cliente HTTP do core',
        'finding_type': 'code_quality', 'severity': 'low', 'category': 'core_api_misuse',
        'rule_id': 'L4-API-3',
        'description': 'Requisição feita com curl/fsockopen/file_get_contents crus, que não '
                       'aplicam o proxy, a lista de hosts bloqueados e as portas permitidas '
                       'configurados pelo administrador.',
        'recommendation': 'Usar \\core\\http_client ou a classe \\curl do core '
                          '(lib/filelib.php).',
    }, locations)]


def check_raw_download(plugin_dir, scan_files):
    pattern = re.compile(r'\breadfile\s*\(|header\s*\(\s*[\'"]Content-Disposition', re.I)
    locations = _grep(plugin_dir, _production_php(scan_files), pattern)
    if not locations:
        return []
    return [_finding({
        'title': 'Download montado com header()/readfile() em vez da API de arquivos',
        'finding_type': 'best_practice', 'severity': 'low', 'category': 'core_api_misuse',
        'rule_id': 'L4-API-4',
        'description': 'O arquivo é enviado com cabeçalhos e leitura manuais, sem passar pelas '
                       'funções do core que tratam cache, nome do arquivo e sessão.',
        'recommendation': 'Usar send_temp_file(), send_file(), send_stored_file() ou '
                          '\\core\\dataformat::download_data().',
    }, locations)]


def check_unserialize(plugin_dir, scan_files):
    locations = _grep(
        plugin_dir, _production_php(scan_files), re.compile(r'(?<![\w>:])unserialize\s*\('),
        skip_line=lambda text: 'allowed_classes' in text)
    if not locations:
        return []
    return [_finding({
        'title': 'unserialize() sem restringir as classes permitidas',
        'finding_type': 'code_quality', 'severity': 'low', 'category': 'core_api_misuse',
        'rule_id': 'L4-API-6',
        'description': 'unserialize() chamado sem a opção allowed_classes. Mesmo sobre dado '
                       'gravado pelo core, a chamada instancia qualquer classe presente no '
                       'valor serializado.',
        'impact': 'Se o valor puder vir do usuário, é injeção de objeto (security); sobre dado '
                  'só do core, é robustez.',
        'recommendation': "unserialize($valor, ['allowed_classes' => false]) ou "
                          'unserialize_object().',
    }, locations)]


def check_ddl_outside_upgrade(plugin_dir, scan_files):
    pattern = re.compile(r'->(create_table|drop_table|rename_table|add_field|drop_field|'
                         r'rename_field|change_field_\w+|add_index|drop_index|add_key|'
                         r'drop_key)\s*\(')
    rels = [r for r in _production_php(scan_files) if not r.startswith('db/')]
    locations = _grep(plugin_dir, rels, pattern)
    if not locations:
        return []
    return [_finding({
        'title': 'Alteração de schema (DDL) fora de db/upgrade.php',
        'finding_type': 'code_quality', 'severity': 'low', 'category': 'core_api_misuse',
        'rule_id': 'L4-API-5',
        'description': 'Operações do database_manager rodam fora do fluxo de upgrade, onde o '
                       'Moodle não controla versão, savepoint nem coerência com install.xml.',
        'recommendation': 'Mover a mudança de schema para um passo de db/upgrade.php.',
    }, locations)]


def check_core_table_writes(plugin_dir, scan_files):
    rels = [r for r in _production_php(scan_files)
            if not r.startswith(('db/', 'classes/privacy/'))]
    # Deleting the plugin's own rows from user_preferences by name prefix has no core API
    # equivalent (unset_user_preference() works one user at a time), so it is not flagged.
    locations = _grep(plugin_dir, rels, DB_WRITE_RE,
                      skip_line=lambda text: re.search(
                          r"delete_records\w*\(\s*['\"]user_preferences['\"]", text))
    if not locations:
        return []
    return [_finding({
        'title': 'Escrita direta em tabela de outro componente do core',
        'finding_type': 'code_quality', 'severity': 'low', 'category': 'core_api_misuse',
        'rule_id': 'L4-API-1',
        'description': 'O plugin grava direto numa tabela do core que tem API própria '
                       '(usuário, perfil, curso, módulo, nota, grupo, papel, matrícula). A '
                       'API dispara eventos, limpa caches e valida; a escrita direta pula '
                       'tudo isso.',
        'recommendation': 'Usar a API correspondente (user_update_user(), '
                          'profile_save_data(), course_update_module(), grade_update(), '
                          'groups_*(), role_assign()...).',
    }, locations)]


def check_implicit_nullable(plugin_dir, scan_files):
    locations = []
    for rel in (e['rel'] for e in scan_files if e['rel'].endswith('.php')):
        content = _read(plugin_dir / rel)
        for signature in FUNCTION_SIGNATURE_RE.finditer(content):
            for param in IMPLICIT_NULLABLE_RE.finditer(signature.group(1)):
                if param.group(1).lower() in ('mixed', 'null'):
                    continue
                locations.append((rel, _line_of(content, signature.start(1) + param.start())))
    if not locations:
        return []
    return [_finding({
        'title': 'Parâmetro implicitamente nullable (depreciado no PHP 8.4)',
        'finding_type': 'code_quality', 'severity': 'low', 'category': 'deprecated_api',
        'rule_id': 'L4-HYG-3', 'deterministic': True,
        'description': 'Parâmetro tipado com valor padrão null mas sem `?Tipo` ou '
                       '`Tipo|null`. O PHP 8.4 emite aviso de depreciação para essa forma.',
        'recommendation': 'Declarar o tipo como `?Tipo $param = null`.',
    }, locations)]


def check_debug_leftovers(plugin_dir, scan_files):
    php = re.compile(r'\b(error_log|var_dump|var_export|debug_zval_dump)\s*\(|'
                     r'\bprint_r\s*\((?![^;]*,\s*true\s*\))')
    js = re.compile(r'\bconsole\.(log|debug)\s*\(|\bdebugger\s*;')
    locations = _grep(plugin_dir, _production_php(scan_files), php)
    js_rels = [e['rel'] for e in scan_files
               if e['rel'].startswith('amd/src/') and e['rel'].endswith('.js')]
    locations += _grep(plugin_dir, js_rels, js)
    if not locations:
        return []
    return [_finding({
        'title': 'Saída de depuração em código de produção',
        'finding_type': 'code_quality', 'severity': 'low', 'category': 'debug_leftover',
        'rule_id': 'L4-HYG-4',
        'description': 'Chamadas de depuração (error_log, var_dump, print_r, console.log) em '
                       'caminho que roda no site em produção.',
        'recommendation': 'Remover, ou trocar por debugging(..., DEBUG_DEVELOPER) quando a '
                          'mensagem for útil a quem desenvolve.',
    }, locations)]


def check_mod_form_validation(plugin_dir, scan_files):
    path = plugin_dir / 'mod_form.php'
    content = _read(path)
    if not content or re.search(r'function\s+validation\s*\(', content):
        return []
    numeric = [m for m in re.finditer(r"setType\(\s*'(\w+)'\s*,\s*PARAM_(INT|FLOAT)", content)]
    if not numeric:
        return []
    fields = ', '.join(sorted({m.group(1) for m in numeric}))
    return [_finding({
        'title': 'Formulário da atividade sem validation() para campos numéricos',
        'finding_type': 'code_quality', 'severity': 'low', 'category': 'input_validation',
        'rule_id': 'L4-ROB-1',
        'description': f'mod_form.php declara campos numéricos ({fields}) e não sobrescreve '
                       'validation(). Confirme se algum valor fora da faixa (zero, negativo, '
                       'mínimo maior que máximo) deixa a atividade inutilizável.',
        'recommendation': 'Sobrescrever validation() com os limites de cada campo e as '
                          'relações entre eles, com mensagens de erro traduzidas.',
    }, [('mod_form.php', _line_of(content, numeric[0].start()))])]


def check_lib_define_guard(plugin_dir, scan_files):
    content = _read(plugin_dir / 'lib.php')
    if not content or 'MOODLE_INTERNAL' in content:
        return []
    match = re.search(r'^define\s*\(', content, re.M)
    if not match:
        return []
    return [_finding({
        'title': 'lib.php com define() no escopo global sem a guarda MOODLE_INTERNAL',
        'finding_type': 'code_quality', 'severity': 'low', 'category': 'robustness',
        'rule_id': 'L4-HYG-6',
        'description': 'lib.php define constantes globais com define() e não tem a guarda '
                       'MOODLE_INTERNAL. O MDL Shield reporta isso como low; o moodle-cs, por '
                       'outro lado, trata define() como declaração e acusa "Unexpected '
                       'MOODLE_INTERNAL check" se a guarda for acrescentada.',
        'recommendation': 'Trocar os define() por constantes de uma classe autoloaded (ex.: '
                          '\\<componente>\\local\\constants::NOME), o que satisfaz os dois '
                          'verificadores. Acrescentar a guarda resolve o MDL Shield mas quebra '
                          'o PHPCS.',
    }, [('lib.php', _line_of(content, match.start()))])]


PLUGIN_CI_COMMANDS = ('install', 'phplint', 'phpcpd', 'phpmd', 'codechecker', 'phpcs',
                      'phpdoc', 'validate', 'savepoints', 'mustache', 'grunt', 'phpunit',
                      'behat')
COMMENTED_CI_STEP_RE = re.compile(
    r'^\s*#\s*(?:-\s*)?(?:run:\s*)?moodle-plugin-ci\s+(?:' + '|'.join(PLUGIN_CI_COMMANDS)
    + r')\b', re.M)


def check_ci_disabled(plugin_dir, scan_files):
    """moodle-plugin-ci steps commented out, set to never run, or set to never fail.

    `continue-on-error: true` on the phpmd step is skipped: moodle-plugin-ci's own
    gha.dist.yml template ships it that way, so it is the expected configuration.
    """
    rels = [e['rel'] for e in scan_files if e['rel'].startswith('.github/workflows/')]
    locations = []
    for rel in rels:
        content = _read(plugin_dir / rel)
        locations += [(rel, _line_of(content, m.start()))
                      for m in COMMENTED_CI_STEP_RE.finditer(content)]
        # A step runs from its "- name:"/"- uses:"/"- run:" line to the next one.
        starts = [m.start() for m in re.finditer(r'^\s*-\s+(?:name|uses|run):', content,
                                                  re.M)]
        for index, start in enumerate(starts):
            end = starts[index + 1] if index + 1 < len(starts) else len(content)
            step = content[start:end]
            disabled = re.search(r'^\s*if:\s*false\b', step, re.M)
            no_fail = re.search(r'continue-on-error:\s*true', step)
            if no_fail and re.search(r'moodle-plugin-ci\s+phpmd\b', step):
                no_fail = None
            if disabled or no_fail:
                locations.append((rel, _line_of(content, start)))
    if not locations:
        return []
    return [_finding({
        'title': 'Workflow de CI com checagens desligadas',
        'finding_type': 'best_practice', 'severity': 'low', 'category': 'testing_ci',
        'rule_id': 'L4-BP-3',
        'description': 'O workflow tem passos do moodle-plugin-ci comentados, marcados para '
                       'nunca rodar (if: false) ou para não falhar o job '
                       '(continue-on-error: true).',
        'recommendation': 'Reativar as checagens, ou documentar por que cada uma está '
                          'desligada.',
    }, sorted(set(locations)))]


def _release_of(requires):
    """The Moodle release a requires value points at, or None when it is past the table.

    A requires slightly above a branching version (a weekly build of the same release) still
    maps to it; one more than a month past the newest known release is a release this table
    does not know yet, and guessing would report a false contradiction.
    """
    if requires > MOODLE_RELEASES[-1][0] + 10000:
        return None
    name = None
    for version, release in MOODLE_RELEASES:
        if requires >= version:
            name = release
    return name


def check_readme_requirement(plugin_dir, scan_files, version_info):
    requires = version_info.get('requires')
    if not requires:
        return []
    release = _release_of(int(requires))
    if not release:
        return []
    readmes = [e['rel'] for e in scan_files if '/' not in e['rel']
               and e['rel'].lower().startswith('readme')]
    pattern = re.compile(r'(?i)(requires?|requirement|minimum|at least|compatible|'
                         r'requer|mínim[oa])[^\n]{0,60}?Moodle\s+(\d\.\d{1,2})')
    locations, stated = [], set()
    for rel in readmes:
        content = _read(plugin_dir / rel)
        for match in pattern.finditer(content):
            if match.group(2) != release:
                stated.add(match.group(2))
                locations.append((rel, _line_of(content, match.start())))
    if not locations:
        return []
    return [_finding({
        'title': 'README declara requisito de Moodle diferente do version.php',
        'finding_type': 'code_quality', 'severity': 'low', 'category': 'packaging',
        'rule_id': 'L4-BP-4',
        'description': f'version.php declara requires = {requires} (Moodle {release}), mas o '
                       f'README menciona Moodle {", ".join(sorted(stated))} como requisito. '
                       'Confirme se a frase é mesmo o requisito mínimo e não, por exemplo, '
                       'a versão testada mais recente.',
        'recommendation': 'Alinhar o README ao $plugin->requires (e ao $plugin->supported).',
    }, locations)]


def check_superglobals(plugin_dir, scan_files):
    locations = _grep(plugin_dir, _production_php(scan_files),
                      re.compile(r'\$_(GET|POST|REQUEST|COOKIE|SERVER|FILES)\b'))
    if not locations:
        return []
    return [_finding({
        'title': 'Acesso direto a superglobais ($_GET, $_POST, $_REQUEST, $_SERVER...)',
        'finding_type': 'code_quality', 'severity': 'low', 'category': 'core_api_misuse',
        'rule_id': 'L1-INPUT-1',
        'description': 'O código lê superglobais diretamente em vez de usar '
                       'optional_param()/required_param() ou as APIs do core para dados do '
                       'servidor. Sem limpeza por PARAM_*, o valor chega cru ao código.',
        'recommendation': 'optional_param()/required_param() com o PARAM_* adequado; para '
                          '$_SERVER, getremoteaddr(), qualified_me() ou $PAGE->url.',
    }, locations)]


def _thirdparty_locations(plugin_dir):
    """Paths declared in thirdpartylibs.xml — bundled code the plugin does not own."""
    content = _read(plugin_dir / 'thirdpartylibs.xml')
    return [loc.strip().rstrip('/')
            for loc in re.findall(r'<location>(.*?)</location>', content, re.S)]


def _is_thirdparty(rel, locations):
    return any(rel == loc or rel.startswith(loc + '/') for loc in locations)


def check_gpl_header(plugin_dir, scan_files):
    thirdparty = _thirdparty_locations(plugin_dir)
    missing = []
    for entry in scan_files:
        rel = entry['rel']
        if not rel.endswith('.php') or _is_thirdparty(rel, thirdparty):
            continue
        head = '\n'.join(_read(plugin_dir / rel).splitlines()[:40])
        if 'GNU General Public License' not in head:
            missing.append((rel, 1))
    if not missing:
        return []
    return [_finding({
        'title': 'Arquivo PHP sem o cabeçalho de licença GPL',
        'finding_type': 'code_quality', 'severity': 'low', 'category': 'packaging',
        'rule_id': 'L4-HYG-9', 'deterministic': True,
        'description': 'Estes arquivos não têm o cabeçalho GPL padrão do Moodle nas primeiras '
                       'linhas, que o Plugin Directory exige em todo arquivo PHP.',
        'recommendation': 'Acrescentar o cabeçalho GPL padrão logo após <?php.',
    }, missing)]


# Directory of each plugin type relative to the Moodle root, for the install.xml PATH check.
PLUGIN_TYPE_DIRS = {
    'mod': 'mod', 'block': 'blocks', 'local': 'local', 'filter': 'filter',
    'availability': 'availability/condition', 'format': 'course/format', 'report': 'report',
    'tool': 'admin/tool', 'tiny': 'lib/editor/tiny/plugins', 'qtype': 'question/type',
    'qbehaviour': 'question/behaviour', 'quizaccess': 'mod/quiz/accessrule',
    'quiz': 'mod/quiz/report', 'enrol': 'enrol', 'auth': 'auth', 'theme': 'theme',
    'customfield': 'customfield/field', 'aiprovider': 'ai/provider',
    'aiplacement': 'ai/placement', 'logstore': 'admin/tool/log/store',
    'assignsubmission': 'mod/assign/submission', 'assignfeedback': 'mod/assign/feedback',
    'gradereport': 'grade/report', 'profilefield': 'user/profile/field',
    'repository': 'repository', 'message': 'message/output', 'booktool': 'mod/book/tool',
    'datafield': 'mod/data/field', 'certificateelement': 'mod/customcert/element',
}


def check_install_xml_path(plugin_dir, scan_files, version_info):
    component = version_info.get('component') or ''
    if '_' not in component:
        return []
    plugintype, name = component.split('_', 1)
    typedir = PLUGIN_TYPE_DIRS.get(plugintype)
    content = _read(plugin_dir / 'db' / 'install.xml')
    match = re.search(r'<XMLDB\b[^>]*\bPATH="([^"]*)"', content)
    if not typedir or not match:
        return []
    expected = f'{typedir}/{name}/db'
    if match.group(1).strip('/') == expected:
        return []
    return [_finding({
        'title': 'db/install.xml com PATH de outro componente',
        'finding_type': 'code_quality', 'severity': 'low', 'category': 'packaging',
        'rule_id': 'L4-HYG-9', 'deterministic': True,
        'description': f'O atributo PATH do <XMLDB> é "{match.group(1)}", mas este plugin fica '
                       f'em "{expected}" — sinal de cabeçalho copiado de outro componente.',
        'recommendation': f'Trocar o PATH para "{expected}" (e conferir o COMMENT).',
    }, [('db/install.xml', _line_of(content, match.start()))])]


def check_behat_in_production(plugin_dir, scan_files):
    pattern = re.compile(r'lib/behat/|behat_util::|/tests/behat/')
    locations = _grep(plugin_dir, _production_php(scan_files), pattern,
                      skip_line=lambda text: 'BEHAT_SITE_RUNNING' in text)
    if not locations:
        return []
    return [_finding({
        'title': 'Infraestrutura de teste do Behat carregada por código de produção',
        'finding_type': 'code_quality', 'severity': 'low', 'category': 'robustness',
        'rule_id': 'L4-HYG-7',
        'description': 'Código que roda em requisições normais inclui ou chama a '
                       'infraestrutura do Behat, em geral só para descobrir se o site é de '
                       'teste.',
        'recommendation': "Usar defined('BEHAT_SITE_RUNNING') e não carregar nada de "
                          'lib/behat/ fora dos testes.',
    }, locations)]


REMOTE_CSS_RE = re.compile(r"(@import\s+url\(|url\()\s*['\"]?https?://", re.I)
REMOTE_MARKUP_RE = re.compile(r"<(script|link)\b[^>]*\b(src|href)\s*=\s*['\"]https?://", re.I)


def check_remote_resources(plugin_dir, scan_files):
    locations = []
    for entry in scan_files:
        rel = entry['rel']
        if rel.endswith('.css'):
            locations += _grep(plugin_dir, [rel], REMOTE_CSS_RE)
        elif rel.endswith(('.mustache', '.php')) and not rel.startswith(('tests/', 'cli/')):
            locations += _grep(plugin_dir, [rel], REMOTE_MARKUP_RE)
    if not locations:
        return []
    return [_finding({
        'title': 'Recurso remoto (fonte, script, estilo) carregado em tempo de execução',
        'finding_type': 'compliance', 'severity': 'low', 'category': 'privacy_api',
        'rule_id': 'L4-PRIV-2',
        'description': 'A página carrega um recurso de um servidor externo, o que entrega o IP '
                       'e o navegador do usuário a um terceiro. Biblioteca precisa ser '
                       'empacotada; serviço externo legítimo precisa ser declarado no Privacy '
                       'Provider.',
        'recommendation': 'Empacotar a fonte/biblioteca no plugin (declarada em '
                          'thirdpartylibs.xml) ou, se for um serviço, declará-lo com '
                          'add_external_location_link().',
    }, locations)]


# Only directory names that mean "someone else's code"; classes/external/ (web services) and
# lib/ (often the plugin's own helpers) would drown the real hits.
VENDORED_RE = re.compile(r'(^|/)(vendor|thirdparty|third_party|libraries)/.+\.(js|css|php)$')


def check_bundled_without_thirdpartylibs(plugin_dir, scan_files):
    if (plugin_dir / 'thirdpartylibs.xml').is_file():
        return []
    files = _git_files(plugin_dir)
    if files is None:
        files = [str(p.relative_to(plugin_dir)) for p in plugin_dir.rglob('*') if p.is_file()]
    hits = [f for f in files
            if not f.startswith(('amd/build/', 'tests/', 'node_modules/', '.git/'))
            and (re.search(r'\.min\.(js|css)$', f) or VENDORED_RE.search(f))]
    if not hits:
        return []
    return [_finding({
        'title': 'Possível biblioteca de terceiro empacotada sem thirdpartylibs.xml',
        'finding_type': 'compliance', 'severity': 'low', 'category': 'packaging',
        'rule_id': 'L4-PRIV-2',
        'description': 'O pacote tem arquivos com cara de biblioteca de terceiro (minificados '
                       'fora de amd/build, ou numa pasta vendor/lib/thirdparty) e não tem '
                       'thirdpartylibs.xml. Confirme se o código é de terceiro.',
        'recommendation': 'Declarar cada biblioteca em thirdpartylibs.xml (nome, versão, '
                          'licença, local) com um readme_moodle.txt ao lado.',
    }, [(f, 1) for f in hits])]


CHECKS = [
    check_junk_files, check_logstore, check_raw_http, check_raw_download, check_unserialize,
    check_ddl_outside_upgrade, check_core_table_writes, check_implicit_nullable,
    check_debug_leftovers, check_mod_form_validation, check_lib_define_guard,
    check_ci_disabled, check_superglobals, check_gpl_header, check_behat_in_production,
    check_remote_resources, check_bundled_without_thirdpartylibs,
]


def run_static_checks(plugin_dir, scan_files, version_info):
    """Every check's candidate findings, in CHECKS order."""
    findings = []
    for check in CHECKS:
        findings.extend(check(plugin_dir, scan_files))
    findings.extend(check_readme_requirement(plugin_dir, scan_files, version_info))
    findings.extend(check_install_xml_path(plugin_dir, scan_files, version_info))
    return findings
