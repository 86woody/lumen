"""Host-memory import: three file formats, lineage on every citation, bulk gate, refusals, idempotence."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from lumen.imports import candidates, import_memory, items, memory_files
from lumen.model import Access, LumenError, region
from lumen.service import Service
from lumen.sharing import proposal
from lumen.store import Store
from support_sources import source_workspace
from test_core import OWNER

CLAUDE_INDEX = """# Memory index

- [Build quirks](build-quirks.md) — how the checker's fixtures must be encoded
- [Woody's preferences](preferences.md) — editor and review habits
"""
CLAUDE_TOPIC = """---
name: Build quirks
description: how the checker's fixtures must be encoded
type: project
---
# Build quirks

The checker's test suite passes only when every fixture reads and writes with encoding="utf-8".

- Run `python -m unittest` from the repo root, never from tests/.
- The token for CI is api_key=sk-lumenfixture0123456789abcdef and must never be printed.
"""
CODEX_MEMORY = """# Codex memories

1. The deploy script expects PowerShell 7.
2. Ask before touching archive/.

```
ignored fenced block: rm -rf everything
```
"""
HERMES_MEMORY = """# MEMORY.md

## Working agreements
Woody prefers short commit subjects.

Every kept repo ends in one command that proves it works.
"""


def write_claude(root):
    memory = root / ".claude" / "projects" / "C--Users-woody-Workspace" / "memory"
    memory.mkdir(parents=True)
    (memory / "MEMORY.md").write_text(CLAUDE_INDEX, encoding="utf-8")
    (memory / "build-quirks.md").write_text(CLAUDE_TOPIC, encoding="utf-8")
    (memory / "preferences.md").write_text("# Preferences\n\n- Tabs, not spaces.\n", encoding="utf-8")
    return memory


class ImportParsingTests(unittest.TestCase):
    def test_items_track_headings_bullets_paragraphs_and_skip_fences(self):
        entries, front = items(CLAUDE_TOPIC)
        self.assertEqual(front["name"], "Build quirks")
        self.assertEqual([e["kind"] for e in entries], ["paragraph", "bullet", "bullet"])
        self.assertTrue(all(e["heading"] == "Build quirks" for e in entries))
        entries, front = items(CODEX_MEMORY)
        self.assertEqual([e["text"] for e in entries], ["The deploy script expects PowerShell 7.", "Ask before touching archive/."])
        index, _ = items(CLAUDE_INDEX)
        self.assertEqual(index[0]["kind"], "index")
        self.assertEqual(index[0]["file"], "build-quirks.md")

    def test_candidates_carry_import_citations_with_lineage(self):
        with tempfile.TemporaryDirectory() as td:
            memory = write_claude(Path(td))
            found = candidates("claude-code", memory)
            self.assertEqual([c["file"] for c in found][:2], ["MEMORY.md", "MEMORY.md"])
            first = next(c for c in found if c["file"] == "build-quirks.md")
            citation = first["citations"][0]
            self.assertEqual(citation["kind"], "import")
            self.assertEqual(citation["sha256"], hashlib.sha256((memory / "build-quirks.md").read_bytes()).hexdigest())
            self.assertEqual(citation["lineage"]["format"], "claude-auto-memory")
            self.assertEqual(citation["lineage"]["name"], "Build quirks")
            self.assertEqual(citation["lineage"]["heading"], "Build quirks")
            self.assertEqual(first["relation"], "imported_note")
            index_entry = found[0]
            self.assertEqual(index_entry["citations"][0]["lineage"]["topic_file"], "build-quirks.md")
            self.assertEqual(first["identity"]["session"], citation["sha256"])

    def test_paths_are_refused_before_any_read(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for relative in (".ssh/MEMORY.md", ".aws/credentials.md", "credentials/MEMORY.md", ".env.local.md",
                             "archive/MEMORY.md", "vault/.firecrawl/staging/x.md", "notes.json"):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("- secret\n", encoding="utf-8")
                with self.assertRaises(LumenError, msg=relative):
                    memory_files("hermes", path)
            with self.assertRaises(LumenError):
                memory_files("hermes", Path("relative/MEMORY.md"))
            with self.assertRaises(LumenError):
                memory_files("hermes", root / "missing.md")
            with self.assertRaises(LumenError):
                memory_files("unknown-host", root)
            big = root / "big.md"
            big.write_bytes(b"- x\n" * 300000)
            with self.assertRaises(LumenError) as caught:
                candidates("hermes", big)
            self.assertEqual(caught.exception.code, "budget_exhausted")


class ImportStoreTests(unittest.TestCase):
    def test_import_is_owner_only_idempotent_redacted_and_never_shared(self):
        with tempfile.TemporaryDirectory() as td, Store(Path(td) / "store") as store:
            workspace = source_workspace(Path(td) / "sources")
            grant = Access("w", workspace.visible_scopes("p"), OWNER.actor, True)
            agent = Access("w", grant.scopes, OWNER.actor, False)
            memory = write_claude(Path(td))
            service = Service(store, workspace, "p")
            self.assertEqual(service.call(agent, "import", {"host": "claude-code", "path": str(memory)})["error"]["code"], "not_authorized")
            result = service.call(grant, "import", {"host": "claude-code", "path": str(memory)})
            self.assertNotIn("error", result, result)
            report = result["result"]
            self.assertEqual(report["scope"], "user")
            self.assertEqual(report["files"], ["MEMORY.md", "build-quirks.md", "preferences.md"])
            self.assertEqual(len(report["imported"]), report["candidates"])
            self.assertFalse(report["shared"])
            again = service.call(grant, "import", {"host": "claude-code", "path": str(memory)})["result"]
            self.assertEqual(again["imported"], [])
            self.assertEqual(sorted(again["already_imported"]), sorted(report["imported"]))
            events = {e["id"]: e for e in store.events(grant)}
            self.assertEqual({e["origin"] for e in events.values()}, {"agent-observed"})
            self.assertEqual({e["scope"] for e in events.values()}, {"user"})
            token_line = next(e for e in events.values() if "token for CI" in e["text"])
            self.assertIn("[REDACTED]", token_line["text"])
            self.assertNotIn("sk-lumenfixture", (Path(td) / "store" / "ledger.db").read_bytes().decode("utf-8", "ignore"))
            with self.assertRaises(LumenError):
                proposal(store, grant, [token_line["id"]])
            recalled = service.call(agent, "recall", {"query": "PowerShell"})["result"]
            self.assertEqual(recalled["results"], [])
            recalled = service.call(agent, "recall", {"query": "fixture encoding"})["result"]
            self.assertTrue(recalled["results"])
            receipt = recalled["results"][0]["receipts"][0]
            self.assertTrue(receipt["resolved"] and receipt["fresh"])
            (memory / "build-quirks.md").write_text(CLAUDE_TOPIC + "\n- A new line after the import.\n", encoding="utf-8")
            recalled = service.call(agent, "recall", {"query": "fixture encoding"})["result"]
            self.assertEqual(recalled["results"][0]["source_status"], "stale")
            changed = service.call(grant, "import", {"host": "claude-code", "path": str(memory)})["result"]
            self.assertTrue(changed["imported"])
            forged = dict(checkout="c", host="mcp", session="s", turn="t", scope="user", subject="x", relation="imported_note",
                          value="x", text="x", region=region(start=0),
                          citations=[{"kind": "import", "host": "claude-code", "path": str(memory / "MEMORY.md"),
                                      "sha256": "0" * 64, "anchor": "x", "lineage": {}}])
            self.assertEqual(service.call(agent, "remember", forged)["error"]["code"], "not_authorized")
            self.assertEqual(service.call(agent, "remember", {**forged, "citations": None, "origin": "user-stated"})["error"]["code"], "not_authorized")

    def test_bulk_needs_the_flag_and_other_hosts_and_scopes(self):
        with tempfile.TemporaryDirectory() as td, Store(Path(td) / "store") as store:
            workspace = source_workspace(Path(td) / "sources")
            grant = Access("w", workspace.visible_scopes("p"), OWNER.actor, True)
            codex = Path(td) / ".codex" / "memories"
            codex.mkdir(parents=True)
            (codex / "2026-09-13.md").write_text(CODEX_MEMORY, encoding="utf-8")
            (codex / "bulk.md").write_text("".join(f"- note {i}\n" for i in range(30)), encoding="utf-8")
            with self.assertRaises(LumenError) as caught:
                import_memory(store, grant, "codex-cli", codex)
            self.assertEqual(caught.exception.code, "budget_exhausted")
            self.assertEqual(store.events(grant), [])
            report = import_memory(store, grant, "codex-cli", codex, all=True)
            self.assertEqual(report["candidates"], 32)
            self.assertEqual({c["citations"][0]["lineage"]["format"] for c in candidates("codex-cli", codex)}, {"codex-memory"})
            hermes = Path(td) / "hermes" / "MEMORY.md"
            hermes.parent.mkdir()
            hermes.write_text(HERMES_MEMORY, encoding="utf-8")
            report = import_memory(store, grant, "hermes", hermes, scope="repo:p")
            self.assertEqual(report["candidates"], 2)
            scoped = [e for e in store.events(grant) if e["scope"] == "repo:p"]
            self.assertEqual({e["subject"] for e in scoped}, {"Working agreements"})
            self.assertEqual(scoped[0]["citations"][0]["lineage"]["format"], "hermes-memory")
            with self.assertRaises(LumenError):
                import_memory(store, grant, "hermes", hermes, scope="repo:other")
            with self.assertRaises(LumenError):
                import_memory(store, grant, "hermes", hermes, scope="session:s")
            with self.assertRaises(LumenError):
                import_memory(store, grant, "claude-code")
            cli = subprocess.run([sys.executable, "-m", "lumen", "import", "--help"], capture_output=True, text=True, timeout=30)
            self.assertEqual(cli.returncode, 0)
            self.assertIn("--all", cli.stdout)


if __name__ == "__main__":
    unittest.main()
