# 0010: Hardening, procedures and feedback (Phase 6, offline parts)

Accepted 2026-09-13. Additional spend USD 0.

## Attack fixtures

`tests/test_security_attacks.py` runs the plan's attack list (§4 Security) against the
built surfaces and is part of `bench --suite poison`:

- **Forged user statements.** A prompt captured on the agent channel that claims
  authority consolidates to `agent-observed`; an agent's `remember` with an `origin`
  argument, a user-stated label in the payload, or a revision successor never gains
  `user-stated`; nothing an agent wrote can be proposed for sharing; the session-start
  context carries only the hint and the catalog slice.
- **Tool-result laundering.** Tool output that quotes "the user said" or carries
  instructions consolidates to `tool-derived` with an episode citation, is refused by
  sharing, and reaches no other channel; `promote` and `approve` are unknown operations.
- **Poisoned shares.** A user-scope event in a repo folder fails the folder check; an
  unreviewed file returns `index_pending` and is never admitted; a self-approving
  control record, a secret, an episode payload or an import citation fails review; a
  tampered file fails the canonical check; a reviewed admission keeps the origin the file
  claims and rewrites nothing, and its text never reaches the session-start context.
- **False citations and malicious tracked comments.** A mismatching hash marks the
  packet stale, a citation outside the workspace stays unresolved, a tracked comment's
  words are evidence text with `support: unverified` and grant nothing.
- **Multistep promotion.** Every owner operation (approve, branch, import, consolidate,
  pending, feedback, skills, restore, purge, export, reindex, checkpoint, audit) refuses
  the agent channel, so no sequence of agent calls creates an approval, a share branch, a
  skill candidate or a resident entry; session working state stays in its session.

A finite passing set is not universal proof; behavioural success through retrieval on an
installed host is not measured here.

## Secret patterns

The detector now covers AWS access keys, Slack tokens, GitHub classic and fine-grained
tokens, Google API keys, JWTs, bearer tokens, npm tokens, OpenAI and Anthropic keys,
`secret`, `password`, `api_key` and `access_token` assignments and PEM private keys.
Patterns stay incomplete by nature; the guarantees come from tiers, approvals and review.

## Skill candidates

The consolidator distils a candidate when the same normalised prompt opening (first eight
word tokens) recurs three times in one project: `~/.lumen/skill-candidates/<id>/SKILL.md`
with front matter, preconditions (project, prompt shape), expiry triggers (a cited episode
purged or its facts revised, the workflow no longer recurring) and the verbatim episodes
with their captured tool records. It is a `skill` derivative depending on those episodes:
a purge marks it stale and the next run deletes the file. `lumen skills` lists candidates
and says how to install one by hand; no command installs, and the test asserts that no
workspace skill directory is written.

## Feedback

`lumen feedback --json {"id", "verdict": helpful|wrong|stale|harmful, "note"}` records the
owner's verdict in a local table; `wrong` and `harmful` become `audit` findings, never
automatic retirements. `lumen pending` reports feedback counts and the review-fatigue
metrics the pilot needs (proposals and events proposed in the last seven days); acceptance,
correction and rejection rates and median review time need the forge's pull-request
timestamps (ADR 0008) and are reported as pending.

## Pending

The decay study and the review-fatigue thresholds need a pilot with inference on installed
hosts. Migration tests cover an older index schema, an older ledger without the new tables,
and a restore that reproduces answers, tokens, tool records and import lineage.
