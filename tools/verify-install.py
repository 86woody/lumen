"""Clean local wheel installation and manifest gate. Never downloads packages."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import venv
import zipfile
from verification_inputs import snapshot

root = Path(__file__).resolve().parents[1]
inputs = snapshot(root)
wheel = root / "dist/lumen_memory-0.1.0a1-py3-none-any.whl"
with zipfile.ZipFile(wheel) as archive:
    metadata = archive.read("lumen_memory-0.1.0a1.dist-info/METADATA").decode()
    if "Requires-Dist:" in metadata:
        raise SystemExit("Unexpected runtime dependency")
    members = archive.namelist()
    inventory = json.loads((root / 'evals/dependencies.lock.json').read_text())
    actual_modules = {name.removeprefix('lumen/'): hashlib.sha256(archive.read(name)).hexdigest()
                      for name in members if name.startswith('lumen/') and name.endswith('.py')}
    expected_modules = {name: entry['sha256'] for name, entry in inventory['modules'].items()}
    if not inventory.get('passed') or inventory.get('runtime_dependencies') != [] or actual_modules != expected_modules:
        raise SystemExit('Packaged runtime differs from the audited dependency inventory')
    license_member = "lumen_memory-0.1.0a1.dist-info/licenses/LICENSE"
    if license_member not in members or archive.read(license_member) != (root / "LICENSE").read_bytes():
        raise SystemExit("Missing or mismatched packaged license")
    if "Description-Content-Type: text/markdown" not in metadata:
        raise SystemExit("Package setup documentation missing")
env = dict(os.environ)
env["PIP_CONFIG_FILE"] = os.devnull
env["PYTHONPATH"] = str(root / "tools/network_guard")
env["LUMEN_NETWORK_DISABLED"] = "1"
report = {"schema": 1, "wheel": wheel.name, "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
          "members": members, "runtime_dependencies": [], "started_ns": time.time_ns(), "commands": [],
          "verification_inputs": inputs}
with tempfile.TemporaryDirectory(prefix="lumen-clean-install-") as td:
    venv.create(td, with_pip=True)
    python = Path(td) / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    commands = [
        [str(python), "-m", "pip", "install", "--isolated", "--no-index", "--no-deps", str(wheel)],
        [str(python), "-c", "import lumen, pathlib, sys; p=pathlib.Path(lumen.__file__).resolve(); "
         "assert p.is_relative_to(pathlib.Path(sys.prefix).resolve()), 'Package resolved outside clean environment'; "
         "assert 'site-packages' in p.parts, 'Package did not resolve to installed wheel'; print(p)"],
        [str(python), "-m", "lumen", "bench", "--suite", "release", "--manifest", "evals/release-1.json", "--clients", "evals/clients.lock.json", "--max-cost-usd", "0", "--output", "artifacts/local/clean-install-release-1.json"],
    ]
    for command in commands:
        run = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, timeout=300)
        report["commands"].append({"command": command, "exit_code": run.returncode,
                                   "stdout": run.stdout, "stderr": run.stderr})
        print(json.dumps({"command": command[2:5], "exit_code": run.returncode}))
        if run.returncode and "bench" not in command:
            break
report["finished_ns"] = time.time_ns()
report['verification_inputs_unchanged'] = inputs == snapshot(root)
report["release_complete"] = report["commands"][-1]["exit_code"] == 0 and len(report["commands"]) == 3 and report['verification_inputs_unchanged']
target = root / "artifacts/local/clean-install.json"
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(json.dumps(report, indent=2), encoding="utf-8")
raise SystemExit(0 if report["release_complete"] else 1)
