"""Fail-closed configuration and source admission; no provider loader exists."""
import re
from pathlib import Path
import tomllib

from .model import LumenError, digest, require

CONFIG = {"schema": 1, "retrieval": "fts5", "automatic_retirement": False,
          "text_generation": False, "resident": False, "max_event_bytes": 262144,
          "max_queue": 10000, "max_packet_bytes": 8000, "max_results": 20}
SECRET = re.compile(
    r"(?i)(?:"
    r"\b(?:sk-[a-z0-9_-]{16,}|gh[pousr]_[a-z0-9]{16,}|github_pat_[a-z0-9_]{22,}|npm_[a-z0-9]{36}|xox[abprs]-[a-z0-9-]{10,})\b|"
    r"\bAKIA[0-9A-Z]{16}\b|\bAIza[A-Za-z0-9_-]{35}\b|"
    r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b|"
    r"\bbearer\s+[A-Za-z0-9._-]{20,}|"
    r"(?:api[_-]?key|password|passwd|access[_-]?token|client[_-]?secret|secret[_-]?key|\bsecret)\s*[:=]\s*[^\s,;]+|"
    r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----)")


def check_config(config):
    require(set(config) == set(CONFIG), "Unknown configuration capability", "unsupported_capability")
    for key in ("schema", "retrieval", "automatic_retirement", "text_generation", "resident"):
        require(config[key] == CONFIG[key] and type(config[key]) is type(CONFIG[key]),
                "Only the offline lexical configuration is supported", "unsupported_capability")
    for key in ("max_event_bytes", "max_queue", "max_packet_bytes", "max_results"):
        require(type(config[key]) is int and 0 < config[key] <= CONFIG[key], "Invalid bound")
    require(config["max_packet_bytes"] >= 512, "Packet bound must accommodate the response envelope")


SHARE_POLICY = {"batch_limit": 100, "pull_request": "unconfigured"}
PULL_REQUEST_MODES = ("unconfigured", "pending", "forge")


def default_policy():
    return {"schema": 1, "version": None, "config": dict(CONFIG), "share": dict(SHARE_POLICY),
            "source": "defaults", "digest": digest({"config": CONFIG, "share": SHARE_POLICY, "version": None})}


def render_policy(version):
    """The committed team policy `lumen init` writes: every value explicit, nothing enabled."""
    caps = "\n".join(f"{key} = {CONFIG[key]}" for key in ("max_event_bytes", "max_queue", "max_packet_bytes", "max_results"))
    header = ["# .lumen/config.toml: team policy, reviewed like code (ADR 0007).",
              "# caps may only be lowered below the runtime defaults; trust flags must stay false;",
              "# share.pull_request is unconfigured, pending (no forge authority yet, acknowledged) or forge.",
              "schema = 1", "", "[lumen]", f'version = "{version}"', "", "[caps]", caps, "",
              "[trust]", "automatic_retirement = false", "text_generation = false", "resident = false", "",
              "[share]", f"batch_limit = {SHARE_POLICY['batch_limit']}", 'pull_request = "unconfigured"']
    return "\n".join(header) + "\n"


def parse_policy(text):
    """config.toml: schema, [lumen] version, [caps], [trust], [share]; unknown keys fail closed."""
    require(isinstance(text, str) and len(text) <= 65536, "Team policy exceeds bound", "budget_exhausted")
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise LumenError("invalid_event", f"config.toml: {exc}") from None
    require(set(data) <= {"schema", "lumen", "caps", "trust", "share"} and data.get("schema") == 1,
            "config.toml: unknown table or unsupported schema", "unsupported_capability")
    lumen = data.get("lumen", {})
    require(isinstance(lumen, dict) and set(lumen) <= {"version"}, "config.toml: unknown lumen key", "unsupported_capability")
    version = lumen.get("version")
    require(version is None or (isinstance(version, str) and re.fullmatch(r"[0-9A-Za-z.+-]{1,64}", version)),
            "config.toml: invalid pinned version")
    config = dict(CONFIG)
    caps = data.get("caps", {})
    require(isinstance(caps, dict) and set(caps) <= {"max_event_bytes", "max_queue", "max_packet_bytes", "max_results"},
            "config.toml: unknown cap", "unsupported_capability")
    for key, value in caps.items():
        require(type(value) is int and 0 < value <= CONFIG[key], f"config.toml: {key} may only be lowered")
        config[key] = value
    trust = data.get("trust", {})
    require(isinstance(trust, dict) and set(trust) <= {"automatic_retirement", "text_generation", "resident"},
            "config.toml: unknown trust key", "unsupported_capability")
    for key, value in trust.items():
        require(value is False, f"config.toml: {key} cannot be enabled by policy", "unsupported_capability")
    share = dict(SHARE_POLICY)
    given = data.get("share", {})
    require(isinstance(given, dict) and set(given) <= set(SHARE_POLICY), "config.toml: unknown share key", "unsupported_capability")
    if "batch_limit" in given:
        require(type(given["batch_limit"]) is int and 0 < given["batch_limit"] <= SHARE_POLICY["batch_limit"],
                "config.toml: share.batch_limit may only be lowered")
        share["batch_limit"] = given["batch_limit"]
    if "pull_request" in given:
        require(given["pull_request"] in PULL_REQUEST_MODES, "config.toml: share.pull_request must be unconfigured, pending or forge")
        share["pull_request"] = given["pull_request"]
    check_config(config)
    return {"schema": 1, "version": version, "config": config, "share": share, "source": "config.toml",
            "digest": digest({"config": config, "share": share, "version": version})}


def redact(text):
    return SECRET.sub("[REDACTED]", text)


def safe_source(root, relative, exclusions=()):
    root = Path(root).resolve()
    p = Path(relative)
    require(not p.is_absolute() and ".." not in p.parts and ":" not in relative,
            "Source unavailable", "source_unavailable")
    def excluded(path):
        parts = tuple(part.casefold() for part in path.parts)
        return any(parts[:len(Path(item).parts)] == tuple(part.casefold() for part in Path(item).parts)
                   for item in exclusions)
    require(not excluded(p), "Source unavailable", "source_unavailable")
    blocked = {".git", ".env", ".ssh", ".aws", "credentials", "archive"}
    require(not any(part.lower() in blocked or part.lower().startswith(".env.") for part in p.parts),
            "Source unavailable", "source_unavailable")
    require("vault/.firecrawl/staging" not in p.as_posix().lower(), "Source unavailable", "source_unavailable")
    resolved = (root / p).resolve()
    require(resolved.is_relative_to(root), "Source unavailable", "source_unavailable")
    resolved_parts = resolved.relative_to(root).parts
    require(not excluded(resolved.relative_to(root)), "Source unavailable", "source_unavailable")
    require(not any(part.lower() in blocked or part.lower().startswith(".env.") for part in resolved_parts),
            "Source unavailable", "source_unavailable")
    return resolved
