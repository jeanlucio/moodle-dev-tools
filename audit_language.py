"""Keeps the audit report in Brazilian Portuguese.

The findings come from a model reading English code, and the scan cache keeps answers written
before the language rule existed, so some findings arrive in English next to a Portuguese summary.
Two layers fix it: every prompt that writes finding text carries LANGUAGE_RULE, and whatever still
reads as English right before the report is translated in one cheap, cached call.

Detection is deliberately simple (function words that exist in only one of the two languages), so
it costs nothing and never calls the model for text that is already Portuguese.
"""

import re

LANGUAGE_RULE = """IDIOMA: escreva TODO o texto que for para o relatório (título, quando aparece,
descrição, impacto, mitigações, cenário ou prova de conceito, correção, padrão para varrer,
motivo) em português do Brasil, mesmo que o código, os comentários e a documentação estejam em
inglês. Mantenha em inglês apenas o que é código: nomes de função, classe, arquivo, constante e
coluna, trechos de código, comandos e expressões regulares, e termos técnicos sem tradução usual.

"""

# Fields of a finding that are prose for the reader. Code-shaped values (file, rule_id, category)
# are left alone.
TRANSLATABLE = ('title', 'exploitable_by', 'description', 'impact', 'mitigations', 'poc',
                'recommendation', 'sweep_pattern')

# Function words used in exactly one of the languages. Words both share ("a", "as", "no", "e",
# "o", "me") are left out on purpose.
ENGLISH = frozenset(
    'the of and to is are that it for with this which by be not from or an but only when if can '
    'does has have was will its their than into then also any each every there where whose whole '
    'instead because while without does do did should would could may might must'.split())
PORTUGUESE = frozenset(
    'de do da dos das em um uma para com não que os se ao são foi como mas pelo pela seu sua já '
    'só entre sem quando também ou mais por na nos nas à às isso esta este esse essa pode podem '
    'deve devem está estão tem têm ser será porque enquanto'.split())
WORD_RE = re.compile(r"[a-záéíóúâêôãõçà]+", re.I)
MIN_WORDS = 3


def ptbr(prompt):
    """A prompt with the language rule in front of it."""
    return LANGUAGE_RULE + prompt


def looks_english(text):
    """Does this prose read as English? Code-only or very short text never does."""
    words = [w.lower() for w in WORD_RE.findall(text or '')]
    if len(words) < MIN_WORDS:
        return False
    english = sum(1 for w in words if w in ENGLISH)
    portuguese = sum(1 for w in words if w in PORTUGUESE)
    if english == 0:
        return False
    return english > portuguese


def english_fields(finding):
    """The prose fields of a finding that read as English."""
    return [name for name in TRANSLATABLE
            if isinstance(finding.get(name), str) and looks_english(finding[name])]


def needs_translation(finding):
    return bool(english_fields(finding))


def payload_for(findings):
    """What the translation call receives: the English fields of each finding, by position."""
    return [{'id': index, **{name: finding[name] for name in english_fields(finding)}}
            for index, finding in enumerate(findings) if needs_translation(finding)]


def apply_translations(findings, translated):
    """Write the translated fields back, only the ones that were asked for and came back as text."""
    count = 0
    for item in translated if isinstance(translated, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get('id'), int):
            continue
        if not 0 <= item['id'] < len(findings):
            continue
        finding = findings[item['id']]
        asked = set(english_fields(finding))
        for name in TRANSLATABLE:
            value = item.get(name)
            if name in asked and isinstance(value, str) and value.strip():
                finding[name] = value.strip()
                count += 1
    return count
