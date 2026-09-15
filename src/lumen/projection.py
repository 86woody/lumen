"""Pure projection: event order and source clocks never resolve conflicts."""
from .model import (DEFAULT_RULES, cardinality, check_rules, contains_region, digest, effective_region, matches,
                    references, require, validate)

# 3: relation dimensions (ADR 0007) widen undeclared region dimensions at match time.
VERSION = 3


def _postorder(edges, cycle_message):
    active, done = set(), set()
    for root in edges:
        stack = [(root, False)]
        while stack:
            eid, finished = stack.pop()
            if finished:
                active.remove(eid)
                done.add(eid)
                yield eid
                continue
            require(eid not in active, cycle_message)
            if eid in done:
                continue
            active.add(eid)
            stack.append((eid, True))
            stack.extend((child, False) for child in reversed(edges.get(eid, [])))


def closure(events):
    events = list(events)
    for e in events:
        validate(e)
    by_id = {e["id"]: e for e in events}
    for e in events:
        require(e["digest"] == by_id[e["id"]]["digest"], "Conflicting event ID")
    pending = set()
    visiting, visited = set(), set()

    def visit(eid):
        require(eid not in visiting, "Reference cycle")
        if eid in visited:
            return eid not in pending
        visiting.add(eid)
        ready = True
        e = by_id[eid]
        assertions = []
        for reference in references(e):
            rid = reference["id"]
            if rid not in by_id:
                ready = False
                continue
            target = by_id[rid]
            require(target["workspace"] == e["workspace"] and target["scope"] == e["scope"],
                    "Inconsistent reference scope")
            ready = visit(rid) and ready
            if e["kind"] != "approval":
                require(target["kind"] == "assertion", "Control references must name assertions")
                assertions.append(target)
        if assertions:
            keys = {(a["subject"], a["relation"]) for a in assertions}
            require(len(keys) == 1, "Inconsistent subject/relation references")
        if ready and e["kind"] == "revision":
            require(any(contains_region(by_id[p["id"]]["region"], e["affected"]) for p in e["predecessors"]),
                    "Affected region must be bounded by a predecessor")
            if e["successor"]:
                require(contains_region(by_id[e["successor"]["id"]]["region"], e["affected"]),
                        "Successor must support the entire affected region")
        if e["kind"] == "reconciliation":
            require(e["selected"] in e["alternatives"], "Selection must name an alternative")
            if ready:
                require(contains_region(by_id[e['selected']['id']]['region'], e['affected']),
                        'Selected alternative must support the entire reconciliation region')
        visiting.remove(eid)
        visited.add(eid)
        if not ready:
            pending.add(eid)
        return ready

    reference_edges = {eid: [r['id'] for r in references(by_id[eid]) if r['id'] in by_id]
                       for eid in sorted(by_id)}
    # Children have already been checked, so visit never recurses down a history.
    for eid in _postorder(reference_edges, 'Reference cycle'):
        visit(eid)
    # Revision causality is assertion -> successor, distinct from reference closure.
    edges = {}
    for e in events:
        if e["kind"] == "revision" and e["successor"]:
            for p in e["predecessors"]:
                edges.setdefault(p["id"], []).append(e["successor"]["id"])
    for _ in _postorder(edges, 'Revision causality cycle'):
        pass
    return [by_id[k] for k in sorted(by_id) if k not in pending], sorted(pending)


def state(events, workspace, subject, relation, rules=None):
    rules = DEFAULT_RULES if rules is None else rules
    check_rules(rules)
    admitted, pending = closure(events)
    assertions = {e["id"]: e for e in admitted if e["kind"] == "assertion" and
                  e["workspace"] == workspace and e["subject"] == subject and e["relation"] == relation}
    relevant = [e for e in admitted if e["id"] in assertions or
                any(r["id"] in assertions for r in references(e))]
    token = digest({"version": VERSION, "rules": rules,
                    "workspace": workspace, "subject": subject, "relation": relation,
                    "events": sorted((e["id"], e["digest"]) for e in relevant)})
    return assertions, relevant, pending, token


def project(events, workspace, subject, relation, at, platform="any", branch="any", path="", rules=None):
    rules = DEFAULT_RULES if rules is None else rules
    assertions, controls, pending, token = state(events, workspace, subject, relation, rules)
    applicable = lambda r: matches(effective_region(r, rules, relation), at, platform, branch, path)
    revisions = [e for e in controls if e["kind"] == "revision"]
    successors = {e["successor"]["id"] for e in revisions if e["successor"]}
    current = {eid for eid, a in assertions.items() if eid not in successors and applicable(a["region"])}
    # Evaluate all bounded edges, then remove all predecessors. No serial winner.
    affected = [e for e in revisions if applicable(e["affected"])]
    for e in affected:
        if e["successor"] and applicable(assertions[e["successor"]["id"]]["region"]):
            current.add(e["successor"]["id"])
    for e in affected:
        current.difference_update(r["id"] for r in e["predecessors"])
    explicit_dispute = False
    for e in controls:
        if e["kind"] == "dispute" and applicable(e["affected"]):
            alternatives = {r["id"] for r in e["alternatives"]}
            resolutions = [x for x in controls if x["kind"] == "reconciliation" and applicable(x["affected"])
                           and {r["id"] for r in x["alternatives"]} == alternatives]
            explicit_dispute |= len({x["selected"]["id"] for x in resolutions}) != 1
        if e["kind"] == "reconciliation" and applicable(e["affected"]):
            # Conflicting reconciliations must not silently pick a winner.
            peers = [x for x in controls if x["kind"] == "reconciliation" and
                     applicable(x["affected"]) and {r["id"] for r in x["alternatives"]} ==
                     {r["id"] for r in e["alternatives"]}]
            if len({x["selected"]["id"] for x in peers}) == 1:
                current.difference_update(r["id"] for r in e["alternatives"] if r != e["selected"])
            else:
                explicit_dispute = True
    selected = [assertions[eid] for eid in sorted(current)]
    single = cardinality(rules, relation) == "single"
    disputed = explicit_dispute or (single and len({e["value"] for e in selected}) > 1)
    # Even undeclared/multi relation explicit revision forks cannot silently win.
    for p in assertions:
        forks = {e["successor"]["id"] for e in affected if e["successor"] and
                 any(r["id"] == p for r in e["predecessors"])} & current
        disputed |= len(forks) > 1
    evidence = {e["id"]: list(e["citations"]) for e in selected}
    for e in controls:
        if e["kind"] == "evidence" and e["target"]["id"] in evidence:
            evidence[e["target"]["id"]].extend(e["citations"])
    return {"assertions": selected, "evidence": evidence, "status": "disputed" if disputed else
            ("current" if selected else "insufficient_evidence"), "state_token": token, "pending": pending}


def intervals(events, workspace, subject, relation, platform="any", branch="any", path="", rules=None):
    _, relevant, _, _ = state(events, workspace, subject, relation, rules)
    bounds = sorted({r[k] for e in relevant for r in [e.get("region", e.get("affected", {}))]
                     for k in ("start", "end") if r.get(k) is not None})
    result = []
    for start, end in zip(bounds, bounds[1:] + [None]):
        p = project(events, workspace, subject, relation, start, platform, branch, path, rules)
        ids = [e["id"] for e in p["assertions"]]
        if result and result[-1]["ids"] == ids and result[-1]["status"] == p["status"]:
            result[-1]["end"] = end
        else:
            result.append({"start": start, "end": end, "ids": ids, "status": p["status"]})
    return result
