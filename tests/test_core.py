import itertools
import json
import multiprocessing
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest

from lumen.model import Access, LumenError, episode, new_event, ref, region, seal
from lumen.projection import closure, intervals, project, state
from lumen.security import CONFIG, safe_source
from lumen.store import Store

OWNER = Access("w", frozenset({"repo:p"}), "local-owner", True)


def assertion(value="A", r=None, **kwargs):
    return new_event("assertion", "w", "repo:p", "local-owner", subject="compiler",
                     relation="test_command", value=value, text="compiler " + value,
                     origin="user-stated", citations=[episode(value)], region=r or region(start=0), **kwargs)


def revision(a, b, r=None):
    return new_event("revision", "w", "repo:p", "local-owner", predecessors=[ref(a)],
                     successor=ref(b) if b else None, revision_kind="change", reason="fixture",
                     affected=r or region(start=10), expected_state="imported")


def capture(store, turn="1", **kwargs):
    fields = dict(checkout="co", host="cli", session="s", turn=turn, scope="repo:p",
                  subject="compiler", relation="test_command", value="A", text="compiler A",
                  region=region(start=0))
    fields.update(kwargs)
    return store.remember(OWNER, **fields)


class ProjectionTests(unittest.TestCase):
    def test_bounded_correction_and_recurrence(self):
        a, b, c = assertion(), assertion("B"), assertion("A")
        ab, bc = revision(a, b), revision(b, c, region(start=20))
        events = [a, b, c, ab, bc]
        self.assertEqual([r["ids"] for r in intervals(events, "w", "compiler", "test_command")],
                         [[a["id"]], [b["id"]], [c["id"]]])
        correction = seal({**ab, "revision_kind": "correction", "affected": region(start=5, end=8)})
        for t, expected in [(4, "A"), (5, "B"), (7, "B"), (8, "A")]:
            p = project([a, b, correction], "w", "compiler", "test_command", t)
            self.assertEqual([x["value"] for x in p["assertions"]], [expected])

    def test_partial_platform_branch_and_path(self):
        a, b = assertion(), assertion("B", region(platform="windows", branch="release/2", path="tests/**", start=0))
        r = revision(a, b, region(platform="windows", branch="release/2", path="tests/**", start=10))
        for platform, branch, path, expected in [
            ("linux", "release/2", "tests/a", "A"), ("windows", "main", "tests/a", "A"),
            ("windows", "release/2", "src/a", "A"), ("windows", "release/2", "tests/a", "B")]:
            p = project([a, b, r], "w", "compiler", "test_command", 15, platform, branch, path)
            self.assertEqual([x["value"] for x in p["assertions"]], [expected])

    def test_shuffled_forks_and_semantic_token(self):
        a, b, c = assertion(), assertion("B"), assertion("C")
        events = [a, b, c, revision(a, b), revision(a, c)]
        expected = project(events, "w", "compiler", "test_command", 15)
        self.assertEqual(expected["status"], "disputed")
        for shuffled in itertools.permutations(events):
            self.assertEqual(project(shuffled, "w", "compiler", "test_command", 15), expected)
        self.assertNotEqual(state(events, "w", "compiler", "test_command", {})[-1], expected["state_token"])

    def test_pending_and_cycles_and_cross_scope(self):
        a, b = assertion(), assertion("B")
        r = revision(a, b)
        self.assertEqual(closure([a, r])[1], [r["id"]])
        self.assertFalse(closure([a, b, r])[1])
        with self.assertRaises(LumenError):
            closure([a, b, r, revision(b, a)])
        with self.assertRaises(LumenError):
            closure([a, seal({**b, "scope": "user"}), r])

    def test_unknown_date_does_not_use_clock(self):
        self.assertFalse(project([assertion(r=region())], "w", "compiler", "test_command", 100)["assertions"])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_recall_payload_accepts_as_of_alias(self):
        from lumen.service import Service
        capture(self.store)
        self.store.reindex(OWNER)
        service = Service(self.store)
        by_at = service.call(OWNER, "recall", {"query": "compiler", "at": 15})
        by_alias = service.call(OWNER, "recall", {"query": "compiler", "as_of": 15})
        self.assertNotIn("error", by_alias, by_alias)
        self.assertEqual(by_alias["result"]["results"][0]["state_token"],
                         by_at["result"]["results"][0]["state_token"])
        both = service.call(OWNER, "recall", {"query": "compiler", "at": 15, "as_of": 15})
        self.assertIn("error", both)

    def test_durable_retry_index_and_known_at(self):
        first = capture(self.store)
        self.assertTrue(first["durable"])
        self.assertFalse(first["indexed"])
        self.assertEqual(capture(self.store)["id"], first["id"])
        with self.assertRaises(LumenError):
            capture(self.store, value="collision")
        with self.assertRaisesRegex(LumenError, "Index not current"):
            self.store.recall(OWNER, "compiler")
        self.store.reindex(OWNER)
        token = self.store.recall(OWNER, "compiler", at=15)["results"][0]["state_token"]
        b = assertion("B")
        self.store.revise(OWNER, [first["id"]], token, region(start=10), "changed", successor=b)
        with self.assertRaises(LumenError) as exc:
            self.store.revise(OWNER, [first["id"]], token, region(start=10), "stale")
        self.assertEqual(exc.exception.code, "revision_conflict")
        self.store.reindex(OWNER)
        now = self.store.recall(OWNER, "compiler", at=15)["results"][0]
        old = self.store.recall(OWNER, "compiler", at=15, known_at=first["snapshot"])["results"][0]
        self.assertEqual(now["assertions"][0]["value"], "B")
        self.assertEqual(old["assertions"][0]["value"], "A")

    def test_isolation_expand_and_fts_query_escaping(self):
        result = capture(self.store)
        self.store.reindex(OWNER)
        other = Access("w", frozenset({"repo:other"}), "local-owner", True)
        self.assertEqual(self.store.expand(other, result["id"]), self.store.expand(other, "unknown"))
        self.assertFalse(self.store.recall(other, "compiler")["results"])
        for query in ['" OR * NOT', "compiler NEAR(", "", "💡 编译器", "test_command"]:
            self.store.recall(OWNER, query)

    def test_redaction_before_disk_and_no_configuration_escape(self):
        capture(self.store, text="password=secretstuff compiler")
        for row in self.store.db.execute("SELECT body FROM events"):
            self.assertNotIn("secretstuff", row[0])
        for config in [{**CONFIG, "embeddings": True}, {**CONFIG, "retrieval": "hosted-semantic-search"},
                       {**CONFIG, "text_generation": True}]:
            with self.assertRaises(LumenError):
                Store(Path(self.tmp.name) / "rejected", config)
        self.assertFalse((Path(self.tmp.name) / "rejected").exists())

    def test_fault_transitions_retry(self):
        for point in ["before_event", "after_event", "after_outbox", "after_idempotency", "before_commit", "after_commit"]:
            def fault(p):
                if p == point:
                    raise RuntimeError(point)
            self.store.fault = fault
            with self.assertRaises(RuntimeError):
                capture(self.store, point)
            self.store.fault = lambda _: None
            a = capture(self.store, point)
            b = capture(self.store, point)
            self.assertEqual(a["id"], b["id"])
        self.assertEqual(len(self.store.events(OWNER)), 6)

    def test_failed_index_does_not_lose_ledger(self):
        capture(self.store)
        def fault(point):
            if point == "before_index_commit":
                raise RuntimeError("index crash")
        self.store.fault = fault
        with self.assertRaises(RuntimeError):
            self.store.reindex(OWNER)
        self.store.fault = lambda _: None
        self.assertFalse(self.store.doctor()["healthy"])
        self.store.reindex(OWNER)
        self.assertTrue(self.store.doctor()["healthy"])

    def test_source_exclusions(self):
        for path in ["../escape", ".env", ".env.local", "archive/private", "vault/.firecrawl/staging/a", "C:/private"]:
            with self.assertRaises(LumenError):
                safe_source(self.tmp.name, path)


if __name__ == "__main__":
    unittest.main()
