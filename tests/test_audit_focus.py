"""Tests for the audit focus (security, quality, all): prompts, filtering, grades and the run history.

Run with:  python3 -m unittest discover -s tests
"""

import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import audit_focus as focus  # noqa: E402
import audit_scope  # noqa: E402
import security_audit as audit  # noqa: E402
from test_audit_scope import ReportTest  # noqa: E402


def finding(ftype, severity='low', title='t'):
    return {'title': title, 'finding_type': ftype, 'severity': severity, 'category': 'x',
            'rule_id': 'R', 'file': 'a.php', 'line': 1, 'description': 'd',
            'recommendation': 'r', 'sweep_pattern': ''}


class PromptTest(unittest.TestCase):
    def test_all_keeps_the_original_prompt(self):
        self.assertEqual('ORIGINAL', focus.scan_prompt('all', 'ORIGINAL'))

    def test_security_asks_only_for_security_and_leaves_layer_4_out(self):
        prompt = focus.scan_prompt('security', 'ORIGINAL')
        self.assertIn('"finding_type": "security"', prompt)
        self.assertNotIn('L4-HYG-1', prompt)
        self.assertNotIn('code_quality', prompt)

    def test_quality_asks_for_layer_4_and_still_reports_a_vulnerability_it_sees(self):
        prompt = focus.scan_prompt('quality', 'ORIGINAL')
        self.assertIn('L4-ROB-1', prompt)
        self.assertIn('"finding_type": "security"', prompt)
        self.assertIn('Ela não pode se perder', prompt)


class FilterTest(unittest.TestCase):
    MIXED = [finding('security'), finding('code_quality'), finding('compliance')]

    def test_security_verifies_only_security_candidates(self):
        kept = focus.keep_for_focus(self.MIXED, 'security')
        self.assertEqual(['security'], [f['finding_type'] for f in kept])

    def test_quality_and_all_keep_everything_so_a_vulnerability_is_never_dropped(self):
        for name in ('quality', 'all'):
            with self.subTest(focus=name):
                self.assertEqual(3, len(focus.keep_for_focus(self.MIXED, name)))

    def test_a_finding_without_a_type_counts_as_security(self):
        self.assertEqual(1, len(focus.keep_for_focus([{'title': 'old'}], 'security')))


class GradeTest(unittest.TestCase):
    def test_a_quality_low_does_not_touch_the_security_grade(self):
        confirmed = [finding('code_quality', 'low')]
        self.assertEqual(('A', 'A+'), (audit.focus_grades(confirmed, 'all')[0],
                                       audit.focus_grades(confirmed, 'all')[2]))
        self.assertEqual('A+', audit.focus_grades(confirmed, 'security')[0])

    def test_security_focus_grades_only_the_security_findings(self):
        grade, _, security = audit.focus_grades([finding('security', 'high')], 'security')
        self.assertEqual(('D', 'D'), (grade, security))

    def test_quality_focus_ignores_security_in_its_own_grade_but_reports_it(self):
        grade, _, security = audit.focus_grades(
            [finding('code_quality', 'low'), finding('security', 'high')], 'quality')
        self.assertEqual('A', grade)
        self.assertEqual('D', security)

    def test_quality_focus_has_no_security_grade_when_nothing_security_was_seen(self):
        self.assertIsNone(audit.focus_grades([finding('code_quality')], 'quality')[2])


class RunHistoryTest(unittest.TestCase):
    def record(self, root, highest, only=None, day=1):
        focus.record_run(root, 'security', only, highest, today=date(2026, 10, day))

    def test_two_complete_runs_without_anything_above_low_close_a_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.record(root, 'low', ['A'])
            self.assertFalse(focus.is_closed(root, 'A'))
            self.record(root, 'none', ['A'], day=2)
            self.assertTrue(focus.is_closed(root, 'A'))

    def test_a_medium_reopens_the_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for day, highest in enumerate(['low', 'low', 'medium'], 1):
                self.record(root, highest, ['A'], day)
            self.assertFalse(focus.is_closed(root, 'A'))

    def test_a_run_over_several_groups_updates_each_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for day in (1, 2):
                self.record(root, 'low', ['A', 'B'], day)
            self.assertTrue(focus.is_closed(root, 'A') and focus.is_closed(root, 'B'))
            self.assertFalse(focus.is_closed(root, 'C'))

    def test_only_the_security_focus_keeps_a_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            focus.record_run(Path(tmp), 'quality', None, 'none')
            self.assertEqual({}, focus.load_state(Path(tmp)))

    def test_the_whole_plugin_is_the_scope_when_no_group_is_named(self):
        self.assertEqual(['*'], focus.scope_key(None))

    def test_highest_severity(self):
        order = audit.SEVERITY_ORDER
        self.assertEqual('none', focus.highest_severity([], order))
        self.assertEqual('medium', focus.highest_severity(
            [finding('security', 'low'), finding('security', 'medium')], order))


class LastTagTest(unittest.TestCase):
    def git(self, root, *args):
        subprocess.run(['git', '-C', str(root), '-c', 'user.name=t', '-c', 'user.email=t@t',
                        '-c', 'core.hooksPath=/dev/null', *args], check=True, capture_output=True)

    def test_the_newest_reachable_tag_is_found_and_no_tag_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.git(root, 'init', '-q')
            (root / 'a.php').write_text('1')
            self.git(root, 'add', '.')
            self.git(root, 'commit', '-q', '-m', 'one')
            self.assertIsNone(audit_scope.last_tag(root))
            self.git(root, 'tag', 'v1.0.0')
            (root / 'a.php').write_text('2')
            self.git(root, 'commit', '-q', '-am', 'two')
            self.git(root, 'tag', 'v1.1.0')
            self.assertEqual('v1.1.0', audit_scope.last_tag(root))

    def test_a_directory_that_is_not_a_repository_has_no_tag(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(audit_scope.last_tag(Path(tmp)))


class CacheKeyTest(unittest.TestCase):
    def test_all_keeps_the_key_it_always_had_so_earlier_scans_stay_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'a.php').write_text('<?php')
            batch = [{'rel': 'a.php', 'lines': 1}]
            old = audit.hash_key(audit.PROMPT_VERSION, [('a.php', b'<?php')])
            self.assertEqual(old, audit._batch_key(root, batch))
            self.assertEqual(old, audit._batch_key(root, batch, 'all'))

    def test_each_focus_has_its_own_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'a.php').write_text('<?php')
            batch = [{'rel': 'a.php', 'lines': 1}]
            keys = {audit._batch_key(root, batch, f) for f in focus.FOCI}
            self.assertEqual(3, len(keys))


class ReportFocusTest(unittest.TestCase):
    def render(self, focus_name, confirmed, **extra):
        ctx = ReportTest().ctx(confirmed=confirmed, focus=focus_name, **extra)
        ctx['grade'], ctx['grade_reason'], ctx['security_grade'] = audit.focus_grades(
            confirmed, focus_name)
        return audit.render_report(ctx)

    def test_a_security_report_says_what_it_did_not_assess_and_shows_one_row(self):
        report = self.render('security', [finding('security', 'low')])
        self.assertIn('**Foco: segurança.**', report)
        self.assertIn('só de segurança', report)
        self.assertNotIn('| qualidade de código |', report)

    def test_a_quality_report_says_security_was_not_assessed(self):
        report = self.render('quality', [finding('code_quality', 'low')])
        self.assertIn('**Foco: qualidade.**', report)
        self.assertIn('A segurança não foi avaliada', report)
        self.assertNotIn('| segurança |', report)

    def test_a_quality_report_still_shows_a_vulnerability_it_found(self):
        report = self.render('quality', [finding('security', 'high'), finding('code_quality', 'low')])
        self.assertIn('1 vulnerabilidade(s) apareceram durante a leitura', report)
        self.assertIn('| segurança |', report)

    def test_a_quality_report_does_not_call_the_inventory_an_attack_surface(self):
        quality = self.render('quality', [finding('code_quality')])
        self.assertIn('**Inventário do plugin**', quality)
        self.assertNotIn('Superfície de ataque', quality)
        for name in ('security', 'all'):
            self.assertIn('**Superfície de ataque**', self.render(name, []))

    def test_the_default_report_is_unchanged(self):
        report = self.render('all', [finding('code_quality', 'low')])
        self.assertNotIn('Foco:', report)
        self.assertIn('Nota contando só os achados de segurança', report)
        self.assertIn('Os quatro tipos contam', report)

    def test_findings_reclassified_out_of_security_are_counted(self):
        report = self.render('security', [], out_of_focus=[finding('code_quality')])
        self.assertIn('1 achado(s) verificado(s) se mostraram de qualidade', report)


class NarrativeFocusTest(unittest.TestCase):
    """The prose must not talk about what the run did not evaluate."""

    def prompt_sent(self, focus_name):
        sent = []
        ctx = {'franken': 'block_x', 'inventory': {}, 'confirmed': [], 'grade': 'A',
               'security_grade': None}
        if focus_name is not None:
            ctx['focus'] = focus_name
        original = audit.call_claude
        audit.call_claude = lambda prompt, *args, **kwargs: sent.append(prompt) or '{}'
        try:
            audit.generate_narrative(ctx, '.', 'm', 'f', '')
        finally:
            audit.call_claude = original
        return sent[0]

    def test_a_quality_run_is_told_security_was_not_evaluated(self):
        prompt = self.prompt_sent('quality')
        self.assertIn('A segurança\nNÃO foi avaliada', prompt)
        self.assertIn('"focus": "quality"', prompt)
        self.assertIn('nunca defesas de segurança', prompt)

    def test_a_security_run_is_told_not_to_judge_quality(self):
        prompt = self.prompt_sent('security')
        self.assertIn('Qualidade de código, conformidade e boas práticas NÃO', prompt)

    def test_a_run_without_focus_keeps_the_original_prompt(self):
        prompt = self.prompt_sent(None)
        self.assertTrue(prompt.startswith(audit.NARRATIVE_PROMPT))


if __name__ == '__main__':
    unittest.main()
