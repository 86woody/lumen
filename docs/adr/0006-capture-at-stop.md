# Capture at stop

Status: implemented for Claude Code on 2026-09-11 after the owner's go; the outcome
section at the end records what the installed client showed. The daemon contract is
host-independent and the Codex and Copilot adapters reuse it.

## Goal and non-goals

Every finished turn in an enrolled workspace becomes one durable, redacted, cited
`episode` event in the local ledger without a model call, and a host that runs the
same hook twice produces one episode. This is plan write-path step 2 and the R-10
duplicate-hook case. Not in this slice: tool outputs, subagent turns, the
consolidator, resident promotion, recall over episodes, per-session JSONL export
files, and any host other than Claude Code.

## Host facts the design rests on

From the current [hooks reference](https://code.claude.com/docs/en/hooks) and the
vault page on Claude Code hooks:

- Every hook receives `session_id`, `prompt_id`, `transcript_path`, `cwd` and
  `hook_event_name`; `agent_id` is present only inside a subagent. `prompt_id`
  identifies the user prompt and requires v2.1.196 or later (pinned client 2.1.268).
- `Stop` adds `last_assistant_message` and `stop_hook_active`. The transcript file
  is written asynchronously and may not contain the current turn at Stop time; the
  docs say to use `last_assistant_message` for the final text. `Stop` does not fire
  after a user interrupt; API errors fire `StopFailure`.
- `UserPromptSubmit` adds `user_prompt`, times out at 30 seconds, and its stdout is
  injected as context, so the capture hook must print nothing.
- The same handler in more than one settings file runs once; a plugin's copy runs
  separately. VS Code and Copilot CLI read Claude's settings files, with payloads of
  their own shape.
- Exec form (`command` plus `args`) runs without a shell; on Windows the command
  must be a real `.exe`. Stop's default timeout is 600 seconds; ours is 5.

## Flow

Two thin launches per turn through the launcher (Go `lumen-hook.exe` until
2026-09-12, since then `python -I -m lumen.hook`; ADR 0004):

1. `UserPromptSubmit` -> `hook-prompt`: stores the redacted prompt as a pending
   half-turn keyed by (workspace, checkout, host, session, prompt_id).
2. `Stop` -> `hook-stop`: commits one `episode` joining the pending prompt with the
   redacted `last_assistant_message`, or with `user: null` and `state: prompt_missing`
   when no pending prompt exists for that prompt_id.

The transcript is never read. It lags by documentation, and the two documented
payload fields carry exactly the user-visible turn. If a session ends without a
Stop for a pending prompt (interrupt, crash), the next capture in that session or
`lumen doctor` flushes it as an episode with `assistant: null, state: interrupted`.

### Launcher rules (standard library only; Python since 2026-09-12)

- Identify the host from the payload, never from the `--host` flag alone: accept
  only `hook_event_name` in {SessionStart, UserPromptSubmit, Stop}, a `session_id`
  matching the existing character rule, no `agent_id`, and a `transcript_path`
  under the Claude projects directory. Any other shape exits 0 with no output, so a
  VS Code or Copilot invocation of the same settings file captures nothing here.
- Read at most 256 KiB from stdin; pass the payload to Python on stdin, never argv.
- Same isolation as SessionStart: `python -I`, explicit `--home`, resolved workspace
  root, 4-second process deadline, exit 0 always. Exit 2 is never used; capture must
  never block the host from stopping.
- No-workspace exit stays under the existing 10 ms target.

### Daemon operations (agent channel)

`capture_prompt` and `capture_stop`, authorized like `session_start`: the workspace
must equal the enrolled one and the resolved project must be inside the grant.

Identity key: `digest([workspace, checkout, host, session, prompt_id])`, the same
`captures` idempotency table `remember` uses. Same key and same request digest
returns the existing event id; same key with different content fails with
`capture_conflict` and increments a counter that `doctor` reports. Doubled hooks
deliver byte-identical payloads, so the ledger holds one episode. A `prompt_id`
absent from the payload is a refused capture, counted, never guessed.

Event kind `episode` (new, closed schema): common envelope plus `host`, `session`,
`turn` (the prompt_id), `project`, `user`, `assistant`, `state` in
{paired, prompt_missing, interrupted}, and `bytes` recording the untruncated length
and sha256 of each text before truncation. Text is redacted before any write, as
`remember` does today, and `_validate_write` still rejects a residual secret.

Bounds (R-12): payload 256 KiB, each text field 64 KiB after which the text is cut
and `bytes.truncated` is true, pending prompts 1,000 per workspace, existing
`max_queue` for indexing. Pending prompts live in a ledger table inside the same
transaction discipline as captures, so a process death at any transition is
retried or replayed exactly as R-01 requires.

Episodes are local only: sharing refuses to propose them (extending the existing
"raw episodes cannot cross" rule), purge removes them and the deletion journal
blocks re-capture of an erased key, export and restore carry them, and `expand`
and `why` return them by id. They are not lexically indexed for `recall` in this
slice; the consolidator is the consumer.

### Installation

`lumen hooks preview --host claude-code` prints the exact user-level settings JSON:
three exec-form handlers (SessionStart, UserPromptSubmit, Stop) naming the audited
launcher path, the installed interpreter and the daemon home, each with a
5-second timeout. `lumen hooks write` merges only those handlers into
`~/.claude/settings.json` after writing a timestamped backup beside it, and refuses
when an unrelated Lumen handler is already present. Preview is the default;
nothing in a user's home changes without the explicit write action. The preview
also prints the ADR 0005 note about approving the AGENTS.md import once when
launching from a subdirectory.

## Frozen native protocol: tools/claude-capture-check.py

Synthetic enrolled workspace, launched from its root, pinned Claude 2.1.268, four
local tools only, current subscription observation under one hour old. Sessions:

1. `capture`: the three hooks registered once through `--settings`; a neutral prompt
   that needs no tool call and contains a synthetic secret `sk-lumenfixture...`.
   Pass requires: exactly one episode for that session in the ledger, `state`
   paired, `user` equal to the prompt with the secret replaced by `[REDACTED]`,
   `assistant` equal to the native stream's final result text, the native stream
   showing one Stop `hook_response` with exit 0 and empty stdout, and zero raw
   secret bytes in every file under the daemon home.
2. `doubled`: the same handlers registered twice as distinct entries (two matcher
   groups) so both deliver. Pass requires two Stop `hook_response` records in the
   stream and still exactly one episode for the prompt_id.
3. `restart-expand`: not a model session; an owner `expand` of the episode id from a
   fresh process returns the same bytes, and `doctor` reports zero conflicts and
   zero pending prompts.

The first run also verifies that `prompt_id` is identical in the UserPromptSubmit
and Stop payloads of one turn. If it is not, the design falls back to pairing by
session order and the ADR is amended before any acceptance claim.

`test_claude_code_duplicate_hooks` runs this driver through the gate exactly as
the hook-disabled case does, binding runtime, driver, configuration and locked
client versions.

## Offline tests before any native run

- Go: payload identification, subagent and foreign-host payloads exit silently,
  stdin bound, event names outside the set ignored.
- Python: same key and content once, same key and different content conflicts;
  pending flush on interrupt; redaction before disk with actual process death at
  each capture transition; text and payload bounds; purge and journal block;
  sharing refusal; closed-schema and MCP schema updates; `hooks preview` output
  is stable and `hooks write` refuses without the explicit action.

## The one command and its gate

```
python -m lumen bench --suite cross-agent --clients evals/clients.lock.json --max-cost-usd 0 --native-config artifacts/local/subscription-checks/native-config.json --native-output artifacts/local/native-gate-attempt3 --output artifacts/local/native-gate-attempt3.json
```

Falsifiable gate for this slice: `test_claude_code_duplicate_hooks` reports
`passed` with two Stop deliveries and one episode, `python tools/offline-tests.py`
exits 0, and a fresh clean install passes with unchanged inputs. The suite still
exits 1 until the Codex and Copilot cases exist; that is correct.

## Cost and authorization

Three to four native Claude sessions per attempt on the existing Max subscription
with usage credits off, USD 0 additional. No Codex or Copilot inference. Only
synthetic fixtures; no real transcript, settings file or home directory is read or
written by the tests.

## Decisions needed from the owner

1. Two hooks per turn (chosen here) versus Stop-only with transcript backfill.
2. Episodes stay out of `recall` until the consolidator exists (plan reading).
3. `lumen hooks write` may edit `~/.claude/settings.json` with a backup; never
   automatically.

## Outcome, 2026-09-13, per-session JSONL export

Built: `~/.lumen/episodes/<host>/<session>.jsonl`, one canonical JSON line per
episode event in ledger order, a projection and never a source of truth. The
session file is rewritten after every capture commit, the whole tree is rebuilt by
`reindex`, and purge drops erased lines and empty files inside the same startup
reconciliation that finishes a killed purge. Host and session ids pass through a
path-component filter; unsafe values are replaced and hash-suffixed. `doctor`
reports files, lines and stale lines, and is unhealthy when a line names an event
the ledger no longer has. Four tests; suite 91 tests, exit 0. Recall over episodes
and the consolidator remain Phase 4.

## Outcome, 2026-09-12 20:30 UTC, Python launcher

The frozen capture driver passed again with the Python launcher (hook.py sha256
84437e78, wheel 2073c8ad): capture and doubled-hook cases, runtime, hook and drivers
unchanged across the run. Evidence in docs/validation/native-capture-2026-09-12/.

## Outcome, 2026-09-12 02:14 UTC

Built as designed: `episode` event kind, `capture_prompt`/`capture_stop`/`flush_session`
daemon operations on the agent channel, `hook-prompt`/`hook-stop` CLI endpoints reading
the payload from stdin, the launcher extended to identify UserPromptSubmit and Stop
payloads, `lumen hooks preview|write|check`, seven offline tests (store, service, real
daemon, process death at every capture transition, settings management) and Go
launcher tests. The full offline suite passed 80 tests.

The frozen driver passed on attempt 3 (docs/validation/native-capture-2026-09-11/):
one episode per session, both paired, user text equal to the prompt with the synthetic
token replaced by `[REDACTED]`, assistant text equal to the native result, one Stop
delivery in the single session and two in the doubled session, all exit 0 with empty
stdout, and no token bytes in any of the five files under the daemon home. A fresh
process expanded both episodes. `prompt_id` was identical across the two hooks of a
turn, which the paired state proves.

Two facts the docs did not give: the pinned client 2.1.268 sends the submitted text
as `prompt`, not the documented `user_prompt`, so the endpoint accepts either
(attempt 2 recorded two `prompt_missing` episodes before the fix); and a live daemon
reports `index_pending` from `doctor` until the outbox drains, so the driver checks
the capture counters live and health only after a restart and reindex. Attempt 1 was a
preflight refusal on a stale account observation and never reached inference.

## Copilot CLI, 2026-09-13

Built from the observations in `docs/validation/copilot-hooks-observation-2026-09-13/`.
Copilot hands `agentStop` a transcript path and no final message, so this is the one host
where capture reads a transcript: `copilot_turn` opens `session-state/<id>/events.jsonl`,
reads at most the last 1 MiB, interprets only `user.message` and `assistant.message`
content, and returns the last finished assistant message with the user message before it.
The assistant message id is the turn key, so a doubled `agentStop` yields one episode; a
tail without a user message captures as `prompt_missing`. `capture_stop` accepts the user
text directly for this path. Session start prints the plain `{"additionalContext"}` shape,
the only one Copilot injects in `-p` mode. The launcher takes the event from `--event`
because Copilot payloads carry no event name, and rejects PascalCase or Claude-shaped
payloads on the Copilot branch. `lumen hooks --host copilot-cli` owns one user-level file,
`hooks/lumen-memory.json`, with `powershell` and `bash` command keys and a 5-second
timeout; `COPILOT_HOME` overrides its location. Four tests; suite 95 tests, exit 0. The
frozen native delivery, capture and duplicate drivers for Copilot remain to be run.

## Outcome, 2026-09-13, Copilot CLI native

Both frozen Copilot drivers passed on the pinned 1.0.83 client from an installed venv
(wheel fea4506c, hook.py unchanged across each run); evidence in
`docs/validation/copilot-hook-delivery-2026-09-13/` and `copilot-capture-2026-09-13/`,
attempts retained beside them with their causes. Two facts learned: Copilot's transcript
records one hook start per event however many entries ran, so a doubled delivery is
evidenced by the ledger's new `duplicate_deliveries` counter (`capture_status`), and the
isolated home must be an absolute path. Copilot is now delivered, captured and
dedupe-proven natively; its instruction-file fallback case remains, as for Codex.
