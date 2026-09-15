import json
from pathlib import Path
import tempfile
import unittest

from lumen.model import Access, digest
from lumen.service import Service
from lumen.sharing import approve, proposal, stage
from lumen.store import Store
from support_sources import source_workspace, citation
from test_core import OWNER, capture


class AuditCITests(unittest.TestCase):
    def test_ci_checks_review_routing_and_sources_without_admission(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = source_workspace(Path(td) / 'workspace')
            manifest_path = workspace.root / '.lumen/workspace.json'
            manifest = json.loads(manifest_path.read_text())
            manifest['projects'][0]['owners'] = ['@owner']
            manifest_path.write_text(json.dumps(manifest))
            workspace.manage_codeowners('write')
            with Store(Path(td) / 'producer') as producer, Store(Path(td) / 'destination') as store:
                eid = capture(producer, citations=[citation()])['id']
                bundle = proposal(producer, OWNER, [eid])
                approve(store, OWNER, bundle, digest(bundle), workspace)
                stage(store, OWNER, bundle, workspace.root / '.lumen/shared', workspace)
                service = Service(store, workspace)
                before = store.watermark()
                result = service.call(OWNER, 'audit', {'ci': True})['result']
                self.assertFalse(result['passed'])
                self.assertEqual(result['checks']['codeowners_routing']['status'], 'passed')
                self.assertEqual(result['checks']['shared_integrity_and_local_review']['status'], 'passed')
                self.assertEqual(result['checks']['permitted_source_bytes']['status'], 'passed')
                self.assertEqual(result['checks']['repository_review_authority']['status'], 'pending')
                self.assertEqual(store.watermark(), before)
                self.assertIsNone(store.expand(OWNER, eid)['event'])
                (workspace.root / 'p/contract.txt').write_bytes(b'changed source')
                result = service.call(OWNER, 'audit', {'ci': True})['result']
                self.assertEqual(result['checks']['permitted_source_bytes']['status'], 'failed')
                self.assertEqual(result['checks']['permitted_source_bytes']['failures'][0]['status'], 'changed_bytes')
                (workspace.root / '.github/CODEOWNERS').write_text('* @wrong-owner\n')
                result = service.call(OWNER, 'audit', {'ci': True})['result']
                self.assertEqual(result['checks']['codeowners_routing']['status'], 'failed')
                (store.home / 'approvals' / (digest(bundle) + '.json')).write_text('{}')
                result = service.call(OWNER, 'audit', {'ci': True})['result']
                self.assertEqual(result['checks']['shared_integrity_and_local_review']['status'], 'failed')
                self.assertEqual(store.watermark(), before)
                agent = Access(OWNER.workspace, OWNER.scopes, OWNER.actor, False)
                self.assertEqual(service.call(agent, 'audit', {'ci': True})['error']['code'], 'not_authorized')

