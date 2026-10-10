"""Tests for the \\curl transport check (rules L3-TLS-1 and L3-SSRF-1) and the discovery of
URL validators that audit_url_probe runs.

The fixtures are local_aihub's real client before (v1.3.3) and after the fix that an outside
review prompted: the check has to flag the first and stay quiet on the second.
Run with:  python3 -m unittest discover -s tests
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import audit_static_checks as checks  # noqa: E402
import audit_url_probe as probe  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / 'fixtures'


def in_plugin(content, rel='classes/local/client.php'):
    """Runs `fn(root, scan_files)` against a throwaway plugin holding one file."""
    def run(fn):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(content, encoding='utf-8')
            return fn(root, [{'rel': rel, 'lines': content.count('\n')}])
    return run


class CurlTransportTest(unittest.TestCase):
    def test_the_vulnerable_client_is_reported_for_tls_and_ssrf(self):
        content = (FIXTURES / 'aihub_client_v133.php').read_text(encoding='utf-8')
        findings = in_plugin(content)(checks.check_curl_transport)
        rules = sorted(f['rule_id'] for f in findings)
        self.assertEqual(['L3-SSRF-1', 'L3-TLS-1'], rules)
        ssrf = next(f for f in findings if f['rule_id'] == 'L3-SSRF-1')
        self.assertIn('CURLOPT_FOLLOWLOCATION', ssrf['description'])
        self.assertIn('CURLOPT_RESOLVE', ssrf['description'])

    def test_the_fixed_client_is_not_reported(self):
        content = (FIXTURES / 'aihub_client_fixed.php').read_text(encoding='utf-8')
        self.assertEqual([], in_plugin(content)(checks.check_curl_transport))

    def test_a_request_without_credentials_or_validation_is_left_alone(self):
        code = "<?php\n$c = new \\curl();\n$c->get('https://example.com/feed');\n"
        self.assertEqual([], in_plugin(code, 'lib.php')(checks.check_curl_transport))

    def test_a_credential_in_another_header_form_counts(self):
        code = ("<?php\n$c = new curl();\n$c->setHeader(['x-api-key: ' . $key]);\n"
                "$c->post($u, $b);\n")
        findings = in_plugin(code, 'lib.php')(checks.check_curl_transport)
        self.assertEqual(['L3-TLS-1'], [f['rule_id'] for f in findings])

    def test_the_options_in_a_comment_do_not_count(self):
        code = ("<?php\n$c = new \\curl();\n$c->setHeader(['Authorization: Bearer ' . $k]);\n"
                "// 'CURLOPT_SSL_VERIFYPEER' => 1 would fix this.\n$c->post($u, $b);\n")
        self.assertEqual(1, len(in_plugin(code, 'lib.php')(checks.check_curl_transport)))

    def test_tests_and_cli_are_not_production(self):
        code = "<?php\n$c = new \\curl();\n$c->setHeader(['Authorization: Bearer x']);\n"
        self.assertEqual([], in_plugin(code, 'tests/fixtures/x.php')(checks.check_curl_transport))


class FindUrlValidatorsTest(unittest.TestCase):
    def test_the_url_methods_of_a_validating_class_are_found(self):
        content = (FIXTURES / 'aihub_client_fixed.php').read_text(encoding='utf-8')
        found = in_plugin(content)(probe.find_url_validators)
        names = sorted(v['method'] for v in found)
        # String-returning methods (endpoint_problem, resolve_openai_url) cannot say
        # "accepted", and is_public_ip takes an IP, not a URL.
        self.assertIn('is_safe_url', names)
        self.assertIn('safe_addresses', names)
        self.assertNotIn('endpoint_problem', names)
        self.assertNotIn('resolve_openai_url', names)
        self.assertNotIn('is_public_ip', names)
        self.assertTrue(all(v['class'] == 'local_aihub\\local\\client' for v in found))

    def test_a_class_that_does_not_validate_addresses_is_ignored(self):
        code = ("<?php\nnamespace local_x;\nclass api {\n"
                "    public function fetch(string $url): bool {\n        return true;\n    }\n}\n")
        self.assertEqual([], in_plugin(code, 'classes/api.php')(probe.find_url_validators))


if __name__ == '__main__':
    unittest.main()
