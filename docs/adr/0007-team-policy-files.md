# 0007: Committed team files: relation vocabulary and policy

Accepted 2026-09-13. Closes audit items 1, 2, 3 and 10. Additional spend USD 0.

## Relation vocabulary, `.lumen/relations.yaml`

The plan names this file and shows it as YAML. The runtime has no YAML parser and
accepts no dependency for one (the reasoning of ADR 0002 for shared event files), so
the file keeps its name and is read by a strict single-line subset parser:

```yaml
# comments and blank lines are ignored
required_encoding: {cardinality: single, dimensions: [platform, branch]}
uses_library:      {cardinality: multi}
```

One relation per line, `name: {cardinality: single|multi[, dimensions: [..]]}`, where
dimensions is a subset of `platform, branch, path` in that order and defaults to all
three. Block mappings, quoting, anchors, nested keys, duplicate relations and unknown
keys are rejected with the line number. Every file the subset accepts is also valid
YAML, so a YAML reader agrees with it; the reverse does not hold and is not claimed.

Semantics: `single` retires the earlier value only where the applicability overlaps,
`multi` never retires, an undeclared relation is `multi` over every dimension (plan
§4 Data model). A relation's undeclared dimensions are widened to `any` (or `**` for
`path`) at match time, so a `test_command` declared over `[platform]` on branch
`main` also answers on `release/2`. Widening happens in projection only; structural
containment checks in the closure keep the stricter written regions. The rules dict
participates in the state token as before, so a vocabulary change invalidates every
compare-and-swap, and projection version rose from 2 to 3 to record the widening.

The built-in defaults declare the four plan relations over all three dimensions
because the R-04 acceptance test relies on `test_command` carrying a `path`
dimension; a team narrows them in the committed file. `lumen init` writes the file
with the defaults. The daemon reloads it on every governed call through
`Workspace.refresh_policy`, so an edit takes effect without a restart and a malformed
file makes those calls fail with `invalid_event` and `doctor` report
`policy_unreadable`.

## Team policy, `.lumen/config.toml`

Read with `tomllib`. Tables: `[lumen] version` (a pinned runtime version; a mismatch
fails `doctor` with `policy_version_mismatch`), `[caps]` (`max_event_bytes`,
`max_queue`, `max_packet_bytes`, `max_results`; each may only be lowered below the
runtime defaults), `[trust]` (`automatic_retirement`, `text_generation`, `resident`;
each must be `false`, so no committed file can enable inference), and `[share]`
(`batch_limit`, lower only; `pull_request` one of `unconfigured`, `pending`, `forge`).
Unknown tables or keys fail closed with `unsupported_capability`. The policy digest
covers config, share and version and is reported by `doctor`, `init` and every
session start. Caps apply to the store on every governed call; the daemon also
starts the store under them.

## Session-start slice and scopes

`session_start` returns the hint, the catalog slice for the enrolled project (this
repo, its dependencies, its dependents, a count of the rest, each entry id, purpose
and availability) and `context`, the plain-text form the hook delivers. The slice is
bounded by a host-independent estimate of one token per three UTF-8 bytes (never
fewer than the words), trimmed from the longer of the two lists with an `omitted`
count until hint plus slice fit 2,000 tokens; the CLI additionally refuses more than
6 KiB, under the launcher's 8 KiB output bound. The entry is frozen per session.

Every grant now carries `user` and `session`. `user` holds this developer's facts:
written through `remember`, recalled with everything else, refused by sharing.
`session` names a family: a call that carries a `session` identity is widened to the
one concrete scope `session:<id>`, so working state written there is recalled only
by the same session, is never exported without it, and is never shared.

## What this does not establish

The slice's token bound is an estimate, not the consumer's tokenizer; the delivery
change meant the frozen Claude Code and Copilot session-start drivers had to be rerun
before their Phase 3 evidence counted again; both passed on 2026-09-14 on the current
wheel (`docs/validation/native-hook-delivery-2026-09-14/`,
`copilot-hook-delivery-2026-09-14/`). The
policy file has no signature; review authority over it is the same CODEOWNERS and
pull-request path as the shared events (ADR 0008).
