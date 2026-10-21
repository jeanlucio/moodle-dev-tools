#!/usr/bin/env python3
"""Checks the custom completion rules of a mod_form.php.

Two defects, both invisible in the normal activity form and in every other local check, hit seven of
the eight activities of this ecosystem that define add_completion_rules() (21/10/2026):

1. The rule element is not named with $this->get_suffix(). The core renders the same form on the
   "Default activity completion" page with the suffix "_<modname>" and removes any custom rule whose
   name lacks it (completion/classes/edit_base_form.php), with only a DEBUG_DEVELOPER message, so
   saving that page erases the rule from the defaults of every new activity. With the empty suffix of
   the normal form nothing looks wrong, so no test notices. BLOCKS the commit.

2. An unticked rule is not stored as 0. A browser sends neither an unticked checkbox nor a field
   disabled by disabledIf(); the form falls back to the value set_data() loaded (or leaves the key
   out), and the update keeps the old value: the teacher can never remove the rule. Only a WARNING:
   the fix may legitimately live elsewhere (an advcheckbox, or lib.php zeroing the value in
   add/update_instance), which a static check can only guess at.
"""

import re
import sys
from pathlib import Path

# A literal, a comment or a heredoc must not confuse the brace counter or the keyword search.
NOISE = re.compile(
    r"""//[^\n]*|\#(?!\[)[^\n]*|/\*.*?\*/|'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*\"""",
    re.DOTALL,
)


def strip_noise(source: str) -> str:
    """Blanks comments and string literals, keeping the line structure.

    @param str $source the PHP source
    @return str the source with comments and literals replaced by same-length blanks
    """
    return NOISE.sub(lambda match: re.sub(r'[^\n]', ' ', match.group(0)), source)


def method_body(source: str, name: str) -> str | None:
    """The text between the braces of a method, found in comment-free source.

    @param str $source the PHP source, noise already removed
    @param str $name the method name
    @return str|None the body, or None when the method is not defined
    """
    declaration = re.search(r'function\s+' + re.escape(name) + r'\s*\(', source)
    if declaration is None:
        return None
    start = source.find('{', declaration.end())
    if start < 0:
        return None
    depth = 0
    for position in range(start, len(source)):
        if source[position] == '{':
            depth += 1
        elif source[position] == '}':
            depth -= 1
            if depth == 0:
                return source[start + 1:position]
    return None


def line_of(source: str, needle: str) -> int:
    """The 1-based line of the first occurrence of a text, or 1.

    @param str $source the original source
    @param str $needle the text to find
    @return int the line number
    """
    position = source.find(needle)
    return source.count('\n', 0, position) + 1 if position >= 0 else 1


def lib_zeroes_the_rule(form: Path) -> bool:
    """Whether the sibling lib.php already stores an unticked rule as 0.

    Looks for the pattern used by the older activities: `if (empty($data->completion...)) {`.

    @param Path $form the mod_form.php being checked
    @return bool true when lib.php appears to handle the unticked case
    """
    lib = form.parent / 'lib.php'
    if not lib.is_file():
        return False
    text = strip_noise(lib.read_text(encoding='utf-8', errors='replace'))
    return re.search(r'empty\(\s*\$\w+->completion\w*\s*\)', text) is not None


def check_mod_form(form: Path) -> tuple[list[str], list[str]]:
    """Checks one mod_form.php.

    @param Path $form the file
    @return tuple[list[str], list[str]] (errors that block, warnings)
    """
    original = form.read_text(encoding='utf-8', errors='replace')
    source = strip_noise(original)

    rules = method_body(source, 'add_completion_rules')
    if rules is None:
        return [], []

    errors = []
    if 'get_suffix' not in rules:
        errors.append(
            f"{form}:{line_of(original, 'function add_completion_rules')}: add_completion_rules() não usa "
            f"$this->get_suffix() nos nomes dos elementos; a página \"Conclusão padrão da atividade\" "
            f"mostra este formulário com o sufixo \"_<modname>\" e apaga a regra cujo nome não o tem"
        )
    enabled = method_body(source, 'completion_rule_enabled')
    if enabled is not None and 'get_suffix' not in enabled:
        errors.append(
            f"{form}:{line_of(original, 'function completion_rule_enabled')}: completion_rule_enabled() "
            f"não usa $this->get_suffix() nas chaves de $data"
        )

    warnings = []
    # Literals are blanked in `source`, so look for the element type in the original text of the method.
    original_rules = method_body(original, 'add_completion_rules') or ''
    only_advcheckbox = (
        re.search(r"['\"]checkbox['\"]", original_rules) is None and 'disabledIf' not in original_rules
    )
    if method_body(source, 'data_postprocessing') is None and not only_advcheckbox \
            and not lib_zeroes_the_rule(form):
        warnings.append(
            f"{form}:{line_of(original, 'function add_completion_rules')}: sem data_postprocessing(): o "
            f"navegador não envia checkbox desmarcada nem campo desativado, então o professor não "
            f"consegue desligar a regra depois de configurada (modelo: mod/forum/mod_form.php). Ignore se "
            f"o lib.php já grava 0 nesse caso"
        )
    return errors, warnings


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print('uso: check_completion_form.py <mod_form.php> [<mod_form.php> ...]', file=sys.stderr)
        return 1

    all_errors = []
    for raw in argv[1:]:
        form = Path(raw).resolve()
        if not form.is_file():
            continue
        errors, warnings = check_mod_form(form)
        all_errors.extend(errors)
        for warning in warnings:
            print(f'aviso: {warning}')

    for error in all_errors:
        print(error)
    if all_errors:
        print(f'\n{len(all_errors)} problema(s) no formulário de conclusão.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
