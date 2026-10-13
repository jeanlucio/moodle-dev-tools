"""Tests for what the audit reads (audit_scope) and for how it stops when the quota is spent.

Run with:  python3 -m unittest discover -s tests
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import audit_scope as scope  # noqa: E402
import claude_cli  # noqa: E402
import security_audit as audit  # noqa: E402


def entries(*rels):
    return [{'rel': rel, 'lines': 10} for rel in rels]


PLUGIN = entries(
    'view.php', 'lib.php', 'README.md', 'styles.css', 'cli/seed.php', 'cli/seed_pt_br.php',
    'classes/game.php', 'classes/external/use_item.php', 'classes/external/deep/x.php',
    'classes/controller/scenes.php', 'classes/output/manage/tab.php', 'templates/a.mustache',
    'amd/src/view.js', 'db/access.php',
)

GROUPS = {
    'A': ['*.php', '*.md', 'classes/controller/'],
    'B': ['classes/external/'],
    'F': ['amd/src/'],
    'C': ['classes/'],
}


class MatchingTest(unittest.TestCase):
    def test_a_pattern_without_a_slash_matches_root_files_only(self):
        self.assertTrue(scope.matches('view.php', '*.php'))
        self.assertFalse(scope.matches('classes/game.php', '*.php'))

    def test_a_directory_pattern_matches_everything_below_it(self):
        self.assertTrue(scope.matches('classes/external/deep/x.php', 'classes/external/'))
        self.assertTrue(scope.matches('classes/external/deep/x.php', 'classes/external'))
        self.assertFalse(scope.matches('classes/externalx/a.php', 'classes/external'))

    def test_a_glob_with_a_slash_matches_the_path(self):
        self.assertTrue(scope.matches('cli/seed_pt_br.php', 'cli/seed*.php'))
        self.assertFalse(scope.matches('cli/other.php', 'cli/seed*.php'))


class IgnoreTest(unittest.TestCase):
    def test_ignored_files_are_split_off(self):
        kept, ignored = scope.apply_ignore(PLUGIN, ['cli/seed*.php', 'styles.css'])
        self.assertEqual(['cli/seed.php', 'cli/seed_pt_br.php', 'styles.css'],
                         sorted(e['rel'] for e in ignored))
        self.assertEqual(len(PLUGIN) - 3, len(kept))


class GroupsTest(unittest.TestCase):
    def test_the_first_matching_group_wins_and_the_catch_all_goes_last(self):
        assigned, _ = scope.assign_groups(PLUGIN, GROUPS)
        self.assertIn('classes/controller/scenes.php', [e['rel'] for e in assigned['A']])
        self.assertNotIn('classes/controller/scenes.php', [e['rel'] for e in assigned['C']])
        self.assertEqual({'classes/external/use_item.php', 'classes/external/deep/x.php'},
                         {e['rel'] for e in assigned['B']})
        self.assertEqual({'classes/game.php', 'classes/output/manage/tab.php'},
                         {e['rel'] for e in assigned['C']})

    def test_files_outside_every_group_are_reported(self):
        _, unassigned = scope.assign_groups(PLUGIN, GROUPS)
        self.assertEqual({'styles.css', 'cli/seed.php', 'cli/seed_pt_br.php',
                          'templates/a.mustache', 'db/access.php'},
                         {e['rel'] for e in unassigned})

    def test_only_accepts_group_names_and_paths(self):
        picked = scope.select_only(PLUGIN, ['B', 'templates/'], GROUPS)
        self.assertEqual({'classes/external/use_item.php', 'classes/external/deep/x.php',
                          'templates/a.mustache'}, {e['rel'] for e in picked})

    def test_only_keeps_the_order_of_the_scan(self):
        picked = scope.select_only(PLUGIN, ['F', 'A'], GROUPS)
        self.assertEqual([e['rel'] for e in PLUGIN if e in picked], [e['rel'] for e in picked])


class ConfigTest(unittest.TestCase):
    def test_a_missing_file_means_no_ignores_and_no_groups(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual({'ignore': [], 'groups': {}}, scope.load_config(Path(tmp)))

    def test_the_file_is_read_in_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / scope.CONFIG_REL
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({'ignore': ['x/'], 'groups': {'Z': ['z/'], 'A': ['a/']}}))
            config = scope.load_config(Path(tmp))
            self.assertEqual(['x/'], config['ignore'])
            self.assertEqual(['Z', 'A'], list(config['groups']))

    def test_a_malformed_file_is_an_error_not_an_empty_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / scope.CONFIG_REL
            path.parent.mkdir(parents=True)
            path.write_text('{"groups": ["not", "an", "object"]}')
            with self.assertRaises(ValueError):
                scope.load_config(Path(tmp))


class SinceTest(unittest.TestCase):
    def git(self, root, *args):
        # core.hooksPath is global here: without overriding it the commit would run the
        # developer's pre-commit gates inside the throwaway repository.
        subprocess.run(['git', '-C', str(root), '-c', 'user.name=t', '-c', 'user.email=t@t',
                        '-c', 'core.hooksPath=/dev/null', *args],
                       check=True, capture_output=True)

    def test_changed_committed_and_untracked_files_are_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.git(root, 'init', '-q')
            (root / 'a.php').write_text('1')
            (root / 'b.php').write_text('1')
            self.git(root, 'add', '.')
            self.git(root, 'commit', '-q', '-m', 'one')
            self.git(root, 'tag', 'v1')
            (root / 'a.php').write_text('2')
            self.git(root, 'commit', '-q', '-am', 'two')
            (root / 'sub').mkdir()
            (root / 'sub' / 'new.php').write_text('3')
            (root / 'b.php').write_text('uncommitted')

            changed = scope.changed_since(root, 'v1')

            self.assertEqual({'a.php', 'b.php', 'sub/new.php'}, changed)
            picked = scope.select_since(entries('a.php', 'b.php', 'c.php', 'sub/new.php'), changed)
            self.assertEqual(['a.php', 'b.php', 'sub/new.php'], [e['rel'] for e in picked])

    def test_an_unknown_reference_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.git(Path(tmp), 'init', '-q')
            with self.assertRaises(ValueError):
                scope.changed_since(Path(tmp), 'no-such-tag')


class QuotaTest(unittest.TestCase):
    def setUp(self):
        claude_cli.quota.reset()

    def tearDown(self):
        claude_cli.quota.reset()

    def call(self, side_effect, fallback='opus'):
        with mock.patch.object(claude_cli, '_run_claude', side_effect=side_effect) as run:
            try:
                return run, claude_cli.call_claude('p', '.', 'fable', fallback)
            except Exception as exc:
                return run, exc

    def test_both_models_out_of_quota_ends_the_run(self):
        run, result = self.call([RuntimeError("You've hit your monthly spend limit"),
                                 RuntimeError("You've hit your session limit")])
        self.assertIsInstance(result, claude_cli.QuotaExhausted)
        self.assertTrue(claude_cli.quota.exhausted)

        run, result = self.call([RuntimeError('should never be called')])
        self.assertIsInstance(result, claude_cli.QuotaExhausted)
        run.assert_not_called()

    def test_a_primary_that_needs_credits_still_falls_back(self):
        run, result = self.call([RuntimeError('Fable 5.1 requires usage credits'), 'answer'])
        self.assertEqual('answer', result)
        self.assertFalse(claude_cli.quota.exhausted)

    def test_a_network_failure_is_not_a_quota_failure(self):
        run, result = self.call([RuntimeError('connection reset'), RuntimeError('timeout')])
        self.assertNotIsInstance(result, claude_cli.QuotaExhausted)
        self.assertIsInstance(result, RuntimeError)
        self.assertFalse(claude_cli.quota.exhausted)

    def test_a_rate_limit_does_not_end_the_run(self):
        self.assertFalse(claude_cli.is_quota_error(RuntimeError('429 rate limit, retry later')))

    def test_without_a_fallback_a_quota_error_ends_the_run(self):
        run, result = self.call([RuntimeError('monthly spend limit reached')], fallback='')
        self.assertIsInstance(result, claude_cli.QuotaExhausted)


class ReportTest(unittest.TestCase):
    def ctx(self, **extra):
        base = {
            'franken': 'block_x', 'version': {}, 'confirmed': [], 'refuted': [],
            'unverified': [], 'phpstan': [], 'libs': [], 'narrative': {},
            'inventory': {'files_scanned': 3, 'lines_scanned': 30, 'entry_points': [],
                          'external_ws': [], 'tests_dir_files': 0, 'phpunit_tests': 0,
                          'behat_features': 0, 'has_privacy': True, 'has_backup': True,
                          'has_access': True, 'has_thirdparty': False},
            'grade': 'A+', 'grade_reason': 'nenhum achado', 'security_grade': 'A+',
        }
        base.update(extra)
        return base

    def test_an_incomplete_run_shows_no_grade_and_lists_what_was_not_verified(self):
        info = {'failed_batches': 2, 'total_batches': 6, 'unverified': 1, 'quota': True,
                'reason': 'limit'}
        ctx = self.ctx(
            incomplete=info, grade=audit.INCONCLUSIVE_GRADE, security_grade=audit.INCONCLUSIVE_GRADE,
            grade_reason=audit.incomplete_reason(info),
            unverified=[{'title': 'Falta checagem', 'file': 'lib.php', 'line': 9,
                         'severity': 'low'}])
        report = audit.render_report(ctx)
        self.assertIn('# Inconclusiva', report)
        self.assertNotIn('# A+', report)
        self.assertIn('2 de 6 lote(s)', report)
        self.assertIn('cota esgotada', report)
        self.assertIn('## Candidatos sem verificação', report)
        self.assertIn('Falta checagem', report)

    def test_a_partial_scope_is_stated_next_to_the_grade(self):
        report = audit.render_report(self.ctx(scope_note='escopo parcial (apenas B): 1 de 4'))
        self.assertIn('Escopo parcial (apenas B)', report)

    def test_a_complete_run_is_unchanged(self):
        report = audit.render_report(self.ctx())
        self.assertIn('# A+', report)
        self.assertNotIn('inconclusiva', report.lower())


if __name__ == '__main__':
    unittest.main()
