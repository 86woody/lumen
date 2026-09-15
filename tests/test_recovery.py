import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import lumen

from lumen.model import LumenError
from lumen.store import Store
from test_core import OWNER, capture


class RecoveryTests(unittest.TestCase):
    def test_concurrent_revision_token_has_one_winner(self):
        code = """
import json,sys
from lumen.store import Store
from lumen.model import LumenError,region
from test_core import OWNER,assertion
with Store(sys.argv[1]) as store:
    print('ready',flush=True)
    sys.stdin.readline()
    try:
        result=store.revise(OWNER,[sys.argv[2]],sys.argv[3],region(start=0),'Concurrent update',successor=assertion(sys.argv[4]))
        print(json.dumps({'status':'committed','id':result['id']}),flush=True)
    except LumenError as exc:
        print(json.dumps({'status':exc.code}),flush=True)
"""
        with tempfile.TemporaryDirectory() as td:
            with Store(td) as store:
                eid = capture(store)['id']
                store.reindex(OWNER)
                token = store.recall(OWNER, 'compiler')['results'][0]['state_token']
            env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(Path(lumen.__file__).resolve().parent.parent), str(Path("tests").resolve()), os.environ.get("PYTHONPATH", "")])}
            processes = [subprocess.Popen([sys.executable, '-u', '-c', code, td, eid, token, str(i)],
                env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(8)]
            try:
                for process in processes:
                    self.assertEqual(process.stdout.readline().strip(), b'ready')
                for process in processes:
                    process.stdin.write(b'go\n')
                    process.stdin.flush()
                outcomes = []
                for process in processes:
                    stdout, stderr = process.communicate(timeout=30)
                    self.assertEqual(process.returncode, 0, stderr.decode())
                    outcomes.append(json.loads(stdout)['status'])
                self.assertEqual(outcomes.count('committed'), 1)
                self.assertEqual(outcomes.count('revision_conflict'), 7)
                with Store(td) as store:
                    events = store.events(OWNER)
                    self.assertEqual(len(events), 3)
                    self.assertEqual(sum(e['kind'] == 'revision' for e in events), 1)
                    store.reindex(OWNER)
                    self.assertTrue(store.doctor()['healthy'])
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.terminate()
                    process.communicate(timeout=10)

    def test_staged_process_death_and_purge(self):
        code = """
import os,sys
from lumen.store import Store
from lumen.sharing import proposal,approve,stage
from lumen.model import digest
from test_core import OWNER,capture
from support_sources import source_workspace,citation
from pathlib import Path
with Store(sys.argv[1]) as store:
    workspace=source_workspace(Path(sys.argv[1]).parent/'sources')
    eid=capture(store,citations=[citation()])['id']
    bundle=proposal(store,OWNER,[eid])
    approve(store,OWNER,bundle,digest(bundle),workspace=workspace)
    store.fault=lambda p: os._exit(77) if p==sys.argv[3] else None
    stage(store,OWNER,bundle,sys.argv[2],workspace=workspace)
"""
        env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(Path(lumen.__file__).resolve().parent.parent), str(Path("tests").resolve()), os.environ.get("PYTHONPATH", "")])}
        for point in ('after_stage_registration', 'after_stage_fsync', 'after_stage_link'):
            with self.subTest(point=point), tempfile.TemporaryDirectory() as td:
                home, staged = Path(td) / 'home', Path(td) / 'prepared'
                run = subprocess.run([sys.executable, '-c', code, str(home), str(staged), point], env=env, capture_output=True)
                self.assertEqual(run.returncode, 77, run.stderr.decode())
                with Store(home) as store:
                    self.assertFalse(list(staged.rglob('*.tmp')))
                    event = store.events(OWNER)[0]
                    store.purge(OWNER, event['id'])
                    self.assertFalse(list(staged.rglob('*.md')))
                    self.assertEqual(store.db.execute('SELECT COUNT(*) FROM staged_files').fetchone()[0], 0)

    def test_purge_export_rewrite_process_death(self):
        code = """
import os,sys
from lumen.store import Store
from test_core import OWNER
with Store(sys.argv[1]) as store:
    store.fault=lambda p: os._exit(77) if p==sys.argv[3] else None
    store.purge(OWNER,sys.argv[2])
"""
        env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(Path(lumen.__file__).resolve().parent.parent), str(Path("tests").resolve()), os.environ.get("PYTHONPATH", "")])}
        for point in ("after_export_registration", "after_export_fsync", "after_export_replace"):
            with self.subTest(point=point), tempfile.TemporaryDirectory() as td:
                home, target = Path(td) / "home", Path(td) / "backup.json"
                with Store(home) as store:
                    eid = capture(store, text='purge_export_canary')["id"]
                    store.export(OWNER, target)
                result = subprocess.run([sys.executable, "-c", code, str(home), eid, point], env=env, capture_output=True)
                self.assertEqual(result.returncode, 77, result.stderr.decode())
                with Store(home) as store:
                    self.assertFalse(list(Path(td).glob('*.tmp')))
                    self.assertNotIn('purge_export_canary', target.read_text())
                    self.assertIsNone(store.expand(OWNER, eid)["event"])

    def test_export_process_death_remains_managed(self):
        code = """
import os,sys
from lumen.store import Store
from test_core import OWNER,capture
with Store(sys.argv[1]) as store:
    capture(store,text='export_crash_erasure_canary')
    store.fault=lambda p: os._exit(77) if p==sys.argv[3] else None
    store.export(OWNER,sys.argv[2])
"""
        env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(Path(lumen.__file__).resolve().parent.parent), str(Path("tests").resolve()), os.environ.get("PYTHONPATH", "")])}
        for point in ("after_export_registration", "after_export_fsync", "after_export_replace"):
            with self.subTest(point=point), tempfile.TemporaryDirectory() as td:
                home, target = Path(td) / "home", Path(td) / "backup.json"
                result = subprocess.run([sys.executable, "-c", code, str(home), str(target), point], env=env, capture_output=True)
                self.assertEqual(result.returncode, 77, result.stderr.decode())
                with Store(home) as store:
                    self.assertFalse(list(Path(td).glob('*.tmp')))
                    self.assertEqual(store.db.execute('SELECT COUNT(*) FROM managed_exports').fetchone()[0], 1)
                    event = store.events(OWNER)[0]
                    store.purge(OWNER, event['id'])
                    if target.exists():
                        self.assertNotIn('export_crash_erasure_canary', target.read_text())
                    store.export(OWNER, target)
                    self.assertEqual(json.loads(target.read_text())['events'], [])

    def test_large_evidence_process_faults_and_erasure(self):
        code = """
import os,sys
from lumen.store import Store
from test_core import capture
with Store(sys.argv[1]) as store:
    store.fault=lambda p: os._exit(77) if p==sys.argv[2] else None
    capture(store,text='large_evidence_canary ' * 1500)
"""
        env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(Path(lumen.__file__).resolve().parent.parent), str(Path("tests").resolve()), os.environ.get("PYTHONPATH", "")])}
        for point in ["before_object_write", "after_object_fsync", "after_object_replace", "after_commit"]:
            with self.subTest(point=point), tempfile.TemporaryDirectory() as td:
                process = subprocess.run([sys.executable, "-c", code, td, point], env=env, capture_output=True)
                self.assertEqual(process.returncode, 77, process.stderr.decode())
                with Store(td) as store:
                    a = capture(store, text="large_evidence_canary " * 1500)
                    self.assertEqual(len(list((Path(td) / "objects").iterdir())), 1)
                    self.assertIn("large_evidence_canary", store.expand(OWNER, a["id"])["event"]["text"])
                    store.purge(OWNER, a["id"])
                    self.assertFalse(list((Path(td) / "objects").iterdir()))

    def test_backup_rollback_and_stale_capture_never_resurrect(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            with Store(home) as store:
                a = capture(store, text="unique_erasure_canary_82734")
                store.reindex(OWNER)
                backup = Path(td) / "backup.json"
                store.export(OWNER, backup)
                old_bundle = json.loads(backup.read_text())
                old_journal = store.journal.snapshot()
                store.purge(OWNER, a["id"])
                checkpoint = store.journal.snapshot()
                self.assertNotIn("unique_erasure_canary_82734", backup.read_text())
                self.assertFalse(store.expand(OWNER, a["id"])["event"])
                with self.assertRaises(LumenError):
                    capture(store, text="unique_erasure_canary_82734")
                self.assertEqual(store.restore(OWNER, old_bundle, checkpoint, checkpoint["digest"])["restored"], 0)
                for path in home.glob("*.db*"):
                    self.assertNotIn(b"unique_erasure_canary_82734", path.read_bytes(), path.name)
            with Store(Path(td) / "fresh") as fresh:
                with self.assertRaises(LumenError):
                    fresh.restore(OWNER, old_bundle)
                with self.assertRaises(LumenError):
                    fresh.restore(OWNER, old_bundle, old_journal, checkpoint["digest"])
                self.assertEqual(fresh.restore(OWNER, old_bundle, checkpoint, checkpoint["digest"])["restored"], 0)

    def test_actual_process_death_at_capture_transitions(self):
        code = """
import os,sys
from lumen.store import Store
from test_core import capture
with Store(sys.argv[1]) as store:
    store.fault=lambda p: os._exit(77) if p==sys.argv[2] else None
    capture(store)
"""
        env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(Path(lumen.__file__).resolve().parent.parent), str(Path("tests").resolve()), os.environ.get("PYTHONPATH", "")])}
        for point in ["before_event", "after_event", "after_outbox", "after_idempotency", "before_commit", "after_commit"]:
            with self.subTest(point=point), tempfile.TemporaryDirectory() as td:
                completed = subprocess.run([sys.executable, "-c", code, td, point], env=env, capture_output=True)
                self.assertEqual(completed.returncode, 77, completed.stderr.decode())
                with Store(td) as store:
                    self.assertEqual(len(store.events(OWNER)), int(point == "after_commit"))
                    a = capture(store)
                    self.assertEqual(capture(store)["id"], a["id"])
                    store.reindex(OWNER)
                    self.assertTrue(store.doctor()["healthy"])

    def test_actual_process_death_after_deletion_commit(self):
        with tempfile.TemporaryDirectory() as td:
            with Store(td) as store:
                eid = capture(store, text="purge_crash_canary_777")["id"]
                store.reindex(OWNER)
            code = """
import os,sys
from lumen.store import Store
from test_core import OWNER
with Store(sys.argv[1]) as store:
    store.fault=lambda p: os._exit(77) if p=='after_tombstone' else None
    store.purge(OWNER,sys.argv[2])
"""
            env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(Path(lumen.__file__).resolve().parent.parent), str(Path("tests").resolve()), os.environ.get("PYTHONPATH", "")])}
            completed = subprocess.run([sys.executable, "-c", code, td, eid], env=env, capture_output=True)
            self.assertEqual(completed.returncode, 77, completed.stderr.decode())
            with Store(td) as store:
                self.assertFalse(store.events(OWNER))
                store.reindex(OWNER)
                self.assertFalse(store.recall(OWNER, "purge_crash_canary_777")["results"])
            for path in Path(td).glob("*.db*"):
                self.assertNotIn(b"purge_crash_canary_777", path.read_bytes())

    def test_multiple_real_processes_capture_once(self):
        with tempfile.TemporaryDirectory() as td:
            with Store(td):
                pass
            code = """
import sys
from lumen.store import Store
from test_core import capture
with Store(sys.argv[1]) as store:
    print(capture(store)['id'])
"""
            env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(Path(lumen.__file__).resolve().parent.parent), str(Path("tests").resolve()), os.environ.get("PYTHONPATH", "")])}
            processes = [subprocess.Popen([sys.executable, "-c", code, td], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(8)]
            ids = set()
            for process in processes:
                stdout, stderr = process.communicate(timeout=30)
                self.assertEqual(process.returncode, 0, stderr.decode())
                ids.add(stdout.strip())
            self.assertEqual(len(ids), 1)
            with Store(td) as store:
                self.assertEqual(len(store.events(OWNER)), 1)


if __name__ == "__main__":
    unittest.main()
