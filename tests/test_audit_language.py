"""Tests for the report language: detecting English prose and translating it in one cached call.

Run with:  python3 -m unittest discover -s tests
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import audit_language as language  # noqa: E402
import security_audit as audit  # noqa: E402

ENGLISH = [
    'IP filter misses the IPv4-compatible IPv6 range (::/96)',
    "AI-generated choice text isn't cut to char(255), so the whole chapter is lost",
    'Log and content tables have no index on the filtered columns',
    'The delegated transaction rolls back and the whole generated chapter is discarded.',
    'Add INDEX questid_userid (questid, userid) to quest_log, and a foreign key on blockinstanceid.',
    "Find AI or external text written to a char column with no cut: grep -nE '->text' classes/ai",
]
PORTUGUESE = [
    'Coluna do install.xml que o backup do curso não salva',
    'Quest de nível aceita alvo acima do nível máximo e nunca pode ser concluída',
    'O professor perde o formulário preenchido quando a URL passa de 255 caracteres.',
    'Rode `moodle-static-checks <tipo/nome> --check backup_columns`: ele já varre todas as tabelas.',
    "grep -rn \"FILTER_FLAG_NO_PRIV_RANGE\\|is_public_ip\" --include=*.php . — para cada lista de IP, "
    "confira se cobre '::/96'",
]


def finding(**fields):
    return {'title': 'Título', 'finding_type': 'code_quality', 'severity': 'low', 'category': 'x',
            'rule_id': 'R', 'file': 'a.php', 'line': 1, **fields}


class DetectionTest(unittest.TestCase):
    def test_english_prose_is_recognised(self):
        for text in ENGLISH:
            with self.subTest(text=text[:40]):
                self.assertTrue(language.looks_english(text))

    def test_portuguese_prose_is_not_flagged(self):
        for text in PORTUGUESE:
            with self.subTest(text=text[:40]):
                self.assertFalse(language.looks_english(text))

    def test_code_and_very_short_text_never_trigger_a_translation(self):
        for text in ('', None, 'classes/ai/generator.php:1232', 'core_text::substr', 'low',
                     "$choice->text = clean_param($x, PARAM_TEXT);", 'PARAM_URL'):
            with self.subTest(text=text):
                self.assertFalse(language.looks_english(text))

    def test_only_the_prose_fields_are_considered(self):
        english = finding(file='the/of/and.php', rule_id='L3-SSRF-1', category='the_and_of')
        self.assertFalse(language.needs_translation(english))
        self.assertEqual(['description'], language.english_fields(
            finding(description='The query runs once for each row of the table.')))


class PayloadTest(unittest.TestCase):
    def test_the_payload_carries_only_the_english_fields_and_their_position(self):
        findings = [finding(title=PORTUGUESE[0]),
                    finding(title=ENGLISH[0], description=PORTUGUESE[2], impact=ENGLISH[3])]
        payload = language.payload_for(findings)
        self.assertEqual([{'id': 1, 'title': ENGLISH[0], 'impact': ENGLISH[3]}], payload)

    def test_translations_are_written_back_to_the_fields_that_were_asked(self):
        findings = [finding(title=ENGLISH[0], description=PORTUGUESE[2])]
        changed = language.apply_translations(findings, [
            {'id': 0, 'title': 'O filtro de IP não cobre a faixa IPv6 compatível com IPv4',
             'description': 'texto que não foi pedido', 'file': 'evil.php'}])
        self.assertEqual(1, changed)
        self.assertEqual('O filtro de IP não cobre a faixa IPv6 compatível com IPv4', findings[0]['title'])
        self.assertEqual(PORTUGUESE[2], findings[0]['description'])
        self.assertEqual('a.php', findings[0]['file'])

    def test_a_malformed_answer_changes_nothing(self):
        findings = [finding(title=ENGLISH[0])]
        for answer in (None, 'x', {}, [None], [{'id': 'a'}], [{'id': 7, 'title': 'x'}],
                       [{'id': 0, 'title': ''}], [{'id': 0, 'title': 3}]):
            with self.subTest(answer=answer):
                self.assertEqual(0, language.apply_translations(findings, answer))
                self.assertEqual(ENGLISH[0], findings[0]['title'])

    def test_the_prompt_prefix_asks_for_portuguese(self):
        prefix = language.ptbr('ORIGINAL')
        self.assertTrue(prefix.endswith('ORIGINAL'))
        self.assertIn('português do Brasil', prefix)


class TranslatePassTest(unittest.TestCase):
    def run_pass(self, findings, answer=None, error=None):
        calls = []

        def fake(prompt, *args, **kwargs):
            calls.append((prompt, kwargs))
            if error:
                raise error
            return answer

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(audit, 'call_claude', fake), \
                mock.patch.object(audit, 'CACHE_DIR', Path(tmp)):
            changed = audit.translate_findings(findings, Path(tmp), 'block_x', 'm', 'f', True)
        return changed, calls

    def test_nothing_is_asked_when_everything_is_already_portuguese(self):
        changed, calls = self.run_pass([finding(title=PORTUGUESE[0], description=PORTUGUESE[2])])
        self.assertEqual((0, []), (changed, calls))

    def test_an_english_finding_is_translated_in_one_call_without_tools(self):
        findings = [finding(title=ENGLISH[0]), finding(title=PORTUGUESE[0])]
        changed, calls = self.run_pass(
            findings, '[{"id": 0, "title": "O filtro de IP deixa passar a faixa ::/96"}]')
        self.assertEqual(1, changed)
        self.assertEqual('O filtro de IP deixa passar a faixa ::/96', findings[0]['title'])
        self.assertEqual(1, len(calls))
        self.assertFalse(calls[0][1]['allow_tools'])

    def test_the_translation_is_cached_so_a_second_run_costs_nothing(self):
        findings = [finding(title=ENGLISH[0])]
        answer = '[{"id": 0, "title": "O filtro de IP deixa passar a faixa ::/96"}]'
        calls = []

        def fake(prompt, *args, **kwargs):
            calls.append(prompt)
            return answer

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(audit, 'call_claude', fake), \
                mock.patch.object(audit, 'CACHE_DIR', Path(tmp)):
            audit.translate_findings(findings, Path(tmp), 'block_x', 'm', 'f', True)
            again = [finding(title=ENGLISH[0])]
            audit.translate_findings(again, Path(tmp), 'block_x', 'm', 'f', True)
        self.assertEqual(1, len(calls))
        self.assertEqual('O filtro de IP deixa passar a faixa ::/96', again[0]['title'])

    def test_a_failed_call_leaves_the_text_as_it_was(self):
        findings = [finding(title=ENGLISH[0])]
        changed, _ = self.run_pass(findings, error=RuntimeError('rede'))
        self.assertEqual(0, changed)
        self.assertEqual(ENGLISH[0], findings[0]['title'])

    def test_many_findings_are_split_into_batches(self):
        findings = [finding(title=ENGLISH[0] + f' {n}') for n in range(audit.TRANSLATE_BATCH + 1)]
        _, calls = self.run_pass(findings, '[]')
        self.assertEqual(2, len(calls))


class ReportLanguageWiringTest(unittest.TestCase):
    def test_every_prompt_that_writes_finding_text_goes_out_with_the_language_rule(self):
        source = Path(audit.__file__).read_text(encoding='utf-8')
        for call in ('TRIAGE_PROMPT', 'DEDUP_PROMPT'):
            self.assertIn(f'audit_language.ptbr({call})', source)
        self.assertGreaterEqual(source.count('audit_language.ptbr(prompt)'), 2)  # scan and verify

    def test_the_verification_cache_was_invalidated_when_the_language_changed(self):
        self.assertIn('ptbr', audit.VERIFY_REVISION)


if __name__ == '__main__':
    unittest.main()
