"""Tests for the commit-scoped filter of static_checks (--staged).

Run with:  python3 -m unittest discover -s tests
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import static_checks as cli  # noqa: E402


def violation(path):
    return {'file': path, 'line': 1}


class StagedFilterTest(unittest.TestCase):
    def test_a_file_level_violation_is_shown_only_when_its_file_is_staged(self):
        results = {'column_lengths': [violation('classes/form/a.php'), violation('classes/form/b.php')]}
        kept = cli.relevant_to_commit(results, ['classes/form/a.php'])
        self.assertEqual(['classes/form/a.php'], [v['file'] for v in kept['column_lengths']])

    def test_schema_checks_stay_quiet_when_the_commit_touches_no_schema_file(self):
        results = {'backup_columns': [violation('db/install.xml')], 'delete_cleanup': [violation('db/install.xml')]}
        kept = cli.relevant_to_commit(results, ['classes/form/a.php'])
        self.assertEqual({'backup_columns': [], 'delete_cleanup': []}, kept)

    def test_schema_checks_speak_up_when_install_xml_backup_or_a_cleanup_class_is_staged(self):
        results = {'backup_columns': [violation('db/install.xml')]}
        for staged in (['db/install.xml'], ['backup/moodle2/backup_x_stepslib.php'], ['lib.php'],
                       ['classes/instance_cleanup.php'], ['classes/observer.php'],
                       ['classes/privacy/provider.php']):
            with self.subTest(staged=staged):
                self.assertEqual(1, len(cli.relevant_to_commit(results, staged)['backup_columns']))

    def test_an_unrelated_class_does_not_count_as_schema(self):
        self.assertFalse(cli.touches_schema(['classes/output/view.php', 'templates/a.mustache']))


if __name__ == '__main__':
    unittest.main()
