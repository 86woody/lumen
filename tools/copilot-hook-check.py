"""Actual native sessionStart delivery on installed Copilot CLI, launched from a child directory.

The Lumen hook file is written into the client's isolated COPILOT_HOME. The session must call
memory_recall and answer with the seeded value and citation; the Copilot transcript must record
the sessionStart hook ending successfully with the Lumen hint as its output.
"""
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
from lumen.hosts import copilot_handlers, manage_copilot_hooks
from lumen.model import region
from lumen.workspace import Workspace
from native_clients import NativeClient, machine_environment, require

parser = argparse.ArgumentParser()
parser.add_argument('--configuration', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
require(Path(lumen.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
        and 'site-packages' in Path(lumen.__file__).parts, 'Use an installed environment')
args.output.mkdir(parents=True, exist_ok=False)
root = Path(tempfile.mkdtemp(prefix='lumen-copilot-hook-'))
workspace_root, home = root / 'workspace', root / 'store'
workspace_root.mkdir()
child = workspace_root / 'child'
child.mkdir()
source = b'The aurora codec uses UTF-32.'
(workspace_root / 'contract.txt').write_bytes(source)
Workspace.initialize(workspace_root, [{'id': 'p', 'root': '.', 'kind': 'project',
    'purpose': 'synthetic native hook fixture', 'owners': ['fixture-owner'], 'depends_on': []}])
workspace = Workspace(workspace_root, home)
enroll(home, workspace.id, {'repo:p'}, project='p')
binary = Path(lumen.__file__).resolve().with_name('hook.py')
binary_digest = hashlib.sha256(binary.read_bytes()).hexdigest()
drivers = [Path(__file__).resolve(), Path(__file__).resolve().with_name('native_clients.py')]
report = {'schema': 1, 'passed': False, 'runtime': runtime_identity(), 'fixture': str(root), 'launch_from': 'child',
          'hook_sha256': binary_digest, 'started_ns': time.time_ns(), 'release_acceptance': False,
          'driver_hashes': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in drivers},
          'configuration_sha256': hashlib.sha256(args.configuration.read_bytes()).hexdigest()}
daemon = subprocess.Popen([sys.executable, '-m', 'lumen', '--home', str(home), 'daemon',
    '--workspace', str(workspace_root)], cwd=workspace_root, env=machine_environment(),
    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
try:
    for attempt in range(100):
        try:
            require(call(home, 'doctor', {})['result']['healthy'], 'Daemon unhealthy')
            break
        except Exception:
            require(daemon.poll() is None and attempt < 99, 'Daemon startup failed')
            time.sleep(.05)
    captured = call(home, 'remember', dict(checkout=workspace.checkout, host='local-fixture', session='seed',
        turn='1', scope='repo:p', subject='aurora codec', relation='required_encoding', value='UTF-32',
        text=source.decode(), region=region(start=0), citations=[{'kind': 'file', 'project': 'p',
        'path': 'contract.txt', 'revision': 'synthetic', 'anchor': 'Synthetic hook test',
        'sha256': hashlib.sha256(source).hexdigest()}]))
    require('error' not in captured, 'Synthetic capture failed')
    config = json.loads(args.configuration.read_text(encoding='utf-8-sig'))['copilot-cli']
    client = NativeClient('copilot-cli', config['binary'], config['coverage'], child, home, sys.executable, args.output)
    handlers = copilot_handlers(Path(sys.executable).resolve(), home.resolve())
    hook_file = client.copilot_home / 'hooks' / 'lumen-memory.json'
    report['hook_file'] = manage_copilot_hooks('write', hook_file, handlers)
    result = client.run('session-start', 'What encoding does the aurora codec require at time 1? '
                        'Answer with the value and its source citation.')
    report['native'] = result
    require(any(c['name'] == 'memory_recall' for c in result['calls']), 'Host did not recall')
    require('UTF-32' in result['answer'] and 'contract.txt' in result['answer'], 'Missing value or source citation')
    messages = [json.loads(line) for line in (args.output / 'copilot-cli-session-start.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
    session_id = next(m for m in messages if m.get('type') == 'result')['sessionId']
    transcript = client.copilot_home / 'session-state' / session_id / 'events.jsonl'
    events = [json.loads(line) for line in transcript.read_text(encoding='utf-8').splitlines() if line.strip()]
    delivery = [e['data'] for e in events if e.get('type') == 'hook.end' and e['data'].get('hookType') == 'sessionStart']
    require(delivery and all(d.get('success') for d in delivery), 'Transcript did not record a successful sessionStart hook')
    require(any('memory_recall' in json.dumps(d.get('output')) for d in delivery), 'Hook output did not carry the Lumen hint')
    report.update(passed=True, delivery=delivery, session_id=session_id,
                  hook_events=[e['data'].get('hookType') for e in events if e.get('type') == 'hook.start'],
                  installed_client_versions={'copilot-cli': client.coverage['version']})
except Exception as exc:
    report['failure'] = type(exc).__name__ + ': ' + str(exc)
finally:
    daemon.terminate()
    _, stderr = daemon.communicate(timeout=10)
    (args.output / 'daemon.stderr').write_bytes(stderr)
    report.update(finished_ns=time.time_ns(), runtime_unchanged=report['runtime'] == runtime_identity(),
                  hook_unchanged=binary_digest == hashlib.sha256(binary.read_bytes()).hexdigest(),
                  drivers_unchanged=report['driver_hashes'] == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in drivers})
    report['passed'] = report['passed'] and report['runtime_unchanged'] and report['hook_unchanged'] and report['drivers_unchanged']
    (args.output / 'report.json').write_text(json.dumps(report, indent=2))
print(json.dumps({'passed': report['passed'], 'report': str(args.output / 'report.json')}))
raise SystemExit(0 if report['passed'] else 1)
