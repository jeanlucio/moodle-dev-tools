#!/usr/bin/env python3
"""Checks that every translated lang string uses the same {$a} placeholders as English.

A translation that drops a placeholder loses the value it carried ("Respondida por estudantes"
instead of "Respondida por 12 estudantes"); one that names a property English does not pass, or
misspells it ({$a->nome} for {$a->name}), is printed literally, braces and all. Nothing else
checks this: PHPCS and the get_string gate only look at the English file, and the string still
"works" in the language the author tests in. Origin: Eduardo Kraus's moodle-plugin-validate, run
against this ecosystem on 15/10/2026 (no current violation; the check is preventive).

Every lang/<language>/ file other than English is compared with lang/en/ file of the same name,
for the keys present in both. A value that is not a single quoted literal (a concatenation, a
heredoc) is skipped rather than guessed. Missing keys are a different problem and not reported.
"""

import re
import sys
from pathlib import Path

PLACEHOLDER_RE = re.compile(r'\{\$a(?:->[A-Za-z_][A-Za-z0-9_]*)?\}')
# $string['key'] = 'value'; with either quote style; the value may contain escaped quotes.
ENTRY_RE = re.compile(
    r"""^[ \t]*\$string\[\s*(['"])(?P<key>[^'"]+)\1\s*\]\s*=\s*
        (?P<q>['"])(?P<value>(?:\\.|(?!(?P=q)).)*)(?P=q)\s*;""",
    re.MULTILINE | re.DOTALL | re.VERBOSE,
)


def entries(path: Path) -> dict[str, tuple[set[str], int]]:
    """Placeholders and line of every literal string in a lang file.

    @param Path $path the lang file
    @return dict[str, tuple[set[str], int]] key => (placeholders, 1-based line)
    """
    text = path.read_text(encoding='utf-8', errors='replace')
    found = {}
    for match in ENTRY_RE.finditer(text):
        value = match.group('value')
        if match.group('q') == '"':
            # In a double-quoted PHP string the placeholder has to be written as {\$a}.
            value = value.replace('\\$', '$')
        found[match.group('key')] = (
            set(PLACEHOLDER_RE.findall(value)),
            text.count('\n', 0, match.start()) + 1,
        )
    return found


def check_plugin(plugin_dir: Path) -> list[str]:
    """Checks every translation of a plugin against English.

    @param Path $plugin_dir absolute path to the plugin's repository root
    @return list[str] one entry per string whose placeholders differ; empty if clean
    """
    langroot = plugin_dir / 'lang'
    english = langroot / 'en'
    if not english.is_dir():
        return []

    problems = []
    for enfile in sorted(english.glob('*.php')):
        base = entries(enfile)
        for langdir in sorted(path for path in langroot.iterdir() if path.is_dir() and path.name != 'en'):
            translated = langdir / enfile.name
            if not translated.is_file():
                continue
            for key, (placeholders, line) in entries(translated).items():
                if key not in base or placeholders == base[key][0]:
                    continue
                missing = ', '.join(sorted(base[key][0] - placeholders)) or '—'
                extra = ', '.join(sorted(placeholders - base[key][0])) or '—'
                problems.append(
                    f"{translated}:{line}: $string['{key}'] usa placeholders diferentes do inglês "
                    f"(faltam: {missing}; sobram: {extra})"
                )
    return problems


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print('uso: check_lang_placeholders.py <caminho-do-plugin> [<caminho-do-plugin> ...]', file=sys.stderr)
        return 1

    all_problems = []
    for raw in argv[1:]:
        plugin_dir = Path(raw).resolve()
        if not plugin_dir.is_dir():
            print(f'erro: {plugin_dir} não existe', file=sys.stderr)
            return 1
        all_problems.extend(check_plugin(plugin_dir))

    for problem in all_problems:
        print(problem)

    if all_problems:
        print(f'\n{len(all_problems)} string(s) traduzida(s) com placeholders diferentes do inglês.', file=sys.stderr)
        return 1

    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
