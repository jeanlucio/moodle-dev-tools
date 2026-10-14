"""Tests for static_schema: the checks that compare a plugin's schema and declarations with its code.

Each case builds a throwaway plugin on disk, because every check reads several files at once
(install.xml with the backup step, the delete hooks, the form and the privacy provider).
Run with:  python3 -m unittest discover -s tests
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import audit_static_checks as audit_checks  # noqa: E402
import static_schema as schema  # noqa: E402

INSTALL = """<?xml version="1.0" encoding="UTF-8" ?>
<XMLDB PATH="mod/demo/db" VERSION="20261014">
  <TABLES>
    <TABLE NAME="demo">
      <FIELDS>
        <FIELD NAME="id" TYPE="int" LENGTH="10" NOTNULL="true" SEQUENCE="true"/>
        <FIELD NAME="course" TYPE="int" LENGTH="10" NOTNULL="true" SEQUENCE="false"/>
        <FIELD NAME="name" TYPE="char" LENGTH="255" NOTNULL="true" SEQUENCE="false"/>
        <FIELD NAME="completionsubmit" TYPE="int" LENGTH="1" NOTNULL="true" SEQUENCE="false"/>
      </FIELDS>
    </TABLE>
    <TABLE NAME="demo_items">
      <FIELDS>
        <FIELD NAME="id" TYPE="int" LENGTH="10" NOTNULL="true" SEQUENCE="true"/>
        <FIELD NAME="demoid" TYPE="int" LENGTH="10" NOTNULL="true" SEQUENCE="false"/>
        <FIELD NAME="label" TYPE="char" LENGTH="100" NOTNULL="true" SEQUENCE="false"/>
        <FIELD NAME="extra" TYPE="text" NOTNULL="false" SEQUENCE="false"/>
      </FIELDS>
      <KEYS>
        <KEY NAME="primary" TYPE="primary" FIELDS="id"/>
        <KEY NAME="demoid" TYPE="foreign" FIELDS="demoid" REFTABLE="demo" REFFIELDS="id"/>
      </KEYS>
    </TABLE>
  </TABLES>
</XMLDB>
"""

BACKUP = """<?php
class backup_demo_activity_structure_step extends backup_activity_structure_step {
    protected function define_structure() {
        $demo = new backup_nested_element('demo', ['id'], ['name', 'timemodified']);
        $items = new backup_nested_element('items');
        $item = new backup_nested_element('item', ['id'], %(itemattrs)s);
        $demo->set_source_table('demo', ['id' => backup::VAR_ACTIVITYID]);
        $item->set_source_table('demo_items', ['demoid' => backup::VAR_PARENTID]);
        return $this->prepare_activity_structure($demo);
    }
}
"""

VERSION = "<?php\n$plugin->component = 'mod_demo';\n"


def make_plugin(files):
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
    return tmp, root


def with_backup(itemattrs="['label']"):
    return {'db/install.xml': INSTALL, 'version.php': VERSION,
            'backup/moodle2/backup_demo_stepslib.php': BACKUP % {'itemattrs': itemattrs}}


class BackupColumnsTest(unittest.TestCase):
    def found(self, root, accepted=None):
        return {(v['table'], v['column']) for v in schema.backup_columns(root, accepted)}

    def test_a_column_the_backup_never_saves_is_found(self):
        tmp, root = make_plugin(with_backup())
        with tmp:
            self.assertEqual({('demo', 'completionsubmit'), ('demo_items', 'extra')},
                             self.found(root))

    def test_the_parent_link_and_the_course_are_not_data(self):
        tmp, root = make_plugin(with_backup())
        with tmp:
            columns = {c for _, c in self.found(root)}
            self.assertNotIn('course', columns)
            self.assertNotIn('demoid', columns)

    def test_a_listed_column_is_not_reported(self):
        tmp, root = make_plugin(with_backup("['label', 'extra']"))
        with tmp:
            self.assertEqual({('demo', 'completionsubmit')}, self.found(root))

    def test_an_accepted_exception_needs_a_reason(self):
        tmp, root = make_plugin(with_backup())
        with tmp:
            accepted = {'backup_columns': {'demo.completionsubmit': 'rebuilt on restore',
                                           'demo_items.extra': ''}}
            self.assertEqual({('demo_items', 'extra')},
                             {(v['table'], v['column'])
                              for v in schema.backup_columns(root, schema_accepted(accepted))})

    def test_an_attribute_list_built_at_run_time_is_not_judged(self):
        tmp, root = make_plugin(with_backup("array_merge(['label'], $more)"))
        with tmp:
            self.assertEqual({('demo', 'completionsubmit')}, self.found(root))

    def test_a_table_the_backup_never_mentions_is_found(self):
        files = with_backup()
        files['backup/moodle2/backup_demo_stepslib.php'] = files[
            'backup/moodle2/backup_demo_stepslib.php'].replace(
                "$item->set_source_table('demo_items', ['demoid' => backup::VAR_PARENTID]);", '')
        tmp, root = make_plugin(files)
        with tmp:
            self.assertIn(('demo_items', None), self.found(root))

    def test_a_table_saved_through_sql_held_in_a_variable_is_not_reported(self):
        files = with_backup()
        files['backup/moodle2/backup_demo_stepslib.php'] = files[
            'backup/moodle2/backup_demo_stepslib.php'].replace(
                "$item->set_source_table('demo_items', ['demoid' => backup::VAR_PARENTID]);",
                '$sql = "SELECT i.* FROM {demo_items} i WHERE i.demoid = ?";\n'
                "        $item->set_source_sql($sql, [backup::VAR_PARENTID]);")
        tmp, root = make_plugin(files)
        with tmp:
            self.assertEqual({('demo', 'completionsubmit')}, self.found(root))

    def test_a_foreign_key_the_restore_assigns_itself_is_not_reported(self):
        install = INSTALL.replace('<FIELD NAME="label"', '<FIELD NAME="otherid" TYPE="int" LENGTH="10" '
                                  'NOTNULL="true" SEQUENCE="false"/>\n        <FIELD NAME="label"')
        install = install.replace('</KEYS>\n    </TABLE>\n  </TABLES>', '<KEY NAME="otherid" TYPE="foreign" '
                                  'FIELDS="otherid" REFTABLE="demo" REFFIELDS="id"/>\n      </KEYS>\n    '
                                  '</TABLE>\n  </TABLES>')
        files = with_backup()
        files['db/install.xml'] = install
        files['backup/moodle2/restore_demo_stepslib.php'] = '<?php\n$data->otherid = $this->get_new_parentid(\'demo\');\n'
        tmp, root = make_plugin(files)
        with tmp:
            self.assertNotIn(('demo_items', 'otherid'), self.found(root))
        del files['backup/moodle2/restore_demo_stepslib.php']
        tmp, root = make_plugin(files)
        with tmp:
            self.assertIn(('demo_items', 'otherid'), self.found(root))

    def test_a_plugin_without_backup_is_not_applicable(self):
        tmp, root = make_plugin({'db/install.xml': INSTALL})
        with tmp:
            self.assertEqual([], schema.backup_columns(root))


def schema_accepted(accepted):
    """The accepted-exceptions mapping with empty reasons dropped, as load_accepted() does."""
    return {check: {k: v for k, v in entries.items() if v.strip()}
            for check, entries in accepted.items()}


class AcceptedFileTest(unittest.TestCase):
    def test_entries_without_a_reason_are_ignored(self):
        tmp, root = make_plugin({schema.ACCEPTED_REL: json.dumps(
            {'backup_columns': {'a.b': 'because', 'c.d': '  '}})})
        with tmp:
            self.assertEqual({'backup_columns': {'a.b': 'because'}}, schema.load_accepted(root))

    def test_a_malformed_file_is_an_error(self):
        tmp, root = make_plugin({schema.ACCEPTED_REL: '{'})
        with tmp, self.assertRaises(ValueError):
            schema.load_accepted(root)


class DeleteCleanupTest(unittest.TestCase):
    def found(self, root):
        return {(v['table'], v['kind']) for v in schema.delete_cleanup(root)}

    def test_a_table_nothing_deletes_is_found(self):
        tmp, root = make_plugin({
            'db/install.xml': INSTALL, 'version.php': VERSION,
            'lib.php': "<?php\nfunction demo_delete_instance($id) {\n    global $DB;\n"
                       "    $DB->delete_records('demo_items', ['demoid' => $id]);\n"
                       "    $DB->delete_records('demo', ['id' => $id]);\n}\n"})
        with tmp:
            self.assertEqual(set(), self.found(root))

    def test_the_privacy_provider_does_not_count_as_cleanup(self):
        tmp, root = make_plugin({
            'db/install.xml': INSTALL, 'version.php': VERSION,
            'lib.php': "<?php\n$DB->delete_records('demo', ['id' => $id]);\n",
            'classes/privacy/provider.php':
                "<?php\n$DB->delete_records('demo_items', ['userid' => $userid]);\n"})
        with tmp:
            self.assertEqual({('demo_items', 'never_deleted')}, self.found(root))

    def test_the_provider_counts_when_an_observer_delegates_the_course_cleanup_to_it(self):
        install = INSTALL.replace('NAME="demoid"', 'NAME="courseid"')
        files = {
            'db/install.xml': install, 'version.php': "<?php\n$plugin->component = 'local_demo';\n",
            'db/events.php': "<?php\n$observers = [['eventname' => course_deleted::class]];\n",
            'classes/observer.php': "<?php\nclass observer {\n    public static function course_deleted($e) {\n"
                                    "        provider::delete_data_for_all_users_in_context($e->get_context());\n    }\n}\n",
            'classes/privacy/provider.php': "<?php\nclass provider {\n    public static function delete_data_for_all_users_in_context($c) {\n"
                                            "        $DB->delete_records('demo');\n        $DB->delete_records('demo_items');\n    }\n}\n",
        }
        tmp, root = make_plugin(files)
        with tmp:
            self.assertEqual(set(), self.found(root))
        del files['classes/observer.php']
        tmp, root = make_plugin(files)
        with tmp:
            self.assertEqual({('demo', 'never_deleted'), ('demo_items', 'never_deleted')}, self.found(root))

    def test_a_helper_that_deletes_through_a_variable_counts_for_the_tables_it_lists(self):
        tmp, root = make_plugin({
            'db/install.xml': INSTALL, 'version.php': VERSION,
            'classes/cleanup.php':
                "<?php\nforeach (['demo_items', 'demo'] as $table) {\n"
                "    $DB->delete_records($table, ['x' => 1]);\n}\n"})
        with tmp:
            self.assertEqual(set(), self.found(root))

    def test_a_course_keyed_table_of_a_non_activity_needs_the_observer(self):
        install = INSTALL.replace('NAME="demoid"', 'NAME="courseid"')
        files = {'db/install.xml': install,
                 'version.php': "<?php\n$plugin->component = 'local_demo';\n",
                 'classes/cleanup.php': "<?php\n$DB->delete_records('demo', ['id' => 1]);\n"
                                        "$DB->delete_records('demo_items', ['id' => 1]);\n"}
        tmp, root = make_plugin(files)
        with tmp:
            self.assertIn(('demo_items', 'no_course_observer'), self.found(root))
        files['db/events.php'] = "<?php\n$observers = [['eventname' => '\\core\\event\\course_deleted']];\n"
        tmp, root = make_plugin(files)
        with tmp:
            self.assertEqual(set(), self.found(root))

    def test_a_site_wide_table_without_an_owner_needs_no_cleanup(self):
        install = INSTALL.replace('<FIELD NAME="demoid" TYPE="int" LENGTH="10" NOTNULL="true" SEQUENCE="false"/>',
                                  '<FIELD NAME="userid" TYPE="int" LENGTH="10" NOTNULL="true" SEQUENCE="false"/>')
        install = install.replace('<KEY NAME="demoid" TYPE="foreign" FIELDS="demoid" REFTABLE="demo" REFFIELDS="id"/>', '')
        install = install.replace('NAME="course" TYPE', 'NAME="siteid" TYPE')
        tmp, root = make_plugin({'db/install.xml': install, 'version.php': "<?php\n$plugin->component = 'local_demo';\n"})
        with tmp:
            self.assertEqual(set(), self.found(root))

    def test_the_course_column_of_an_activity_is_not_course_keyed(self):
        tmp, root = make_plugin({
            'db/install.xml': INSTALL, 'version.php': VERSION,
            'lib.php': "<?php\n$DB->delete_records('demo', ['id' => 1]);\n"
                       "$DB->delete_records('demo_items', ['id' => 1]);\n"})
        with tmp:
            self.assertEqual(set(), self.found(root))


FORM = """<?php
class demo_form extends moodleform {
    public function definition() {
        $mform = $this->_form;
%s
    }
}
"""


class ColumnLengthsTest(unittest.TestCase):
    def found(self, body, extra=None):
        files = {'db/install.xml': INSTALL, 'version.php': VERSION, 'mod_form.php': FORM % body}
        files.update(extra or {})
        tmp, root = make_plugin(files)
        with tmp:
            return [(v['kind'], v.get('field') or v['file'], v['limit'])
                    for v in schema.column_lengths(root)]

    def test_a_name_without_maxlength_is_found(self):
        body = "$mform->addElement('text', 'name', 'Name');\n$mform->setType('name', PARAM_TEXT);"
        self.assertEqual([('form', 'name', 255)], self.found(body))

    def test_a_maxlength_rule_at_the_column_length_passes(self):
        body = ("$mform->addElement('text', 'name', 'Name');\n"
                "$mform->addRule('name', null, 'maxlength', 255, 'client');")
        self.assertEqual([], self.found(body))

    def test_a_maxlength_above_the_column_is_found(self):
        body = ("$mform->addElement('text', 'name', 'Name');\n"
                "$mform->addRule('name', null, 'maxlength', 300, 'client');")
        self.assertEqual([('form', 'name', 255)], self.found(body))

    def test_a_maxlength_attribute_on_the_element_passes(self):
        body = "$mform->addElement('text', 'label', 'Label', ['maxlength' => 100]);"
        self.assertEqual([], self.found(body))

    def test_a_name_shared_by_columns_of_different_length_is_judged_against_the_longest(self):
        install = INSTALL.replace('NAME="label" TYPE="char" LENGTH="100"', 'NAME="name" TYPE="char" LENGTH="100"')
        body = ("$mform->addElement('text', 'name', 'Name');\n"
                "$mform->addRule('name', null, 'maxlength', 255, 'client');")
        tmp, root = make_plugin({'db/install.xml': install, 'mod_form.php': FORM % body})
        with tmp:
            self.assertEqual([], schema.column_lengths(root))
        tmp, root = make_plugin({'db/install.xml': install, 'mod_form.php': FORM % body.split('\n')[0]})
        with tmp:
            self.assertEqual([('form', 'name', '100-255')],
                             [(v['kind'], v['field'], v['limit']) for v in schema.column_lengths(root)])

    def test_a_field_that_is_not_a_char_column_is_ignored(self):
        self.assertEqual([], self.found("$mform->addElement('text', 'whatever', 'Other');"))

    def test_the_config_prefix_of_a_block_field_is_dropped(self):
        body = "$mform->addElement('text', 'config_name', 'Name');"
        self.assertEqual([('form', 'config_name', 255)], self.found(body))

    def test_a_field_named_by_a_loop_is_matched_by_its_prefix(self):
        install = INSTALL.replace('NAME="extra" TYPE="text"', 'NAME="emoji1" TYPE="char" LENGTH="255"')
        body = ("for ($tier = 1; $tier <= 5; $tier++) {\n"
                "    $mform->addElement('text', 'emoji' . $tier, 'Emoji');\n}")
        tmp, root = make_plugin({'db/install.xml': install, 'mod_form.php': FORM % body})
        with tmp:
            self.assertEqual([('form', 'emoji', 255)],
                             [(v['kind'], v['field'], v['limit']) for v in schema.column_lengths(root)])
        guarded = body.replace("}\n", "") + ("\n    $mform->addRule('emoji' . $tier, null, "
                                              "'maxlength', 255, 'client');\n}")
        tmp, root = make_plugin({'db/install.xml': install, 'mod_form.php': FORM % guarded})
        with tmp:
            self.assertEqual([], schema.column_lengths(root))

    def test_substr_cut_at_a_column_length_is_found_but_core_text_is_not(self):
        code = ("<?php\n$a = substr($text, 0, 255);\n$b = core_text::substr($text, 0, 255);\n"
                "$c = substr($text, 0, 7);\n")
        self.assertEqual([('substr', 'lib.php', 255)], self.found('', {'lib.php': code}))

    def test_an_accepted_file_is_skipped(self):
        files = {'db/install.xml': INSTALL, 'lib.php': "<?php\n$a = substr($text, 0, 255);\n",
                 schema.ACCEPTED_REL: json.dumps({'column_lengths': {'lib.php': 'binary path'}})}
        tmp, root = make_plugin(files)
        with tmp:
            self.assertEqual([], schema.column_lengths(root))


class UserPreferencesTest(unittest.TestCase):
    def found(self, provider, code):
        tmp, root = make_plugin({'lib.php': code, 'classes/privacy/provider.php': provider})
        with tmp:
            return [v['name'] for v in schema.user_preferences(root)]

    def test_an_undeclared_preference_is_found(self):
        code = "<?php\nset_user_preference('block_demo_celebration', 1, $USER);\n"
        self.assertEqual(['block_demo_celebration'], self.found('<?php // nothing', code))

    def test_a_declared_preference_passes(self):
        code = "<?php\nset_user_preference('block_demo_celebration', 1, $USER);\n"
        provider = "<?php\n$c->add_user_preference('block_demo_celebration', 'privacy:pref');\n"
        self.assertEqual([], self.found(provider, code))

    def test_a_dynamic_name_is_covered_by_its_declared_prefix(self):
        code = "<?php\nset_user_preference('block_demo_avatar_' . $id, 1);\n"
        provider = "<?php\n$sql = $DB->sql_like('name', ':p');\n$p = 'block_demo_avatar_';\n"
        self.assertEqual([], self.found(provider, code))
        self.assertEqual(['block_demo_avatar_'], self.found('<?php // none', code))

    def test_a_preference_read_only_is_not_reported(self):
        self.assertEqual([], self.found('<?php', "<?php\n$x = get_user_preferences('block_demo_x');\n"))


class WeakRandomTest(unittest.TestCase):
    def found(self, code):
        tmp, root = make_plugin({'lib.php': code})
        with tmp:
            return [(v['function'], v['line']) for v in schema.weak_random(root)]

    def test_a_token_from_uniqid_is_found(self):
        self.assertEqual([('uniqid', 2)], self.found("<?php\n$token = md5(uniqid());\n"))

    def test_a_hashed_uniqid_is_found_even_under_a_plain_name(self):
        self.assertEqual([('uniqid', 2)], self.found("<?php\n$value = sha1(uniqid('', true));\n"))

    def test_an_array_key_named_code_is_found(self):
        self.assertEqual([('mt_rand', 2)], self.found("<?php\n$r = ['code' => mt_rand(100, 999)];\n"))

    def test_gameplay_randomness_is_not_found(self):
        self.assertEqual([], self.found("<?php\n$pick = rand(0, 3);\n$roll = mt_rand(1, 6);\n"))

    def test_a_secure_generator_is_not_found(self):
        self.assertEqual([], self.found("<?php\n$token = random_string(32);\n"))



class AuditIntegrationTest(unittest.TestCase):
    def test_every_violation_kind_is_reported_as_one_finding_with_its_rule(self):
        files = with_backup()
        files['lib.php'] = "<?php\n$DB->delete_records('demo', ['id' => 1]);\n$a = substr($t, 0, 255);\n"
        tmp, root = make_plugin(files)
        with tmp:
            found = {f['rule_id']: f for f in audit_checks.check_schema(root, [])}
        self.assertEqual({'L2-BAK-1', 'L2-DEL-1', 'L4-BUG-1'}, set(found))
        backup = found['L2-BAK-1']
        self.assertEqual('static', backup['source'])
        self.assertEqual(2, 1 + len(backup['extra_locations']))
        self.assertIn('demo.completionsubmit', backup['description'])

    def test_a_clean_plugin_has_no_findings(self):
        tmp, root = make_plugin({'db/install.xml': INSTALL, 'version.php': VERSION,
                                 'lib.php': "<?php\n$DB->delete_records('demo', ['id' => 1]);\n"
                                            "$DB->delete_records('demo_items', ['demoid' => 1]);\n"})
        with tmp:
            self.assertEqual([], audit_checks.check_schema(root, []))

    def test_a_direct_write_to_block_instances_is_a_core_table_write(self):
        tmp, root = make_plugin({'classes/keys.php': "<?php\n$DB->update_record('block_instances', $row);\n"})
        with tmp:
            found = audit_checks.check_core_table_writes(root, [{'rel': 'classes/keys.php', 'lines': 2}])
        self.assertEqual(['L4-API-1'], [f['rule_id'] for f in found])

if __name__ == '__main__':
    unittest.main()
