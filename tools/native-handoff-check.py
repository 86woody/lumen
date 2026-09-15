"""Execute a real ordered handoff on installed native clients using synthetic data."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import lumen
from lumen.daemon import call, enroll
from lumen.evaluation import runtime_identity
from lumen.model import region
from lumen.workspace import Workspace
from native_clients import NativeClient, machine_environment, require


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--from-client', choices=['claude-code', 'codex-cli', 'copilot-cli'], required=True)
    parser.add_argument('--to-client', choices=['claude-code', 'codex-cli', 'copilot-cli'], required=True)
    parser.add_argument('--configuration', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    require(args.from_client != args.to_client, 'Handoff requires different clients')
    require(Path(lumen.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
            and 'site-packages' in Path(lumen.__file__).parts, 'Use a clean installed Lumen environment')
    config = json.loads(args.configuration.read_text(encoding='utf-8-sig'))
    args.output.mkdir(parents=True, exist_ok=False)
    root = Path(tempfile.mkdtemp(prefix='lumen-native-handoff-'))
    workspace_root, home = root / 'workspace', root / 'store'
    workspace_root.mkdir()
    source = b'Synthetic protocol: the nebula codec requires UTF-8. Corrected requirement: UTF-16.'
    (workspace_root / 'contract.txt').write_bytes(source)
    Workspace.initialize(workspace_root, [{'id': 'p', 'root': '.', 'kind': 'project',
        'purpose': 'synthetic handoff fixture', 'owners': ['fixture-owner'], 'depends_on': []}])
    workspace = Workspace(workspace_root, home)
    enroll(home, workspace.id, {'repo:p'}, project='p')
    report = {'schema': 1, 'started_ns': time.time_ns(), 'from': args.from_client, 'to': args.to_client,
              'runtime': runtime_identity(), 'fixture': str(root), 'runs': {}, 'passed': False,
              'release_acceptance': False, 'additional_spend_authorized_usd': 0,
              'driver_hashes': {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in (Path(__file__), Path(__file__).with_name('native_clients.py'))},
              'configuration_sha256': hashlib.sha256(args.configuration.read_bytes()).hexdigest()}
    daemon = subprocess.Popen([sys.executable, '-m', 'lumen', '--home', str(home), 'daemon',
                               '--workspace', str(workspace_root)], cwd=workspace_root,
                              env=machine_environment(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for attempt in range(100):
            try:
                require(call(home, 'doctor', {})['result']['healthy'], 'Unhealthy daemon')
                break
            except Exception:
                require(daemon.poll() is None and attempt < 99, 'Daemon failed to start')
                time.sleep(.05)
        clients = {name: NativeClient(name, config[name]['binary'], config[name]['coverage'],
                                     workspace_root, home, sys.executable, args.output)
                   for name in (args.from_client, args.to_client)}
        report['installed_client_versions'] = {name: clients[name].coverage['version'] for name in clients}

        def run(name, stage, prompt, expected_call):
            response = clients[name].run(stage, prompt)
            report['runs'][stage] = response
            require(any(c['name'] == expected_call for c in response['calls']),
                    'Native host did not call required tool: ' + stage)
            return response

        fields = dict(checkout=workspace.checkout, host=args.from_client, session='synthetic-1', turn='1',
            scope='repo:p', subject='nebula codec', relation='required_encoding', value='UTF-8',
            text='The nebula codec requires UTF-8.', region=region(start=0), citations=[{
                'kind': 'file', 'project': 'p', 'path': 'contract.txt', 'revision': 'synthetic',
                'sha256': hashlib.sha256(source).hexdigest(), 'anchor': 'Synthetic protocol'}])
        captured = run(args.from_client, 'capture', 'Call memory_remember exactly once using these '
            'explicit synthetic arguments. Report its returned ID.\n' + json.dumps(fields), 'memory_remember')
        require(sum(c['name'] == 'memory_remember' for c in captured['calls']) == 1, 'Duplicate native capture calls')
        initial = call(home, 'recall', {'query': 'nebula codec', 'at': 1})
        require(initial['result']['results'][0]['assertions'][0]['value'] == 'UTF-8', 'Capture absent')
        peer = run(args.to_client, 'peer-recall', 'Recall the nebula codec required encoding at time 1. '
            'Give its value and source citation. Do not modify memory.', 'memory_recall')
        require('UTF-8' in peer['answer'] and 'contract.txt' in peer['answer'], 'Peer omitted value or citation')
        successor = {key: fields[key] for key in ('scope', 'subject', 'relation', 'value', 'text', 'region', 'citations')}
        successor.update(value='UTF-16', text='The corrected nebula codec requirement is UTF-16.')
        revision = run(args.to_client, 'peer-correction', 'First recall the nebula codec at time 1. '
            'Then correct it using memory_revise exactly once with the returned assertion ID in predecessors '
            'and its complete state_token as expected_state. revision_kind is correction, reason is synthetic '
            'protocol correction, affected is the successor region. Use this successor. Do not call '
            'memory_remember.\n' + json.dumps(successor), 'memory_revise')
        require(sum(c['name'] == 'memory_revise' for c in revision['calls']) == 1, 'Correction required retries')
        require(not any(c['name'] == 'memory_remember' for c in revision['calls']), 'Correction bypassed revision')
        now = call(home, 'recall', {'query': 'nebula codec', 'at': 1})
        require({a['value'] for r in now['result']['results'] for a in r['assertions']} == {'UTF-16'},
                'Correction not projected')
        restarted = run(args.from_client, 'origin-restarted', 'Recall the nebula codec required encoding '
            'at time 1 and state its current value and source citation. Do not modify memory.', 'memory_recall')
        require('UTF-16' in restarted['answer'] and 'contract.txt' in restarted['answer'], 'Origin missed correction')
        history = call(home, 'recall', {'query': 'nebula codec', 'at': 1, 'known_at': initial['result']['snapshot']})
        require({a['value'] for r in history['result']['results'] for a in r['assertions']} == {'UTF-8'},
                'Historical knowledge changed')
        eid = initial['result']['results'][0]['assertions'][0]['id']
        erased = call(home, 'purge', {'eid': eid}, owner=True)
        require('error' not in erased, 'Owner erasure failed')
        require(not call(home, 'recall', {'query': 'nebula codec', 'at': 1})['result']['results'], 'Erased lineage remains')
        for name in clients:
            result = run(name, name + '-erased', 'Recall the nebula codec required encoding at time 1. '
                'If there is no evidence, say it is unavailable. Do not guess or modify memory.', 'memory_recall')
            require('UTF-8' not in result['answer'] and 'UTF-16' not in result['answer'], 'Client repeated erased value')
        report.update(passed=True, before=initial, corrected=now, historical=history, erased=erased)
    except Exception as exc:
        report['failure'] = type(exc).__name__ + ': ' + str(exc)
    finally:
        daemon.terminate()
        _, stderr = daemon.communicate(timeout=10)
        (args.output / 'daemon.stderr').write_bytes(stderr)
        report['finished_ns'] = time.time_ns()
        report['runtime_unchanged'] = report['runtime'] == runtime_identity()
        report['passed'] = report['passed'] and report['runtime_unchanged']
        (args.output / 'report.json').write_text(json.dumps(report, indent=2))
    print(json.dumps({'passed': report['passed'], 'report': str(args.output / 'report.json')}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
