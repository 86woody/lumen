# 0009: The consolidator, the disabled judge and tool-derived origin

Accepted 2026-09-13. Phase 4 offline; closes audit item 13. Additional spend USD 0.

## What runs

`lumen consolidate` and the daemon after every acknowledged `capture_stop` run
`Consolidator.run`: only when new episodes exist past `consolidated_seq` and, unless
forced, at least sixty seconds after the last run; at most 200 episodes per run;
each episode in one ledger transaction together with its extraction record, so a
crash repeats nothing and loses nothing. Episodes outside the grant or erased are
skipped and never re-read.

**Extraction is deterministic and vocabulary-bound.** No model runs. A line of the
user text, the assistant text or a captured tool output that reads
`<subject> <relation>: <value>` or `<subject>.<relation> = <value>` with `relation`
declared in the workspace's relation vocabulary (ADR 0007) becomes a candidate; any
other prose is ignored. The candidate takes the episode's scope (plan §4: scope
from the files cited; an episode cites no files, so its project scope applies), an
episode citation naming the host, session, turn and the line, and a validity start
at the episode's recorded time. The extractor id, judge decision and produced ids
are stored per episode (`extractions`), which is what the plan asks of a model
extractor's output and prompt ids; when a model extractor exists it records its
model and prompt there and the deterministic one keeps running first.

**Supersession, deterministic checks only.** Same value on a currently projected
assertion with overlapping applicability: an `evidence` event. No current value:
a new assertion. Same value after the earlier one was retired: a new assertion, so
A, B, A stays three events. A different value on a `multi` relation: a new
assertion, nothing retired. A different value on a `single` relation: the judge.

**The judge is disabled.** `automatic_retirement` is `false` in every accepted
configuration (ADR 0001, `config.toml` cannot enable it), so `judge_for` returns
`DisabledJudge`, whose only verdict is `dispute`. The consolidator then writes the
new assertion and an explicit `dispute` event over the competing alternatives; it
never writes a revision, so it retires nothing. The dispute stands until an
explicit `reconciliation` selects an alternative or an owner revision retires one
and reconciles, which R-17 disabled mode requires to keep working. An enabled
judge needs inference and the frozen acceptance data of R-17; it is pending, not
skipped, and `judge_for` refuses an enabled configuration.

**Origin.** Facts extracted from the episode's user text are `agent-observed`, not
`user-stated`: the capture endpoints run on the agent channel, so a hostile MCP
caller could submit a forged prompt through `capture_prompt`; only the owner's
explicit `remember` authenticates a user statement. Facts from assistant text are
`agent-observed`. Facts from tool outputs are `tool-derived`.

**Tool outputs** (plan §4 Layers, audit item 13). `capture_stop` accepts an optional
`tools` list of `{name, output}`; each output is redacted, hashed and measured
untruncated, then cut at 4 KiB, at most 32 per episode; the record is part of the
episode's capture identity, so a doubled delivery matches and a changed one
conflicts. No installed host adapter passes tool outputs yet: Claude Code's `Stop`
payload carries none and the Copilot transcript reader does not extract them, so
`tool-derived` production is fixture-exercised only. Adding either host's
extraction is a capture change that reruns the frozen drivers.

**Dependency invalidation.** Derivatives record the events they depend on. The
consolidator writes one `summary` per touched subject and relation (current values,
status, state token, dependencies); a `revise` or a purge of any dependency marks
dependent derivatives stale, `pending` and `doctor` report them, and the next run
regenerates or deletes them. Skill candidates (Phase 6) use the same table.

## Gate

`lumen bench --suite fixture` runs the offline suite including `test_consolidation`
(six tests): the disabled judge disputes and retires nothing while the explicit
revision and reconciliation still resolve; same value adds evidence and a
recurrence is a third assertion; tool outputs are truncated, hashed, redacted and
yield `tool-derived` facts that sharing refuses; runs are idempotent,
interval-gated and atomic per episode; revision and purge invalidate summaries.
R-17 enabled mode stays pending on inference.
