import copy
import json
from pathlib import Path
import unittest

from lumen.evaluation import gate_manifest, client_acceptance_cases, native_tests
from lumen.model import LumenError


class GateTests(unittest.TestCase):
    def test_native_acceptance_requires_explicit_configuration_and_client_lock(self):
        from unittest.mock import patch
        import tempfile
        with patch('unittest.TestLoader.discover', side_effect=AssertionError('Native discovery must not run')):
            result = native_tests()
        self.assertFalse(result['passed'])
        self.assertEqual(result['test_count'], 0)
        self.assertEqual(len(result['cases']), 12)
        self.assertTrue(all(case['status'] == 'pending' for case in result['cases'].values()))
        with tempfile.TemporaryDirectory() as td:
            configuration = Path(td) / 'config.json'
            configuration.write_text('{}')
            with self.assertRaises(LumenError):
                native_tests(configuration, Path(td) / 'out', None)
            self.assertFalse((Path(td) / 'out').exists())

    def test_client_status_labels_and_versionless_results_cannot_pass(self):
        manifest = json.loads(Path('evals/release-1.json').read_text())
        clients = json.loads(Path('evals/clients.lock.json').read_text())
        for client in clients['clients'].values():
            client.update(installed=True, acceptance='passed')
        local = {'cases': {}}
        result = gate_manifest(manifest, clients, local)
        for name in clients['clients']:
            self.assertEqual(result['cases']['client:' + name]['status'], 'pending')
            self.assertEqual(len(result['cases']['client:' + name]['missing_tests']), 6)
            for test in client_acceptance_cases(name):
                local['cases'][test] = {'status': 'passed'}
        result = gate_manifest(manifest, clients, local)
        self.assertTrue(all(result['cases']['client:' + name]['version_mismatches'] for name in clients['clients']))
        test = next(iter(client_acceptance_cases('claude-code')))
        local['cases'][test] = {'status': 'failed'}
        result = gate_manifest(manifest, clients, local)
        self.assertEqual(result['cases']['client:claude-code']['status'], 'failed')
        self.assertFalse(result['passed'])

    def test_failed_requirements_remain_failed(self):
        manifest = json.loads(Path("evals/release-1.json").read_text())
        test = manifest["cases"]["R-01"]["tests"][0]
        result = gate_manifest(manifest, None, {"cases": {test: {"status": "failed"}}})
        self.assertEqual(result["cases"]["R-01"]["status"], "failed")
        self.assertEqual(result["cases"]["R-01"]["failed_tests"], [test])
        self.assertNotIn(test, result["cases"]["R-01"]["missing_tests"])
        self.assertEqual(result["cases"]["pilot-week"]["status"], "pending")

    def test_missing_acceptance_cannot_pass(self):
        manifest = json.loads(Path("evals/release-1.json").read_text())
        result = gate_manifest(manifest, None, {"cases": {}})
        self.assertFalse(result["passed"])
        self.assertEqual(result["cases"]["pilot-week"]["status"], "pending")
        manifest["cases"].pop("R-10")
        with self.assertRaises(LumenError):
            gate_manifest(manifest, None, {"cases": {}})

    def test_paid_or_opaque_evaluation_rejected(self):
        manifest = json.loads(Path("evals/release-2.json").read_text())
        manifest["capabilities"] = ["semantic-search"]
        with self.assertRaises(LumenError):
            gate_manifest(manifest, None, {"cases": {}})
