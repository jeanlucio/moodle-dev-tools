#!/usr/bin/env python3
"""Checks that no INDEX in db/install.xml repeats the fields of a KEY of the same table.

Moodle already creates an index for every key it declares: the primary key has its own, and
for unique, foreign and foreign-unique keys the DDL generator adds the underlying index itself
(lib/ddl/sql_generator.php, getCreateTableSQL(), "add the indexes needed from keys"). An INDEX
with exactly the same fields therefore becomes a second, identical index in the database: more
disk and slower writes, no gain. Confirmed on a live site (15/10/2026): mdl_playerwords had two
indexes on course, one from the foreign key and one from the INDEX. Found in six plugins of this
ecosystem by Eduardo Kraus's moodle-plugin-validate; no other local tool caught it, because
the file is valid and installs without errors.

Fields are compared in order: an index on (a, b) serves queries a key on (b, a) cannot, so it
is not reported.
"""

import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def fields_of(element: ET.Element) -> tuple[str, ...]:
    """The ordered field names of a KEY or INDEX element.

    @param ET.Element $element the KEY or INDEX element
    @return tuple[str, ...] field names, whitespace removed
    """
    return tuple(name.strip() for name in (element.get('FIELDS') or '').split(',') if name.strip())


def line_of(source: str, table: str, index: str) -> int:
    """Line of an INDEX inside its TABLE in install.xml, or of the table when not found.

    @param str $source the install.xml text
    @param str $table the table name
    @param str $index the index name
    @return int 1-based line number
    """
    start = source.find(f'<TABLE NAME="{table}"')
    if start < 0:
        return 1
    found = source.find(f'<INDEX NAME="{index}"', start)
    position = found if found >= 0 else start
    return source.count('\n', 0, position) + 1


def check_install_xml(install_xml: Path) -> list[str]:
    """Checks one install.xml for indexes that duplicate a key.

    @param Path $install_xml the install.xml file to check
    @return list[str] one entry per redundant index; empty if clean or unparsable
    """
    try:
        root = ET.parse(install_xml).getroot()
    except ET.ParseError:
        # Malformed XML is moodle-check-schema's job; it reports it more precisely.
        return []
    source = install_xml.read_text(encoding='utf-8', errors='replace')

    problems = []
    for table in root.iter('TABLE'):
        tablename = table.get('NAME') or '?'
        keys = {}
        for key in table.iter('KEY'):
            fields = fields_of(key)
            if fields:
                keys.setdefault(fields, key)
        for index in table.iter('INDEX'):
            key = keys.get(fields_of(index))
            if key is None:
                continue
            indexname = index.get('NAME') or '?'
            fields = ', '.join(fields_of(index))
            keytype = key.get('TYPE') or '?'
            if index.get('UNIQUE') == 'true' and keytype == 'foreign':
                fix = "troque a chave para TYPE=\"foreign-unique\" e remova o INDEX"
            else:
                fix = "remova o INDEX"
            problems.append(
                f"{install_xml}:{line_of(source, tablename, indexname)}: tabela '{tablename}': "
                f"índice '{indexname}' ({fields}) repete a chave '{key.get('NAME')}' ({keytype}), "
                f"para a qual o Moodle já cria um índice; {fix} (em plugin publicado, com um passo "
                f"de upgrade que faça drop_index)"
            )
    return problems


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print('uso: check_redundant_indexes.py <install.xml> [<install.xml> ...]', file=sys.stderr)
        return 1

    all_problems = []
    for raw in argv[1:]:
        install_xml = Path(raw).resolve()
        if not install_xml.is_file():
            continue
        all_problems.extend(check_install_xml(install_xml))

    for problem in all_problems:
        print(problem)

    if all_problems:
        print(f'\n{len(all_problems)} índice(s) repetindo chave.', file=sys.stderr)
        return 1

    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
