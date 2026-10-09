#!/usr/bin/env python3
"""Blocks old-style JavaScript on the lines a commit adds or changes in amd/src.

Moodle's own .eslintrc does not enable no-var, prefer-const, prefer-arrow-callback,
prefer-template, eqeqeq or promise/prefer-await-to-then, so the regular ESLint gate lets
old-style code through. Turning those rules on for whole files would block every edit of a
legacy module, so this check runs them but keeps only the violations that sit on lines the
commit adds. Old code stays untouched until someone chooses to modernize it.

Usage: check_modern_js.py <eslint-binary> <file> [<file> ...]
Run from the repository root with the files staged. The staged version of each file is
linted (not the working tree), so line numbers match the staged diff.
"""

import json
import re
import subprocess
import sys

RULES = {
    'no-var': 'error',
    'prefer-const': 'error',
    'prefer-arrow-callback': 'error',
    'prefer-template': 'error',
    'eqeqeq': 'error',
    'promise/prefer-await-to-then': 'error',
}

HINTS = {
    'no-var': 'use const (or let if it is reassigned) instead of var',
    'prefer-const': 'the variable is never reassigned: use const',
    'prefer-arrow-callback': 'use an arrow function for the callback',
    'prefer-template': 'use a template literal instead of string concatenation',
    'eqeqeq': 'use === / !== instead of == / !=',
    'promise/prefer-await-to-then': 'use async/await instead of .then()',
}

HUNK = re.compile(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@')


def added_lines(diff_text: str) -> set[int]:
    """Returns the line numbers a zero-context diff adds or changes in the new file.

    @param str diff_text output of `git diff --cached -U0 -- <file>`
    @return set[int] 1-based line numbers in the staged file
    """
    lines: set[int] = set()
    for row in diff_text.splitlines():
        match = HUNK.match(row)
        if not match:
            continue
        start = int(match.group(1))
        count = 1 if match.group(2) is None else int(match.group(2))
        lines.update(range(start, start + count))

    return lines


def violations_on_lines(messages: list[dict], lines: set[int]) -> list[dict]:
    """Keeps the rule violations that sit on one of the given lines.

    @param list[dict] messages ESLint JSON messages for one file
    @param set[int] lines line numbers to keep
    @return list[dict] the matching messages that belong to the modern-JS rules
    """
    return [m for m in messages if m.get('ruleId') in RULES and m.get('line') in lines]


def check_file(eslint: str, path: str) -> list[str]:
    """Lints the staged content of one file and reports violations on its added lines.

    @param str eslint path to the ESLint binary
    @param str path file path relative to the repository root
    @return list[str] one printable line per violation; empty if clean or not checkable
    """
    diff = subprocess.run(
        ['git', 'diff', '--cached', '-U0', '--', path], capture_output=True, text=True, check=False
    ).stdout
    lines = added_lines(diff)
    if not lines:
        return []

    staged = subprocess.run(['git', 'show', f':{path}'], capture_output=True, text=True, check=False)
    if staged.returncode != 0:
        return []

    result = subprocess.run(
        [eslint, '--stdin', '--stdin-filename', path, '-f', 'json', '--rule', json.dumps(RULES)],
        input=staged.stdout, capture_output=True, text=True, check=False,
    )
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError:
        # A file ESLint cannot parse is the regular ESLint gate's problem, not this one's.
        return []

    found: list[str] = []
    for entry in report:
        for message in violations_on_lines(entry.get('messages', []), lines):
            rule = message['ruleId']
            found.append(f"{path}:{message['line']}:{message['column']}  {rule}  {HINTS[rule]}")

    return found


def main(argv: list[str]) -> int:
    """Checks every file given on the command line.

    @param list[str] argv eslint binary followed by the files to check
    @return int 1 if any violation was found, 0 otherwise
    """
    if len(argv) < 3:
        print(__doc__)
        return 2

    problems: list[str] = []
    for path in argv[2:]:
        problems.extend(check_file(argv[1], path))

    for problem in problems:
        print(problem)

    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
