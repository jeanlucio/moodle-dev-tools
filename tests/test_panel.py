"""Tests for the panel: plugin discovery, strict validation, the run queue and the HTTP layer.

Each case builds a throwaway Moodle tree with git repositories, because discovery, the groups of
a plugin and the tags all come from disk. The audit itself is replaced by a stand-in script
(MDT_PANEL_AUDIT_CMD), so no test spends quota.
Run with:  python3 -m unittest discover -s tests
"""

import http.client
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import audit_panel  # noqa: E402
import panel_data  # noqa: E402
import panel_runs  # noqa: E402

REPORT = """# Relatório de auditoria — block_demo

## Nota geral

# A

> **Foco: segurança.** Qualidade não foi avaliada.

| Tipo | critical |
"""


def git(root, *args):
    subprocess.run(['git', '-C', str(root), '-c', 'user.name=t', '-c', 'user.email=t@t',
                    '-c', 'core.hooksPath=/dev/null', *args], check=True, capture_output=True)


def make_public(tmp):
    """A Moodle public tree with one own plugin (a git repo with a tag and groups) and one foreign."""
    public = Path(tmp)
    plugin = public / 'blocks' / 'demo'
    (plugin / 'classes').mkdir(parents=True)
    (plugin / 'version.php').write_text(
        "<?php\n$plugin->component = 'block_demo';\n$plugin->version = 2026101400;\n"
        "$plugin->release = '1.2.3';\n")
    (plugin / 'classes' / 'a.php').write_text('<?php\n' + 'echo 1;\n' * 20)
    (plugin / 'view.php').write_text('<?php\n' + 'echo 2;\n' * 10)
    audit = plugin / '.plans' / 'security-audit'
    audit.mkdir(parents=True)
    (audit / 'config.json').write_text(json.dumps(
        {'ignore': [], 'groups': {'A': ['*.php'], 'B': ['classes/']}}))
    (audit / 'block_demo-2026-10-14-120000.md').write_text(REPORT)
    git(plugin, 'init', '-q')
    git(plugin, 'add', '-A')
    git(plugin, 'commit', '-q', '-m', 'one')
    git(plugin, 'tag', 'v1.0.0')
    foreign = public / 'blocks' / 'foreign'
    foreign.mkdir(parents=True)
    (foreign / 'version.php').write_text("<?php\n$plugin->component = 'block_foreign';\n")
    return public, plugin


def write_report(plugin, stamp, grade='A', scope=None, tests_read=False, failed=False):
    """A report with just the lines the panel reads: grade, scope, tests line, failed verification."""
    text = f'# Relatório\n\n## Nota geral\n\n# {grade}\n\n'
    if scope:
        text += f'> **Escopo parcial ({scope}): 4 de 9 arquivos analisáveis.** A nota vale só para eles.\n\n'
    text += '## Metodologia\n\n- Testes (`tests/`): ' + (
        'lidos a fundo (`--with-tests`)' if tests_read else 'só contados, não lidos') + '\n\n'
    if failed:
        text += '## Descartados na verificação\n\n- **X** — verificação falhou: cota\n'
    folder = plugin / '.plans' / 'security-audit'
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f'block_demo-{stamp}.md').write_text(text)


STAND_IN = """#!/bin/sh
echo "args: $@"
echo "Relatório: /somewhere/block_demo-2026-10-14-130000.md"
exit ${STAND_IN_EXIT:-0}
"""
SLOW = """#!/bin/sh
echo started
sleep 60
"""


def script(directory, body, name='audit.sh'):
    path = Path(directory) / name
    path.write_text(body)
    path.chmod(0o755)
    return str(path)


def wait_for(condition, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError('condição não atendida a tempo')


class DataTest(unittest.TestCase):
    def test_only_git_repositories_with_a_version_file_are_own_plugins(self):
        with tempfile.TemporaryDirectory() as tmp:
            public, _ = make_public(tmp)
            self.assertEqual(['blocks/demo'], [p['path'] for p in panel_data.discover_plugins(public)])

    def test_a_third_party_plugin_cloned_into_the_tree_is_not_listed(self):
        with tempfile.TemporaryDirectory() as tmp:
            public, plugin = make_public(tmp)
            with mock.patch.dict(os.environ, {'MDT_GITHUB_OWNER': 'me'}):
                panel_data.owner.cache_clear()
                try:
                    git(plugin, 'remote', 'add', 'origin', 'git@github.com:someone-else/moodle-block_demo.git')
                    self.assertEqual([], panel_data.discover_plugins(public))
                    git(plugin, 'remote', 'set-url', 'origin', 'git@github.com:me/moodle-block_demo.git')
                    self.assertEqual(['blocks/demo'], [p['path'] for p in panel_data.discover_plugins(public)])
                finally:
                    panel_data.owner.cache_clear()

    def test_the_component_and_release_come_from_version_php(self):
        with tempfile.TemporaryDirectory() as tmp:
            public, _ = make_public(tmp)
            plugin = panel_data.discover_plugins(public)[0]
            self.assertEqual(('block_demo', '1.2.3'), (plugin['component'], plugin['release']))

    def test_a_path_that_is_not_exactly_a_discovered_plugin_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            public, _ = make_public(tmp)
            for path in ('blocks/foreign', '../etc', 'blocks/demo/..', '/etc/passwd', ''):
                with self.subTest(path=path):
                    self.assertIsNone(panel_data.find_plugin(path, public))
            self.assertIsNotNone(panel_data.find_plugin('blocks/demo', public))

    def test_the_last_audit_is_read_from_the_newest_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, plugin = make_public(tmp)
            audit = panel_data.last_audit(plugin)
            self.assertEqual(('A', 'security', '2026-10-14'),
                             (audit['grade'], audit['focus'], audit['date']))

    def test_each_side_keeps_the_last_run_that_graded_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, plugin = make_public(tmp)
            folder = plugin / '.plans' / 'security-audit'
            (folder / 'block_demo-2026-10-15-120000.md').write_text(
                '# Relatório\n\n## Nota geral\n\n# B+\n\n'
                '> **Foco: qualidade.** Segurança não foi avaliada.\n')
            grades = panel_data.last_grades(plugin)
            self.assertEqual(('B+', '2026-10-15'), (grades['quality']['grade'], grades['quality']['date']))
            self.assertEqual(('A', '2026-10-14'), (grades['security']['grade'], grades['security']['date']))

    def test_a_general_run_grades_quality_from_its_findings_and_security_from_its_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, plugin = make_public(tmp)
            folder = plugin / '.plans' / 'security-audit'
            name = 'block_demo-2026-10-16-120000'
            (folder / f'{name}.md').write_text(
                '# Relatório\n\n## Nota geral\n\n# B+\n\n'
                'Nota contando só os achados de segurança: **A+**\n')
            findings = [{'finding_type': 'quality', 'severity': 'medium'}]
            (folder / f'{name}.json').write_text(json.dumps({'confirmed': findings}))
            grades = panel_data.last_grades(plugin)
            self.assertEqual('A+', grades['security']['grade'])
            self.assertEqual(('B+', '2026-10-16'), (grades['quality']['grade'], grades['quality']['date']))
            self.assertEqual(grades['quality']['file'], grades['security']['file'])

    def test_an_inconclusive_run_grades_neither_side(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, plugin = make_public(tmp)
            folder = plugin / '.plans' / 'security-audit'
            (folder / 'block_demo-2026-10-16-120000.md').write_text(
                REPORT.replace('# A', '# inconclusiva', 1))
            grades = panel_data.last_grades(plugin)
            self.assertEqual('2026-10-14', grades['security']['date'])
            self.assertIsNone(grades['quality'])

    def test_a_report_is_read_only_by_its_plain_file_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, plugin = make_public(tmp)
            self.assertIn('Nota geral', panel_data.read_report(plugin, 'block_demo-2026-10-14-120000.md'))
            for name in ('../config.json', '../../version.php', 'config.json', '/etc/passwd',
                         'block_demo-2026-10-14-120000.md/../../x.md', ''):
                with self.subTest(name=name):
                    self.assertIsNone(panel_data.read_report(plugin, name))

    def test_groups_show_their_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, plugin = make_public(tmp)
            groups = {g['name']: g for g in panel_data.groups_info(plugin)}
            # version.php and view.php sit at the root; a.php is under classes/.
            self.assertEqual(2, groups['A']['files'])
            self.assertEqual(1, groups['B']['files'])

    def test_groups_are_listed_by_name_but_still_claim_files_in_config_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, plugin = make_public(tmp)
            (plugin / 'classes' / 'ai').mkdir()
            (plugin / 'classes' / 'ai' / 'b.php').write_text('<?php\necho 1;\n')
            config = plugin / '.plans' / 'security-audit' / 'config.json'
            # The broad group C comes after D on purpose: it would otherwise take D's files.
            config.write_text(json.dumps({'ignore': [], 'groups': {
                'A': ['*.php'], 'E10': ['templates/'], 'D': ['classes/ai/'], 'E2': ['amd/'],
                'C': ['classes/']}}))
            groups = panel_data.groups_info(plugin)
            self.assertEqual(['A', 'C', 'D', 'E2', 'E10'], [g['name'] for g in groups])
            by_name = {g['name']: g for g in groups}
            self.assertEqual(1, by_name['D']['files'])
            self.assertEqual(1, by_name['C']['files'])

    def test_a_tests_group_shows_its_real_size_and_says_it_reads_tests(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, plugin = make_public(tmp)
            (plugin / 'tests').mkdir()
            (plugin / 'tests' / 'a_test.php').write_text('<?php\n' + 'echo 3;\n' * 5)
            config = plugin / '.plans' / 'security-audit' / 'config.json'
            config.write_text(json.dumps({'ignore': [], 'groups': {
                'A': ['*.php'], 'B': ['classes/'], 'T': ['tests/']}}))
            groups = {g['name']: g for g in panel_data.groups_info(plugin)}
            self.assertEqual((1, True), (groups['T']['files'], groups['T']['tests']))
            self.assertFalse(groups['A']['tests'])

    def test_the_estimate_counts_the_files_a_run_would_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, plugin = make_public(tmp)
            all_files = panel_data.estimate(plugin, {'focus': 'all', 'only': [], 'since': None})
            only_b = panel_data.estimate(plugin, {'focus': 'all', 'only': ['B'], 'since': None})
            self.assertEqual(3, all_files['files'])
            self.assertEqual(1, only_b['files'])
            self.assertEqual(1, only_b['batches'])


class GroupCoverageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        _, self.plugin = make_public(self.tmp.name)
        # make_public already wrote a full-plugin report dated 2026-10-14; start from a clean slate.
        for report in (self.plugin / '.plans' / 'security-audit').glob('block_demo-*.md'):
            report.unlink()

    def cover(self, names=('A', 'B', 'T'), tests=('T',)):
        return panel_data.group_coverage(self.plugin, list(names), set(tests))

    def test_a_group_never_audited_has_no_last_report(self):
        self.assertIsNone(self.cover()['A']['last'])

    def test_a_report_of_the_whole_plugin_covers_every_group(self):
        write_report(self.plugin, '2026-10-01-100000', 'B+')
        found = self.cover(tests=())
        self.assertEqual({('2026-10-01', 'plugin')}, {(f['last']['date'], f['last']['scope']) for f in found.values()})

    def test_a_partial_run_covers_only_the_groups_it_named(self):
        write_report(self.plugin, '2026-10-02-100000', 'A+', scope='apenas A, B')
        found = self.cover(tests=())
        self.assertEqual('group', found['A']['last']['scope'])
        self.assertEqual('group', found['B']['last']['scope'])
        self.assertIsNone(found['T']['last'])

    def test_the_newest_report_that_covers_the_group_wins(self):
        write_report(self.plugin, '2026-10-01-100000', 'B+')
        write_report(self.plugin, '2026-10-03-100000', 'A+', scope='apenas A')
        found = self.cover(tests=())
        self.assertEqual(('2026-10-03', 'A+'), (found['A']['last']['date'], found['A']['last']['grade']))
        self.assertEqual(('2026-10-01', 'B+'), (found['B']['last']['date'], found['B']['last']['grade']))

    def test_a_run_narrowed_by_since_does_not_count_and_is_reported_as_set_aside(self):
        write_report(self.plugin, '2026-10-01-100000', 'B+')
        write_report(self.plugin, '2026-10-05-100000', 'A+', scope='apenas A; alterados desde v1.0.0')
        entry = self.cover(tests=())['A']
        self.assertEqual('2026-10-01', entry['last']['date'])
        self.assertEqual('2026-10-05', entry['ignored']['date'])
        self.assertIn('v1.0.0', entry['ignored']['reason'])

    def test_a_report_whose_verification_failed_is_not_a_result(self):
        write_report(self.plugin, '2026-10-01-100000', 'B+')
        write_report(self.plugin, '2026-10-06-100000', 'A+', failed=True)
        entry = self.cover(tests=())['A']
        self.assertEqual(('2026-10-01', 'B+'), (entry['last']['date'], entry['last']['grade']))
        self.assertEqual('a verificação falhou', entry['ignored']['reason'])

    def test_an_inconclusive_report_is_not_a_result(self):
        write_report(self.plugin, '2026-10-07-100000', 'Inconclusiva')
        entry = self.cover(tests=())['A']
        self.assertIsNone(entry['last'])
        self.assertIn('inconclusiva', entry['ignored']['reason'])

    def test_a_group_that_reads_tests_needs_a_report_that_read_them(self):
        write_report(self.plugin, '2026-10-01-100000', 'B+', tests_read=True)
        write_report(self.plugin, '2026-10-08-100000', 'A+', tests_read=False)
        entry = self.cover()['T']
        self.assertEqual('2026-10-01', entry['last']['date'])
        self.assertEqual('não leu tests/', entry['ignored']['reason'])

    def test_a_group_that_does_not_read_tests_ignores_that_line(self):
        write_report(self.plugin, '2026-10-08-100000', 'A+', tests_read=False)
        self.assertEqual('2026-10-08', self.cover()['A']['last']['date'])

    def test_groups_info_and_the_summary_carry_the_coverage(self):
        write_report(self.plugin, '2026-10-02-100000', 'A+', scope='apenas A')
        groups = {g['name']: g for g in panel_data.groups_info(self.plugin)}
        self.assertEqual('A+', groups['A']['last']['grade'])
        self.assertIsNone(groups['B']['last'])
        self.assertEqual({'total': 2, 'covered': 1, 'closed': 0}, panel_data.groups_summary(self.plugin))

    def test_a_plugin_without_groups_has_no_summary(self):
        (self.plugin / '.plans' / 'security-audit' / 'config.json').write_text(
            json.dumps({'ignore': [], 'groups': {}}))
        self.assertIsNone(panel_data.groups_summary(self.plugin))


class ValidationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.public, self.plugin = make_public(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def check(self, payload):
        return panel_runs.validate({'plugin': 'blocks/demo', **payload}, self.public)

    def test_a_minimal_request_gets_every_option_filled_in(self):
        options = self.check({})
        self.assertEqual('all', options['focus'])
        self.assertEqual([], options['only'])
        self.assertFalse(options['with_tests'])
        self.assertEqual(6, options['phpstan_level'])

    def test_a_full_request_is_accepted(self):
        options = self.check({'focus': 'security', 'only': ['A', 'B'], 'since': 'v1.0.0',
                              'with_tests': True, 'no_verify': True, 'no_phpstan': True})
        self.assertEqual(['A', 'B'], options['only'])
        self.assertTrue(options['no_phpstan'])
        self.assertEqual('v1.0.0', options['since'])

    def test_everything_unexpected_is_refused(self):
        bad = [
            {'plugin': 'blocks/foreign'}, {'plugin': '../x'}, {'focus': 'everything'},
            {'only': ['Z']}, {'only': 'A'}, {'only': ['A; rm -rf /']}, {'only': [1]},
            {'since': '--help'}, {'since': '-x'}, {'since': 'v9.9.9'}, {'since': '$(id)'},
            {'since': 'v1.0.0; ls'}, {'with_tests': 'yes'}, {'no_verify': 1}, {'no_phpstan': 'sim'},
            {'phpstan_level': 10}, {'phpstan_level': -1}, {'phpstan_level': '6'}, {'phpstan_level': True},
            {'phpstan_level': 6.0},
            {'full': True},  # only valid for the quality focus
            {'model': 'x'}, {'argv': ['ls']}, {'cmd': 'ls'},
        ]
        for payload in bad:
            with self.subTest(payload=payload), self.assertRaises(panel_runs.ValidationError):
                self.check(payload)

    def test_a_non_default_phpstan_level_reaches_the_command(self):
        with mock.patch.dict(os.environ, {'MDT_PANEL_AUDIT_CMD': '/x/audit'}):
            argv = panel_runs.build_argv(self.check({'phpstan_level': 8}))
            default = panel_runs.build_argv(self.check({}))
        self.assertEqual(['--phpstan-level', '8'], argv[-2:])
        self.assertNotIn('--phpstan-level', default)

    def test_full_is_valid_for_the_quality_focus(self):
        self.assertTrue(self.check({'focus': 'quality', 'full': True})['full'])

    def test_a_body_that_is_not_an_object_is_refused(self):
        for body in ('x', [], None, 3):
            with self.subTest(body=body), self.assertRaises(panel_runs.ValidationError):
                panel_runs.validate(body, self.public)

    def test_the_argument_list_has_no_shell_and_only_validated_values(self):
        with mock.patch.dict(os.environ, {'MDT_PANEL_AUDIT_CMD': '/x/audit'}):
            argv = panel_runs.build_argv(self.check({
                'focus': 'quality', 'only': ['A'], 'since': 'v1.0.0', 'with_tests': True,
                'no_verify': True, 'no_phpstan': True}))
        self.assertEqual(['/x/audit', 'blocks/demo', '--focus', 'quality', '--json', '--only', 'A',
                          '--since', 'v1.0.0', '--with-tests', '--no-verify', '--no-phpstan'], argv)


class QueueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.public, _ = make_public(self.tmp.name + '/moodle')
        self.runs = Path(self.tmp.name) / 'runs'
        self.addCleanup(self.tmp.cleanup)
        self.opts = panel_runs.validate({'plugin': 'blocks/demo', 'focus': 'security'}, self.public)

    def patch(self, command):
        patcher = mock.patch.dict(os.environ, {'MDT_PANEL_AUDIT_CMD': command})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_enqueue_writes_a_queued_run_that_can_be_read_back(self):
        run = panel_runs.enqueue(self.opts, self.runs)
        self.assertEqual('queued', panel_runs.read_run(run['id'], self.runs)['status'])
        self.assertEqual([run['id']], [r['id'] for r in panel_runs.list_runs(10, self.runs)])

    def test_a_run_id_is_never_used_as_a_path(self):
        for run_id in ('../x', 'a/b', '', 'a.b'):
            self.assertIsNone(panel_runs.read_run(run_id, self.runs))

    def test_the_audit_runs_to_the_end_and_its_report_is_recorded(self):
        self.patch(script(self.tmp.name, STAND_IN))
        run = panel_runs.enqueue(self.opts, self.runs)
        panel_runs.tick(self.runs)
        done = wait_for(lambda: (r := panel_runs.read_run(run['id'], self.runs))
                        and r['status'] == 'done' and r)
        self.assertEqual(0, done['exit_code'])
        self.assertEqual('block_demo-2026-10-14-130000.md', done['report'])
        text, _ = panel_runs.read_log(done)
        self.assertIn('--focus security', text)

    def test_exit_status_2_is_an_inconclusive_run_not_a_failure(self):
        self.patch(script(self.tmp.name, STAND_IN))
        with mock.patch.dict(os.environ, {'STAND_IN_EXIT': '2'}):
            run = panel_runs.enqueue(self.opts, self.runs)
            panel_runs.tick(self.runs)
            done = wait_for(lambda: (r := panel_runs.read_run(run['id'], self.runs))
                            and r['status'] not in panel_runs.ACTIVE and r)
        self.assertEqual('inconclusive', done['status'])

    def test_only_one_run_is_active_at_a_time(self):
        self.patch(script(self.tmp.name, SLOW))
        first = panel_runs.enqueue(self.opts, self.runs, now=1000)
        second = panel_runs.enqueue(self.opts, self.runs, now=2000)
        self.assertEqual(first['id'], panel_runs.tick(self.runs)['id'])
        wait_for(lambda: panel_runs.runner_alive(panel_runs.read_run(first['id'], self.runs)))
        self.assertIsNone(panel_runs.tick(self.runs))
        self.assertEqual('queued', panel_runs.read_run(second['id'], self.runs)['status'])
        panel_runs.cancel(first['id'], self.runs)
        wait_for(lambda: not panel_runs.runner_alive(panel_runs.read_run(first['id'], self.runs)))

    def test_the_oldest_queued_run_starts_first(self):
        self.patch(script(self.tmp.name, STAND_IN))
        newer = panel_runs.enqueue(self.opts, self.runs, now=2000)
        older = panel_runs.enqueue(self.opts, self.runs, now=1000)
        self.assertEqual(older['id'], panel_runs.tick(self.runs)['id'])
        wait_for(lambda: panel_runs.read_run(older['id'], self.runs)['status'] == 'done')
        self.assertEqual(newer['id'], panel_runs.tick(self.runs)['id'])
        wait_for(lambda: panel_runs.read_run(newer['id'], self.runs)['status'] == 'done')

    def test_cancelling_a_queued_run_keeps_it_from_starting(self):
        run = panel_runs.enqueue(self.opts, self.runs)
        self.assertEqual('cancelled', panel_runs.cancel(run['id'], self.runs)['status'])
        self.assertIsNone(panel_runs.tick(self.runs))

    def test_cancelling_a_running_run_stops_the_runner_and_the_audit_it_started(self):
        self.patch(script(self.tmp.name, SLOW))
        run = panel_runs.enqueue(self.opts, self.runs)
        panel_runs.tick(self.runs)
        running = wait_for(lambda: (r := panel_runs.read_run(run['id'], self.runs))
                           and r['pid'] and panel_runs.runner_alive(r) and r)
        group = os.getpgid(running['pid'])
        panel_runs.cancel(run['id'], self.runs)
        wait_for(lambda: not panel_runs.runner_alive(running))
        self.assertEqual('cancelled', panel_runs.read_run(run['id'], self.runs)['status'])

        def group_gone():
            try:
                os.killpg(group, 0)
            except ProcessLookupError:
                return True
            return False
        wait_for(group_gone)

    def test_a_running_run_whose_runner_vanished_is_marked_lost(self):
        run = panel_runs.enqueue(self.opts, self.runs)
        run.update(status='running', pid=999999, started=time.time() - 600)
        panel_runs.write_run(run, self.runs)
        panel_runs.tick(self.runs)
        self.assertEqual('lost', panel_runs.read_run(run['id'], self.runs)['status'])

    def test_a_run_that_just_started_is_not_judged_lost(self):
        run = panel_runs.enqueue(self.opts, self.runs)
        run.update(status='running', pid=None, started=time.time())
        panel_runs.write_run(run, self.runs)
        self.assertEqual('running', panel_runs.reconcile(run, self.runs)['status'])

    def test_a_pid_that_belongs_to_another_program_is_not_taken_for_the_runner(self):
        run = panel_runs.enqueue(self.opts, self.runs)
        run.update(status='running', pid=os.getpid(), started=time.time() - 600)
        self.assertFalse(panel_runs.runner_alive(run))


class ServerTest(unittest.TestCase):
    TOKEN = 'test-token'

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.public, cls.plugin = make_public(cls.tmp.name + '/moodle')
        cls.runs = Path(cls.tmp.name) / 'runs'
        cls.server = audit_panel.make_server(0, cls.TOKEN, cls.public, cls.runs)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def request(self, method, path, body=None, headers=None, host=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        merged = {'Host': host or f'127.0.0.1:{self.port}', **(headers or {})}
        connection.request(method, path, body=body, headers=merged)
        response = connection.getresponse()
        data = response.read().decode('utf-8')
        connection.close()
        return response, data

    def post(self, path, payload, token=None, content_type='application/json'):
        headers = {'Content-Type': content_type}
        if token is not None:
            headers['X-Panel-Token'] = token
        return self.request('POST', path, json.dumps(payload), headers)

    def test_a_page_with_a_foreign_host_header_is_refused(self):
        for host in ('evil.example', f'evil.example:{self.port}', '127.0.0.1.evil.example',
                     f'localhost.evil.example:{self.port}', f'192.168.0.5:{self.port}'):
            with self.subTest(host=host):
                self.assertEqual(403, self.request('GET', '/api/plugins', host=host)[0].status)

    def test_localhost_and_127_are_accepted_on_any_port_for_a_port_forward(self):
        for host in (f'localhost:{self.port}', f'127.0.0.1:{self.port}', 'localhost:9999',
                     '127.0.0.1:8766', 'localhost'):
            with self.subTest(host=host):
                self.assertEqual(200, self.request('GET', '/api/plugins', host=host)[0].status)

    def test_the_served_page_carries_the_token_and_a_strict_policy(self):
        response, body = self.request('GET', '/')
        self.assertIn(f'content="{self.TOKEN}"', body)
        self.assertNotIn('__TOKEN__', body)
        csp = response.getheader('Content-Security-Policy')
        self.assertIn("default-src 'none'", csp)
        self.assertNotIn('unsafe-inline', csp)
        self.assertEqual('nosniff', response.getheader('X-Content-Type-Options'))

    def test_static_files_are_served(self):
        self.assertEqual(200, self.request('GET', '/panel.js')[0].status)
        self.assertEqual(200, self.request('GET', '/panel.css')[0].status)

    def test_the_plugin_list_has_the_last_audit(self):
        _, body = self.request('GET', '/api/plugins')
        plugins = json.loads(body)
        self.assertEqual(['block_demo'], [p['component'] for p in plugins])
        self.assertEqual('A', plugins[0]['last_audit']['grade'])

    def test_the_plugin_detail_lists_groups_tags_and_reports(self):
        _, body = self.request('GET', '/api/plugin?path=blocks/demo')
        detail = json.loads(body)
        self.assertEqual(['A', 'B'], [g['name'] for g in detail['groups']])
        self.assertEqual(['v1.0.0'], detail['tags'])
        self.assertEqual(1, len(detail['reports']))

    def test_an_unknown_plugin_is_a_404(self):
        self.assertEqual(404, self.request('GET', '/api/plugin?path=blocks/foreign')[0].status)
        self.assertEqual(404, self.request('GET', '/api/plugin?path=../x')[0].status)

    def test_a_report_is_served_and_a_traversal_is_not(self):
        ok, body = self.request('GET', '/api/report?plugin=blocks/demo&file=block_demo-2026-10-14-120000.md')
        self.assertEqual(200, ok.status)
        self.assertIn('Nota geral', body)
        for name in ('../config.json', '..%2Fconfig.json', '../../version.php'):
            with self.subTest(name=name):
                self.assertEqual(404, self.request(
                    'GET', f'/api/report?plugin=blocks/demo&file={name}')[0].status)

    def test_the_estimate_is_served(self):
        _, body = self.request('GET', '/api/estimate?plugin=blocks/demo&focus=all&only=B')
        self.assertEqual(1, json.loads(body)['files'])

    def test_the_progress_endpoint_refuses_a_name_that_is_not_a_component(self):
        self.assertEqual(400, self.request('GET', '/api/progress?component=../x')[0].status)

    def test_an_action_without_the_token_is_refused(self):
        self.assertEqual(403, self.post('/api/runs', {'plugin': 'blocks/demo'})[0].status)
        self.assertEqual(403, self.post('/api/runs', {'plugin': 'blocks/demo'}, token='wrong')[0].status)

    def test_an_action_that_is_not_json_is_refused(self):
        response, _ = self.post('/api/runs', {'plugin': 'blocks/demo'}, self.TOKEN,
                                content_type='application/x-www-form-urlencoded')
        self.assertEqual(415, response.status)

    def test_a_valid_request_is_queued_and_listed(self):
        response, body = self.post('/api/runs', {'plugin': 'blocks/demo', 'focus': 'security',
                                                 'only': ['A']}, self.TOKEN)
        self.assertEqual(201, response.status)
        run = json.loads(body)
        self.assertEqual('queued', run['status'])
        self.assertNotIn('argv', run)
        listed = json.loads(self.request('GET', '/api/runs')[1])
        self.assertIn(run['id'], [r['id'] for r in listed])

    def test_an_invalid_request_is_a_400_with_the_reason(self):
        response, body = self.post('/api/runs', {'plugin': 'blocks/demo', 'since': '--help'}, self.TOKEN)
        self.assertEqual(400, response.status)
        self.assertIn('referência', json.loads(body)['error'])

    def test_a_queued_run_can_be_cancelled_over_http(self):
        run = json.loads(self.post('/api/runs', {'plugin': 'blocks/demo'}, self.TOKEN)[1])
        response, body = self.post(f'/api/runs/{run["id"]}/cancel', {}, self.TOKEN)
        self.assertEqual(200, response.status)
        self.assertEqual('cancelled', json.loads(body)['status'])

    def test_an_unknown_run_is_a_404(self):
        self.assertEqual(404, self.request('GET', '/api/runs/20260101-000000-abcdef')[0].status)

    def test_the_server_listens_on_loopback_only(self):
        self.assertEqual('127.0.0.1', self.server.server_address[0])


if __name__ == '__main__':
    unittest.main()
