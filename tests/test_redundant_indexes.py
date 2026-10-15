"""Tests for check_redundant_indexes: an INDEX that repeats the fields of a KEY of the same table.

Run with:  python3 -m unittest discover -s tests
"""

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import check_redundant_indexes as checker  # noqa: E402

HEADER = '<?xml version="1.0" encoding="UTF-8" ?>\n<XMLDB PATH="mod/demo/db" VERSION="20261015">\n<TABLES>\n'
FOOTER = '</TABLES>\n</XMLDB>\n'


def table(name, keys, indexes):
    """A TABLE element with the given KEY and INDEX lines (fields are not needed by the check)."""
    body = f'<TABLE NAME="{name}">\n<FIELDS>\n<FIELD NAME="id" TYPE="int" LENGTH="10"/>\n</FIELDS>\n'
    body += '<KEYS>\n' + ''.join(keys) + '</KEYS>\n'
    if indexes:
        body += '<INDEXES>\n' + ''.join(indexes) + '</INDEXES>\n'
    return body + '</TABLE>\n'


def key(name, keytype, fields):
    return f'<KEY NAME="{name}" TYPE="{keytype}" FIELDS="{fields}"/>\n'


def index(name, fields, unique='false'):
    return f'<INDEX NAME="{name}" UNIQUE="{unique}" FIELDS="{fields}"/>\n'


class RedundantIndexesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'install.xml'

    def tearDown(self):
        self.tmp.cleanup()

    def check(self, *tables):
        self.path.write_text(HEADER + ''.join(tables) + FOOTER)
        return checker.check_install_xml(self.path)

    def test_index_repeating_a_foreign_key_is_reported_with_its_line(self):
        problems = self.check(table('demo', [key('primary', 'primary', 'id'), key('fk_course', 'foreign', 'course')],
                                    [index('course_ix', 'course')]))
        self.assertEqual(1, len(problems))
        line = int(problems[0].split(':')[1])
        self.assertIn('<INDEX NAME="course_ix"', self.path.read_text().splitlines()[line - 1])
        self.assertIn("'course_ix'", problems[0])
        self.assertIn("'fk_course' (foreign)", problems[0])
        self.assertIn('remova o INDEX', problems[0])

    def test_index_on_a_field_without_key_is_clean(self):
        self.assertEqual([], self.check(table('demo', [key('primary', 'primary', 'id')],
                                              [index('timecreated_ix', 'timecreated')])))

    def test_composite_index_in_a_different_order_is_clean(self):
        self.assertEqual([], self.check(table('demo', [key('fk_pair', 'foreign', 'a, b')],
                                              [index('ba_ix', 'b, a')])))

    def test_composite_index_in_the_same_order_is_reported(self):
        problems = self.check(table('demo', [key('fk_pair', 'foreign', 'a, b')], [index('ab_ix', 'a,b')]))
        self.assertEqual(1, len(problems))

    def test_unique_index_on_a_foreign_key_suggests_foreign_unique(self):
        problems = self.check(table('demo', [key('fk_user', 'foreign', 'userid')],
                                    [index('userid_uix', 'userid', unique='true')]))
        self.assertEqual(1, len(problems))
        self.assertIn('foreign-unique', problems[0])

    def test_index_repeating_the_primary_key_is_reported(self):
        problems = self.check(table('demo', [key('primary', 'primary', 'id')], [index('id_ix', 'id')]))
        self.assertEqual(1, len(problems))

    def test_line_points_at_the_index_of_the_right_table(self):
        first = table('one', [key('primary', 'primary', 'id')], [index('idx_userid', 'userid')])
        second = table('two', [key('fk_user', 'foreign', 'userid')], [index('idx_userid', 'userid')])
        problems = self.check(first, second)
        self.assertEqual(1, len(problems))
        self.assertIn("tabela 'two'", problems[0])
        source = self.path.read_text().splitlines()
        line = int(problems[0].split(':')[1])
        self.assertIn('idx_userid', source[line - 1])
        self.assertGreater(line, source.index('<TABLE NAME="two">'))

    def test_malformed_xml_is_left_to_check_schema(self):
        self.path.write_text('<XMLDB><TABLES>')
        self.assertEqual([], checker.check_install_xml(self.path))

    def quiet_main(self, argv):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return checker.main(argv)

    def test_main_exit_codes(self):
        self.check(table('demo', [key('fk_course', 'foreign', 'course')], [index('course_ix', 'course')]))
        self.assertEqual(1, self.quiet_main(['check_redundant_indexes.py', str(self.path)]))
        self.check(table('demo', [key('fk_course', 'foreign', 'course')], []))
        self.assertEqual(0, self.quiet_main(['check_redundant_indexes.py', str(self.path)]))


if __name__ == '__main__':
    unittest.main()
