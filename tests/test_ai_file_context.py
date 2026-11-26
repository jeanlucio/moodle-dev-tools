"""Tests for ai_file_context: the unchanged lines given to the AI review next to the diff.

Run with:  python3 -m unittest discover -s tests
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ai_file_context as context  # noqa: E402

PHP = """<?php
namespace format_demo\\local;

use cm_info;
use core_availability\\info;

class builder {
    use some_trait;
}
"""

CSS = """/**
 * This file is part of Moodle. See the GNU General Public License.
 */
/**
 * Styles for the demo filter.
 */
/* The filter renders on pages it does not own, so no .path-* body class applies. */
div.demo-widget {
    display: flex;
}

/* An unrelated note about spacing. */
div.demo-widget p {
    margin: 0;
}

/* Not path-class scoped: this iframe is injected into the course page. */
iframe.demo-video {
    width: 100%;
}
"""


class ContextLinesTest(unittest.TestCase):
    """What each file type contributes."""

    def test_php_gives_top_level_namespace_and_use_only(self):
        lines = context.context_lines('classes/local/builder.php', PHP)
        self.assertEqual(
            [(2, 'namespace format_demo\\local;'), (4, 'use cm_info;'), (5, 'use core_availability\\info;')],
            lines,
        )

    def test_javascript_gives_imports(self):
        source = "import Ajax from 'core/ajax';\nimport {getString} from 'core/str';\nconst a = 1;\n"
        self.assertEqual(2, len(context.context_lines('amd/src/demo.js', source)))

    def test_css_gives_file_level_and_scoping_comments_without_the_licence(self):
        text = '\n'.join(line for _, line in context.context_lines('styles.css', CSS))
        self.assertIn('Styles for the demo filter.', text)
        self.assertIn('renders on pages it does not own', text)
        self.assertIn('injected into the course page', text)
        self.assertNotIn('unrelated note', text)
        self.assertNotIn('General Public License', text)
        self.assertNotIn('display: flex', text)

    def test_css_line_numbers_point_into_the_file(self):
        lines = context.context_lines('styles.css', CSS)
        number = next(number for number, text in lines if 'does not own' in text)
        self.assertEqual(7, number)

    def test_other_file_types_give_nothing(self):
        self.assertEqual([], context.context_lines('templates/demo.mustache', '<p>{{a}}</p>'))


class BuildContextTest(unittest.TestCase):
    """The block built from a real repository."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = self.folder.name
        subprocess.run(['git', 'init', '-q', self.root], check=True)

    def stage(self, name, source):
        (Path(self.root) / name).write_text(source, encoding='utf-8')
        subprocess.run(['git', '-C', self.root, 'add', name], check=True)

    def test_reads_the_staged_version_not_the_working_copy(self):
        self.stage('builder.php', PHP)
        (Path(self.root) / 'builder.php').write_text('<?php\nuse other;\n', encoding='utf-8')
        block = context.build_context(self.root, ['builder.php'])
        self.assertIn('--- builder.php', block)
        self.assertIn('L5: use core_availability\\info;', block)
        self.assertNotIn('use other;', block)

    def test_a_file_with_nothing_to_show_and_a_missing_one_are_left_out(self):
        self.stage('plain.php', '<?php\necho 1;\n')
        self.assertEqual('', context.build_context(self.root, ['plain.php', 'absent.php']))

    def test_a_long_list_is_cut(self):
        self.stage('many.php', '<?php\n' + ''.join(f'use a\\b{i};\n' for i in range(80)))
        block = context.build_context(self.root, ['many.php'])
        self.assertIn('(+20 more lines not shown)', block)


if __name__ == '__main__':
    unittest.main()
