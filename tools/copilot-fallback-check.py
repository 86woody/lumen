"""Actual instruction-file fallback on installed Copilot CLI: no hooks, fenced AGENTS.md only.

Copilot reads AGENTS.md from the git root and the working directory, so there is no import
chain. The isolated COPILOT_HOME holds no hooks directory and no personal instruction file.
Session one must quote the fenced section; session two asks a neutral question and passes
only on an unprompted memory_recall with the correct value and citation and zero hook
events in the transcript; session three runs with custom instructions off and is recorded,
never used as a gate, because the MCP server's own instructions reach every session.
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
from lumen.model import region
from lumen.workspace import Workspace, git_environment
from native_clients import NativeClient, machine_environment, require

parser = argparse.ArgumentParser()
parser.add_argument('--configuration', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
require(Path(lumen.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
        and 'site-packages' in Path(lumen.__file__).parts, 'Use an installed environment')
args.output.mkdir(parents=True, exist_ok=False)
root = Path(tempfile.mkdtemp(prefix='lumen-copilot-fallback-'))
workspace_root, home = root / 'workspace', root / 'store'
workspace_root.mkdir()
source = b'The borealis codec uses UTF-32.'
(workspace_root / 'contract.txt').write_bytes(source)
# A repository marker makes the workspace root the host's project root for instruction loading.
subprocess.run(['git', 'init', '-q', str(workspace_root)], check=True, env=git_environment(), capture_output=True)
Workspace.initialize(workspace_root, [{'id': 'p', 'root': '.', 'kind': 'project',
    'purpose': 'synthetic native fallback fixture', 'owners': ['fixture-owner'], 'depends_on': []}])
workspace = Workspace(workspace_root, home)
instructions = workspace.manage_instructions('write')
require(instructions['changed'] and b'BEGIN LUMEN MEMORY' in (workspace_root / 'AGENTS.md').read_bytes(), 'Instruction file not generated')
enroll(home, workspace.id, {'repo:p'}, project='p')
drivers = [Path(__file__).resolve(), Path(__file__).resolve().with_name('native_clients.py')]
report = {'schema': 1, 'passed': False, 'runtime': runtime_identity(), 'fixture': str(root), 'launch_from': 'root',
          'started_ns': time.time_ns(), 'release_acceptance': False, 'runs': [],
          'driver_hashes': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in drivers},
          'configuration_sha256': hashlib.sha256(args.configuration.read_bytes()).hexdigest(),
          'instruction_section_sha256': hashlib.sha256((workspace_root / 'AGENTS.md').read_bytes()).hexdigest()}
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
        turn='1', scope='repo:p', subject='borealis codec', relation='required_encoding', value='UTF-32',
        text=source.decode(), region=region(start=0), citations=[{'kind': 'file', 'project': 'p',
        'path': 'contract.txt', 'revision': 'synthetic', 'anchor': 'Synthetic fallback test',
        'sha256': hashlib.sha256(source).hexdigest()}]))
    require('error' not in captured, 'Synthetic capture failed')
    config = json.loads(args.configuration.read_text(encoding='utf-8-sig'))['copilot-cli']
    client = NativeClient('copilot-cli', config['binary'], config['coverage'], workspace_root, home, sys.executable, args.output)
    require(not (client.copilot_home / 'hooks').exists() and not (client.copilot_home / 'copilot-instructions.md').exists(),
            'Isolated home must hold no hooks and no personal instructions')
    isolated = list(client.command)
    require('--no-custom-instructions' in isolated, 'Native command shape changed')
    with_instructions = [a for a in isolated if a != '--no-custom-instructions']
    question = ('What encoding does the borealis codec require at time 1? '
                'Answer with the value and its source citation.')

    def session(label, command, prompt=question):
        client.command = command
        result = client.run(label, prompt)
        messages = [json.loads(line) for line in (args.output / ('copilot-cli-' + label + '.jsonl')).read_text(encoding='utf-8').splitlines() if line.strip()]
        session_id = next(m for m in messages if m.get('type') == 'result')['sessionId']
        transcript = client.copilot_home / 'session-state' / session_id / 'events.jsonl'
        events = [json.loads(line) for line in transcript.read_text(encoding='utf-8').splitlines() if line.strip()]
        hook_events = [e for e in events if e.get('type') in ('hook.start', 'hook.end')]
        recalled = any(c['name'] == 'memory_recall' for c in result['calls'])
        correct = 'UTF-32' in (result['answer'] or '') and 'contract.txt' in (result['answer'] or '')
        report['runs'].append({'label': label, 'session_id': session_id, 'custom_instructions': '--no-custom-instructions' not in command,
                               'recalled': recalled, 'correct_citation': correct, 'hook_events': len(hook_events),
                               'calls': result['calls'], 'answer': result['answer'], 'run': result['run'],
                               'stdout_sha256': result['stdout_sha256'], 'stderr_sha256': result['stderr_sha256']})
        return recalled, correct, hook_events

    session('loaded', with_instructions, "What does this project's instruction file tell you to do before starting work? "
                                         'Quote the relevant line exactly.')
    loaded = report['runs'][-1]
    require(not loaded['hook_events'] and 'memory_recall' in (loaded['answer'] or ''),
            'Host did not report the fenced instruction section')
    recalled, correct, hook_events = session('fallback', with_instructions)
    require(not hook_events, 'Hook events appeared although no hook file exists')
    require(recalled, 'Host did not recall through the instruction-file fallback')
    require(correct, 'Missing value or source citation')
    control = session('control', isolated)
    report['control'] = {'recalled': control[0], 'correct_citation': control[1], 'hook_events': len(control[2]),
                         'note': 'Observation only; the control never gates the fallback case'}
    report.update(passed=True, installed_client_versions={'copilot-cli': client.coverage['version']})
except Exception as exc:
    report['failure'] = type(exc).__name__ + ': ' + str(exc)
finally:
    daemon.terminate()
    _, stderr = daemon.communicate(timeout=10)
    (args.output / 'daemon.stderr').write_bytes(stderr)
    report.update(finished_ns=time.time_ns(), runtime_unchanged=report['runtime'] == runtime_identity(),
                  drivers_unchanged=report['driver_hashes'] == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in drivers})
    report['passed'] = report['passed'] and report['runtime_unchanged'] and report['drivers_unchanged']
    (args.output / 'report.json').write_text(json.dumps(report, indent=2))
print(json.dumps({'passed': report['passed'], 'report': str(args.output / 'report.json')}))
raise SystemExit(0 if report['passed'] else 1)
