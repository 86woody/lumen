# Native hook boundary

**Superseded 2026-09-12.** The owner decided Python only. The Go launcher is removed;
its behaviour is ported line for line into `src/lumen/hook.py`, launched as
`python -I -m lumen.hook` with the same flags, payload identification, blocked paths,
outermost-root walk, 8 KiB output bound and four-second endpoint deadline. The
no-workspace exit measured cold 29.9 ms, warm p95 26.4 ms, against a bare
interpreter baseline of 12.9 ms (`docs/validation/python-hook-2026-09-12.json`);
the Go launcher's own cold launches were 148 to 186 ms, recorded below. The
under-10 ms target is withdrawn from the plan; R-14's 300 ms SessionStart p95 is
the gate. The delivery outcome below was established with the Go launcher.
Re-established 2026-09-12 with the Python launcher (hook.py sha256 84437e78,
wheel 2073c8ad) on the pinned Claude Code 2.1.268 from an installed venv: the
frozen driver passed, raw evidence in docs/validation/native-hook-delivery-2026-09-12/.

Use a standard-library-only native launcher for the no-workspace path. It parses a
bounded host envelope, resolves the outermost Lumen workspace, rejects prohibited
paths, and starts the installed Python endpoint only for a recognized SessionStart.
Python runs isolated (`-I`), with explicit argv and a four-second process deadline.
Output is bounded before accumulation. No shell, transcript read or model call is
part of this launcher. The endpoint uses the unprivileged daemon channel and binds
the requested workspace/project to its current grant.

Initial platform implementation: Go 1.26.4, CGO disabled, local toolchain only,
module download and telemetry disabled. Build dependency inspection permits only
standard packages plus this source directory. Other hosts/platforms remain pending.
No user settings are installed automatically. Capture-at-stop is a separate pending
adapter; a successful hint is not capture, fallback or duplicate-capture acceptance.

Freeze the actual Claude delivery experiment in tools/claude-hook-check.py before
execution. It uses one native Claude 2.1.268 session, a synthetic enrolled workspace
launched from a child directory, and explicit exec-form SessionStart settings. The
question does not instruct recall. Success requires actual recall with the correct
value/citation plus native hook_response evidence containing the delivered hint.
Only local four-tool MCP access and the current subscription are authorized.

The documented hook schema and exec-form argument behavior were checked against
[Claude's hook reference](https://code.claude.com/docs/en/hooks). Actual installed
execution, not this reference or the Go contract tests, establishes delivery.

First local measurements failed the strict no-workspace latency target: first
process launches were 148.240 and 186.3882 ms; warm p95 was 8.0427 and 8.5284 ms.
These are first launches in each measured batch, not proof that OS caches were cold.
All raw samples are retained. The cause of slow first launches is unproven and the
under-10-ms gate remains unpassed. Do not disable security controls to improve it.

Outcome, 2026-09-11 20:37 UTC: the frozen experiment passed once on installed
Claude Code 2.1.268 with launcher f09fba15 and runtime 195cd618; raw evidence is in
docs/validation/native-hook-delivery-2026-09-11/. Delivery is established for that
configuration only. The fallback and duplicate-hook cases need their own protocols.

