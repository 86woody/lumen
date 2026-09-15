"""Actual capture-at-stop and duplicate-hook delivery on installed Copilot CLI.

Session one installs the Lumen hook file once and must leave exactly one paired, redacted
episode whose assistant text equals the native result. Session two lists every handler twice;
Copilot's transcript records one hook start per event however many entries ran, so the second
delivery is evidenced by Lumen's duplicate-delivery counter, and the ledger still holds one
episode. A fresh store then expands each episode by id. Copilot hands the stop hook no final message, so the
episode comes from the transcript's last turn, keyed by the assistant message id.
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
from lumen.model import Access
from lumen.security import redact
from lumen.store import Store
from lumen.workspace import Workspace, git_environment
from native_clients import NativeClient, machine_environment, require

parser = argparse.ArgumentParser()
parser.add_argument('--configuration', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
require(Path(lumen.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
        and 'site-packages' in Path(lumen.__file__).parts, 'Use an installed environment')
args.output.mkdir(parents=True, exist_ok=False)
root = Path(tempfile.mkdtemp(prefix='lumen-copilot-capture-'))
workspace_root, home = root / 'workspace', root / 'store'
workspace_root.mkdir()
subprocess.run(['git', 'init', '-q', str(workspace_root)], check=True, env=git_environment(), capture_output=True)
Workspace.initialize(workspace_root, [{'id': 'p', 'root': '.', 'kind': 'project',
    'purpose': 'synthetic native capture fixture', 'owners': ['fixture-owner'], 'depends_on': []}])
workspace = Workspace(workspace_root, home)
enroll(home, workspace.id, {'repo:p'}, project='p')
binary = Path(lumen.__file__).resolve().with_name('hook.py')
binary_digest = hashlib.sha256(binary.read_bytes()).hexdigest()
drivers = [Path(__file__).resolve(), Path(__file__).resolve().with_name('native_clients.py')]
report = {'schema': 1, 'passed': False, 'runtime': runtime_identity(), 'fixture': str(root), 'launch_from': 'root',
          'hook_sha256': binary_digest, 'started_ns': time.time_ns(), 'release_acceptance': False, 'runs': [],
          'driver_hashes': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in drivers},
          'configuration_sha256': hashlib.sha256(args.configuration.read_bytes()).hexdigest()}
SECRET = 'sk-lumenfixture0123456789abcdef'
question = ('Reply with one short greeting sentence and nothing else; no tool call is needed. '
            'Fixture token, not to be repeated: ' + SECRET)
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
    config = json.loads(args.configuration.read_text(encoding='utf-8-sig'))['copilot-cli']
    client = NativeClient('copilot-cli', config['binary'], config['coverage'], workspace_root, home, sys.executable, args.output)
    handlers = copilot_handlers(Path(sys.executable).resolve(), home.resolve())
    doubled = {**handlers, 'hooks': {event: groups + [json.loads(json.dumps(groups[0]).replace('lumen-memory', 'lumen-memory-second'))]
                                     for event, groups in handlers['hooks'].items()}}
    hook_file = client.copilot_home / 'hooks' / 'lumen-memory.json'

    def session(label, hooks):
        manage_copilot_hooks('write', hook_file, hooks)
        result = client.run(label, question)
        messages = [json.loads(line) for line in (args.output / ('copilot-cli-' + label + '.jsonl')).read_text(encoding='utf-8').splitlines() if line.strip()]
        session_id = next(m for m in messages if m.get('type') == 'result')['sessionId']
        transcript = client.copilot_home / 'session-state' / session_id / 'events.jsonl'
        events = [json.loads(line) for line in transcript.read_text(encoding='utf-8').splitlines() if line.strip()]
        deliveries = {}
        for event in ('sessionStart', 'agentStop'):
            starts = [e for e in events if e.get('type') == 'hook.start' and e['data'].get('hookType') == event]
            ends = [e for e in events if e.get('type') == 'hook.end' and e['data'].get('hookType') == event]
            deliveries[event] = {'count': len(starts), 'ended': len(ends), 'all_success': all(e['data'].get('success') for e in ends)}
        report['runs'].append({'label': label, 'session_id': session_id, 'deliveries': deliveries,
                               'calls': result['calls'], 'answer': result['answer'], 'run': result['run'],
                               'stdout_sha256': result['stdout_sha256'], 'stderr_sha256': result['stderr_sha256']})
        return report['runs'][-1]

    once = session('capture', handlers)
    require(once['deliveries']['agentStop'] == {'count': 1, 'ended': 1, 'all_success': True}, 'Single agentStop delivery not observed')
    require(call(home, 'doctor', {})['result']['capture']['duplicate_deliveries'] == 0, 'Unexpected duplicate before the doubled session')
    twice = session('doubled', doubled)
    # Copilot's transcript records one hook start per event however many entries ran (observation run 2),
    # so the second delivery is evidenced by Lumen's own duplicate counter, not by the transcript.
    require(twice['deliveries']['agentStop'] == {'count': 1, 'ended': 1, 'all_success': True}, 'Doubled session agentStop not observed')
    status = call(home, 'doctor', {})['result']
    require(status['capture'] == {'pending_prompts': 0, 'capture_conflicts': 0, 'duplicate_deliveries': 1},
            'Daemon did not record exactly one duplicate delivery: ' + json.dumps(status['capture']))
    report['doctor'] = {'capture': status['capture'], 'failures': status.get('failures')}
except Exception as exc:
    report['failure'] = type(exc).__name__ + ': ' + str(exc)
finally:
    daemon.terminate()
    _, stderr = daemon.communicate(timeout=10)
    (args.output / 'daemon.stderr').write_bytes(stderr)
try:
    require('failure' not in report, report.get('failure', ''))
    require(len(report['runs']) == 2, 'Native sessions incomplete')
    grant = Access(workspace.id, frozenset({'repo:p'}), 'local-owner', True)
    with Store(home) as store:
        captured = [e for e in store.events(grant) if e['kind'] == 'episode']
        store.reindex(grant)
        require(store.doctor()['healthy'], 'Restarted store unhealthy')
    report['episodes'] = [{k: e[k] for k in ('id', 'session', 'turn', 'state', 'bytes', 'digest')} for e in captured]
    require(len(captured) == 2, 'Expected exactly one episode per session, found %d' % len(captured))
    for run in report['runs']:
        matching = [e for e in captured if e['session'] == run['session_id']]
        require(len(matching) == 1, 'Session did not capture exactly once')
        episode = matching[0]
        require(episode['host'] == 'copilot-cli', 'Episode host differs')
        require(episode['state'] == 'paired', 'Transcript turn was not paired with its user message')
        require(episode['user'] == redact(question) and SECRET not in episode['user'], 'Prompt text not captured verbatim and redacted')
        require(episode['assistant'] == redact(run['answer']), 'Assistant text differs from the native result')
        run['episode_id'] = episode['id']
    scanned = 0
    for path in home.rglob('*'):
        if path.is_file():
            scanned += 1
            require(SECRET.encode() not in path.read_bytes(), 'Secret reached disk: ' + path.name)
    with Store(home) as fresh:
        for run in report['runs']:
            expanded = fresh.expand(grant, run['episode_id'])['event']
            require(expanded is not None and expanded['digest'] == next(e['digest'] for e in captured if e['id'] == run['episode_id']),
                    'Fresh process could not expand the episode')
    report.update(passed=True, scanned_files=scanned, installed_client_versions={'copilot-cli': client.coverage['version']})
except Exception as exc:
    report['failure'] = type(exc).__name__ + ': ' + str(exc)
finally:
    report.update(finished_ns=time.time_ns(), runtime_unchanged=report['runtime'] == runtime_identity(),
                  hook_unchanged=binary_digest == hashlib.sha256(binary.read_bytes()).hexdigest(),
                  drivers_unchanged=report['driver_hashes'] == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in drivers})
    report['passed'] = report['passed'] and report['runtime_unchanged'] and report['hook_unchanged'] and report['drivers_unchanged']
    (args.output / 'report.json').write_text(json.dumps(report, indent=2))
print(json.dumps({'passed': report['passed'], 'report': str(args.output / 'report.json')}))
raise SystemExit(0 if report['passed'] else 1)
