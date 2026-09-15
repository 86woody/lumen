"""Actual capture-at-stop and duplicate-hook delivery on installed Claude Code.

Session one registers the three Lumen hooks once and must leave exactly one paired,
redacted episode whose assistant text equals the native result. Session two registers
every handler twice as distinct entries; the stream must show two Stop deliveries and
the ledger still one episode. A fresh store then expands each episode by id.
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
from lumen.hosts import claude_handlers
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
root = Path(tempfile.mkdtemp(prefix='lumen-native-capture-'))
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
    config = json.loads(args.configuration.read_text(encoding='utf-8-sig'))['claude-code']
    client = NativeClient('claude-code', config['binary'], config['coverage'], workspace_root, home, sys.executable, args.output)
    settings_index = client.command.index('--settings') + 1
    base = json.loads(client.command[settings_index])
    client.command.append('--include-hook-events')
    handlers = claude_handlers(Path(sys.executable).resolve(), home.resolve())
    doubled = {event: groups + [json.loads(json.dumps(groups[0]).replace('lumen-memory', 'lumen-memory-second'))]
               for event, groups in handlers.items()}

    def session(label, hooks):
        client.command[settings_index] = json.dumps({**base, 'disableAllHooks': False, 'hooks': hooks})
        result = client.run(label, question)
        messages = [json.loads(line) for line in (args.output / ('claude-code-' + label + '.jsonl')).read_text(encoding='utf-8').splitlines()]
        init = next(m for m in messages if m.get('type') == 'system' and m.get('subtype') == 'init')
        responses = [m for m in messages if m.get('type') == 'system' and m.get('subtype') == 'hook_response']
        deliveries = {}
        for event in ('SessionStart', 'UserPromptSubmit', 'Stop'):
            matching = [m for m in responses if m.get('hook_event') == event]
            deliveries[event] = {'count': len(matching), 'all_exit_zero': all(m.get('exit_code') == 0 for m in matching),
                                 'capture_stdout_empty': all(not m.get('stdout') for m in matching) if event != 'SessionStart' else None}
        report['runs'].append({'label': label, 'session_id': init['session_id'], 'deliveries': deliveries,
                               'calls': result['calls'], 'answer': result['answer'], 'run': result['run'],
                               'stdout_sha256': result['stdout_sha256'], 'stderr_sha256': result['stderr_sha256']})
        return report['runs'][-1]

    once = session('capture', handlers)
    require(once['deliveries']['Stop'] == {'count': 1, 'all_exit_zero': True, 'capture_stdout_empty': True}, 'Single Stop delivery not observed')
    require(once['deliveries']['UserPromptSubmit']['count'] == 1, 'Single prompt delivery not observed')
    twice = session('doubled', doubled)
    require(twice['deliveries']['Stop'] == {'count': 2, 'all_exit_zero': True, 'capture_stdout_empty': True}, 'Doubled Stop delivery not observed')
    require(twice['deliveries']['UserPromptSubmit']['count'] == 2, 'Doubled prompt delivery not observed')
    status = call(home, 'doctor', {})['result']
    # The live daemon reports index_pending until the outbox drains; capture counters are the check here.
    require({k: status['capture'][k] for k in ('pending_prompts', 'capture_conflicts')} == {'pending_prompts': 0, 'capture_conflicts': 0},
            'Daemon reported capture trouble: ' + json.dumps(status['capture']))
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
        require(episode['state'] == 'paired', 'Prompt and stop were not paired; prompt_id differed or a delivery was lost')
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
    report.update(passed=True, scanned_files=scanned, installed_client_versions={'claude-code': client.coverage['version']})
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
