import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from lumen.hook import MAX_OUTPUT, blocked, deliver, dispatch, parse, transcript_path_ok, workspace

HOOK = Path(__file__).resolve().parents[1] / "src" / "lumen" / "hook.py"


def fixture(root):
    (root / ".lumen").mkdir(parents=True)
    (root / ".lumen" / "workspace.json").write_text("{}")
    return root


def encode(fields):
    return json.dumps(fields).encode()


def refuse(*_):
    raise AssertionError("Unexpected launch")


class HookLauncherTests(unittest.TestCase):
    def test_no_launch_outside_workspace_or_for_invalid_input(self):
        with tempfile.TemporaryDirectory() as td:
            valid = encode({"hook_event_name": "SessionStart", "session_id": "fixture-1", "cwd": td})
            for data in (valid, b"null", b"[]", b"x" * 262145, b'{"hook_event_name":"PreToolUse"}',
                         b'{"hook_event_name":"SessionStart","session_id":5,"cwd":"' + td.encode().replace(b"\\", b"/") + b'"}'):
                dispatch(data, "claude-code", refuse)

    def test_capture_payload_shape(self):
        with tempfile.TemporaryDirectory() as td:
            root = fixture(Path(td))
            transcript = str(root / "transcript.jsonl")
            base = {"hook_event_name": "Stop", "session_id": "fixture-1", "cwd": str(root),
                    "prompt_id": "550e8400-e29b-41d4-a716-446655440000", "transcript_path": transcript,
                    "last_assistant_message": "done"}
            rejected = [{"prompt_id": ""}, {"prompt_id": "has space"}, {"agent_id": "subagent-1"},
                        {"transcript_path": "relative.jsonl"}, {"transcript_path": str(root / "rollout.json")},
                        {"hook_event_name": "SubagentStop"}, {"hook_event_name": "agentStop"}, {"cwd": 7}]
            for change in rejected:
                dispatch(encode({**base, **change}), "claude-code", refuse)
            expected_root = os.path.realpath(str(root))
            for event, endpoint in (("Stop", "hook-stop"), ("UserPromptSubmit", "hook-prompt")):
                raw = encode({**base, "hook_event_name": event})
                calls = []
                dispatch(raw, "claude-code", lambda *a: calls.append(a))
                self.assertEqual(calls, [(endpoint, expected_root, expected_root, "fixture-1", raw)])

    def test_output_bound(self):
        self.assertEqual(deliver("hook-session", 0, b"x" * MAX_OUTPUT), b"x" * MAX_OUTPUT)
        self.assertEqual(deliver("hook-session", 0, b"x" * (MAX_OUTPUT + 1)), b"")
        self.assertEqual(deliver("hook-session", 1, b"hint"), b"")
        self.assertEqual(deliver("hook-stop", 0, b"hint"), b"")

    def test_outermost_root_and_host_isolation(self):
        with tempfile.TemporaryDirectory() as td:
            root = fixture(Path(td))
            child = fixture(root / "child")
            data = encode({"hook_event_name": "SessionStart", "session_id": "fixture-1", "cwd": str(child)})
            calls = []
            dispatch(data, "claude-code", lambda *a: calls.append(a))
            self.assertEqual(len(calls), 1, (workspace(str(child)), os.path.realpath(str(child))))
            endpoint, found, cwd, session, _ = calls[0]
            self.assertEqual((endpoint, found, cwd, session),
                             ("hook-session", os.path.realpath(str(root)), os.path.realpath(str(child)), "fixture-1"))
            dispatch(data, "copilot-cli", refuse)
            self.assertTrue(blocked(str(root / "archive" / "child")))
            self.assertTrue(blocked("C:/x/.env.local/y"))
            self.assertFalse(blocked(str(root)))

    def test_copilot_shape_routes_by_event_flag_and_rejects_foreign_shapes(self):
        with tempfile.TemporaryDirectory() as td:
            root = fixture(Path(td))
            expected = os.path.realpath(str(root))
            transcript = str(root / "session-state" / "8cbe976d" / "events.jsonl")
            start = {"sessionId": "8cbe976d-efa6-4e71-b944-aa72455d5128", "timestamp": 1789322120456, "cwd": str(root), "source": "new"}
            stop = {**start, "transcriptPath": transcript, "stopReason": "end_turn", "stop_hook_active": False}
            del stop["source"]
            calls = []
            dispatch(encode(start), "copilot-cli", lambda *a: calls.append(a), event="sessionStart")
            dispatch(encode(stop), "copilot-cli", lambda *a: calls.append(a), event="agentStop")
            self.assertEqual([(c[0], c[1], c[2], c[3]) for c in calls],
                             [("hook-session", expected, expected, start["sessionId"]), ("hook-stop", expected, expected, start["sessionId"])])
            rejected = [(start, "agentStop"), (stop, "sessionStart"), (start, "userPromptSubmitted"), (start, ""),
                        ({**start, "hook_event_name": "SessionStart"}, "sessionStart"),
                        ({**start, "session_id": "x"}, "sessionStart"),
                        ({**start, "source": "weird"}, "sessionStart"), ({**start, "sessionId": "has space"}, "sessionStart"),
                        ({**stop, "transcriptPath": "relative.jsonl"}, "agentStop"),
                        ({**stop, "transcriptPath": str(root / "elsewhere" / "events.jsonl")}, "agentStop"),
                        ({**stop, "transcriptPath": transcript[:-6] + ".json"}, "agentStop"),
                        ({**stop, "stopReason": 3}, "agentStop")]
            for payload, event in rejected:
                dispatch(encode(payload), "copilot-cli", refuse, event=event)
            dispatch(encode(start), "claude-code", refuse)
            dispatch(encode(stop), "claude-code", refuse)
            self.assertTrue(transcript_path_ok(transcript))
            self.assertFalse(transcript_path_ok(str(root / "events.jsonl")))

    def test_flag_parsing(self):
        self.assertEqual(parse(["--host", "claude-code", "--home=C:/h", "-marker", "m"]),
                         {"host": "claude-code", "home": "C:/h", "marker": "m"})
        self.assertEqual(parse(["--host"]), {})
        self.assertEqual(parse(["stray"]), {})

    def test_no_workspace_process_exits_silently(self):
        with tempfile.TemporaryDirectory() as td:
            request = encode({"hook_event_name": "SessionStart", "session_id": "fixture-1", "cwd": td})
            for arguments in (["--host", "claude-code", "--python", sys.executable, "--home", td],
                              ["--host", "copilot-cli", "--event", "sessionStart", "--python", sys.executable, "--home", td],
                              ["--host", "claude-code", "--python", "relative", "--home", td],
                              ["--unknown"]):
                result = subprocess.run([sys.executable, "-I", str(HOOK), *arguments], input=request,
                                        capture_output=True, cwd=td, timeout=30)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""), arguments)


if __name__ == "__main__":
    unittest.main()
