"""Bounded native CLI acceptance adapters. No inference API or credential access.

These are test drivers, not runtime Lumen providers. Only synthetic fixtures may be
supplied. UI billing observations must be refreshed before running the drivers.
"""
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time


def require(value, message):
    if not value:
        raise RuntimeError(message)


def machine_environment():
    keep = {'PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT', 'TEMP', 'TMP',
            'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'PROGRAMDATA', 'HOMEDRIVE', 'HOMEPATH'}
    return {key: value for key, value in os.environ.items() if key.upper() in keep}


def skill_overrides(paths):
    return '[' + ','.join('{path=' + json.dumps(path) + ',enabled=false}' for path in paths) + ']'


def codex_account(binary, cwd, env, disabled_skills=None):
    """Only documented read operations; never starts a thread, turn or login."""
    command = [str(binary), 'app-server', '--stdio', '-c', 'mcp_servers={}',
               '-c', 'project_doc_max_bytes=0', '-c', 'forced_login_method="chatgpt"']
    for feature in ('apps', 'plugins', 'hooks', 'multi_agent', 'shell_tool', 'skill_mcp_dependency_install'):
        command.extend(['--disable', feature])
    if disabled_skills is not None:
        command.extend(['-c', 'skills.config=' + skill_overrides(disabled_skills)])
    process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               text=True, encoding='utf-8')
    responses = queue.Queue(maxsize=100)

    def read():
        try:
            for line in process.stdout:
                responses.put_nowait(json.loads(line))
        except (ValueError, queue.Full):
            pass
        finally:
            try:
                responses.put_nowait(None)
            except queue.Full:
                pass

    reader = threading.Thread(target=read, daemon=True)
    reader.start()

    def send(value):
        process.stdin.write(json.dumps(value) + '\n')
        process.stdin.flush()

    def request(identifier, method, params):
        send({'id': identifier, 'method': method, 'params': params})
        deadline = time.monotonic() + 20
        for _ in range(100):
            result = responses.get(timeout=max(.001, deadline - time.monotonic()))
            require(result is not None, 'Native account reader exited')
            if result.get('id') == identifier:
                require('error' not in result, 'Native account method rejected')
                return result['result']
        raise RuntimeError('Excessive native notifications')

    try:
        request(1, 'initialize', {'clientInfo': {'name': 'lumen_account_check', 'version': '0.1.0'}})
        send({'method': 'initialized', 'params': {}})
        account = request(2, 'account/read', {'refreshToken': False}).get('account') or {}
        limits = request(3, 'account/rateLimits/read', {})
        catalogs = request(4, 'skills/list', {'cwds': [str(cwd)], 'forceReload': True}).get('data', [])
        require(len(catalogs) == 1 and not catalogs[0].get('errors'), 'Skill discovery is incomplete')
        skills = catalogs[0].get('skills', [])
        return {'type': account.get('type'), 'planType': account.get('planType'),
                'account_sha256': hashlib.sha256(account['email'].strip().lower().encode()).hexdigest()
                if account.get('email') else None, 'limits': limits.get('rateLimits'),
                'skill_paths': sorted({s['path'] for s in skills}),
                'enabled_skill_paths': sorted({s['path'] for s in skills if s.get('enabled', True)})}
    finally:
        process.terminate()
        process.wait(timeout=10)
        reader.join(timeout=1)
        process.stdin.close()
        process.stdout.close()


TOOLS = ('memory_recall', 'memory_remember', 'memory_revise', 'memory_expand')


class NativeClient:
    def __init__(self, name, binary, coverage_path, cwd, home, python, output):
        require(name in ('claude-code', 'codex-cli', 'copilot-cli'), 'Unqualified client adapter')
        self.name, self.cwd, self.output = name, Path(cwd), Path(output)
        self.binary, self.env = str(Path(binary).resolve()), machine_environment()
        self.binary_sha256 = hashlib.sha256(Path(self.binary).read_bytes()).hexdigest()
        self.helper = Path(self.binary).with_name('codex-code-mode-host.exe') if name == 'codex-cli' else None
        self.helper_sha256 = hashlib.sha256(self.helper.read_bytes()).hexdigest() if self.helper else None
        if name == 'claude-code':
            self.env.update(CLAUDE_CODE_DISABLE_AUTO_MEMORY='1', ENABLE_TOOL_SEARCH='false',
                CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1', CLAUDE_CODE_DISABLE_FAST_MODE='1',
                DISABLE_UPDATES='1', ENABLE_CLAUDEAI_MCP_SERVERS='false')
        elif name == 'copilot-cli':
            # An isolated home: no user hooks, MCP config, instructions or plugins reach the session.
            # The stored login lives in the system credential store and survives the override.
            self.copilot_home = (self.output / 'copilot-home').resolve()
            self.copilot_home.mkdir(parents=True, exist_ok=True)
            self.env.update(COPILOT_HOME=str(self.copilot_home), COPILOT_AUTO_UPDATE='false')
        self.coverage = json.loads(Path(coverage_path).read_text(encoding='utf-8-sig'))
        self.coverage_digest = hashlib.sha256(Path(coverage_path).read_bytes()).hexdigest()
        require(self.coverage['client'] == name, 'Wrong coverage observation')
        self.version = subprocess.run([self.binary, '--version'], env=self.env, cwd=self.cwd,
            capture_output=True, text=True, check=True, timeout=20).stdout.strip()
        require(self.coverage['version'] in [t.rstrip('.') for t in self.version.split()], 'Installed client version differs')
        self.preflight()
        if name == 'claude-code':
            settings = {'autoMemoryEnabled': False, 'claudeMdExcludes': ['**'], 'disableAllHooks': True}
            mcp = {'mcpServers': {'lumen': {'type': 'stdio', 'command': str(python),
                                           'args': ['-m', 'lumen', '--home', str(home), 'mcp']}}}
            self.command = [self.binary, '--print', '--restricted', '--setting-sources', '',
                '--settings', json.dumps(settings), '--strict-mcp-config', '--mcp-config', json.dumps(mcp),
                '--tools', '', '--allowedTools', ','.join('mcp__lumen__' + t for t in TOOLS),
                '--disable-slash-commands', '--no-chrome', '--permission-mode', 'dontAsk',
                '--output-format', 'stream-json', '--verbose', '--model', 'sonnet', '--effort', 'low']
        elif name == 'copilot-cli':
            mcp = {'mcpServers': {'lumen': {'type': 'local', 'command': str(python),
                                           'args': ['-m', 'lumen', '--home', str(home), 'mcp'], 'tools': ['*']}}}
            self.command = [self.binary, '--output-format', 'json', '--no-custom-instructions',
                '--disable-builtin-mcps', '--no-auto-update', '--no-ask-user', '--no-color',
                '--no-remote', '--no-remote-export',
                '--additional-mcp-config', json.dumps(mcp), '--allow-tool', 'lumen',
                '--excluded-tools', 'powershell,bash,create,edit,web_fetch,task,glob,grep,view',
                '--model', 'claude-sonnet-5', '--max-ai-credits', '30', '--effort', 'low',
                '--log-dir', str((self.output / 'copilot-logs').resolve())]
        else:
            settings = {'forced_login_method': 'chatgpt', 'model_provider': 'openai',
                'web_search': 'disabled', 'project_doc_max_bytes': 0, 'approval_policy': 'never',
                'model_reasoning_effort': 'low', 'mcp_servers.lumen.command': str(python),
                'mcp_servers.lumen.args': ['-m', 'lumen', '--home', str(home), 'mcp'],
                'mcp_servers.lumen.enabled_tools': list(TOOLS)}
            for tool in TOOLS:
                settings['mcp_servers.lumen.tools.' + tool + '.approval_mode'] = 'approve'
            self.command = [self.binary, 'exec', '--ignore-user-config', '--ignore-rules',
                '--strict-config', '--skip-git-repo-check', '--sandbox', 'read-only',
                '--json', '--model', 'gpt-5.6-luna']
            for key, value in settings.items():
                self.command.extend(['-c', key + '=' + json.dumps(value)])
            for feature in ('apps', 'plugins', 'hooks', 'multi_agent', 'shell_tool', 'unified_exec',
                            'shell_snapshot', 'browser_use', 'browser_use_external', 'in_app_browser',
                            'image_generation', 'view_image', 'memories',
                            'external_agent_memory_import', 'skill_search', 'skill_mcp_dependency_install'):
                self.command.extend(['--disable', feature])
            self.command.extend(['-c', 'skills.config=' + skill_overrides(self.disabled_skills)])

    def preflight(self):
        require(hashlib.sha256(Path(self.binary).read_bytes()).hexdigest() == self.binary_sha256,
                'Installed client binary changed')
        if self.helper:
            require(hashlib.sha256(self.helper.read_bytes()).hexdigest() == self.helper_sha256,
                    'Installed code-mode helper changed')
        require(0 <= time.time() - self.coverage['observed_unix_seconds'] <= 3600,
                'Refresh account-bound billing observation')
        if self.name == 'claude-code':
            require(self.coverage['usage_credits_enabled'] is False, 'Usage credits must be disabled')
            result = subprocess.run([self.binary, 'auth', 'status'], cwd=self.cwd, env=self.env,
                                    capture_output=True, text=True, check=True, timeout=20)
            auth = json.loads(result.stdout)
            require(auth.get('loggedIn') and auth.get('authMethod') == 'claude.ai'
                    and auth.get('apiProvider') == 'firstParty' and auth.get('subscriptionType') == 'max',
                    'Matched first-party subscription required')
            identity = hashlib.sha256(auth['email'].strip().lower().encode()).hexdigest()
        elif self.name == 'copilot-cli':
            # No non-interactive auth status exists. The plan and paid-usage state come from the
            # UI observation; the account is the one the gh CLI reports, and the session's own
            # usage file is checked after every run. The stored Copilot login is not attested.
            require(self.coverage['plan'] == 'GitHub Copilot Pro', 'Paid Copilot plan required')
            require(self.coverage['additional_usage_enabled'] is False, 'Additional paid usage must be disabled')
            login = subprocess.run(['gh', 'api', 'user', '--jq', '.login'], cwd=self.cwd, env=self.env,
                                   capture_output=True, text=True, check=True, timeout=30, shell=True).stdout.strip()
            identity = hashlib.sha256(login.encode()).hexdigest()
        else:
            require(self.coverage['auto_reload_enabled'] is False, 'Auto reload must be disabled')
            auth = codex_account(self.binary, self.cwd, self.env, getattr(self, 'disabled_skills', None))
            if not hasattr(self, 'disabled_skills'):
                self.disabled_skills = auth['skill_paths']
            else:
                require(not auth['enabled_skill_paths'], 'Skill isolation failed; no inference permitted')
            require(auth['type'] == 'chatgpt' and auth['planType'] not in (None, 'free'),
                    'Supported paid subscription required')
            limits = auth['limits'] or {}
            require(limits.get('credits', {}).get('hasCredits') is False,
                    'Paid-credit execution is not authorized')
            require(limits.get('primary', {}).get('usedPercent', 100) < 98,
                    'Subscription reserve reached; continue local work')
            identity = auth['account_sha256']
        require(identity == self.coverage['account_sha256'], 'Account differs from checked billing account')

    def run(self, label, prompt):
        self.preflight()
        prefix = self.output / (self.name + '-' + label)
        stdout, stderr = prefix.with_suffix('.jsonl'), prefix.with_suffix('.stderr')
        require(not stdout.exists() and not stderr.exists(), 'Evidence already exists')
        if self.name == 'copilot-cli':
            usage_file = prefix.with_suffix('.usage.json').resolve()
            command = self.command + ['--usage-output-file', str(usage_file), '-p', prompt]
        else:
            command = self.command + ['--', prompt]
        record = {'client': self.name, 'version': self.version, 'command': command,
                  'binary_sha256': self.binary_sha256,
                  'helper_sha256': self.helper_sha256,
                  'coverage_sha256': self.coverage_digest, 'started_ns': time.time_ns()}
        with stdout.open('wb') as out, stderr.open('wb') as err:
            process = subprocess.Popen(command, cwd=self.cwd, env=self.env,
                                       stdin=subprocess.DEVNULL, stdout=out, stderr=err)
            deadline = time.monotonic() + 180
            try:
                while process.poll() is None:
                    require(time.monotonic() < deadline, 'Native client timed out')
                    require(stdout.stat().st_size + stderr.stat().st_size <= 16 * 1024 * 1024,
                            'Native output exceeded bound')
                    time.sleep(.05)
            finally:
                if process.poll() is None:
                    process.terminate()
                record.update(exit_code=process.wait(timeout=10), finished_ns=time.time_ns())
                prefix.with_suffix('.run.json').write_text(json.dumps(record, indent=2))
        require(record['exit_code'] == 0, 'Native client failed; inspect retained output')
        messages = [json.loads(line) for line in stdout.read_text(encoding='utf-8').splitlines() if line.strip()]
        calls, answer = [], None
        if self.name == 'claude-code':
            init = next(m for m in messages if m.get('type') == 'system' and m.get('subtype') == 'init')
            require(set(init['tools']) == {'mcp__lumen__' + t for t in TOOLS}
                    and not init.get('plugins') and not init.get('skills') and init.get('apiKeySource') == 'none',
                    'Claude isolation configuration did not take effect')
            for message in messages:
                if message.get('type') == 'assistant':
                    calls.extend({'name': b['name'].removeprefix('mcp__lumen__'), 'arguments': b['input']}
                        for b in message.get('message', {}).get('content', []) if b.get('type') == 'tool_use')
                if message.get('type') == 'result':
                    require(not message.get('is_error'), 'Native result failed')
                    answer = message.get('result')
        elif self.name == 'copilot-cli':
            loaded = next(m for m in messages if m.get('type') == 'session.mcp_servers_loaded')
            servers = {s['name']: s['status'] for s in loaded['data']['servers']}
            # The loaded snapshot can still show lumen as pending; the status_changed event settles it.
            connected = servers.get('lumen') == 'connected' or any(
                m.get('type') == 'session.mcp_server_status_changed' and m['data'] == {'serverName': 'lumen', 'status': 'connected'}
                for m in messages)
            require(set(servers) == {'github-mcp-server', 'lumen'} and servers['github-mcp-server'] == 'disabled' and connected,
                    'Copilot MCP isolation did not take effect')
            require(any(m.get('type') == 'session.info' and m['data'].get('message', '').startswith('Disabled tools: create, edit, glob, grep, powershell, task, view, web_fetch')
                        for m in messages), 'Copilot tool exclusion did not take effect')
            outcomes = {m['data']['toolCallId']: m['data'] for m in messages if m.get('type') == 'tool.execution_complete'}
            for message in messages:
                if message.get('type') == 'tool.execution_start':
                    data = message['data']
                    require(data.get('mcpServerName') == 'lumen', 'Unexpected native tool')
                    outcome = outcomes.get(data['toolCallId'], {})
                    require(outcome.get('success') is True, 'MCP call failed')
                    calls.append({'name': data['mcpToolName'], 'arguments': data.get('arguments'),
                                  'result': outcome.get('result')})
                elif message.get('type') == 'assistant.message' and message['data'].get('content'):
                    answer = message['data']['content']
            final = next(m for m in messages if m.get('type') == 'result')
            require(final.get('exitCode') == 0, 'Copilot result failed')
            usage = json.loads(usage_file.read_text(encoding='utf-8'))
            record['usage'] = {'premium_requests': final['usage'].get('premiumRequests'),
                               'total_premium_request_cost': usage.get('totalPremiumRequestCost'),
                               'total_nano_aiu': usage.get('totalNanoAiu'), 'model_metrics': list(usage.get('modelMetrics', {}))}
            require(set(record['usage']['model_metrics']) <= {'claude-sonnet-5'}, 'Unexpected Copilot model')
        else:
            require(any(m.get('type') == 'turn.completed' for m in messages), 'Codex turn incomplete')
            for message in messages:
                if message.get('type') != 'item.completed':
                    continue
                item = message['item']
                if item.get('type') == 'mcp_tool_call':
                    require(item.get('server') == 'lumen', 'Unexpected MCP server')
                    require(item.get('status') == 'completed', 'MCP call failed')
                    calls.append({'name': item['tool'], 'arguments': item.get('arguments'),
                                  'result': item.get('result'), 'error': item.get('error')})
                elif item.get('type') == 'agent_message':
                    answer = item.get('text')
                else:
                    require(item.get('type') in ('reasoning', 'todo_list'), 'Unexpected native tool/item')
        require(answer is not None and all(c['name'] in TOOLS for c in calls), 'Unexpected or absent native result')
        return {'calls': calls, 'answer': answer, 'run': record,
                'stdout_sha256': hashlib.sha256(stdout.read_bytes()).hexdigest(),
                'stderr_sha256': hashlib.sha256(stderr.read_bytes()).hexdigest()}
