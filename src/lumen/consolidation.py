"""Background consolidator over the episode outbox: deterministic extraction, scoped supersession.

No model runs here. Extraction recognises statements in the relation vocabulary only
(`<subject> <relation>: <value>` or `<subject>.<relation> = <value>`), assigns the episode's
scope, cites the episode line, and runs the deterministic half of the supersession procedure:
same value on a current assertion adds evidence, a different value on a single relation is a
dispute (the judge is disabled: ADR 0009), a multi relation adds and retires nothing, a
recurrence is a new assertion. The consolidator never writes a revision. Every extraction is
recorded per episode so a replay repeats nothing, and summaries record the events they
depend on so a revision or purge marks them stale.
"""
import json
import re
import time

from .model import (Access, LumenError, cardinality, canonical, digest, effective_region, episode as episode_citation,
                    new_event, overlaps, ref, region, require)
from .projection import project

EXTRACTOR = "lumen-deterministic-1"
MIN_INTERVAL_NS = 60 * 1_000_000_000
MAX_EPISODES_PER_RUN = 200
MAX_CANDIDATES_PER_EPISODE = 50
STATEMENT = re.compile(r"^\s*(?:[-*]\s+|\d+[.)]\s+)?(?P<subject>[A-Za-z_][\w./:-]{0,63})(?:\s+|\.)(?P<relation>[a-z_][a-z0-9_]{0,63})\s*[:=]\s*(?P<value>\S.{0,255}?)\s*[.;]?\s*$")


class DisabledJudge:
    """The supersession judge in disabled mode: every ambiguity is a dispute, never a retirement."""
    mode = "disabled"
    model = None
    prompt = None

    def decide(self, candidate, competing):
        return {"verdict": "dispute", "mode": self.mode, "model": self.model, "prompt": self.prompt,
                "reasoning": "automatic retirement is disabled; competing values stay in dispute until an explicit revision"}


def judge_for(config):
    require(config.get("automatic_retirement") is False, "Enabled retirement needs a calibrated judge (R-17)", "unsupported_capability")
    return DisabledJudge()


def extract(event, rules):
    """Candidates from one episode: (origin, source label, line) triples matched against the vocabulary."""
    sources = [("agent-observed", "user", event.get("user")), ("agent-observed", "assistant", event.get("assistant"))]
    for index, tool in enumerate(event.get("tools") or []):
        sources.append(("tool-derived", f"tool:{index}:{tool['name']}", tool["text"]))
    candidates, seen = [], set()
    for origin, label, text in sources:
        if not text:
            continue
        for line in text.splitlines():
            match = STATEMENT.match(line)
            if not match or match.group("relation") not in rules:
                continue
            subject, relation, value = match.group("subject"), match.group("relation"), match.group("value")
            key = (subject, relation, value)
            if key in seen:
                continue
            seen.add(key)
            candidates.append({"subject": subject, "relation": relation, "value": value, "origin": origin,
                               "source": label, "line": line.strip()})
            require(len(candidates) <= MAX_CANDIDATES_PER_EPISODE, "Episode yields too many candidates", "budget_exhausted")
    return candidates


class Consolidator:
    def __init__(self, store):
        self.store = store
        self.judge = judge_for(store.config)

    def status(self, workspace):
        db = self.store.db
        seq = db.execute("SELECT value FROM metadata WHERE key='consolidated_seq'").fetchone()
        last = db.execute("SELECT value FROM metadata WHERE key='last_consolidation_ns'").fetchone()
        waiting = db.execute("SELECT COUNT(*) FROM event_keys k JOIN events e ON e.workspace=k.workspace AND e.id=k.id "
                             "WHERE k.workspace=? AND k.relation='episode' AND e.seq>?",
                             (workspace, int(seq[0]) if seq else 0)).fetchone()[0]
        return {"consolidated_seq": int(seq[0]) if seq else 0, "last_run_ns": int(last[0]) if last else None,
                "episodes_waiting": waiting, "judge": self.judge.mode, "min_interval_s": MIN_INTERVAL_NS // 1_000_000_000}

    def run(self, access, now_ns=None, force=False):
        require(access.owner, "Consolidation requires the owner channel", "not_authorized")
        now_ns = time.time_ns() if now_ns is None else now_ns
        status = self.status(access.workspace)
        report = {"judge": self.judge.mode, "retirements": 0, "episodes": 0, "candidates": 0, "assertions": 0,
                  "evidence": 0, "disputes": 0, "skipped": None, "extractor": EXTRACTOR}
        if status["episodes_waiting"] == 0:
            report["skipped"] = "no_new_episodes"
        elif not force and status["last_run_ns"] is not None and now_ns - status["last_run_ns"] < MIN_INTERVAL_NS:
            report["skipped"] = "interval"
        if report["skipped"]:
            # Stale summaries regenerate on any run; a revision or purge left them out of current answers.
            report["regenerated"] = self.store.regenerate_stale_summaries(access)
            from .procedures import distil
            report["skill_candidates"] = distil(self.store, access)
            report.update(status=self.status(access.workspace), derivatives=self.store.derivative_status(access.workspace))
            return report
        rows = self.store.db.execute("SELECT seq, body FROM events WHERE workspace=? AND seq>? ORDER BY seq LIMIT ?",
                                     (access.workspace, status["consolidated_seq"], MAX_EPISODES_PER_RUN)).fetchall()
        touched, last_seq = set(), status["consolidated_seq"]
        for row in rows:
            event = self.store._decode(row["body"])
            last_seq = row["seq"]
            if event["kind"] != "episode" or event["scope"] not in access.scopes \
                    or self.store.journal.blocked(event["workspace"], event["id"]):
                continue
            if self.store.db.execute("SELECT 1 FROM extractions WHERE episode_id=?", (event["id"],)).fetchone():
                continue  # replayed episode: its extraction is recorded, nothing repeats
            counts = self._consolidate_episode(access, event, touched)
            report["episodes"] += 1
            for key, value in counts.items():
                report[key] += value
        with self.store.transaction():
            self.store.db.execute("INSERT OR REPLACE INTO metadata VALUES('consolidated_seq',?)", (str(last_seq),))
            self.store.db.execute("INSERT OR REPLACE INTO metadata VALUES('last_consolidation_ns',?)", (str(now_ns),))
        self.store.drain_index(access)
        for scope, subject, relation in sorted(touched):
            self.store.refresh_summary(access, scope, subject, relation)
        report["regenerated"] = self.store.regenerate_stale_summaries(access)
        from .procedures import distil
        report["skill_candidates"] = distil(self.store, access)
        report.update(status=self.status(access.workspace), derivatives=self.store.derivative_status(access.workspace))
        return report

    def _consolidate_episode(self, access, event, touched):
        candidates = extract(event, self.store.rules)
        produced, counts = [], {"candidates": len(candidates), "assertions": 0, "evidence": 0, "disputes": 0}
        with self.store.transaction():
            for candidate in candidates:
                outcome = self._apply(access, event, candidate)
                counts[outcome["kind"]] += 1
                produced.append(outcome)
                touched.add((event["scope"], candidate["subject"], candidate["relation"]))
            record = {"extractor": EXTRACTOR, "judge": self.judge.decide(None, None), "candidates": candidates,
                      "produced": produced}
            self.store.db.execute("INSERT INTO extractions VALUES(?,?,?,?,?,?,?,?)",
                                  (event["id"], event["workspace"], EXTRACTOR, None, None,
                                   canonical(candidates).decode(), canonical(produced).decode(), time.time_ns()))
            self.store.fault("after_extraction")
        return counts

    def _apply(self, access, event, candidate):
        scope, subject, relation = event["scope"], candidate["subject"], candidate["relation"]
        at = event["recorded_at"] // 1_000_000_000
        citation = episode_citation(candidate["line"], source=f"{event['host']}/{event['session']}#{event['turn']}")
        events = self.store.key_events(access, scope, subject, relation)
        packet = project(events, access.workspace, subject, relation, at, rules=self.store.rules)
        same = [a for a in packet["assertions"] if a["value"] == candidate["value"]]
        if same:
            target = same[0]
            evidence = new_event("evidence", access.workspace, scope, access.actor, target=ref(target), citations=[citation])
            self.store._validate_write(evidence, access)
            self.store._insert(evidence)
            return {"kind": "evidence", "id": evidence["id"], "target": target["id"]}
        assertion = new_event("assertion", access.workspace, scope, access.actor, subject=subject, relation=relation,
                              value=candidate["value"], text=candidate["line"], origin=candidate["origin"],
                              citations=[citation], region=region(start=at))
        self.store._validate_write(assertion, access)
        self.store._insert(assertion)
        competing = [a for a in packet["assertions"] if a["value"] != candidate["value"]
                     and overlaps(effective_region(a["region"], self.store.rules, relation), assertion["region"])]
        if competing and cardinality(self.store.rules, relation) == "single":
            decision = self.judge.decide(candidate, competing)
            require(decision["verdict"] == "dispute", "Judge produced a retirement in disabled mode", "unsupported_capability")
            dispute = new_event("dispute", access.workspace, scope, access.actor,
                                alternatives=[ref(a) for a in competing] + [ref(assertion)], affected=assertion["region"],
                                reason="consolidator: " + decision["reasoning"])
            self.store._validate_write(dispute, access)
            self.store._insert(dispute)
            return {"kind": "disputes", "id": assertion["id"], "dispute": dispute["id"], "against": [a["id"] for a in competing]}
        return {"kind": "assertions", "id": assertion["id"]}
