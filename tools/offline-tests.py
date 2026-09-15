"""Run Lumen tests with Python network connect/bind denied in every subprocess."""
import os
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
env = dict(os.environ)
env["PYTHONPATH"] = os.pathsep.join([str(root / "tools/network_guard"), str(root / "src"), str(root / "tests")])
env["LUMEN_NETWORK_DISABLED"] = "1"
raise SystemExit(subprocess.call([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"], cwd=root, env=env))
