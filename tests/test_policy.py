"""Committed team files, the session-start slice, user and session scopes, loud-failure and bound cases."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lumen import __version__
from lumen.model import (DEFAULT_RULES, Access, LumenError, check_rules, effective_region, estimate_tokens,
                         parse_relations, region, render_relations)
from lumen.projection import project, state
from lumen.security import CONFIG, default_policy, parse_policy, render_policy
from lumen.service import Service
from lumen.sharing import proposal
from lumen.store import Store
from lumen.workspace import HINT, SLICE_TOKENS, Workspace, render_slice
from support_sources import source_workspace
from test_core import OWNER, assertion, capture, revision


class RelationVocabularyTests(unittest.TestCase):
    def test_strict_subset_round_trips_and_rejects_other_yaml(self):
        rules = parse_relations(render_relations(DEFAULT_RULES))
        self.assertEqual(rules, DEFAULT_RULES)
        parsed = parse_relations("# comment\n\nrequired_encoding: {cardinality: single, dimensions: [platform, branch]}\n"
                                 "uses_library: {cardinality: multi}   # trailing comment\n")
        self.assertEqual(parsed["required_encoding"], {"cardinality": "single", "dimensions": ["platform", "branch"]})
        self.assertEqual(parsed["uses_library"]["dimensions"], ["platform", "branch", "path"])
        for text in ("x:\n  cardinality: single\n", "x: {cardinality: sometimes}", "x: {cardinality: single, dimensions: [host]}",
                     "x: {cardinality: single}\nx: {cardinality: multi}", "- x", "x: {cardinality: single, dimensions: [branch, platform]}",
                     "x: {cardinality: single, extra: 1}"):
            with self.assertRaises(LumenError, msg=text):
                parse_relations(text)
        with self.assertRaises(LumenError):
            check_rules({"x": "single"})

    def test_declared_dimensions_widen_matching_and_change_the_state_token(self):
        rules = {"test_command": {"cardinality": "single", "dimensions": ["platform"]}}
        a = assertion("A", region(branch="main", start=0))
        b = assertion("B", region(platform="windows", branch="main", start=0))
        r = revision(a, b, region(platform="windows", branch="main", start=10))
        events = [a, b, r]
        # branch is not a dimension of test_command under these rules: release/2 sees the main facts.
        default_answer = project(events, "w", "compiler", "test_command", 15, "windows", "release/2", "src/a")
        self.assertEqual([x["value"] for x in default_answer["assertions"]], [])
        widened = project(events, "w", "compiler", "test_command", 15, "windows", "release/2", "src/a", rules)
        self.assertEqual([x["value"] for x in widened["assertions"]], ["B"])
        linux = project(events, "w", "compiler", "test_command", 15, "linux", "release/2", "src/a", rules)
        self.assertEqual([x["value"] for x in linux["assertions"]], ["A"])
        self.assertNotEqual(default_answer["state_token"], widened["state_token"])
        self.assertEqual(effective_region(a["region"], rules, "test_command")["branch"], "any")
        self.assertEqual(effective_region(a["region"], rules, "undeclared"), a["region"])
        self.assertEqual(state(events, "w", "compiler", "test_command", rules)[-1], widened["state_token"])

    def test_workspace_loads_the_file_and_the_service_follows_it_without_restart(self):
        with tempfile.TemporaryDirectory() as td, Store(Path(td) / "store") as store:
            workspace = source_workspace(Path(td) / "sources")
            path = workspace.root / ".lumen/relations.yaml"
            self.assertTrue(path.is_file())
            self.assertEqual(workspace.relations, DEFAULT_RULES)
            capture(store)
            store.reindex(OWNER)
            service = Service(store, workspace)
            before = service.call(OWNER, "recall", {"query": "compiler", "at": 5})["state_token"]
            path.write_text("test_command: {cardinality: single, dimensions: [platform]}\n", encoding="utf-8")
            after = service.call(OWNER, "recall", {"query": "compiler", "at": 5})["state_token"]
            self.assertNotEqual(before, after)
            self.assertEqual(store.rules, {"test_command": {"cardinality": "single", "dimensions": ["platform"]}})
            path.write_text("test_command:\n  cardinality: single\n", encoding="utf-8")
            self.assertEqual(service.call(OWNER, "recall", {"query": "compiler"})["error"]["code"], "invalid_event")
            doctor = service.call(OWNER, "doctor", {})["result"]
            self.assertFalse(doctor["healthy"])
            self.assertIn("policy_unreadable:invalid_event", doctor["failures"])
            path.unlink()
            self.assertEqual(service.call(OWNER, "recall", {"query": "compiler", "at": 5})["state_token"], before)


class TeamPolicyTests(unittest.TestCase):
    def test_config_toml_lowers_caps_only_and_never_enables_inference(self):
        rendered = parse_policy(render_policy(__version__))
        self.assertEqual(rendered["config"], CONFIG)
        self.assertEqual(rendered["version"], __version__)
        self.assertEqual(rendered["share"], {"batch_limit": 100, "pull_request": "unconfigured"})
        lowered = parse_policy('schema = 1\n[caps]\nmax_results = 3\nmax_packet_bytes = 4096\n[share]\nbatch_limit = 10\npull_request = "pending"\n')
        self.assertEqual((lowered["config"]["max_results"], lowered["config"]["max_packet_bytes"]), (3, 4096))
        self.assertEqual(lowered["share"], {"batch_limit": 10, "pull_request": "pending"})
        self.assertNotEqual(lowered["digest"], default_policy()["digest"])
        for text in ('schema = 1\n[caps]\nmax_results = 21\n', 'schema = 1\n[caps]\nmax_packet_bytes = 100\n',
                     'schema = 1\n[trust]\ntext_generation = true\n', 'schema = 1\n[trust]\nautomatic_retirement = true\n',
                     'schema = 1\n[embeddings]\nprovider = "x"\n', 'schema = 2\n', 'schema = 1\n[share]\npull_request = "auto"\n',
                     'schema = 1\n[share]\nbatch_limit = 1000\n', 'schema = 1\n[caps]\nmax_results = "5"\n', 'not toml ='):
            with self.assertRaises(LumenError, msg=text):
                parse_policy(text)

    def test_policy_governs_the_store_and_a_pinned_version_mismatch_fails_doctor(self):
        with tempfile.TemporaryDirectory() as td, Store(Path(td) / "store") as store:
            workspace = source_workspace(Path(td) / "sources")
            config = workspace.root / ".lumen/config.toml"
            self.assertTrue(config.is_file())
            for turn in range(5):
                capture(store, str(turn), subject="component" + str(turn))
            store.reindex(OWNER)
            service = Service(store, workspace)
            self.assertEqual(len(service.call(OWNER, "recall", {"query": "compiler"})["result"]["results"]), 5)
            config.write_text('schema = 1\n[caps]\nmax_results = 2\n', encoding="utf-8")
            self.assertEqual(len(service.call(OWNER, "recall", {"query": "compiler"})["result"]["results"]), 2)
            self.assertEqual(store.config["max_results"], 2)
            report = service.call(OWNER, "doctor", {})["result"]
            self.assertTrue(report["healthy"])
            self.assertEqual(report["policy"]["source"], "config.toml")
            config.write_text('schema = 1\n[lumen]\nversion = "9.9.9"\n', encoding="utf-8")
            report = service.call(OWNER, "doctor", {})["result"]
            self.assertFalse(report["healthy"])
            self.assertIn("policy_version_mismatch", report["failures"])
            self.assertEqual(report["policy"]["version_pinned"], "9.9.9")
            config.unlink()
            report = service.call(OWNER, "doctor", {})["result"]
            self.assertTrue(report["healthy"])
            self.assertEqual(report["policy"]["source"], "defaults")


class SessionStartSliceTests(unittest.TestCase):
    def build(self, td, count, purpose="p"):
        root = Path(td) / "workspace"
        projects = []
        for i in range(count):
            (root / f"r{i}").mkdir(parents=True)
            (root / f"r{i}" / "x").write_text("x")
            projects.append({"id": f"r{i}", "root": f"r{i}", "kind": "library", "purpose": purpose * (i % 5 + 1),
                             "owners": ["owner"], "depends_on": ["r0"] if i and i % 2 else []})
        Workspace.initialize(root, projects)
        return Workspace(root, Path(td) / "home")

    def test_slice_has_this_repo_dependencies_dependents_and_a_count_of_the_rest(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = self.build(td, 8)
            grant = Access(workspace.id, workspace.visible_scopes("r1"), "owner", True)
            self.assertIn("user", grant.scopes)
            self.assertIn("session", grant.scopes)
            with Store(Path(td) / "store") as store:
                service = Service(store, workspace, "r1")
                result = service.call(grant, "session_start", {"session": "s1", "project": "r1"})["result"]
                self.assertEqual(result["hint"], HINT)
                self.assertTrue(result["context"].startswith(HINT + "\n"))
                slice_ = result["catalog"]
                self.assertEqual(slice_["project"]["id"], "r1")
                self.assertEqual([e["id"] for e in slice_["dependencies"]], ["r0"])
                self.assertEqual(slice_["dependents"], [])
                # r1's grant sees r1 and r0 only; the rest of the workspace is counted, never listed.
                self.assertEqual(slice_["others"], 0)
                self.assertIn("This repo: r1", result["context"])
                self.assertIn("Dependencies: r0", result["context"])
                owner = Access(workspace.id, frozenset("repo:r%d" % i for i in range(8)) | {"monorepo"}, "owner", True)
                wide = Service(store, workspace).call(owner, "session_start", {"session": "s2", "project": "r0"})["result"]
                self.assertEqual([e["id"] for e in wide["catalog"]["dependents"]], ["r1", "r3", "r5", "r7"])
                self.assertEqual(wide["catalog"]["others"], 3)
                self.assertIn("3 other repos", wide["context"])
                again = Service(store, workspace).call(owner, "session_start", {"session": "s2", "project": "r0"})["result"]
                self.assertEqual(again, wide)

    def test_slice_stays_under_the_token_bound_with_a_large_workspace(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = self.build(td, 120, purpose="a purpose sentence that takes some room to describe ")
            scopes = frozenset("repo:r%d" % i for i in range(120)) | {"monorepo"}
            owner = Access(workspace.id, scopes, "owner", True)
            with Store(Path(td) / "store") as store:
                result = Service(store, workspace).call(owner, "session_start", {"session": "s", "project": "r0"})["result"]
                slice_ = result["catalog"]
                self.assertGreater(slice_["omitted"], 0)
                self.assertLessEqual(estimate_tokens(result["context"]), SLICE_TOKENS)
                self.assertLessEqual(len(result["context"].encode()), 6144)
                self.assertIn(str(slice_["others"] + slice_["omitted"]) + " other repos", render_slice(slice_))
                self.assertEqual(slice_["others"] + slice_["omitted"] + len(slice_["dependents"]) + 1, 120)


class UserAndSessionScopeTests(unittest.TestCase):
    def test_user_facts_are_written_recalled_and_never_shared(self):
        with tempfile.TemporaryDirectory() as td, Store(Path(td) / "store") as store:
            workspace = source_workspace(Path(td) / "sources")
            grant = Access("w", workspace.visible_scopes("p"), OWNER.actor, True)
            agent = Access("w", grant.scopes, OWNER.actor, False)
            service = Service(store, workspace, "p")
            args = dict(checkout="co", host="cli", session="s", turn="u1", scope="user", subject="woody",
                        relation="editor_preference", value="tabs", text="woody prefers tabs", region=region(start=0))
            stated = service.call(grant, "remember", args)["result"]
            observed = service.call(agent, "remember", {**args, "turn": "u2", "value": "spaces", "text": "woody prefers spaces"})["result"]
            self.assertEqual(store.expand(grant, stated["id"])["event"]["origin"], "user-stated")
            self.assertEqual(store.expand(grant, observed["id"])["event"]["origin"], "agent-observed")
            recalled = service.call(agent, "recall", {"query": "woody"})["result"]
            self.assertIn("user", recalled["scopes_searched"])
            self.assertEqual(recalled["results"][0]["assertions"][0]["scope"], "user")
            with self.assertRaises(LumenError):
                proposal(store, grant, [stated["id"]])
            self.assertEqual(service.call(grant, "share", {"action": "propose", "ids": [stated["id"]]})["error"]["code"], "not_authorized")

    def test_session_working_state_is_visible_only_to_its_session(self):
        with tempfile.TemporaryDirectory() as td, Store(Path(td) / "store") as store:
            workspace = source_workspace(Path(td) / "sources")
            agent = Access("w", workspace.visible_scopes("p"), OWNER.actor, False)
            service = Service(store, workspace, "p")
            args = dict(checkout="co", host="claude-code", session="s1", turn="w1", scope="session:s1", subject="task",
                        relation="next_step", value="run the suite", text="next step: run the suite", region=region(start=0))
            written = service.call(agent, "remember", args)
            self.assertNotIn("error", written, written)
            same = service.call(agent, "recall", {"query": "suite", "session": "s1"})["result"]
            self.assertEqual(same["results"][0]["assertions"][0]["scope"], "session:s1")
            other = service.call(agent, "recall", {"query": "suite", "session": "s2"})["result"]
            self.assertEqual(other["results"], [])
            self.assertNotIn("session:s1", other["scopes_searched"])
            none = service.call(agent, "recall", {"query": "suite"})["result"]
            self.assertEqual(none["results"], [])
            self.assertEqual(service.call(agent, "remember", {**args, "session": "s2", "turn": "w2"})["error"]["code"], "not_authorized")
            self.assertIn("error", service.call(agent, "remember", {**args, "session": "bad session", "turn": "w3"}))
            self.assertIsNone(service.call(agent, "expand", {"eid": written["result"]["id"]})["result"]["event"])
            self.assertIsNotNone(service.call(agent, "expand", {"eid": written["result"]["id"], "session": "s1"})["result"]["event"])
            with self.assertRaises(LumenError):
                proposal(store, Access("w", OWNER.scopes | {"session:s1"}, OWNER.actor, True), [written["result"]["id"]])
            exported = store.events(Access("w", workspace.visible_scopes("p"), OWNER.actor, True))
            self.assertEqual([e["scope"] for e in exported], [])


class LoudFailureAndBoundTests(unittest.TestCase):
    def test_missing_fts5_is_an_installation_failure(self):
        with tempfile.TemporaryDirectory() as td:
            with patch("lumen.store.fts5_available", return_value=False):
                with self.assertRaises(LumenError) as caught:
                    Store(Path(td) / "store")
            self.assertEqual(caught.exception.code, "unsupported_capability")
            self.assertFalse((Path(td) / "store").exists())
            with Store(Path(td) / "store") as store:
                self.assertTrue(store.doctor()["healthy"])
                with patch("lumen.store.fts5_available", return_value=False):
                    report = store.doctor()
                self.assertFalse(report["healthy"])
                self.assertIn("fts5_unavailable", report["failures"])

    def test_corrupt_relation_and_index_structures_fail_doctor(self):
        with tempfile.TemporaryDirectory() as td, Store(Path(td) / "store") as store:
            capture(store)
            store.reindex(OWNER)
            store.rules = {"test_command": "single"}
            report = store.doctor()
            self.assertIn("relation_schema_invalid", report["failures"])
            with self.assertRaises(LumenError):
                store.recall(OWNER, "compiler")
            store.rules = dict(DEFAULT_RULES)
            self.assertTrue(store.doctor()["healthy"])
            store.index.execute("DROP TABLE exact")
            self.assertFalse(store.doctor()["healthy"])
            store.index.execute("CREATE TABLE exact(workspace TEXT,scope TEXT,event_id TEXT,identifier TEXT)")
            store.index.execute("DELETE FROM admissions")
            self.assertIn("index_admission_integrity", store.doctor()["failures"])
            store.reindex(OWNER)
            self.assertTrue(store.doctor()["healthy"])

    def test_index_retries_are_counted_bounded_and_never_drop_events(self):
        with tempfile.TemporaryDirectory() as td, Store(Path(td) / "store") as store:
            capture(store)

            def fault(point):
                if point == "before_index_commit":
                    raise RuntimeError("index crash")
            store.fault = fault
            for attempt in range(1, 6):
                with self.assertRaises(RuntimeError):
                    store.drain_index(OWNER)
                self.assertEqual(store.index_retry_status()["max_attempts"], attempt)
            report = store.doctor()
            self.assertIn("index_retry_exhausted", report["failures"])
            self.assertEqual(report["index_retries"]["queued"], 1)
            self.assertEqual(len(store.events(OWNER)), 1)
            store.fault = lambda _: None
            store.drain_index(OWNER)
            self.assertEqual(store.index_retry_status(), {"queued": 0, "max_attempts": 0, "bound": 5, "exhausted": False})
            self.assertTrue(store.doctor()["healthy"])

    def test_token_estimate_is_conservative(self):
        self.assertEqual(estimate_tokens(""), 0)
        self.assertGreaterEqual(estimate_tokens("one two three"), 3)
        self.assertGreaterEqual(estimate_tokens("x" * 3000), 1000)
        self.assertGreaterEqual(estimate_tokens("日本語のテキスト"), 8)


if __name__ == "__main__":
    unittest.main()
