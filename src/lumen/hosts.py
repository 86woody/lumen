"""Host hook settings: preview by default; a user settings file changes only on explicit write."""
import json
import os
from pathlib import Path
import time

from .model import require

EVENTS = {"SessionStart": "startup", "UserPromptSubmit": None, "Stop": None}
COPILOT_EVENTS = ("sessionStart", "agentStop")
MARKER = "lumen-memory"
TRANSCRIPT_TAIL = 1024 * 1024
IMPORT_NOTE = ("A Claude Code session launched from a subdirectory treats the root CLAUDE.md import of "
               "AGENTS.md as an external import and asks for a one-time approval per project before the "
               "Lumen fallback line is delivered; approve it once interactively or launch from the root.")


def claude_handlers(python, home):
    """Exec-form handlers that run the Python launcher `lumen.hook` in isolated mode."""
    for path in (python, home):
        require(Path(path).is_absolute(), "Hook paths must be absolute")
    handlers = {}
    for event, matcher in EVENTS.items():
        hook = {"type": "command", "command": str(python), "timeout": 5,
                "args": ["-I", "-m", "lumen.hook", "--host", "claude-code", "--python", str(python),
                         "--home", str(home), "--marker", MARKER]}
        group = {"hooks": [hook]}
        if matcher:
            group["matcher"] = matcher
        handlers[event] = [group]
    return handlers


def copilot_hooks_path():
    """The user-level hook file Lumen owns; COPILOT_HOME overrides the default home."""
    return Path(os.environ.get("COPILOT_HOME") or Path.home() / ".copilot") / "hooks" / "lumen-memory.json"


def _quoted(value):
    return '"' + str(value).replace('"', '') + '"'


def copilot_handlers(python, home):
    """Copilot hook file: sessionStart delivers the hint, agentStop captures from the transcript."""
    for path in (python, home):
        require(Path(path).is_absolute(), "Hook paths must be absolute")

    def entry(event):
        arguments = ["-I", "-m", "lumen.hook", "--host", "copilot-cli", "--event", event,
                     "--python", str(python), "--home", str(home), "--marker", MARKER]
        command = _quoted(python) + " " + " ".join(_quoted(a) for a in arguments)
        return {"type": "command", "powershell": "& " + command, "bash": command, "timeoutSec": 5}
    return {"version": 1, "hooks": {event: [entry(event)] for event in COPILOT_EVENTS}}


def manage_copilot_hooks(action, path, handlers):
    """Lumen owns this whole file, so there is nothing to merge; write only on explicit action."""
    require(action in ("preview", "check", "write"), "Unknown hook action")
    path = Path(path)
    require(not path.is_symlink(), "Linked hook files are not managed")
    old = path.read_bytes() if path.exists() else None
    require(old is None or len(old) <= 1024 * 1024, "Hook file exceeds byte bound", "budget_exhausted")
    content = (json.dumps(handlers, indent=2) + "\n").encode()
    result = {"path": str(path), "changed": old != content, "exists": old is not None, "owned_file": True}
    if action == "preview":
        return {**result, "generated": handlers}
    if action == "check":
        return {**result, "healthy": old == content}
    return _replace_file(path, old, content, result)


def copilot_turn(path):
    """The finishing turn of a Copilot transcript: the last assistant message and the user message before it.

    Reads at most the last TRANSCRIPT_TAIL bytes and only the two message kinds; nothing else in the
    transcript is interpreted. Returns None when no finished assistant message is present.
    """
    if not isinstance(path, str) or not os.path.isabs(path) or not path.endswith(".jsonl") \
            or "session-state" not in path.replace("\\", "/").split("/"):
        return None
    try:
        with open(path, "rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - TRANSCRIPT_TAIL))
            data = stream.read()
    except OSError:
        return None
    lines = data.split(b"\n")
    if size > TRANSCRIPT_TAIL:
        lines = lines[1:]
    user, assistant = None, None
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or not isinstance(event.get("data"), dict):
            continue
        kind, body = event.get("type"), event["data"]
        if kind == "user.message" and isinstance(body.get("content"), str):
            user, assistant = body["content"], None
        elif kind == "assistant.message" and isinstance(body.get("content"), str) and body["content"] \
                and isinstance(body.get("messageId"), str) and 0 < len(body["messageId"]) <= 128:
            assistant = {"id": body["messageId"], "assistant": body["content"]}
    if assistant is None:
        return None
    return {**assistant, "user": user}


def _is_lumen(group):
    return any(isinstance(h, dict) and MARKER in (h.get("args") or []) for h in group.get("hooks", []))


def manage_hooks(action, settings_path, handlers):
    require(action in ("preview", "check", "write"), "Unknown hook action")
    settings_path = Path(settings_path)
    require(not settings_path.is_symlink(), "Linked settings files are not managed")
    old = settings_path.read_bytes() if settings_path.exists() else None
    require(old is None or len(old) <= 4 * 1024 * 1024, "Settings file exceeds byte bound", "budget_exhausted")
    current = json.loads(old.decode("utf-8-sig")) if old else {}
    require(isinstance(current, dict), "Settings file is not an object")
    hooks = current.get("hooks", {})
    require(isinstance(hooks, dict), "Settings hooks must be an object")
    merged, foreign = json.loads(json.dumps(hooks)), []
    for event, groups in handlers.items():
        existing = merged.get(event, [])
        require(isinstance(existing, list), "Hook event must be a list")
        kept = [g for g in existing if not (isinstance(g, dict) and _is_lumen(g))]
        foreign.extend(event for g in existing if isinstance(g, dict) and _is_lumen(g) and g not in groups)
        merged[event] = kept + groups
    proposed = {**current, "hooks": merged}
    content = (json.dumps(proposed, indent=2) + "\n").encode()
    result = {"path": str(settings_path), "changed": old != content, "exists": old is not None,
              "replaced_lumen_entries": sorted(set(foreign)), "note": IMPORT_NOTE}
    if action == "preview":
        return {**result, "generated": {"hooks": handlers}}
    if action == "check":
        return {**result, "healthy": old == content}
    return _replace_file(settings_path, old, content, result)


def _replace_file(settings_path, old, content, result):
    if old == content:
        return {**result, "backup": None}
    backup = None
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    if old is not None:
        backup = settings_path.with_name(settings_path.name + "." + time.strftime("%Y%m%dT%H%M%S") + ".lumen-backup")
        require(not backup.exists(), "Backup already exists; retry")
        backup.write_bytes(old)
    temporary = settings_path.with_name(settings_path.name + ".lumen-tmp")
    with temporary.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        require((settings_path.read_bytes() if settings_path.exists() else None) == old,
                "Settings changed during generation; retry", "revision_conflict")
        os.replace(temporary, settings_path)
    finally:
        temporary.unlink(missing_ok=True)
    return {**result, "backup": str(backup) if backup else None}
