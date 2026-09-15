import hashlib
from pathlib import Path
import tempfile
import unittest

from lumen.model import Access, LumenError, region
from lumen.service import Service
from lumen.sharing import proposal
from lumen.store import Store
from test_workspace import topology, run_git, git


class HistoricalSourceTests(unittest.TestCase):
    def test_changed_checkout_can_verify_original_commit(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = topology(Path(td) / 'workspace', 'logical')
            old_revision = git(workspace.root, 'rev-parse', 'HEAD')
            source = workspace.root / 'producer/contract.txt'
            old_bytes = workspace.historical_source('producer', 'contract.txt', old_revision)
            access = Access(workspace.id, frozenset({'repo:producer'}), 'owner', True)
            with Store(Path(td) / 'store') as store:
                eid = store.remember(access, checkout=workspace.checkout, host='fixture', session='s', turn='1',
                    scope='repo:producer', subject='codec', relation='required_encoding', value='UTF-8', text='codec UTF-8',
                    region=region(start=0), citations=[{'kind':'file', 'project':'producer', 'path':'contract.txt',
                    'revision':old_revision, 'sha256':hashlib.sha256(old_bytes).hexdigest(), 'anchor':'codec'}])['id']
                source.write_bytes(b'codec UTF-16\n')
                run_git(workspace.root, 'add', 'producer/contract.txt')
                run_git(workspace.root, 'commit', '-m', 'Changed contract')
                bundle = proposal(store, access, [eid])
                self.assertEqual(workspace.historical_source('producer', 'contract.txt', old_revision), old_bytes)
                service = Service(store, workspace)
                args = {'action':'verify', 'bundle':bundle}
                self.assertFalse(service.call(access, 'share', args)['result']['all_sources_match'])
                historical = service.call(access, 'share', {**args, 'snapshot':'cited_revision'})
                self.assertTrue(historical['result']['all_sources_match'], historical)
                self.assertEqual(historical['result']['receipts'][0]['observed_revision'], old_revision)
                for revision in ('HEAD', '--help', '0'*40):
                    with self.assertRaises(LumenError):
                        workspace.historical_source('producer', 'contract.txt', revision)
                workspace.manifest['exclusions'].append('producer/contract.txt')
                with self.assertRaises(LumenError):
                    workspace.historical_source('producer', 'contract.txt', old_revision)
