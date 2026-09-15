"""Actual native SessionStart delivery using only a synthetic enrolled workspace."""
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

parser = argparse.ArgumentParser()
parser.add_argument('--configuration', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
require(Path(lumen.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
        and 'site-packages' in Path(lumen.__file__).parts, 'Use an installed environment')
args.output.mkdir(parents=True, exist_ok=False)
root = Path(tempfile.mkdtemp(prefix='lumen-native-hook-'))
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
report = {'schema': 1, 'passed': False, 'runtime': runtime_identity(), 'fixture': str(root),
          'hook_sha256': binary_digest, 'started_ns': time.time_ns(), 'release_acceptance': False,
          'driver_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
          'native_adapter_sha256': hashlib.sha256(Path(__file__).with_name('native_clients.py').read_bytes()).hexdigest()}
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
        text=source.decode(), region=region(start=0), citations=[{'kind':'file', 'project':'p',
        'path':'contract.txt', 'revision':'synthetic', 'anchor':'Synthetic hook test',
        'sha256':hashlib.sha256(source).hexdigest()}]))
    require('error' not in captured, 'Synthetic capture failed')
    config = json.loads(args.configuration.read_text(encoding='utf-8-sig'))['claude-code']
    client = NativeClient('claude-code', config['binary'], config['coverage'], child, home, sys.executable, args.output)
    settings_index = client.command.index('--settings') + 1
    settings = json.loads(client.command[settings_index])
    settings.update(disableAllHooks=False, hooks={'SessionStart':[{'matcher':'startup','hooks':[{
        'type':'command', 'command':sys.executable, 'args':['-I','-m','lumen.hook','--host','claude-code',
        '--python',sys.executable,'--home',str(home)], 'timeout':5}]}]})
    client.command[settings_index] = json.dumps(settings)
    client.command.append('--include-hook-events')
    result = client.run('session-start', 'What encoding does the aurora codec require at time 1? '
                        'Answer with the value and its source citation.')
    report['native'] = result
    require(any(c['name'] == 'memory_recall' for c in result['calls']), 'Host did not recall')
    require('UTF-32' in result['answer'] and 'contract.txt' in result['answer'], 'Missing value or source citation')
    messages = [json.loads(line) for line in (args.output/'claude-code-session-start.jsonl').read_text(encoding='utf-8').splitlines()]
    delivery = [m for m in messages if m.get('type') == 'system' and m.get('subtype') == 'hook_response'
                and 'SessionStart' in json.dumps(m) and 'memory_recall' in json.dumps(m)]
    require(delivery, 'Native stream did not record SessionStart hint delivery')
    report.update(passed=True, delivery=delivery, installed_client_versions={'claude-code':client.coverage['version']})
except Exception as exc:
    report['failure'] = type(exc).__name__ + ': ' + str(exc)
finally:
    daemon.terminate()
    _, stderr = daemon.communicate(timeout=10)
    (args.output/'daemon.stderr').write_bytes(stderr)
    report.update(finished_ns=time.time_ns(), runtime_unchanged=report['runtime']==runtime_identity(),
                  hook_unchanged=binary_digest==hashlib.sha256(binary.read_bytes()).hexdigest())
    report['passed'] = report['passed'] and report['runtime_unchanged'] and report['hook_unchanged']
    (args.output/'report.json').write_text(json.dumps(report,indent=2))
print(json.dumps({'passed':report['passed'],'report':str(args.output/'report.json')}))
raise SystemExit(0 if report['passed'] else 1)
