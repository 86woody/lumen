"""Actual instruction-file fallback: hooks disabled, fenced AGENTS.md section only.

Session one must quote the fenced section, proving the CLAUDE.md import chain loaded
with every hook disabled. Session two asks a neutral question; passing requires an
unprompted memory_recall with the correct value and citation and zero hook events in
the native stream. Session three is a control with all instruction files excluded;
its outcome is recorded, never used as a gate. Attempts 2 and 3 showed restricted
mode never loads project instruction files, so this case runs unrestricted.
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
parser.add_argument('--launch-from', choices=['root', 'child'], default='root',
                    help='Host launch directory. From a child directory the root CLAUDE.md import of '
                         'AGENTS.md resolves outside the launch directory, is treated as an external '
                         'import awaiting an interactive approval, and delivers nothing headless.')
args = parser.parse_args()
require(Path(lumen.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
        and 'site-packages' in Path(lumen.__file__).parts, 'Use an installed environment')
args.output.mkdir(parents=True, exist_ok=False)
root = Path(tempfile.mkdtemp(prefix='lumen-native-fallback-'))
workspace_root, home = root / 'workspace', root / 'store'
workspace_root.mkdir()
child = workspace_root / 'child'
child.mkdir()
source = b'The borealis codec uses UTF-32.'
(workspace_root / 'contract.txt').write_bytes(source)
# A repository marker makes the workspace root the host's project root for instruction loading.
subprocess.run(['git', 'init', '-q', str(workspace_root)], check=True, env=git_environment(), capture_output=True)
Workspace.initialize(workspace_root, [{'id': 'p', 'root': '.', 'kind': 'project',
    'purpose': 'synthetic native fallback fixture', 'owners': ['fixture-owner'], 'depends_on': []}])
workspace = Workspace(workspace_root, home)
instructions = workspace.manage_instructions('write')
require(instructions['changed'] and (workspace_root / 'CLAUDE.md').read_bytes() == b'@AGENTS.md\n', 'Instruction files not generated')
enroll(home, workspace.id, {'repo:p'}, project='p')
drivers = [Path(__file__).resolve(), Path(__file__).resolve().with_name('native_clients.py')]
report = {'schema': 1, 'passed': False, 'runtime': runtime_identity(), 'fixture': str(root),
          'started_ns': time.time_ns(), 'release_acceptance': False, 'runs': [],
          'driver_hashes': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in drivers},
          'configuration_sha256': hashlib.sha256(args.configuration.read_bytes()).hexdigest(),
          'instruction_section_sha256': hashlib.sha256((workspace_root / 'AGENTS.md').read_bytes()).hexdigest()}
# Every instruction file outside the synthetic workspace is excluded by absolute path, in both
# separator forms, so no private content can reach the model even if the host walks further.
outside = [Path.home() / '.claude' / 'CLAUDE.md', Path.home() / '.claude' / 'CLAUDE.local.md']
for ancestor in [workspace_root.resolve().parent, *workspace_root.resolve().parent.parents]:
    outside.extend([ancestor / 'CLAUDE.md', ancestor / 'CLAUDE.local.md', ancestor / 'AGENTS.md'])
excluded = sorted({form for p in outside for form in (str(p), p.as_posix())})
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
        text=source.decode(), region=region(start=0), citations=[{'kind':'file', 'project':'p',
        'path':'contract.txt', 'revision':'synthetic', 'anchor':'Synthetic fallback test',
        'sha256':hashlib.sha256(source).hexdigest()}]))
    require('error' not in captured, 'Synthetic capture failed')
    config = json.loads(args.configuration.read_text(encoding='utf-8-sig'))['claude-code']
    launch = child if args.launch_from == 'child' else workspace_root
    report['launch_from'] = args.launch_from
    client = NativeClient('claude-code', config['binary'], config['coverage'], launch, home, sys.executable, args.output)
    settings_index = client.command.index('--settings') + 1
    base = json.loads(client.command[settings_index])
    require(base['disableAllHooks'] is True and 'hooks' not in base, 'Hooks must stay disabled')
    client.command.append('--include-hook-events')
    question = ('What encoding does the borealis codec require at time 1? '
                'Answer with the value and its source citation.')

    def session(label, exclusions, prompt=question):
        # Recompute the position: attempt 4 reused a stale index after removing --restricted,
        # which turned the settings JSON into the prompt and dropped --strict-mcp-config.
        client.command[client.command.index('--settings') + 1] = json.dumps({**base, 'claudeMdExcludes': exclusions})
        require(client.command.count('--settings') == 1 and '--strict-mcp-config' in client.command
                and '--restricted' not in client.command and client.command[-1] == '--include-hook-events'
                and client.command[client.command.index('--setting-sources') + 1] == 'project',
                'Native command shape changed')
        result = client.run(label, prompt)
        messages = [json.loads(line) for line in (args.output / ('claude-code-' + label + '.jsonl')).read_text(encoding='utf-8').splitlines()]
        hook_events = [m for m in messages if m.get('type') == 'system' and m.get('subtype') == 'hook_response']
        recalled = any(c['name'] == 'memory_recall' for c in result['calls'])
        correct = 'UTF-32' in (result['answer'] or '') and 'contract.txt' in (result['answer'] or '')
        report['runs'].append({'label': label, 'excluded_count': len(exclusions), 'recalled': recalled,
                               'correct_citation': correct, 'hook_events': len(hook_events),
                               'calls': result['calls'], 'answer': result['answer'], 'run': result['run'],
                               'stdout_sha256': result['stdout_sha256'], 'stderr_sha256': result['stderr_sha256']})
        return recalled, correct, hook_events

    # Restricted mode loads only managed settings and --settings, and attempts 2 and 3 showed no
    # project instruction file reaches the model under it. This case therefore runs without
    # --restricted: no built-in tools (--tools ''), only the four strict local MCP tools, explicit
    # --settings, and only the synthetic fixture's project scope as a setting source. The fixture
    # holds no settings, skills, agents, commands or MCP files; every outside instruction file is
    # excluded by absolute path above.
    client.command.remove('--restricted')
    sources_index = client.command.index('--setting-sources') + 1
    require(client.command[sources_index] == '', 'Unexpected setting sources')
    client.command[sources_index] = 'project'
    # The import chain CLAUDE.md -> @AGENTS.md -> fenced section is only observable through the model.
    session('loaded', excluded, "What does this project's instruction file tell you to do before starting work? "
                                'Quote the relevant line exactly.')
    loaded = report['runs'][-1]
    require(not loaded['hook_events'] and 'memory_recall' in (loaded['answer'] or ''),
            'Host did not report the fenced instruction section')
    recalled, correct, hook_events = session('fallback', excluded)
    require(not hook_events, 'Hook events appeared although every hook was disabled')
    require(recalled, 'Host did not recall through the instruction-file fallback')
    require(correct, 'Missing value or source citation')
    control = session('control', ['**'])
    report['control'] = {'recalled': control[0], 'correct_citation': control[1], 'hook_events': len(control[2]),
                         'note': 'Observation only; the control never gates the fallback case'}
    report.update(passed=True, installed_client_versions={'claude-code': client.coverage['version']},
                  excluded_patterns=excluded)
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
