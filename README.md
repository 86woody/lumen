# Lumen

Lumen is an offline lexical memory implementation in development. **Release 1
and Release 2 are incomplete.** The current specification is
[Lumen.md](.agents/plans/Lumen.md); remaining acceptance is in
[the requirement matrix](docs/requirements.md) and the design decisions are in
[docs/adr](docs/adr). The validation evidence (client transcripts, install
reports, scale measurements) that those documents cite is kept out of this
repository.

The alpha provides durable explicit capture, bounded revisions and retractions,
historical point queries, FTS5/exact lookup, source inspection, local receipts,
reviewed local transfer, export, deletion and journal-checked restore. A local
daemon serves the CLI and four MCP tools. Runtime dependencies are Python 3.12+
with SQLite FTS5 and Git. Windows x64 is the tested development platform.

There are no embedding, vector, model, hosted retrieval or paid API paths.
Automatic retirement and all optional model features are disabled. Ambiguous
single-valued facts are disputed until an explicit revision resolves them.

## Run the alpha locally

From a clone, install into a virtual environment with pip or uv; pip fetches the
pinned setuptools itself:

```powershell
python -m venv .venv; .venv/Scripts/pip install .
uv venv .venv; uv pip install .
```

The maintainer build below is the reproducible path: it uses the pinned build
wheel kept outside the repository under `artifacts/local/wheels` and produces the
wheel whose digest the evidence records.

```powershell
python -m pip wheel --no-index --find-links artifacts/local/wheels --no-deps --wheel-dir dist .
python -m venv .venv
.venv/Scripts/python -m pip install --no-index --no-deps dist/lumen_memory-0.1.0a1-py3-none-any.whl
```

Create a disposable demo directory and use an explicit local store. Run the daemon
in a separate terminal; the command remains in the foreground.

```powershell
New-Item -ItemType Directory -Force demo
.venv/Scripts/lumen --home .lumen-local init --workspace demo --project local
.venv/Scripts/lumen --home .lumen-local daemon --workspace demo
```

The owner CLI accepts JSON on stdin to avoid shell-quoting issues:

```powershell
'{"checkout":"demo","host":"cli","session":"s1","turn":"t1","scope":"repo:local","subject":"demo compiler","relation":"test_command","value":"python -m unittest","text":"The demo test command is python -m unittest.","region":{"platform":"any","branch":"any","path":"**","start":0,"end":null}}' | .venv/Scripts/lumen --home .lumen-local remember --json -
'{"query":"demo compiler"}' | .venv/Scripts/lumen --home .lumen-local recall --json -
.venv/Scripts/lumen --home .lumen-local doctor
.venv/Scripts/lumen --home .lumen-local repos --workspace demo
```

Capture requires stable checkout/host/session/turn identity. Reusing it with
different content fails. `revise` requires predecessor IDs, the latest complete
state token, an affected region and a reason. `known_at` is a local ingestion
sequence; `at` is integer UTC seconds. A null start is unknown, not today.

For MCP, launch the installed Python executable with arguments
`-m lumen --home <absolute-store-path> mcp`. The daemon must already be running.
MCP exposes remember, recall, revise and expand; owner management is excluded.
Client installation and automatic hooks are not yet qualified.

## Reviewed local sharing

`share --json -` accepts owner-only actions. Use `{"action":"propose","ids":["event-id"]}`
to obtain a bundle. Review its complete contents, then use `action: approve` with
that `bundle` and its canonical `reviewed_digest`. Approval binds the local owner,
workspace, destination and policy, and rechecks its permitted source bytes using
the daemon's workspace. Changed or unavailable evidence prevents approval. Use
`"snapshot":"cited_revision"` when approving raw Git blob citations. Receipts from
earlier alpha versions require fresh review. `action: stage` accepts the same bundle and a
`directory` for local preparation; `action: import` requires independent approval
in the destination store. These commands do not publish or open a pull request.
`action: branch` takes the same reviewed bundle and commits its events to
`lumen/share/<repo-id>/<digest>` branches, one per repo batch capped by `share.batch_limit`,
through a temporary worktree that leaves the checkout untouched; nothing is pushed and the
pull request is opened by hand until the forge client exists (ADR 0008). `lumen pending`
lists branches awaiting one.
Source review records the workspace policy digest. Staging and import reload that
policy from disk and require fresh review if it changed. Changing the workspace ID
requires enrollment again.

For files received locally, use `{"action":"inspect","directory":"absolute/path/to/staged"}`.
Inspection returns `bundle` and `reviewed_digest` without admitting events. Review
that bundle before approving it in this store, then import the unchanged bundle.
Inspection reads granted scope folders only, with a maximum of 100 events and a
bounded total size. Oversized batches fail; split them without breaking references.

`{"action":"verify","bundle":{...}}` checks cited bytes against the daemon's
permitted current checkout and reports matching, changed or unavailable sources.
It needs a workspace-configured daemon and grants no approval. Matching hashes
establish byte identity only. Add `"snapshot":"cited_revision"` to verify a full
commit ID against local Git blob bytes, with fetching and external protocols
disabled. This uses raw Git bytes, not checkout filters or newline conversion;
a worktree-byte hash may differ. Missing objects report unavailable. Full CI
sharing acceptance and historical paths across logical project moves remain pending.

Staging writes `<repo-id>/<event-id>.md` or `_monorepo/<event-id>.md`. Files contain
canonical JSON in a Markdown fence. Unique files are atomically linked into place;
existing identical events are retained and conflicting IDs fail. The filesystem
must support hard links. Staging registers its files and temporary payloads durably;
restart cleans interrupted temporaries and purge removes unchanged managed copies.
If a managed file was edited, cleanup refuses to delete it and reports that owner
cleanup is required. PR/CODEOWNERS integration remains pending. Use a dedicated
preparation directory; Git history and copies made outside Lumen remain outside
this local cleanup guarantee.

## Instruction files and Claude Code hooks

`lumen instructions preview|write|check --workspace <root>` maintains one fenced
Lumen section in the workspace root `AGENTS.md` and creates `CLAUDE.md` containing
only `@AGENTS.md` when it is absent. Existing `CLAUDE.md` files are never edited and
bytes outside the markers are preserved. A Claude Code session launched from a
subdirectory treats that import as external and asks for a one-time approval per
project; launch from the root or approve it once interactively (ADR 0005).

`lumen hooks preview --host claude-code` prints the user-level settings for three
exec-form hooks, each running the stdlib-only launcher `python -I -m lumen.hook`
from the installed interpreter: SessionStart delivers the memory hint, UserPromptSubmit
and Stop capture each finished turn as one redacted `episode` event without any model
call. `hooks write` merges only those handlers into `~/.claude/settings.json` after
writing a timestamped backup; nothing changes without that explicit action.
`lumen hooks preview --host copilot-cli` does the same for Copilot CLI through one owned
file under the Copilot hooks directory; its stop hook captures the finished turn from the
Copilot transcript (ADR 0006). Outside
a Lumen workspace the launcher exits silently after one interpreter start; measure it
with `python tools/hook-timing.py`. Episodes stay on the machine: sharing refuses
them, purge erases them, and `expand` returns them by id. A doubled hook delivers
the same payload twice and the ledger keeps one episode (ADR 0006).

## Team files, session start and scopes

`lumen init` writes two committed team files beside `workspace.json` (ADR 0007):
`.lumen/relations.yaml`, the relation vocabulary in a strict one-line-per-relation
subset (`name: {cardinality: single, dimensions: [platform, branch]}`; an undeclared
relation is `multi` over every dimension), and `.lumen/config.toml`, the policy read
with `tomllib` (a pinned version, caps that may only be lowered, trust flags that must
stay `false`, share limits). Both reload on every daemon call; a malformed file fails
those calls loudly and `doctor` reports it. The session-start hook delivers the hint
plus a catalog slice (this repo, dependencies, dependents, a count of the rest) under
2,000 estimated tokens. Every grant carries `user` (this developer's facts, never
shared) and `session`: a call that names a `session` reads and writes `session:<id>`
working state that no other session sees.

## Retrieval

Recall is lexical and relational only: BM25 over the assertion and its citation text,
exact lookup over identifier variants (`checkEvidence`, `check_evidence`, a path's basename),
one expansion pass through source-backed aliases (an assertion with relation `alias_of`;
an alias naming two entities expands nothing and the packet says so), a one-hop join along
indexed relation edges, demotion of results that only repeat an already-shown source span,
and scope widening from the current repo through the monorepo and one dependency hop to the
developer's own facts, nearer first. `signals` and `expansions` in every packet name what
ran. There is no embedding, vector or semantic path; `python tools/measure-scale.py 100000
--ipc` records the 100k-record distributions for R-14.

## Consolidation

`lumen consolidate [--force]` (and the daemon after every captured turn, at most once a
minute) turns new episodes into facts without a model: a line of user, assistant or tool
text that reads `<subject> <relation>: <value>` with a relation from `.lumen/relations.yaml`
becomes an `agent-observed` (or `tool-derived`) assertion citing the episode. The same value
on a current fact adds evidence; a competing value on a single-valued relation becomes an
explicit dispute, because the supersession judge is disabled and the consolidator never
retires anything (ADR 0009). Revisions and purges mark dependent summaries stale;
`pending` and `doctor` report them.

## Procedures and feedback

After three recurrences of the same prompt shape in one project the consolidator writes a
candidate `SKILL.md` under `~/.lumen/skill-candidates/<id>/` from the verbatim episodes and
their tool records, with preconditions and expiry triggers. `lumen skills` lists candidates;
nothing installs one, and a purged episode makes its candidate stale and deleted (ADR 0010).
`lumen feedback --json '{"id": "...", "verdict": "wrong", "note": "..."}'` records an owner
verdict; `wrong` and `harmful` become `audit` findings, never retirements. `lumen pending`
reports share branches awaiting a pull request, stale derivatives, skill candidates, feedback
counts and the review-fatigue numbers computable offline.

## Importing a host's memory

`lumen import --host claude-code|codex-cli|hermes --path <file-or-directory> [--scope user|repo:<id>] [--all]`
reads the host's memory files (Claude Code auto memory under `~/.claude/projects/<slug>/memory/`,
Codex `~/.codex/memories/`, a Hermes `MEMORY.md`) as `agent-observed` `imported_note`
candidates with an `import` citation that carries the file hash and the lineage the file
records. Reruns on an unchanged file are idempotent; more than twenty candidates need `--all`;
credential and environment paths are refused; imports never share (ADR 0001).

## Recovery

`lumen why <event-id>` returns structured events and references in both directions
under the current grant, including revisions linking successors to predecessors.
A bounded or unresolved chain reports `complete: false`; lineage
does not prove that the source supports the assertion.

`lumen audit` lists known shared assertions with missing or 90-day-old source
verification. Every citation must have a recent matching receipt. It never retires
or deletes facts. `lumen audit --ci` exits 0 only when `.lumen/config.toml` commits
`share.pull_request = "pending"`, the team's acknowledgement that no forge review authority
exists yet; otherwise it exits 1 because the full CODEOWNERS
and conflict acceptance checks are pending. Receipts created before timestamp
tracking are conservatively treated as unverified.

`export --json -` accepts `{"destination":"absolute/path/backup.json"}`.
`journal-checkpoint` returns content-free deletion state for independent backup.
`purge --json -` accepts `{"eid":"event-id"}` and erases dependent records too.

`restore` requires an event bundle, the latest independent journal checkpoint,
and its digest obtained through a trusted owner channel. A matching hash proves
identity, not freshness: the operator must establish that it is the latest
checkpoint. Backup timestamps do not establish this. Missing proof refuses
restore. Other developers' copies, Git history, filesystem snapshots and context
already delivered are outside local erasure guarantees.

## Verification and release status

```powershell
python tools/offline-tests.py
.venv/Scripts/lumen bench --suite release --manifest evals/release-1.json --clients evals/clients.lock.json --max-cost-usd 0
```

The first command runs component tests with Python IP networking disabled.
The release command currently exits 1 because mandatory acceptance is missing.
Reports include runtime/configuration hashes, revisions, commands and outcomes.
No mocked client or fabricated pilot can establish release acceptance.

Known limits include bounded evidence, one enrolled workspace per daemon
home, incomplete catalog discovery/owner-review integration, unqualified end-to-end
scale, incomplete client hooks, and no real pilot or full comparison results.
The alpha should use an explicit allowlisted store while these are addressed.

New CLI enrollments bind the selected project to the transport grant. The daemon
requires its matching workspace policy and reloads project membership and dependency
relationships before memory operations. Removing a project or relationship revokes
access without restarting. Added relationships do not expand the original grant;
they require renewed enrollment. Older enrollments without a project anchor retain
their explicit scope ceiling and receive project-membership revocation only.
The owner can still purge data from a removed project.

With a workspace configured, recall and owner reindex inspect `.lumen/shared/`
within the granted folders. Events must have existing local destination approvals
(`share inspect`, then source-checked `share approve`). The
daemon admits approved events before retrieval and rechecks after restart.
Missing or invalid review returns `index_pending`; it never grants approval.
Human review batches stay limited to 100 events. A checkout can combine separately
reviewed batches up to 10,000 events and 64 MiB. New source proofs bind each member's
ID, scope and exact event digest, so adding another approved batch does not revoke
earlier reviews. Older proofs without membership support still require an exact
bundle match or renewed review. CODEOWNERS/PR authority remains incomplete.

For a new workspace, repeat `lumen init --owner` for each explicit `@user`,
`@org/team` or email owner. Without those identities, local initialization succeeds
and reports CODEOWNERS routing as pending. Existing workspaces use the manifest's
owners. `lumen codeowners preview|write|check --workspace <path>` prepares, updates
or checks routing; check exits 1 on drift. Generation preserves existing entries
and maintains a final marked block in the first existing CODEOWNERS location
(`.github/`, root, then `docs/`), or creates `.github/CODEOWNERS`.
Shared folders route to their project owners; monorepo files, workspace intent and
CODEOWNERS files route to the union of configured owners. Generation verifies syntax
and drift only. It does not verify hosted identities, review or branch protection.

`lumen audit --ci` runs read-only checks through the owner daemon: CODEOWNERS drift,
permitted shared-file integrity and local approval, secret patterns, and source-byte
verification using each review's snapshot mode. Output separates passed, failed and
pending checks. It does not import files or approve them. CI still exits 1 while
repository-review authority and full conflict/entailment acceptance remain pending.

Shared controls require their complete referenced assertions before approval or
checkout admission. Missing targets return `index_pending`. Reconciliation can
select an assertion only within that assertion's applicable region. Projection
version 2 invalidates earlier state tokens; recall again before submitting a revision.

Local transport bounds mutual authentication before accepting a request. An
unresponsive authentication peer is disconnected after five seconds. The client
keeps a bounded authentication wait, including time queued behind another peer.
This preserves the standard multiprocessing challenge protocol. Connection setup,
authenticated large-frame writes and Unix platform qualification remain hardening
work; this timeout is not complete denial-of-service acceptance.
