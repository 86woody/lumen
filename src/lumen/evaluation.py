"""Manifest gates fail closed. Unit tests never establish installed-client support."""
import hashlib
import io
import json
import platform
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import unittest

from .model import digest, require
from .recovery import atomic_json
from .security import CONFIG

PRODUCT_CASES = {f"R-{i:02d}" for i in range(1, 19)} | {"clean-install", "three-platforms", "pilot-week", "fixture-calibration"}
COMPARISON_CASES = {"release-1", "bm25-reference", "judge-calibration", "forgetting", "monorepo-comparison",
                    "longmemeval-s", "longmemeval-m", "beam-10m", "longmemeval-v2", "coding-study",
                    "decay", "ablations", "lifecycle-cost", "reproduction", "permitted-raw-outputs"}

CLIENT_NAMES = ('claude-code', 'codex-cli', 'copilot-cli')


def client_acceptance_cases(client):
    """Fixed product cases; a lock-file label cannot manufacture execution."""
    prefix = 'test_installed_clients.InstalledClientTests.test_'
    names = {name: name.replace('-', '_') for name in CLIENT_NAMES}
    cases = {prefix + names[a] + '_to_' + names[b]: {a, b}
             for a in CLIENT_NAMES for b in CLIENT_NAMES if a != b and client in {a, b}}
    for suffix in ('hook_disabled', 'duplicate_hooks'):
        cases[prefix + names[client] + '_' + suffix] = {client}
    return cases


def runtime_identity():
    root = Path(__file__).parent
    files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.glob("*.py"))}
    return {"runtime_digest": digest(files), "files": files, "configuration_digest": digest(CONFIG),
            "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
            "platform": platform.platform(), "machine": platform.machine(), "dependencies": []}


class RecordingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cases = {}

    def addSuccess(self, test):
        super().addSuccess(test)
        self.cases[test.id()] = {"status": "passed"}
        versions = getattr(test, 'installed_client_versions', None)
        if isinstance(versions, dict):
            self.cases[test.id()]['installed_client_versions'] = dict(versions)
        evidence = getattr(test, 'native_evidence', None)
        if isinstance(evidence, dict):
            self.cases[test.id()]['native_evidence'] = dict(evidence)

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.cases[test.id()] = {"status": "failed", "reason": self._exc_info_to_string(err, test)}

    def addError(self, test, err):
        super().addError(test, err)
        self.cases[test.id()] = {"status": "failed", "reason": self._exc_info_to_string(err, test)}

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.cases[test.id()] = {"status": "pending", "reason": reason}


def local_tests(pattern="test_*.py"):
    root = Path.cwd() / "tests"
    require(root.is_dir(), "Reproduction kit tests directory missing")
    suite = unittest.defaultTestLoader.discover(str(root), pattern=pattern)
    stream = io.StringIO()
    run = unittest.TextTestRunner(stream=stream, verbosity=2, resultclass=RecordingResult).run(suite)
    return {"passed": run.wasSuccessful() and not run.skipped and run.testsRun > 0,
            "cases": run.cases, "test_count": run.testsRun, "transcript": stream.getvalue()}


def native_tests(configuration=None, output=None, clients=None):
    required = {case for client in CLIENT_NAMES for case in client_acceptance_cases(client)}
    if configuration is None:
        return {'passed': False, 'test_count': 0, 'cases': {case: {'status': 'pending',
            'reason': 'Explicit native subscription configuration not supplied'} for case in sorted(required)}}
    require(output is not None and not output.exists(), 'Native output must be a new directory')
    require(configuration.is_file(), 'Native configuration missing')
    require(clients is not None and set(clients.get('clients', {})) == set(CLIENT_NAMES),
            'All three locked client identities required before native execution')
    root = Path.cwd() / 'acceptance'
    require(root.is_dir(), 'Installed-client acceptance sources missing')
    suite = unittest.TestLoader().discover(str(root), pattern='test_installed_clients.py')

    def configure(group):
        for test in group:
            if isinstance(test, unittest.TestSuite):
                configure(test)
            else:
                test.native_configuration = configuration.resolve()
                test.native_output = output.resolve()
                test.native_client_lock = clients['clients']
    configure(suite)
    output.mkdir(parents=True)
    stream = io.StringIO()
    run = unittest.TextTestRunner(stream=stream, verbosity=2, resultclass=RecordingResult).run(suite)
    for case in required - run.cases.keys():
        run.cases[case] = {'status': 'pending', 'reason': 'Required native test did not execute'}
    return {'passed': run.wasSuccessful() and not run.skipped and required <= run.cases.keys()
            and all(run.cases[case]['status'] == 'passed' for case in required),
            'cases': run.cases, 'test_count': run.testsRun, 'transcript': stream.getvalue()}


def gate_manifest(manifest, clients, local):
    require(manifest.get("schema") == 1 and manifest.get("release") in {1, 2}, "Unsupported manifest")
    require(manifest.get("max_cost_usd") == 0 and manifest.get("capabilities") == ["lexical"], "Noncompliant evaluation configuration")
    required = PRODUCT_CASES if manifest["release"] == 1 else COMPARISON_CASES
    require(set(manifest["cases"]) == required, "Missing or unexpected required release cases")
    cases = {}
    for name, definition in manifest["cases"].items():
        tests = definition.get("tests", [])
        failed = [t for t in tests if local["cases"].get(t, {}).get("status") == "failed"]
        missing = [t for t in tests if local["cases"].get(t, {}).get("status") not in {"passed", "failed"}]
        # External evidence is not accepted by the mere existence of a report.
        remaining = definition.get("remaining", [])
        passed = bool(tests) and not failed and not missing and not remaining
        cases[name] = {"status": "failed" if failed else "passed" if passed else "pending", "tested": tests,
                       "failed_tests": failed, "missing_tests": missing,
                       "remaining": remaining or ([] if tests else ["No executable acceptance evidence"])}
    expected_clients = set(CLIENT_NAMES)
    if clients is None or set(clients.get("clients", {})) != expected_clients:
        cases["client-lock"] = {"status": "pending", "remaining": ["All three exact installed editions required"]}
    else:
        for name, client in clients["clients"].items():
            required_tests = client_acceptance_cases(name)
            failed_tests, missing_tests, version_mismatches = [], [], []
            for test, involved in required_tests.items():
                observed = local['cases'].get(test, {})
                if observed.get('status') == 'failed':
                    failed_tests.append(test)
                elif observed.get('status') != 'passed':
                    missing_tests.append(test)
                else:
                    versions = observed.get('installed_client_versions', {})
                    if any(not clients['clients'][host].get('version') or
                           versions.get(host) != clients['clients'][host]['version'] for host in involved):
                        version_mismatches.append(test)
            failed = client.get('acceptance') == 'failed' or bool(failed_tests)
            passed = (client.get('installed') is True and client.get('acceptance') == 'passed'
                      and not failed_tests and not missing_tests and not version_mismatches)
            cases['client:' + name] = {'status': 'failed' if failed else 'passed' if passed else 'pending',
                'tested': sorted(required_tests), 'failed_tests': failed_tests,
                'missing_tests': missing_tests, 'version_mismatches': version_mismatches,
                'remaining': [] if passed else ['Installed handoffs, hook fallback and deduplication must execute at locked versions']}
    return {"passed": all(c["status"] == "passed" for c in cases.values()), "cases": cases}


def bench(args):
    require(args.max_cost_usd == 0, "Only zero additional spend is authorized")
    start = time.time_ns()
    identity = runtime_identity()
    revision = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    report = {"schema": 1, "suite": args.suite, "started_ns": start, "identity": identity,
              "code_revision": revision, "additional_cost_usd": 0, "models": "disabled",
              "command": sys.argv, "limitations": ["Local tests do not establish installed-client or pilot acceptance"]}
    patterns = {"unit": "test_*.py", "fixture": "test_*.py", "calibrate": "test_core.py", "monorepo": "test_workspace.py", "poison": "test_security*.py"}
    if args.suite == "release":
        require(args.manifest is not None, "Release manifest required")
        manifest = json.loads(args.manifest.read_text())
        clients = json.loads(args.clients.read_text()) if args.clients and args.clients.exists() else None
        local = local_tests()
        native = native_tests(getattr(args, 'native_config', None), getattr(args, 'native_output', None), clients)
        combined = {**local, 'cases': {**local['cases'], **native['cases']}}
        report.update(gate_manifest(manifest, clients, combined))
        report['native'] = native
        if getattr(args, 'native_config', None) is not None:
            report['models'] = 'explicit subscription acceptance clients; product model features disabled'
        report.update(manifest_digest=digest(manifest), clients_digest=digest(clients), local=local)
    elif args.suite == 'cross-agent':
        clients = json.loads(args.clients.read_text()) if args.clients and args.clients.exists() else None
        native = native_tests(getattr(args, 'native_config', None), getattr(args, 'native_output', None), clients)
        report.update(native)
        client_gate = gate_manifest({'schema': 1, 'release': 1, 'max_cost_usd': 0,
            'capabilities': ['lexical'], 'cases': {name: {'tests': []} for name in PRODUCT_CASES}}, clients, native)
        client_cases = {name: result for name, result in client_gate['cases'].items() if name.startswith('client')}
        report['cases'].update(client_cases)
        report['passed'] = native['passed'] and all(case['status'] == 'passed' for case in client_cases.values())
        if getattr(args, 'native_config', None) is not None:
            report['models'] = 'explicit subscription acceptance clients; product model features disabled'
    elif args.suite in patterns:
        report.update(local_tests(patterns[args.suite]))
        report["component_tests_passed"] = report["passed"]
        if args.suite != "unit":
            # These are specification phase gates, not synonyms for any passing subset.
            report["passed"] = False
            report["remaining_acceptance"] = ["Full phase acceptance is pending; see release manifest and requirements matrix"]
        if args.suite == "calibrate":
            report["reference_replication"] = "pending: pinned LongMemEval reference not executed"
    else:
        report.update(passed=False, cases={args.suite: {"status": "pending", "remaining": ["Suite acceptance not implemented"]}})
    report["finished_ns"] = time.time_ns()
    report["runtime_unchanged"] = identity == runtime_identity()
    if not report["runtime_unchanged"]:
        report["passed"] = False
        report["limitations"].append("Runtime changed during execution; evidence invalidated")
    report["elapsed_ms"] = (report["finished_ns"] - start) // 1000000
    report["exit_code"] = 0 if report["passed"] else 1
    atomic_json(args.output, report)
    return report
