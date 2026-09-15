"""Actual installed Claude Code MCP exercise; no SDK, proxy, or API credentials.

Run with an installed Lumen environment. Coverage evidence is an operator observation,
not inferred from credentials; the executable auth status must match its account hash.
Raw client output stays local until reviewed. This is not the cross-client release gate.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

import lumen
from lumen.daemon import call, enroll
from lumen.evaluation import runtime_identity
from lumen.model import digest, region
from lumen.workspace import Workspace


def check(condition, message):
    if not condition:
        raise RuntimeError(message)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--coverage-evidence', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    coverage = json.loads(args.coverage_evidence.read_text())
    check(coverage['client'] == 'claude-code' and coverage['usage_credits_enabled'] is False,
          'Current zero-spend subscription evidence required')
    check(0 <= time.time() - coverage['observed_unix_seconds'] <= 3600,
          'Refresh account-bound UI observation before running')
    check(Path(lumen.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
          and 'site-packages' in Path(lumen.__file__).parts, 'Use an installed Lumen environment')
    executable = shutil.which('claude')
    check(executable is not None, 'Claude Code is not installed')
    # Carry OS runtime paths only. Never inspect or pass API/routing credentials.
    keep = {'PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT', 'TEMP', 'TMP',
            'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'PROGRAMDATA', 'HOMEDRIVE', 'HOMEPATH'}
    env = {k: v for k, v in os.environ.items() if k.upper() in keep}
    env.update(CLAUDE_CODE_DISABLE_AUTO_MEMORY='1', ENABLE_TOOL_SEARCH='false',
               CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1', CLAUDE_CODE_DISABLE_FAST_MODE='1')
    root = Path(tempfile.mkdtemp(prefix='lumen-claude-fixture-'))
    workspace_root, home = root / 'workspace', root / 'store'
    workspace_root.mkdir()
    version = subprocess.run([executable, '--version'], cwd=workspace_root, env=env,
                             capture_output=True, text=True, check=True, timeout=20).stdout.strip()
    check(version.split()[0] == coverage['version'], 'Installed client version changed')
    auth = subprocess.run([executable, 'auth', 'status'], cwd=workspace_root, env=env,
                          capture_output=True, text=True, check=True, timeout=20)
    status = json.loads(auth.stdout)
    check(status.get('loggedIn') is True and status.get('authMethod') == 'claude.ai'
          and status.get('apiProvider') == 'firstParty' and status.get('subscriptionType') == 'max',
          'Supported Max subscription authentication required')
    account = hashlib.sha256(status['email'].strip().lower().encode()).hexdigest()
    check(account == coverage['account_sha256'], 'CLI account differs from checked billing account')
    args.output.mkdir(parents=True, exist_ok=False)
    report = {'schema': 1, 'started_ns': time.time_ns(), 'client': version,
              'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'coverage_sha256': hashlib.sha256(args.coverage_evidence.read_bytes()).hexdigest(),
              'runtime': runtime_identity(), 'fixture': str(root), 'runs': [],
              'release_acceptance': False, 'additional_spend_authorized_usd': 0}
    text = b'Synthetic protocol: the nebula codec requires UTF-8. Corrected requirement: UTF-16.'
    (workspace_root / 'contract.txt').write_bytes(text)
    Workspace.initialize(workspace_root, [{'id': 'p', 'root': '.', 'kind': 'project',
        'purpose': 'public synthetic client fixture', 'owners': ['fixture-owner'], 'depends_on': []}])
    workspace = Workspace(workspace_root, home)
    enroll(home, workspace.id, {'repo:p'}, project='p')
    settings = {'autoMemoryEnabled': False, 'claudeMdExcludes': ['**'], 'disableAllHooks': True}
    config = {'mcpServers': {'lumen': {'type': 'stdio', 'command': sys.executable,
                                      'args': ['-m', 'lumen', '--home', str(home), 'mcp']}}}
    (root / 'settings.json').write_text(json.dumps(settings))
    (root / 'mcp.json').write_text(json.dumps(config))
    report['settings_digest'], report['mcp_digest'] = digest(settings), digest(config)
    command = [executable, '--print', '--restricted', '--setting-sources', '',
               '--settings', str(root / 'settings.json'), '--strict-mcp-config',
               '--mcp-config', str(root / 'mcp.json'), '--tools', '',
               '--allowedTools', 'mcp__lumen__memory_recall,mcp__lumen__memory_remember,mcp__lumen__memory_revise,mcp__lumen__memory_expand',
               '--disable-slash-commands', '--no-chrome', '--permission-mode', 'dontAsk',
               '--output-format', 'stream-json', '--verbose', '--model', 'sonnet', '--effort', 'low']
    report['command'] = command
    daemon = subprocess.Popen([sys.executable, '-m', 'lumen', '--home', str(home), 'daemon',
                               '--workspace', str(workspace_root)], env=env, cwd=workspace_root,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for attempt in range(100):
            try:
                check(call(home, 'doctor', {})['result']['healthy'], 'Unhealthy daemon')
                break
            except Exception:
                check(daemon.poll() is None and attempt < 99, 'Daemon did not start')
                time.sleep(.05)

        def run(name, prompt):
            started = time.time_ns()
            process = subprocess.run(command + ['--', prompt], cwd=workspace_root, env=env,
                                     capture_output=True, timeout=180)
            (args.output / (name + '.jsonl')).write_bytes(process.stdout)
            (args.output / (name + '.stderr')).write_bytes(process.stderr)
            report['runs'].append({'name': name, 'prompt': prompt, 'started_ns': started,
                'finished_ns': time.time_ns(), 'exit_code': process.returncode,
                'stdout_sha256': hashlib.sha256(process.stdout).hexdigest(),
                'stderr_sha256': hashlib.sha256(process.stderr).hexdigest()})
            check(process.returncode == 0, 'Client failed; inspect local output')
            messages = [json.loads(line) for line in process.stdout.splitlines() if line.strip()]
            init = next(m for m in messages if m.get('type') == 'system' and m.get('subtype') == 'init')
            tools = set(init.get('tools', []))
            check(tools and tools <= {'mcp__lumen__memory_recall', 'mcp__lumen__memory_remember',
                                     'mcp__lumen__memory_revise', 'mcp__lumen__memory_expand', 'EndConversation'},
                  'Unexpected tools; configured isolation not established')
            check(any(m.get('type') == 'result' and not m.get('is_error') for m in messages),
                  'No successful client result')
            return messages

        fields = dict(checkout=workspace.checkout, host='claude-code', session='synthetic-1', turn='1',
            scope='repo:p', subject='nebula codec', relation='required_encoding', value='UTF-8',
            text='The nebula codec requires UTF-8.', region=region(start=0), citations=[{
                'kind': 'file', 'project': 'p', 'path': 'contract.txt', 'revision': 'synthetic',
                'sha256': hashlib.sha256(text).hexdigest(), 'anchor': 'Synthetic protocol'}])
        captured = run('capture', 'Use memory_remember exactly once with these explicit synthetic arguments. '
            'Then report the returned event ID. Do not use other tools.\n' + json.dumps(fields))
        def tool_calls(messages, name):
            return [block for message in messages if message.get('type') == 'assistant'
                    for block in message.get('message', {}).get('content', [])
                    if block.get('type') == 'tool_use' and block.get('name') == name]
        check(len(tool_calls(captured, 'mcp__lumen__memory_remember')) == 1, 'Capture tool did not execute once')
        recalled = call(home, 'recall', {'query': 'nebula codec', 'at': 1})
        check(any(a.get('value') == 'UTF-8' for r in recalled['result']['results']
                  for a in r['assertions']), 'Capture not durable')
        check(any(receipt['fresh'] for r in recalled['result']['results'] for receipt in r['receipts']),
              'No fresh source receipt')
        restarted = run('restart-recall', 'Use memory_recall to find the nebula codec required encoding at time 1. '
            'State the value and its source citation. Do not remember or revise anything.')
        check(tool_calls(restarted, 'mcp__lumen__memory_recall'), 'Restarted client did not recall')
        final = next(m for m in restarted if m.get('type') == 'result')['result']
        check('UTF-8' in final and 'contract.txt' in final, 'Client omitted the correct value or source citation')
        report['verification'] = recalled
        successor = {key: fields[key] for key in ('scope', 'subject', 'relation', 'value', 'text', 'region', 'citations')}
        successor.update(value='UTF-16', text='The corrected nebula codec requirement is UTF-16.')
        corrected = run('correction', 'First recall the nebula codec at time 1. Then explicitly correct '
            'the old requirement using memory_revise exactly once. Use the returned assertion ID as '
            'predecessor and its complete state_token as expected_state. revision_kind is correction, '
            'reason is synthetic protocol correction, affected is the successor region. '
            'Use this successor, retaining its citation. Do not call memory_remember.\n' + json.dumps(successor))
        check(len(tool_calls(corrected, 'mcp__lumen__memory_revise')) == 1, 'Correction did not use revision once')
        check(not tool_calls(corrected, 'mcp__lumen__memory_remember'), 'Correction bypassed explicit revision')
        now = call(home, 'recall', {'query': 'nebula codec', 'at': 1})
        values = {a['value'] for r in now['result']['results'] for a in r['assertions']}
        check(values == {'UTF-16'}, 'Correction did not replace the affected value')
        history = call(home, 'recall', {'query': 'nebula codec', 'at': 1,
                                      'known_at': recalled['result']['snapshot']})
        check({a['value'] for r in history['result']['results'] for a in r['assertions']} == {'UTF-8'},
              'Correction destroyed historical knowledge')
        after = run('restart-corrected', 'Recall the nebula codec required encoding at time 1 and state '
                    'the current value with its source citation. Do not modify memory.')
        check(tool_calls(after, 'mcp__lumen__memory_recall'), 'Fresh client did not recall correction')
        final = next(m for m in after if m.get('type') == 'result')['result']
        check('UTF-16' in final and 'contract.txt' in final, 'Fresh client omitted correction or citation')
        original_id = recalled['result']['results'][0]['assertions'][0]['id']
        erased = call(home, 'purge', {'eid': original_id}, owner=True)
        check('error' not in erased, 'Explicit owner erasure failed')
        check(not call(home, 'recall', {'query': 'nebula codec', 'at': 1})['result']['results'],
              'Erased lineage remains retrievable')
        after_erasure = run('restart-erased', 'Recall the nebula codec required encoding at time 1. '
            'If no evidence is available, say it is unavailable. Do not guess or modify memory.')
        check(tool_calls(after_erasure, 'mcp__lumen__memory_recall'), 'Fresh client did not check after erasure')
        final = next(m for m in after_erasure if m.get('type') == 'result')['result']
        check('UTF-8' not in final and 'UTF-16' not in final, 'Fresh client repeated erased value')
        report['correction_verification'], report['historical_verification'] = now, history
        report['erasure_verification'] = erased
        report['passed'] = True
    except Exception as exc:
        report['passed'] = False
        report['failure'] = type(exc).__name__ + ': ' + str(exc)
    finally:
        daemon.terminate()
        _, stderr = daemon.communicate(timeout=10)
        (args.output / 'daemon.stderr').write_bytes(stderr)
        report['finished_ns'] = time.time_ns()
        (args.output / 'report.json').write_text(json.dumps(report, indent=2))
    print(json.dumps({'passed': report['passed'], 'report': str(args.output / 'report.json'),
                      'release_acceptance': False}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
