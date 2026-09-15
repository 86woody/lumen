"""The share branch-and-batch flow: branches per repo batch in a real repository, no push, no PR."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from lumen.model import Access, digest, region
from lumen.service import Service
from lumen.sharing import approve, branch_share, list_shares, proposal
from lumen.store import Store
from test_workspace import run_git, topology


class ShareFlowTests(unittest.TestCase):
    def prepare(self, count=5, policy='schema = 1\n[share]\nbatch_limit = 2\npull_request = "pending"\n'):
        td = self.enterContext(tempfile.TemporaryDirectory())
        workspace = topology(Path(td) / "workspace", "logical")
        (workspace.root / ".lumen/config.toml").write_text(policy, encoding="utf-8")
        manifest_path = workspace.root / ".lumen/workspace.json"
        manifest = json.loads(manifest_path.read_text())
        for project in manifest["projects"]:
            project["owners"] = ["@fixture-owner"]
        manifest_path.write_text(json.dumps(manifest))
        workspace.refresh_policy()
        workspace.manage_codeowners("write")
        access = Access(workspace.id, workspace.visible_scopes("producer"), "fixture-owner", True)
        source = workspace.root / "producer/contract.txt"
        citation = {"kind": "file", "project": "producer", "path": "contract.txt", "revision": "fixture",
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "anchor": "codec"}
        producer, destination = Store(Path(td) / "producer-store"), Store(Path(td) / "destination")
        self.addCleanup(producer.close)
        self.addCleanup(destination.close)
        ids = [producer.remember(access, checkout="co", host="fixture", session="s", turn=str(i), scope="repo:producer",
                             subject="codec" + str(i), relation="required_encoding", value="UTF-8", text="codec UTF-8",
                             region=region(start=0), citations=[citation])["id"] for i in range(count)]
        bundle = proposal(producer, access, ids)
        approve(destination, access, bundle, digest(bundle), workspace)
        self.citation = citation
        return workspace, access, destination, bundle, ids

    def test_branches_per_repo_batch_leave_the_checkout_untouched(self):
        workspace, access, store, bundle, ids = self.prepare()
        before_status = run_git(workspace.root, "status", "--porcelain")
        record = branch_share(store, access, bundle, workspace)
        self.assertEqual(record["pull_request"]["status"], "pending")
        self.assertFalse(record["pushed"])
        self.assertEqual(len(record["batches"]), 3)
        self.assertEqual([len(b["events"]) for b in record["batches"]], [2, 2, 1])
        self.assertEqual(sorted(e for b in record["batches"] for e in b["events"]), sorted(ids))
        self.assertEqual(run_git(workspace.root, "branch", "--show-current").strip(), "fixture")
        self.assertEqual(run_git(workspace.root, "status", "--porcelain"), before_status)
        self.assertFalse((workspace.root / ".lumen/shared").exists())
        self.assertFalse(list((store.home / "worktrees").iterdir()))
        self.assertEqual(len(run_git(workspace.root, "worktree", "list").strip().splitlines()), 1)
        for batch in record["batches"]:
            self.assertTrue(batch["branch"].startswith("lumen/share/producer/"))
            listed = run_git(workspace.root, "ls-tree", "-r", "--name-only", batch["branch"], "--", ".lumen/shared/producer")
            self.assertEqual(sorted(Path(line).stem for line in listed.split()), sorted(batch["events"]))
            self.assertEqual(run_git(workspace.root, "rev-parse", batch["branch"] + "~1").strip(), batch["base"])
            self.assertIn("Reviewed bundle " + record["bundle_digest"], run_git(workspace.root, "log", "-1", "--format=%B", batch["branch"]))
        again = branch_share(store, access, bundle, workspace)
        self.assertTrue(again["already_branched"])
        self.assertEqual(len(run_git(workspace.root, "branch", "--list", "lumen/share/producer/*").split()), 3)
        self.assertEqual([s["bundle_digest"] for s in list_shares(store, access)], [record["bundle_digest"]])
        service = Service(store, workspace)
        pending = service.call(access, "pending", {})["result"]
        self.assertEqual(len(pending["awaiting_pull_request"]), 3)
        self.assertEqual(pending["pull_request_mode"], "pending")
        agent = Access(access.workspace, access.scopes, access.actor, False)
        self.assertEqual(service.call(agent, "pending", {})["error"]["code"], "not_authorized")
        self.assertEqual(service.call(agent, "share", {"action": "branch", "bundle": bundle})["error"]["code"], "not_authorized")
        self.assertTrue(service.call(access, "share", {"action": "branch", "bundle": bundle})["result"]["already_branched"])
        (store.home / "shares" / (record["bundle_digest"] + ".json")).unlink()
        self.assertTrue(all(b["reused"] for b in branch_share(store, access, bundle, workspace)["batches"]))

    def test_branching_requires_review_and_refuses_erased_events(self):
        workspace, access, store, bundle, ids = self.prepare(count=1)
        from lumen.model import LumenError
        with self.assertRaises(LumenError):
            branch_share(store, access, {**bundle, "events": []}, workspace)
        store.journal.add([(workspace.id, ids[0])])
        with self.assertRaises(LumenError):
            branch_share(store, access, bundle, workspace)
        self.assertEqual(run_git(workspace.root, "branch", "--list", "lumen/share/*"), "")
        self.assertFalse((store.home / "shares").exists())

    def test_audit_ci_accepts_only_an_explicitly_configured_pending_authority(self):
        workspace, access, store, bundle, ids = self.prepare(count=1)
        service = Service(store, workspace)
        config = workspace.root / ".lumen/config.toml"
        result = service.call(access, "audit", {"ci": True})["result"]
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["checks"]["repository_review_authority"]["status"], "pending")
        self.assertTrue(result["checks"]["repository_review_authority"]["configured"])
        self.assertEqual(result["accepted_pending"], ["repository_review_authority"])
        self.assertEqual(result["checks"]["pull_request_branches"]["awaiting_pull_request"], 0)
        branch_share(store, access, bundle, workspace)
        result = service.call(access, "audit", {"ci": True})["result"]
        self.assertEqual(result["checks"]["pull_request_branches"]["awaiting_pull_request"], 1)
        config.write_text('schema = 1\n[share]\npull_request = "forge"\n', encoding="utf-8")
        result = service.call(access, "audit", {"ci": True})["result"]
        self.assertFalse(result["passed"])
        self.assertEqual(result["checks"]["repository_review_authority"]["status"], "failed")
        config.write_text("schema = 1\n", encoding="utf-8")
        result = service.call(access, "audit", {"ci": True})["result"]
        self.assertFalse(result["passed"])
        self.assertFalse(result["checks"]["repository_review_authority"]["configured"])
        self.assertEqual(result["accepted_pending"], [])
        cli = subprocess.run([sys.executable, "-m", "lumen", "--help"], capture_output=True, text=True, timeout=30)
        self.assertIn("pending", cli.stdout)

    def test_audit_ci_rejects_silent_competing_successors_and_accepts_explicit_disputes(self):
        from lumen.model import new_event, ref
        from lumen.sharing import stage
        workspace, access, store, bundle, ids = self.prepare(count=1)
        producer = Store(Path(self.enterContext(tempfile.TemporaryDirectory())) / "p")
        self.addCleanup(producer.close)
        base = producer.remember(access, checkout="co", host="fixture", session="s", turn="base", scope="repo:producer",
                                 subject="codec", relation="required_encoding", value="UTF-8", text="codec UTF-8",
                                 region=region(start=0), citations=[self.citation])["id"]
        producer.reindex(access)
        token = producer.recall(access, "codec", at=5)["results"][0]["state_token"]
        successors = []
        # Two machines revise the same predecessor against the same token; a merge carries both.
        for value in ("UTF-16", "UTF-32"):
            successor = new_event("assertion", workspace.id, "repo:producer", access.actor, subject="codec",
                                  relation="required_encoding", value=value, text="codec " + value, origin="user-stated",
                                  citations=[self.citation], region=region(start=0))
            fork = new_event("revision", workspace.id, "repo:producer", access.actor,
                             predecessors=[{"workspace": workspace.id, "id": base}], successor=ref(successor),
                             revision_kind="change", affected=region(start=10), reason="fork " + value, expected_state=token)
            with producer.transaction():
                for event in (successor, fork):
                    producer._validate_write(event, access)
                    producer._insert(event)
            successors.append(successor["id"])
        producer.reindex(access)
        forked = proposal(producer, access, successors)
        approve(store, access, forked, digest(forked), workspace)
        stage(store, access, forked, workspace.root / ".lumen/shared", workspace)
        service = Service(store, workspace)
        result = service.call(access, "audit", {"ci": True})["result"]
        self.assertFalse(result["passed"])
        check = result["checks"]["conflicts_and_type_specific_evidence"]
        self.assertEqual(check["status"], "failed")
        self.assertEqual(check["findings"][0]["reason"], "silent_competing_successors")
        dispute = new_event("dispute", workspace.id, "repo:producer", access.actor,
                            alternatives=[{"workspace": workspace.id, "id": s} for s in successors],
                            affected=region(start=10), reason="Both encodings claimed; owners to reconcile")
        with producer.transaction():
            producer._validate_write(dispute, access)
            producer._insert(dispute)
        disputed = proposal(producer, access, successors + [dispute["id"]])
        approve(store, access, disputed, digest(disputed), workspace)
        stage(store, access, disputed, workspace.root / ".lumen/shared", workspace)
        result = service.call(access, "audit", {"ci": True})["result"]
        self.assertEqual(result["checks"]["conflicts_and_type_specific_evidence"]["status"], "passed")
        self.assertTrue(result["passed"], result)


if __name__ == "__main__":
    unittest.main()
