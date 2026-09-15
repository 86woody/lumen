"""Fenced AGENTS.md section and untouched CLAUDE.md, in process and through the CLI."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from lumen.model import LumenError
from lumen.workspace import Workspace


class InstructionTests(unittest.TestCase):
    def workspace(self, td):
        root = Path(td) / "workspace"
        root.mkdir()
        Workspace.initialize(root, [{"id": "p", "root": ".", "kind": "project", "purpose": "fixture",
                                     "owners": ["fixture-owner"], "depends_on": []}])
        return Workspace(root, Path(td) / "home")

    def test_fresh_write_is_idempotent_and_bounded(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = self.workspace(td)
            preview = workspace.manage_instructions("preview")
            self.assertTrue(preview["changed"])
            self.assertFalse((workspace.root / "AGENTS.md").exists())
            self.assertFalse(workspace.manage_instructions("check")["healthy"])
            first = workspace.manage_instructions("write")
            self.assertTrue(first["changed"])
            self.assertLess(first["section_lines"], 15)
            agents = (workspace.root / "AGENTS.md").read_bytes()
            self.assertEqual(agents, Workspace.SECTION.encode())
            self.assertEqual((workspace.root / "CLAUDE.md").read_bytes(), b"@AGENTS.md\n")
            self.assertIn("memory_recall", agents.decode())
            self.assertFalse(workspace.manage_instructions("write")["changed"])
            self.assertTrue(workspace.manage_instructions("check")["healthy"])
            self.assertEqual((workspace.root / "AGENTS.md").read_bytes(), agents)

    def test_existing_files_are_preserved_outside_the_fence(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = self.workspace(td)
            before = (b"# Team rules\r\nKeep this.\r\n\r\n<!-- BEGIN LUMEN MEMORY -->\r\nstale\r\n"
                      b"<!-- END LUMEN MEMORY -->\r\n\r\n## After\r\nAlso kept.\r\n")
            (workspace.root / "AGENTS.md").write_bytes(before)
            (workspace.root / "CLAUDE.md").write_bytes(b"# Existing\n")
            result = workspace.manage_instructions("write")
            after = (workspace.root / "AGENTS.md").read_bytes()
            self.assertTrue(after.startswith(b"# Team rules\r\nKeep this.\r\n\r\n<!-- BEGIN LUMEN MEMORY -->\n"))
            self.assertTrue(after.endswith(b"<!-- END LUMEN MEMORY -->\n\r\n## After\r\nAlso kept.\r\n"))
            self.assertNotIn(b"stale", after)
            self.assertEqual(after.count(b"BEGIN LUMEN MEMORY"), 1)
            self.assertEqual((workspace.root / "CLAUDE.md").read_bytes(), b"# Existing\n")
            self.assertFalse(result["claude_md"]["imports_agents"])
            self.assertFalse(result["claude_md"]["edited"])
            self.assertFalse(workspace.manage_instructions("check")["healthy"])
            (workspace.root / "AGENTS.md").write_bytes(
                b"x\n<!-- BEGIN LUMEN MEMORY -->\n<!-- BEGIN LUMEN MEMORY -->\n"
                b"<!-- END LUMEN MEMORY -->\n<!-- END LUMEN MEMORY -->\n")
            with self.assertRaises(LumenError):
                workspace.manage_instructions("write")
            (workspace.root / "AGENTS.md").write_bytes(b"x <!-- BEGIN LUMEN MEMORY -->\n<!-- END LUMEN MEMORY -->\n")
            with self.assertRaises(LumenError):
                workspace.manage_instructions("check")

    def test_cli_process_writes_and_checks(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = self.workspace(td)
            env = {key: os.environ.get(key, "") for key in ("SYSTEMROOT", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "HOME")}
            env |= {"PATH": "",
                   "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
            for action, code in (("preview", 0), ("check", 1), ("write", 0), ("check", 0)):
                run = subprocess.run([sys.executable, "-m", "lumen", "--home", str(Path(td) / "home"),
                                      "instructions", action, "--workspace", str(workspace.root)],
                                     capture_output=True, text=True, env=env, timeout=60)
                self.assertEqual(run.returncode, code, action + run.stdout + run.stderr)
                payload = json.loads(run.stdout)
                self.assertEqual(payload["path"], str(workspace.root / "AGENTS.md"))
            self.assertEqual((workspace.root / "AGENTS.md").read_bytes(), Workspace.SECTION.encode())


if __name__ == "__main__":
    unittest.main()
