"""Lumen's hook launcher: standard library only, at most one bounded endpoint launch.

Run as ``python -I -m lumen.hook --host claude-code --python <exe> --home <dir>`` with the
host's hook envelope on stdin. When no Lumen workspace resolves it exits 0 with no output
and without importing anything beyond json, os and sys. This is the Python port of the
Go launcher retired on 2026-09-12 (ADR 0004).
"""
import json
import os
import sys

ENDPOINTS = {"SessionStart": "hook-session", "UserPromptSubmit": "hook-prompt", "Stop": "hook-stop"}
COPILOT_ENDPOINTS = {"sessionStart": "hook-session", "agentStop": "hook-stop"}
MAX_INPUT = 262144
MAX_OUTPUT = 8192
DEADLINE_SECONDS = 4
BLOCKED_PARTS = {"archive", ".git", ".ssh", ".aws", "credentials"}
IDENTIFIER_EXTRA = "-_:."


def blocked(path):
    lowered = path.lower().replace("\\", "/")
    for part in lowered.split("/"):
        if part in BLOCKED_PARTS or part.startswith(".env"):
            return True
    return "vault/.firecrawl/staging" in lowered


def workspace(start):
    """Outermost directory holding .lumen/workspace.json above the resolved start, or ("", "")."""
    if not os.path.isabs(start) or blocked(start):
        return "", ""
    try:
        resolved = os.path.realpath(start, strict=True)
    except OSError:
        return "", ""
    if blocked(resolved) or not os.path.isdir(resolved):
        return "", ""
    found, path = "", resolved
    for _ in range(256):
        marker = os.path.join(path, ".lumen", "workspace.json")
        try:
            if (os.lstat(marker).st_mode & 0o170000) == 0o100000:
                found = path
        except OSError:
            pass
        parent = os.path.dirname(path)
        if parent == path:
            return found, resolved
        path = parent
    return "", ""


def identifier(value):
    return (isinstance(value, str) and 0 < len(value) <= 128
            and all(c.isascii() and (c.isalnum() or c in IDENTIFIER_EXTRA) for c in value))


def transcript_path_ok(path):
    """A Copilot transcript: absolute, JSONL, inside a session-state directory."""
    return (isinstance(path, str) and os.path.isabs(path) and path.endswith(".jsonl")
            and "session-state" in path.replace("\\", "/").split("/"))


def dispatch(data, host, launch, event=""):
    """Identify the host from the payload shape, never from the flag alone; launch at most once."""
    if not isinstance(data, bytes) or len(data) > MAX_INPUT:
        return
    if host == "claude-code":
        _dispatch_claude(data, launch)
    elif host == "copilot-cli":
        _dispatch_copilot(data, event, launch)


def _dispatch_copilot(data, event, launch):
    """Copilot names the event in its configuration, so the flag selects it; the shape must be camelCase."""
    endpoint = COPILOT_ENDPOINTS.get(event)
    if endpoint is None:
        return
    try:
        payload = json.loads(data)
    except ValueError:
        return
    if not isinstance(payload, dict) or "hook_event_name" in payload or "session_id" in payload:
        return
    if not identifier(payload.get("sessionId")) or not isinstance(payload.get("cwd"), str):
        return
    if event == "sessionStart":
        if payload.get("source") not in ("startup", "resume", "new"):
            return
    elif not transcript_path_ok(payload.get("transcriptPath")) or not isinstance(payload.get("stopReason"), str):
        return
    root, cwd = workspace(payload["cwd"])
    if root:
        launch(endpoint, root, cwd, payload["sessionId"], data)


def _dispatch_claude(data, launch):
    try:
        event = json.loads(data)
    except ValueError:
        return
    if not isinstance(event, dict) or not identifier(event.get("session_id")):
        return
    name = event.get("hook_event_name")
    endpoint = ENDPOINTS.get(name) if isinstance(name, str) else None
    if endpoint is None:
        return
    if name != "SessionStart":
        transcript = event.get("transcript_path")
        if (event.get("agent_id") not in (None, "") or not identifier(event.get("prompt_id"))
                or not isinstance(transcript, str) or not os.path.isabs(transcript)
                or not transcript.endswith(".jsonl")):
            return
    start = event.get("cwd")
    if not isinstance(start, str):
        return
    root, cwd = workspace(start)
    if root:
        launch(endpoint, root, cwd, event["session_id"], data)


def deliver(endpoint, returncode, output):
    """Only a successful session-start hint within the bound is forwarded; capture prints nothing."""
    if returncode == 0 and endpoint == "hook-session" and len(output) <= MAX_OUTPUT:
        return output
    return b""


def launcher(python, home, host):
    def launch(endpoint, root, cwd, session, raw):
        import subprocess
        command = [python, "-I", "-m", "lumen", "--home", home, endpoint, "--workspace", root,
                   "--cwd", cwd, "--session", session, "--host", host]
        try:
            result = subprocess.run(command, cwd=cwd, stdin=subprocess.DEVNULL if endpoint == "hook-session" else None,
                                    input=None if endpoint == "hook-session" else raw,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=DEADLINE_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            return
        forwarded = deliver(endpoint, result.returncode, result.stdout)
        if forwarded:
            sys.stdout.buffer.write(forwarded)
            sys.stdout.buffer.flush()
    return launch


def parse(argv):
    options, index = {}, 0
    while index < len(argv):
        item = argv[index]
        if item.startswith("-") and "=" in item:
            name, value = item.lstrip("-").split("=", 1)
            index += 1
        elif item.startswith("-") and index + 1 < len(argv):
            name, value = item.lstrip("-"), argv[index + 1]
            index += 2
        else:
            return {}
        options[name] = value
    return options


def main(argv=None):
    options = parse(sys.argv[1:] if argv is None else argv)
    python, home, host = options.get("python", ""), options.get("home", ""), options.get("host", "")
    if not os.path.isabs(python) or not os.path.isabs(home):
        return 0
    data = sys.stdin.buffer.read(MAX_INPUT + 1)
    dispatch(data, host, launcher(python, home, host), options.get("event", ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
