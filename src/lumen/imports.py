"""Host-memory import: Claude Code auto memory, Codex memory files, Hermes MEMORY.md, read as files.

Every candidate is an `agent-observed` assertion in the `user` scope (or a granted repo scope)
with an `import` citation naming the host, the file, its hash, an anchor and whatever lineage
the file records. Nothing is inspected beyond the named path; credential stores and the other
blocked locations are refused before any read; bulk runs need the explicit flag.
"""
import hashlib
import os
from pathlib import Path
import re

from .model import LumenError, region, require
from .security import redact

HOSTS = {"claude-code": "claude-auto-memory", "codex-cli": "codex-memory", "hermes": "hermes-memory"}
MAX_FILE_BYTES = 1024 * 1024
MAX_FILES = 50
MAX_ITEMS_PER_FILE = 1000
BULK_THRESHOLD = 20
BLOCKED_PARTS = {"archive", ".git", ".ssh", ".aws", "credentials", ".gnupg", ".password-store"}
INDEX_LINE = re.compile(r"^\s*[-*]\s*\[(?P<title>[^\]]{1,200})\]\((?P<file>[^)]{1,200})\)\s*(?:[-:—–]+\s*(?P<note>.*))?$")


def blocked(path):
    lowered = str(path).lower().replace("\\", "/")
    parts = lowered.split("/")
    return (any(part in BLOCKED_PARTS or part.startswith(".env") for part in parts)
            or "vault/.firecrawl/staging" in lowered or lowered.endswith((".db", ".sqlite", ".json", ".keychain")))


def default_path(host):
    if host == "codex-cli":
        return Path.home() / ".codex" / "memories"
    raise LumenError("source_unavailable", f"{host} import needs an explicit --path (Claude Code: ~/.claude/projects/<slug>/memory; Hermes: the MEMORY.md)")


def memory_files(host, path):
    """The memory files under a path: one Markdown file, or the Markdown files of one directory."""
    require(host in HOSTS, "Unsupported import host", "unsupported_capability")
    path = Path(path)
    require(path.is_absolute() and not blocked(path), "Import path refused", "source_unavailable")
    require(not path.is_symlink(), "Linked import paths are not read", "source_unavailable")
    if path.is_file():
        require(path.suffix.lower() == ".md", "Memory files are Markdown", "unsupported_capability")
        files = [path]
    elif path.is_dir():
        files = sorted(p for p in path.iterdir() if p.is_file() and not p.is_symlink() and p.suffix.lower() == ".md"
                       and not blocked(p))
        require(bool(files), "No Markdown memory files at the import path", "source_unavailable")
    else:
        raise LumenError("source_unavailable", "Import path does not exist")
    require(len(files) <= MAX_FILES, "Too many memory files; import a narrower path", "budget_exhausted")
    # Claude Code's MEMORY.md is an index of topic files; read it first so its notes carry the file lineage.
    files.sort(key=lambda p: (p.name.upper() != "MEMORY.MD", p.name))
    return files


def read_memory(path):
    with open(path, "rb") as stream:
        data = stream.read(MAX_FILE_BYTES + 1)
    require(len(data) <= MAX_FILE_BYTES, f"Memory file exceeds {MAX_FILE_BYTES} bytes: {path.name}", "budget_exhausted")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise LumenError("unsupported_capability", f"Memory file is not UTF-8: {path.name}") from None
    return text, hashlib.sha256(data).hexdigest()


def items(text):
    """Bullets and paragraphs with their nearest heading; fenced blocks and front matter are skipped."""
    heading, front, fenced, paragraph, result = "", None, False, [], []
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        try:
            close = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
            front = {k.strip(): v.strip() for k, v in (l.split(":", 1) for l in lines[1:close] if ":" in l)}
            lines = lines[close + 1:]
        except StopIteration:
            pass

    def flush():
        if paragraph:
            result.append({"heading": heading, "text": " ".join(paragraph).strip(), "kind": "paragraph"})
            paragraph.clear()
    for raw in lines:
        line = raw.rstrip()
        if line.strip().startswith("```"):
            fenced = not fenced
            flush()
            continue
        if fenced:
            continue
        if not line.strip():
            flush()
            continue
        if line.lstrip().startswith("#"):
            flush()
            heading = line.lstrip("#").strip()[:200]
            continue
        stripped = line.lstrip()
        if stripped[:2] in ("- ", "* ") or re.match(r"^\d+[.)]\s", stripped):
            flush()
            body = re.sub(r"^(?:[-*]|\d+[.)])\s+", "", stripped)
            match = INDEX_LINE.match(line)
            entry = {"heading": heading, "text": body.strip(), "kind": "bullet"}
            if match:
                entry.update(kind="index", file=match.group("file"), title=match.group("title"))
            result.append(entry)
            continue
        paragraph.append(line.strip())
    flush()
    require(len(result) <= MAX_ITEMS_PER_FILE, "Memory file has too many items", "budget_exhausted")
    return [i for i in result if len(i["text"]) >= 3], front or {}


def candidates(host, path):
    """Assertion candidates for one path, each with its import citation. No store access."""
    result = []
    for file in memory_files(host, path):
        text, sha256 = read_memory(file)
        entries, front = items(text)
        for index, entry in enumerate(entries):
            # Redaction precedes every field, including the anchor; the file hash stays that of the raw file.
            entry = {**entry, "text": redact(entry["text"]), "heading": redact(entry["heading"])}
            lineage = {"format": HOSTS[host], "file": file.name, "kind": entry["kind"]}
            if entry["heading"]:
                lineage["heading"] = entry["heading"]
            if entry["kind"] == "index":
                lineage["topic_file"] = entry["file"]
            for key in ("name", "description", "type", "created", "updated"):
                if key in front:
                    lineage[key] = str(front[key])[:256]
            subject = (entry["heading"] or file.stem)[:256]
            value = entry["text"][:512]
            result.append({"subject": subject, "relation": "imported_note", "value": value, "text": entry["text"][:4096],
                           "region": region(start=0),
                           "citations": [{"kind": "import", "host": host, "path": str(file), "sha256": sha256,
                                          "anchor": entry["text"][:80], "lineage": lineage}],
                           "identity": {"checkout": "import", "host": host, "session": sha256, "turn": str(index)},
                           "file": file.name})
    return result


def import_memory(store, access, host, path=None, scope="user", all=False):
    require(access.owner, "Import requires the owner channel", "not_authorized")
    require(host in HOSTS, "Unsupported import host", "unsupported_capability")
    require(scope == "user" or scope.startswith("repo:"), "Imports land in user or a repo scope")
    require(scope in access.scopes, "Scope outside grant", "not_authorized")
    require(type(all) is bool, "Invalid bulk flag")
    path = Path(path) if path is not None else default_path(host)
    found = candidates(host, path)
    require(all or len(found) <= BULK_THRESHOLD,
            f"{len(found)} candidates exceed the {BULK_THRESHOLD}-item bulk threshold; rerun with --all to import them",
            "budget_exhausted")
    imported, already = [], []
    for candidate in found:
        fields = {k: v for k, v in candidate.items() if k not in ("identity", "file")}
        result = store.remember(access, **candidate["identity"], scope=scope, origin="agent-observed", **fields)
        (already if result.get("state") == "already_captured" else imported).append(result["id"])
    files = sorted({c["file"] for c in found})
    return {"host": host, "path": str(path), "scope": scope, "files": files, "candidates": len(found),
            "imported": imported, "already_imported": already, "origin": "agent-observed",
            "resident_approval": "none: unknown lineage gets no automatic resident or shared approval",
            "shared": False}
