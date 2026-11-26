#!/usr/bin/env python3
"""Context for the pre-commit AI review: the lines a diff hides but a verdict depends on.

The review only sees the changed hunks, so it has reported a `use` statement that was already in
the file as missing, and asked for a scoping comment that stood at the top of the stylesheet.
This prints, for each staged file, those unchanged lines taken from the staged version:

* PHP: the top-level `namespace` and `use` statements;
* JavaScript: the `import` statements;
* CSS: the comments ahead of the first rule, and any comment about scoping.

Usage: ai_file_context.py <repo root> <staged file>...
"""

import re
import subprocess
import sys

MAX_LINES_PER_FILE = 60

LICENSE_MARKER = 'GNU General Public License'

# Top level only: a trait `use` inside a class body is indented.
PHP_LINE = re.compile(r'^(?:namespace|use)\s')
JS_LINE = re.compile(r'^import[\s{]')
CSS_COMMENT = re.compile(r'/\*.*?\*/', re.DOTALL)
CSS_SCOPING = re.compile(r'\.path-|path[- ]class|scop', re.IGNORECASE)


def numbered_matches(source, pattern):
    """Returns the (line number, text) pairs of the lines that match the pattern."""
    return [(number, line) for number, line in enumerate(source.split('\n'), 1) if pattern.match(line)]


def css_lines(source):
    """Returns the lines of the file-level comments and of the comments about scoping.

    The licence block is left out. A comment is file-level when nothing but other comments and
    blank space comes before it.
    """
    found = []
    codebefore = False
    previousend = 0
    for match in CSS_COMMENT.finditer(source):
        if source[previousend:match.start()].strip():
            codebefore = True
        previousend = match.end()
        comment = match.group(0)
        if LICENSE_MARKER in comment:
            continue
        if codebefore and not CSS_SCOPING.search(comment):
            continue
        first = source.count('\n', 0, match.start()) + 1
        found.extend((first + offset, line) for offset, line in enumerate(comment.split('\n')))
    return found


def context_lines(path, source):
    """Returns the (line number, text) context of one file; empty for other file types."""
    if path.endswith('.php'):
        return numbered_matches(source, PHP_LINE)
    if path.endswith('.js'):
        return numbered_matches(source, JS_LINE)
    if path.endswith('.css'):
        return css_lines(source)
    return []


def staged_source(root, path):
    """Returns the staged content of a file, or None when git cannot show it."""
    result = subprocess.run(
        ['git', '-C', root, 'show', f':{path}'],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.decode('utf-8', errors='replace')


def build_context(root, paths):
    """Returns the context block of the staged files, or an empty string when there is none."""
    blocks = []
    for path in paths:
        source = staged_source(root, path)
        if source is None:
            continue
        lines = context_lines(path, source)
        if not lines:
            continue
        shown = [f'L{number}: {text}' for number, text in lines[:MAX_LINES_PER_FILE]]
        if len(lines) > MAX_LINES_PER_FILE:
            shown.append(f'(+{len(lines) - MAX_LINES_PER_FILE} more lines not shown)')
        blocks.append(f'--- {path}\n' + '\n'.join(shown))
    return '\n'.join(blocks)


def main(argv):
    """Prints the context of the staged files given on the command line."""
    if len(argv) < 2:
        print('usage: ai_file_context.py <repo root> <staged file>...', file=sys.stderr)
        return 2
    context = build_context(argv[0], argv[1:])
    if context:
        print(context)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
