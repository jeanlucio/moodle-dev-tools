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


def sized(**files):
    """A scan from {path: lines}; paths hold slashes, so callers pass the dict as `**{...}`."""
    return [{'rel': rel, 'lines': lines} for rel, lines in files.items()]


def big_plugin():
    scan = sized(**{'view.php': 300, 'lib.php': 200, 'db/access.php': 100, 'templates/a.mustache': 500,
                    'classes/controller/c.php': 2000, 'classes/external/e.php': 3000,
                    'classes/local/a/one.php': 4000, 'classes/local/a/two.php': 4000,
                    'classes/local/b/three.php': 4000, 'classes/output/o.php': 1000,
                    'lib/helper.php': 500, 'amd/src/main.js': 1500,
                    'tests/external/e_test.php': 3000, 'tests/misc_test.php': 2000})
    return scan


class SuggestGroupsTest(unittest.TestCase):
    def test_a_small_plugin_is_read_whole(self):
        self.assertEqual({}, scope.suggest_groups(sized(**{'view.php': 500, 'classes/a.php': 900})))

    def test_tests_do_not_count_towards_being_big(self):
        scan = sized(**{'view.php': 500, 'tests/a_test.php': 90000})
        self.assertEqual({}, scope.suggest_groups(scan))

    def test_files_fall_into_the_semantic_slices_with_the_playerhud_names(self):
        scan = big_plugin()
        groups = scope.suggest_groups(scan)
        assigned, unassigned = scope.assign_groups(scan, groups)
        self.assertEqual([], unassigned)
        by_file = {e['rel']: name for name, entries in assigned.items() for e in entries}
        self.assertEqual('A', by_file['view.php'])
        self.assertEqual('A', by_file['classes/controller/c.php'])
        self.assertEqual('B', by_file['classes/external/e.php'])
        self.assertEqual('D', by_file['db/access.php'])
        self.assertEqual('E1', by_file['classes/output/o.php'])
        self.assertEqual('E2', by_file['templates/a.mustache'])
        self.assertEqual('F', by_file['amd/src/main.js'])
        self.assertEqual('T2', by_file['tests/external/e_test.php'])
        self.assertEqual('T1', by_file['tests/misc_test.php'])

    def test_a_directory_outside_the_known_slices_goes_to_the_catch_all(self):
        groups = scope.suggest_groups(big_plugin())
        assigned, _ = scope.assign_groups(big_plugin(), groups)
        owners = {name for name, entries in assigned.items() if any(e['rel'] == 'lib/helper.php' for e in entries)}
        self.assertEqual(1, len(owners))
        self.assertTrue(owners.pop().startswith('C'))

    def test_an_oversized_slice_is_split_along_directories_within_the_budget(self):
        scan = big_plugin()
        groups = scope.suggest_groups(scan, budget=8000)
        assigned, _ = scope.assign_groups(scan, groups)
        catch_all = {name: entries for name, entries in assigned.items() if name.startswith('C-')}
        self.assertGreaterEqual(len(catch_all), 2)
        for name, entries in catch_all.items():
            self.assertLessEqual(sum(e['lines'] for e in entries), 8000, name)
        # A directory stays in one piece when it fits, so a/one.php and a/two.php travel together.
        owner = {e['rel']: name for name, entries in catch_all.items() for e in entries}
        self.assertEqual(owner['classes/local/a/one.php'], owner['classes/local/a/two.php'])

    def test_every_file_lands_in_exactly_the_group_it_was_planned_for(self):
        # A split slice must not claim files of a slice that comes after it.
        scan = big_plugin() + sized(**{'classes/controller/deep/x.php': 9000,
                                       'classes/form/f.php': 9000, 'classes/other.php': 100})
        groups = scope.suggest_groups(scan, budget=9000)
        assigned, unassigned = scope.assign_groups(scan, groups)
        self.assertEqual([], unassigned)
        self.assertEqual(len(scan), sum(len(entries) for entries in assigned.values()))
        owner = {e['rel']: name for name, entries in assigned.items() for e in entries}
        self.assertTrue(owner['classes/external/e.php'] == 'B')
        self.assertTrue(owner['classes/other.php'].startswith('C'))

    def test_a_split_pattern_that_would_claim_a_later_slice_falls_back_to_listing_files(self):
        # A is over budget; its small `classes/` units would be written as `classes/`, which also
        # matches classes/external/ (B), classes/local/ (C) and so on. Each file must still land
        # in the group planned for it.
        scan = sized(**{'big.php': 6000, 'lib.php': 6000, 'classes/controller/c.php': 100,
                        'classes/form/f.php': 100, 'classes/external/e.php': 100,
                        'classes/local/l.php': 100, 'templates/t.mustache': 15000})
        groups = scope.suggest_groups(scan, budget=7000)
        assigned, unassigned = scope.assign_groups(scan, groups)
        self.assertEqual([], unassigned)
        owner = {e['rel']: name for name, entries in assigned.items() for e in entries}
        self.assertEqual('B', owner['classes/external/e.php'])
        self.assertEqual('C', owner['classes/local/l.php'])
        self.assertTrue(owner['classes/controller/c.php'].startswith('A-'))

    def test_a_tests_group_is_recognised_as_reading_tests(self):
        groups = scope.suggest_groups(big_plugin())
        for name in ('T1', 'T2'):
            self.assertTrue(scope.targets_tests([name], groups))
        self.assertFalse(scope.targets_tests(['A'], groups))

    def test_the_result_is_stable_between_calls(self):
        self.assertEqual(scope.suggest_groups(big_plugin()), scope.suggest_groups(big_plugin()))


class SaveGroupsTest(unittest.TestCase):
    def test_the_groups_are_written_and_the_ignore_list_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = Path(tmp)
            (plugin / '.plans' / 'security-audit').mkdir(parents=True)
            (plugin / scope.CONFIG_REL).write_text(json.dumps({'ignore': ['styles.css']}))
            scope.save_groups(plugin, {'A': ['*'], 'C': ['classes/']})
            config = scope.load_config(plugin)
            self.assertEqual(['styles.css'], config['ignore'])
            self.assertEqual(['A', 'C'], list(config['groups']))

    def test_groups_somebody_wrote_are_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = Path(tmp)
            (plugin / '.plans' / 'security-audit').mkdir(parents=True)
            (plugin / scope.CONFIG_REL).write_text(json.dumps({'groups': {'X': ['x/']}}))
            with self.assertRaises(ValueError):
                scope.save_groups(plugin, {'A': ['*']})
            self.assertEqual({'X': ['x/']}, scope.load_config(plugin)['groups'])

    def test_an_empty_suggestion_is_not_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                scope.save_groups(Path(tmp), {})


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


class TestsGroupTest(unittest.TestCase):
    GROUPS = {'A': ['*.php'], 'T': ['tests/external/', 'tests/']}

    def test_naming_a_tests_group_or_path_asks_for_tests(self):
        self.assertTrue(scope.targets_tests(['T'], self.GROUPS))
        self.assertTrue(scope.targets_tests(['tests/output/'], self.GROUPS))
        self.assertTrue(scope.targets_tests(['A', 'T'], self.GROUPS))

    def test_other_selections_do_not_read_tests(self):
        self.assertFalse(scope.targets_tests(['A'], self.GROUPS))
        self.assertFalse(scope.targets_tests(['classes/'], self.GROUPS))
        self.assertFalse(scope.targets_tests(None, self.GROUPS))


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



class SweepPatternTest(unittest.TestCase):
    def finding(self, title, severity, sweep=''):
        return {'title': title, 'finding_type': 'code_quality', 'severity': severity,
                'category': 'robustness', 'rule_id': 'L4-ROB-1', 'file': 'a.php', 'line': 3,
                'description': 'd', 'recommendation': 'r', 'sweep_pattern': sweep}

    def ctx(self, confirmed):
        return ReportTest().ctx(confirmed=confirmed)

    def test_the_pattern_is_shown_under_the_finding_and_listed_at_the_end(self):
        report = audit.render_report(self.ctx([self.finding('Campo sem limite', 'low',
                                                            "grep -rn \"addElement('text'\"")]))
        self.assertIn('**Padrão para varrer**', report)
        self.assertIn('## Varredura de irmãos', report)
        self.assertIn("1. **Campo sem limite**: grep -rn \"addElement('text'\"", report)

    def test_the_numbers_at_the_end_follow_the_order_of_the_report(self):
        # The report sorts by severity, so the medium finding is number 1 even though it comes second.
        report = audit.render_report(self.ctx([self.finding('Baixo', 'low', 'p-baixo'),
                                               self.finding('Médio', 'medium', 'p-medio')]))
        self.assertIn('### 1. Médio', report)
        self.assertIn('1. **Médio**: p-medio', report)
        self.assertIn('2. **Baixo**: p-baixo', report)

    def test_nothing_is_added_when_no_finding_has_a_pattern(self):
        report = audit.render_report(self.ctx([self.finding('Sem padrão', 'low')]))
        self.assertNotIn('Padrão para varrer', report)
        self.assertNotIn('Varredura de irmãos', report)

    def test_the_closing_section_tells_the_reader_to_fix_the_whole_family(self):
        report = audit.render_report(self.ctx([self.finding('X', 'low', 'busca')]))
        self.assertIn('Corrija a família inteira', report)

    def test_deterministic_checks_carry_their_own_pattern(self):
        import static_schema
        for key, spec in static_schema.SPECS.items():
            with self.subTest(check=key):
                self.assertIn('moodle-static-checks', spec['sweep_pattern'])


if __name__ == '__main__':
    unittest.main()
