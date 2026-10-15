"""Tests for check_backup_support: FEATURE_BACKUP_MOODLE2 must agree with the backup files.

Run with:  python3 -m unittest discover -s tests
"""

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import check_backup_support as checker  # noqa: E402

SWITCH = """<?php
function demo_supports($feature) {
    switch ($feature) {
        case FEATURE_MOD_INTRO:
        case FEATURE_BACKUP_MOODLE2:
        case FEATURE_SHOW_DESCRIPTION:
            return %s;
        default:
            return null;
    }
}
"""

ARRAY = """<?php
function demo_supports(string $feature) {
    $features = [
        FEATURE_MOD_INTRO => true,
        FEATURE_BACKUP_MOODLE2 => %s,
    ];
    return $features[$feature] ?? null;
}
"""

MATCH = """<?php
function demo_supports(string $feature): mixed {
    return match ($feature) {
        FEATURE_MOD_INTRO, FEATURE_BACKUP_MOODLE2 => %s,
        default => null,
    };
}
"""


class BackupSupportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'version.php').write_text("<?php\n$plugin->component = 'mod_demo';\n")

    def tearDown(self):
        self.tmp.cleanup()

    def lib(self, source):
        (self.root / 'lib.php').write_text(source)

    def backup_files(self):
        folder = self.root / 'backup' / 'moodle2'
        folder.mkdir(parents=True, exist_ok=True)
        for name in ('backup_demo_activity_task.class.php', 'restore_demo_activity_task.class.php'):
            (folder / name).write_text('<?php\n')

    def test_reads_every_declaration_shape(self):
        for template in (SWITCH, ARRAY, MATCH):
            for value, expected in (('true', 'true'), ('false', 'false'), ('null', 'false'), ('$x', None)):
                with self.subTest(template=template[:40], value=value):
                    self.assertEqual(expected, checker.declared(template % value))

    def test_assignment_shape_and_absent_feature(self):
        self.assertEqual('true', checker.declared("<?php\n$features[FEATURE_BACKUP_MOODLE2] = true;\n"))
        self.assertEqual('false', checker.declared("<?php\nfunction demo_supports($f) { return null; }\n"))

    def test_commented_out_declaration_is_ignored(self):
        self.assertEqual('false', checker.declared("<?php\n// case FEATURE_BACKUP_MOODLE2: return true;\n"))

    def test_declared_without_files_is_reported(self):
        self.lib(SWITCH % 'true')
        problems = checker.check_plugin(self.root)
        self.assertEqual(1, len(problems))
        self.assertIn('backup_demo_activity_task.class.php', problems[0])

    def test_file_without_class_suffix_does_not_count(self):
        self.lib(SWITCH % 'true')
        folder = self.root / 'backup' / 'moodle2'
        folder.mkdir(parents=True)
        (folder / 'backup_demo_activity_task.php').write_text('<?php\n')
        (folder / 'restore_demo_activity_task.php').write_text('<?php\n')
        self.assertEqual(1, len(checker.check_plugin(self.root)))

    def test_files_without_declaration_are_reported(self):
        self.lib(SWITCH % 'false')
        self.backup_files()
        problems = checker.check_plugin(self.root)
        self.assertEqual(1, len(problems))
        self.assertIn('fica fora', problems[0])

    def test_agreeing_declarations_are_clean(self):
        self.lib(ARRAY % 'false')
        self.assertEqual([], checker.check_plugin(self.root))
        self.lib(MATCH % 'true')
        self.backup_files()
        self.assertEqual([], checker.check_plugin(self.root))

    def test_unreadable_value_is_skipped(self):
        self.lib(SWITCH % 'demo_has_backup()')
        self.assertEqual([], checker.check_plugin(self.root))

    def test_non_activity_is_skipped(self):
        (self.root / 'version.php').write_text("<?php\n$plugin->component = 'local_demo';\n")
        self.lib(SWITCH % 'true')
        self.assertEqual([], checker.check_plugin(self.root))

    def test_main_exit_codes(self):
        self.lib(SWITCH % 'true')
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(1, checker.main(['check_backup_support.py', str(self.root)]))
        self.backup_files()
        self.assertEqual(0, checker.main(['check_backup_support.py', str(self.root)]))


if __name__ == '__main__':
    unittest.main()
