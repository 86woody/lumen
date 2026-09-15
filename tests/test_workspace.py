import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from lumen.model import Access, LumenError, digest, new_event, region
from lumen.sharing import approve, import_reviewed, proposal, stage
from lumen.store import Store
from lumen.service import Service
from lumen.workspace import Workspace, git, git_environment


def run_git(path, *args):
    command = ["git", "-C", str(path), "-c", "user.name=Lumen fixture", "-c", "user.email=fixture@localhost", *args]
    p = subprocess.run(command, capture_output=True, text=True, env=git_environment())
    if p.returncode:
        raise RuntimeError(p.stderr)
    return p.stdout


def topology(root, kind):
    root.mkdir(parents=True)
    run_git(root, "init", "-b", "fixture")
    projects = []
    for name in ("producer", "consumer", "contracts"):
        path = root / name
        if kind == "submodules":
            path = root.parent / (name + "-origin")
        path.mkdir()
        (path / "contract.txt").write_text("codec UTF-8\n", encoding="utf-8")
        if kind in ("nested", "submodules"):
            run_git(path, "init", "-b", "fixture")
            run_git(path, "add", "contract.txt")
            run_git(path, "commit", "-m", "Fixture source")
        if kind == "submodules":
            run_git(root, "-c", "protocol.file.allow=always", "submodule", "add", str(path), name)
        projects.append({"id": name, "root": name, "kind": "library", "purpose": name,
                         "owners": ["fixture-owner"], "depends_on": ["producer"] if name == "consumer" else []})
    Workspace.initialize(root, projects)
    run_git(root, "add", ".")
    run_git(root, "commit", "-m", "Semantics fixture")
    return Workspace(root, root.parent / "home")


class WorkspaceTests(unittest.TestCase):
    def test_three_topologies_move_retains_id_and_citations(self):
        for kind in ("logical", "nested", "submodules"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as td:
                workspace = topology(Path(td) / "workspace", kind)
                self.assertEqual(workspace.resolve(workspace.root / "consumer"), "consumer")
                self.assertIn("repo:producer", workspace.visible_scopes("consumer"))
                workspace.build()
                self.assertTrue(workspace.check()["healthy"])
                original_hash = hashlib.sha256((workspace.root / "producer/contract.txt").read_bytes()).hexdigest()
                (workspace.root / "producer").rename(workspace.root / "producer-moved")
                self.assertFalse(workspace.check()["healthy"])
                manifest = workspace.manifest
                manifest["projects"][0]["root"] = "producer-moved"
                (workspace.root / ".lumen/workspace.json").write_text(json.dumps(manifest))
                if kind == "submodules":
                    modules = workspace.root / ".gitmodules"
                    modules.write_text(modules.read_text().replace("path = producer\n", "path = producer-moved\n"))
                moved = Workspace(workspace.root, workspace.home)
                moved.build()
                self.assertEqual(moved.resolve(workspace.root / "producer-moved"), "producer")
                self.assertEqual(moved.id, workspace.id)
                self.assertEqual(moved.checkout, workspace.checkout)
                listed = subprocess.run([sys.executable, '-m', 'lumen', '--home', str(workspace.home),
                                         'repos', '--workspace', str(workspace.root)],
                                        capture_output=True, text=True, timeout=15)
                self.assertEqual(listed.returncode, 0, listed.stderr)
                projects = {p['id']: p for p in json.loads(listed.stdout)['projects']}
                self.assertEqual(projects['producer']['root'], 'producer-moved')
                self.assertEqual(projects['producer']['availability'], 'current')
                self.assertTrue((Path(moved.roots()["producer"]) / "contract.txt").is_file())
                access = Access(workspace.id, frozenset({"repo:producer"}), "fixture-owner", True)
                citation = {"kind": "file", "project": "producer", "path": "contract.txt", "revision": "fixture",
                    "sha256": original_hash, "anchor": "codec"}
                with Store(Path(td) / "store") as store:
                    store.remember(access, checkout=workspace.checkout, host="fixture", session="s", turn="1",
                        scope="repo:producer", subject="codec", relation="required_encoding", value="UTF-8",
                        text="codec UTF-8", region=region(start=0), citations=[citation])
                    store.reindex(access)
                    packet = Service(store, moved).call(access, "recall", {"query": "codec"})
                    self.assertNotIn("error", packet, packet)
                    self.assertTrue(packet["result"]["results"][0]["receipts"][0]["fresh"])

    def test_sparse_and_uninitialized_coverage(self):
        for mode in ("sparse", "uninitialized"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as td:
                workspace = topology(Path(td) / "workspace", "submodules")
                access = Access(workspace.id, workspace.visible_scopes("consumer"), "fixture-owner", True)
                citation = {"kind": "file", "project": "producer", "path": "contract.txt", "revision": "fixture",
                    "sha256": hashlib.sha256((workspace.root / "producer/contract.txt").read_bytes()).hexdigest(), "anchor": "codec"}
                with Store(Path(td) / "store") as store:
                    store.remember(access, checkout=workspace.checkout, host="fixture", session="s", turn="1",
                        scope="repo:producer", subject="codec", relation="required_encoding", value="UTF-8",
                        text="codec UTF-8", region=region(start=0), citations=[citation])
                    store.reindex(access)
                    if mode == "sparse":
                        run_git(workspace.root / "producer", "sparse-checkout", "init", "--cone")
                    else:
                        run_git(workspace.root, "submodule", "deinit", "-f", "--", "producer")
                    packet = Service(store, workspace).call(access, "recall", {"query": "codec"})["result"]
                    self.assertIn("producer", packet["repos_skipped"])
                    self.assertNotIn("producer", packet["repos_searched"])
                    self.assertIn("consumer", packet["repos_searched"])
                    receipt = packet["results"][0]["receipts"][0]
                    self.assertFalse(receipt["resolved"])
                    self.assertFalse(receipt["fresh"])
                    self.assertEqual(next(p for p in packet["repos"] if p["id"] == "producer")["availability"], "unavailable")

    def test_receipts_do_not_dirty_checkout(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = topology(Path(td) / "workspace", "logical")
            access = Access(workspace.id, frozenset({"repo:producer"}), "fixture-owner", True)
            citation = {"kind": "file", "project": "producer", "path": "contract.txt", "revision": git(workspace.root, "rev-parse", "HEAD"),
                        "sha256": hashlib.sha256((workspace.root / "producer/contract.txt").read_bytes()).hexdigest(), "anchor": "codec"}
            with Store(Path(td) / "store") as store:
                store.remember(access, checkout=workspace.checkout, host="fixture", session="s", turn="1",
                               scope="repo:producer", subject="codec", relation="required_encoding", value="UTF-8",
                               text="codec UTF-8", region=region(start=0), citations=[citation])
                store.reindex(access)
                for _ in range(1000):
                    packet = store.recall(access, "codec", roots=workspace.roots())
                    self.assertTrue(packet["results"][0]["receipts"][0]["fresh"])
                self.assertEqual(git(workspace.root, "status", "--porcelain"), "")

    def test_two_home_review_requires_authority_and_preserves_origin(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = topology(Path(td) / "workspace", "logical")
            access = Access(workspace.id, frozenset({"repo:producer"}), "fixture-owner", True)
            agent = Access(workspace.id, access.scopes, access.actor, False)
            citation = {"kind": "file", "project": "producer", "path": "contract.txt", "revision": "fixture",
                        "sha256": hashlib.sha256((workspace.root / "producer/contract.txt").read_bytes()).hexdigest(), "anchor": "codec"}
            with Store(Path(td) / "a") as a, Store(Path(td) / "b") as b:
                eid = a.remember(agent, checkout="co", host="fixture", session="s", turn="1", scope="repo:producer",
                                 subject="codec", relation="required_encoding", value="UTF-8", text="codec UTF-8",
                                 region=region(start=0), citations=[citation])["id"]
                bundle = proposal(a, access, [eid])
                with self.assertRaises(LumenError):
                    import_reviewed(b, access, bundle, workspace=workspace)
                with self.assertRaises(LumenError):
                    approve(b, agent, bundle, digest(bundle))
                approve(b, access, bundle, digest(bundle), workspace=workspace)
                import_reviewed(b, access, bundle, workspace=workspace)
                self.assertEqual(b.expand(access, eid)["event"]["origin"], "agent-observed")
                poisoned = {**bundle, "events": []}
                with self.assertRaises(LumenError):
                    import_reviewed(b, access, poisoned, workspace=workspace)
                stage(b, access, bundle, Path(td) / "staged", workspace=workspace)
                other_owner = Access(workspace.id, access.scopes, "different-owner", True)
                denied_scope = Access(workspace.id, frozenset(), access.actor, True)
                for denied in (agent, other_owner, denied_scope):
                    with self.assertRaises(LumenError):
                        stage(b, denied, bundle, Path(td) / "denied", workspace=workspace)
                self.assertFalse((Path(td) / "denied").exists())
                approval_path = b.home / "approvals" / (digest(bundle) + ".json")
                original_receipt = approval_path.read_bytes()
                approval_path.write_text('{}')
                with self.assertRaises(LumenError):
                    stage(b, access, bundle, Path(td) / "corrupt-review", workspace=workspace)
                self.assertFalse((Path(td) / "corrupt-review").exists())
                approval_path.write_bytes(original_receipt)
                b.purge(access, eid)
                self.assertFalse(list((Path(td) / "staged").rglob("*.md")))
                with self.assertRaises(LumenError):
                    import_reviewed(b, access, bundle, workspace=workspace)
                with self.assertRaises(LumenError):
                    stage(b, access, bundle, Path(td) / "erased", workspace=workspace)
                self.assertFalse((Path(td) / "erased").exists())


if __name__ == "__main__":
    unittest.main()
