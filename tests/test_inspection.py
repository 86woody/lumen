import json
from pathlib import Path
import tempfile
import unittest

from lumen.model import Access, LumenError, canonical, digest, new_event, ref, region
from lumen.sharing import approve, import_reviewed, proposal
from lumen.store import Store
from test_core import OWNER, capture


class InspectionTests(unittest.TestCase):
    def test_successor_lineage_includes_predecessor_and_rebuilds(self):
        from test_core import assertion
        with tempfile.TemporaryDirectory() as td:
            with Store(td) as store:
                original = capture(store)['id']
                store.reindex(OWNER)
                token = store.recall(OWNER, 'compiler')['results'][0]['state_token']
                successor = assertion('B')
                revision = store.revise(OWNER, [original], token, region(start=0), 'Corrected command', successor=successor)['id']
                expected = {original, successor['id'], revision}
                chain = store.why(OWNER, successor['id'])
                self.assertEqual({e['id'] for e in chain['events']}, expected)
                self.assertTrue(chain['complete'])
                store.db.execute('DELETE FROM event_refs')
                self.assertIn('reference_integrity', store.doctor()['failures'])
                store.db.execute("DELETE FROM metadata WHERE key='reference_schema'")
            with Store(td) as store:
                self.assertEqual({e['id'] for e in store.why(OWNER, successor['id'])['events']}, expected)
                store.reindex(OWNER)
                self.assertTrue(store.doctor()['healthy'])
                self.assertEqual({e['id'] for e in store.why(OWNER, original)['events']}, expected)
                store.purge(OWNER, original)
                self.assertEqual(store.db.execute('SELECT COUNT(*) FROM event_refs').fetchone()[0], 0)

    def test_lineage_is_structured_scoped_and_purge_safe(self):
        with tempfile.TemporaryDirectory() as td, Store(td) as store:
            eid = capture(store)['id']
            dispute = new_event('dispute', OWNER.workspace, 'repo:p', OWNER.actor,
                alternatives=[{'workspace': OWNER.workspace, 'id': eid}], affected=region(start=0), reason='Conflicting observation')
            with store.transaction():
                store._insert(dispute)
            chain = store.why(OWNER, dispute['id'])
            self.assertEqual([e['id'] for e in chain['events']], [dispute['id'], eid])
            self.assertTrue(chain['complete'])
            denied = Access('w', frozenset({'repo:other'}), 'owner', True)
            self.assertEqual(store.why(denied, eid), store.why(denied, 'missing'))
            store.purge(OWNER, eid)
            self.assertFalse(store.why(OWNER, dispute['id'])['events'])

    def test_audit_requires_all_citations_and_never_retires(self):
        with tempfile.TemporaryDirectory() as td, Store(td) as store:
            from support_sources import source_workspace, citation
            workspace = source_workspace(Path(td) / 'sources')
            citations = [citation(name) for name in ('a.txt', 'b.txt')]
            eid = capture(store, citations=citations)['id']
            bundle = proposal(store, OWNER, [eid])
            approve(store, OWNER, bundle, digest(bundle), workspace=workspace)
            import_reviewed(store, OWNER, bundle, workspace=workspace)
            now = 10_000_000
            self.assertEqual(store.audit(OWNER, now)['findings'][0]['reason'], 'never_verified')
            for citation in citations:
                store.db.execute('INSERT OR REPLACE INTO receipts VALUES(?,?,?,?)',
                    ('w', eid, digest(citation), canonical({'fresh': True, 'checked_at': now}).decode()))
            self.assertFalse(store.audit(OWNER, now)['findings'])
            self.assertEqual(store.audit(OWNER, now + 90 * 86400)['findings'][0]['reason'], 'unverified_90_days')
            self.assertIsNotNone(store.expand(OWNER, eid)['event'])
            agent = Access('w', OWNER.scopes, 'agent', False)
            with self.assertRaises(LumenError):
                store.audit(agent, now)
