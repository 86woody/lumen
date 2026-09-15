"""Independent lexical calibration. Upstream model/API code is never imported."""
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import random
import re
import sqlite3
import statistics
import sys
import time

root = Path(__file__).resolve().parents[1]
config_path = root / "evals/baselines-v1.json"
config_bytes = config_path.read_bytes()
config = json.loads(config_bytes)
lock = json.loads((root / config["dataset_lock"]).read_text())
dataset = root / "artifacts/local/datasets/longmemeval/longmemeval_s_cleaned.json"
if "--self-test" not in sys.argv:
    dataset_bytes = dataset.read_bytes()
    assert hashlib.sha256(dataset_bytes).hexdigest() == lock["files"][dataset.name]["sha256"]
    all_data = json.loads(dataset_bytes)
    data = [row for row in all_data if not row["question_id"].endswith("_abs")]
    assert len(data) == 470, "Unexpected reference denominator"
    for row in data:
        assert len(row["haystack_session_ids"]) == len(row["haystack_sessions"])
        assert set(row["answer_session_ids"]) <= set(row["haystack_session_ids"])


def bm25(corpus, query):
    counts = [Counter(document.split(" ")) for document in corpus]
    lengths = [sum(c.values()) for c in counts]
    df = Counter(term for c in counts for term in c)
    size = len(corpus)
    average = sum(lengths) / max(1, size)
    idf = {term: math.log(size - n + .5) - math.log(n + .5) for term, n in df.items()}
    floor = .25 * sum(idf.values()) / max(1, len(idf))
    idf = {t: v if v >= 0 else floor for t, v in idf.items()}
    scores = []
    for terms, length in zip(counts, lengths):
        score = 0.0
        for term in query.split(" "):
            frequency = terms.get(term, 0)
            if frequency:
                score += idf[term] * frequency * 2.5 / (frequency + 1.5 * (.25 + .75 * length / average))
        scores.append(score)
    return sorted(range(size), key=lambda i: (scores[i], i), reverse=True), scores


def fts(corpus, query):
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE VIRTUAL TABLE text_index USING fts5(text,tokenize='unicode61 remove_diacritics 2')")
        db.executemany("INSERT INTO text_index(rowid,text) VALUES(?,?)", enumerate(corpus, 1))
        tokens = re.findall(r"\w+", query, re.UNICODE)
        if not tokens:
            return [], []
        match = " OR ".join('"' + t + '"' for t in tokens)
        rows = db.execute("SELECT rowid,bm25(text_index) FROM text_index WHERE text_index MATCH ? ORDER BY bm25(text_index),rowid", (match,)).fetchall()
        return [r[0] - 1 for r in rows], [-r[1] for r in rows]


def interval(values):
    rng = random.Random(1729)
    means = sorted(sum(rng.choice(values) for _ in values) / len(values) for _ in range(2000))
    return [means[49], means[1949]]


if "--self-test" in sys.argv:
    corpus = ["rare", "common", "common", "unrelated", "other"]
    ranking, scores = bm25(corpus, "rare")
    assert ranking[0] == 0
    assert abs(scores[0] - math.log(4.5 / 1.5)) < 1e-12
    assert fts(corpus, "rare")[0] == [0]
    assert fts(corpus, "missing")[0] == []
    assert bm25(corpus, "missing")[0] == [4, 3, 2, 1, 0]
    assert interval([0] * 10) == [0, 0] and interval([1] * 10) == [1, 1]
    print("Independent lexical scorer fixtures passed")
    raise SystemExit(0)

output = root / "artifacts/local/reference"
output.mkdir(parents=True, exist_ok=True)
script_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
started = time.time_ns()
summaries = []
first_hash = None
for repetition in range(config["repetitions"]):
    results = []
    start = time.perf_counter_ns()
    for ordinal, row in enumerate(data):
        ids = row["haystack_session_ids"]
        sessions = row["haystack_sessions"]
        gold = set(row["answer_session_ids"])
        all_turns = [" ".join(turn["content"] for turn in session) for session in sessions]
        user_only = [" ".join(turn["content"] for turn in session if turn["role"] == "user") for session in sessions]
        upstream_gold = {sid for sid, session in zip(ids, sessions) if "answer" in sid and
                         any(turn.get("has_answer", False) for turn in session if turn["role"] == "user")}
        for name in config["baseline_names"]:
            begin = time.perf_counter_ns()
            corpus = user_only if name == "bm25-user-only" else all_turns
            if name == "no-memory":
                ranked, scores = [], []
            elif name.startswith("bm25"):
                ranked, scores = bm25(corpus, row["question"])
            else:
                ranked, scores = fts(corpus, row["question"])
            selected = [ids[i] for i in ranked[:5]]
            found = set(selected) & gold
            results.append({"question_id": row["question_id"], "baseline": name, "selected_sessions": selected,
                            "gold_sessions": sorted(gold), "recall_any": int(bool(found)),
                            "recall_all": int(gold <= set(selected)), "evidence_fraction": len(found) / max(1, len(gold)),
                            "upstream_user_gold_eligible": bool(upstream_gold),
                            "upstream_user_recall_any": int(bool(set(selected) & upstream_gold)),
                            "retrieved_bytes": sum(len(corpus[i].encode()) for i in ranked[:5]),
                            "elapsed_us": (time.perf_counter_ns() - begin) // 1000})
        if ordinal % 100 == 0:
            print(json.dumps({"repetition": repetition + 1, "questions": ordinal + 1}), flush=True)
    canonical_results = [{k: v for k, v in r.items() if k != "elapsed_us"} for r in results]
    deterministic_hash = hashlib.sha256(json.dumps(canonical_results, sort_keys=True).encode()).hexdigest()
    if first_hash is None:
        first_hash = deterministic_hash
    assert deterministic_hash == first_hash, "Nondeterministic lexical outputs"
    (output / f"raw-{repetition + 1}.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    for name in config["baseline_names"]:
        rows = [r for r in results if r["baseline"] == name]
        summary = {"repetition": repetition + 1, "baseline": name, "n": len(rows),
                   "recall_any": statistics.mean(r["recall_any"] for r in rows),
                   "recall_all": statistics.mean(r["recall_all"] for r in rows),
                   "ci95_recall_any": interval([r["recall_any"] for r in rows]),
                   "p95_us": sorted(r["elapsed_us"] for r in rows)[int(.95 * (len(rows) - 1))],
                   "retrieved_bytes": sum(r["retrieved_bytes"] for r in rows), "additional_cost_usd": 0}
        summaries.append(summary)
report = {"schema": 1, "dataset_revision": lock["dataset_revision"], "config_sha256": hashlib.sha256(config_bytes).hexdigest(),
          "duplicate_session_id_histories": sum(len(set(r["haystack_session_ids"])) != len(r["haystack_session_ids"]) for r in data),
          "script_sha256": script_hash, "started_ns": started, "finished_ns": time.time_ns(), "summaries": summaries,
          "deterministic_output_sha256": first_hash, "reference_target_percent": 93.8,
          "faithful_replication": False, "answer_accuracy": "not measured; no permitted reader/judge run",
          "limitations": ["Independent adapter, not execution of upstream runner", "Tie order differs from NumPy default argsort", "All-turn protocol and official user-only protocol reported separately", "Reported 93.8 exact protocol remains to be identified", "Calibration results cannot be used to tune future held-out experiments"]}
assert config_path.read_bytes() == config_bytes and hashlib.sha256(Path(__file__).read_bytes()).hexdigest() == script_hash
(output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(summaries, indent=2))
