"""Capture at stop: episodes, dedupe, redaction, bounds, erasure, sharing, recovery, hooks settings, CLI."""
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
from lumen.daemon import call, enroll
from lumen.hosts import claude_handlers, copilot_handlers, copilot_turn, manage_copilot_hooks, manage_hooks
from lumen.model import Access, LumenError, canonical
from lumen.service import Service
from lumen.sharing import proposal
from lumen.store import Store, safe_component
from lumen.workspace import Workspace
from test_core import OWNER

AGENT = Access("w", OWNER.scopes, OWNER.actor, False)
IDENTITY = dict(checkout="co", host="claude-code", session="s1", project="p")
SECRET = "sk-lumenfixture0123456789abcdef"


def prompt(store, turn="t1", text="Which encoding? token sk-lumenfixture0123456789abcdef please", access=AGENT):
    return store.capture_prompt(access, turn=turn, text=text, **IDENTITY)


def stop(store, turn="t1", assistant="UTF-32, per contract.txt.", access=AGENT):
    return store.capture_stop(access, turn=turn, assistant=assistant, **IDENTITY)


def episodes(store):
    return [e for e in store.events(OWNER) if e["kind"] == "episode"]


class CaptureStoreTests(unittest.TestCase):
    def test_paired_episode_is_redacted_deduped_and_conflicts_are_counted(self):
        with tempfile.TemporaryDirectory() as td, Store(td) as store:
            self.assertEqual(prompt(store)["state"], "pending")
            self.assertEqual(prompt(store)["state"], "pending")
            first = stop(store)
            self.assertEqual(first["state"], "paired")
            self.assertEqual(stop(store)["id"], first["id"])
            self.assertEqual(prompt(store)["state"], "already_captured")
            captured = episodes(store)
            self.assertEqual(len(captured), 1)
            event = captured[0]
            self.assertEqual(event["user"], "Which encoding? token [REDACTED] please")
            self.assertEqual(event["assistant"], "UTF-32, per contract.txt.")
            self.assertEqual(event["scope"], "repo:p")
            self.assertEqual((event["host"], event["session"], event["turn"], event["project"]), ("claude-code", "s1", "t1", "p"))
            self.assertEqual(event["bytes"]["user"]["sha256"], hashlib.sha256(event["user"].encode()).hexdigest())
            self.assertFalse(event["bytes"]["assistant"]["truncated"])
            self.assertNotIn(SECRET, (Path(td) / "ledger.db").read_bytes().decode("utf-8", "ignore"))
            with self.assertRaises(LumenError):
                stop(store, assistant="a different answer")
            self.assertEqual(store.capture_status("w"), {"pending_prompts": 0, "capture_conflicts": 1, "duplicate_deliveries": 1})
            self.assertEqual(len(episodes(store)), 1)
            self.assertEqual(store.expand(OWNER, first["id"])["event"]["id"], first["id"])
            store.drain_index(OWNER)
            self.assertTrue(store.doctor()["healthy"])
            with self.assertRaises(LumenError):
                stop(store, turn="other-project", access=Access("w", frozenset({"repo:q"}), OWNER.actor, False))

    def test_missing_prompt_interrupted_flush_and_bounds(self):
        with tempfile.TemporaryDirectory() as td, Store(td) as store:
            self.assertEqual(stop(store, turn="lonely")["state"], "prompt_missing")
            prompt(store, turn="a", text="first question")
            prompt(store, turn="b", text="second question")
            result = stop(store, turn="b", assistant="second answer")
            self.assertEqual(result["state"], "paired")
            self.assertEqual(len(result["flushed"]), 1)
            states = {e["turn"]: e["state"] for e in episodes(store)}
            self.assertEqual(states, {"lonely": "prompt_missing", "a": "interrupted", "b": "paired"})
            interrupted = next(e for e in episodes(store) if e["turn"] == "a")
            self.assertEqual((interrupted["user"], interrupted["assistant"]), ("first question", None))
            prompt(store, turn="c", text="third question")
            self.assertEqual(len(store.flush_session(AGENT, checkout="co", host="claude-code", session="s1")["flushed"]), 1)
            self.assertEqual(store.capture_status("w")["pending_prompts"], 0)
            long = "x" * 70000
            oversized = stop(store, turn="big", assistant=long)
            event = store.expand(OWNER, oversized["id"])["event"]
            self.assertEqual(len(event["assistant"].encode()), 65536)
            self.assertEqual(event["bytes"]["assistant"], {"length": 70000, "truncated": True,
                             "sha256": hashlib.sha256(long.encode()).hexdigest()})
            with self.assertRaises(LumenError):
                store.capture_prompt(AGENT, turn="t", text="", **IDENTITY)
            with self.assertRaises(LumenError):
                store.capture_stop(AGENT, turn="t", assistant=None, **IDENTITY)

    def test_purge_blocks_recapture_sharing_refuses_and_export_restores(self):
        with tempfile.TemporaryDirectory() as td:
            with Store(td) as store:
                prompt(store)
                eid = stop(store)["id"]
                prompt(store, turn="t2", text="pending prompt")
                store.reindex(OWNER)
                with self.assertRaises(LumenError) as refused:
                    proposal(store, OWNER, [eid])
                self.assertEqual(refused.exception.code, "not_authorized")
                bundle_path = Path(td) / "exports" / "bundle.json"
                store.export(OWNER, bundle_path)
                bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
                journal = store.journal.snapshot()
                store.purge(OWNER, eid)
                # Purge rewrites the managed export in place; the erased episode is gone from disk.
                self.assertNotIn("episode", bundle_path.read_text(encoding="utf-8"))
                with self.assertRaises(LumenError):
                    stop(store)
                with self.assertRaises(LumenError):
                    prompt(store)
                self.assertEqual(episodes(store), [])
                self.assertEqual(store.capture_status("w")["pending_prompts"], 1)
            with tempfile.TemporaryDirectory() as fresh_home, Store(fresh_home) as fresh:
                fresh.restore(OWNER, bundle, journal, journal["digest"])
                self.assertEqual([e["id"] for e in episodes(fresh)], [eid])
                fresh.reindex(OWNER)
                self.assertTrue(fresh.doctor()["healthy"])

    def test_actual_process_death_at_capture_stop_transitions(self):
        code = """
import os,sys
from lumen.store import Store
from test_capture import prompt, stop
with Store(sys.argv[1]) as store:
    prompt(store)
    store.fault=lambda p: os._exit(77) if p==sys.argv[2] else None
    stop(store)
"""
        env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(Path(lumen.__file__).resolve().parent.parent), str(Path(__file__).resolve().parent), os.environ.get("PYTHONPATH", "")])}
        for point in ["before_event", "after_event", "after_outbox", "after_idempotency", "before_commit", "after_commit"]:
            with self.subTest(point=point), tempfile.TemporaryDirectory() as td:
                completed = subprocess.run([sys.executable, "-c", code, td, point], env=env, capture_output=True)
                self.assertEqual(completed.returncode, 77, completed.stderr.decode())
                with Store(td) as store:
                    self.assertEqual(len(episodes(store)), int(point == "after_commit"))
                    self.assertEqual(store.capture_status("w")["pending_prompts"], int(point != "after_commit"))
                    first = stop(store)
                    self.assertEqual(stop(store)["id"], first["id"])
                    self.assertEqual(len(episodes(store)), 1)
                    self.assertEqual(episodes(store)[0]["state"], "paired")
                    store.reindex(OWNER)
                    self.assertTrue(store.doctor()["healthy"])

    def test_service_binds_capture_to_workspace_and_grant(self):
        with tempfile.TemporaryDirectory() as td, Store(td) as store:
            service = Service(store)
            good = {**IDENTITY, "turn": "t1", "workspace": "w"}
            self.assertIn("error", service.call(AGENT, "capture_prompt", {**good, "text": "hello", "workspace": "other"}))
            self.assertIn("error", service.call(AGENT, "capture_prompt", {**good, "text": "hello", "project": "q"}))
            self.assertIn("error", service.call(AGENT, "capture_prompt", {**good, "text": "hello", "extra": 1}))
            self.assertIn("error", service.call(AGENT, "capture_stop", {**good, "assistant": ""}))
            self.assertEqual(service.call(AGENT, "capture_prompt", {**good, "text": "hello"})["result"]["state"], "pending")
            self.assertEqual(service.call(AGENT, "capture_stop", {**good, "assistant": "world"})["result"]["state"], "paired")
            doctor = service.call(AGENT, "doctor", {})["result"]
            self.assertEqual(doctor["capture"], {"pending_prompts": 0, "capture_conflicts": 0, "duplicate_deliveries": 0})


class EpisodeFileTests(unittest.TestCase):
    def path(self, td):
        return Path(td) / "episodes" / "claude-code" / "s1.jsonl"

    def test_capture_writes_one_line_and_doubled_hook_keeps_one(self):
        with tempfile.TemporaryDirectory() as td, Store(td) as store:
            prompt(store)
            eid = stop(store)["id"]
            self.assertEqual(stop(store)["id"], eid)
            lines = self.path(td).read_bytes().splitlines()
            self.assertEqual(lines, [canonical(episodes(store)[0])])
            self.assertEqual(json.loads(lines[0])["id"], eid)
            self.assertNotIn(SECRET, lines[0].decode())
            store.reindex(OWNER)
            self.assertEqual(store.doctor()["episodes"], {"files": 1, "lines": 1, "stale": 0})

    def test_purge_removes_lines_and_a_killed_purge_finishes_on_reopen(self):
        with tempfile.TemporaryDirectory() as td:
            with Store(td) as store:
                prompt(store)
                first = stop(store)["id"]
                prompt(store, turn="t2")
                second = stop(store, turn="t2")["id"]
                self.assertEqual(len(self.path(td).read_bytes().splitlines()), 2)
                store.reindex(OWNER)
                store.purge(OWNER, first)
                lines = self.path(td).read_bytes().splitlines()
                self.assertEqual([json.loads(l)["id"] for l in lines], [second])
                erased_line = canonical({**episodes(store)[0], "id": first})
                store.purge(OWNER, second)
                self.assertFalse(self.path(td).exists())
                self.assertFalse(self.path(td).parent.exists())
                # Simulate a purge killed after its journal commit but before file cleanup.
                self.path(td).parent.mkdir(parents=True)
                self.path(td).write_bytes(erased_line + b"\n")
            with Store(td) as reopened:
                self.assertFalse(self.path(td).exists())
                self.assertTrue(reopened.doctor()["healthy"])
                # A line the journal does not know is a stale projection: loud, and reindex repairs it.
                self.path(td).parent.mkdir(parents=True)
                self.path(td).write_bytes(b'{"id": "unknown", "workspace": "w"}\n')
                doctor = reopened.doctor()
                self.assertIn("episode_projection_stale", doctor["failures"])
                reopened.reindex(OWNER)
                self.assertTrue(reopened.doctor()["healthy"])
                self.assertFalse(self.path(td).exists())

    def test_reindex_rebuilds_after_loss_and_restore_reproduces(self):
        with tempfile.TemporaryDirectory() as td:
            with Store(td) as store:
                prompt(store)
                stop(store)
                original = self.path(td).read_bytes()
                import shutil
                shutil.rmtree(Path(td) / "episodes")
                self.assertEqual(store.reindex(OWNER)["episode_lines"], 1)
                self.assertEqual(self.path(td).read_bytes(), original)
                bundle_path = Path(td) / "exports" / "bundle.json"
                store.export(OWNER, bundle_path)
                bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
                journal = store.journal.snapshot()
            with tempfile.TemporaryDirectory() as fresh_home, Store(fresh_home) as fresh:
                fresh.restore(OWNER, bundle, journal, journal["digest"])
                fresh.reindex(OWNER)
                self.assertEqual(self.path(fresh_home).read_bytes(), original)

    def test_unsafe_path_components_are_mapped(self):
        self.assertEqual(safe_component("claude-code"), "claude-code")
        self.assertEqual(safe_component("550e8400-e29b-41d4-a716-446655440000"), "550e8400-e29b-41d4-a716-446655440000")
        for unsafe in ("..", ".", "", "a/b", "a\\b", "c:d", "e f", "x" * 200):
            mapped = safe_component(unsafe)
            self.assertNotIn("/", mapped)
            self.assertNotIn("\\", mapped)
            self.assertNotIn(":", mapped)
            self.assertLessEqual(len(mapped), 129)
            self.assertNotIn(mapped, (".", "..", ""))
        self.assertNotEqual(safe_component("a/b"), safe_component("a_b"))


class CopilotHookTests(unittest.TestCase):
    @staticmethod
    def transcript(path, turns):
        """turns: list of (user or None, assistant, message id); tool-call assistant events are interleaved."""
        lines = [{"type": "session.start", "data": {"sessionId": "s"}}]
        for user, assistant, mid in turns:
            if user is not None:
                lines.append({"type": "user.message", "data": {"content": user, "transformedContent": "<x>" + user}})
            lines.append({"type": "assistant.message", "data": {"content": "", "toolRequests": [{"name": "t"}], "messageId": mid + "-tool"}})
            lines.append({"type": "tool.execution_complete", "data": {"toolCallId": "c", "success": True}})
            lines.append({"type": "assistant.message", "data": {"content": assistant, "toolRequests": [], "messageId": mid}})
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8")

    def test_copilot_turn_reads_a_bounded_tail(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "session-state" / "s" / "events.jsonl"
            self.transcript(path, [("first", "one", "m1"), ("second", "two", "m2")])
            self.assertEqual(copilot_turn(str(path)), {"id": "m2", "assistant": "two", "user": "second"})
            self.transcript(path, [(None, "alone", "m3")])
            self.assertEqual(copilot_turn(str(path)), {"id": "m3", "assistant": "alone", "user": None})
            filler = [("noise " + str(i), "x" * 4096, "f" + str(i)) for i in range(400)]
            self.transcript(path, filler + [("last question", "last answer", "m9")])
            self.assertGreater(path.stat().st_size, 1024 * 1024)
            self.assertEqual(copilot_turn(str(path)), {"id": "m9", "assistant": "last answer", "user": "last question"})
            path.write_text('{"type": "session.start", "data": {}}\nnot json\n', encoding="utf-8")
            self.assertIsNone(copilot_turn(str(path)))
            self.assertIsNone(copilot_turn(str(Path(td) / "elsewhere" / "events.jsonl")))
            self.assertIsNone(copilot_turn(str(path.with_suffix(".json"))))
            self.assertIsNone(copilot_turn(None))

    def test_copilot_hook_file_is_owned_previewed_written_and_checked(self):
        with tempfile.TemporaryDirectory() as td:
            python, home = Path(sys.executable), Path(td) / "home"
            handlers = copilot_handlers(python, home)
            self.assertEqual(set(handlers["hooks"]), {"sessionStart", "agentStop"})
            for event, groups in handlers["hooks"].items():
                self.assertEqual(groups[0]["timeoutSec"], 5)
                self.assertIn("\"-m\" \"lumen.hook\"", groups[0]["bash"])
                self.assertIn("\"--event\" \"" + event + "\"", groups[0]["powershell"])
                self.assertTrue(groups[0]["powershell"].startswith("& "))
            path = Path(td) / ".copilot" / "hooks" / "lumen-memory.json"
            preview = manage_copilot_hooks("preview", path, handlers)
            self.assertTrue(preview["changed"] and not path.exists() and preview["owned_file"])
            self.assertFalse(manage_copilot_hooks("check", path, handlers)["healthy"])
            written = manage_copilot_hooks("write", path, handlers)
            self.assertTrue(written["changed"] and written["backup"] is None)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), handlers)
            self.assertTrue(manage_copilot_hooks("check", path, handlers)["healthy"])
            self.assertFalse(manage_copilot_hooks("write", path, handlers)["changed"])
            path.write_text("{}", encoding="utf-8")
            self.assertFalse(manage_copilot_hooks("check", path, handlers)["healthy"])
            again = manage_copilot_hooks("write", path, handlers)
            self.assertTrue(again["changed"] and Path(again["backup"]).exists())
            with self.assertRaises(LumenError):
                copilot_handlers(Path("relative"), home)

    def test_copilot_endpoints_deliver_and_capture_from_the_transcript_through_a_real_daemon(self):
        with tempfile.TemporaryDirectory() as td:
            root, home = Path(td) / "workspace", Path(td) / "home"
            root.mkdir()
            Workspace.initialize(root, [{"id": "p", "root": ".", "kind": "project", "purpose": "fixture",
                                         "owners": ["fixture-owner"], "depends_on": []}])
            workspace = Workspace(root, home)
            enroll(home, workspace.id, {"repo:p"}, project="p")
            env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(Path(lumen.__file__).resolve().parent.parent), os.environ.get("PYTHONPATH", "")])}
            daemon = subprocess.Popen([sys.executable, "-m", "lumen", "--home", str(home), "daemon", "--workspace", str(root)],
                                      env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            transcript = Path(td) / "copilot-home" / "session-state" / "8cbe976d" / "events.jsonl"
            session = "8cbe976d-efa6-4e71-b944-aa72455d5128"
            try:
                for _ in range(100):
                    try:
                        if call(home, "doctor", {})["result"]["healthy"]:
                            break
                    except (LumenError, OSError):
                        self.assertIsNone(daemon.poll())
                        time.sleep(0.05)

                def hook(endpoint, payload, session_flag=session):
                    return subprocess.run([sys.executable, "-m", "lumen", "--home", str(home), endpoint, "--workspace", str(root),
                                           "--cwd", str(root), "--session", session_flag, "--host", "copilot-cli"],
                                          input=json.dumps(payload), text=True, env=env, capture_output=True, timeout=30)

                start = hook("hook-session", {"sessionId": session, "timestamp": 1, "cwd": str(root), "source": "new"})
                self.assertEqual(start.returncode, 0, start.stderr)
                self.assertEqual(list(json.loads(start.stdout)), ["additionalContext"])
                stop = {"sessionId": session, "timestamp": 2, "cwd": str(root), "transcriptPath": str(transcript), "stopReason": "end_turn"}
                self.assertEqual(hook("hook-stop", stop).returncode, 1)  # no transcript yet
                self.transcript(transcript, [("Which encoding? " + SECRET, "UTF-32, per contract.txt.", "m1")])
                for _ in range(2):
                    run = hook("hook-stop", stop)
                    self.assertEqual((run.returncode, run.stdout), (0, ""), run.stderr)
                self.transcript(transcript, [("Which encoding? " + SECRET, "UTF-32, per contract.txt.", "m1"), ("And then?", "Done.", "m2")])
                self.assertEqual(hook("hook-stop", stop).returncode, 0)
                self.transcript(transcript, [(None, "Unprompted.", "m3")])
                self.assertEqual(hook("hook-stop", stop).returncode, 0)
                self.assertEqual(hook("hook-stop", stop, session_flag="wrong").returncode, 1)
                self.assertEqual(hook("hook-prompt", stop).returncode, 1)
                self.assertEqual(call(home, "doctor", {})["result"]["capture"], {"pending_prompts": 0, "capture_conflicts": 0, "duplicate_deliveries": 1})
            finally:
                daemon.terminate()
                daemon.wait(timeout=10)
            with Store(home) as store:
                grant = Access(workspace.id, frozenset({"repo:p"}), "local-owner", True)
                found = {e["turn"]: e for e in store.events(grant) if e["kind"] == "episode"}
                self.assertEqual(set(found), {"m1", "m2", "m3"})
                self.assertEqual((found["m1"]["state"], found["m1"]["assistant"]), ("paired", "UTF-32, per contract.txt."))
                self.assertNotIn(SECRET, found["m1"]["user"])
                self.assertEqual((found["m2"]["state"], found["m2"]["user"]), ("paired", "And then?"))
                self.assertEqual((found["m3"]["state"], found["m3"]["user"]), ("prompt_missing", None))
                self.assertEqual(found["m1"]["host"], "copilot-cli")


class HookSettingsTests(unittest.TestCase):
    def test_preview_never_writes_and_write_merges_with_backup(self):
        with tempfile.TemporaryDirectory() as td:
            python, home = Path(sys.executable), Path(td) / "home"
            handlers = claude_handlers(python, home)
            self.assertEqual(set(handlers), {"SessionStart", "UserPromptSubmit", "Stop"})
            self.assertEqual(handlers["Stop"][0]["hooks"][0]["command"], str(python))
            self.assertEqual(handlers["Stop"][0]["hooks"][0]["args"][:3], ["-I", "-m", "lumen.hook"])
            self.assertEqual(handlers["SessionStart"][0]["matcher"], "startup")
            self.assertTrue(all(g["hooks"][0]["timeout"] == 5 and "args" in g["hooks"][0] for groups in handlers.values() for g in groups))
            settings = Path(td) / ".claude" / "settings.json"
            preview = manage_hooks("preview", settings, handlers)
            self.assertTrue(preview["changed"] and not settings.exists())
            self.assertFalse(manage_hooks("check", settings, handlers)["healthy"])
            settings.parent.mkdir()
            settings.write_text(json.dumps({"theme": "dark", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo"}]}]}}), encoding="utf-8")
            written = manage_hooks("write", settings, handlers)
            self.assertTrue(written["changed"] and Path(written["backup"]).exists())
            merged = json.loads(settings.read_text(encoding="utf-8"))
            self.assertEqual(merged["theme"], "dark")
            self.assertEqual(merged["hooks"]["Stop"][0]["hooks"][0]["command"], "echo")
            self.assertEqual(merged["hooks"]["Stop"][1], handlers["Stop"][0])
            self.assertEqual(len(merged["hooks"]["SessionStart"]), 1)
            self.assertTrue(manage_hooks("check", settings, handlers)["healthy"])
            self.assertFalse(manage_hooks("write", settings, handlers)["changed"])
            other = claude_handlers(Path(td) / "other-python.exe", home)
            self.assertEqual(manage_hooks("preview", settings, other)["replaced_lumen_entries"], ["SessionStart", "Stop", "UserPromptSubmit"])
            with self.assertRaises(LumenError):
                claude_handlers(Path("relative.exe"), home)


class HookProcessTests(unittest.TestCase):
    def test_hook_endpoints_capture_once_through_a_real_daemon(self):
        with tempfile.TemporaryDirectory() as td:
            root, home = Path(td) / "workspace", Path(td) / "home"
            root.mkdir()
            Workspace.initialize(root, [{"id": "p", "root": ".", "kind": "project", "purpose": "fixture",
                                         "owners": ["fixture-owner"], "depends_on": []}])
            workspace = Workspace(root, home)
            enroll(home, workspace.id, {"repo:p"}, project="p")
            env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(Path(lumen.__file__).resolve().parent.parent), os.environ.get("PYTHONPATH", "")])}
            daemon = subprocess.Popen([sys.executable, "-m", "lumen", "--home", str(home), "daemon", "--workspace", str(root)],
                                      env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                for _ in range(100):
                    try:
                        if call(home, "doctor", {})["result"]["healthy"]:
                            break
                    except (LumenError, OSError):
                        self.assertIsNone(daemon.poll())
                        time.sleep(0.05)
                base = {"session_id": "sess-1", "cwd": str(root), "transcript_path": str(root / "t.jsonl"),
                        "prompt_id": "550e8400-e29b-41d4-a716-446655440000"}

                def hook(endpoint, payload, session="sess-1"):
                    return subprocess.run([sys.executable, "-m", "lumen", "--home", str(home), endpoint, "--workspace", str(root),
                                           "--cwd", str(root), "--session", session, "--host", "claude-code"],
                                          input=json.dumps(payload), text=True, env=env, capture_output=True, timeout=30)

                self.assertEqual(hook("hook-prompt", {**base, "hook_event_name": "UserPromptSubmit", "prompt": "What now? " + SECRET}).returncode, 0)
                self.assertEqual(hook("hook-prompt", {**base, "hook_event_name": "UserPromptSubmit", "user_prompt": "What now? " + SECRET}).returncode, 0)
                for _ in range(2):
                    run = hook("hook-stop", {**base, "hook_event_name": "Stop", "last_assistant_message": "Nothing yet."})
                    self.assertEqual(run.returncode, 0, run.stderr)
                    self.assertEqual(run.stdout, "")
                self.assertEqual(hook("hook-stop", {**base, "hook_event_name": "Stop", "last_assistant_message": "Nothing yet."}, session="wrong").returncode, 1)
                self.assertEqual(hook("hook-stop", {**base, "hook_event_name": "Stop"}).returncode, 1)
                self.assertEqual(hook("hook-stop", {**base, "hook_event_name": "Stop", "last_assistant_message": "changed"}).returncode, 1)
                status = call(home, "doctor", {})["result"]["capture"]
                self.assertEqual(status, {"pending_prompts": 0, "capture_conflicts": 1, "duplicate_deliveries": 1})
            finally:
                daemon.terminate()
                daemon.wait(timeout=10)
            with Store(home) as store:
                grant = Access(workspace.id, frozenset({"repo:p"}), "local-owner", True)
                captured = [e for e in store.events(grant) if e["kind"] == "episode"]
                self.assertEqual(len(captured), 1)
                self.assertEqual(captured[0]["state"], "paired")
                self.assertEqual(captured[0]["user"], "What now? [REDACTED]")
                self.assertEqual(captured[0]["turn"], base["prompt_id"])


if __name__ == "__main__":
    unittest.main()
