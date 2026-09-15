"""Skill candidates and feedback: procedures distilled from recurring episodes, never installed.

A workflow seen three times (the same normalised opening of a user prompt in one project, any
sessions) becomes a candidate `SKILL.md` under `~/.lumen/skill-candidates/<id>/`, built from the
verbatim episodes with their captured tool records and outcomes, with preconditions and expiry
triggers. It is a derivative that depends on those episodes: a purge marks it stale and the next
consolidation deletes it. Nothing here writes into a workspace's skill directories; installing a
candidate is a human copying a file and reviewing it like code (plan §4 Write path, step 6).
"""
import json
from pathlib import Path
import re
import shutil
import time

from .model import digest, require
from .security import redact

RECURRENCE = 3
KEY_TOKENS = 8
MAX_EXCERPT = 2048
MAX_TOOL_EXCERPT = 512
INSTALL_TARGETS = (".claude/skills", ".agents/skills", ".github/skills")
VERDICTS = ("helpful", "wrong", "stale", "harmful")


def workflow_key(text):
    """The normalised opening of a prompt: lowercase word tokens, the first KEY_TOKENS of them."""
    tokens = re.findall(r"[a-z0-9]+", (text or "").lower())
    return " ".join(tokens[:KEY_TOKENS]) if len(tokens) >= 3 else ""


def candidates_from(episodes):
    """Group paired episodes by project and workflow key; a group of RECURRENCE or more is a candidate."""
    groups = {}
    for e in episodes:
        if e["state"] != "paired":
            continue
        key = workflow_key(e["user"])
        if key:
            groups.setdefault((e["project"], key), []).append(e)
    return {k: sorted(v, key=lambda e: (e["recorded_at"], e["id"])) for k, v in groups.items() if len(v) >= RECURRENCE}


def render_skill(project, key, episodes):
    name = "lumen-" + re.sub(r"[^a-z0-9]+", "-", key)[:48].strip("-")
    tools = sorted({t["name"] for e in episodes for t in e.get("tools") or []})
    lines = ["---", f"name: {name}", f"description: Recurring workflow in project {project}: {key}",
             "status: candidate, not installed", f"project: {project}", f"hosts: {', '.join(sorted({e['host'] for e in episodes}))}",
             f"recurrences: {len(episodes)}", f"tools: {', '.join(tools) if tools else 'none captured'}", "---", "",
             f"# {name}", "", "Candidate procedure distilled by Lumen from verbatim episodes. Review it like code before",
             "copying it into a skills directory; Lumen never installs it.", "", "## Preconditions", "",
             f"- Working in project `{project}` of this workspace.",
             f"- The task starts like: \"{key}\".", "", "## Expiry", "",
             "- Any cited episode is purged or its facts are revised (the candidate goes stale and is deleted).",
             "- The workflow stops recurring for the project.", "", "## Episodes"]
    for e in episodes:
        lines += ["", f"### {e['host']}/{e['session']}#{e['turn']} (episode {e['id']})", "",
                  "User:", "", "```", redact(e["user"])[:MAX_EXCERPT], "```", "", "Assistant:", "", "```",
                  redact(e["assistant"])[:MAX_EXCERPT], "```"]
        for t in e.get("tools") or []:
            lines += ["", f"Tool `{t['name']}` (sha256 {t['sha256']}, {t['length']} bytes" + (", truncated" if t["truncated"] else "") + "):",
                      "", "```", t["text"][:MAX_TOOL_EXCERPT], "```"]
    return "\n".join(lines) + "\n"


def distil(store, access):
    """Write or refresh skill candidates from the ledger's episodes; return their ids."""
    require(access.owner, "Distillation requires the owner channel", "not_authorized")
    episodes = [e for e in store.events(access) if e["kind"] == "episode"]
    root = store.home / "skill-candidates"
    written = []
    for (project, key), group in sorted(candidates_from(episodes).items()):
        cid = digest([access.workspace, project, key])
        body = {"project": project, "key": key, "episodes": [e["id"] for e in group], "name": "lumen-" + re.sub(r"[^a-z0-9]+", "-", key)[:48].strip("-"),
                "path": str(root / cid / "SKILL.md"), "installed": False}
        directory = root / cid
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "SKILL.md").write_text(render_skill(project, key, group), encoding="utf-8")
        store._put_derivative(access.workspace, "skill", cid, [e["id"] for e in group], body)
        written.append(cid)
    for derivative in store.derivatives(access.workspace, "skill"):
        if derivative["stale"] or derivative["key"] not in written:
            shutil.rmtree(root / derivative["key"], ignore_errors=True)
            store.db.execute("DELETE FROM derivatives WHERE id=?", (derivative["id"],))
    return written


def list_skills(store, access):
    require(access.owner, "Skill candidates require the owner channel", "not_authorized")
    candidates = []
    for derivative in store.derivatives(access.workspace, "skill"):
        body = derivative["body"]
        candidates.append({"id": derivative["key"], "name": body["name"], "project": body["project"], "path": body["path"],
                           "episodes": len(body["episodes"]), "stale": derivative["stale"],
                           "present": Path(body["path"]).is_file(), "installed": False})
    return {"candidates": candidates, "install": "manual: copy SKILL.md into a skills directory and review it like code",
            "auto_install": False}


def record_feedback(store, access, event_id, verdict, note=""):
    require(access.owner, "Feedback requires the owner channel", "not_authorized")
    require(verdict in VERDICTS, "Verdict must be helpful, wrong, stale or harmful")
    require(isinstance(event_id, str) and 0 < len(event_id) <= 64 and isinstance(note, str) and len(note) <= 1024, "Invalid feedback")
    require(store.expand(access, event_id)["event"] is not None, "Source unavailable", "source_unavailable")
    store.db.execute("INSERT INTO feedback VALUES(?,?,?,?,?,?)",
                     (digest([access.workspace, event_id, verdict, note, time.time_ns()]), access.workspace, event_id, verdict,
                      redact(note), time.time_ns()))
    return {"event": event_id, "verdict": verdict, "recorded": True, "effect": "audit finding for wrong or harmful; never automatic retirement"}


def feedback_summary(store, workspace):
    counts = {v: 0 for v in VERDICTS}
    for verdict, count in store.db.execute("SELECT verdict, COUNT(*) FROM feedback WHERE workspace=? GROUP BY verdict", (workspace,)):
        counts[verdict] = count
    flagged = [r[0] for r in store.db.execute("SELECT DISTINCT event_id FROM feedback WHERE workspace=? AND verdict IN ('wrong','harmful') ORDER BY event_id LIMIT 100", (workspace,))]
    return {"counts": counts, "flagged": flagged}


def review_metrics(store, access, now_ns=None):
    """Review-fatigue metrics the pilot must report; median review time needs pull-request timestamps."""
    from .sharing import list_shares
    now_ns = time.time_ns() if now_ns is None else now_ns
    week = 7 * 86400 * 10**9
    shares = list_shares(store, access)
    recent = [s for s in shares if now_ns - s.get("created_ns", 0) <= week]
    return {"proposals_last_7_days": len(recent), "events_proposed_last_7_days": sum(len(b["events"]) for s in recent for b in s["batches"]),
            "acceptance_rate": None, "correction_rate": None, "rejection_rate": None, "median_review_time_s": None,
            "pending": "acceptance, correction, rejection and median review time need pull-request timestamps from the forge (ADR 0008)",
            "feedback": feedback_summary(store, access.workspace)}
