"""Local scale measurement for R-14: 100k records, direct core, then IPC through a real daemon.

Usage: python tools/measure-scale.py [count] [--ipc] [--clients N] [--output path] [--profile]

Phase 1 ingests `count` assertions across twenty repo scopes with one file citation each and
times 100 direct recalls. Phase 2 (`--ipc`) enrolls a workspace over the same home, starts the
daemon, runs `--clients` client processes that each recall 25 times over the pipe, times the
session-start hook endpoint and the stdlib launcher end to end, and records p50/p95 of every
distribution. No model calls; additional cost USD 0. Hardware RAM and media are not verified.
"""
import hashlib
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tempfile
import time
import cProfile
import pstats

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "src"))
from lumen.evaluation import runtime_identity
from lumen.model import Access, new_event, region
from lumen.store import Store
from lumen.workspace import Workspace

args = [a for a in sys.argv[1:] if not a.startswith("--")]
count = int(args[0]) if args else 100000
flags = sys.argv[1:]
clients = int(flags[flags.index("--clients") + 1]) if "--clients" in flags else 4
output = Path(flags[flags.index("--output") + 1]) if "--output" in flags else root / "artifacts/local/scale.json"
# The launcher runs isolated (-I), so it needs an interpreter with lumen installed, not the source tree.
launcher_python = flags[flags.index("--launcher-python") + 1] if "--launcher-python" in flags else sys.executable
SCOPES = 20


def percentiles(values):
    ordered = sorted(values)
    return {"n": len(ordered), "p50_us": int(statistics.median(ordered)),
            "p95_us": ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], "max_us": ordered[-1]}


CLIENT = r"""
import json, sys, time
from lumen.daemon import call
home, count, base, seed = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
timings = []
for i in range(base):
    # The enrolled project is p1, whose grant covers p1 and its dependency p0: records with index % 20 in {0, 1}.
    query = "E" + format(((i * 991 + seed * 7919) % (count // 20)) * 20 + i % 2, "06d")
    started = time.perf_counter_ns()
    response = call(home, "recall", {"query": query}, owner=False, timeout=30)
    elapsed = time.perf_counter_ns() - started
    result = response.get("result", {})
    if "error" in response or len(result.get("results", [])) != 1 or not result["results"][0]["receipts"][0]["fresh"]:
        raise SystemExit("IPC recall failed: " + json.dumps(response)[:400])
    timings.append(elapsed // 1000)
print(json.dumps(timings))
"""

start = time.perf_counter_ns()
identity = runtime_identity()
report = {"schema": 2, "identity": identity, "records": count, "scopes": SCOPES, "additional_cost_usd": 0,
          "hardware": {"logical_cpus": os.cpu_count(), "processor": platform.processor(), "platform": platform.platform(),
                       "ram": "unverified", "media": "unverified"},
          "limitations": ["Hardware RAM/media not verified", "Single machine; four client processes share it with the daemon",
                          "Not full R-14 acceptance: an installed host's SessionStart is not on this path"]}
with tempfile.TemporaryDirectory(prefix="lumen-scale-") as td:
    td = Path(td)
    workspace_root = td / "workspace"
    projects = []
    for i in range(SCOPES):
        (workspace_root / f"p{i}").mkdir(parents=True)
        projects.append({"id": f"p{i}", "root": f"p{i}", "kind": "library", "purpose": f"scale project {i}",
                         "owners": ["scale-owner"], "depends_on": ["p0"] if i else []})
    source = workspace_root / "p0" / "contract.txt"
    source.write_bytes(b"Fixture contract: each component uses UTF-8.\n")
    Workspace.initialize(workspace_root, projects)
    home = td / "home"
    workspace = Workspace(workspace_root, home)
    citation = {"kind": "file", "project": "p0", "path": "contract.txt", "revision": "synthetic-v1",
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "anchor": "UTF-8"}
    access = Access(workspace.id, frozenset({"monorepo", "user", "session", *("repo:p" + str(i) for i in range(SCOPES))}),
                    "scale-fixture", True)
    with Store(home) as store:
        for batch in range(0, count, 1000):
            with store.transaction():
                for i in range(batch, min(count, batch + 1000)):
                    event = new_event("assertion", workspace.id, "repo:p" + str(i % SCOPES), "scale-fixture",
                                      subject="component_" + str(i), relation="required_encoding", value="UTF-8",
                                      text=f"Component component_{i} uses UTF-8 contract error E{i:06d}",
                                      origin="tool-derived", citations=[citation], region=region(start=0))
                    store._validate_write(event, access)
                    store._insert(event)
            store.drain_index(access)
            if batch % 20000 == 0:
                print(json.dumps({"records": min(count, batch + 1000)}), flush=True)
        report["ingest_ms"] = (time.perf_counter_ns() - start) // 1000000
        timings, samples = [], []
        profiler = cProfile.Profile() if "--profile" in flags else None
        if profiler:
            profiler.enable()
        for i in range(100):
            query = "E" + format((i * 997) % count, "06d")
            started = time.perf_counter_ns()
            packet = store.recall(access, query, roots={"p0": str(workspace_root / "p0")})
            elapsed = time.perf_counter_ns() - started
            if len(packet["results"]) != 1 or not packet["results"][0]["receipts"][0]["fresh"]:
                raise RuntimeError("Scale result failed evidence check")
            timings.append(elapsed // 1000)
            samples.append({"query": query, "elapsed_us": elapsed // 1000, "result_ids": [a["id"] for a in packet["results"][0]["assertions"]]})
        if profiler:
            profiler.disable()
            pstats.Stats(profiler).sort_stats("cumulative").print_stats(20)
        report["direct_core"] = {**percentiles(timings), "samples": samples[:10]}
        print(json.dumps({"direct_core": report["direct_core"]["p95_us"]}), flush=True)
    if "--ipc" in flags:
        from lumen.daemon import call, enroll
        enroll(home, workspace.id, access.scopes, "p1")
        env = {**os.environ, "PYTHONPATH": str(root / "src")}
        daemon = subprocess.Popen([sys.executable, "-m", "lumen", "--home", str(home), "daemon", "--workspace", str(workspace_root)],
                                  env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            from lumen.model import LumenError
            ready = False
            for _ in range(600):
                if daemon.poll() is not None:
                    break
                try:
                    ready = call(home, "doctor", {})["result"]["healthy"]
                    break
                except (LumenError, FileNotFoundError):
                    time.sleep(0.1)
            if not ready:
                raise RuntimeError("Daemon did not become healthy: " + daemon.stderr.read().decode(errors="replace")[:500])
            per_client = 25
            processes = [subprocess.Popen([sys.executable, "-c", CLIENT, str(home), str(count), str(per_client), str(seed)],
                                          env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for seed in range(clients)]
            wall = time.perf_counter_ns()
            ipc = []
            for process in processes:
                out, err = process.communicate(timeout=600)
                if process.returncode:
                    raise RuntimeError("Client failed: " + err.decode(errors="replace")[:500])
                ipc.extend(json.loads(out))
            report["ipc_recall"] = {**percentiles(ipc), "clients": clients, "per_client": per_client,
                                    "wall_ms": (time.perf_counter_ns() - wall) // 1000000}
            print(json.dumps({"ipc_recall": report["ipc_recall"]["p95_us"]}), flush=True)
            hook, launcher = [], []
            payload = json.dumps({"session_id": "scale-session", "hook_event_name": "SessionStart", "cwd": str(workspace_root / "p1")}).encode()
            installed = subprocess.run([launcher_python, "-I", "-c", "import lumen.hook"], capture_output=True).returncode == 0
            for i in range(20):
                started = time.perf_counter_ns()
                run = subprocess.run([sys.executable, "-m", "lumen", "--home", str(home), "hook-session", "--workspace", str(workspace_root),
                                      "--cwd", str(workspace_root / "p1"), "--session", "scale-" + str(i), "--host", "claude-code"],
                                     env=env, capture_output=True, timeout=30)
                hook.append((time.perf_counter_ns() - started) // 1000)
                if run.returncode or b"additionalContext" not in run.stdout:
                    raise RuntimeError("Session-start endpoint failed: " + run.stderr.decode(errors="replace")[:300])
                if not installed:
                    continue
                started = time.perf_counter_ns()
                run = subprocess.run([launcher_python, "-I", "-m", "lumen.hook", "--host", "claude-code", "--python", launcher_python,
                                      "--home", str(home)], env=env, input=payload, capture_output=True, timeout=30)
                launcher.append((time.perf_counter_ns() - started) // 1000)
                if run.returncode or b"additionalContext" not in run.stdout:
                    raise RuntimeError("Launcher session start failed: " + run.stderr.decode(errors="replace")[:300])
            report["session_start_endpoint"] = percentiles(hook)
            if installed:
                report["session_start_launcher"] = {**percentiles(launcher), "python": launcher_python}
                print(json.dumps({"session_start_launcher_p95_us": report["session_start_launcher"]["p95_us"]}), flush=True)
            else:
                report["session_start_launcher"] = {"skipped": "lumen is not importable in isolated mode by " + launcher_python
                                                    + "; pass --launcher-python <interpreter with the wheel installed>"}
        finally:
            daemon.terminate()
            daemon.wait(timeout=30)
    report["runtime_unchanged"] = identity == runtime_identity()
    report["elapsed_ms"] = (time.perf_counter_ns() - start) // 1000000
    report["r14"] = {"bm25_exact_p95_under_200ms": report["direct_core"]["p95_us"] < 200000,
                     "ipc_recall_p95_under_1s": report.get("ipc_recall", {}).get("p95_us", 10**9) < 1000000,
                     "session_start_launcher_p95_under_300ms": report.get("session_start_launcher", {}).get("p95_us", 10**9) < 300000,
                     "records_at_100k": count >= 100000, "four_clients": clients >= 4 and "ipc_recall" in report,
                     "note": "Local measurement on unverified hardware; not acceptance through an installed host"}
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps({"records": count, "ingest_ms": report["ingest_ms"], "direct_p95_us": report["direct_core"]["p95_us"],
                  "ipc_p95_us": report.get("ipc_recall", {}).get("p95_us"), "r14": report["r14"]}))
