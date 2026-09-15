"""Phase 5 offline retrieval: identifier variants, source-backed aliases, relation and evidence expansion,
span-overlap demotion, scope widening with nearness ranking, and the scale measurement tool."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from lumen.model import Access, LumenError, episode, new_event, ref, region
from lumen.service import Service
from lumen.store import Store, identifier_variants, span_key
from support_sources import citation, source_workspace
from test_core import OWNER, capture

ROOT = Path(__file__).resolve().parents[1]


def remember(store, access, turn, subject, relation, value, text=None, scope="repo:p", **extra):
    return store.remember(access, checkout="co", host="cli", session="s", turn=turn, scope=scope, subject=subject,
                          relation=relation, value=value, text=text or f"{subject} {relation} {value}",
                          region=region(start=0), **extra)


class IdentifierAndAliasTests(unittest.TestCase):
    def test_identifier_variants_cover_case_and_separator_forms(self):
        variants = set(identifier_variants("tests/test_check_evidence.py"))
        self.assertIn("test_check_evidence.py", variants)
        self.assertIn("check", variants)
        self.assertIn("tests_test_check_evidence_py", variants)
        self.assertEqual(set(identifier_variants("checkEvidence")) >= {"checkevidence", "check", "evidence", "check_evidence"}, True)
        self.assertLessEqual(len(identifier_variants("a" + "_b" * 100)), 32)

    def test_variants_find_a_fact_by_any_spelling(self):
        with tempfile.TemporaryDirectory() as td, Store(td) as store:
            remember(store, OWNER, "1", "checkEvidence", "test_command", "python -m unittest tests/test_check_evidence.py")
            store.reindex(OWNER)
            for query in ("check_evidence", "checkevidence", "test_check_evidence.py", "CheckEvidence", "check evidence"):
                packet = store.recall(OWNER, query)
                self.assertEqual(len(packet["results"]), 1, query)
                self.assertEqual(packet["signals"]["identifier_variants"], "ran")

    def test_source_backed_aliases_expand_once_and_ambiguity_expands_nothing(self):
        with tempfile.TemporaryDirectory() as td, Store(td) as store:
            remember(store, OWNER, "1", "identity-service", "required_encoding", "utf-8", "The identity service writes utf-8 everywhere")
            self.assertEqual(store.recall(OWNER, "ids")["results"], []) if store.reindex(OWNER) else None
            remember(store, OWNER, "2", "IDS", "alias_of", "identity-service", "IDS is the identity service", citations=[citation()])
            store.reindex(OWNER)
            packet = store.recall(OWNER, "ids")
            self.assertEqual(packet["signals"]["aliases"], "expanded")
            self.assertEqual(packet["expansions"]["alias_terms"], ["identity-service"])
            self.assertIn("identity-service", [p["assertions"][0]["subject"] for p in packet["results"]])
            remember(store, OWNER, "3", "IDS", "alias_of", "intrusion-detection", "IDS also means intrusion detection", citations=[citation()])
            remember(store, OWNER, "4", "intrusion-detection", "required_encoding", "ascii")
            store.reindex(OWNER)
            packet = store.recall(OWNER, "ids")
            self.assertEqual(packet["signals"]["aliases"], "ambiguous:ids")
            self.assertEqual(packet["expansions"]["alias_terms"], [])
            subjects = {p["assertions"][0]["subject"] for p in packet["results"]}
            self.assertNotIn("intrusion-detection", subjects)
            self.assertNotIn("identity-service", subjects)
            # An alias declared outside the grant never expands a query, and a purged alias vanishes.
            other = Access("w", frozenset({"repo:other"}), OWNER.actor, True)
            self.assertEqual(store.recall(other, "ids")["signals"]["aliases"], "ran")
            alias_ids = [e["id"] for e in store.events(OWNER) if e.get("relation") == "alias_of"]
            store.purge(OWNER, alias_ids[1])
            self.assertEqual(store.recall(OWNER, "ids")["signals"]["aliases"], "expanded")


class ExpansionAndRankingTests(unittest.TestCase):
    def test_relation_expansion_joins_one_hop_and_evidence_text_is_searchable(self):
        with tempfile.TemporaryDirectory() as td, Store(td) as store:
            remember(store, OWNER, "1", "gateway", "uses_library", "libcrypto", "gateway links the TLS dependency")
            remember(store, OWNER, "2", "libcrypto", "required_encoding", "utf-8", "libcrypto sources are utf-8")
            remember(store, OWNER, "3", "unrelated", "required_encoding", "latin-1", "unrelated legacy sources")
            store.reindex(OWNER)
            self.assertEqual(store.recall(OWNER, "unicode text files")["results"], [])
            target = next(e for e in store.events(OWNER) if e["subject"] == "libcrypto")
            evidence = new_event("evidence", "w", "repo:p", OWNER.actor, target=ref(target),
                                 citations=[episode("The crypto library's tree is entirely unicode text files")])
            with store.transaction():
                store._validate_write(evidence, OWNER)
                store._insert(evidence)
            store.reindex(OWNER)
            self.assertTrue(store.doctor()["healthy"])
            # The paraphrase lives only in the evidence citation; the fact is found through it, and the
            # one-hop relation join then adds the fact whose value names that subject.
            packet = store.recall(OWNER, "unicode text files")
            subjects = [p["assertions"][0]["subject"] for p in packet["results"]]
            self.assertEqual(subjects, ["libcrypto", "gateway"])
            self.assertEqual(len(packet["results"][0]["evidence"][target["id"]]), 2)
            self.assertEqual(packet["results"][1]["via"], "relation:libcrypto")
            self.assertEqual(packet["expansions"]["relation_subjects"], ["libcrypto"])
            self.assertEqual(packet["signals"]["relations"], "ran")
            other = Access("w", frozenset({"repo:other"}), OWNER.actor, True)
            self.assertEqual(store.recall(other, "unicode text files")["results"], [])

    def test_span_overlap_is_demoted_not_dropped(self):
        with tempfile.TemporaryDirectory() as td, Store(td) as store:
            shared = citation()
            remember(store, OWNER, "1", "encoder", "required_encoding", "utf-8", "encoder utf-8 per contract", citations=[shared])
            remember(store, OWNER, "2", "decoder", "required_encoding", "utf-8", "decoder utf-8 per contract", citations=[shared])
            remember(store, OWNER, "3", "printer", "required_encoding", "utf-8", "printer utf-8 per contract", citations=[citation("a.txt")])
            store.reindex(OWNER)
            packet = store.recall(OWNER, "utf-8 per contract")
            self.assertEqual(len(packet["results"]), 3)
            self.assertEqual(packet["span_overlaps"], 1)
            demoted = packet["results"][-1]
            self.assertTrue(demoted.get("span_overlap"))
            self.assertIn(demoted["assertions"][0]["subject"], {"encoder", "decoder"})
            self.assertEqual({span_key(c) for c in demoted["evidence"][demoted["assertions"][0]["id"]]}, {span_key(shared)})
            self.assertFalse(any(p.get("span_overlap") for p in packet["results"][:2]))
            self.assertEqual(packet["signals"]["span_dedupe"], "ran")

    def test_scope_widens_from_repo_to_monorepo_to_dependency_to_user_nearest_first(self):
        with tempfile.TemporaryDirectory() as td, Store(Path(td) / "store") as store:
            workspace = source_workspace(Path(td) / "sources")
            path = workspace.root / ".lumen/workspace.json"
            manifest = json.loads(path.read_text())
            manifest["projects"].append({"id": "consumer", "root": "consumer", "kind": "project", "purpose": "consumer",
                                         "owners": ["owner"], "depends_on": ["p"]})
            manifest["projects"].append({"id": "far", "root": "far", "kind": "project", "purpose": "far",
                                         "owners": ["owner"], "depends_on": []})
            (workspace.root / "consumer").mkdir()
            (workspace.root / "far").mkdir()
            path.write_text(json.dumps(manifest))
            workspace.refresh_policy()
            grant = Access("w", workspace.visible_scopes("consumer"), OWNER.actor, True)
            self.assertNotIn("repo:far", grant.scopes)
            for turn, scope in (("u", "user"), ("d", "repo:p"), ("m", "monorepo"), ("c", "repo:consumer")):
                remember(store, grant, turn, "codec-" + scope.replace(":", "-"), "required_encoding", "utf-8",
                         "codec uses utf-8 in " + scope, scope=scope)
            far = Access("w", frozenset({"repo:far"}), OWNER.actor, True)
            remember(store, far, "f", "codec-far", "required_encoding", "utf-8", "codec uses utf-8 in far", scope="repo:far")
            store.reindex(OWNER)
            service = Service(store, workspace, "consumer")
            packet = service.call(grant, "recall", {"query": "codec utf-8"})["result"]
            self.assertEqual([p["scope"] for p in packet["results"]], ["repo:consumer", "monorepo", "repo:p", "user"])
            self.assertEqual([p["nearness"] for p in packet["results"]], [1, 2, 3, 4])
            self.assertEqual(packet["scopes_searched"][:4], ["repo:consumer", "monorepo", "repo:p", "user"])
            self.assertEqual(packet["signals"]["scope_widening"], "ran")
            self.assertNotIn("repo:far", packet["scopes_searched"])
            flat = store.recall(grant, "codec utf-8")
            self.assertEqual(flat["signals"]["scope_widening"], "flat")
            self.assertEqual({p["scope"] for p in flat["results"]}, {"repo:consumer", "monorepo", "repo:p", "user"})
            with self.assertRaises(LumenError):
                store.recall(grant, "codec", nearness={"repo:p": "near"})
            self.assertEqual(service.call(grant, "recall", {"query": "codec", "nearness": {}})["error"]["code"], "not_authorized")


class ScaleToolTests(unittest.TestCase):
    def test_measurement_tool_records_core_ipc_and_session_start_distributions(self):
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "scale.json"
            run = subprocess.run([sys.executable, str(ROOT / "tools/measure-scale.py"), "600", "--ipc", "--clients", "2",
                                  "--output", str(output)], capture_output=True, text=True, timeout=600, cwd=ROOT)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["records"], 600)
            for key in ("direct_core", "ipc_recall", "session_start_endpoint"):
                self.assertIn("p95_us", report[key], key)
                self.assertEqual(report[key]["n"], {"direct_core": 100, "ipc_recall": 50}.get(key, 20))
            # From the source tree the isolated launcher cannot import lumen; the tool says so instead of faking it.
            self.assertTrue("p95_us" in report["session_start_launcher"] or "skipped" in report["session_start_launcher"])
            self.assertTrue(report["runtime_unchanged"])
            self.assertFalse(report["r14"]["records_at_100k"])
            self.assertFalse(report["r14"]["four_clients"])
            self.assertEqual(report["additional_cost_usd"], 0)


if __name__ == "__main__":
    unittest.main()
