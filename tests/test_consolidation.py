"""Phase 4: the consolidator over episodes, the disabled judge (R-17), tool-derived origin, invalidation."""
import hashlib
from pathlib import Path
import tempfile
import unittest

from lumen.consolidation import Consolidator, DisabledJudge, extract, judge_for
from lumen.model import Access, LumenError, region
from lumen.security import CONFIG, redact
from lumen.service import Service
from lumen.sharing import proposal
from lumen.store import Store
from test_core import OWNER

AGENT = Access("w", OWNER.scopes, OWNER.actor, False)
IDENTITY = dict(checkout="co", host="claude-code", session="s1", project="p")


def turn(store, number, assistant, user="What did we decide?", tools=None):
    fields = dict(turn="t" + str(number), assistant=assistant, user=user, **IDENTITY)
    if tools:
        fields["tools"] = tools
    return store.capture_stop(AGENT, **fields)


def current(store, at=None):
    store.reindex(OWNER)
    packet = store.recall(OWNER, "compiler test_command", at=at or 4102444800)["results"][0]
    return packet["status"], sorted(a["value"] for a in packet["assertions"]), packet


class ExtractionTests(unittest.TestCase):
    def test_statements_match_only_the_declared_vocabulary(self):
        rules = {"test_command": {"cardinality": "single", "dimensions": ["platform", "branch", "path"]}}
        event = {"user": "compiler test_command: python -m unittest\ncompiler colour = blue", "assistant":
                 "- compiler.test_command = python -m unittest\nUnrelated prose about test_command: nothing.",
                 "tools": [{"name": "bash", "text": "compiler test_command: python -m pytest"}]}
        found = extract(event, rules)
        self.assertEqual([(c["value"], c["origin"], c["source"]) for c in found],
                         [("python -m unittest", "agent-observed", "user"), ("python -m pytest", "tool-derived", "tool:0:bash")])
        self.assertEqual(extract({"user": None, "assistant": "compiler.owned_by = woody"}, rules), [])
        self.assertIsInstance(judge_for(CONFIG), DisabledJudge)
        with self.assertRaises(LumenError):
            judge_for({**CONFIG, "automatic_retirement": True})


class ConsolidatorTests(unittest.TestCase):
    def test_disabled_judge_disputes_competing_values_and_retires_nothing(self):
        with tempfile.TemporaryDirectory() as td, Store(td) as store:
            turn(store, 1, "Noted.\ncompiler test_command: python -m unittest")
            report = Consolidator(store).run(OWNER, force=True)
            self.assertEqual((report["episodes"], report["assertions"], report["disputes"], report["retirements"]), (1, 1, 0, 0))
            self.assertEqual(report["judge"], "disabled")
            self.assertEqual(current(store)[:2], ("current", ["python -m unittest"]))
            turn(store, 2, "Actually:\n- compiler test_command: python -m pytest")
            report = Consolidator(store).run(OWNER, force=True)
            self.assertEqual((report["assertions"], report["disputes"], report["retirements"]), (0, 1, 0))
            status, values, packet = current(store)
            self.assertEqual(status, "disputed")
            self.assertEqual(values, ["python -m pytest", "python -m unittest"])
            kinds = [e["kind"] for e in store.events(OWNER)]
            self.assertEqual(kinds.count("revision"), 0)
            self.assertEqual(kinds.count("dispute"), 1)
            dispute = next(e for e in store.events(OWNER) if e["kind"] == "dispute")
            self.assertIn("automatic retirement is disabled", dispute["reason"])
            # The explicit revision path still works and resolves the dispute.
            loser = next(a for a in packet["assertions"] if a["value"] == "python -m unittest")
            store.revise(OWNER, [loser["id"]], packet["state_token"], loser["region"], "owner decided on pytest")
            # The explicit dispute record stands until a reconciliation selects an alternative (plan §4).
            status, values, packet = current(store)
            self.assertEqual((status, values), ("disputed", ["python -m pytest"]))
            from lumen.model import new_event
            winner = packet["assertions"][0]
            reconciliation = new_event("reconciliation", "w", "repo:p", OWNER.actor,
                                       alternatives=dispute["alternatives"], selected={"workspace": "w", "id": winner["id"]},
                                       affected=winner["region"], reason="owner selected pytest")
            with store.transaction():
                store._validate_write(reconciliation, OWNER)
                store._insert(reconciliation)
            self.assertEqual(current(store)[:2], ("current", ["python -m pytest"]))
            self.assertEqual(Consolidator(store).status("w")["episodes_waiting"], 0)

    def test_same_value_adds_evidence_and_recurrence_is_a_new_assertion(self):
        with tempfile.TemporaryDirectory() as td, Store(td) as store:
            turn(store, 1, "compiler test_command: python -m unittest")
            turn(store, 2, "Still true:\ncompiler test_command: python -m unittest")
            report = Consolidator(store).run(OWNER, force=True)
            self.assertEqual((report["assertions"], report["evidence"], report["disputes"]), (1, 1, 0))
            status, values, packet = current(store)
            self.assertEqual(values, ["python -m unittest"])
            self.assertEqual(len(packet["evidence"][packet["assertions"][0]["id"]]), 2)
            self.assertEqual({c["source"] for c in packet["evidence"][packet["assertions"][0]["id"]]},
                             {"claude-code/s1#t1", "claude-code/s1#t2"})
            # Retire the value explicitly, then see it stated again: a recurrence is a third event, not evidence.
            store.revise(OWNER, [packet["assertions"][0]["id"]], packet["state_token"], packet["assertions"][0]["region"], "dropped")
            turn(store, 3, "Back to it.\ncompiler test_command: python -m unittest")
            report = Consolidator(store).run(OWNER, force=True)
            self.assertEqual((report["assertions"], report["evidence"]), (1, 0))
            self.assertEqual(sum(e["kind"] == "assertion" for e in store.events(OWNER)), 2)

    def test_tool_outputs_are_truncated_hashed_and_yield_tool_derived_facts(self):
        with tempfile.TemporaryDirectory() as td, Store(td) as store:
            output = "compiler test_command: make check\n" + "x" * 10000 + " api_key=sk-lumenfixture0123456789abcdef"
            result = turn(store, 1, "I ran the build.", user="I am the user.\ncompiler owned_by: woody",
                          tools=[{"name": "bash", "output": output}])
            event = store.expand(OWNER, result["id"])["event"]
            record = event["tools"][0]
            self.assertTrue(record["truncated"])
            self.assertEqual(len(record["text"].encode()), 4096)
            self.assertEqual(record["sha256"], hashlib.sha256(redact(output).encode()).hexdigest())
            self.assertEqual(record["length"], len(redact(output).encode()))
            self.assertNotIn("sk-lumenfixture", (Path(td) / "ledger.db").read_bytes().decode("utf-8", "ignore"))
            self.assertEqual(turn(store, 1, "I ran the build.", user="I am the user.\ncompiler owned_by: woody",
                                  tools=[{"name": "bash", "output": output}])["id"], result["id"])
            with self.assertRaises(LumenError):
                turn(store, 1, "I ran the build.", user="I am the user.\ncompiler owned_by: woody",
                     tools=[{"name": "bash", "output": "different"}])
            report = Consolidator(store).run(OWNER, force=True)
            self.assertEqual(report["assertions"], 2)
            origins = {(e["subject"], e["relation"]): e["origin"] for e in store.events(OWNER) if e["kind"] == "assertion"}
            self.assertEqual(origins[("compiler", "test_command")], "tool-derived")
            # A claim in the captured user text is never user-stated: the agent channel captured it.
            self.assertEqual(origins[("compiler", "owned_by")], "agent-observed")
            derived = next(e for e in store.events(OWNER) if e.get("origin") == "tool-derived")
            self.assertEqual(derived["citations"][0]["kind"], "episode")
            with self.assertRaises(LumenError):
                proposal(store, OWNER, [derived["id"]])
            with self.assertRaises(LumenError):
                turn(store, 9, "x", tools=[{"name": "bash", "output": "y"}] * 33)

    def test_runs_are_idempotent_interval_gated_and_atomic_per_episode(self):
        with tempfile.TemporaryDirectory() as td, Store(td) as store:
            turn(store, 1, "compiler test_command: python -m unittest")
            consolidator = Consolidator(store)
            first = consolidator.run(OWNER, now_ns=1_000_000_000_000, force=True)
            self.assertEqual(first["assertions"], 1)
            self.assertEqual(consolidator.run(OWNER, now_ns=1_000_000_000_000)["skipped"], "no_new_episodes")
            turn(store, 2, "compiler test_command: python -m unittest")
            self.assertEqual(consolidator.run(OWNER, now_ns=1_000_000_000_000 + 10)["skipped"], "interval")
            self.assertEqual(consolidator.run(OWNER, now_ns=1_000_000_000_000 + 61_000_000_000)["evidence"], 1)
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM extractions").fetchone()[0], 2)
            turn(store, 3, "compiler test_command: python -m pytest")
            before = len(store.events(OWNER))

            def fault(point):
                if point == "after_extraction":
                    raise RuntimeError("crash before commit")
            store.fault = fault
            with self.assertRaises(RuntimeError):
                consolidator.run(OWNER, force=True)
            self.assertEqual(len(store.events(OWNER)), before)
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM extractions").fetchone()[0], 2)
            store.fault = lambda _: None
            report = consolidator.run(OWNER, force=True)
            self.assertEqual(report["disputes"], 1)
            self.assertEqual(consolidator.run(OWNER, force=True)["skipped"], "no_new_episodes")
            self.assertEqual(sum(e["kind"] == "dispute" for e in store.events(OWNER)), 1)
            with self.assertRaises(LumenError):
                consolidator.run(AGENT, force=True)

    def test_revision_and_purge_invalidate_dependent_summaries(self):
        with tempfile.TemporaryDirectory() as td, Store(td) as store:
            turn(store, 1, "compiler test_command: python -m unittest")
            Consolidator(store).run(OWNER, force=True)
            summaries = store.derivatives("w", "summary")
            self.assertEqual(len(summaries), 1)
            self.assertEqual(summaries[0]["body"]["values"], ["python -m unittest"])
            self.assertFalse(summaries[0]["stale"])
            status, values, packet = current(store)
            aid = packet["assertions"][0]["id"]
            self.assertIn(aid, summaries[0]["depends_on"])
            result = store.revise(OWNER, [aid], packet["state_token"], packet["assertions"][0]["region"], "retracted")
            self.assertEqual(result["derivatives_invalidated"], [summaries[0]["id"]])
            self.assertTrue(store.derivatives("w", "summary")[0]["stale"])
            self.assertEqual(store.derivative_status("w")["kinds"]["summary"], {"current": 0, "stale": 1})
            Consolidator(store).run(OWNER, force=True)
            self.assertEqual(Consolidator(store).run(OWNER, force=True)["skipped"], "no_new_episodes")
            regenerated = store.derivatives("w", "summary")[0]
            self.assertFalse(regenerated["stale"])
            self.assertEqual(regenerated["body"]["values"], [])
            store.purge(OWNER, aid)
            self.assertTrue(store.derivatives("w", "summary")[0]["stale"])
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM extractions").fetchone()[0], 1)
            episode_id = next(e["id"] for e in store.events(OWNER) if e["kind"] == "episode")
            store.purge(OWNER, episode_id)
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM extractions").fetchone()[0], 0)
            service = Service(store)
            report = service.call(OWNER, "doctor", {})["result"]
            self.assertEqual(report["consolidation"]["judge"], "disabled")
            self.assertIn("derivatives", report)
            self.assertEqual(service.call(OWNER, "consolidate", {"force": True})["result"]["skipped"], "no_new_episodes")
            self.assertEqual(service.call(AGENT, "consolidate", {})["error"]["code"], "not_authorized")


if __name__ == "__main__":
    unittest.main()
