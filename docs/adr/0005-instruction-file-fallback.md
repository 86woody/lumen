# Instruction-file fallback boundary

`lumen instructions write` maintains one fenced section in the workspace root
`AGENTS.md` and creates `CLAUDE.md` containing only `@AGENTS.md` when absent. It never
edits an existing `CLAUDE.md`, rewrites only the bytes between its own markers, and
`check` fails when the section drifts or an existing `CLAUDE.md` lacks the import.
The section is under fifteen lines and tells the model to call `memory_recall` when
no Lumen session-start hint appeared. Retrieved text is declared advisory.

The frozen delivery experiment is `tools/claude-fallback-check.py`: three native
Claude sessions with every hook disabled. The first must quote the fenced line, which
is the only headless-observable proof that `CLAUDE.md -> @AGENTS.md -> section`
loaded. The second asks a neutral question and must recall the value with its
citation and record zero hook events. The third excludes all instruction files and
is an observation, never a gate. The launch directory is the workspace root.

Findings from the six attempts on 2026-09-11, all retained under
`docs/validation/native-fallback-2026-09-11/`:

- Restricted mode loads only managed settings and `--settings`; no project
  instruction file reached the model under it (attempts 2 and 3). The fallback case
  therefore runs unrestricted with `--tools ''`, strict local MCP, explicit settings
  and only the synthetic project as a setting source.
- Launched from a child directory, the root `CLAUDE.md` import of `AGENTS.md`
  resolves outside the launch directory. Claude Code treats it as an external import
  awaiting an interactive approval, so headless sessions receive no instruction
  content (attempt 5). Subdirectory launches need that one-time approval per project
  before the fallback line is delivered; this is a product limitation to surface in
  `lumen init` output, not something the test may bypass. Surfaced 2026-09-13:
  `init` reports the instruction check (`healthy` or `pending` with the exact
  `lumen instructions` commands) and carries the approval note in `notes`.
- Attempt 4 is invalid: a stale argument index turned the settings JSON into the
  prompt. Attempt 1 passed on non-discriminating criteria and is superseded.
- The Lumen MCP server's own instructions ("Use memory_recall before work") reach
  the model on every session where the server connects, so the control session also
  recalled. The instruction-file section is a second, host-independent channel, and
  neither channel is capture or duplicate-hook evidence.

## Copilot CLI, 2026-09-13

`tools/copilot-fallback-check.py` passed on the pinned Copilot CLI 1.0.83 from an installed
venv (`docs/validation/copilot-fallback-2026-09-13/`). Copilot reads the root `AGENTS.md`
directly, so there is no import chain and no child-directory approval. The isolated home
held no hooks directory and no personal instruction file; the transcript of every session
shows zero hook events. The loaded session quoted the fenced line, the fallback session
recalled unprompted with the correct value and citation, and the control session with
custom instructions off also recalled, because the MCP server's own instructions reach
every session; the control is recorded, never a gate. Codex's fallback case waits for its
reserve reset and uses the same protocol shape.
