"""Tests for check_modern_js: which diff lines count and which violations are kept.

Run with:  python3 -m unittest discover -s tests
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import check_modern_js as check  # noqa: E402


class AddedLinesTest(unittest.TestCase):
    def test_a_single_line_hunk_without_a_count_is_one_line(self):
        self.assertEqual({7}, check.added_lines('@@ -6 +7 @@\n-old\n+new\n'))

    def test_a_hunk_with_a_count_covers_every_added_line(self):
        self.assertEqual({10, 11, 12}, check.added_lines('@@ -9,0 +10,3 @@\n+a\n+b\n+c\n'))

    def test_a_pure_deletion_adds_nothing(self):
        self.assertEqual(set(), check.added_lines('@@ -4,2 +3,0 @@\n-a\n-b\n'))

    def test_several_hunks_are_merged(self):
        diff = '@@ -1 +1 @@\n-a\n+b\n@@ -20,0 +21,2 @@\n+c\n+d\n'
        self.assertEqual({1, 21, 22}, check.added_lines(diff))

    def test_a_new_file_covers_all_its_lines(self):
        self.assertEqual({1, 2, 3}, check.added_lines('@@ -0,0 +1,3 @@\n+a\n+b\n+c\n'))


class ViolationsOnLinesTest(unittest.TestCase):
    def test_a_violation_on_an_added_line_is_kept(self):
        messages = [{'ruleId': 'no-var', 'line': 5, 'column': 1}]
        self.assertEqual(messages, check.violations_on_lines(messages, {5}))

    def test_a_violation_on_an_untouched_line_is_dropped(self):
        messages = [{'ruleId': 'no-var', 'line': 5, 'column': 1}]
        self.assertEqual([], check.violations_on_lines(messages, {6}))

    def test_a_rule_outside_the_modern_js_set_is_dropped(self):
        messages = [{'ruleId': 'no-unused-vars', 'line': 5, 'column': 1}]
        self.assertEqual([], check.violations_on_lines(messages, {5}))

    def test_a_parse_error_without_a_rule_id_is_dropped(self):
        messages = [{'ruleId': None, 'line': 5, 'column': 1}]
        self.assertEqual([], check.violations_on_lines(messages, {5}))


if __name__ == '__main__':
    unittest.main()
