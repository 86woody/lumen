import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from lumen.cli import main
from lumen.model import LumenError
from lumen.workspace import Workspace


class CodeownersTests(unittest.TestCase):
    def test_init_routes_explicit_owners_and_check_detects_drift(self):
        with tempfile.TemporaryDirectory() as td:
            root, home = Path(td) / 'workspace', Path(td) / 'home'
            root.mkdir()
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(['--home', str(home), 'init', '--workspace', str(root),
                    '--project', 'producer', '--owner', '@team/maintainers']), 0)
            initialized = json.loads(output.getvalue())
            self.assertFalse(initialized['codeowners']['review_authority_verified'])
            self.assertEqual(initialized['instructions']['status'], 'pending')
            self.assertIn('lumen instructions write', initialized['instructions']['action'])
            self.assertTrue(any('one-time approval' in note for note in initialized['notes']))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(['--home', str(home), 'instructions', 'write', '--workspace', str(root)]), 0)
            # A second developer home initialising the same workspace sees the section already in place.
            with contextlib.redirect_stdout(io.StringIO()) as again:
                self.assertEqual(main(['--home', str(Path(td) / 'home2'), 'init', '--workspace', str(root),
                    '--project', 'producer', '--owner', '@team/maintainers']), 0)
            repeated = json.loads(again.getvalue())['instructions']
            self.assertEqual(repeated['status'], 'healthy')
            self.assertNotIn('action', repeated)
            self.assertTrue(repeated['claude_md']['imports_agents'])
            workspace = Workspace(root, home)
            path = root / '.github/CODEOWNERS'
            self.assertIn('/.lumen/shared/producer/ @team/maintainers', path.read_text())
            self.assertTrue(workspace.manage_codeowners('check')['healthy'])
            manifest_path = root / '.lumen/workspace.json'
            manifest = json.loads(manifest_path.read_text())
            manifest['projects'][0]['owners'] = ['@new-owner']
            manifest_path.write_text(json.dumps(manifest))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(['--home', str(home), 'codeowners', 'check', '--workspace', str(root)]), 1)
            workspace.manage_codeowners('write')
            self.assertNotIn('@team/maintainers', path.read_text())
            self.assertTrue(workspace.manage_codeowners('check')['healthy'])

    def test_preserves_existing_rules_and_rejects_unsafe_routing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            Workspace.initialize(root, [{'id':'p', 'root':'.', 'kind':'project', 'purpose':'fixture',
                'owners':['@owner'], 'depends_on':[]}])
            workspace = Workspace(root, root / 'home')
            path = root / 'CODEOWNERS'
            prefix = b'# Existing routing\r\n/src/ @other-owner\r\n'
            path.write_bytes(prefix)
            workspace.manage_codeowners('write')
            first = path.read_bytes()
            self.assertTrue(first.startswith(prefix))
            self.assertFalse(workspace.manage_codeowners('write')['changed'])
            self.assertEqual(path.read_bytes(), first)
            self.assertFalse((root / '.github/CODEOWNERS').exists())
            path.write_bytes(first + b'* @override\n')
            with self.assertRaises(LumenError):
                workspace.manage_codeowners('write')
            self.assertEqual(path.read_bytes(), first + b'* @override\n')
            path.write_bytes(prefix)
            manifest_path = root / '.lumen/workspace.json'
            manifest = json.loads(manifest_path.read_text())
            for owner in ('local-owner', '@owner\n* @attacker', '@owner # comment'):
                manifest['projects'][0]['owners'] = [owner]
                manifest_path.write_text(json.dumps(manifest))
                with self.assertRaises(LumenError):
                    workspace.manage_codeowners('write')
                self.assertEqual(path.read_bytes(), prefix)

