"""Closed event vocabulary, canonical digests and bounded region algebra."""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass


class LumenError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def require(condition, message, code="invalid_event"):
    if not condition:
        raise LumenError(code, message)


def canonical(value) -> bytes:
    def check(obj):
        require(not isinstance(obj, float), "Floating point is not canonical event data")
        if isinstance(obj, dict):
            require(all(isinstance(k, str) for k in obj), "Object keys must be strings")
            for child in obj.values():
                check(child)
        elif isinstance(obj, list):
            for child in obj:
                check(child)
    check(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def seal(event):
    return {**event, "digest": digest({k: v for k, v in event.items() if k != "digest"})}


DIMENSIONS = ("platform", "branch", "path")
# Built-in vocabulary when a workspace declares none: every relation over all three dimensions.
DEFAULT_RULES = {name: {"cardinality": kind, "dimensions": list(DIMENSIONS)} for name, kind in
                 (("test_command", "single"), ("required_encoding", "single"),
                  ("uses_library", "multi"), ("owned_by", "multi"))}
RELATION_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]{0,63})\s*:\s*\{\s*cardinality\s*:\s*(single|multi)"
                           r"(?:\s*,\s*dimensions\s*:\s*\[([A-Za-z, ]*)\])?\s*\}\s*$")


def check_rules(rules):
    """The relation vocabulary: cardinality single or multi, dimensions a subset of platform, branch, path."""
    require(isinstance(rules, dict) and len(rules) <= 1000, "Relation vocabulary must be an object")
    for name, rule in rules.items():
        require(isinstance(name, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", name) is not None,
                "Invalid relation name")
        require(isinstance(rule, dict) and set(rule) == {"cardinality", "dimensions"}, "Invalid relation rule")
        require(rule["cardinality"] in ("single", "multi"), "Relation cardinality must be single or multi")
        dims = rule["dimensions"]
        require(isinstance(dims, list) and len(dims) == len(set(dims)) and set(dims) <= set(DIMENSIONS),
                "Relation dimensions must be a subset of platform, branch, path")
        require(dims == [d for d in DIMENSIONS if d in dims], "Relation dimensions must be in canonical order")


def parse_relations(text):
    """Strict single-line subset of the plan's relations.yaml: `name: {cardinality: single, dimensions: [a, b]}`.

    Comments and blank lines are ignored; anything else is rejected, because the runtime has no YAML
    parser (ADR 0007). An undeclared relation is multi over every dimension.
    """
    require(isinstance(text, str) and len(text) <= 65536, "Relation vocabulary exceeds bound", "budget_exhausted")
    rules = {}
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        match = RELATION_LINE.match(line)
        require(match is not None, f"relations.yaml line {number}: unsupported syntax")
        name, cardinality, dims = match.group(1), match.group(2), match.group(3)
        require(name not in rules, f"relations.yaml line {number}: duplicate relation")
        dimensions = [d.strip() for d in dims.split(",") if d.strip()] if dims is not None else list(DIMENSIONS)
        rules[name] = {"cardinality": cardinality, "dimensions": dimensions}
    check_rules(rules)
    return rules


def render_relations(rules):
    check_rules(rules)
    lines = ["# .lumen/relations.yaml: the relation vocabulary, one relation per line.",
             "# cardinality single retires an earlier value only where the applicability overlaps;",
             "# multi never retires. dimensions lists which of platform, branch, path apply.",
             "# An undeclared relation is multi over every dimension. Reviewed like code (ADR 0007)."]
    for name in sorted(rules):
        rule = rules[name]
        lines.append(f"{name}: {{cardinality: {rule['cardinality']}, dimensions: [{', '.join(rule['dimensions'])}]}}")
    return "\n".join(lines) + "\n"


def cardinality(rules, relation):
    rule = rules.get(relation)
    return rule["cardinality"] if isinstance(rule, dict) else "multi"


def effective_region(r, rules, relation):
    """A region seen through the relation's declared dimensions: undeclared ones match everything."""
    rule = rules.get(relation)
    dims = set(rule["dimensions"]) if isinstance(rule, dict) else set(DIMENSIONS)
    if dims == set(DIMENSIONS):
        return r
    widened = dict(r)
    for dim in DIMENSIONS:
        if dim not in dims:
            widened[dim] = "**" if dim == "path" else "any"
    return widened


def estimate_tokens(text):
    """Conservative host-independent estimate: one token per three UTF-8 bytes, never fewer than the words."""
    raw = text.encode("utf-8") if isinstance(text, str) else bytes(text)
    return max(-(-len(raw) // 3), len(raw.split()))
KINDS = {"assertion", "evidence", "revision", "dispute", "approval", "reconciliation", "episode"}
EPISODE_STATES = {"paired", "prompt_missing", "interrupted"}
MAX_EPISODE_TEXT = 65536
MAX_TOOL_TEXT = 4096
MAX_TOOL_OUTPUTS = 32
COMMON = {"schema", "id", "workspace", "scope", "actor", "recorded_at", "kind", "digest"}
FIELDS = {
    "assertion": {"subject", "relation", "value", "text", "origin", "citations", "region"},
    "evidence": {"target", "citations"},
    "revision": {"predecessors", "successor", "revision_kind", "affected", "reason", "expected_state"},
    "dispute": {"alternatives", "affected", "reason"},
    "reconciliation": {"alternatives", "selected", "affected", "reason"},
    "approval": {"targets", "destination", "policy"},
    "episode": {"host", "session", "turn", "project", "user", "assistant", "state", "bytes"},
}


def new_event(kind, workspace, scope, actor, **fields):
    event = seal(dict(schema=1, id=uuid.uuid4().hex, workspace=workspace, scope=scope,
                      actor=actor, recorded_at=time.time_ns(), kind=kind, **fields))
    validate(event)
    return event


def region(platform="any", branch="any", path="**", start=None, end=None):
    result = dict(platform=platform, branch=branch, path=path, start=start, end=end)
    validate_region(result)
    return result


def validate_region(r):
    require(isinstance(r, dict) and set(r) == {"platform", "branch", "path", "start", "end"},
            "Region requires platform, branch, path, start, end")
    for key in ("platform", "branch", "path"):
        require(isinstance(r[key], str) and bool(r[key]), "Region dimensions must be nonempty strings")
    path = r["path"]
    require("\\" not in path and not path.startswith("/") and ":" not in path
            and ".." not in path.split("/"), "Unsafe applicability path")
    require(path == "**" or not any(c in path.removesuffix("/**") for c in "*?[]"),
            "Only exact paths and subtree prefixes supported")
    for key in ("start", "end"):
        require(r[key] is None or type(r[key]) is int, "Date must be integer UTC seconds or null")
    require(r["start"] is None or r["end"] is None or r["start"] < r["end"], "Empty interval")


def path_contains(pattern, path):
    return pattern == "**" or pattern == path or (pattern.endswith("/**") and
           (path == pattern[:-3] or path.startswith(pattern[:-2])))


def matches(r, at, platform, branch, path):
    return (r["start"] is not None and r["start"] <= at and
            (r["end"] is None or at < r["end"]) and
            r["platform"] in ("any", platform) and r["branch"] in ("any", branch) and
            path_contains(r["path"], path))


def overlaps(a, b):
    for dim in ("platform", "branch"):
        if a[dim] != "any" and b[dim] != "any" and a[dim] != b[dim]:
            return False
    if not (path_contains(a["path"], b["path"].removesuffix("/**")) or
            path_contains(b["path"], a["path"].removesuffix("/**"))):
        return False
    if a["start"] is None or b["start"] is None:
        return False
    return ((a["end"] is None or b["start"] < a["end"]) and
            (b["end"] is None or a["start"] < b["end"]))


def contains_region(outer, inner):
    return (all(outer[k] == "any" or outer[k] == inner[k] for k in ("platform", "branch")) and
            path_contains(outer["path"], inner["path"].removesuffix("/**")) and
            not (inner["path"].endswith("/**") and not outer["path"].endswith("**")) and
            outer["start"] is not None and inner["start"] is not None and outer["start"] <= inner["start"] and
            (outer["end"] is None or (inner["end"] is not None and inner["end"] <= outer["end"])))


def references(event):
    kind = event["kind"]
    if kind == "evidence":
        return [event["target"]]
    if kind == "revision":
        return event["predecessors"] + ([event["successor"]] if event["successor"] else [])
    if kind in ("dispute", "reconciliation"):
        return event["alternatives"]
    if kind == "approval":
        return [t["ref"] for t in event["targets"]]
    return []


def ref(event):
    return {"workspace": event["workspace"], "id": event["id"]}


def validate(event):
    require(isinstance(event, dict), "Event must be an object")
    kind = event.get("kind")
    require(kind in KINDS, "Unsupported event kind", "unsupported_capability")
    require(set(event) == COMMON | FIELDS[kind] or (kind == "episode" and set(event) == COMMON | FIELDS[kind] | {"tools"}),
            "Unknown or missing event fields")
    require(event["schema"] == 1, "Unsupported schema", "unsupported_capability")
    require(re.fullmatch(r"[a-f0-9]{32}", event["id"]) is not None, "Invalid ID")
    for key in ("workspace", "scope", "actor"):
        require(isinstance(event[key], str) and 0 < len(event[key]) <= 256, "Invalid envelope")
    require(type(event["recorded_at"]) is int, "Invalid recorded time")
    require(event["digest"] == seal(event)["digest"], "Digest mismatch")
    if kind == "assertion":
        for key in ("subject", "relation", "value", "text"):
            require(isinstance(event[key], str) and bool(event[key]), "Assertion fields must be text")
        require(event["origin"] in {"user-stated", "agent-observed", "tool-derived"}, "Invalid origin")
        validate_region(event["region"])
    if kind == "episode":
        for key in ("host", "session", "turn", "project"):
            require(isinstance(event[key], str) and 0 < len(event[key]) <= 256, "Episode identity must be text")
        require(event["state"] in EPISODE_STATES, "Invalid episode state")
        require(isinstance(event["bytes"], dict) and set(event["bytes"]) == {"user", "assistant"}, "Invalid episode bytes")
        for key in ("user", "assistant"):
            text, meta = event[key], event["bytes"][key]
            require(text is None or isinstance(text, str), "Episode text must be text or null")
            require(isinstance(meta, dict) and set(meta) == {"length", "sha256", "truncated"}
                    and type(meta["length"]) is int and meta["length"] >= 0 and type(meta["truncated"]) is bool,
                    "Invalid episode text record")
            if text is None:
                require(meta == {"length": 0, "sha256": None, "truncated": False}, "Null episode text must be empty")
            else:
                require(len(text.encode()) <= MAX_EPISODE_TEXT, "Episode text exceeds bound", "budget_exhausted")
                require(isinstance(meta["sha256"], str) and re.fullmatch(r"[a-f0-9]{64}", meta["sha256"]) is not None,
                        "Invalid episode text digest")
                require(meta["truncated"] or (meta["sha256"] == hashlib.sha256(text.encode()).hexdigest()
                        and meta["length"] == len(text.encode())), "Episode text digest mismatch")
        require(event["state"] != "paired" or (event["user"] is not None and event["assistant"] is not None),
                "Paired episode needs both texts")
        require(event["state"] != "prompt_missing" or event["user"] is None, "Missing prompt must be null")
        require(event["state"] != "interrupted" or event["assistant"] is None, "Interrupted turn has no response")
        if "tools" in event:
            # Tool outputs are truncated and hashed at capture; the hash names the untruncated output.
            tools = event["tools"]
            require(isinstance(tools, list) and 0 < len(tools) <= MAX_TOOL_OUTPUTS, "Invalid tool output list")
            for tool in tools:
                require(isinstance(tool, dict) and set(tool) == {"name", "text", "sha256", "length", "truncated"},
                        "Invalid tool output record")
                require(isinstance(tool["name"], str) and 0 < len(tool["name"]) <= 128, "Invalid tool name")
                require(isinstance(tool["text"], str) and len(tool["text"].encode()) <= MAX_TOOL_TEXT, "Tool output exceeds bound")
                require(type(tool["length"]) is int and tool["length"] >= 0 and type(tool["truncated"]) is bool
                        and isinstance(tool["sha256"], str) and re.fullmatch(r"[a-f0-9]{64}", tool["sha256"]) is not None,
                        "Invalid tool output record")
                require(tool["truncated"] or (tool["sha256"] == hashlib.sha256(tool["text"].encode()).hexdigest()
                        and tool["length"] == len(tool["text"].encode())), "Tool output digest mismatch")
    if "affected" in event:
        validate_region(event["affected"])
    if "reason" in event:
        require(isinstance(event["reason"], str) and bool(event["reason"].strip()), "Reason required")
    if kind == "revision":
        require(event["revision_kind"] in {"change", "correction"}, "Invalid revision kind")
        require(isinstance(event["expected_state"], str), "State token required")
        require(bool(event["predecessors"]), "Predecessor required")
    for r in references(event):
        require(isinstance(r, dict) and set(r) == {"workspace", "id"}, "Namespaced reference required")
        require(r["workspace"] == event["workspace"], "Cross-workspace reference rejected")
        require(isinstance(r["id"], str), "Invalid reference")
    if kind in {"assertion", "evidence"}:
        require(isinstance(event["citations"], list) and bool(event["citations"]), "Evidence required")
        for c in event["citations"]:
            require(isinstance(c, dict), "Invalid citation")
            if c.get("kind") == "episode":
                require(set(c) == {"kind", "source", "text", "sha256"}, "Invalid episode citation")
                require(c["sha256"] == hashlib.sha256(c["text"].encode()).hexdigest(), "Evidence digest mismatch")
            elif c.get("kind") == "file":
                require(set(c) == {"kind", "project", "path", "revision", "sha256", "anchor"}, "Invalid file citation")
                require(all(isinstance(v, str) for v in c.values()), "Invalid file citation values")
                require(re.fullmatch(r"[a-f0-9]{64}", c["sha256"]) is not None, "Invalid source hash")
            elif c.get("kind") == "import":
                # A host memory file read by `lumen import`: the file, its hash, an anchor and the
                # lineage the file itself records. Never shared; resolves only on this machine.
                require(set(c) == {"kind", "host", "path", "sha256", "anchor", "lineage"}, "Invalid import citation")
                require(all(isinstance(c[k], str) and 0 < len(c[k]) <= 4096 for k in ("host", "path", "anchor")),
                        "Invalid import citation values")
                require(re.fullmatch(r"[a-f0-9]{64}", c["sha256"]) is not None, "Invalid source hash")
                lineage = c["lineage"]
                require(isinstance(lineage, dict) and len(lineage) <= 16 and all(
                    isinstance(k, str) and 0 < len(k) <= 64 and isinstance(v, str) and len(v) <= 256
                    for k, v in lineage.items()), "Invalid import lineage")
            else:
                raise LumenError("unsupported_capability", "Unsupported citation kind")


def episode(text, source="explicit"):
    return {"kind": "episode", "source": source, "text": text,
            "sha256": hashlib.sha256(text.encode()).hexdigest()}


@dataclass(frozen=True)
class Access:
    workspace: str
    scopes: frozenset[str]
    actor: str
    owner: bool = False

    def permits(self, event):
        return event["workspace"] == self.workspace and event["scope"] in self.scopes
