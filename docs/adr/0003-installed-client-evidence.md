# Installed-client acceptance inputs

Status: gate implemented; actual host adapters/executions pending.

The client lock records editions and qualification status; its `acceptance` string
cannot establish execution. The gate requires six ordered pair tests across Claude
Code, Codex CLI and Copilot CLI, plus each host's disabled-hook fallback and
duplicate-hook case. Each client therefore depends on four pair cases and its two
hook cases. Names are fixed by `client_acceptance_cases`, not supplied by a lock file.

Successful installed tests report `installed_client_versions` in their recorded
result. Every involved host must match the locked version. Missing/versionless
results stay pending; actual failed tests remain failed. The existing qualification
flag must also be passed. No actual host test implementation is supplied by this ADR.

This metadata contract is necessary, not proof that a test used a real host.
Acceptance still requires review of the frozen adapter source and actual supported
client execution artifacts. Mocks must remain contract tests and must not claim
installed-client observations. No inference may run until supported subscription
coverage and zero additional spending are established; missing access never permits
substitution with a paid API or an embedding-backed service.

## Copilot CLI adapter, 2026-09-13

The pinned Copilot CLI 1.0.83 runs headless as `copilot -p` with `--output-format json`.
Isolation: `COPILOT_HOME` pointed at an empty directory under the evidence folder (no
user hooks, MCP config, instructions or plugins reach the session; the stored login is
in the system credential store and survives the override), built-in MCP servers off,
custom instructions off, only the Lumen server passed inline with permission for its
tools, every file and shell tool excluded, `--no-ask-user`, model pinned to
`claude-sonnet-5` at low effort, `--max-ai-credits 30` as a hard session cap, and a
usage file per run recorded into the run record.

Identity is weaker than for Claude Code: no non-interactive auth status exists. The
preflight requires a UI observation under an hour old showing Copilot Pro with
additional paid usage off, and the observed account equal to the login the `gh` CLI
reports. The Copilot login itself is not independently attested; the usage file after
each run shows only included-credit consumption. Parsing pins `tool.execution_start`
and `tool.execution_complete` for MCP calls, the last non-empty `assistant.message`
for the answer, and the final `result` event. The `session.mcp_servers_loaded`
snapshot can still show the server as pending; the connected status event settles it
(attempt 2 failed on that assertion before it was relaxed).

Evidence: `docs/validation/native-handoff-2026-09-13/`, both directions with Claude
Code passed, six stages each, one premium request per Copilot turn. The pairs with
Codex wait for its weekly reserve reset. Copilot hooks, fallback and duplicate-hook
cases remain unbuilt.

Machine boundary, found after those runs: Copilot syncs session data to the GitHub
account by default (`remoteExport`). The adapter now passes `--no-remote` and
`--no-remote-export`; the 2026-09-13 handoff runs predate the flags and carried only
synthetic fixtures. In headless mode with the isolated home no session-state files
were written locally, so the transcript that `agentStop` hands over has to be
observed from a real hook run before capture can be designed.
