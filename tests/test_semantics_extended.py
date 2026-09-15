import itertools
from pathlib import Path
import tempfile
import unittest

from lumen.model import Access, LumenError, new_event, ref, region, seal
from lumen.projection import project
from lumen.store import Store
from test_core import OWNER, assertion, capture, revision


class ExtendedSemanticsTests(unittest.TestCase):
    def test_deep_reference_closure_propagates_pending_and_rejects_cycle(self):
        from lumen.projection import closure
        base = assertion()
        events = [base]
        for _ in range(1500):
            events.append(new_event('approval', 'w', 'repo:p', 'local-owner',
                targets=[{'ref': ref(events[-1]), 'digest': events[-1]['digest']}], destination='shared', policy=1))
        admitted, pending = closure(list(reversed(events)))
        self.assertEqual(len(admitted), 1501)
        self.assertFalse(pending)
        admitted, pending = closure(events[1:])
        self.assertFalse(admitted)
        self.assertEqual(len(pending), 1500)
        cyclic = [seal({**events[1], 'targets': [{'ref': ref(events[-1]), 'digest': events[-1]['digest']}]})] + events[2:]
        with self.assertRaises(LumenError) as error:
            closure(cyclic)
        self.assertIn('Reference cycle', str(error.exception))

    def test_deep_revision_history_and_cycle_without_recursion(self):
        from lumen.projection import closure
        assertions = [assertion(str(i)) for i in range(1500)]
        changes = [revision(a, b) for a, b in zip(assertions, assertions[1:])]
        events = assertions + changes
        admitted, pending = closure(events)
        self.assertEqual(len(admitted), len(events))
        self.assertFalse(pending)
        self.assertEqual(closure(list(reversed(events))), (admitted, pending))
        with self.assertRaises(LumenError) as error:
            closure(events + [revision(assertions[-1], assertions[0])])
        self.assertIn('causality cycle', str(error.exception))

    def test_reconciliation_cannot_select_inapplicable_alternative(self):
        windows = assertion('B', region(platform='windows', start=10, end=20))
        other = assertion('C')
        for affected in (region(start=10, end=20), region(platform='windows', start=0, end=20),
                         region(platform='windows', start=10, end=30)):
            resolution = new_event('reconciliation', 'w', 'repo:p', 'local-owner',
                alternatives=[ref(windows), ref(other)], selected=ref(windows), affected=affected,
                reason='Selected option must actually apply')
            with self.assertRaises(LumenError):
                project([windows, other, resolution], 'w', 'compiler', 'test_command', 15, 'windows')
        valid = new_event('reconciliation', 'w', 'repo:p', 'local-owner',
            alternatives=[ref(windows), ref(other)], selected=ref(windows),
            affected=region(platform='windows', start=10, end=20), reason='Bounded selection')
        self.assertEqual(project([windows, other, valid], 'w', 'compiler', 'test_command', 15, 'windows')['assertions'], [windows])

    def test_disjoint_forks_and_reconciliation(self):
        a = assertion()
        b = assertion("B", region(platform="windows", start=0))
        c = assertion("C", region(platform="linux", start=0))
        disjoint = [a, b, c, revision(a, b, region(platform="windows", start=10)), revision(a, c, region(platform="linux", start=10))]
        for platform, expected in [("windows", "B"), ("linux", "C"), ("macos", "A")]:
            p = project(disjoint, "w", "compiler", "test_command", 20, platform)
            self.assertEqual(p["status"], "current")
            self.assertEqual(p["assertions"][0]["value"], expected)
        b, c = assertion("B"), assertion("C")
        controls = dict(alternatives=[ref(b), ref(c)], affected=region(start=10), reason="owner resolution")
        dispute = new_event("dispute", "w", "repo:p", "local-owner", **controls)
        reconciled = new_event("reconciliation", "w", "repo:p", "local-owner", **controls, selected=ref(b))
        events = [a, b, c, revision(a, b), revision(a, c), dispute, reconciled]
        p = project(events, "w", "compiler", "test_command", 20)
        self.assertEqual(p["status"], "current")
        self.assertEqual(p["assertions"], [b])
        other = new_event("reconciliation", "w", "repo:p", "local-owner", **controls, selected=ref(c))
        self.assertEqual(project(events + [other], "w", "compiler", "test_command", 20)["status"], "disputed")

    def test_bad_region_cannot_retire_other_platforms(self):
        a, b = assertion(), assertion("B", region(platform="windows", start=0))
        with self.assertRaises(LumenError):
            project([a, b, revision(a, b)], "w", "compiler", "test_command", 20, "linux")

    def test_undeclared_multi_values_never_automatically_retire(self):
        a, b = [seal({**assertion(v), "relation": "unknown_relation"}) for v in ["A", "B"]]
        p = project([a, b], "w", "compiler", "unknown_relation", 1)
        self.assertEqual(p["status"], "current")
        self.assertEqual(len(p["assertions"]), 2)

    def test_same_names_in_unrelated_scopes_do_not_merge(self):
        with tempfile.TemporaryDirectory() as td, Store(td) as store:
            capture(store)
            other = Access("w", frozenset({"repo:other"}), OWNER.actor, True)
            store.remember(other, checkout="co", host="cli", session="s", turn="2", scope="repo:other",
                           subject="compiler", relation="test_command", value="B", text="compiler B", region=region(start=0))
            store.reindex(OWNER)
            combined = Access("w", frozenset({"repo:p", "repo:other"}), OWNER.actor, True)
            packets = store.recall(combined, "compiler")["results"]
            self.assertEqual(len(packets), 2)
            self.assertTrue(all(p["status"] == "current" for p in packets))

    def test_duplicate_ids_in_different_workspaces_remain_isolated(self):
        with tempfile.TemporaryDirectory() as td, Store(td) as store:
            a = assertion()
            b = seal({**a, "workspace": "w2", "value": "B"})
            with store.transaction():
                store._insert(a)
                store._insert(b)
            store.reindex(OWNER)
            other = Access("w2", OWNER.scopes, OWNER.actor, True)
            self.assertEqual(store.recall(OWNER, "compiler")["results"][0]["assertions"][0]["value"], "A")
            self.assertEqual(store.recall(other, "compiler")["results"][0]["assertions"][0]["value"], "B")

    def test_snapshot_never_regresses_after_purge(self):
        with tempfile.TemporaryDirectory() as td, Store(td) as store:
            a = capture(store)
            store.purge(OWNER, a["id"])
            self.assertEqual(store.watermark(), a["snapshot"])
