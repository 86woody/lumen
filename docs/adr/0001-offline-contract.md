# 0001: Offline event core and authority

Accepted 2026-09-11 against Lumen v5. Additional spend is USD 0.

Python 3.12+, standard library SQLite/FTS5 and Git are the runtime boundary.
No provider plugins, dynamic extensions, models, network retrieval, or optional
embedding dependencies are accepted. Automatic retirement, extraction, textual
reformulation, resident facts and procedure generation start disabled.

Canonical JSON uses UTF-8, sorted keys, compact separators, no NaN and no floats.
SHA-256 covers all event fields except digest. IDs are UUID hex strings;
references carry workspace plus ID. Recorded timestamps are UTC metadata;
local ingestion sequences determine known_at. Projection version and relation
rules participate in the complete subject/relation state token.

Field names, amended 2026-09-12 into the plan: an assertion's applicability and
validity live in one `region` object with `platform`, `branch`, `path`, `start`
and `end`; a revision's is `affected` with the same shape. These replace the plan's
earlier `applies`, `valid_from` and `valid_to`. The recall time is `at`; `as_of`
is accepted as an alias in the recall payload and rejected when both are given.
`known_at` is the local ingestion sequence, never a timestamp. `recorded_at` is an
integer UTC nanosecond timestamp on the envelope, whose `actor` replaces the plan's
`recorded_by`.

Applicability initially permits exact platform/branch names or `any`, and exact
project scope; paths are normalized relative POSIX prefixes ending in `/**` or
exact files. Arbitrary glob syntax fails closed. Half-open integer UTC-second
intervals allow null ends. Unknown starts remain unknown and are excluded from
dated answers. Point projection evaluates bounded revisions without rewriting
assertions. Interval output partitions at all event boundaries.

The local owner is the OS account. Same-account hostile processes are outside
the promise. MCP callers cannot submit authenticated user origin or approvals.
Owner CLI approvals bind destination, policy and complete event digests. Shared
import needs independently configured owner verification; caller-labelled
approval alone never authorizes admission. Implemented surfaces must use one
enforcement path. No sharing publication is authorized.

Per-user daemon transport will use Windows named pipes / Unix domain sockets,
with OS permissions and bounded messages. A process ownership lock serializes
writers. Core transactions use WAL and synchronous FULL; acknowledgement follows
commit of event, idempotency key and outbox. Rebuildable indexing has a watermark.
No TCP listener or client automation proxy is permitted.

Deletion journal is separate from the ledger. Tombstones are fsynced before
content cleanup. Backups cannot establish journal freshness by their timestamps.
Fresh-machine restore requires an owner-supplied trusted latest checkpoint;
without it restore must refuse purge-safe serving. Physical cleanup and external
copies are reported separately. Journal and restore implementation are mandatory,
not satisfied by this decision record.

Runtime naming remains provisional until public registry collision checks before
distribution. Apache-2.0 applies to original code; external datasets keep their
own licences. Real topology is currently a logical directory inside the Workspace
Git repository. Cross-platform and all three fixture topologies remain required.


### Source exclusion vocabulary

Workspace exclusions are literal workspace-relative paths, applying to the named
file/directory and its descendants. They are compared case-insensitively as a
conservative portable policy. Absolute paths, parent traversal and glob syntax
are rejected. Built-in credential/archive exclusions remain mandatory. Both the
requested path and resolved path are checked before source file content is read;
project containment and authenticated scope grants remain independent checks.
Discovery rejects excluded children before probing their Git markers. The owner
workspace manifest supplies this policy; an MCP/CLI recall payload cannot replace
source roots or the resolver. Changing policy requires reloading the workspace.


### Import citations, 2026-09-13

`lumen import` (plan §4, Phase 3) reads a host's memory files as candidates: Claude Code
auto memory (`~/.claude/projects/<slug>/memory/`, the `MEMORY.md` index plus topic files
with their front matter), Codex `~/.codex/memories/*.md` and Hermes `MEMORY.md`, as files
only. Each bullet or paragraph becomes an `agent-observed` assertion with relation
`imported_note` (multi, retires nothing) in the `user` scope or a granted repo scope, and
a third citation kind, `import`: `{kind, host, path, sha256, anchor, lineage}`, where
`lineage` is what the file records (format, file, heading, index topic file, front-matter
name, description, type, created, updated), bounded to sixteen short string fields.
Import citations resolve on this machine only (recall re-hashes the file and marks the
packet stale when it changed), sharing refuses them like episode payloads, and only the
owner channel may write them; an agent's `remember` with an import citation or an
`origin` argument is `not_authorized`. Origin is `agent-observed` because the host's
agent wrote the file; the owner may lower origin on `remember` (imports, tool output),
never raise it. Capture identity is `import` / host / file hash / item index, so a
rerun on an unchanged file is idempotent and a changed file yields new candidates
beside the old. Bulk: more than twenty candidates in one run needs `--all`. Blocked
paths (`.ssh`, `.aws`, `credentials`, `.gnupg`, `.env*`, `archive`, the vault staging
area, databases and JSON stores) are refused before any read; symlinks are not
followed. Unknown lineage gets no automatic resident or shared approval.
