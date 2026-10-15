"""Tests for the moodle_exception part of check_get_string.

A moodle_exception's error code is a lang string key; when the module is omitted, 'moodle' or
'core', the constructor looks it up in core's lang/en/error.php, not in moodle.php.
Run with:  python3 -m unittest discover -s tests
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import check_get_string as checker  # noqa: E402


class GetStringExceptionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.moodle = base / 'public'
        core = self.moodle / 'lang' / 'en'
        core.mkdir(parents=True)
        (core / 'error.php').write_text("<?php\n$string['invalidrecord'] = 'x';\n")
        (core / 'moodle.php').write_text("<?php\n$string['savechanges'] = 'x';\n")
        self.plugin = base / 'plugin'
        (self.plugin / 'lang' / 'en').mkdir(parents=True)
        (self.plugin / 'version.php').write_text("<?php\n$plugin->component = 'mod_demo';\n")
        (self.plugin / 'lang' / 'en' / 'demo.php').write_text("<?php\n$string['nopermission'] = 'x';\n")

    def tearDown(self):
        self.tmp.cleanup()

    def check(self, source):
        php = self.plugin / 'view.php'
        php.write_text('<?php\n' + source)
        return checker.check_file(php, 'mod_demo', self.plugin, self.moodle)

    def test_existing_keys_are_clean(self):
        self.assertEqual([], self.check(
            "throw new moodle_exception('nopermission', 'mod_demo');\n"
            "throw new \\moodle_exception('invalidrecord');\n"
            "throw new \\core\\exception\\moodle_exception('invalidrecord', 'core', $url);\n"
        ))

    def test_missing_plugin_key_is_reported(self):
        problems = self.check("throw new moodle_exception('nosuchkey', 'mod_demo');\n")
        self.assertEqual(1, len(problems))
        self.assertIn("moodle_exception('nosuchkey', 'mod_demo')", problems[0])

    def test_omitted_module_is_read_from_error_not_moodle(self):
        problems = self.check("throw new moodle_exception('savechanges');\n")
        self.assertEqual(1, len(problems))
        self.assertIn('error.php', problems[0])

    def test_explicit_error_module_resolves_to_error(self):
        problems = self.check("throw new moodle_exception('invalidparameter', 'error');\n")
        self.assertEqual(1, len(problems))

    def test_non_literal_error_code_is_skipped(self):
        self.assertEqual([], self.check("throw new moodle_exception($code, 'mod_demo');\n"))

    def test_get_string_still_reads_moodle_php(self):
        self.assertEqual([], self.check("echo get_string('savechanges');\n"))
        self.assertEqual(1, len(self.check("echo get_string('invalidrecord');\n")))

    def test_problems_follow_source_order(self):
        problems = self.check(
            "throw new moodle_exception('nosuchkey', 'mod_demo');\n"
            "echo get_string('nosuchstring', 'mod_demo');\n"
        )
        self.assertEqual(2, len(problems))
        self.assertIn('moodle_exception', problems[0])
        self.assertIn('get_string', problems[1])


if __name__ == '__main__':
    unittest.main()
