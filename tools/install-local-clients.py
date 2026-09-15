"""Install pinned free portable clients inside the project; no machine settings."""
import hashlib
import json
from pathlib import Path
import urllib.request
import zipfile

root = Path(__file__).resolve().parents[1]
metadata = json.loads((root / "artifacts/local/client-release-metadata.json").read_text())
selected = {"codex": "codex-x86_64-pc-windows-msvc.exe.zip", "copilot": "copilot-win32-x64.zip",
            "powershell": "PowerShell-7.6.6-win-x64.zip"}
inventory = {}
for name, filename in selected.items():
    asset = next(a for a in metadata[name]["assets"] if a["name"] == filename)
    destination = root / "artifacts/local/clients" / name
    destination.mkdir(parents=True, exist_ok=True)
    archive_path = destination / filename
    if not archive_path.exists():
        print("Downloading " + filename, flush=True)
        with urllib.request.urlopen(asset["url"], timeout=90) as response, archive_path.open("wb") as stream:
            while block := response.read(1024 * 1024):
                stream.write(block)
    actual = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    assert asset["digest"] == "sha256:" + actual, "Release asset digest mismatch"
    with zipfile.ZipFile(archive_path) as archive:
        for entry in archive.infolist():
            path = (destination / entry.filename).resolve()
            assert path.is_relative_to(destination.resolve())
            assert not ((entry.external_attr >> 16) & 0o170000) == 0o120000, "Symlinks excluded"
        archive.extractall(destination)
    executables = [{"path": str(p.relative_to(root)), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                   for p in destination.rglob("*.exe")]
    inventory[name] = {"version": metadata[name]["tag"], "asset": asset, "executables": executables,
                       "inference": "not executed", "embedding_capabilities": "not authorized; client tool/configuration audit pending"}
(root / "evals/client-binaries.lock.json").write_text(json.dumps(inventory, indent=2), encoding="utf-8")
print(json.dumps({name: entry["version"] for name, entry in inventory.items()}))
