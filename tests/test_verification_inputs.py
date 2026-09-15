import importlib.util
import os
from pathlib import Path
import tempfile
import subprocess
import unittest


class VerificationInputTests(unittest.TestCase):
    def test_changes_additions_and_removals_invalidate_evidence(self):
        module_path = Path(__file__).resolve().parents[1] / 'tools/verification_inputs.py'
        spec = importlib.util.spec_from_file_location('verification_inputs', module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for name in ('.agents/plans/Lumen.md', 'pyproject.toml', 'README.md', 'LICENSE', 'tests/test_example.py'):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('original')
            before = module.snapshot(root)
            test = root / 'tests/test_example.py'
            test.write_text('changed')
            self.assertNotEqual(module.snapshot(root), before)
            test.write_text('original')
            self.assertEqual(module.snapshot(root), before)
            extra = root / 'tests/test_extra.py'
            extra.write_text('new test')
            self.assertNotEqual(module.snapshot(root), before)
            extra.unlink()
            test.unlink()
            self.assertNotEqual(module.snapshot(root), before)
            outside = root / 'outside'
            outside.mkdir()
            linked = root / 'tests/linked'
            if os.name == 'nt':
                subprocess.run(['cmd', '/c', 'mklink', '/J', str(linked), str(outside)],
                               check=True, capture_output=True)
            else:
                linked.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, 'Linked acceptance directories'):
                module.snapshot(root)
