"""Tests for check_db_strings: the lang strings core builds from db/caches.php and db/messages.php.

Each case builds a throwaway plugin on disk. Run with:  python3 -m unittest discover -s tests
"""

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import check_db_strings as checker  # noqa: E402

CACHES = """<?php
defined('MOODLE_INTERNAL') || die();

$definitions = [
    'ranking' => [
        'mode' => cache_store::MODE_APPLICATION,
        'invalidationevents' => ['changesinranking'],
    ],
    // 'commented' => [ is not a definition.
    'scores' => [
        'mode' => cache_store::MODE_SESSION,
    ],
];
"""

MESSAGES = """<?php
defined('MOODLE_INTERNAL') || die();

$messageproviders = array(
    'graded' => array(
        'defaults' => array(
            'popup' => MESSAGE_PERMITTED + MESSAGE_DEFAULT_ENABLED,
        ),
    ),
    SOME_CONSTANT => [],
);
"""


class DbStringsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def plugin(self, component, langname, files, strings):
        """Writes version.php, the given files and an en lang file holding `strings`."""
        (self.root / 'version.php').write_text(f"<?php\n$plugin->component = '{component}';\n")
        for relative, content in files.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        lang = self.root / 'lang' / 'en' / f'{langname}.php'
        lang.parent.mkdir(parents=True, exist_ok=True)
        lang.write_text('<?php\n' + ''.join(f"$string['{key}'] = 'x';\n" for key in strings))
        return self.root

    def test_reads_only_first_level_keys(self):
        keys = [key for key, _ in checker.top_level_keys(CACHES, 'definitions')]
        self.assertEqual(['ranking', 'scores'], keys)

    def test_reads_legacy_array_syntax_and_skips_non_literal_keys(self):
        keys = [key for key, _ in checker.top_level_keys(MESSAGES, 'messageproviders')]
        self.assertEqual(['graded'], keys)

    def test_line_number_points_at_the_key(self):
        lines = dict(checker.top_level_keys(CACHES, 'definitions'))
        self.assertEqual(5, lines['ranking'])
        self.assertEqual(10, lines['scores'])

    def test_complete_plugin_is_clean(self):
        root = self.plugin('local_demo', 'local_demo', {'db/caches.php': CACHES},
                           ['pluginname', 'cachedef_ranking', 'cachedef_scores'])
        self.assertEqual([], checker.check_plugin(root))

    def test_missing_cache_string_is_reported(self):
        root = self.plugin('local_demo', 'local_demo', {'db/caches.php': CACHES},
                           ['pluginname', 'cachedef_ranking'])
        problems = checker.check_plugin(root)
        self.assertEqual(1, len(problems))
        self.assertIn("caches.php:10", problems[0])
        self.assertIn("cachedef_scores", problems[0])

    def test_message_provider_needs_its_string_but_nested_keys_do_not(self):
        root = self.plugin('mod_demo', 'demo', {'db/messages.php': MESSAGES}, ['pluginname'])
        problems = checker.check_plugin(root)
        self.assertEqual(1, len(problems))
        self.assertIn("messageprovider:graded", problems[0])

    def test_mod_component_uses_lang_file_without_prefix(self):
        root = self.plugin('mod_demo', 'demo', {'db/messages.php': MESSAGES},
                           ['pluginname', 'messageprovider:graded'])
        self.assertEqual([], checker.check_plugin(root))

    def test_plugin_without_definition_files_is_clean(self):
        root = self.plugin('local_demo', 'local_demo', {}, ['pluginname'])
        self.assertEqual([], checker.check_plugin(root))

    def test_missing_lang_file_is_reported_once(self):
        root = self.plugin('local_demo', 'local_demo', {'db/caches.php': CACHES}, [])
        (root / 'lang' / 'en' / 'local_demo.php').unlink()
        problems = checker.check_plugin(root)
        self.assertEqual(1, len(problems))
        self.assertIn('lang/en/<component>.php', problems[0])

    def quiet_main(self, argv):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return checker.main(argv)

    def test_main_exit_codes(self):
        root = self.plugin('local_demo', 'local_demo', {'db/caches.php': CACHES}, ['pluginname'])
        self.assertEqual(1, self.quiet_main(['check_db_strings.py', str(root)]))
        self.plugin('local_demo', 'local_demo', {'db/caches.php': CACHES},
                    ['cachedef_ranking', 'cachedef_scores'])
        self.assertEqual(0, self.quiet_main(['check_db_strings.py', str(root)]))


if __name__ == '__main__':
    unittest.main()
