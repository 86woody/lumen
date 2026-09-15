"""Bounded local request dispatcher. Authority is supplied by transport, not arguments."""
import re
import time
import uuid

from . import __version__
from .model import Access, LumenError, canonical, digest, new_event, require
from .security import CONFIG

SESSION_SCOPE = re.compile(r"session:[A-Za-z0-9._:-]{1,128}")
POLICY_OPERATIONS = {'remember', 'revise', 'recall', 'expand', 'why', 'share', 'session_start', 'reindex', 'audit',
                     'capture_prompt', 'capture_stop', 'flush_session', 'consolidate', 'pending',
                     'import', 'feedback', 'skills'}


class Service:
    def __init__(self, store, workspace=None, project=None):
        self.store = store
        self.workspace = workspace
        self.project = project
        self.sessions = {}

    def apply_policy(self):
        """Committed team files govern the store's relation vocabulary and caps on every call."""
        self.workspace.refresh_policy()
        self.store.rules = dict(self.workspace.relations)
        self.store.config = dict(self.workspace.policy["config"])

    def policy_report(self):
        policy = self.workspace.policy if self.workspace else None
        if policy is None:
            return {"source": "defaults", "digest": None, "version_pinned": None, "version_match": True,
                    "relations_digest": digest(self.store.rules)}
        return {"source": policy["source"], "digest": policy["digest"], "version_pinned": policy["version"],
                "version_match": policy["version"] in (None, __version__), "relations_digest": digest(self.workspace.relations),
                "share": policy["share"]}

    def call(self, access, operation, arguments):
        response = {"request_id": uuid.uuid4().hex, "schema": 1, "snapshot": self.store.watermark(),
                    "policy_revision": 1, "deletion_epoch": self.store.journal.snapshot()["digest"],
                    "state_token": None, "complete": True, "budget_used": 0}
        try:
            require(isinstance(arguments, dict), "Arguments must be an object")
            require(len(canonical(arguments)) <= CONFIG["max_event_bytes"], "Request too large", "budget_exhausted")
            if self.workspace and operation in POLICY_OPERATIONS:
                self.apply_policy()
                current_projects = {'repo:' + p['id'] for p in self.workspace.manifest['projects']}
                if self.project is not None:
                    require('repo:' + self.project in current_projects, 'Enrolled project removed', 'not_authorized')
                    current_projects &= self.workspace.visible_scopes(self.project)
                scopes = frozenset(s for s in access.scopes if not s.startswith('repo:') or s in current_projects)
                access = Access(access.workspace, scopes, access.actor, access.owner)
            # Working state lives in session:<id>; the grant names the family, the call names one session.
            session = arguments.get("session")
            if "session" in access.scopes and isinstance(session, str) and SESSION_SCOPE.fullmatch("session:" + session):
                access = Access(access.workspace, access.scopes | {"session:" + session}, access.actor, access.owner)
            if operation in {"recall", "expand", "why", "purge", "export"} and "session" in arguments:
                arguments = {k: v for k, v in arguments.items() if k != "session"}
            if operation == "remember":
                require("origin" not in arguments, "Origin is authenticated by transport", "not_authorized")
                result = self.store.remember(access, **arguments)
                self.store.drain_index(Access(access.workspace, access.scopes, access.actor, True))
                result["indexed"] = True
            elif operation == "revise":
                args = dict(arguments)
                if args.get("successor"):
                    fields = args["successor"]
                    require("origin" not in fields and "actor" not in fields, "Origin is authenticated by transport", "not_authorized")
                    scope = fields.pop("scope")
                    args["successor"] = new_event("assertion", access.workspace, scope, access.actor,
                                                 origin="user-stated" if access.owner else "agent-observed", **fields)
                result = self.store.revise(access, **args)
                self.store.reindex(Access(access.workspace, access.scopes, access.actor, True))
            elif operation == "recall":
                require(not {"roots", "source_resolver", "nearness"} & arguments.keys(), "Source policy is server configured", "not_authorized")
                if "as_of" in arguments:
                    require("at" not in arguments, "Give at or as_of, not both")
                    arguments = {**arguments, "at": arguments["as_of"]}
                    del arguments["as_of"]
                if self.workspace:
                    from .sharing import sync_checkout
                    sync_checkout(self.store, Access(access.workspace, access.scopes, access.actor, True), self.workspace)
                catalog = self.workspace.snapshot() if self.workspace else None
                projects = [p for p in catalog["projects"] if "repo:" + p["id"] in access.scopes] if catalog else []
                roots = {p["id"]: p["root_resolved"] for p in projects if p["availability"] == "current"}
                nearness = self.workspace.nearness(self.project) if self.workspace and self.project else None
                result = self.store.recall(access, **arguments, roots=roots, nearness=nearness,
                    source_resolver=self.workspace.source if self.workspace else None)
                if self.workspace:
                    result["repos"] = [{"id": p["id"], "availability": p["availability"]} for p in catalog["projects"]
                                       if "repo:" + p["id"] in access.scopes]
                    result["repos_searched"] = sorted(roots)
                    result["repos_skipped"] = [p["id"] for p in projects if p["availability"] != "current"]
            elif operation == "expand":
                result = self.store.expand(access, **arguments)
            elif operation == "why":
                result = self.store.why(access, **arguments)
            elif operation == "audit":
                args = dict(arguments)
                ci = args.pop('ci', False)
                require(type(ci) is bool, 'Invalid CI mode')
                result = self.store.audit(access, **args)
                if ci:
                    from .sharing import audit_checkout
                    result.update(audit_checkout(self.store, access, self.workspace))
            elif operation == "session_start":
                require(set(arguments) in ({"session", "project"}, {"session", "project", "workspace"}), "Invalid session arguments")
                require(arguments.get('workspace', access.workspace) == access.workspace,
                        'Workspace does not match enrolled daemon', 'not_authorized')
                require(isinstance(arguments['session'], str) and 0 < len(arguments['session']) <= 128,
                        'Invalid session identity')
                require(isinstance(arguments['project'], str) and 'repo:' + arguments['project'] in access.scopes,
                        'Project is outside the current grant', 'not_authorized')
                key = (access.workspace, arguments["session"], arguments["project"], tuple(sorted(access.scopes)))
                if key not in self.sessions:
                    from .workspace import HINT, render_slice
                    require(len(self.sessions) < 1000, "Session capacity reached", "budget_exhausted")
                    entry = {"hint": HINT, "resident": "disabled", "catalog": None, "context": HINT}
                    if self.workspace:
                        catalog = self.workspace.snapshot()
                        visible = {**catalog, "projects": [p for p in catalog["projects"] if "repo:" + p["id"] in access.scopes]}
                        entry["catalog"] = self.workspace.catalog_slice(arguments["project"], visible)
                        entry["context"] = HINT + "\n" + render_slice(entry["catalog"])
                        entry["policy_digest"] = self.workspace.policy["digest"]
                    # The block is frozen for the session: the same bytes on every delivery.
                    self.sessions[key] = entry
                result = self.sessions[key]
            elif operation in ("capture_prompt", "capture_stop", "flush_session"):
                expected = {"checkout", "host", "session", "turn", "project", "workspace"}
                expected |= {"text"} if operation == "capture_prompt" else {"assistant"} if operation == "capture_stop" else set()
                if operation == "flush_session":
                    expected -= {"turn", "project"}
                optional = {"user", "tools"} if operation == "capture_stop" else set()
                require(expected <= set(arguments) <= expected | optional, "Invalid capture arguments")
                require(arguments["workspace"] == access.workspace, "Workspace does not match enrolled daemon", "not_authorized")
                require(all(isinstance(arguments[k], str) and 0 < len(arguments[k]) <= 256
                            for k in expected - {"text", "assistant"}), "Invalid capture identity")
                if "project" in expected:
                    require("repo:" + arguments["project"] in access.scopes, "Project is outside the current grant", "not_authorized")
                fields = {k: v for k, v in arguments.items() if k != "workspace"}
                result = getattr(self.store, operation)(access, **fields)
            elif operation == "doctor":
                policy_failures = []
                if self.workspace:
                    try:
                        self.apply_policy()
                    except LumenError as exc:
                        policy_failures.append("policy_unreadable:" + exc.code)
                from .consolidation import Consolidator
                result = {**self.store.doctor(), "capture": self.store.capture_status(access.workspace),
                          "policy": self.policy_report(), "consolidation": Consolidator(self.store).status(access.workspace),
                          "derivatives": self.store.derivative_status(access.workspace)}
                if not result["policy"]["version_match"]:
                    policy_failures.append("policy_version_mismatch")
                if policy_failures:
                    result["failures"].extend(policy_failures)
                    result["healthy"] = False
            elif operation == "reindex":
                require(access.owner, 'Reindex requires owner', 'not_authorized')
                if self.workspace:
                    from .sharing import sync_checkout
                    sync_checkout(self.store, access, self.workspace)
                result = self.store.reindex(access)
            elif operation == "export":
                result = self.store.export(access, **arguments)
            elif operation == "restore":
                result = self.store.restore(access, **arguments)
            elif operation == "purge":
                result = self.store.purge(access, **arguments)
            elif operation == "consolidate":
                require(access.owner, "Consolidation requires the owner channel", "not_authorized")
                require(set(arguments) <= {"force"} and type(arguments.get("force", False)) is bool, "Invalid consolidate arguments")
                from .consolidation import Consolidator
                result = Consolidator(self.store).run(access, force=arguments.get("force", False))
            elif operation == "import":
                require(access.owner, "Import requires the owner channel", "not_authorized")
                require(set(arguments) <= {"host", "path", "scope", "all"} and "host" in arguments, "Invalid import arguments")
                from .imports import import_memory
                result = import_memory(self.store, access, **arguments)
                self.store.drain_index(access)
            elif operation == "feedback":
                require(access.owner, "Feedback requires the owner channel", "not_authorized")
                require(set(arguments) <= {"id", "verdict", "note"} and {"id", "verdict"} <= set(arguments), "Invalid feedback arguments")
                from .procedures import record_feedback
                result = record_feedback(self.store, access, arguments["id"], arguments["verdict"], arguments.get("note", ""))
            elif operation == "skills":
                require(access.owner, "Skill candidates require the owner channel", "not_authorized")
                require(not arguments, "Skills takes no arguments")
                from .procedures import list_skills
                result = list_skills(self.store, access)
            elif operation == "pending":
                require(access.owner, "Pending review requires owner channel", "not_authorized")
                require(not arguments, "Pending takes no arguments")
                from .sharing import list_shares
                from .procedures import list_skills, review_metrics
                shares = list_shares(self.store, access)
                result = {"shares": shares, "review_metrics": review_metrics(self.store, access),
                          "skill_candidates": list_skills(self.store, access)["candidates"],
                          "resident_proposals": "disabled",
                          "awaiting_pull_request": [b["branch"] for s in shares for b in s["batches"]
                                                    if s["pull_request"]["status"] == "pending"],
                          "pull_request_mode": self.workspace.policy["share"]["pull_request"] if self.workspace else "unconfigured",
                          "derivatives": self.store.derivative_status(access.workspace),
                          "stale_derivatives": [d["key"] for d in self.store.derivatives(access.workspace) if d["stale"]][:100]}
            elif operation == "journal_checkpoint":
                require(access.owner, "Checkpoint requires owner", "not_authorized")
                result = self.store.journal.snapshot()
            elif operation == "share":
                require(access.owner, "Sharing requires owner channel", "not_authorized")
                from .sharing import proposal, approve, import_reviewed, stage, inspect_files, verify_sources, branch_share
                args = dict(arguments)
                action = args.pop("action", None)
                operations = {"propose": proposal, "approve": approve, "inspect": inspect_files,
                              "import": import_reviewed, "stage": stage, "branch": branch_share}
                require(action in operations or action == 'verify', "Unknown sharing action")
                require('workspace' not in args, 'Source workspace is server configured', 'not_authorized')
                if action == 'branch':
                    require(self.workspace is not None, 'Branching requires a workspace-configured daemon', 'source_unavailable')
                if action in {'verify', 'approve', 'stage', 'import', 'branch'}:
                    if self.workspace:
                        self.workspace.refresh_policy()
                    operation_fn = verify_sources if action == 'verify' else operations[action]
                    result = operation_fn(self.store, access, workspace=self.workspace, **args)
                else:
                    result = operations[action](self.store, access, **args)
            else:
                raise LumenError("unsupported_capability", "Unknown operation")
            response.update(result=result, snapshot=self.store.watermark(),
                            deletion_epoch=self.store.journal.snapshot()["digest"],
                            complete=result.get("complete", True), budget_used=len(canonical(result)))
            response["state_token"] = result.get("state_token")
            if response["state_token"] is None and result.get("results"):
                response["state_token"] = result["results"][0].get("state_token")
        except LumenError as exc:
            response.update(error={"code": exc.code, "message": str(exc)}, complete=False)
        except (TypeError, KeyError, ValueError):
            response.update(error={"code": "invalid_event", "message": "Invalid request fields"}, complete=False)
        if operation == "recall":
            limit = self.store.config["max_packet_bytes"]
            for _ in range(3):
                response["budget_used"] = len(canonical(response))
            while len(canonical(response)) > limit and response.get("result", {}).get("results"):
                result = response["result"]
                result["results"].pop()
                result["complete"] = response["complete"] = False
                result["status"] = "ok" if result["results"] else "insufficient_evidence"
                result["budget_used_bytes"] = sum(len(canonical(p)) for p in result["results"])
                response["state_token"] = result["results"][0].get("state_token") if result["results"] else None
                for _ in range(3):
                    response["budget_used"] = len(canonical(response))
            if len(canonical(response)) > limit:
                response.pop("result", None)
                response.update(complete=False, state_token=None,
                    error={"code": "budget_exhausted", "message": "Recall metadata exceeds packet limit"})
                for _ in range(3):
                    response["budget_used"] = len(canonical(response))
        return response
