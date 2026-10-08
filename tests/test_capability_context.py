"""Tests for the capability-context check of audit_static_checks (rule L4-ROB-7).

Each case builds a throwaway plugin on disk with a db/access.php and one PHP file, because
the check reads both: what a capability is granted to, and the context it is checked against.
Run with:  python3 -m unittest discover -s tests
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import audit_static_checks as checks  # noqa: E402

ACCESS = """<?php
$capabilities = [
    'local/demo:teachers' => [
        'contextlevel' => CONTEXT_SYSTEM,
        'archetypes' => ['manager' => CAP_ALLOW, 'editingteacher' => CAP_ALLOW],
    ],
    'local/demo:managers' => [
        'contextlevel' => CONTEXT_SYSTEM,
        'archetypes' => ['manager' => CAP_ALLOW],
    ],
    'local/demo:everyone' => [
        'contextlevel' => CONTEXT_SYSTEM,
        'archetypes' => ['user' => CAP_ALLOW, 'student' => CAP_ALLOW],
    ],
];
"""


def run_check(code):
    """The findings for a plugin whose only production file is `code`."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / 'db').mkdir()
        (root / 'db' / 'access.php').write_text(ACCESS, encoding='utf-8')
        (root / 'page.php').write_text('<?php\n' + code, encoding='utf-8')
        scan_files = [{'rel': 'db/access.php', 'lines': 1}, {'rel': 'page.php', 'lines': 1}]
        return checks.check_capability_context(root, scan_files)


class CapabilityContextTest(unittest.TestCase):
    def test_a_teacher_capability_checked_at_the_system_is_reported(self):
        findings = run_check("require_capability('local/demo:teachers', context_system::instance());")
        self.assertEqual(1, len(findings))
        self.assertEqual('L4-ROB-7', findings[0]['rule_id'])
        self.assertIn('local/demo:teachers', findings[0]['description'])

    def test_the_context_may_come_through_a_variable(self):
        code = "$context = \\context_system::instance();\nif (has_capability('local/demo:teachers', $context)) {}"
        self.assertEqual(1, len(run_check(code)))

    def test_the_page_context_counts_when_it_was_set_to_the_system(self):
        code = ("$PAGE->set_context(context_system::instance());\n"
                "has_capability('local/demo:teachers', $PAGE->context);")
        self.assertEqual(1, len(run_check(code)))

    def test_a_course_context_is_fine(self):
        code = "$context = context_course::instance($courseid);\nrequire_capability('local/demo:teachers', $context);"
        self.assertEqual([], run_check(code))

    def test_a_capability_only_for_managers_is_fine_at_the_system(self):
        self.assertEqual([], run_check("require_capability('local/demo:managers', context_system::instance());"))

    def test_a_capability_that_also_goes_to_every_user_is_fine(self):
        self.assertEqual([], run_check("require_capability('local/demo:everyone', context_system::instance());"))

    def test_looking_in_the_users_courses_counts_as_handled(self):
        code = ("if (has_capability('local/demo:teachers', context_system::instance(), $userid)) { return true; }\n"
                "return !empty(get_user_capability_course('local/demo:teachers', $userid, false, '', '', 1));")
        self.assertEqual([], run_check(code))

    def test_another_plugins_capability_is_not_this_rules_business(self):
        self.assertEqual([], run_check("require_capability('moodle/site:config', context_system::instance());"))

    def test_every_place_is_folded_into_one_finding(self):
        code = ("has_capability('local/demo:teachers', context_system::instance());\n"
                "require_capability('local/demo:teachers', context_system::instance());")
        findings = run_check(code)
        self.assertEqual(1, len(findings))
        self.assertEqual(1, len(findings[0]['extra_locations']))


if __name__ == '__main__':
    unittest.main()
