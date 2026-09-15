"""Reviewed local transfer. Publishing and PR creation are deliberately separate."""
import json
import hashlib
import os
from pathlib import Path
import subprocess
import uuid
import time

from .model import LumenError, canonical, digest, references, require, validate
from .projection import closure
from .recovery import atomic_json
from .security import CONFIG, SECRET, safe_source


def read_review(path):
    try:
        with Path(path).open('rb') as stream:
            data = stream.read(1024 * 1024 + 1)
        require(len(data) <= 1024 * 1024, 'Review evidence exceeds bound', 'not_authorized')
        value = json.loads(data)
        require(isinstance(value, dict), 'Invalid review evidence', 'not_authorized')
        return value
    except (OSError, ValueError):
        raise LumenError('not_authorized', 'Review evidence unavailable or invalid') from None


def event_markdown(event):
    validate(event)
    payload = canonical(event)
    require(len(payload) <= CONFIG["max_event_bytes"], "Shared event too large", "budget_exhausted")
    return b"# Lumen event\n\n```json\n" + payload + b"\n```\n"


def read_event(path):
    with Path(path).open("rb") as stream:
        data = stream.read(CONFIG["max_event_bytes"] + 65)
    require(len(data) <= CONFIG["max_event_bytes"] + 64, "Shared event too large", "budget_exhausted")
    prefix, suffix = b"# Lumen event\n\n```json\n", b"\n```\n"
    require(data.startswith(prefix) and data.endswith(suffix), "Invalid shared event file")
    event = json.loads(data[len(prefix):-len(suffix)])
    require(event_markdown(event) == data, "Noncanonical shared event file")
    require(Path(path).stem == event["id"], "Shared event filename mismatch")
    return event


def write_event(path, event, *, temporary=None, fault=lambda _: None):
    """Publish a complete immutable local file without replacing an existing ID."""
    path = Path(path)
    payload = event_markdown(event)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(temporary) if temporary else path.with_name(".lumen-" + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        fault("after_stage_fsync")
        try:
            os.link(temporary, path)
        except FileExistsError:
            require(read_event(path) == event, "Shared ID collision")
        fault("after_stage_link")
        if os.name != "nt":
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


def proposal(store, access, ids):
    require(access.owner, "Sharing requires owner channel", "not_authorized")
    by_id = {e["id"]: e for e in store.events(access)}
    require(all(i in by_id for i in ids), "Source unavailable", "source_unavailable")
    selected = set(ids)
    while True:
        old = set(selected)
        for i in old:
            selected.update(r["id"] for r in references(by_id[i]))
        require(selected <= by_id.keys(), "Missing reference")
        if old == selected:
            break
    events = [by_id[i] for i in sorted(selected)]
    for e in events:
        require(e["scope"] == "monorepo" or e["scope"].startswith("repo:"), "Private scope cannot be shared", "not_authorized")
        require(e["kind"] != "approval", "Approvals are local control records", "not_authorized")
        require(e["kind"] != "episode", "Captured episodes never leave the machine", "not_authorized")
        if "citations" in e:
            require(all(c["kind"] == "file" for c in e["citations"]),
                    "Episode payloads cannot cross; create and review a file-backed derivative", "not_authorized")
        require(not SECRET.search(canonical(e).decode()), "Secret in share", "not_authorized")
    closure(events)
    return {"schema": 1, "workspace": access.workspace, "destination": "shared",
            "policy": 1, "events": events}


def _read_shared_events(store, access, directory, max_events, max_bytes):
    require(access.owner, "Inspection requires owner channel", "not_authorized")
    events, total = [], 0
    for scope in sorted(access.scopes):
        if scope != "monorepo" and not scope.startswith("repo:"):
            continue
        folder = "_monorepo" if scope == "monorepo" else scope.removeprefix("repo:")
        root = safe_source(directory, folder)
        if not root.is_dir():
            continue
        names = []
        with os.scandir(root) as entries:
            for count, entry in enumerate(entries, 1):
                require(count <= max(1000, max_events), "Shared directory scan bound exceeded", "budget_exhausted")
                if entry.name.endswith(".md"):
                    names.append(entry.name)
                    require(len(names) + len(events) <= max_events, "Shared event count exceeds bound", "budget_exhausted")
        for name in sorted(names):
            path = safe_source(root, name)
            require(len(events) < max_events, "Shared event count exceeds bound", "budget_exhausted")
            event = read_event(path)
            require(access.permits(event) and event["scope"] == scope, "Shared folder/scope mismatch", "not_authorized")
            require(not store.journal.blocked(event["workspace"], event["id"]), "Erased event", "not_authorized")
            total += len(canonical(event))
            require(total <= max_bytes, "Shared content exceeds byte bound", "budget_exhausted")
            events.append(event)
    _, pending = closure(events)
    require(not pending, 'Shared reference targets are missing', 'index_pending')
    return sorted(events, key=lambda e: e['id'])


def inspect_files(store, access, directory):
    """Read only granted scope folders; inspection never grants admission."""
    events = _read_shared_events(store, access, directory, 100, CONFIG['max_event_bytes'] - 1024)
    bundle = {"schema": 1, "workspace": access.workspace, "destination": "shared", "policy": 1,
              "events": events}
    return {"bundle": bundle, "reviewed_digest": digest(bundle), "admitted": False}


def approve(store, access, bundle, reviewed_digest, workspace=None, snapshot='current'):
    require(access.owner, "Review requires owner channel", "not_authorized")
    require(digest(bundle) == reviewed_digest, "Review digest mismatch", "not_authorized")
    require(set(bundle) == {"schema", "workspace", "destination", "policy", "events"} and
            bundle["schema"] == 1 and bundle["workspace"] == access.workspace and
            bundle["destination"] == "shared" and bundle["policy"] == 1, "Invalid share schema")
    for e in bundle["events"]:
        validate(e)
        require(access.permits(e) and e["scope"] != "user" and not e["scope"].startswith("session"), "Share outside authority", "not_authorized")
        require(e["kind"] != "approval", "Self-approving records rejected", "not_authorized")
        require(e["kind"] != "episode", "Captured episodes never leave the machine", "not_authorized")
        require(not SECRET.search(canonical(e).decode()), "Secret in share", "not_authorized")
        if "citations" in e:
            require(all(c["kind"] == "file" for c in e["citations"]), "Raw episodes cannot cross", "not_authorized")
    _, pending = closure(bundle['events'])
    require(not pending, 'Sharing approval requires complete reference targets', 'index_pending')
    proof = verify_sources(store, access, bundle, workspace, snapshot)
    require(proof['all_sources_match'], "Permitted source verification required before approval", "source_unavailable")
    require(len(canonical(proof)) <= 1024 * 1024, 'Review evidence exceeds bound', 'budget_exhausted')
    receipt = {"schema": 2, "digest": reviewed_digest, "actor": access.actor,
               "workspace": access.workspace, "destination": "shared", "policy": 1,
               "source_proof": digest(proof), "source_snapshot": snapshot}
    atomic_json(store.home / 'source-reviews' / (receipt['source_proof'] + '.json'), proof)
    atomic_json(store.home / "approvals" / (reviewed_digest + ".json"), receipt)
    return receipt


def verify_sources(store, access, bundle, workspace, snapshot='current'):
    """Check permitted checkout bytes without granting sharing authority."""
    require(access.owner, "Source review requires owner", "not_authorized")
    require(snapshot in {'current', 'cited_revision'}, "Unsupported source snapshot")
    require(workspace is not None and workspace.id == access.workspace, "Workspace source context required", "source_unavailable")
    workspace.refresh_policy()
    require(bundle.get("workspace") == access.workspace, "Bundle outside workspace", "not_authorized")
    events = bundle.get("events", [])
    require(isinstance(events, list) and len(events) <= 100, "Review batch too large", "budget_exhausted")
    for event in events:
        validate(event)
        require(access.permits(event), "Share outside grant", "not_authorized")
        require(not store.journal.blocked(event['workspace'], event['id']), "Erased event", "not_authorized")
    closure(events)
    catalog = workspace.snapshot()
    available = {p['id']: p for p in catalog['projects'] if p['availability'] == 'current' and 'repo:' + p['id'] in access.scopes}
    receipts = []
    for event in events:
        for citation in event.get('citations', []):
            receipt = {'event': event['id'], 'citation_digest': digest(citation), 'status': 'source_unavailable'}
            if citation['kind'] == 'file' and citation['project'] in available:
                try:
                    if snapshot == 'cited_revision':
                        data = workspace.historical_source(citation['project'], citation['path'], citation['revision'])
                    else:
                        source = workspace.source(citation['project'], citation['path'])
                        with source.open('rb') as stream:
                            data = stream.read(8 * 1024 * 1024 + 1)
                    require(len(data) <= 8 * 1024 * 1024, "Source too large", "budget_exhausted")
                    receipt.update(status='matching_bytes' if hashlib.sha256(data).hexdigest() == citation['sha256'] else 'changed_bytes',
                                   observed_revision=citation['revision'] if snapshot == 'cited_revision' else available[citation['project']]['revision'])
                except (OSError, ValueError, LumenError):
                    pass
            receipts.append(receipt)
    return {'bundle_digest': digest(bundle), 'workspace_policy_digest': digest(workspace.manifest),
            'members': [{'id': e['id'], 'scope': e['scope'], 'digest': digest(e)} for e in events],
            'receipts': receipts, 'all_sources_match': bool(receipts) and all(r['status'] == 'matching_bytes' for r in receipts),
            'approval_granted': False, 'snapshot': snapshot, 'support': 'Matching bytes do not establish entailment'}


def _review_proof(store, access, key, workspace):
    require(access.owner, "Review requires owner channel", "not_authorized")
    require(workspace is not None and workspace.id == access.workspace, 'Workspace source context required', 'not_authorized')
    workspace.refresh_policy()
    path = store.home / "approvals" / (key + ".json")
    require(path.is_file(), "No independently recorded destination review", "not_authorized")
    receipt = read_review(path)
    require(set(receipt) == {'schema', 'digest', 'actor', 'workspace', 'destination', 'policy', 'source_proof', 'source_snapshot'} and
            {k: v for k, v in receipt.items() if k not in {'source_proof', 'source_snapshot'}} ==
            {"schema": 2, "digest": key, "actor": access.actor, "workspace": access.workspace,
             "destination": "shared", "policy": 1} and receipt['source_snapshot'] in {'current', 'cited_revision'} and
            isinstance(receipt['source_proof'], str) and len(receipt['source_proof']) == 64 and
            all(c in '0123456789abcdef' for c in receipt['source_proof']),
            "Invalid authority receipt; fresh source review required", "not_authorized")
    proof_path = store.home / 'source-reviews' / (receipt['source_proof'] + '.json')
    require(proof_path.is_file(), 'Source review evidence missing', 'not_authorized')
    proof = read_review(proof_path)
    require(digest(proof) == receipt['source_proof'] and proof.get('bundle_digest') == key and
            proof.get('snapshot') == receipt['source_snapshot'] and proof.get('all_sources_match') is True,
            'Source review evidence mismatch', 'not_authorized')
    require(proof.get('workspace_policy_digest') == digest(workspace.manifest),
            'Workspace policy changed; fresh review required', 'not_authorized')
    return proof


def reviewed_receipt(store, access, bundle, workspace=None):
    key = digest(bundle)
    _review_proof(store, access, key, workspace)
    require(bundle["workspace"] == access.workspace and bundle["destination"] == "shared",
            "Invalid destination", "not_authorized")
    _, pending = closure(bundle['events'])
    require(not pending, 'Shared reference targets are missing', 'index_pending')
    for event in bundle["events"]:
        validate(event)
        require(access.permits(event), "Share outside grant", "not_authorized")
        require(not store.journal.blocked(event["workspace"], event["id"]), "Erased event", "not_authorized")
    return key


def import_reviewed(store, access, bundle, workspace=None):
    key = reviewed_receipt(store, access, bundle, workspace)
    with store.transaction():
        closure(store.events(access) + bundle["events"])
        for e in bundle["events"]:
            require(access.permits(e), "Share outside grant", "not_authorized")
            store._insert(e)
            store.db.execute("INSERT OR REPLACE INTO shared_admissions VALUES(?,?,?)", (e["workspace"], e["id"], key))
    store.reindex(access)
    return {"admitted": len(bundle["events"]), "approval_digest": key}


def sync_checkout(store, access, workspace, *, check_only=False):
    """Reconcile the permitted shared tree using an existing local destination review."""
    require(workspace is not None and workspace.id == access.workspace,
            'Workspace source context required', 'source_unavailable')
    workspace.refresh_policy()
    directory = safe_source(workspace.root, '.lumen/shared', workspace.manifest['exclusions'])
    if not directory.exists():
        return {'events': [], 'reviews': {}} if check_only else {'admitted': 0, 'checked': 0}
    started = time.monotonic()
    events = _read_shared_events(store, access, directory, 10000, 64 * 1024 * 1024)
    require(time.monotonic() - started < 0.5, 'Shared checkout scan exceeded deadline', 'index_pending')
    if not events:
        return {'events': [], 'reviews': {}} if check_only else {'admitted': 0, 'checked': 0}
    bundle = {'schema': 1, 'workspace': access.workspace, 'destination': 'shared', 'policy': 1, 'events': events}
    reviews = {}
    try:
        key = reviewed_receipt(store, access, bundle, workspace)
        reviews = {event['id']: key for event in events}
    except LumenError:
        # Each member digest is bound into its independently authenticated source proof.
        # No event payload or scope outside the caller's grant is read from shared files.
        expected = {event['id']: {'id': event['id'], 'scope': event['scope'], 'digest': digest(event)} for event in events}
        approval_directory = store.home / 'approvals'
        candidates = []
        if approval_directory.is_dir():
            with os.scandir(approval_directory) as entries:
                for count, entry in enumerate(entries, 1):
                    require(count <= 1000, 'Review lookup exceeds bound', 'index_pending')
                    key = Path(entry.name).stem
                    if entry.name == key + '.json' and len(key) == 64 and all(c in '0123456789abcdef' for c in key):
                        candidates.append(key)
        proof_bytes = 0
        for key in sorted(candidates):
            require(time.monotonic() - started < 0.5, 'Shared review lookup exceeded deadline', 'index_pending')
            try:
                proof = _review_proof(store, access, key, workspace)
            except LumenError:
                continue
            proof_bytes += len(canonical(proof))
            require(proof_bytes <= 16 * 1024 * 1024, 'Review lookup exceeds byte bound', 'index_pending')
            members = proof.get('members', [])
            require(isinstance(members, list) and len(members) <= 100, 'Invalid review membership', 'index_pending')
            for member in members:
                if isinstance(member, dict) and isinstance(member.get('id'), str) and expected.get(member['id']) == member:
                    reviews.setdefault(member['id'], key)
            if len(reviews) == len(events):
                break
    require(len(reviews) == len(events), 'Shared checkout requires valid destination review', 'index_pending')
    if check_only:
        return {'events': events, 'reviews': reviews}
    unchanged = all(store.db.execute(
        'SELECT 1 FROM shared_admissions WHERE workspace=? AND id=? AND review=?',
        (event['workspace'], event['id'], reviews[event['id']])).fetchone() for event in events)
    if unchanged:
        return {'admitted': 0, 'checked': len(events)}
    with store.transaction():
        closure(store.events(access) + events)
        for event in events:
            require(access.permits(event), 'Share outside grant', 'not_authorized')
            store._insert(event)
            store.db.execute('INSERT OR REPLACE INTO shared_admissions VALUES(?,?,?)',
                (event['workspace'], event['id'], reviews[event['id']]))
    store.reindex(access)
    return {'admitted': len(events), 'checked': len(events), 'reviews': len(set(reviews.values()))}


def audit_checkout(store, access, workspace):
    """Read-only component checks; hosted repository authority stays explicit."""
    require(access.owner, 'Audit requires owner', 'not_authorized')
    require(workspace is not None, 'Audit requires workspace', 'source_unavailable')
    checks = {}
    try:
        routing = workspace.manage_codeowners('check')
        checks['codeowners_routing'] = {'status': 'passed' if routing['healthy'] else 'failed',
                                      'changed': routing['changed']}
    except (LumenError, OSError, ValueError) as exc:
        checks['codeowners_routing'] = {'status': 'failed', 'reason': getattr(exc, 'code', 'source_unavailable')}
    try:
        inspected = sync_checkout(store, access, workspace, check_only=True)
        events, reviews = inspected['events'], inspected['reviews']
        require(all(not SECRET.search(canonical(event).decode()) for event in events),
                'Secret pattern in shared content', 'not_authorized')
        checks['shared_integrity_and_local_review'] = {'status': 'passed', 'events': len(events)}
        groups = {}
        for event in events:
            groups.setdefault(reviews[event['id']], []).append(event)
        failures, checked = [], 0
        for key, members in sorted(groups.items()):
            proof = _review_proof(store, access, key, workspace)
            bundle = {'schema': 1, 'workspace': access.workspace, 'destination': 'shared', 'policy': 1, 'events': members}
            verified = verify_sources(store, access, bundle, workspace, proof['snapshot'])
            checked += len(verified['receipts'])
            failures.extend({'event': receipt['event'], 'citation_digest': receipt['citation_digest'], 'status': receipt['status']}
                            for receipt in verified['receipts'] if receipt['status'] != 'matching_bytes')
        checks['permitted_source_bytes'] = {'status': 'failed' if failures else 'passed',
                                          'checked': checked, 'failures': failures[:100], 'complete': len(failures) <= 100}
    except (LumenError, OSError, ValueError) as exc:
        checks['shared_integrity_and_local_review'] = {'status': 'failed', 'reason': getattr(exc, 'code', 'source_unavailable')}
        checks['permitted_source_bytes'] = {'status': 'pending', 'reason': 'Shared review checks did not complete'}
    mode = workspace.policy['share']['pull_request']
    if mode == 'pending':
        # The team has acknowledged in config.toml that no forge authority exists yet (ADR 0008);
        # the check reports pending and is accepted, never silently passed.
        checks['repository_review_authority'] = {'status': 'pending', 'configured': True, 'mode': mode,
            'reason': 'share.pull_request = "pending": forge review authority acknowledged as not yet available'}
    elif mode == 'forge':
        checks['repository_review_authority'] = {'status': 'failed', 'configured': True, 'mode': mode,
            'reason': 'share.pull_request = "forge" but no forge authority client exists (ADR 0008)'}
    else:
        checks['repository_review_authority'] = {'status': 'pending', 'configured': False, 'mode': mode,
            'reason': 'Authenticated owning-repository review evidence is not implemented; set share.pull_request in config.toml'}
    try:
        checks['conflicts_and_type_specific_evidence'] = _conflict_check(inspected['events'], store.rules)
    except (LumenError, NameError):
        checks['conflicts_and_type_specific_evidence'] = {'status': 'pending', 'reason': 'Shared review checks did not complete'}
    checks['pull_request_branches'] = _branch_check(store, access)
    accepted = lambda check: check['status'] == 'passed' or (check['status'] == 'pending' and check.get('configured') is True)
    return {'passed': all(accepted(check) for check in checks.values()), 'checks': checks,
            'read_only': True, 'automatic_deletion': False,
            'accepted_pending': sorted(name for name, check in checks.items() if check['status'] == 'pending' and check.get('configured'))}


def _conflict_check(events, rules):
    """Type-specific evidence and explicit-dispute audit over the admitted shared events.

    Shared assertions carry at least one file citation; evidence events cite files; control
    events name a reason and resolve inside the tree (the closure already rejected dangling or
    cross-scope references). Competing successors without an explicit dispute are a failure:
    git agreement is not semantic agreement, and a silent fork must not project as truth.
    """
    from .projection import intervals
    findings = []
    for e in events:
        if e['kind'] in ('assertion', 'evidence') and not any(c['kind'] == 'file' for c in e['citations']):
            findings.append({'event': e['id'], 'reason': 'no_file_citation'})
        if e['kind'] == 'evidence' and any(c['kind'] != 'file' for c in e['citations']):
            findings.append({'event': e['id'], 'reason': 'episode_payload_in_shared_evidence'})
    keys = {(e['workspace'], e['scope'], e['subject'], e['relation']) for e in events if e['kind'] == 'assertion'}
    for workspace, scope, subject, relation in sorted(keys):
        group = [e for e in events if e['scope'] == scope]
        explicit = any(e['kind'] == 'dispute' for e in group)
        for span in intervals(group, workspace, subject, relation, rules=rules):
            if span['status'] == 'disputed' and not explicit:
                findings.append({'subject': subject, 'relation': relation, 'scope': scope, 'start': span['start'],
                                 'reason': 'silent_competing_successors'})
                break
    return {'status': 'failed' if findings else 'passed', 'events': len(events), 'subjects': len(keys),
            'findings': findings[:100], 'complete': len(findings) <= 100,
            'support': 'Type and dispute checks; entailment of a source is not audited mechanically'}


def _branch_check(store, access):
    shares = list_shares(store, access)
    open_ = [share for share in shares if share['pull_request']['status'] == 'pending']
    return {'status': 'passed', 'shares': len(shares), 'awaiting_pull_request': len(open_),
            'branches': [batch['branch'] for share in open_ for batch in share['batches']][:100]}


def _git_env():
    from .workspace import git_environment
    env = git_environment()
    env.update(GIT_ALLOW_PROTOCOL='', GIT_NO_LAZY_FETCH='1')
    return env


def _git(root, *args, env_extra=None, stdin=None):
    env = _git_env()
    if env_extra:
        env.update(env_extra)
    try:
        run = subprocess.run(['git', '--no-optional-locks', '-C', str(root), '-c', 'protocol.allow=never', *args],
                             capture_output=True, text=True, timeout=30, env=env, input=stdin)
    except (OSError, subprocess.TimeoutExpired):
        raise LumenError('source_unavailable', 'Git unavailable or timed out') from None
    require(run.returncode == 0, 'Git operation failed: ' + ' '.join(args[:2]), 'source_unavailable')
    return run.stdout.strip()


def _share_identity(root):
    """Author identity for share commits: the checkout's own configuration, else a fixed local one."""
    try:
        name, email = _git(root, 'config', 'user.name'), _git(root, 'config', 'user.email')
    except LumenError:
        name, email = '', ''
    return {'GIT_AUTHOR_NAME': name or 'Lumen share', 'GIT_AUTHOR_EMAIL': email or 'lumen@localhost',
            'GIT_COMMITTER_NAME': name or 'Lumen share', 'GIT_COMMITTER_EMAIL': email or 'lumen@localhost'}


def branch_share(store, access, bundle, workspace=None):
    """Write a reviewed bundle to `.lumen/shared/` on branches, one per repo batch, without pushing.

    Each batch is capped by `share.batch_limit` from config.toml and committed on its own branch
    `lumen/share/<folder>/<digest12>` through a temporary worktree, so the developer's checkout
    never changes. The share record under `~/.lumen/shares/` carries pull_request.status
    `pending` until a forge authority client exists (ADR 0008). Never pushes, never opens a PR.
    """
    key = reviewed_receipt(store, access, bundle, workspace)
    limit = workspace.policy['share']['batch_limit']
    record_path = store.home / 'shares' / (key + '.json')
    if record_path.is_file():
        existing = read_review(record_path)
        require(existing.get('bundle_digest') == key, 'Share record mismatch', 'not_authorized')
        return {**existing, 'already_branched': True}
    gitroot = Path(_git(workspace.root, 'rev-parse', '--show-toplevel')).resolve()
    require(workspace.root.is_relative_to(gitroot), 'Workspace outside its repository', 'source_unavailable')
    base = _git(workspace.root, 'rev-parse', 'HEAD')
    shared = safe_source(workspace.root, '.lumen/shared', workspace.manifest['exclusions'])
    prefix = shared.relative_to(gitroot).as_posix()
    groups = {}
    for e in sorted(bundle['events'], key=lambda e: e['id']):
        require(not store.journal.blocked(e['workspace'], e['id']), 'Erased event', 'not_authorized')
        folder = '_monorepo' if e['scope'] == 'monorepo' else e['scope'].removeprefix('repo:')
        groups.setdefault(folder, []).append(e)
    batches = []
    identity = _share_identity(workspace.root)
    for folder in sorted(groups):
        events = groups[folder]
        for start in range(0, len(events), limit):
            batch = events[start:start + limit]
            branch = f'lumen/share/{folder}/{digest([e["digest"] for e in batch])[:12]}'
            if _git(workspace.root, 'branch', '--list', branch):
                batches.append({'branch': branch, 'folder': folder, 'events': [e['id'] for e in batch],
                                'commit': _git(workspace.root, 'rev-parse', branch), 'base': base, 'reused': True})
                continue
            worktree = store.home / 'worktrees' / uuid.uuid4().hex
            worktree.parent.mkdir(parents=True, exist_ok=True)
            _git(workspace.root, 'worktree', 'add', '--quiet', '-b', branch, str(worktree), base)
            try:
                for e in batch:
                    path = safe_source(worktree, f'{prefix}/{folder}/{e["id"]}.md')
                    if path.exists():
                        require(read_event(path) == e, 'Shared ID collision')
                    else:
                        write_event(path, e)
                _git(worktree, 'add', '-A', '--', prefix + '/' + folder)
                message = (f'Share {len(batch)} Lumen event(s) for {folder}\n\nReviewed bundle {key}\n'
                           + '\n'.join('- ' + e['id'] for e in batch) + '\n')
                _git(worktree, 'commit', '--quiet', '--no-verify', '-F', '-', env_extra=identity, stdin=message)
                commit = _git(worktree, 'rev-parse', 'HEAD')
            finally:
                subprocess.run(['git', '-C', str(workspace.root), 'worktree', 'remove', '--force', str(worktree)],
                               capture_output=True, timeout=30, env=_git_env())
            batches.append({'branch': branch, 'folder': folder, 'events': [e['id'] for e in batch],
                            'commit': commit, 'base': base, 'reused': False})
    record = {'schema': 1, 'workspace': access.workspace, 'bundle_digest': key, 'repository': str(gitroot),
              'batch_limit': limit, 'batches': batches, 'pushed': False,
              'pull_request': {'status': 'pending', 'mode': workspace.policy['share']['pull_request'],
                               'reason': 'No forge authority client; push the branch and open the pull request by hand (ADR 0008)'},
              'created_ns': time.time_ns()}
    atomic_json(record_path, record)
    return record


def list_shares(store, access):
    require(access.owner, 'Pending review requires owner', 'not_authorized')
    directory = store.home / 'shares'
    shares = []
    if directory.is_dir():
        for path in sorted(directory.glob('*.json'))[:1000]:
            try:
                record = read_review(path)
            except LumenError:
                shares.append({'bundle_digest': path.stem, 'pull_request': {'status': 'unreadable'}, 'batches': []})
                continue
            if record.get('workspace') == access.workspace:
                shares.append(record)
    return shares


def stage(store, access, bundle, directory, workspace=None):
    """Write immutable reviewed events to a local preparation directory only."""
    reviewed_receipt(store, access, bundle, workspace)
    for e in bundle["events"]:
        require(not store.journal.blocked(e["workspace"], e["id"]), "Erased event", "not_authorized")
        folder = "_monorepo" if e["scope"] == "monorepo" else e["scope"].removeprefix("repo:")
        path = safe_source(directory, folder + "/" + e["id"] + ".md")
        temporary = path.with_name(".lumen-" + uuid.uuid4().hex + ".tmp")
        with store.transaction():
            require(not store.journal.blocked(e["workspace"], e["id"]), "Erased event", "not_authorized")
            if path.exists():
                require(read_event(path) == e, "Shared ID collision")
            require(not store.db.execute("SELECT 1 FROM staged_files WHERE path=? AND temporary IS NOT NULL", (str(path),)).fetchone(),
                    "Staging pending; reopen store to recover", "index_pending")
            store.db.execute("INSERT OR REPLACE INTO staged_files VALUES(?,?,?,?,?)",
                (str(path), e["workspace"], e["id"], hashlib.sha256(event_markdown(e)).hexdigest(), str(temporary)))
        store.fault("after_stage_registration")
        with store.transaction():
            require(store.db.execute("SELECT 1 FROM staged_files WHERE path=? AND temporary=?", (str(path), str(temporary))).fetchone(),
                    "Stage recovered by another writer; retry", "index_pending")
            require(not store.journal.blocked(e["workspace"], e["id"]), "Erased event", "not_authorized")
            write_event(path, e, temporary=temporary, fault=store.fault)
            store.db.execute("UPDATE staged_files SET temporary=NULL WHERE path=?", (str(path),))
    return {"staged": len(bundle["events"]), "published": False}
