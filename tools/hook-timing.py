"""Measure actual no-workspace exits of the Python hook launcher and record them as evidence.

Usage: python tools/hook-timing.py [--output docs/validation/python-hook-<date>.json]
Exit 0 only when every launch exited 0 with no output and the report was written.
"""
import argparse
import datetime
import hashlib
import json
import math
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tempfile
import time

root = Path(__file__).resolve().parents[1]
source = root / "src" / "lumen" / "hook.py"
today = datetime.date.today().isoformat()
parser = argparse.ArgumentParser()
parser.add_argument("--output", type=Path, default=root / "artifacts" / "local" / "python-hook-timing.json",
                    help="Recorded evidence goes to docs/validation/python-hook-<date>.json explicitly")
parser.add_argument("--samples", type=int, default=31)
args = parser.parse_args()

importable = subprocess.run([sys.executable, "-I", "-c", "import lumen.hook"], capture_output=True).returncode == 0
launcher = [sys.executable, "-I", "-m", "lumen.hook"] if importable else [sys.executable, "-I", str(source)]


def measure(command, request, cwd):
    timings = []
    for _ in range(args.samples):
        start = time.perf_counter_ns()
        result = subprocess.run(command, input=request, capture_output=True, cwd=cwd, timeout=10)
        timings.append((time.perf_counter_ns() - start) / 1e6)
        if result.returncode or result.stdout or result.stderr:
            raise SystemExit("Launch emitted output or failed: " + repr((result.returncode, result.stdout, result.stderr)))
    return timings


def summary(timings):
    warm = timings[1:]
    return {"samples_ms": timings, "cold_ms": timings[0], "warm_p50_ms": statistics.median(warm),
            "warm_p95_ms": sorted(warm)[math.ceil(len(warm) * .95) - 1], "warm_max_ms": max(warm),
            "all_under_10ms": max(timings) < 10}


with tempfile.TemporaryDirectory(prefix="lumen-hook-no-workspace-") as td:
    request = json.dumps({"hook_event_name": "SessionStart", "session_id": "timing-fixture", "cwd": td}).encode()
    command = [*launcher, "--host", "claude-code", "--python", sys.executable, "--home", td]
    no_workspace = summary(measure(command, request, td))
    baseline = summary(measure([sys.executable, "-I", "-S", "-c", "pass"], b"", td))

report = {"schema": 1, "date": today, "platform": platform.platform(), "machine": platform.machine(),
          "python": platform.python_version(), "python_executable": sys.executable,
          "launcher_form": "module" if importable else "file", "command": command[:-2] + ["<home>"],
          "hook_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
          "no_workspace": no_workspace, "interpreter_baseline": baseline,
          "gate": {"r14_session_start_p95_ms": 300, "plan_aspiration_ms": 10},
          "host_acceptance": False, "additional_cost_usd": 0}
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_bytes(json.dumps(report, indent=2).encode() + b"\n")
print(json.dumps({"output": str(args.output.relative_to(root)), "launcher_form": report["launcher_form"],
                  "cold_ms": round(no_workspace["cold_ms"], 1), "warm_p50_ms": round(no_workspace["warm_p50_ms"], 1),
                  "warm_p95_ms": round(no_workspace["warm_p95_ms"], 1),
                  "baseline_warm_p50_ms": round(baseline["warm_p50_ms"], 1),
                  "all_under_10ms": no_workspace["all_under_10ms"]}))
