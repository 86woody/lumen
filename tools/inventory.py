"""Produce an auditable dependency and direct execution-path inventory."""
import ast
import hashlib
import json
from pathlib import Path
import sys
import tomllib
import zipfile

root = Path(__file__).resolve().parents[1]
project = tomllib.loads((root / "pyproject.toml").read_text())
failures = []
if project["project"].get("dependencies"):
    failures.append("Runtime dependencies require explicit review")
modules = {}
for path in sorted((root / "src/lumen").glob("*.py")):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            imported.add((node.module or "").split(".")[0])
        elif isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else ""
            if name in {"eval", "exec", "__import__", "import_module", "load_extension"}:
                failures.append(f"Dynamic execution requires review: {path.name}:{node.lineno}")
    unknown = imported - sys.stdlib_module_names - {"lumen", "__future__"}
    if unknown:
        failures.append(f"Nonstandard imports: {path.name}: {sorted(unknown)}")
    modules[path.name] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "imports": sorted(imported)}
wheels = []
for path in sorted((root / "artifacts/local/wheels").glob("*.whl")):
    with zipfile.ZipFile(path) as wheel:
        metadata = next(n for n in wheel.namelist() if n.endswith(".dist-info/METADATA"))
        dependencies = [line for line in wheel.read(metadata).decode().splitlines() if line.startswith("Requires-Dist:")]
    wheels.append({"name": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                   "purpose": "isolated build only", "metadata_requirements": dependencies})
result = {"schema": 1, "runtime_dependencies": [], "modules": modules, "build_wheels": wheels,
          "passed": not failures, "failures": failures,
          "limits": "Static import review plus closed schemas; dynamic/no-network execution tests are separate evidence."}
target = root / "evals/dependencies.lock.json"
target.parent.mkdir(exist_ok=True)
target.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
print(json.dumps({"passed": result["passed"], "modules": len(modules), "build_wheels": len(wheels), "failures": failures}))
raise SystemExit(0 if result["passed"] else 1)
