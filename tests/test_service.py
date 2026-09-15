import io
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import lumen

from lumen.daemon import call, enroll, ownership, endpoint, AUTH_TIMEOUT
from lumen.mcp import serve_stdio
from lumen.model import LumenError, region
from lumen.model import digest


class ServiceTests(unittest.TestCase):
    def test_mcp_process_survives_deep_json_and_malformed_envelopes(self):
        malformed = ['[' * 2000 + '0' + ']' * 2000, 'null', '[]', 'false',
                     '{"jsonrpc":"2.0","id":1,"method":"initialize","params":[]}']
        frames = []
        for index, bad in enumerate(malformed):
            frames.extend([bad, json.dumps({'jsonrpc': '2.0', 'id': 100 + index, 'method': 'ping'})])
        frames.extend([json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized'}),
                       json.dumps({'jsonrpc': '2.0', 'id': 200, 'method': 'tools/list'})])
        process = subprocess.run([sys.executable, '-m', 'lumen', '--home', 'unused', 'mcp'],
                                 input='\n'.join(frames) + '\n', capture_output=True,
                                 text=True, timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)
        responses = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual(len(responses), 2 * len(malformed) + 1)
        for index in range(len(malformed)):
            self.assertEqual(responses[2 * index]['error']['code'], -32600)
            self.assertEqual(responses[2 * index + 1]['id'], 100 + index)
            self.assertEqual(responses[2 * index + 1]['result'], {})
        self.assertEqual(responses[-1]['id'], 200)
        self.assertEqual(responses[-1]['error']['code'], -32600)

    def test_mcp_requires_initialization_exchange_and_discards_oversized_line(self):
        from lumen.daemon import MAX_FRAME
        requests = [
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
            {"method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/list"},
        ]
        output = io.StringIO()
        serve_stdio('unused', io.StringIO('\n'.join(json.dumps(r) for r in requests)), output)
        replies = {r['id']: r for r in map(json.loads, output.getvalue().splitlines()) if r['id'] is not None}
        self.assertIn('error', replies[1])
        self.assertIn('error', replies[3])
        self.assertEqual(len(replies[4]['result']['tools']), 4)
        output = io.StringIO()
        tail = json.dumps({'jsonrpc': '2.0', 'id': 99, 'method': 'ping'})
        serve_stdio('unused', io.StringIO('x' * (MAX_FRAME + 1) + tail + '\n' + tail + '\n'), output)
        replies = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(replies), 2)
        self.assertIn('error', replies[0])
        self.assertEqual(replies[1]['id'], 99)
        from unittest.mock import patch
        requests = [requests[2], requests[5], {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                    "params": {"name": "memory_expand", "arguments": {"eid": "fixture"}}}]
        output = io.StringIO()
        with patch('lumen.mcp.call', return_value={'text': 'x' * MAX_FRAME}):
            serve_stdio('unused', io.StringIO('\n'.join(json.dumps(r) for r in requests)), output)
        final = output.getvalue().splitlines()[-1]
        self.assertLess(len(final.encode()), MAX_FRAME)
        self.assertEqual(json.loads(final)['error']['data']['code'], 'budget_exhausted')

    def test_mcp_handshake_four_tools_and_no_management(self):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "purge"}},
        ]
        output = io.StringIO()
        serve_stdio("unused", io.StringIO("\n".join(json.dumps(r) for r in requests)), output)
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(responses[0]["result"]["protocolVersion"], "2025-11-25")
        self.assertEqual({t["name"] for t in responses[1]["result"]["tools"]},
                         {"memory_recall", "memory_remember", "memory_revise", "memory_expand"})
        self.assertIn("error", responses[2])

    def test_real_daemon_pipe_and_authority(self):
        with tempfile.TemporaryDirectory() as td:
            enroll(td, "w", {"repo:p"}, project='p')
            from support_sources import source_workspace, citation
            workspace = source_workspace(Path(td) / 'sources')
            env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(Path(lumen.__file__).resolve().parent.parent), os.environ.get("PYTHONPATH", "")])}
            process = subprocess.Popen([sys.executable, "-m", "lumen", "--home", td, "daemon", '--workspace', str(workspace.root)],
                                       env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                ready = False
                for _ in range(100):
                    if process.poll() is not None:
                        break
                    try:
                        ready = call(td, "doctor", {})["result"]["healthy"]
                        break
                    except (LumenError, FileNotFoundError):
                        time.sleep(0.05)
                self.assertTrue(ready)
                for _ in range(2):
                    hook = subprocess.run([sys.executable, '-m', 'lumen', '--home', td, 'hook-session',
                        '--workspace', str(workspace.root), '--cwd', str(workspace.root / 'p'),
                        '--session', 'hook-fixture', '--host', 'claude-code'],
                        env=env, capture_output=True, text=True, timeout=5)
                    self.assertEqual(hook.returncode, 0, hook.stdout + hook.stderr)
                    context = json.loads(hook.stdout)['hookSpecificOutput']
                    self.assertEqual(context['hookEventName'], 'SessionStart')
                    self.assertIn('memory_recall', context['additionalContext'])
                    self.assertLess(len(hook.stdout.encode()), 8192)
                for invalid in ({'session': 's', 'project': 'p', 'workspace': 'other'},
                                {'session': 's', 'project': 'private'},
                                {'session': [], 'project': 'p'}):
                    self.assertIn('error', call(td, 'session_start', invalid))
                import multiprocessing.connection as ipc
                address, family = endpoint(td)
                # Only the disposable enrollment created by this test is read.
                fixture_auth = json.loads((Path(td) / 'transport.json').read_text())
                invalid_frames = [b'null', b'0', b'[{}]',
                                  b'["token","operation","arguments"]',
                                  b'[' * 2000 + b'0' + b']' * 2000]
                for fields in [dict(token='\u00e9', operation='doctor', arguments={}),
                               dict(token=fixture_auth['agent'], operation=[], arguments={}),
                               dict(token=fixture_auth['agent'], operation='doctor', arguments=None)]:
                    invalid_frames.append(json.dumps(fields).encode())
                for frame in invalid_frames:
                    with ipc.Client(address, family=family,
                                    authkey=bytes.fromhex(fixture_auth['transport'])) as malformed:
                        malformed.send_bytes(frame)
                        try:
                            closed = malformed.poll(2)
                        except OSError:
                            closed = True  # The daemon already closed the rejected connection.
                        self.assertTrue(closed)
                        with self.assertRaises((EOFError, OSError)):
                            malformed.recv_bytes(1048576)
                    self.assertTrue(call(td, 'doctor', {})['result']['healthy'])
                    self.assertIsNone(process.poll())
                with ipc.Client(address, family=family, authkey=None) as stalled:
                    self.assertTrue(stalled.poll(2))
                    stalled.recv_bytes(256)  # Read the challenge but never answer it.
                    started = time.monotonic()
                    self.assertTrue(call(td, 'doctor', {}, timeout=AUTH_TIMEOUT + 5)['result']['healthy'])
                    self.assertLess(time.monotonic() - started, AUTH_TIMEOUT + 4)
                self.assertIsNone(process.poll())
                with self.assertRaises(ipc.AuthenticationError):
                    ipc.Client(address, family=family, authkey=b'intentionally-wrong-fixture-key')
                self.assertTrue(call(td, 'doctor', {})['result']['healthy'])
                with self.assertRaises(LumenError):
                    with ownership(td):
                        pass
                args = dict(checkout="co", host="mcp", session="s", turn="t", scope="repo:p", subject="codec",
                            relation="required_encoding", value="UTF-8", text="codec UTF-8", region=region(start=0))
                (Path(td) / "contract.txt").write_bytes(b"codec UTF-8")
                args["citations"] = [citation()]
                result = call(td, "remember", args)
                self.assertNotIn("error", result, result)
                eid = result["result"]["id"]
                explanation = subprocess.run([sys.executable, "-m", "lumen", "--home", td, "why", eid],
                    env=env, capture_output=True, text=True)
                self.assertEqual(explanation.returncode, 0, explanation.stderr)
                self.assertEqual(json.loads(explanation.stdout)["result"]["events"][0]["id"], eid)
                audit = subprocess.run([sys.executable, "-m", "lumen", "--home", td, "audit", "--ci"],
                    env=env, capture_output=True, text=True)
                self.assertEqual(audit.returncode, 1)
                self.assertFalse(json.loads(audit.stdout)["passed"])
                self.assertIn('repository_review_authority', json.loads(audit.stdout)['result']['checks'])
                self.assertEqual(call(td, "expand", {"eid": eid})["result"]["event"]["origin"], "agent-observed")
                self.assertEqual(call(td, "purge", {"eid": eid})["error"]["code"], "not_authorized")
                self.assertEqual(call(td, "remember", {**args, "turn": "forged", "scope": "user"})["error"]["code"], "not_authorized")
                proposal_args = {"action": "propose", "ids": [eid]}
                self.assertEqual(call(td, "share", proposal_args)["error"]["code"], "not_authorized")
                bundle = call(td, "share", proposal_args, owner=True)["result"]
                approval_args = {"action": "approve", "bundle": bundle, "reviewed_digest": digest(bundle)}
                self.assertEqual(call(td, "share", approval_args)["error"]["code"], "not_authorized")
                self.assertNotIn("error", call(td, "share", approval_args, owner=True))
                staged = call(td, "share", {"action": "stage", "bundle": bundle,
                              "directory": str(Path(td) / "prepared")}, owner=True)
                self.assertEqual(staged["result"]["staged"], 1)
                self.assertTrue((Path(td) / "prepared" / "p" / (eid + ".md")).is_file())
                inspect_args = {"action": "inspect", "directory": str(Path(td) / "prepared")}
                self.assertEqual(call(td, "share", inspect_args)["error"]["code"], "not_authorized")
                inspected = call(td, "share", inspect_args, owner=True)["result"]
                self.assertFalse(inspected["admitted"])
                self.assertEqual(inspected["bundle"], bundle)
                self.assertEqual(inspected["reviewed_digest"], digest(bundle))
                self.assertNotIn("error", call(td, "purge", {"eid": eid}, owner=True))
                self.assertIsNone(call(td, "expand", {"eid": eid})["result"]["event"])
            finally:
                process.terminate()
                stdout, stderr = process.communicate(timeout=10)
                self.assertFalse(stderr, stderr.decode())


if __name__ == "__main__":
    unittest.main()
