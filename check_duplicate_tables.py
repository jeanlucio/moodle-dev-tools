#!/usr/bin/env python3
"""Checks that a plugin's db/install.xml never declares the same table NAME twice.

The simplest and most self-contained of this batch of checkers — no cross-file or
cross-component resolution needed, just one file parsed against itself. Uses a real XML
parser (xml.etree.ElementTree) rather than a regex, since install.xml is proper XML and a
parser handles attribute order/quoting/whitespace correctly for free.
"""

import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def check_install_xml(install_xml: Path) -> list[str]:
    """Checks one install.xml for duplicate TABLE NAME attributes.

    @param Path $install_xml the install.xml file to check
    @return list[str] one entry per duplicated table name; empty if clean or unparsable
    """
    try:
        tree = ET.parse(install_xml)
    except ET.ParseError as exc:
        # Malformed XML is a real problem, but a different one -- moodle-check-schema's
        # canonical-format round-trip already catches this more precisely. Skip rather
        # than duplicate that check's job with a worse error message.
        return []

    names = [table.get('NAME') for table in tree.getroot().iter('TABLE')]
    seen: set[str] = set()
    duplicates: list[str] = []
    for name in names:
        if name is None:
            continue
        if name in seen and name not in duplicates:
            duplicates.append(name)
        seen.add(name)

    return [f"{install_xml}: tabela '{name}' declarada mais de uma vez" for name in duplicates]


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print('uso: check_duplicate_tables.py <install.xml> [<install.xml> ...]', file=sys.stderr)
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
        print(f'\n{len(all_problems)} tabela(s) duplicada(s).', file=sys.stderr)
        return 1

    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
