"""Migration and restore: older index schemas rebuild, older ledgers gain tables, restores reproduce answers."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from lumen.model import Access, LumenError, region
from lumen.store import INDEX_SCHEMA, Store
from test_core import OWNER, capture

AGENT = Access("w", OWNER.scopes, OWNER.actor, False)


class MigrationTests(unittest.TestCase):
    def test_older_index_schema_is_rebuilt_not_trusted(self):
        with tempfile.TemporaryDirectory() as td:
            with Store(td) as store:
                capture(store)
                store.reindex(OWNER)
                self.assertTrue(store.doctor()["healthy"])
                self.assertEqual(store.index.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()[0], INDEX_SCHEMA)
                # An index written by the previous runtime: same tables, older schema stamp.
                store.index.execute("UPDATE metadata SET value='2' WHERE key='schema'")
                self.assertEqual(store.index_watermark(), -1)
                report = store.doctor()
                self.assertFalse(report["healthy"])
                self.assertIn("index_pending", report["failures"])
                with self.assertRaises(LumenError) as caught:
                    store.recall(OWNER, "compiler")
                self.assertEqual(caught.exception.code, "index_pending")
                store.drain_index(OWNER)
                self.assertTrue(store.doctor()["healthy"])
                self.assertEqual(store.index.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()[0], INDEX_SCHEMA)
                self.assertTrue(store.recall(OWNER, "compiler")["results"])

    def test_older_ledger_without_new_tables_opens_and_serves(self):
        with tempfile.TemporaryDirectory() as td:
            with Store(td) as store:
                eid = capture(store)["id"]
                store.reindex(OWNER)
            ledger = sqlite3.connect(Path(td) / "ledger.db")
            for table in ("extractions", "derivatives"):
                ledger.execute(f"DROP TABLE {table}")
            ledger.commit()
            ledger.close()
            index = sqlite3.connect(Path(td) / "index.db")
            for table in ("aliases", "edges"):
                index.execute(f"DROP TABLE {table}")
            index.commit()
            index.close()
            with Store(td) as store:
                self.assertEqual(store.derivative_status("w"), {"kinds": {}, "extractions": 0})
                report = store.doctor()
                # The dropped index tables come back empty; the assertion index is stale until rebuilt.
                self.assertTrue(report["healthy"] or "index_pending" in report["failures"] or "index_structure_missing" in report["failures"])
                store.reindex(OWNER)
                self.assertTrue(store.doctor()["healthy"])
                self.assertEqual(store.recall(OWNER, "compiler")["results"][0]["assertions"][0]["id"], eid)

    def test_restore_reproduces_answers_tokens_tools_and_imports(self):
        with tempfile.TemporaryDirectory() as td:
            bundle_path = Path(td) / "bundle.json"
            with Store(Path(td) / "a") as a:
                capture(a)
                a.capture_stop(AGENT, checkout="co", host="claude-code", session="s", turn="t", project="p",
                               assistant="done", user="go", tools=[{"name": "bash", "output": "compiler test_command: x"}])
                a.remember(OWNER, checkout="import", host="hermes", session="f" * 64, turn="0", scope="repo:p", subject="note",
                           relation="imported_note", value="v", text="v", region=region(start=0), origin="agent-observed",
                           citations=[{"kind": "import", "host": "hermes", "path": str(Path(td) / "MEMORY.md"), "sha256": "a" * 64,
                                       "anchor": "v", "lineage": {"format": "hermes-memory"}}])
                a.reindex(OWNER)
                before = a.recall(OWNER, "compiler", at=15)["results"][0]
                a.export(OWNER, bundle_path)
                journal = a.journal.snapshot()
            bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
            self.assertEqual(len(bundle["events"]), 3)
            with Store(Path(td) / "b") as b:
                b.restore(OWNER, bundle, journal, journal["digest"])
                after = b.recall(OWNER, "compiler", at=15)["results"][0]
                self.assertEqual(after["state_token"], before["state_token"])
                self.assertEqual([x["value"] for x in after["assertions"]], [x["value"] for x in before["assertions"]])
                restored = {e["kind"]: e for e in b.events(OWNER)}
                self.assertEqual(restored["episode"]["tools"][0]["name"], "bash")
                imported = next(e for e in b.events(OWNER) if e.get("relation") == "imported_note")
                self.assertEqual(imported["citations"][0]["lineage"], {"format": "hermes-memory"})
                self.assertTrue(b.doctor()["healthy"])
                bad = {**bundle, "events": [{**bundle["events"][0], "citations": [{"kind": "vector", "embedding": [0.1]}]}]}
                with self.assertRaises(LumenError):
                    b.restore(OWNER, bad, journal, journal["digest"])
                with self.assertRaises(LumenError):
                    b.restore(AGENT, bundle, journal, journal["digest"])


if __name__ == "__main__":
    unittest.main()
