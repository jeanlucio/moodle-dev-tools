"""Tests for check_lang_placeholders: translations must keep English's {$a} placeholders.

Run with:  python3 -m unittest discover -s tests
"""

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import check_lang_placeholders as checker  # noqa: E402


class LangPlaceholdersTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def lang(self, language, body, name='local_demo.php'):
        path = self.root / 'lang' / language / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('<?php\n' + body)
        return path

    def test_matching_placeholders_are_clean(self):
        self.lang('en', "$string['answered'] = 'Answered by {$a} students';\n"
                        "$string['range'] = 'From {$a->start} to {$a->end}';\n")
        self.lang('pt_br', "$string['answered'] = 'Respondida por {$a} estudantes';\n"
                           "$string['range'] = 'De {$a->start} a {$a->end}';\n")
        self.assertEqual([], checker.check_plugin(self.root))

    def test_dropped_placeholder_is_reported_with_its_line(self):
        self.lang('en', "$string['answered'] = 'Answered by {$a} students';\n")
        self.lang('pt_br', "$string['x'] = 'Outro';\n$string['answered'] = 'Respondida por estudantes';\n")
        problems = checker.check_plugin(self.root)
        self.assertEqual(1, len(problems))
        self.assertIn('pt_br/local_demo.php:3:', problems[0])
        self.assertIn('faltam: {$a}', problems[0])

    def test_misspelled_property_is_reported_as_missing_and_extra(self):
        self.lang('en', "$string['hello'] = 'Hello {$a->name}';\n")
        self.lang('pt_br', "$string['hello'] = 'Olá {$a->nome}';\n")
        problems = checker.check_plugin(self.root)
        self.assertEqual(1, len(problems))
        self.assertIn('faltam: {$a->name}', problems[0])
        self.assertIn('sobram: {$a->nome}', problems[0])

    def test_double_quoted_value_with_escaped_dollar_is_read(self):
        self.lang('en', "$string['hello'] = \"Hello {\\$a}\";\n")
        self.lang('pt_br', "$string['hello'] = 'Olá';\n")
        self.assertEqual(1, len(checker.check_plugin(self.root)))

    def test_escaped_quote_inside_value_does_not_break_parsing(self):
        self.lang('en', "$string['a'] = 'It\\'s {$a}';\n$string['b'] = 'Plain';\n")
        self.lang('pt_br', "$string['a'] = 'É {$a}';\n$string['b'] = 'Simples {$a}';\n")
        problems = checker.check_plugin(self.root)
        self.assertEqual(1, len(problems))
        self.assertIn("$string['b']", problems[0])

    def test_key_missing_from_one_side_is_not_this_checks_concern(self):
        self.lang('en', "$string['only_en'] = 'Count {$a}';\n")
        self.lang('pt_br', "$string['only_pt'] = 'Conta';\n")
        self.assertEqual([], checker.check_plugin(self.root))

    def test_concatenated_value_is_skipped(self):
        self.lang('en', "$string['long'] = 'Part {$a} ' . 'more';\n")
        self.lang('pt_br', "$string['long'] = 'Parte';\n")
        self.assertEqual([], checker.check_plugin(self.root))

    def test_every_translation_is_compared(self):
        self.lang('en', "$string['hello'] = 'Hello {$a}';\n")
        self.lang('pt_br', "$string['hello'] = 'Olá {$a}';\n")
        self.lang('es', "$string['hello'] = 'Hola';\n")
        problems = checker.check_plugin(self.root)
        self.assertEqual(1, len(problems))
        self.assertIn('/es/', problems[0])

    def test_plugin_without_lang_is_clean(self):
        self.assertEqual([], checker.check_plugin(self.root))

    def test_main_exit_codes(self):
        self.lang('en', "$string['hello'] = 'Hello {$a}';\n")
        self.lang('pt_br', "$string['hello'] = 'Olá';\n")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(1, checker.main(['check_lang_placeholders.py', str(self.root)]))
        self.lang('pt_br', "$string['hello'] = 'Olá {$a}';\n")
        self.assertEqual(0, checker.main(['check_lang_placeholders.py', str(self.root)]))


if __name__ == '__main__':
    unittest.main()
