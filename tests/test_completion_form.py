"""Tests for check_completion_form: suffix and unticked-rule handling in a mod_form.php.

Run with:  python3 -m unittest discover -s tests
"""

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import check_completion_form as checker  # noqa: E402

FORM = """<?php
class mod_demo_mod_form extends moodleform_mod {
    public function add_completion_rules(): array {
        $mform = $this->_form;
%(suffixline)s
        $mform->addElement('%(type)s', 'completionsubmit' . $suffix, '', 'label');
        return ['completionsubmit' . $suffix];
    }

    public function completion_rule_enabled($data): bool {
        %(enabledline)s
        return !empty($data['completionsubmit' . $suffix]);
    }
%(post)s
}
"""

POST = """
    public function data_postprocessing($data): void {
        parent::data_postprocessing($data);
        if (empty($data->completionsubmit)) {
            $data->completionsubmit = 0;
        }
    }
"""


class CompletionFormTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def form(self, suffix=True, enabledsuffix=True, post=True, type_='checkbox'):
        text = FORM % {
            'suffixline': '        $suffix = $this->get_suffix();' if suffix else '        $suffix = "";',
            'enabledline': '$suffix = $this->get_suffix();' if enabledsuffix else '$suffix = "";',
            'type': type_,
            'post': POST if post else '',
        }
        path = self.dir / 'mod_form.php'
        path.write_text(text, encoding='utf-8')
        return path

    def test_a_correct_form_is_clean(self):
        errors, warnings = checker.check_mod_form(self.form())
        self.assertEqual([], errors)
        self.assertEqual([], warnings)

    def test_missing_suffix_in_the_rule_elements_blocks(self):
        errors, _ = checker.check_mod_form(self.form(suffix=False))
        self.assertEqual(1, len(errors))
        self.assertIn('add_completion_rules()', errors[0])

    def test_missing_suffix_in_completion_rule_enabled_blocks(self):
        errors, _ = checker.check_mod_form(self.form(enabledsuffix=False))
        self.assertEqual(1, len(errors))
        self.assertIn('completion_rule_enabled()', errors[0])

    def test_a_form_without_completion_rules_is_ignored(self):
        path = self.dir / 'mod_form.php'
        path.write_text('<?php\nclass mod_demo_mod_form extends moodleform_mod {}\n', encoding='utf-8')
        self.assertEqual(([], []), checker.check_mod_form(path))

    def test_a_plain_checkbox_without_data_postprocessing_warns_but_does_not_block(self):
        errors, warnings = checker.check_mod_form(self.form(post=False))
        self.assertEqual([], errors)
        self.assertEqual(1, len(warnings))
        self.assertIn('data_postprocessing', warnings[0])

    def test_an_advcheckbox_needs_no_data_postprocessing(self):
        errors, warnings = checker.check_mod_form(self.form(post=False, type_='advcheckbox'))
        self.assertEqual(([], []), (errors, warnings))

    def test_lib_php_that_zeroes_the_rule_silences_the_warning(self):
        path = self.form(post=False)
        (self.dir / 'lib.php').write_text(
            "<?php\nfunction demo_update_instance($data) {\n"
            "    if (empty($data->completionsubmitenabled)) {\n        $data->completionsubmit = 0;\n    }\n}\n",
            encoding='utf-8',
        )
        _, warnings = checker.check_mod_form(path)
        self.assertEqual([], warnings)

    def test_a_suffix_mentioned_only_in_a_comment_does_not_count(self):
        path = self.dir / 'mod_form.php'
        path.write_text(
            "<?php\nclass f extends moodleform_mod {\n"
            "    public function add_completion_rules(): array {\n"
            "        // TODO use $this->get_suffix() here.\n"
            "        $this->_form->addElement('advcheckbox', 'completionsubmit', '', 'x');\n"
            "        return ['completionsubmit'];\n    }\n}\n",
            encoding='utf-8',
        )
        errors, _ = checker.check_mod_form(path)
        self.assertEqual(1, len(errors))

    def test_main_exit_codes(self):
        bad = self.form(suffix=False)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(1, checker.main(['x', str(bad)]))
        good = self.form()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(0, checker.main(['x', str(good)]))


if __name__ == '__main__':
    unittest.main()
