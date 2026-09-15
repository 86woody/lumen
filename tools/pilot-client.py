"""Explicit local pilot client. Agent authority only; evidence logs contain no payloads."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import time
import sys

from lumen.daemon import call
from lumen.evaluation import runtime_identity
from lumen.model import canonical, digest

parser = argparse.ArgumentParser()
parser.add_argument('operation', choices=['remember', 'revise', 'recall', 'expand', 'session_start'])
parser.add_argument('--home', type=Path, required=True)
parser.add_argument('--json', required=True)
parser.add_argument('--evidence', type=Path, required=True)
args = parser.parse_args()
request = json.load(sys.stdin) if args.json == '-' else json.loads(args.json)
started = time.time_ns()
clock = time.perf_counter_ns()
response = call(args.home, args.operation, request, owner=False)
elapsed = time.perf_counter_ns() - clock
result = response.get('result', {})
record = {'schema': 1, 'started_ns': started, 'finished_ns': time.time_ns(),
    'elapsed_ns': elapsed, 'operation': args.operation, 'request_digest': digest(request),
    'response_digest': digest(response), 'error': response.get('error', {}).get('code'),
    'response_bytes': len(canonical(response)), 'event_id': result.get('id'),
    'result_count': len(result.get('results', [])), 'complete': response.get('complete'),
    'runtime': runtime_identity(), 'client': 'explicit-agent-pilot-client',
    'client_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    'additional_cost_usd': 0, 'installed_host_acceptance': False}
args.evidence.parent.mkdir(parents=True, exist_ok=True)
with args.evidence.open('ab') as stream:
    stream.write(canonical(record) + b'\n')
    stream.flush()
    os.fsync(stream.fileno())
print(json.dumps(response, ensure_ascii=False))
raise SystemExit(1 if 'error' in response else 0)
