#!/usr/bin/env python3
"""Checks that the lang strings core builds from db/caches.php and db/messages.php exist.

Core never calls get_string() with these keys literally: it assembles them at runtime from the
array keys of the definition file ('cachedef_' . $area in cache/classes/definition.php,
'messageprovider:' . $name in message/renderer.php and the notification preferences). So the
get_string gate, which only reads literal calls, cannot see them, and a missing one only shows
up as [[cachedef_x]] on the cache admin page or [[messageprovider:x]] in a user's notification
preferences. Three plugins of this ecosystem had a missing cachedef_ string when this checker
was written (15/10/2026), found by Eduardo Kraus's moodle-plugin-validate.

Only the first level of each array is read: a cache or provider definition nests its own arrays
('defaults' => [...], 'invalidationevents' => [...]), and those keys are options, not names. A
key that is not a quoted literal (a constant, a concatenation) is skipped rather than guessed.
"""

import sys
from pathlib import Path

from check_capability_strings import STRING_KEY_RE, find_component_lang_file

# (definition file, array variable it assigns, lang key core builds from each array key).
SOURCES = (
    ('db/caches.php', 'definitions', 'cachedef_{}', 'cache'),
    ('db/messages.php', 'messageproviders', 'messageprovider:{}', 'provedor de mensagem'),
)


def skip_quoted(source: str, start: int) -> int:
    """Index just past the quoted string that opens at `start`, honouring backslash escapes.

    @param str $source the PHP source
    @param int $start index of the opening quote
    @return int index of the character after the closing quote (or the end of the source)
    """
    quote = source[start]
    i = start + 1
    while i < len(source):
        if source[i] == '\\':
            i += 2
            continue
        if source[i] == quote:
            return i + 1
        i += 1
    return len(source)


def top_level_keys(source: str, variable: str) -> list[tuple[str, int]]:
    """Literal keys of the array assigned to $variable, first nesting level only.

    @param str $source the PHP source of the definition file
    @param str $variable the array variable name, without '$'
    @return list[tuple[str, int]] (key, line number) in source order; empty if not found
    """
    marker = '$' + variable
    start = source.find(marker)
    while start >= 0:
        rest = source[start + len(marker):].lstrip()
        if rest.startswith('='):
            break
        start = source.find(marker, start + 1)
    if start < 0:
        return []

    # Move to the bracket that opens the array: '[' or the legacy 'array('.
    i = start + len(marker)
    while i < len(source) and source[i] not in '[(;':
        i += 1
    if i >= len(source) or source[i] == ';':
        return []

    keys = []
    depth = 0
    while i < len(source):
        char = source[i]
        if char in '\'"':
            end = skip_quoted(source, i)
            if depth == 1 and source[end:].lstrip().startswith('=>'):
                keys.append((source[i + 1:end - 1], source.count('\n', 0, i) + 1))
            i = end
            continue
        if source.startswith('//', i) or char == '#':
            newline = source.find('\n', i)
            i = len(source) if newline < 0 else newline
            continue
        if source.startswith('/*', i):
            close = source.find('*/', i + 2)
            i = len(source) if close < 0 else close + 2
            continue
        if char in '[(':
            depth += 1
        elif char in '])':
            depth -= 1
            if depth == 0:
                break
        i += 1
    return keys


def check_plugin(plugin_dir: Path) -> list[str]:
    """Checks one plugin, returning a list of human-readable problem descriptions.

    @param Path $plugin_dir absolute path to the plugin's repository root
    @return list[str] one entry per missing lang string; empty if clean
    """
    found = []
    for relative, variable, pattern, label in SOURCES:
        path = plugin_dir / relative
        if path.is_file():
            source = path.read_text(encoding='utf-8', errors='replace')
            for key, line in top_level_keys(source, variable):
                found.append((path, line, key, pattern.format(key), label))
    if not found:
        return []

    lang_file = find_component_lang_file(plugin_dir)
    if lang_file is None:
        return [
            f"{plugin_dir}: declara {len(found)} cache(s)/provedor(es) de mensagem mas não achei "
            f"lang/en/<component>.php pra conferir (version.php ausente ou ilegível)"
        ]

    defined_strings = set(STRING_KEY_RE.findall(lang_file.read_text(encoding='utf-8', errors='replace')))
    return [
        f"{path}:{line}: {label} '{key}' não tem string \"$string['{expected}']\" em {lang_file}"
        for path, line, key, expected, label in found
        if expected not in defined_strings
    ]


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print('uso: check_db_strings.py <caminho-do-plugin> [<caminho-do-plugin> ...]', file=sys.stderr)
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
        print(f'\n{len(all_problems)} string(s) de cache/mensagem faltando.', file=sys.stderr)
        return 1

    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
