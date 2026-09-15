from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest

from lumen.model import LumenError, seal
from lumen.sharing import read_event, write_event, inspect_files
from lumen.store import Store
from test_core import assertion, OWNER


class SharedFileTests(unittest.TestCase):
    def test_missing_control_targets_cannot_be_approved_or_scanned(self):
        from lumen.sharing import proposal, approve, sync_checkout
        from lumen.model import digest, new_event, ref, region
        from support_sources import source_workspace, citation
        from test_core import capture
        with tempfile.TemporaryDirectory() as td, Store(Path(td) / 'store') as store:
            workspace = source_workspace(Path(td) / 'workspace')
            eid = capture(store, citations=[citation()])['id']
            bundle = proposal(store, OWNER, [eid])
            missing = new_event('dispute', OWNER.workspace, 'repo:p', OWNER.actor,
                alternatives=[{'workspace': OWNER.workspace, 'id': 'missing-target'}],
                affected=region(start=0), reason='Target absent')
            bundle['events'].append(missing)
            with self.assertRaises(LumenError) as error:
                approve(store, OWNER, bundle, digest(bundle), workspace)
            self.assertEqual(error.exception.code, 'index_pending')
            self.assertFalse((store.home / 'approvals').exists())
            for event in bundle['events']:
                write_event(workspace.root / '.lumen/shared/p' / (event['id'] + '.md'), event)
            with self.assertRaises(LumenError) as error:
                sync_checkout(store, OWNER, workspace)
            self.assertEqual(error.exception.code, 'index_pending')

    def test_independent_review_batches_over_100_and_erasure(self):
        from lumen.sharing import proposal, approve, stage, sync_checkout
        from lumen.model import digest
        from support_sources import source_workspace, citation
        from test_core import capture
        with tempfile.TemporaryDirectory() as td:
            workspace = source_workspace(Path(td) / 'workspace')
            directory = workspace.root / '.lumen/shared'
            with Store(Path(td) / 'producer') as source, Store(Path(td) / 'destination') as destination:
                ids = [capture(source, turn=str(i), citations=[citation()])['id'] for i in range(101)]
                for selected in (ids[:60], ids[60:]):
                    bundle = proposal(source, OWNER, selected)
                    approve(destination, OWNER, bundle, digest(bundle), workspace)
                    stage(destination, OWNER, bundle, directory, workspace)
                with self.assertRaises(LumenError):
                    inspect_files(destination, OWNER, directory)  # Human review batches stay bounded.
                result = sync_checkout(destination, OWNER, workspace)
                self.assertEqual(result['admitted'], 101)
                self.assertEqual(result['reviews'], 2)
                self.assertIsNotNone(destination.expand(OWNER, ids[-1])['event'])
                self.assertEqual(sync_checkout(destination, OWNER, workspace)['admitted'], 0)
                path = directory / 'p' / (ids[-1] + '.md')
                original = path.read_bytes()
                changed = read_event(path)
                changed['text'] = 'unreviewed changed assertion'
                changed = seal(changed)
                from lumen.sharing import event_markdown
                path.write_bytes(event_markdown(changed))
                with self.assertRaises(LumenError) as error:
                    sync_checkout(destination, OWNER, workspace)
                self.assertEqual(error.exception.code, 'index_pending')
                self.assertNotEqual(destination.expand(OWNER, ids[-1])['event']['text'], changed['text'])
                path.write_bytes(original)
                destination.purge(OWNER, ids[-1])
                self.assertFalse(path.exists())
                self.assertEqual(sync_checkout(destination, OWNER, workspace)['checked'], 100)
                path.write_bytes(original)  # Simulate a stale checkout restoring erased content.
                with self.assertRaises(LumenError):
                    sync_checkout(destination, OWNER, workspace)
                self.assertIsNone(destination.expand(OWNER, ids[-1])['event'])

    def test_checkout_recall_requires_review_and_rechecks_after_restart(self):
        from lumen.sharing import proposal, approve
        from lumen.model import Access, digest
        from lumen.service import Service
        from support_sources import source_workspace, citation
        from test_core import capture
        with tempfile.TemporaryDirectory() as td:
            workspace = source_workspace(Path(td) / 'workspace')
            home = Path(td) / 'destination'
            agent = Access(OWNER.workspace, OWNER.scopes, OWNER.actor, False)
            with Store(Path(td) / 'producer') as source:
                eid = capture(source, citations=[citation()])['id']
                bundle = proposal(source, OWNER, [eid])
            shared = workspace.root / '.lumen/shared/p' / (eid + '.md')
            write_event(shared, bundle['events'][0])
            with Store(home) as destination:
                service = Service(destination, workspace)
                result = service.call(agent, 'recall', {'query':'compiler'})
                self.assertEqual(result['error']['code'], 'index_pending')
                self.assertIsNone(destination.expand(OWNER, eid)['event'])
                self.assertEqual(service.call(agent, 'reindex', {})['error']['code'], 'not_authorized')
                self.assertEqual(service.call(OWNER, 'reindex', {})['error']['code'], 'index_pending')
                approve(destination, OWNER, bundle, digest(bundle), workspace)
                recalled = service.call(agent, 'recall', {'query':'compiler'})
                self.assertTrue(recalled['result']['results'])
                self.assertNotIn('error', service.call(OWNER, 'reindex', {}))
                watermark = destination.watermark()
                self.assertTrue(service.call(agent, 'recall', {'query':'compiler'})['result']['results'])
                self.assertEqual(destination.watermark(), watermark)
            with Store(home) as destination:
                service = Service(destination, workspace)
                self.assertTrue(service.call(agent, 'recall', {'query':'compiler'})['result']['results'])
                proof = home / 'approvals' / (digest(bundle) + '.json')
                proof.write_text('{}')
                self.assertEqual(service.call(agent, 'recall', {'query':'compiler'})['error']['code'], 'index_pending')
                self.assertEqual(destination.watermark(), watermark)

    def test_review_evidence_corruption_blocks_staging_and_import(self):
        import json
        from lumen.sharing import proposal, approve, stage, import_reviewed
        from lumen.model import digest
        from test_core import capture
        from support_sources import source_workspace, citation
        with tempfile.TemporaryDirectory() as td, Store(Path(td) / 'store') as store:
            workspace = source_workspace(Path(td) / 'sources')
            eid = capture(store, citations=[citation()])['id']
            bundle = proposal(store, OWNER, [eid])
            receipt = approve(store, OWNER, bundle, digest(bundle), workspace=workspace)
            proof = store.home / 'source-reviews' / (receipt['source_proof'] + '.json')
            original = proof.read_bytes()
            for corrupted in (b'{}', b'[]', b'invalid json', b'x' * (1024 * 1024 + 1)):
                proof.write_bytes(corrupted)
                with self.assertRaises(LumenError):
                    stage(store, OWNER, bundle, Path(td) / 'blocked', workspace=workspace)
                with self.assertRaises(LumenError):
                    import_reviewed(store, OWNER, bundle, workspace=workspace)
                self.assertFalse((Path(td) / 'blocked').exists())
            proof.unlink()
            with self.assertRaises(LumenError):
                import_reviewed(store, OWNER, bundle, workspace=workspace)
            proof.write_bytes(original)
            import_reviewed(store, OWNER, bundle, workspace=workspace)
            self.assertIsNotNone(store.expand(OWNER, eid)['event'])

    def test_source_review_reports_bytes_without_approval(self):
        import hashlib
        import json
        from lumen.workspace import Workspace
        from lumen.service import Service
        from lumen.sharing import proposal
        from test_core import capture
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'workspace'
            (root / 'p').mkdir(parents=True)
            source = root / 'p/contract.txt'
            source.write_bytes(b'compiler contract')
            manifest = Workspace.initialize(root, [{'id': 'p', 'root': 'p', 'kind': 'project',
                'purpose': 'fixture', 'owners': ['owner'], 'depends_on': []}])
            manifest['id'] = OWNER.workspace
            (root / '.lumen/workspace.json').write_text(json.dumps(manifest))
            workspace = Workspace(root, Path(td) / 'home')
            with Store(Path(td) / 'store') as store:
                eid = capture(store, citations=[{'kind': 'file', 'project': 'p', 'path': 'contract.txt',
                    'revision': 'fixture', 'sha256': hashlib.sha256(source.read_bytes()).hexdigest(), 'anchor': 'compiler'}])['id']
                bundle = proposal(store, OWNER, [eid])
                service = Service(store, workspace)
                args = {'action': 'verify', 'bundle': bundle}
                result = service.call(OWNER, 'share', args)['result']
                self.assertTrue(result['all_sources_match'])
                self.assertFalse(result['approval_granted'])
                self.assertFalse((store.home / 'approvals').exists())
                source.write_bytes(b'changed contract')
                result = service.call(OWNER, 'share', args)['result']
                self.assertEqual(result['receipts'][0]['status'], 'changed_bytes')
                from lumen.model import digest
                approval_args = {'action':'approve', 'bundle':bundle, 'reviewed_digest':digest(bundle)}
                self.assertEqual(service.call(OWNER, 'share', approval_args)['error']['code'], 'source_unavailable')
                self.assertFalse((store.home / 'approvals').exists())
                source.write_bytes(b'compiler contract')
                self.assertEqual(service.call(OWNER, 'share', approval_args)['result']['schema'], 2)
                workspace.manifest['exclusions'].append('p/contract.txt')
                (workspace.root / '.lumen/workspace.json').write_text(json.dumps(workspace.manifest))
                result = service.call(OWNER, 'share', args)['result']
                self.assertEqual(result['receipts'][0]['status'], 'source_unavailable')
                store.reindex(OWNER)
                recalled = service.call(OWNER, 'recall', {'query':'compiler'})
                self.assertFalse(recalled['result']['results'][0]['receipts'][0]['resolved'])
                from lumen.sharing import stage, import_reviewed
                with self.assertRaises(LumenError):
                    stage(store, OWNER, bundle, Path(td) / 'policy-blocked', workspace=workspace)
                with self.assertRaises(LumenError):
                    import_reviewed(store, OWNER, bundle, workspace=workspace)
                self.assertFalse((Path(td) / 'policy-blocked').exists())
                manifest_path = workspace.root / '.lumen/workspace.json'
                saved_policy = manifest_path.read_bytes()
                manifest_path.write_bytes(b'[]')
                self.assertEqual(service.call(OWNER, 'recall', {'query':'compiler'})['error']['code'], 'source_unavailable')
                manifest_path.write_bytes(saved_policy)
                store.purge(OWNER, eid)
                self.assertEqual(service.call(OWNER, 'share', args)['error']['code'], 'not_authorized')

    def test_purge_refuses_to_delete_modified_staged_file(self):
        from lumen.sharing import proposal, approve, stage
        from lumen.model import digest
        from test_core import capture
        with tempfile.TemporaryDirectory() as td, Store(Path(td) / 'store') as store:
            from support_sources import source_workspace, citation
            workspace = source_workspace(Path(td) / 'sources')
            eid = capture(store, citations=[citation()])['id']
            bundle = proposal(store, OWNER, [eid])
            approve(store, OWNER, bundle, digest(bundle), workspace=workspace)
            stage(store, OWNER, bundle, Path(td) / 'staged', workspace=workspace)
            path = Path(td) / 'staged' / 'p' / (eid + '.md')
            path.write_bytes(b'owner changed this file')
            with self.assertRaises(LumenError):
                store.purge(OWNER, eid)
            self.assertEqual(path.read_bytes(), b'owner changed this file')
            self.assertTrue(store.journal.blocked(OWNER.workspace, eid))
            self.assertIsNone(store.expand(OWNER, eid)['event'])

    def test_inspection_checks_folder_and_batch_without_admission(self):
        with tempfile.TemporaryDirectory() as td, Store(Path(td) / 'store') as store:
            root = Path(td) / 'staged'
            event = assertion()
            write_event(root / 'p' / (event['id'] + '.md'), event)
            inspected = inspect_files(store, OWNER, root)
            self.assertEqual(inspected['bundle']['events'], [event])
            self.assertFalse(store.events(OWNER))
            wrong = seal({**event, 'scope': 'repo:other'})
            path = root / 'p' / (event['id'] + '.md')
            from lumen.sharing import event_markdown
            path.write_bytes(event_markdown(wrong))
            with self.assertRaises(LumenError):
                inspect_files(store, OWNER, root)
            path.write_bytes(event_markdown(event))
            for _ in range(100):
                extra = assertion()
                write_event(root / 'p' / (extra['id'] + '.md'), extra)
            with self.assertRaises(LumenError) as raised:
                inspect_files(store, OWNER, root)
            self.assertEqual(raised.exception.code, 'budget_exhausted')
            self.assertFalse(store.events(OWNER))

    def test_concurrent_unique_and_duplicate_ids(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            events = [assertion(value=str(i)) for i in range(100)]
            def write(event):
                write_event(root / (event['id'] + '.md'), event)
            with ThreadPoolExecutor(max_workers=16) as pool:
                list(pool.map(write, events))
                list(pool.map(write, [events[0]] * 100))
            self.assertEqual(len(list(root.glob('*.md'))), 100)
            self.assertFalse(list(root.glob('*.tmp')))
            for event in events:
                self.assertEqual(read_event(root / (event['id'] + '.md')), event)
            collision = seal({**events[0], 'value': 'conflicting value'})
            with self.assertRaises(LumenError):
                write(collision)
            self.assertEqual(read_event(root / (events[0]['id'] + '.md')), events[0])

    def test_noncanonical_or_misnamed_file_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            event = assertion()
            path = Path(td) / (event['id'] + '.md')
            write_event(path, event)
            wrong = Path(td) / 'wrong.md'
            wrong.write_bytes(path.read_bytes())
            with self.assertRaises(LumenError):
                read_event(wrong)
            path.write_bytes(path.read_bytes() + b'additional instructions')
            with self.assertRaises(LumenError):
                read_event(path)
