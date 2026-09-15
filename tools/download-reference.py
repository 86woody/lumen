"""Download public MIT benchmark text and pin sources. No API keys or inference."""
import hashlib
import json
from pathlib import Path
import urllib.request

root = Path(__file__).resolve().parents[1]
directory = root / "artifacts/local/datasets/longmemeval"
directory.mkdir(parents=True, exist_ok=True)


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": "Lumen-local-benchmark-reproduction"})
    with urllib.request.urlopen(request, timeout=90) as response:
        return response.read()


metadata = json.loads(fetch("https://huggingface.co/api/datasets/xiaowu0162/longmemeval-cleaned/revision/main"))
if metadata.get("cardData", {}).get("license") != "mit":
    raise SystemExit("Dataset license requires review")
dataset_revision = metadata["sha"]
source = json.loads(fetch("https://api.github.com/repos/xiaowu0162/LongMemEval/commits/main"))
source_revision = source["sha"]
files = {}
for name, url in {
    "dataset-card.md": f"https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/raw/{dataset_revision}/README.md",
    "source-license.txt": f"https://raw.githubusercontent.com/xiaowu0162/LongMemEval/{source_revision}/LICENSE",
    "retrieval-source.txt": f"https://raw.githubusercontent.com/xiaowu0162/LongMemEval/{source_revision}/src/retrieval/run_retrieval.py",
    "scorer-source.txt": f"https://raw.githubusercontent.com/xiaowu0162/LongMemEval/{source_revision}/src/retrieval/eval_utils.py",
    "longmemeval_s_cleaned.json": f"https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/{dataset_revision}/longmemeval_s_cleaned.json",
}.items():
    print("Downloading " + name, flush=True)
    payload = fetch(url)
    (directory / name).write_bytes(payload)
    files[name] = {"url": url, "sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload)}
lock = {"schema": 1, "dataset": "longmemeval-cleaned", "dataset_revision": dataset_revision,
        "source_revision": source_revision, "license": "MIT", "files": files,
        "upstream_execution": "excluded: unconditional embedding model and API imports",
        "permitted_execution": "independent local lexical adapter only; raw source stored as text, never imported"}
(root / "evals/longmemeval.lock.json").write_text(json.dumps(lock, indent=2), encoding="utf-8")
print(json.dumps({"dataset_revision": dataset_revision, "source_revision": source_revision, "files": len(files)}))
