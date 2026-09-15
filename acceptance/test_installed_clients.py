"""Actual native acceptance; never discovered by the offline component runner."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest

from lumen.evaluation import runtime_identity


class InstalledClientTests(unittest.TestCase):
    def handoff(self, origin, peer):
        configuration = getattr(self, 'native_configuration', None)
        if configuration is None:
            self.skipTest('Explicit native configuration required; no inference attempted')
        config = json.loads(configuration.read_text(encoding='utf-8-sig'))
        for client in (origin, peer):
            observation = json.loads(Path(config[client]['coverage']).read_text(encoding='utf-8-sig'))
            self.assertEqual(observation['version'], self.native_client_lock[client]['version'],
                             'Coverage and release client lock differ; no inference attempted')
        output = self.native_output / (origin + '-to-' + peer)
        before = runtime_identity()
        driver = Path.cwd() / 'tools/native-handoff-check.py'
        adapters = [driver, driver.with_name('native_clients.py')]
        hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in adapters}
        command = [sys.executable, str(driver), '--from-client', origin, '--to-client', peer,
                   '--configuration', str(configuration), '--output', str(output)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=1500)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        raw = (output / 'report.json').read_bytes()
        report = json.loads(raw)
        self.assertTrue(report['passed'], report.get('failure'))
        self.assertTrue(report['runtime_unchanged'])
        self.assertEqual(report['runtime'], before)
        self.assertEqual(runtime_identity(), before)
        self.assertEqual(report['driver_hashes'], hashes)
        self.assertEqual(hashes, {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in adapters})
        self.assertEqual(report['configuration_sha256'], hashlib.sha256(configuration.read_bytes()).hexdigest())
        self.assertEqual(len(report['runs']), 6)
        self.installed_client_versions = report['installed_client_versions']
        for client in (origin, peer):
            self.assertEqual(self.installed_client_versions[client], self.native_client_lock[client]['version'])
        self.native_evidence = {'report': str(output / 'report.json'),
                                'sha256': hashlib.sha256(raw).hexdigest(), 'driver_hashes': hashes}

    def test_claude_code_to_codex_cli(self):
        self.handoff('claude-code', 'codex-cli')

    def test_codex_cli_to_claude_code(self):
        self.handoff('codex-cli', 'claude-code')

    def test_claude_code_to_copilot_cli(self):
        self.handoff('claude-code', 'copilot-cli')

    def test_codex_cli_to_copilot_cli(self):
        self.handoff('codex-cli', 'copilot-cli')

    def test_copilot_cli_to_claude_code(self):
        self.handoff('copilot-cli', 'claude-code')

    def test_copilot_cli_to_codex_cli(self):
        self.handoff('copilot-cli', 'codex-cli')

    def fallback(self, client):
        configuration = getattr(self, 'native_configuration', None)
        if configuration is None:
            self.skipTest('Explicit native configuration required; no inference attempted')
        config = json.loads(configuration.read_text(encoding='utf-8-sig'))
        observation = json.loads(Path(config[client]['coverage']).read_text(encoding='utf-8-sig'))
        self.assertEqual(observation['version'], self.native_client_lock[client]['version'],
                         'Coverage and release client lock differ; no inference attempted')
        output = self.native_output / (client + '-hook-disabled')
        before = runtime_identity()
        driver = Path.cwd() / ('tools/' + {'claude-code': 'claude-fallback-check.py', 'copilot-cli': 'copilot-fallback-check.py'}[client])
        adapters = [driver, driver.with_name('native_clients.py')]
        hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in adapters}
        command = [sys.executable, str(driver), '--configuration', str(configuration), '--output', str(output)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=900)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        raw = (output / 'report.json').read_bytes()
        report = json.loads(raw)
        self.assertTrue(report['passed'], report.get('failure'))
        self.assertTrue(report['runtime_unchanged'] and report['drivers_unchanged'])
        self.assertEqual(report['runtime'], before)
        self.assertEqual(runtime_identity(), before)
        self.assertEqual(report['driver_hashes'], hashes)
        self.assertEqual(hashes, {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in adapters})
        self.assertEqual(report['configuration_sha256'], hashlib.sha256(configuration.read_bytes()).hexdigest())
        self.assertEqual([run['label'] for run in report['runs']], ['loaded', 'fallback', 'control'])
        self.assertEqual(report['launch_from'], 'root')
        self.assertEqual(report['runs'][1]['hook_events'], 0)
        self.installed_client_versions = report['installed_client_versions']
        self.assertEqual(self.installed_client_versions[client], self.native_client_lock[client]['version'])
        self.native_evidence = {'report': str(output / 'report.json'),
                                'sha256': hashlib.sha256(raw).hexdigest(), 'driver_hashes': hashes}

    def test_claude_code_hook_disabled(self):
        self.fallback('claude-code')

    def test_codex_cli_hook_disabled(self):
        self.skipTest('Actual instruction-file fallback protocol remains unimplemented')

    def test_copilot_cli_hook_disabled(self):
        self.fallback('copilot-cli')

    def duplicate_hooks(self, client):
        configuration = getattr(self, 'native_configuration', None)
        if configuration is None:
            self.skipTest('Explicit native configuration required; no inference attempted')
        config = json.loads(configuration.read_text(encoding='utf-8-sig'))
        observation = json.loads(Path(config[client]['coverage']).read_text(encoding='utf-8-sig'))
        self.assertEqual(observation['version'], self.native_client_lock[client]['version'],
                         'Coverage and release client lock differ; no inference attempted')
        output = self.native_output / (client + '-duplicate-hooks')
        before = runtime_identity()
        driver = Path.cwd() / ('tools/' + {'claude-code': 'claude-capture-check.py', 'copilot-cli': 'copilot-capture-check.py'}[client])
        adapters = [driver, driver.with_name('native_clients.py')]
        hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in adapters}
        hook = Path.cwd() / 'src/lumen/hook.py'
        hook_digest = hashlib.sha256(hook.read_bytes()).hexdigest()
        command = [sys.executable, str(driver), '--configuration', str(configuration), '--output', str(output)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=900)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        raw = (output / 'report.json').read_bytes()
        report = json.loads(raw)
        self.assertTrue(report['passed'], report.get('failure'))
        self.assertTrue(report['runtime_unchanged'] and report['drivers_unchanged'] and report['hook_unchanged'])
        self.assertEqual(report['runtime'], before)
        self.assertEqual(runtime_identity(), before)
        self.assertEqual(report['driver_hashes'], hashes)
        self.assertEqual(hashes, {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in adapters})
        self.assertEqual(report['hook_sha256'], hook_digest)
        self.assertEqual(report['configuration_sha256'], hashlib.sha256(configuration.read_bytes()).hexdigest())
        self.assertEqual([run['label'] for run in report['runs']], ['capture', 'doubled'])
        if client == 'claude-code':
            self.assertEqual(report['runs'][1]['deliveries']['Stop']['count'], 2)
        else:
            # Copilot records one hook start per event however many entries ran; the ledger counts the second delivery.
            self.assertEqual(report['runs'][1]['deliveries']['agentStop']['count'], 1)
            self.assertEqual(report['doctor']['capture']['duplicate_deliveries'], 1)
        self.assertEqual(len(report['episodes']), 2)
        self.installed_client_versions = report['installed_client_versions']
        self.assertEqual(self.installed_client_versions[client], self.native_client_lock[client]['version'])
        self.native_evidence = {'report': str(output / 'report.json'),
                                'sha256': hashlib.sha256(raw).hexdigest(), 'driver_hashes': hashes, 'hook_sha256': hook_digest}

    def test_claude_code_duplicate_hooks(self):
        self.duplicate_hooks('claude-code')

    def test_codex_cli_duplicate_hooks(self):
        self.skipTest('Actual duplicate-hook delivery protocol remains unimplemented')

    def test_copilot_cli_duplicate_hooks(self):
        self.duplicate_hooks('copilot-cli')
