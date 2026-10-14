"""Deterministic checks that compare a plugin's schema and declarations with its code.

These are the checks that need no judgement: a column of install.xml that the backup never
saves, a table nothing ever deletes, a text field longer than its column. Each function returns
plain violation dicts, so the audit (audit_static_checks) and the standalone CLI
(static_checks.py) format them their own way.

A violation that is deliberate goes in `<plugin>/.plans/security-audit/accepted.json`, with the
reason. That folder is gitignored in every plugin, so it never ships in the Plugin Directory zip:

    {"backup_columns": {"table.column": "why it is not backed up", "table": "why"},
     "delete_cleanup": {"table": "why nothing deletes it"},
     "column_lengths": {"classes/form/x.php:field": "why", "classes/y.php": "why"},
     "user_preferences": {"block_x_pref": "why"},
     "weak_random": {"classes/z.php": "why"}}

An entry without a reason is ignored, so an exception can never be added silently.
"""

import json
import re
import xml.etree.ElementTree as ET

ACCEPTED_REL = '.plans/security-audit/accepted.json'

SKIP_DIRS = ('.git/', '.plans/', 'node_modules/', 'vendor/', 'thirdparty/', 'tests/', 'cli/',
             'lang/', 'amd/build/', 'yui/build/')
# Files that delete or write on behalf of a single user or an upgrade, not of an instance.
NOT_CLEANUP_SOURCES = ('classes/privacy/', 'db/upgrade.php', 'db/install.php', 'db/uninstall.php')
# Column keys of set_source_table() bound to a backup variable: the parent link, not data.
VAR_BOUND_RE = re.compile(r"'([A-Za-z0-9_]+)'\s*=>\s*backup::VAR_(\w+)")
OWNER_COLUMNS = ('courseid', 'course', 'cmid', 'blockinstanceid', 'instanceid', 'contextid')
SUBSTR_RE = re.compile(r'(?<![\w>:\\])substr\(')
WEAK_RANDOM_RE = re.compile(r'(?<![\w>:\\])(uniqid|rand|mt_rand|lcg_value)\(')
SECRET_NAME_RE = re.compile(r'token|secret|code|key|hash|passw|nonce|salt|invite|session', re.I)
HASH_WRAP_RE = re.compile(r'(?<![\w>:\\])(md5|sha1|dechex|base64_encode|hash)\(')


def load_accepted(plugin_dir):
    """The plugin's accepted exceptions: {check: {key: reason}}, reasons required."""
    path = plugin_dir / ACCEPTED_REL
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f'{ACCEPTED_REL} inválido: {exc}')
    accepted = {}
    for check, entries in (data or {}).items():
        if isinstance(entries, dict):
            accepted[check] = {str(k): str(v) for k, v in entries.items()
                               if isinstance(v, str) and v.strip()}
    return accepted


def read(path):
    try:
        return path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return ''


def strip_comments(source):
    """PHP source without comments, keeping line numbers (comment text becomes blank)."""
    def blank(match):
        return re.sub(r'[^\n]', ' ', match.group(0))
    source = re.sub(r'/\*.*?\*/', blank, source, flags=re.S)
    return re.sub(r'(?m)(?<![:\'"])//.*$|^\s*#.*$', blank, source)


def install_tables(plugin_dir):
    """{table: {column: {'type': str, 'length': int|None}}} from db/install.xml, or {}."""
    path = plugin_dir / 'db' / 'install.xml'
    if not path.is_file():
        return {}
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return {}
    tables = {}
    for table in root.iter('TABLE'):
        columns = {}
        for field in table.iter('FIELD'):
            length = field.get('LENGTH')
            columns[field.get('NAME')] = {
                'type': field.get('TYPE'),
                'length': int(length) if length and length.isdigit() else None,
            }
        tables[table.get('NAME')] = columns
    return tables


def install_foreign(plugin_dir):
    """{table: {column: referenced table}} for the foreign keys declared in db/install.xml."""
    path = plugin_dir / 'db' / 'install.xml'
    foreign = {}
    if not path.is_file():
        return foreign
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return foreign
    for table in root.iter('TABLE'):
        for key in table.iter('KEY'):
            if key.get('TYPE') == 'foreign' and key.get('FIELDS') and key.get('REFTABLE'):
                for column in key.get('FIELDS').split(','):
                    foreign.setdefault(table.get('NAME'), {})[column.strip()] = key.get('REFTABLE')
    return foreign


def install_line(plugin_dir, table, column=None):
    """Line of the table (or of one of its columns) in db/install.xml; 1 when not found."""
    source = read(plugin_dir / 'db' / 'install.xml')
    start = source.find(f'<TABLE NAME="{table}"')
    if start < 0:
        return 1
    if column:
        found = source.find(f'<FIELD NAME="{column}"', start)
        start = found if found >= 0 else start
    return source.count('\n', 0, start) + 1


def production_php(plugin_dir, extra_skip=()):
    """(relative path, source without comments) of every PHP file that runs on a live site."""
    files = []
    for path in sorted(plugin_dir.rglob('*.php')):
        rel = path.relative_to(plugin_dir).as_posix()
        if rel.startswith(SKIP_DIRS + tuple(extra_skip)):
            continue
        files.append((rel, strip_comments(read(path))))
    return files


def split_args(source, start):
    """Top-level arguments of the call whose '(' is just before `start`; ([args], end index)."""
    args, depth, current, i, quote = [], 0, [], start, None
    while i < len(source):
        char = source[i]
        if quote:
            current.append(char)
            if char == '\\':
                i += 1
                current.append(source[i:i + 1])
            elif char == quote:
                quote = None
        elif char in '\'"':
            quote = char
            current.append(char)
        elif char in '([{':
            depth += 1
            current.append(char)
        elif char in ')]}':
            if depth == 0:
                args.append(''.join(current).strip())
                return args, i
            depth -= 1
            current.append(char)
        elif char == ',' and depth == 0:
            args.append(''.join(current).strip())
            current = []
        else:
            current.append(char)
        i += 1
    return args, i


def _names(text):
    return set(re.findall(r"'([A-Za-z0-9_]+)'", text))


def _resolve_list(argument, source):
    """The quoted names of a list argument; None when it cannot be read statically."""
    if not argument:
        return set()
    if argument.startswith('$'):
        match = re.search(re.escape(argument) + r'\s*=\s*(\[|array\()', source)
        if not match:
            return None
        args, _ = split_args(source, match.end())
        return _names(args[0]) if args else None
    if re.search(r'\w\s*\(', argument.replace('array(', '')):
        return None
    return _names(argument)


def parse_backup(plugin_dir):
    """What the backup step files save: {table: {'columns': set|None, 'structural': set}}.

    None when the plugin has no backup/moodle2 step file. `columns` is None for a table whose
    attribute list cannot be read statically, so it is never judged.
    """
    files = sorted((plugin_dir / 'backup' / 'moodle2').glob('backup_*_stepslib.php'))
    if not files:
        return None
    saved = {}
    for path in files:
        source = strip_comments(read(path))
        elements = {}
        for match in re.finditer(r'\$(\w+)\s*=\s*new\s+backup_nested_element\(', source):
            args, _ = split_args(source, match.end())
            ids = _resolve_list(args[1], source) if len(args) > 1 else set()
            attrs = _resolve_list(args[2], source) if len(args) > 2 else set()
            columns = None if ids is None or attrs is None else ids | attrs
            elements[match.group(1)] = columns
        for match in re.finditer(r'\$(\w+)->set_source_table\(', source):
            args, _ = split_args(source, match.end())
            literal = re.fullmatch(r"'([a-z0-9_]+)'", args[0]) if args else None
            if not literal or match.group(1) not in elements:
                continue
            bound = VAR_BOUND_RE.findall(args[1]) if len(args) > 1 else []
            structural = {column for column, _ in bound}
            if any(var in ('ACTIVITYID', 'MODID') for _, var in bound):
                structural.add('course')
            _merge(saved, literal.group(1), elements[match.group(1)], structural)
        # A source built from SQL (often held in a variable or returned by a helper method): the
        # table is backed up, but its columns cannot be judged.
        for match in re.finditer(r'\bFROM\s+\{(\w+)\}', source):
            _merge(saved, match.group(1), None, set())
    return saved


def restore_assigned(plugin_dir):
    """Column names the restore step files assign explicitly ($data->column = ...)."""
    assigned = set()
    for path in (plugin_dir / 'backup' / 'moodle2').glob('restore_*_stepslib.php'):
        assigned |= set(re.findall(r'\$data->(\w+)\s*=', strip_comments(read(path))))
    return assigned


def _merge(saved, table, columns, structural):
    entry = saved.setdefault(table, {'columns': set(), 'structural': set()})
    if columns is None or entry['columns'] is None:
        entry['columns'] = None
    else:
        entry['columns'] |= columns
    entry['structural'] |= structural


def backup_columns(plugin_dir, accepted=None):
    """Columns (and tables) of install.xml that the course backup never saves (L2-BAK-1)."""
    accepted = (accepted if accepted is not None else load_accepted(plugin_dir)).get(
        'backup_columns', {})
    saved = parse_backup(plugin_dir)
    tables = install_tables(plugin_dir)
    if saved is None or not tables:
        return []
    foreign = install_foreign(plugin_dir)
    rebuilt = restore_assigned(plugin_dir)
    violations = []
    for table, columns in tables.items():
        if table in accepted:
            continue
        if table not in saved:
            violations.append({'table': table, 'column': None, 'file': 'db/install.xml',
                               'line': install_line(plugin_dir, table)})
            continue
        backed = saved[table]['columns']
        if backed is None:
            continue
        for column in columns:
            # A foreign key the restore sets itself (e.g. the activity id of a denormalised child).
            rebuilt_key = column in rebuilt and column in foreign.get(table, {})
            if (column == 'id' or column in backed or column in saved[table]['structural']
                    or rebuilt_key or f'{table}.{column}' in accepted):
                continue
            violations.append({'table': table, 'column': column, 'file': 'db/install.xml',
                               'line': install_line(plugin_dir, table, column)})
    return violations


def _deleted_tables(files, tables):
    """Tables of the plugin named by a delete in the given (rel, source) files."""
    deleted = set()
    for _, source in files:
        for match in re.finditer(r"delete_records(?:_select|_list)?\(\s*['\"]([a-z0-9_]+)['\"]", source):
            deleted.add(match.group(1))
        for match in re.finditer(r'DELETE\s+FROM\s+\{([a-z0-9_]+)\}', source, re.I):
            deleted.add(match.group(1))
        # A helper that deletes through a variable ($DB->delete_records($table, ...)): every table
        # name written in that file counts, since the list is built there.
        if re.search(r'delete_records(?:_select|_list)?\(\s*(\$|self::|static::)', source):
            for table in tables:
                if re.search(r"['\"]" + re.escape(table) + r"['\"]", source):
                    deleted.add(table)
    return deleted


def delete_cleanup(plugin_dir, accepted=None):
    """Tables nothing ever deletes, and course-keyed tables with no course_deleted observer."""
    accepted = (accepted if accepted is not None else load_accepted(plugin_dir)).get(
        'delete_cleanup', {})
    tables = install_tables(plugin_dir)
    if not tables:
        return []
    files = production_php(plugin_dir, NOT_CLEANUP_SOURCES)
    deleted = _deleted_tables(files, tables)
    # The Privacy Provider's all-users deletion counts when other code calls it: a course_deleted
    # observer or <modname>_delete_instance() that delegates to it makes it a cleanup path too.
    if any(re.search(r'delete_data_for_all_users_in_context\(', source) for _, source in files):
        provider = strip_comments(read(plugin_dir / 'classes' / 'privacy' / 'provider.php'))
        deleted |= _deleted_tables([('classes/privacy/provider.php', provider)], tables)
    component = re.search(r"\$plugin->component\s*=\s*'(\w+)'", read(plugin_dir / 'version.php'))
    plugintype = component.group(1).split('_')[0] if component else ''
    events = read(plugin_dir / 'db' / 'events.php')
    has_observer = 'course_deleted' in events
    foreign = install_foreign(plugin_dir)
    violations = []
    for table, columns in tables.items():
        if table in accepted:
            continue
        # Only a table that belongs to something that gets deleted (an instance, a course or a
        # parent row of the plugin) needs a cleanup; site-wide and per-user tables do not.
        owned = (any(c in columns for c in OWNER_COLUMNS)
                 or any(ref in tables for ref in foreign.get(table, {}).values()))
        if owned and table not in deleted:
            violations.append({'table': table, 'kind': 'never_deleted', 'file': 'db/install.xml',
                               'line': install_line(plugin_dir, table)})
        keyed = 'courseid' in columns or ('course' in columns and plugintype != 'mod')
        if keyed and not has_observer:
            violations.append({'table': table, 'kind': 'no_course_observer',
                               'file': 'db/install.xml', 'line': install_line(plugin_dir, table)})
    return violations


def _text_fields(source):
    """(field literal, dynamic?, options text, offset) of each text addElement() in a form."""
    fields = []
    for match in re.finditer(r"addElement\(\s*'(?:text|url)'\s*,\s*'([A-Za-z0-9_]+)'(\s*\.\s*\$\w+)?",
                             source):
        args, _ = split_args(source, match.start() + len('addElement('))
        fields.append((match.group(1), bool(match.group(2)), ' '.join(args[3:]), match.start()))
    return fields


def column_lengths(plugin_dir, accepted=None):
    """Text fields without a maxlength for their column, and substr() cut at a column length."""
    accepted = (accepted if accepted is not None else load_accepted(plugin_dir)).get(
        'column_lengths', {})
    tables = install_tables(plugin_dir)
    chars = {}
    for columns in tables.values():
        for name, info in columns.items():
            if info['type'] == 'char' and info['length']:
                chars.setdefault(name, set()).add(info['length'])
    if not chars:
        return []
    lengths = {length for found in chars.values() for length in found}
    violations = []
    for rel, source in production_php(plugin_dir):
        for literal, dynamic, options, offset in _text_fields(source):
            base = re.sub(r'^config_', '', literal)
            limits = [length for name, found in chars.items() for length in found
                      if (name.startswith(base) if dynamic else name == base)]
            if not limits:
                continue
            low, high = min(limits), max(limits)
            limit = low if low == high else f'{low}-{high}'
            pattern = re.escape(literal) + (r"'\s*\.\s*\$\w+" if dynamic else "'")
            rules = re.findall(r"addRule\(\s*'" + pattern + r"[^;]*?'maxlength'\s*,\s*(\d+)", source)
            rules += re.findall(r"'maxlength'\s*=>\s*(\d+)", options)
            line = source.count('\n', 0, offset) + 1
            key = f'{rel}:{literal}'
            if key in accepted or rel in accepted:
                continue
            if not rules or min(int(r) for r in rules) > high:
                violations.append({'kind': 'form', 'field': literal, 'limit': limit,
                                   'file': rel, 'line': line})
        for match in SUBSTR_RE.finditer(source):
            args, _ = split_args(source, match.end())
            if len(args) >= 3 and args[1] == '0' and args[2].isdigit() and int(args[2]) in lengths:
                if rel in accepted or f'{rel}:{source.count(chr(10), 0, match.start()) + 1}' in accepted:
                    continue
                violations.append({'kind': 'substr', 'limit': int(args[2]), 'file': rel,
                                   'line': source.count('\n', 0, match.start()) + 1})
    return violations


def user_preferences(plugin_dir, accepted=None):
    """User preference names the plugin writes that its Privacy Provider never mentions."""
    accepted = (accepted if accepted is not None else load_accepted(plugin_dir)).get(
        'user_preferences', {})
    provider = read(plugin_dir / 'classes' / 'privacy' / 'provider.php')
    mentioned = set(re.findall(r"['\"]([A-Za-z0-9_\-:.]+)['\"]", provider))
    violations = []
    seen = set()
    for rel, source in production_php(plugin_dir, NOT_CLEANUP_SOURCES[:1]):
        for match in re.finditer(r"(?<![\w>:\\])set_user_preference\(\s*'([A-Za-z0-9_\-:.]+)'(\s*\.)?",
                                 source):
            name, dynamic = match.group(1), bool(match.group(2))
            if name in accepted or name in seen:
                continue
            covered = any(m == name or (dynamic and m.startswith(name)) or
                          (m.endswith('_') and name.startswith(m)) for m in mentioned)
            if not covered:
                seen.add(name)
                violations.append({'name': name, 'file': rel,
                                   'line': source.count('\n', 0, match.start()) + 1})
    return violations


def weak_random(plugin_dir, accepted=None):
    """Predictable generators (uniqid, rand, mt_rand) that feed a token, code or secret."""
    accepted = (accepted if accepted is not None else load_accepted(plugin_dir)).get(
        'weak_random', {})
    violations = []
    for rel, source in production_php(plugin_dir):
        if rel in accepted:
            continue
        for number, line in enumerate(source.split('\n'), 1):
            call = WEAK_RANDOM_RE.search(line)
            if not call:
                continue
            before = line[:call.start()]
            secret = SECRET_NAME_RE.search(before.split('=>')[0] if '=>' in before else before)
            hashed = HASH_WRAP_RE.search(before) and call.group(1) in ('uniqid', 'rand', 'mt_rand')
            if secret or hashed:
                violations.append({'function': call.group(1), 'file': rel, 'line': number})
    return violations


# How each violation kind is reported: one spec per kind, shared by the audit and the CLI.
SPECS = {
    ('backup_columns', None): {
        'sweep_pattern': (
            'Rode `moodle-static-checks <tipo/nome> --check backup_columns`: ele já varre todas as tabelas do plugin. Em seguida, confira o `restore_*_stepslib.php` (cada FK remapeada com `get_mappingid()`).'),
        'title': 'Coluna do install.xml que o backup do curso não salva',
        'finding_type': 'code_quality', 'severity': 'low', 'category': 'robustness',
        'rule_id': 'L2-BAK-1',
        'description': 'Toda coluna do install.xml precisa estar no backup_nested_element da sua '
                       'tabela (ou ser a chave do pai); toda tabela precisa de um elemento de '
                       'backup. A coluna que falta volta ao valor padrão do banco a cada '
                       'restauração ou duplicação, sem erro nenhum.',
        'recommendation': 'Acrescentar a coluna ao array de atributos do elemento (e, se for FK '
                          'para outra tabela do plugin, remapear no restore com get_mappingid()). '
                          'Se a omissão é deliberada, registrar em accepted.json com o motivo.',
    },
    ('delete_cleanup', 'never_deleted'): {
        'sweep_pattern': (
            'Rode `moodle-static-checks <tipo/nome> --check delete_cleanup`: ele já varre todas as tabelas. Confira também se o hook de exclusão (`delete_instance`, `instance_delete`, observer de `course_deleted`) realmente é chamado.'),
        'title': 'Tabela do plugin que nenhum código apaga',
        'finding_type': 'code_quality', 'severity': 'low', 'category': 'robustness',
        'rule_id': 'L2-DEL-1',
        'description': 'Nenhum delete_records*/DELETE do plugin (fora do Privacy Provider, do '
                       'upgrade e dos testes) cita a tabela. Ao apagar a instância ou o curso as '
                       'linhas ficam órfãs.',
        'recommendation': 'Apagar a tabela em <modname>_delete_instance(), instance_delete() ou '
                          'no observer de course_deleted. Se ela é limpa por outro caminho '
                          '(contexto, tarefa agendada), registrar em accepted.json com o motivo.',
    },
    ('delete_cleanup', 'no_course_observer'): {
        'sweep_pattern': (
            'Rode `moodle-static-checks <tipo/nome> --check delete_cleanup`: ele já lista todas as tabelas chaveadas por curso sem observer.'),
        'title': 'Tabela chaveada por curso sem observer de course_deleted',
        'finding_type': 'code_quality', 'severity': 'low', 'category': 'robustness',
        'rule_id': 'L2-DEL-2',
        'description': 'O Moodle não faz cascata de tabelas de plugin ao apagar um curso, e o '
                       'KEY TYPE="foreign" do install.xml é só documentação. Sem observer de '
                       '\\core\\event\\course_deleted, as linhas do curso apagado ficam órfãs.',
        'recommendation': 'Registrar um observer de \\core\\event\\course_deleted em '
                          'db/events.php que apague a tabela (ou chame o '
                          'delete_data_for_all_users_in_context() do Privacy Provider). Tabela '
                          'puramente de log pode ir para accepted.json com o motivo.',
    },
    ('column_lengths', 'form'): {
        'sweep_pattern': (
            'Rode `moodle-static-checks <tipo/nome> --check column_lengths`. Depois confira os caminhos que gravam sem formulário (web services, assistente, importação, chamadas de IA): o valor chega truncado ao tamanho da coluna?'),
        'title': 'Campo de texto sem maxlength para a coluna char(N) que o recebe',
        'finding_type': 'code_quality', 'severity': 'low', 'category': 'input_validation',
        'rule_id': 'L4-ROB-1',
        'description': 'O campo grava numa coluna char com limite, mas o formulário não tem a '
                       'regra maxlength (ou a regra é maior que a coluna). No PostgreSQL e no '
                       'MySQL estrito o insert falha com dml_write_exception e o professor '
                       'perde o formulário preenchido.',
        'recommendation': "$mform->addRule('<campo>', get_string('maximumchars', '', N), "
                          "'maxlength', N, 'client'); o HTML_QuickForm também valida no servidor.",
    },
    ('column_lengths', 'substr'): {
        'sweep_pattern': (
            'Rode `moodle-static-checks <tipo/nome> --check column_lengths`. Procure também `strlen(`, `str_pad(`, `strtolower(` e `ucfirst(` sobre texto de usuário: todos contam bytes.'),
        'title': 'substr() em bytes cortando texto no tamanho de uma coluna',
        'finding_type': 'code_quality', 'severity': 'low', 'category': 'robustness',
        'rule_id': 'L4-BUG-1',
        'description': 'substr() conta bytes: cortado no tamanho da coluna, um texto com caractere '
                       'multibyte pode terminar no meio dele, e o PostgreSQL rejeita a sequência '
                       'UTF-8 inválida.',
        'recommendation': 'Usar core_text::substr($texto, 0, N), que corta por caractere.',
    },
    ('user_preferences', None): {
        'sweep_pattern': (
            'Rode `moodle-static-checks <tipo/nome> --check user_preferences`. Confira também `get_user_preferences()` com nome dinâmico e preferências gravadas em outros arquivos.'),
        'title': 'Preferência de usuário gravada e não declarada no Privacy Provider',
        'finding_type': 'compliance', 'severity': 'low', 'category': 'privacy_api',
        'rule_id': 'L4-PRIV-1',
        'description': 'O plugin grava uma preferência de usuário que o Privacy Provider não '
                       'menciona, então ela não é declarada, exportada nem apagada.',
        'recommendation': 'Declarar com add_user_preference(), exportar em '
                          'export_user_preferences() e apagar em delete_user_preferences().',
    },
    ('weak_random', None): {
        'sweep_pattern': (
            'Rode `moodle-static-checks <tipo/nome> --check weak_random`. Procure ainda `md5(` e `sha1(` sobre `time()`, `id` ou `timecreated` usados como código ou token.'),
        'title': 'Token ou código gerado com função previsível (uniqid/rand/mt_rand)',
        'finding_type': 'security', 'severity': 'low', 'category': 'confidential_info_leakage',
        'rule_id': 'L3-RAND-1',
        'description': 'uniqid(), rand() e mt_rand() são previsíveis; um valor que funciona como '
                       'token, código de convite ou segredo pode ser adivinhado.',
        'recommendation': 'Usar random_string() do core ou random_int()/random_bytes().',
    },
}


def spec_key(check, violation):
    """Which SPECS entry reports this violation."""
    if check == 'delete_cleanup':
        return (check, violation['kind'])
    if check == 'column_lengths':
        return (check, violation['kind'])
    return (check, None)


def describe(check, violation):
    """One human line for a violation."""
    where = f"{violation['file']}:{violation['line']}"
    if check == 'backup_columns':
        if violation['column'] is None:
            return f"{where}  tabela {violation['table']} sem elemento no backup"
        return f"{where}  {violation['table']}.{violation['column']} não está no backup"
    if check == 'delete_cleanup':
        if violation['kind'] == 'never_deleted':
            return f"{where}  tabela {violation['table']} nunca é apagada"
        return f"{where}  tabela {violation['table']} é chaveada por curso e não há observer de course_deleted"
    if check == 'column_lengths':
        if violation['kind'] == 'form':
            return f"{where}  campo '{violation['field']}' sem maxlength {violation['limit']}"
        return f"{where}  substr() cortando em {violation['limit']} bytes"
    if check == 'user_preferences':
        return f"{where}  preferência '{violation['name']}' fora do Privacy Provider"
    return f"{where}  {violation['function']}() gerando token ou código"


CHECKS = {
    'backup_columns': backup_columns,
    'delete_cleanup': delete_cleanup,
    'column_lengths': column_lengths,
    'user_preferences': user_preferences,
    'weak_random': weak_random,
}
