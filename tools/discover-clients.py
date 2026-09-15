"""Inspect public release metadata only; never reads credentials or starts inference."""
import json
from pathlib import Path
import urllib.request

root = Path(__file__).resolve().parents[1]
result = {}
for name, repo in [("codex", "openai/codex"), ("copilot", "github/copilot-cli"), ("powershell", "PowerShell/PowerShell")]:
    request = urllib.request.Request("https://api.github.com/repos/" + repo + "/releases/latest", headers={"User-Agent": "Lumen-client-qualification"})
    with urllib.request.urlopen(request, timeout=30) as response:
        release = json.load(response)
    assets = [{"name": a["name"], "size": a["size"], "url": a["browser_download_url"], "digest": a.get("digest")}
              for a in release["assets"] if any(t in a["name"].lower() for t in ["windows", "win32", "win-x64"])]
    result[name] = {"tag": release["tag_name"], "assets": assets, "source": release["html_url"]}
target = root / "artifacts/local/client-release-metadata.json"
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(json.dumps(result, indent=2), encoding="utf-8")
print(json.dumps(result, indent=2))
