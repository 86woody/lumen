# Requirement-to-test matrix

Candidate: initial offline slice. Full requirement status remains pending until
all named acceptance conditions pass. A passing component test is partial evidence.

| Requirement | Status | Executable partial evidence | Missing acceptance |
|---|---|---|---|
| R-01 | pending | test_core.StoreTests.test_fault_transitions_retry; test_recovery.RecoveryTests.test_actual_process_death_at_capture_transitions; test_multiple_real_processes_capture_once | daemon, full outbox/large-payload transitions, all OSes |
| R-02 | pending | test_bounded_correction_and_recurrence; test_durable_retry_index_and_known_at | topology integration and historical version migration |
| R-03 | pending | test_bounded_correction_and_recurrence | topology integration |
| R-04 | pending | test_partial_platform_branch_and_path; test_policy.RelationVocabularyTests.test_declared_dimensions_widen_matching_and_change_the_state_token | undeclared/multi and malformed-region adversarial expansion |
| R-05 | pending | test_shuffled_forks_and_semantic_token; test_pending_and_cycles_and_cross_scope | shared import and reconciliation, concurrent CAS |
| R-06 | pending | test_workspace.WorkspaceTests.test_three_topologies_move_retains_id_and_citations | rehash citations after move; expanded discovery |
| R-07 | pending | none | sparse/uninitialized topology coverage |
| R-08 | pending | test_isolation_expand_and_fts_query_escaping; test_workspace.WorkspaceTests.test_two_home_review_requires_authority_and_preserves_origin; test_share_flow.ShareFlowTests (four tests: branches per repo batch, review and erasure refusal, configured-pending authority, silent competing successors) | forge push, pull request and owner-review evidence (ADR 0008); full lineage integration |
| R-09 | pending | test_backup_rollback_and_stale_capture_never_resurrect; test_actual_process_death_after_deletion_commit; test_migration (older index schema rebuilt, older ledger opens, restore reproduces answers, tokens, tool records and import lineage) | all derivative/export crash paths, fresh-machine owner checkpoint workflow |
| R-10 | pending | none | six actual installed-client handoffs |
| R-11 | pending | test_security.SecurityTests.test_untrusted_text_cannot_promote_or_claim_user_origin; test_service.ServiceTests.test_real_daemon_pipe_and_authority; test_security_attacks (six tests: secret shapes, forged user statements, tool-result laundering, poisoned shares, false citations and tracked comments, multistep promotion); test_consolidation tool-derived origin; test_import channel refusals | behavioural poisoning through retrieval on installed hosts; resident contamination once a resident block exists |
| R-12 | pending | test_recall_envelope_is_bounded_and_truncation_is_honest; test_bounds_and_fixed_origin_parameters (queue); test_policy.LoudFailureAndBoundTests.test_index_retries_are_counted_bounded_and_never_drop_events (retry); test_policy.SessionStartSliceTests.test_slice_stays_under_the_token_bound_with_a_large_workspace (tokens); test_hook.HookLauncherTests.test_output_bound (hook) | consumer-tokenizer measurement on an installed host; over-cap resident write |
| R-13 | pending | test_failed_index_does_not_lose_ledger; test_policy.LoudFailureAndBoundTests.test_missing_fts5_is_an_installation_failure; test_corrupt_relation_and_index_structures_fail_doctor; test_policy.TeamPolicyTests.test_policy_governs_the_store_and_a_pinned_version_mismatch_fails_doctor; test_redaction_before_disk_and_no_configuration_escape | unavailable-daemon case through an installed host; Linux and macOS execution |
| R-14 | pending | docs/validation/scale-2026-09-13.json (100k records, 20 scopes, Windows x64 unverified hardware): direct-core recall p50 2 ms / p95 3.3 ms; four-client IPC recall through the daemon p50 319 ms / p95 412 ms; session-start endpoint p95 188 ms; installed launcher session start p95 221 ms; test_retrieval.ScaleToolTests | verified 8-core/16 GB/SSD hardware; SessionStart through an installed host; optional text generation measured separately |
| R-15 | passed | test_workspace.WorkspaceTests.test_receipts_do_not_dirty_checkout | none in tested Windows configuration |
| R-16 | pending | none | real pilot week, all first-turn hints and evidence binding |
| R-17 | pending | test_security.SecurityTests.test_disabled_retirement_disputes_and_explicit_revision_works; test_consolidation.ConsolidatorTests.test_disabled_judge_disputes_competing_values_and_retires_nothing; test_same_value_adds_evidence_and_recurrence_is_a_new_assertion; test_consolidation.ExtractionTests.test_statements_match_only_the_declared_vocabulary | enabled mode: two judge configurations on frozen acceptance data (inference) |
| R-18 | pending | test_security.SecurityTests.test_closed_import_and_event_schemas; tools/offline-tests.py; tools/inventory.py; clean installed 28-test run | broaden adapter audit as remaining adapters are implemented |

Run partial evidence with `python -m unittest discover -s tests -v` after setting
`PYTHONPATH=src`. This command does not constitute either release gate.

## Expanded recovery evidence

R-01 additionally covers `test_large_evidence_process_faults_and_erasure`.
R-09 additionally covers `test_export_process_death_remains_managed` and
`test_purge_export_rewrite_process_death`: actual process exits after durable
registration, temporary fsync and replacement, restart cleanup, retry and absence
of erased content. Managed export registration now precedes file creation. Purge
rewrites use the same durable job protocol. These tests do not establish external
backup freshness or all supported OS acceptance; R-01 and R-09 remain pending.

## Workspace coverage additions

`test_sparse_and_uninitialized_coverage` constructs actual local Git submodules,
enables sparse checkout or deinitializes the producer, and checks service packets
list the producer as skipped/unavailable while retaining current consumer coverage.
Unavailable roots cannot produce fresh source receipts. The three-topology move
test now compares source hashes through service recall after moving the producer.
R-06 and R-07 retain pending status for broader platform acceptance.

## Source exclusion evidence

R-11 additionally includes `test_manifest_exclusion_blocks_source_before_read`.
It exercises service recall with a manifest-excluded file and intercepts attempted
content reads. Source policy is supplied by the server workspace. Literal path
prefix semantics, case-insensitive denial, and nonmatching sibling filenames are
checked. Alias/reparse-point races and wider adversarial coverage remain pending;
this component test does not qualify the full R-11 security gate.

## Shared immutable file evidence

`test_shared_files.SharedFileTests` covers 100 unique files through 16 workers,
100 same-ID retries, conflicting IDs, filename/digest/canonical format checks.
Staging now follows the scope-directory Markdown layout. Full concurrent approval,
PR authority, restart admission and managed staged-file erasure are still pending;
R-05/R-08 are not passed by these file-layer tests.


Shared inspection component evidence: `test_inspection_checks_folder_and_batch_without_admission`
and `test_real_daemon_pipe_and_authority` verify bounded owner inspection, scope
folder matching, digest round trips and no implicit admission. R-08 remains pending
for complete reviewed checkout ingestion and PR authority evidence.


Managed staging recovery: `test_staged_process_death_and_purge` uses real process
termination after registration, fsync and linking. Startup removes registered
temporaries, and tombstones remove managed staged events. The two-home workspace
test additionally verifies ordinary purge removes the staged copy. R-09 remains
pending for broader recovery/platform and independent-journal requirements.


Inspection/audit component evidence: `test_inspection.InspectionTests` checks
structured outgoing provenance, grant filtering, purge-safe inspection, all-citation
verification, 90-day staleness and no automatic retirement. CLI `why` and `audit`
are implemented; `audit --ci` intentionally remains unsuccessful until authority
and conflict validation are complete. Reverse lineage and full shared audit coverage
still require acceptance work; no full requirement is promoted by these tests.


R-05 concurrency evidence: `test_concurrent_revision_token_has_one_winner` starts
eight independent Store processes, waits until all are open, then releases their
writes against the same state token. Exactly one revision must commit and seven
must return revision_conflict; no losing successor may remain in the ledger.
This supplements, rather than replaces, shared-proposal/reconciliation acceptance.


Reverse provenance: `test_successor_lineage_includes_predecessor_and_rebuilds`
verifies successor-to-revision-to-predecessor inspection, upgrade backfill, explicit
reindex and deletion of derived links. Traversal remains grant-filtered and bounded;
truncated chains report incomplete. Full hostile shared-import/admission coverage
remains pending.


R-12 byte-envelope evidence: `test_recall_envelope_is_bounded_and_truncation_is_honest`
checks an oversized result reports incomplete and the entire canonical service
response fits a 512-byte cap. The budget counter includes the envelope itself.
Configurations below 512 bytes are rejected. Client-tokenizer/MCP wrapping and
other operation bounds remain separate pending acceptance requirements.


MCP framing: `test_mcp_requires_initialization_exchange_and_discards_oversized_line`
checks negotiation ordering, malformed notification rejection, input-line resync
and oversized output errors. Stubbed oversized output verifies a protocol contract,
not product support. R-10/R-12 remain pending installed-client and tokenizer gates.


Source review component: `test_source_review_reports_bytes_without_approval`
checks matching/changed/excluded file bytes, no implicit approval and tombstone
rejection through the service. Historical revisions, entailment and mandatory
sharing admission policy remain pending; matching hashes are not those proofs.


Historical source component: `test_changed_checkout_can_verify_original_commit`
uses actual local commits, verifies raw blob identity after a checkout change,
and rejects branch/option/missing-object revisions and excluded paths. Windows
worktree newline reconstruction and old logical-project paths remain pending.
No full historical acceptance is inferred from this narrower blob check.


Source-checked approval: the shared-file source-review test now verifies that a
changed source blocks approval, while a restored permitted source allows schema-2
approval. Real daemon tests exercise source-bound approval/staging. Old receipts
require renewed source review. Full reviewed-Git authority and policy revision
propagation remain pending R-08/R-11 acceptance.


Review evidence integrity: `test_review_evidence_corruption_blocks_staging_and_import`
rejects missing, malformed, non-object, mismatched and oversized proof records before
staged output/admission. Restoring exact proof bytes permits import. This is partial
R-08/R-11 evidence, not full destination authority acceptance.


Workspace policy binding: source proofs include the manifest digest. Staging and
import reload/validate policy and reject mismatched approvals. The source-review
service test edits an exclusion on disk after approval and verifies both operations
fail without output. Full reviewed-Git and client acceptance remains pending.

Dynamic grant revocation: `test_removed_project_revokes_cached_service_grant` and
`test_removed_dependency_revokes_related_project_without_restart` change policy
on disk and verify expansion, lineage and lexical retrieval enforce the reduced
grant. Owner erasure remains available. New CLI enrollments persist the selected
project; the real named-pipe test exercises that enrollment. Legacy enrollments
without this anchor cannot establish dependency-revocation acceptance. Full
multi-workspace enrollment and reviewed-checkout acceptance remain pending.

Shared-checkout component: `test_checkout_recall_requires_review_and_rechecks_after_restart`
verifies destination-review gating, owner-only reindex, automatic recall admission,
idempotent repeated checks, and proof corruption after Store restart. The daemon
checks the permitted shared tree before recall; unreviewed content returns
`index_pending`. This covers a bounded local review bundle, not CODEOWNERS/PR
authority, independently reviewed multi-batch checkout admission, or a qualified
end-to-end deadline. R-08 remains pending.

Multi-batch follow-up: `test_independent_review_batches_over_100_and_erasure`
admits 101 events from two independent destination approvals, verifies idempotent
sync and altered-content rejection, erases one managed event while preserving the
other 100, and rejects stale-file resurrection. The human inspection limit remains
100. This supersedes the single-bundle implementation limit above; CODEOWNERS/PR
authority, large-checkout latency and all-topology propagation remain pending.

CODEOWNERS routing component: `test_init_routes_explicit_owners_and_check_detects_drift`
executes the CLI with explicit owners, verifies generated routing and a failing
check after manifest drift. `test_preserves_existing_rules_and_rejects_unsafe_routing`
checks exact preservation, idempotence, later override rejection and owner-string
injection rejection. Eight targeted CODEOWNERS/service/release-gate tests passed.
Hosted owner identity, authenticated PR approval and full audit CI remain pending;
generated routing is not review evidence.

Read-only audit component: `test_ci_checks_review_routing_and_sources_without_admission`
checks valid local review/routing/source bytes, changed source bytes, routing drift,
corrupt review and agent denial. The ledger watermark remains unchanged and the
shared event is not admitted. The real daemon test verifies the CLI returns detailed
CI checks and exit 1. Repository review authority and complete conflict/entailment
acceptance remain explicitly pending; this is partial R-08/R-11 evidence.

Reference and reconciliation validation: `test_missing_control_targets_cannot_be_approved_or_scanned`
rejects a reviewed bundle containing a missing dispute target before writing any
approval; checkout scanning also returns index_pending. `test_reconciliation_cannot_select_inapplicable_alternative`
rejects platform/start/end overreach and permits a bounded selection. Projection
version 2 binds the stricter semantics into state tokens. Full historical migration
and repository-review authority remain pending; these cases do not complete R-03/R-08.

Deep graph validation: `test_deep_revision_history_and_cycle_without_recursion`
checks a 1,500-assertion revision chain, reversed delivery and a closing cycle.
`test_deep_reference_closure_propagates_pending_and_rejects_cycle` checks 1,500
dependent control records, complete propagation of a missing base and a reference
cycle. The latter is a graph-validator test, not approval-admission acceptance.
Traversal uses an explicit stack; no interpreter recursion limit is increased.

Acceptance-input binding: `test_changes_additions_and_removals_invalidate_evidence`
checks that changing, adding or removing a test invalidates the recorded input
snapshot. The installer compares snapshots before/after execution; packaging
requires the same current snapshot and explicit checks unaffected by Python -O.
This is evidence integrity, not completion of the pending product release gate.

Catalog latency follow-up: the existing actual-topology, security, audit and service
tests pass with conditional submodule-helper execution. Five interleaved catalog
measurements are recorded in `validation/catalog-performance-2026-09-11.md`, with
frozen protocol and raw runtime-bound samples. This does not satisfy R-14's complete
IPC/client/100k workload or replace the initial pilot's recorded slow recalls.

Authentication timeout: the real daemon service test opens a raw named-pipe client,
reads its challenge without answering, and verifies a queued legitimate request
is served after the stalled peer expires. It also verifies wrong-key rejection and
continued daemon health. The updated client successfully called the existing pilot
daemon, demonstrating local protocol compatibility. Unix partial-frame tests,
connection/write deadlines and sustained-flood fairness remain unqualified.

Installed-client evidence gate: `test_client_status_labels_and_versionless_results_cannot_pass`
sets lock labels to passed, then supplies missing or versionless test results; each
client remains pending. An executed failure remains failed. The fixed six ordered
handoffs and each host's two hook cases must execute at locked versions. The actual
host adapters and artifacts remain pending; this test only validates the gate.

Malformed IPC envelope follow-up: `test_real_daemon_pipe_and_authority` sends
null, scalar, array, deeply nested JSON, non-ASCII caller tokens, non-string
operations and non-object arguments over an authenticated real connection.
Every rejected connection closes and a subsequent doctor request succeeds.
The regression failed against the preceding runtime (daemon unavailable after
null), then passed with explicit envelope validation and recursion-error handling.
This is Windows transport evidence; Unix execution and sustained-flood fairness
remain pending.

Actual single-client evidence: `tools/claude-client-check.py` at 97c1812 passed on
Claude Code 2.1.268 with the installed catalog-candidate wheel. Real native calls
captured a synthetic fact through MCP and recalled its correct value/citation in a
new client session. Versioned raw output: `validation/claude-explicit-2026-09-11/`.
This is partial R-10 evidence, not six handoffs, hook fallback or full acceptance.

Installed Claude correction/erasure: the unchanged b406a99 launcher failed against
the older opaque MCP region schema, then passed against installed runtime e1ac32ef
(source 8670420). Five native sessions verify one explicit capture, restart recall,
one bounded correction, restart recall of the corrected value, and abstention after
owner erasure. Independent local reads verify known_at history and absent erased
lineage. Both outcomes are versioned under `validation/claude-correction-*-2026-09-11/`.
This does not complete cross-client/hook R-10 or the real pilot R-16.

Actual ordered-pair evidence: native driver 2a7195d passed Claude Code 2.1.268 to
Codex CLI 0.154.0 and the reverse direction, with installed runtime e1ac32ef.
Each protocol exercised six fresh native sessions, one capture, explicit correction,
source-cited recall, independent historical lookup and owner erasure followed by
both clients abstaining. Raw successful and failed attempts are preserved in
`validation/native-handoffs-2026-09-11/`. These are two development-protocol passes;
four other pairs, hooks, fixed manifest integration and full R-10 remain pending.

`test_mcp_process_survives_deep_json_and_malformed_envelopes` starts a real MCP
process, sends malformed frames interleaved with pings, and checks that malformed
initialization plus an initialized notification cannot authorize tools/list.
The source fix delays handshake state changes until the response is constructed
and catches decoder recursion exhaustion. This newer runtime change is not covered
by the preceding installed-client artifacts; those remain bound to their wheel.

CLI scope audit against the specification: `feedback`, native host-memory `import`,
and `skills` commands are not yet implemented. Reviewed `share import` covers a
narrower operation and does not satisfy native host-memory import. The standalone
`repos --workspace` interface now reports repository identities and availability;
the actual topology test invokes it after project moves in all three layouts.
Native memory import must retain file/lineage evidence and default
to candidate admission; it must never inspect credential stores or bulk import
without the required explicit flag. Automatic procedure generation remains subject
to its disabled-feature gate. These gaps remain in the full completion scope.

Native gate integration now has twelve named executable cases in `acceptance/`.
Two call the real six-session native protocol; ten explicitly skip as unimplemented.
Without explicit native configuration all twelve remain pending and no inference is
attempted. `test_native_acceptance_requires_explicit_configuration_and_client_lock`
verifies this boundary and rejects a missing client lock before execution. This is
gate contract evidence, not new host acceptance. The previously passed handoffs
remain separate from the newer runtime and native-gate implementation.

Native SessionStart delivery: the frozen `tools/claude-hook-check.py` at d0fa386
passed on installed Claude Code 2.1.268 with the current wheel (runtime 195cd618)
and launcher f09fba15. The native stream records the hook_response carrying the
Lumen hint, and the host recalled the synthetic value with its file citation
without being told to. Versioned raw output: `validation/native-hook-delivery-2026-09-11/`.
This is partial R-10 and R-16 first-turn-hint evidence for one host and one case;
`test_claude_code_hook_disabled` and `test_claude_code_duplicate_hooks` remain
unimplemented and pending, as do capture-at-stop and the other two hosts.

Instruction-file fallback: `test_instructions.InstructionTests` (three tests) covers
the fenced `AGENTS.md` section, untouched `CLAUDE.md`, drift detection and the CLI.
The frozen native driver passed once on installed Claude Code 2.1.268 with runtime
2f251532 (attempt 6): with every hook disabled the host quoted the fenced line, then
recalled the synthetic value with its citation, zero hook events. Attempts 1 to 5
are retained as negative or superseded evidence in `validation/native-fallback-2026-09-11/`;
ADR 0005 records the restricted-mode and child-directory limitations. This is
partial R-10 hook-disabled evidence for Claude only; Codex/Copilot fallback,
duplicate hooks and capture-at-stop remain pending. The control session shows the
MCP server instructions alone also prompt recall, so the instruction file is not
proven necessary, only delivered.

Capture at stop: `test_capture` (seven tests) covers paired/redacted episodes,
idempotent doubled stops, counted conflicts, missing and interrupted prompts, text
bounds, purge and journal blocking, sharing refusal, export/restore, actual process
death at each capture transition, service authorization and the real-daemon hook
endpoints; Go tests cover payload identification. The frozen native driver passed on
installed Claude Code 2.1.268 at attempt 3 with runtime f60c32d1: one episode per
session with hooks registered once and twice, redaction proven on disk, and a fresh
process expanding each episode. Raw attempts are in `validation/native-capture-2026-09-11/`.
This is partial R-10 duplicate-hook and R-01/R-12 capture evidence for Claude only;
Codex and Copilot hook cases, the six-pair completion and the pilot remain pending.

Copilot CLI pairs, 2026-09-13: `claude-code -> copilot-cli` and `copilot-cli -> claude-code`
passed through the frozen handoff driver on the pinned 1.0.83 client with an isolated
home and a 30-credit cap (`validation/native-handoff-2026-09-13/`). R-10 now has one
host through all four pairs it can reach today; the four Codex pairs wait for the
weekly reserve reset, and every hook case for Codex and Copilot remains pending.

Copilot CLI hook cases, 2026-09-13: session-start delivery, capture at stop with the doubled
hook, and the instruction-file fallback all passed through frozen drivers on the pinned
1.0.83 client (`validation/copilot-hook-delivery-2026-09-13/`, `copilot-capture-2026-09-13/`,
`copilot-fallback-2026-09-13/`). With the two Claude pairs, Copilot lacks only its Codex
pairs for R-10. Copilot's transcript records one hook start per event, so the doubled case
is evidenced by the ledger's `duplicate_deliveries` counter.

Team files, slice and scopes, 2026-09-13 (ADR 0007): `test_policy` (twelve tests) covers the
strict `relations.yaml` subset, dimension widening in projection and the state token, reload
without restart, `config.toml` caps that only lower and trust flags that cannot be enabled,
the pinned-version doctor failure, the session-start slice with its 2,000-token estimate on a
120-project workspace, `user` facts that sharing refuses, and `session:<id>` working state
visible only to its own session. `test_daemon_unix` (seven tests) drives the AF_UNIX framing,
the flock branch and the endpoint through callables and a proxied `os` on Windows; it is not
Linux or macOS execution. Both CI workflows are written and unexecuted, so `three-platforms`
stays pending. The session-start delivery changed, so both frozen delivery drivers were
rerun on 2026-09-14 from the installed venv on the current wheel and passed: Claude Code
2.1.268 (`validation/native-hook-delivery-2026-09-14/`) and Copilot CLI 1.0.83
(`validation/copilot-hook-delivery-2026-09-14/`), each with the hook output ending in the
slice line and a cited `memory_recall` answer. The fixture holds one project, so only the
`This repo` line of the slice reached a host; the other lines rest on `test_policy`.

Share branch flow, 2026-09-13 (ADR 0008): `test_share_flow` (four tests) runs against a real
repository: five reviewed events under `batch_limit = 2` become three `lumen/share/producer/*`
branches from `HEAD`, the checkout's branch, status and tree are unchanged, the worktree is
removed, the branches are reused on repeat, `pending` lists them, an unreviewed bundle or an
erased event refuses before any branch exists. `audit --ci` exits 0 only with
`share.pull_request = "pending"` committed, fails on `"forge"`, and the new
`conflicts_and_type_specific_evidence` check fails a staged fork without a `dispute` event and
passes once one is shared. No push, pull request or forge review evidence exists; R-08 stays
pending on those.

Host-memory import, 2026-09-13 (ADR 0001 section): `test_import` (five tests) parses the
Claude auto-memory index and topic files with front matter, Codex memory files and a Hermes
`MEMORY.md`, keeps the file hash, heading, index topic and front-matter lineage on every
`import` citation, refuses credential, environment, archive, vault-staging and non-Markdown
paths before reading, bounds file size, redacts secrets before the anchor is built, imports
idempotently, marks a changed file stale on recall, refuses the agent channel and forged
import citations, refuses sharing, and gates bulk runs behind `--all`. This is the Phase 3
"lineage-preserving import" deliverable; it adds partial R-10 (command surface) and R-11
(origin and channel) evidence and promotes nothing.

Consolidation, 2026-09-13 (ADR 0009): `test_consolidation` (six tests) runs the deterministic
consolidator over captured episodes. Disabled mode: a competing single-valued fact yields a new
assertion plus an explicit `dispute`, zero revisions, the explicit revision retires and a
reconciliation resolves; same value adds evidence, a retired-then-restated value is a third
assertion; tool outputs are redacted, hashed, truncated at 4 KiB and yield `tool-derived` facts
that sharing refuses; a captured user line is `agent-observed`, never `user-stated`; runs are
idempotent, interval-gated and atomic per episode with the extraction recorded; revisions and
purges mark dependent summaries stale and the next run regenerates them. This is R-17's disabled
fallback tested as a complete configuration, plus partial R-01 and R-11 evidence. Enabled
retirement, a model extractor and judge calibration stay pending on inference.

Retrieval and scale, 2026-09-13: `test_retrieval` (seven tests) covers identifier variants, one
alias expansion pass with ambiguity refused and grant-bound, the one-hop relation join reached
through evidence citation text, span-overlap demotion, and scope widening ranked repo, monorepo,
dependency, user through the enrolled service, plus a 600-record run of the measurement tool.
`tools/measure-scale.py 100000 --ipc --clients 4 --launcher-python .venv/Scripts/python.exe`
recorded `docs/validation/scale-2026-09-13.json` on the development workstation (Windows x64, 16 logical CPUs,
RAM and media unverified): ingest 65 s, direct-core recall p95 3.3 ms, four
client processes over the named pipe p95 412 ms (p50 319 ms, 100 recalls, 8 s wall),
the session-start endpoint p95 188 ms and the installed stdlib launcher end to end p95 221 ms.
The three plan thresholds (200 ms, 1 s, 300 ms) hold in this measurement, but the hardware is
unverified and no installed host is on the path, so R-14 stays pending. The textual planner and
the full-evidence router need inference and remain disabled and pending.

Hardening, 2026-09-13 (ADR 0010): `test_security_attacks` (six tests) covers twelve secret
token shapes with benign prose untouched, forged user statements on every channel, tool-result
laundering, poisoned shared files (wrong folder, unreviewed, self-approving, secret, episode
payload, import citation, tampered bytes, admitted without rewriting origin), false citations and
malicious tracked comments as inert evidence, and multistep promotion with every owner
operation refusing the agent channel. `test_migration` (three tests) rebuilds an older index
schema, opens an older ledger without the new tables, and restores a bundle that reproduces
answers, state tokens, tool records and import lineage while refusing a vector citation. Skill
candidates are distilled after three recurrences and never installed
(`test_consolidation` and `test_security_attacks` assert no skills directory is written).
The decay study, review-time metrics and behavioural attacks through installed hosts need
the pilot with inference and the forge; they remain pending.
