"""Tests for check_license_header: the Moodle GPL header on Mustache, CSS and JS files.

Each case builds a throwaway plugin on disk. Run with:  python3 -m unittest discover -s tests
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import check_license_header as checker  # noqa: E402

MUSTACHE_WITH = """{{!
    This file is part of Moodle - https://moodle.org/

    Moodle is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
}}
{{!
    @template local_demo/page
}}
<p>Hi</p>
"""

MUSTACHE_WITHOUT = """{{!
    @template local_demo/page
}}
<p>Hi</p>
"""

CSS_WITH = """/**
 * This file is part of Moodle - https://moodle.org/
 *
 * Moodle is free software: you can redistribute it and/or modify
 */
.path-local-demo .x { color: red; }
"""

JS_WITH = """// This file is part of Moodle - https://moodle.org/
//
// Moodle is free software: you can redistribute it and/or modify
export const init = () => {};
"""

JS_WITHOUT = "export const init = () => {};\n"


class LicenseHeaderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.plugin = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, relative: str, content: str) -> str:
        path = self.plugin / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return relative

    def check(self, *files: str) -> list[str]:
        return checker.check_plugin(self.plugin, list(files))

    def test_files_with_the_header_pass(self):
        files = [
            self.write('templates/page.mustache', MUSTACHE_WITH),
            self.write('styles.css', CSS_WITH),
            self.write('amd/src/page.js', JS_WITH),
        ]
        self.assertEqual([], self.check(*files))

    def test_each_kind_without_the_header_is_reported(self):
        files = [
            self.write('templates/page.mustache', MUSTACHE_WITHOUT),
            self.write('styles.css', '.x { color: red; }\n'),
            self.write('amd/src/page.js', JS_WITHOUT),
        ]
        problems = self.check(*files)
        self.assertEqual(3, len(problems))
        for relative in files:
            self.assertTrue(any(p.startswith(relative + ':') for p in problems), relative)

    def test_a_header_far_below_the_top_does_not_count(self):
        body = '\n'.join(f'line {i}' for i in range(40)) + '\n' + MUSTACHE_WITH
        relative = self.write('templates/page.mustache', body)
        self.assertEqual(1, len(self.check(relative)))

    def test_only_half_of_the_header_is_not_enough(self):
        relative = self.write('amd/src/page.js', '// This file is part of Moodle - https://moodle.org/\n')
        self.assertEqual(1, len(self.check(relative)))

    def test_docs_minified_and_vendor_paths_are_skipped(self):
        files = [
            self.write('docs/assets/js/carousel.js', JS_WITHOUT),
            self.write('docs/assets/css/style.css', '.x {}\n'),
            self.write('javascript/phaser.min.js', JS_WITHOUT),
            self.write('yui/build/moodle-x-form/moodle-x-form-min.js', JS_WITHOUT),
            self.write('yui/build/moodle-x-form/moodle-x-form-debug.js', JS_WITHOUT),
            self.write('node_modules/lib/index.js', JS_WITHOUT),
        ]
        self.assertEqual([], self.check(*files))

    def test_third_party_locations_are_skipped(self):
        self.write('thirdpartylibs.xml', """<?xml version="1.0"?>
<libraries>
  <library>
    <location>javascript/lib</location>
  </library>
  <library>
    <location>pix/vendor.js</location>
  </library>
</libraries>
""")
        files = [
            self.write('javascript/lib/engine.js', JS_WITHOUT),
            self.write('pix/vendor.js', JS_WITHOUT),
        ]
        self.assertEqual([], self.check(*files))
        # A neighbour that only shares the prefix is not covered.
        other = self.write('javascript/library_other.js', JS_WITHOUT)
        self.assertEqual(1, len(self.check(other)))

    def test_a_file_that_no_longer_exists_is_ignored(self):
        self.assertEqual([], self.check('templates/gone.mustache'))


if __name__ == '__main__':
    unittest.main()
