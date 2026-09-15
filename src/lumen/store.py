"""Durable offline ledger. All public operations enforce the same access grant."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import time
import uuid

from .model import (Access, DEFAULT_RULES, LumenError, MAX_EPISODE_TEXT, MAX_TOOL_OUTPUTS, MAX_TOOL_TEXT, canonical,
                    check_rules, digest, episode, new_event, ref, references, require, seal, validate)
from .projection import closure, project, state
from .security import CONFIG, SECRET, check_config, redact, safe_source
from .recovery import DeletionJournal, atomic_json

MAX_INDEX_ATTEMPTS = 5
# 3: identifier variants, alias and edge tables, citation text (2026-09-13); older indexes rebuild.
INDEX_SCHEMA = "3"
MAX_VARIANTS = 32


def identifier_variants(identifier):
    """Exact-lookup forms of one identifier: itself, lowercase, camel/snake/kebab/path splits and joins."""
    variants = {identifier, identifier.lower()}
    camel = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", identifier)
    parts = [x for x in re.split(r"[\s_./:\\-]+", camel) if x]
    if len(parts) > 1:
        variants.update(x.lower() for x in parts)
        variants.add("".join(parts).lower())
        variants.add("_".join(parts).lower())
    for separator in "/\\":
        if separator in identifier:
            variants.add(identifier.rsplit(separator, 1)[-1].lower())
    return sorted(v for v in variants if v)[:MAX_VARIANTS]


def span_key(citation):
    """The source span a citation names; two results over one span are duplicates, not corroboration."""
    if citation["kind"] == "file":
        return ("file", citation["project"], citation["path"], citation["sha256"], citation["anchor"])
    if citation["kind"] == "import":
        return ("import", citation["path"], citation["anchor"])
    return ("episode", citation["sha256"])


def fts5_available():
    """A required FTS5 capability is an installation failure, never a silent fallback."""
    try:
        probe = sqlite3.connect(":memory:")
        try:
            probe.execute("CREATE VIRTUAL TABLE probe USING fts5(text, tokenize='unicode61 remove_diacritics 2')")
            probe.execute("INSERT INTO probe VALUES('lumen')")
            return probe.execute("SELECT count(*) FROM probe WHERE probe MATCH 'lumen'").fetchone()[0] == 1
        finally:
            probe.close()
    except sqlite3.Error:
        return False


def connect(path):
    db = sqlite3.connect(path, timeout=10, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.execute("PRAGMA secure_delete=ON")
    db.enable_load_extension(False)
    return db


def safe_component(value):
    """A host or session id as one path component; anything unsafe is replaced and hash-suffixed."""
    value = str(value)
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", value)[:128]
    if cleaned != value or cleaned.strip(".") == "":
        cleaned = (cleaned.strip(".") or "x")[:120] + "-" + hashlib.sha256(value.encode()).hexdigest()[:8]
    return cleaned


class Store:
    def __init__(self, home, config=None, fault=None):
        self.home = Path(home)
        self.config = dict(CONFIG if config is None else config)
        check_config(self.config)
        require(fts5_available(), "SQLite FTS5 is required and unavailable in this installation", "unsupported_capability")
        self.home.mkdir(parents=True, exist_ok=True)
        self.fault = fault or (lambda _: None)
        self.rules = dict(DEFAULT_RULES)
        self.journal = DeletionJournal(self.home)
        self.db = connect(self.home / "ledger.db")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS events(
            seq INTEGER PRIMARY KEY AUTOINCREMENT, workspace TEXT NOT NULL, id TEXT NOT NULL,
            scope TEXT NOT NULL, digest TEXT NOT NULL, body TEXT NOT NULL, admitted_ns INTEGER NOT NULL,
            UNIQUE(workspace,id));
          CREATE INDEX IF NOT EXISTS event_scope ON events(workspace,scope);
          CREATE TABLE IF NOT EXISTS captures(key TEXT PRIMARY KEY, request_digest TEXT NOT NULL,
            workspace TEXT NOT NULL, event_id TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS pending_prompts(key TEXT PRIMARY KEY, request_digest TEXT NOT NULL,
            workspace TEXT NOT NULL, checkout TEXT NOT NULL, host TEXT NOT NULL, session TEXT NOT NULL,
            turn TEXT NOT NULL, project TEXT NOT NULL, text TEXT NOT NULL, record TEXT NOT NULL,
            recorded_ns INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS capture_conflicts(key TEXT PRIMARY KEY, workspace TEXT NOT NULL,
            count INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS capture_duplicates(key TEXT PRIMARY KEY, workspace TEXT NOT NULL,
            count INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS outbox(seq INTEGER PRIMARY KEY, attempts INTEGER NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS receipts(workspace TEXT,id TEXT,source TEXT,body TEXT,
            PRIMARY KEY(workspace,id,source));
          CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS managed_exports(path TEXT PRIMARY KEY);
          CREATE TABLE IF NOT EXISTS export_jobs(path TEXT PRIMARY KEY, temporary TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS staged_files(path TEXT PRIMARY KEY, workspace TEXT, id TEXT,
            sha256 TEXT, temporary TEXT);
          CREATE TABLE IF NOT EXISTS shared_admissions(workspace TEXT,id TEXT,review TEXT,
            PRIMARY KEY(workspace,id));
          CREATE TABLE IF NOT EXISTS event_keys(workspace TEXT,id TEXT,scope TEXT,subject TEXT,relation TEXT,
            PRIMARY KEY(workspace,id));
          CREATE INDEX IF NOT EXISTS subject_keys ON event_keys(workspace,scope,subject,relation);
          CREATE TABLE IF NOT EXISTS event_refs(workspace TEXT,id TEXT,scope TEXT,target_workspace TEXT,target_id TEXT,
            PRIMARY KEY(workspace,id,target_workspace,target_id));
          CREATE INDEX IF NOT EXISTS reverse_refs ON event_refs(target_workspace,target_id,workspace,scope);
          CREATE TABLE IF NOT EXISTS extractions(episode_id TEXT PRIMARY KEY, workspace TEXT NOT NULL,
            extractor TEXT NOT NULL, model TEXT, prompt TEXT, candidates TEXT NOT NULL, produced TEXT NOT NULL,
            recorded_ns INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS derivatives(id TEXT PRIMARY KEY, workspace TEXT NOT NULL, kind TEXT NOT NULL,
            key TEXT NOT NULL, depends_on TEXT NOT NULL, body TEXT NOT NULL, stale INTEGER NOT NULL DEFAULT 0,
            updated_ns INTEGER NOT NULL);
          CREATE INDEX IF NOT EXISTS derivative_keys ON derivatives(workspace,kind,key);
          CREATE TABLE IF NOT EXISTS feedback(id TEXT PRIMARY KEY, workspace TEXT NOT NULL, event_id TEXT NOT NULL,
            verdict TEXT NOT NULL, note TEXT NOT NULL, recorded_ns INTEGER NOT NULL);
        """)
        self.index = connect(self.home / "index.db")
        self.index.executescript("""
          CREATE VIRTUAL TABLE IF NOT EXISTS lexical USING fts5(workspace UNINDEXED,
            scope UNINDEXED, event_id UNINDEXED, text, tokenize='unicode61 remove_diacritics 2');
          CREATE TABLE IF NOT EXISTS exact(workspace TEXT,scope TEXT,event_id TEXT,identifier TEXT);
          CREATE INDEX IF NOT EXISTS exact_lookup ON exact(workspace,scope,identifier);
          CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS admissions(workspace TEXT,event_id TEXT,seq INTEGER,
            PRIMARY KEY(workspace,event_id));
          CREATE TABLE IF NOT EXISTS pending(workspace TEXT,scope TEXT,event_id TEXT);
          CREATE TABLE IF NOT EXISTS aliases(workspace TEXT,scope TEXT,event_id TEXT,alias TEXT,canonical TEXT);
          CREATE INDEX IF NOT EXISTS alias_lookup ON aliases(workspace,scope,alias);
          CREATE TABLE IF NOT EXISTS edges(workspace TEXT,scope TEXT,event_id TEXT,subject TEXT,relation TEXT,value TEXT);
          CREATE INDEX IF NOT EXISTS edge_values ON edges(workspace,scope,value);
        """)
        self._reconcile_deletions()
        with self.transaction():
            if not self.db.execute("SELECT 1 FROM metadata WHERE key='reference_schema'").fetchone():
                for row in self.db.execute("SELECT body FROM events"):
                    self._index_references(self._decode(row[0]))
                self.db.execute("INSERT INTO metadata VALUES('reference_schema','1')")
        self._sweep_objects()

    def close(self):
        self.index.close()
        self.db.close()
        self.journal.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @contextlib.contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.fault("before_commit")
            self.db.execute("COMMIT")
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise
        self.fault("after_commit")

    def watermark(self):
        row = self.db.execute("SELECT seq FROM sqlite_sequence WHERE name='events'").fetchone()
        return row[0] if row else 0

    def index_watermark(self):
        version = self.index.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()
        if (version is None or version[0] != INDEX_SCHEMA) and self.watermark() > 0:
            return -1
        row = self.index.execute("SELECT value FROM metadata WHERE key='watermark'").fetchone()
        return int(row[0]) if row else 0

    def events(self, access, known_at=None):
        require(bool(access.scopes), "No grant", "not_authorized")
        marks = ",".join("?" for _ in access.scopes)
        sql = f"SELECT body FROM events WHERE workspace=? AND scope IN ({marks})"
        args = [access.workspace, *sorted(access.scopes)]
        if known_at is not None:
            require(type(known_at) is int and known_at >= 0, "known_at must be a local sequence")
            sql += " AND seq<=?"
            args.append(known_at)
        return [e for row in self.db.execute(sql + " ORDER BY id", args)
                if not self.journal.blocked((e := self._decode(row[0]))["workspace"], e["id"])]

    def _decode(self, body):
        value = json.loads(body)
        if set(value) == {"object"}:
            key = value["object"]
            require(isinstance(key, str) and re.fullmatch(r"[a-f0-9]{64}", key), "Invalid object reference")
            try:
                payload = (self.home / "objects" / key).read_bytes()
            except OSError as exc:
                raise LumenError("source_unavailable", "Evidence object unavailable") from exc
            require(hashlib.sha256(payload).hexdigest() == key, "Evidence object digest mismatch", "source_unavailable")
            return json.loads(payload)
        return value

    def _persist_body(self, event):
        payload = canonical(event)
        if len(payload) <= 16384:
            return payload.decode()
        key = hashlib.sha256(payload).hexdigest()
        directory = self.home / "objects"
        directory.mkdir(exist_ok=True)
        path = directory / key
        if not path.exists():
            self.fault("before_object_write")
            temporary = directory / (key + ".tmp")
            with temporary.open("wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
                self.fault("after_object_fsync")
            os.replace(temporary, path)
            if os.name != "nt":
                fd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            self.fault("after_object_replace")
        require(hashlib.sha256(path.read_bytes()).hexdigest() == key, "Object collision")
        return canonical({"object": key}).decode()

    def _sweep_objects(self):
        directory = self.home / "objects"
        if not directory.exists():
            return
        with self.transaction():
            referenced = {obj["object"] for row in self.db.execute("SELECT body FROM events")
                          if set(obj := json.loads(row[0])) == {"object"}}
            for path in directory.iterdir():
                if re.fullmatch(r"[a-f0-9]{64}(?:\.tmp)?", path.name) and path.name not in referenced:
                    path.unlink()

    def _insert(self, event):
        require(not self.journal.blocked(event["workspace"], event["id"]), "Erased event", "not_authorized")
        require(not any(self.journal.blocked(r["workspace"], r["id"]) for r in references(event)),
                "Erased dependency", "not_authorized")
        row = self.db.execute("SELECT digest FROM events WHERE workspace=? AND id=?",
                              (event["workspace"], event["id"])).fetchone()
        if row:
            require(row[0] == event["digest"], "Conflicting event ID")
            return
        self.fault("before_event")
        body = self._persist_body(event)
        cur = self.db.execute("INSERT INTO events(workspace,id,scope,digest,body,admitted_ns) VALUES(?,?,?,?,?,?)",
                              (event["workspace"], event["id"], event["scope"], event["digest"],
                               body, time.time_ns()))
        self.fault("after_event")
        self.db.execute("INSERT INTO outbox(seq) VALUES(?)", (cur.lastrowid,))
        self._index_key(event)
        self._index_references(event)
        self.fault("after_outbox")

    def _index_references(self, event):
        self.db.executemany("INSERT OR IGNORE INTO event_refs VALUES(?,?,?,?,?)",
            [(event["workspace"], event["id"], event["scope"], r["workspace"], r["id"]) for r in references(event)])

    def _index_key(self, event):
        if event["kind"] == "assertion":
            subject, relation = event["subject"], event["relation"]
        elif event["kind"] == "episode":
            subject, relation = event["host"] + ":" + event["session"], "episode"
        else:
            targets = references(event)
            if not targets:
                return
            row = self.db.execute("SELECT subject,relation FROM event_keys WHERE workspace=? AND id=?",
                                  (event["workspace"], targets[0]["id"])).fetchone()
            if row is None:
                return
            subject, relation = row
        self.db.execute("INSERT OR REPLACE INTO event_keys VALUES(?,?,?,?,?)",
                        (event["workspace"], event["id"], event["scope"], subject, relation))

    def key_events(self, access, scope, subject, relation, known_at=None):
        require(scope in access.scopes, "No grant", "not_authorized")
        sql = """SELECT e.body FROM event_keys k INDEXED BY subject_keys CROSS JOIN events e
                 ON e.workspace=k.workspace AND e.id=k.id AND e.scope=k.scope
                 WHERE k.workspace=? AND k.scope=? AND k.subject=? AND k.relation=?"""
        args = [access.workspace, scope, subject, relation]
        if known_at is not None:
            sql += " AND e.seq<=?"
            args.append(known_at)
        return [self._decode(row[0]) for row in self.db.execute(sql + " ORDER BY e.id", args)
                if not self.journal.blocked(access.workspace, json.loads(row[0]).get("id", ""))]

    def _validate_write(self, event, access):
        validate(event)
        require(access.permits(event), "No grant", "not_authorized")
        require(event["actor"] == access.actor, "Actor is not caller", "not_authorized")
        require(len(canonical(event)) <= self.config["max_event_bytes"], "Event exceeds bound", "budget_exhausted")
        require(not SECRET.search(canonical(event).decode()), "Unredacted secret rejected", "not_authorized")
        if event["kind"] == "approval":
            require(access.owner, "Approval requires owner channel", "not_authorized")
        if event["kind"] == "assertion" and event["origin"] == "user-stated":
            require(access.owner, "User origin requires owner channel", "not_authorized")
        if event["kind"] in ("assertion", "evidence") and any(c["kind"] == "import" for c in event["citations"]):
            require(access.owner, "Import citations come from the owner's import only", "not_authorized")
        require(self.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] < self.config["max_queue"],
                "Index queue full", "budget_exhausted")

    def remember(self, access, *, checkout, host, session, turn, scope, subject, relation,
                 value, text, region, citations=None, origin=None):
        require(all(isinstance(x, str) and x for x in (checkout, host, session, turn)), "Capture identity required")
        # Redaction precedes all persistent writes; identity logs carry only hashes.
        text, value, subject = map(redact, (text, value, subject))
        citations = citations if citations is not None else [episode(text)]
        # Origin follows the authenticated channel; the owner may only lower it (imports, tool output).
        authenticated = "user-stated" if access.owner else "agent-observed"
        require(origin is None or (access.owner and origin in ("agent-observed", "tool-derived")),
                "Origin is authenticated by transport", "not_authorized")
        fields = dict(subject=subject, relation=relation, value=value, text=text, region=region,
                      citations=citations, origin=origin or authenticated)
        request_digest = digest({"scope": scope, **fields})
        require(scope in access.scopes, "No grant", "not_authorized")
        key = digest([access.workspace, checkout, host, session, turn])
        with self.transaction():
            require(not self.journal.capture_blocked(key), "Erased capture", "not_authorized")
            prior = self.db.execute("SELECT * FROM captures WHERE key=?", (key,)).fetchone()
            if prior:
                require(prior["request_digest"] == request_digest, "Capture identity reused with different content")
                eid, state = prior["event_id"], "already_captured"
            else:
                event = new_event("assertion", access.workspace, scope, access.actor, **fields)
                self._validate_write(event, access)
                self._insert(event)
                self.db.execute("INSERT INTO captures VALUES(?,?,?,?)", (key, request_digest, access.workspace, event["id"]))
                self.fault("after_idempotency")
                eid, state = event["id"], "captured"
        return {"id": eid, "durable": True, "indexed": self.index_watermark() == self.watermark(),
                "snapshot": self.watermark(), "state": state}

    @staticmethod
    def _bounded_text(text):
        """Redact, then cut at the episode bound while recording the untruncated identity."""
        if text is None:
            return None, {"length": 0, "sha256": None, "truncated": False}
        require(isinstance(text, str), "Episode text must be text")
        text = redact(text)
        raw = text.encode()
        record = {"length": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "truncated": False}
        if len(raw) > MAX_EPISODE_TEXT:
            text, record["truncated"] = raw[:MAX_EPISODE_TEXT].decode("utf-8", "ignore"), True
        return text, record

    def _capture_identity(self, access, checkout, host, session, turn, project):
        require(all(isinstance(x, str) and 0 < len(x) <= 256 for x in (checkout, host, session, turn, project)),
                "Capture identity required")
        scope = "repo:" + project
        require(scope in access.scopes, "No grant", "not_authorized")
        return scope, digest(["episode", access.workspace, checkout, host, session, turn])

    def _conflict(self, key, workspace):
        self.db.execute("INSERT INTO capture_conflicts VALUES(?,?,1) ON CONFLICT(key) DO UPDATE SET count=count+1",
                        (key, workspace))
        self.db.execute("COMMIT")
        raise LumenError("invalid_event", "Capture identity reused with different content")

    def capture_prompt(self, access, *, checkout, host, session, turn, project, text):
        """Hold a redacted user prompt until the turn's stop arrives. Idempotent per turn."""
        scope, key = self._capture_identity(access, checkout, host, session, turn, project)
        text, record = self._bounded_text(text)
        require(bool(text), "Prompt text required")
        request_digest = digest({"scope": scope, "text": text, "record": record})
        with self.transaction():
            require(not self.journal.capture_blocked(key), "Erased capture", "not_authorized")
            captured = self.db.execute("SELECT event_id FROM captures WHERE key=?", (key,)).fetchone()
            if captured:
                return {"id": captured[0], "durable": True, "state": "already_captured"}
            prior = self.db.execute("SELECT request_digest FROM pending_prompts WHERE key=?", (key,)).fetchone()
            if prior:
                if prior[0] != request_digest:
                    self._conflict(key, access.workspace)
                return {"id": None, "durable": True, "state": "pending"}
            require(self.db.execute("SELECT COUNT(*) FROM pending_prompts WHERE workspace=?", (access.workspace,)).fetchone()[0]
                    < 1000, "Pending prompt capacity reached", "budget_exhausted")
            self.db.execute("INSERT INTO pending_prompts VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (key, request_digest, access.workspace, checkout, host, session, turn, project,
                             text, json.dumps(record), time.time_ns()))
            self.fault("after_pending_prompt")
        return {"id": None, "durable": True, "state": "pending"}

    @staticmethod
    def _bounded_tools(tools):
        """Tool outputs: redacted, cut at MAX_TOOL_TEXT, the hash and length naming the untruncated output."""
        if tools is None:
            return None
        require(isinstance(tools, list) and 0 < len(tools) <= MAX_TOOL_OUTPUTS, "Invalid tool output list")
        result = []
        for tool in tools:
            require(isinstance(tool, dict) and set(tool) == {"name", "output"} and isinstance(tool["name"], str)
                    and 0 < len(tool["name"]) <= 128 and isinstance(tool["output"], str), "Invalid tool output")
            raw = redact(tool["output"]).encode()
            record = {"name": tool["name"], "sha256": hashlib.sha256(raw).hexdigest(), "length": len(raw),
                      "truncated": len(raw) > MAX_TOOL_TEXT,
                      "text": raw[:MAX_TOOL_TEXT].decode("utf-8", "ignore") if len(raw) > MAX_TOOL_TEXT else raw.decode()}
            result.append(record)
        return result

    def capture_stop(self, access, *, checkout, host, session, turn, project, assistant, user=None, tools=None):
        """Commit one episode for the turn, joining any pending prompt; doubled deliveries return the same id.

        A host that hands over no per-prompt half turn (Copilot reads the finished turn from its
        transcript) passes the user text here instead. Tool outputs, when a host supplies them,
        are truncated and hashed (plan §4 Layers) and feed `tool-derived` consolidation.
        """
        scope, key = self._capture_identity(access, checkout, host, session, turn, project)
        assistant, assistant_record = self._bounded_text(assistant)
        require(bool(assistant), "Assistant text required")
        given_user, given_record = self._bounded_text(user)
        tools = self._bounded_tools(tools)
        request_digest = digest({"scope": scope, "assistant": assistant, "record": assistant_record, "user": given_user,
                                 **({"tools": tools} if tools else {})})
        with self.transaction():
            require(not self.journal.capture_blocked(key), "Erased capture", "not_authorized")
            prior = self.db.execute("SELECT * FROM captures WHERE key=?", (key,)).fetchone()
            if prior:
                if prior["request_digest"] != request_digest:
                    self._conflict(key, access.workspace)
                else:
                    # A doubled delivery of the same turn: counted so a host without per-entry
                    # hook records (Copilot) still leaves evidence that both deliveries arrived.
                    self.db.execute("INSERT INTO capture_duplicates VALUES(?,?,1) ON CONFLICT(key) DO UPDATE SET count=count+1",
                                    (key, access.workspace))
                return {"id": prior["event_id"], "durable": True, "state": "already_captured",
                        "indexed": self.index_watermark() == self.watermark(), "snapshot": self.watermark()}
            pending = self.db.execute("SELECT text, record FROM pending_prompts WHERE key=?", (key,)).fetchone()
            if pending:
                user, user_record = pending["text"], json.loads(pending["record"])
            elif given_user is not None:
                user, user_record = given_user, given_record
            else:
                user, user_record = None, given_record
            event = new_event("episode", access.workspace, scope, access.actor, host=host, session=session, turn=turn,
                              project=project, user=user, assistant=assistant,
                              state="paired" if user is not None else "prompt_missing",
                              bytes={"user": user_record, "assistant": assistant_record},
                              **({"tools": tools} if tools else {}))
            self._validate_write(event, access)
            self._insert(event)
            self.db.execute("INSERT INTO captures VALUES(?,?,?,?)", (key, request_digest, access.workspace, event["id"]))
            self.db.execute("DELETE FROM pending_prompts WHERE key=?", (key,))
            self.fault("after_idempotency")
            flushed = self._flush_pending(access, checkout, host, session, exclude=key)
        self._write_episode_file(host, session)
        return {"id": event["id"], "durable": True, "state": event["state"], "flushed": flushed,
                "indexed": self.index_watermark() == self.watermark(), "snapshot": self.watermark()}

    def _flush_pending(self, access, checkout, host, session, exclude=None):
        """Prompts of this session that never received a stop become interrupted episodes."""
        rows = self.db.execute("SELECT * FROM pending_prompts WHERE workspace=? AND checkout=? AND host=? AND session=? ORDER BY recorded_ns",
                               (access.workspace, checkout, host, session)).fetchall()
        flushed = []
        for row in rows:
            if row["key"] == exclude:
                continue
            scope = "repo:" + row["project"]
            if scope not in access.scopes:
                continue
            event = new_event("episode", access.workspace, scope, access.actor, host=host, session=session,
                              turn=row["turn"], project=row["project"], user=row["text"], assistant=None,
                              state="interrupted", bytes={"user": json.loads(row["record"]), "assistant": self._bounded_text(None)[1]})
            self._validate_write(event, access)
            self._insert(event)
            self.db.execute("INSERT INTO captures VALUES(?,?,?,?)", (row["key"], row["request_digest"], access.workspace, event["id"]))
            self.db.execute("DELETE FROM pending_prompts WHERE key=?", (row["key"],))
            flushed.append(event["id"])
        return flushed

    def flush_session(self, access, *, checkout, host, session):
        with self.transaction():
            flushed = self._flush_pending(access, checkout, host, session)
        if flushed:
            self._write_episode_file(host, session)
        return {"flushed": flushed, "durable": True}

    # Episode files: ~/.lumen/episodes/<host>/<session>.jsonl, a rebuildable projection of the
    # ledger's episode events, one canonical JSON line each in ledger order. Never a source of truth.
    def _episodes(self):
        return [e for e in (self._decode(r[0]) for r in self.db.execute("SELECT body FROM events ORDER BY seq"))
                if e["kind"] == "episode"]

    def _episode_path(self, host, session):
        return self.home / "episodes" / safe_component(host) / (safe_component(session) + ".jsonl")

    def _write_episode_lines(self, path, lines):
        if not lines:
            path.unlink(missing_ok=True)
            try:
                path.parent.rmdir()
            except OSError:
                pass
            return 0
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".lumen-" + uuid.uuid4().hex + ".tmp")
        with temporary.open("xb") as stream:
            stream.write(b"\n".join(lines) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        return len(lines)

    def _write_episode_file(self, host, session):
        lines = [canonical(e) for e in self._episodes() if e["host"] == host and e["session"] == session]
        return self._write_episode_lines(self._episode_path(host, session), lines)

    def rebuild_episode_files(self):
        """Rewrite the whole episodes tree from the ledger; stale or foreign files vanish."""
        directory = self.home / "episodes"
        if directory.exists():
            shutil.rmtree(directory)
        sessions = sorted({(e["host"], e["session"]) for e in self._episodes()})
        return sum(self._write_episode_file(host, session) for host, session in sessions)

    def _purge_episode_files(self):
        """Drop erased or unreadable lines from every episode file; a killed purge finishes here."""
        for path in sorted((self.home / "episodes").glob("*/*.jsonl")):
            raw = [line for line in path.read_bytes().splitlines() if line]
            kept = []
            for line in raw:
                try:
                    event = json.loads(line)
                    blocked = self.journal.blocked(event["workspace"], event["id"])
                except (ValueError, KeyError, TypeError):
                    continue
                if not blocked:
                    kept.append(line)
            if len(kept) != len(raw):
                self._write_episode_lines(path, kept)

    def episode_files_status(self):
        """Files and lines on disk, and how many lines name an event the ledger no longer has."""
        ids = {row[0] for row in self.db.execute("SELECT id FROM events")}
        files = lines = stale = 0
        for path in (self.home / "episodes").glob("*/*.jsonl"):
            files += 1
            for line in path.read_bytes().splitlines():
                if not line:
                    continue
                lines += 1
                try:
                    if json.loads(line)["id"] not in ids:
                        stale += 1
                except (ValueError, KeyError, TypeError):
                    stale += 1
        return {"files": files, "lines": lines, "stale": stale}

    def capture_status(self, workspace):
        pending = self.db.execute("SELECT COUNT(*) FROM pending_prompts WHERE workspace=?", (workspace,)).fetchone()[0]
        conflicts = self.db.execute("SELECT COALESCE(SUM(count),0) FROM capture_conflicts WHERE workspace=?", (workspace,)).fetchone()[0]
        duplicates = self.db.execute("SELECT COALESCE(SUM(count),0) FROM capture_duplicates WHERE workspace=?", (workspace,)).fetchone()[0]
        return {"pending_prompts": pending, "capture_conflicts": conflicts, "duplicate_deliveries": duplicates}

    def revise(self, access, predecessors, expected_state, affected, reason, revision_kind="change", successor=None):
        with self.transaction():
            require(bool(predecessors), "Predecessor required")
            first = self.expand(access, predecessors[0])["event"]
            require(first is not None and first["kind"] == "assertion", "Source unavailable", "source_unavailable")
            events = self.key_events(access, first["scope"], first["subject"], first["relation"])
            by_id = {e["id"]: e for e in events}
            require(bool(predecessors) and all(p in by_id for p in predecessors), "Source unavailable", "source_unavailable")
            first = by_id[predecessors[0]]
            require(first["kind"] == "assertion", "Predecessor must be assertion")
            _, _, _, token = state(events, access.workspace, first["subject"], first["relation"], self.rules)
            require(token == expected_state, "State changed", "revision_conflict")
            if successor:
                self._validate_write(successor, access)
            revision = new_event("revision", access.workspace, first["scope"], access.actor,
                                 predecessors=[ref(by_id[p]) for p in predecessors],
                                 successor=ref(successor) if successor else None, affected=affected,
                                 reason=redact(reason), revision_kind=revision_kind, expected_state=expected_state)
            self._validate_write(revision, access)
            closure(events + ([successor] if successor else []) + [revision])
            if successor:
                self._insert(successor)
            self._insert(revision)
            stale = self._invalidate_derivatives(access.workspace, predecessors)
        return {"id": revision["id"], "durable": True, "snapshot": self.watermark(), "derivatives_invalidated": stale}

    # Derivatives (summaries now, skill candidates in Phase 6) record the events they depend on; a
    # revision or an erasure of any of those marks them stale and out of current answers until
    # the consolidator regenerates them (plan §4 Scoped supersession, step 6).
    def _invalidate_derivatives(self, workspace, event_ids):
        stale = []
        for row in self.db.execute("SELECT id, depends_on FROM derivatives WHERE workspace=? AND stale=0", (workspace,)):
            if set(json.loads(row["depends_on"])) & set(event_ids):
                stale.append(row["id"])
        if stale:
            self.db.executemany("UPDATE derivatives SET stale=1 WHERE id=?", [(i,) for i in stale])
        return stale

    def derivative_status(self, workspace):
        rows = self.db.execute("SELECT kind, stale, COUNT(*) FROM derivatives WHERE workspace=? GROUP BY kind, stale", (workspace,))
        status = {}
        for kind, stale, count in rows:
            status.setdefault(kind, {"current": 0, "stale": 0})["stale" if stale else "current"] += count
        extractions = self.db.execute("SELECT COUNT(*) FROM extractions WHERE workspace=?", (workspace,)).fetchone()[0]
        return {"kinds": status, "extractions": extractions}

    def derivatives(self, workspace, kind=None, include_stale=True):
        sql, args = "SELECT * FROM derivatives WHERE workspace=?", [workspace]
        if kind:
            sql, args = sql + " AND kind=?", args + [kind]
        if not include_stale:
            sql += " AND stale=0"
        return [{"id": r["id"], "kind": r["kind"], "key": r["key"], "stale": bool(r["stale"]),
                 "depends_on": json.loads(r["depends_on"]), "body": json.loads(r["body"]), "updated_ns": r["updated_ns"]}
                for r in self.db.execute(sql + " ORDER BY kind, key LIMIT 1000", args)]

    def _put_derivative(self, workspace, kind, key, depends_on, body):
        did = digest([workspace, kind, key])
        self.db.execute("INSERT OR REPLACE INTO derivatives VALUES(?,?,?,?,?,?,0,?)",
                        (did, workspace, kind, key, canonical(sorted(set(depends_on))).decode(), canonical(body).decode(), time.time_ns()))
        return did

    def refresh_summary(self, access, scope, subject, relation):
        """The current values of one subject/relation, with the events it depends on."""
        events = self.key_events(access, scope, subject, relation)
        packet = project(events, access.workspace, subject, relation, int(time.time()), rules=self.rules)
        body = {"scope": scope, "subject": subject, "relation": relation, "status": packet["status"],
                "values": sorted({a["value"] for a in packet["assertions"]}), "state_token": packet["state_token"]}
        return self._put_derivative(access.workspace, "summary", "|".join((scope, subject, relation)),
                                    [e["id"] for e in events], body)

    def regenerate_stale_summaries(self, access):
        regenerated = []
        for derivative in self.derivatives(access.workspace, "summary"):
            if not derivative["stale"]:
                continue
            scope, subject, relation = derivative["body"]["scope"], derivative["body"]["subject"], derivative["body"]["relation"]
            if scope not in access.scopes:
                continue
            if not self.key_events(access, scope, subject, relation):
                self.db.execute("DELETE FROM derivatives WHERE id=?", (derivative["id"],))
            else:
                self.refresh_summary(access, scope, subject, relation)
            regenerated.append(derivative["id"])
        return regenerated

    def _record_index_failure(self):
        """A failed index pass counts one attempt against every queued entry; nothing is dropped."""
        if self.db.in_transaction:
            self.db.execute("ROLLBACK")
        self.db.execute("UPDATE outbox SET attempts=attempts+1")

    def index_retry_status(self):
        row = self.db.execute("SELECT COUNT(*), COALESCE(MAX(attempts),0) FROM outbox").fetchone()
        return {"queued": row[0], "max_attempts": row[1], "bound": MAX_INDEX_ATTEMPTS,
                "exhausted": row[1] >= MAX_INDEX_ATTEMPTS}

    def reindex(self, access):
        require(access.owner, "Reindex requires owner", "not_authorized")
        try:
            return self._reindex()
        except BaseException:
            self._record_index_failure()
            raise

    def _reindex(self):
        # Writer transaction prevents watermark racing capture while rebuilding.
        with self.transaction():
            events = [self._decode(row[0]) for row in self.db.execute("SELECT body FROM events ORDER BY id")]
            admitted, pending = [], []
            for workspace in sorted({e["workspace"] for e in events}):
                active, waiting = closure([e for e in events if e["workspace"] == workspace])
                admitted.extend(active)
                pending.extend(waiting)
            watermark = self.watermark()
            self.index.execute("BEGIN IMMEDIATE")
            try:
                for table in ("lexical", "exact", "admissions", "pending", "aliases", "edges"):
                    self.index.execute(f"DELETE FROM {table}")
                self.db.execute("DELETE FROM event_keys")
                self.db.execute("DELETE FROM event_refs")
                for event in events:
                    self._index_references(event)
                for event in sorted(admitted, key=lambda e: e["kind"] != "assertion"):
                    self._index_key(event)
                sequences = {(r["workspace"], r["id"]): r["seq"] for r in self.db.execute("SELECT workspace,id,seq FROM events")}
                for e in events:
                    if e["id"] in pending:
                        self.index.execute("INSERT INTO pending VALUES(?,?,?)", (e["workspace"], e["scope"], e["id"]))
                self.fault("index_cleared")
                for e in admitted:
                    if e["kind"] != "assertion":
                        continue
                    self._index_assertion(e, sequences[e["workspace"], e["id"]])
                admitted_ids = {(e["workspace"], e["id"]) for e in admitted if e["kind"] == "assertion"}
                for e in admitted:
                    # Bounded evidence expansion: an evidence event's citation text is searchable
                    # under the assertion it supports, so a paraphrase in the evidence finds the fact.
                    if e["kind"] == "evidence" and (e["workspace"], e["target"]["id"]) in admitted_ids:
                        text = self._citation_text(e["citations"])
                        if text:
                            self.index.execute("INSERT INTO lexical VALUES(?,?,?,?)", (e["workspace"], e["scope"], e["target"]["id"], text))
                self.index.execute("INSERT OR REPLACE INTO metadata VALUES('watermark',?)", (str(watermark),))
                self.index.execute("INSERT OR REPLACE INTO metadata VALUES('schema',?)", (INDEX_SCHEMA,))
                self.fault("before_index_commit")
                self.index.execute("COMMIT")
            except BaseException:
                if self.index.in_transaction:
                    self.index.execute("ROLLBACK")
                raise
            self.fault("after_index_commit")
            self.db.execute("DELETE FROM outbox WHERE seq<=?", (watermark,))
            episode_lines = self.rebuild_episode_files()
        return {"watermark": watermark, "pending": pending, "episode_lines": episode_lines}

    @staticmethod
    def _citation_text(citations):
        parts = []
        for c in citations:
            if c["kind"] == "episode":
                parts.append(c["text"][:2048])
            else:
                parts.append(c["anchor"][:512])
        return " ".join(parts)

    def _index_assertion(self, e, seq):
        # Retry after an index commit / outbox commit crash replaces this derived row.
        exists = self.index.execute("SELECT 1 FROM admissions WHERE workspace=? AND event_id=?", (e["workspace"], e["id"])).fetchone()
        if exists:
            for table in ("lexical", "exact", "admissions", "aliases", "edges"):
                self.index.execute(f"DELETE FROM {table} WHERE workspace=? AND event_id=?", (e["workspace"], e["id"]))
        content = " ".join(e[k] for k in ("subject", "relation", "value", "text"))
        normalized = re.sub(r"([a-z])([A-Z])", r"\1 \2", content).replace("_", " ")
        # Retrieval fields are disposable aids: the original evidence stays in the ledger untouched.
        self.index.execute("INSERT INTO lexical VALUES(?,?,?,?)",
                           (e["workspace"], e["scope"], e["id"], " ".join((content, normalized, self._citation_text(e["citations"])))))
        self.index.execute("INSERT INTO admissions VALUES(?,?,?)", (e["workspace"], e["id"], seq))
        identifiers = set()
        for identifier in re.findall(r"[\w./:\\-]+", content)[:256]:
            identifiers.update(identifier_variants(identifier))
        self.index.executemany("INSERT INTO exact VALUES(?,?,?,?)",
                               [(e["workspace"], e["scope"], e["id"], i) for i in sorted(identifiers)])
        if e["relation"] == "alias_of":
            # Source-backed alias: subject names the alias, value the canonical entity; provenance is the event.
            self.index.execute("INSERT INTO aliases VALUES(?,?,?,?,?)",
                               (e["workspace"], e["scope"], e["id"], e["subject"].lower(), e["value"]))
        if re.fullmatch(r"[\w./:\\-]{1,64}", e["value"]):
            self.index.execute("INSERT INTO edges VALUES(?,?,?,?,?,?)",
                               (e["workspace"], e["scope"], e["id"], e["subject"], e["relation"], e["value"].lower()))

    def drain_index(self, access):
        require(access.owner, "Index maintenance requires owner", "not_authorized")
        # Control-event admission uses full graph validation. Pure assertion capture is incremental.
        pending = self.db.execute("SELECT e.body FROM events e JOIN outbox o ON e.seq=o.seq").fetchall()
        if self.index_watermark() < 0 or any(self._decode(row[0])["kind"] not in ("assertion", "episode") for row in pending):
            return self.reindex(access)
        try:
            return self._drain_index()
        except BaseException:
            self._record_index_failure()
            raise

    def _drain_index(self):
        with self.transaction():
            rows = self.db.execute("SELECT e.seq,e.body FROM events e JOIN outbox o ON e.seq=o.seq ORDER BY e.seq").fetchall()
            self.index.execute("BEGIN IMMEDIATE")
            try:
                for row in rows:
                    e = self._decode(row["body"])
                    validate(e)
                    require(not self.journal.blocked(e["workspace"], e["id"]), "Erased event", "not_authorized")
                    if e["kind"] == "assertion":
                        self._index_assertion(e, row["seq"])
                self.index.execute("INSERT OR REPLACE INTO metadata VALUES('watermark',?)", (str(self.watermark()),))
                self.index.execute("INSERT OR REPLACE INTO metadata VALUES('schema',?)", (INDEX_SCHEMA,))
                self.fault("before_index_commit")
                self.index.execute("COMMIT")
            except BaseException:
                if self.index.in_transaction:
                    self.index.execute("ROLLBACK")
                raise
            self.fault("after_index_commit")
            self.db.execute("DELETE FROM outbox")
        return {"watermark": self.index_watermark(), "indexed": len(rows)}

    def recall(self, access, query, at=None, platform="any", branch="any", path="", known_at=None, roots=None,
               source_resolver=None, nearness=None):
        require(len(query.encode()) <= 4096, "Query too large", "budget_exhausted")
        require(self.index_watermark() == self.watermark(), "Index not current", "index_pending")
        at = int(time.time()) if at is None else at
        require(known_at is None or type(known_at) is int and known_at >= 0, "known_at must be a local sequence")
        require(nearness is None or (isinstance(nearness, dict) and all(type(v) is int for v in nearness.values())),
                "Invalid nearness ranking")
        tokens = re.findall(r"\w+", query, re.UNICODE)[:32]
        scopes = sorted(access.scopes)
        require(bool(scopes), "No grant", "not_authorized")
        tier = lambda scope: (nearness or {}).get(scope, 0 if scope.startswith("session:") else 5)
        marks = ",".join("?" for _ in scopes)
        horizon = known_at if known_at is not None else self.watermark()
        pending = [r[0] for r in self.index.execute(f"SELECT event_id FROM pending WHERE workspace=? AND scope IN ({marks}) ORDER BY event_id", [access.workspace, *scopes])]
        signals = {"bm25": "ran", "exact": "ran", "identifier_variants": "ran", "aliases": "ran", "relations": "ran",
                   "span_dedupe": "ran", "scope_widening": "ran" if nearness else "flat", "text_generation": "disabled"}
        truncated, candidates, expansions = False, [], {"alias_terms": [], "ambiguous_aliases": [], "relation_subjects": []}
        if tokens:
            # One expansion pass: source-backed aliases add the canonical name's tokens; an alias that
            # names more than one canonical entity is ambiguous and expands nothing.
            search_tokens = list(tokens)
            rows = self.index.execute(f"SELECT alias, canonical FROM aliases WHERE workspace=? AND scope IN ({marks}) AND alias IN ({','.join('?' for _ in tokens)})",
                                      [access.workspace, *scopes, *(t.lower() for t in tokens)]).fetchall()
            by_alias = {}
            for alias, canonical_name in rows:
                by_alias.setdefault(alias, set()).add(canonical_name)
            for alias in sorted(by_alias):
                if len(by_alias[alias]) == 1:
                    canonical_name = next(iter(by_alias[alias]))
                    expansions["alias_terms"].append(canonical_name)
                    search_tokens.extend(re.findall(r"\w+", canonical_name)[:8])
                else:
                    expansions["ambiguous_aliases"].append(alias)
            search_tokens = list(dict.fromkeys(search_tokens))[:48]
            if expansions["alias_terms"]:
                signals["aliases"] = "expanded"
            if expansions["ambiguous_aliases"]:
                signals["aliases"] = "ambiguous:" + ",".join(expansions["ambiguous_aliases"])
            fts = " OR ".join('"' + t.replace('"', '""') + '"' for t in search_tokens)
            # Scope predicates inside SQL, prior to ranking/limit; no global top-k leakage.
            sql = f"""SELECT lexical.event_id,bm25(lexical) score FROM lexical CROSS JOIN admissions a
                     ON lexical.workspace=a.workspace AND lexical.event_id=a.event_id
                     WHERE lexical MATCH ? AND lexical.workspace=? AND lexical.scope IN ({marks}) AND a.seq<=?
                     ORDER BY score,lexical.event_id LIMIT 501"""
            lexical = list(dict.fromkeys(r[0] for r in self.index.execute(sql, [fts, access.workspace, *scopes, horizon])))
            variants = set(identifier_variants(query.strip()) if query.strip() else [])
            for token in search_tokens[:16]:
                variants.update(identifier_variants(token))
            variants = sorted(variants)[:64]
            exact = [r[0] for r in self.index.execute(
                f"""SELECT DISTINCT exact.event_id FROM exact JOIN admissions a
                    ON exact.workspace=a.workspace AND exact.event_id=a.event_id WHERE exact.workspace=?
                    AND exact.scope IN ({marks}) AND identifier IN ({','.join('?' for _ in variants)}) AND a.seq<=? ORDER BY exact.event_id LIMIT 501""",
                [access.workspace, *scopes, *variants, horizon])] if variants else []
            truncated = len(lexical) > 500 or len(exact) > 500
            lexical, exact = lexical[:500], exact[:500]
            scores = {}
            for ranking in (lexical, exact):
                for rank, eid in enumerate(ranking, 1):
                    scores[eid] = scores.get(eid, 0) + 1 / (60 + rank)
            # Fuse with RRF and stable ids, then rank nearer scopes higher.
            ordered = sorted(scores, key=lambda eid: (-scores[eid], eid))
            scoped = []
            for eid in ordered:
                event = self.expand(access, eid)["event"]
                if event is not None:
                    scoped.append((tier(event["scope"]), -scores[eid], eid, event))
            candidates = [(eid, event) for _, _, eid, event in sorted(scoped, key=lambda x: x[:3])]
        results, seen, used, spans, deferred = [], set(), 0, set(), []

        def add(a, via=None, demoted=False):
            nonlocal used, truncated
            key = (a["scope"], a["subject"], a["relation"])
            if key in seen:
                return True
            seen.add(key)
            events = self.key_events(access, *key, known_at)
            packet = project(events, access.workspace, a["subject"], a["relation"], at, platform, branch, path, self.rules)
            if not packet["assertions"]:
                return True
            packet_spans = {span_key(c) for citations in packet["evidence"].values() for c in citations}
            if packet_spans and packet_spans <= spans and not demoted:
                # Span overlap is penalised, not dropped: a fact over an already-shown span ranks last.
                seen.discard(key)
                deferred.append(a)
                return True
            spans.update(packet_spans)
            packet["receipts"] = self._verify(access, packet, roots or {}, source_resolver)
            packet["scope"] = a["scope"]
            packet["nearness"] = tier(a["scope"])
            if via:
                packet["via"] = via
            if demoted:
                packet["span_overlap"] = True
            size = len(canonical(packet))
            if used + size > self.config["max_packet_bytes"]:
                truncated = True
                return False
            used += size
            results.append(packet)
            return len(results) < self.config["max_results"]

        for eid, a in candidates:
            if not add(a):
                break
        span_overlaps = len(deferred)
        for a in deferred:
            if len(results) >= self.config["max_results"] or not add(a, demoted=True):
                break
        # Bounded relation expansion, one hop: facts whose value names a found subject (the other end
        # of an edge) join the answer, nearest scope first, at most ten.
        if results and len(results) < self.config["max_results"]:
            joined = 0
            for subject in list(dict.fromkeys(p["assertions"][0]["subject"] for p in results))[:5]:
                rows = self.index.execute(
                    f"""SELECT edges.event_id, edges.scope FROM edges JOIN admissions a ON edges.workspace=a.workspace
                        AND edges.event_id=a.event_id WHERE edges.workspace=? AND edges.scope IN ({marks}) AND edges.value=? AND a.seq<=?
                        ORDER BY edges.event_id LIMIT 20""", [access.workspace, *scopes, subject.lower(), horizon]).fetchall()
                for eid, _ in sorted(rows, key=lambda r: (tier(r[1]), r[0])):
                    event = self.expand(access, eid)["event"]
                    if event is None or (event["scope"], event["subject"], event["relation"]) in seen:
                        continue
                    expansions["relation_subjects"].append(subject)
                    joined += 1
                    if not add(event, via="relation:" + subject) or joined >= 10:
                        break
                if joined >= 10 or len(results) >= self.config["max_results"]:
                    break
        return {"results": results, "snapshot": self.watermark(), "pending": pending,
                "status": "ok" if results else "insufficient_evidence", "budget_used_bytes": used,
                "signals": signals, "expansions": expansions, "span_overlaps": span_overlaps,
                "scopes_searched": sorted(scopes, key=lambda s: (tier(s), s)),
                "complete": not pending and not truncated and len(seen) >= len(candidates)}

    def _verify(self, access, packet, roots, source_resolver=None):
        receipts = []
        for eid, citations in packet["evidence"].items():
            for c in citations:
                receipt = {"id": eid, "resolved": False, "fresh": False, "support": "unverified",
                           "checked_at": int(time.time())}
                if c["kind"] == "episode":
                    receipt.update(resolved=True, fresh=True)
                elif c["kind"] == "import":
                    receipt.update(self._verify_import(c))
                elif c["project"] in roots and "repo:" + c["project"] in access.scopes:
                    try:
                        source = source_resolver(c["project"], c["path"]) if source_resolver else safe_source(roots[c["project"]], c["path"])
                        require(source.stat().st_size <= 8 * 1024 * 1024, "Source too large", "source_unavailable")
                        actual = hashlib.sha256(source.read_bytes()).hexdigest()
                        receipt.update(resolved=True, fresh=actual == c["sha256"])
                    except (OSError, LumenError):
                        pass
                if not receipt["fresh"]:
                    packet["source_status"] = "stale" if receipt["resolved"] else "source_unavailable"
                self.db.execute("INSERT OR REPLACE INTO receipts VALUES(?,?,?,?)",
                                (access.workspace, eid, digest(c), canonical(receipt).decode()))
                receipts.append(receipt)
        return receipts

    @staticmethod
    def _verify_import(citation):
        """Re-hash the imported memory file on this machine; a blocked or oversized path stays unresolved."""
        from .imports import MAX_FILE_BYTES, blocked
        path = Path(citation["path"])
        if not path.is_absolute() or blocked(path) or path.is_symlink() or not path.is_file():
            return {}
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                return {}
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return {}
        return {"resolved": True, "fresh": actual == citation["sha256"]}

    def expand(self, access, eid):
        if self.journal.blocked(access.workspace, eid):
            return {"event": None, "status": "insufficient_evidence"}
        marks = ",".join("?" for _ in access.scopes)
        row = self.db.execute(f"SELECT body FROM events WHERE workspace=? AND id=? AND scope IN ({marks})",
                              [access.workspace, eid, *sorted(access.scopes)]).fetchone()
        if row:
            return {"event": self._decode(row[0]), "status": "ok"}
        return {"event": None, "status": "insufficient_evidence"}

    def why(self, access, eid):
        """Return a bounded, structured reference chain under the caller's grant."""
        first = self.expand(access, eid)["event"]
        if first is None:
            return {"events": [], "unresolved": [], "complete": True, "status": "insufficient_evidence"}
        pending, seen, events, unresolved, used = [eid], set(), [], [], 0
        truncated = False
        while pending:
            current = pending.pop(0)
            if current in seen:
                continue
            event = self.expand(access, current)["event"]
            if event is None:
                unresolved.append(current)
                seen.add(current)
                continue
            size = len(canonical(event))
            if len(events) >= 100 or used + size > self.config["max_event_bytes"] - 4096:
                pending.insert(0, current)
                break
            seen.add(current)
            used += size
            events.append(event)
            pending.extend(r["id"] for r in references(event) if r["workspace"] == access.workspace and r["id"] not in seen)
            marks = ','.join('?' for _ in access.scopes)
            incoming = [r[0] for r in self.db.execute(f"SELECT id FROM event_refs WHERE target_workspace=? AND target_id=? AND workspace=? AND scope IN ({marks}) ORDER BY id LIMIT 101",
                [access.workspace, current, access.workspace, *sorted(access.scopes)])]
            truncated |= len(incoming) > 100
            pending.extend(i for i in incoming[:100] if i not in seen)
            pending = list(dict.fromkeys(pending))
            if len(pending) > 100:
                truncated = True
                pending = pending[:100]
        complete = not pending and not unresolved and not truncated
        return {"events": events, "unresolved": sorted(set(unresolved)), "complete": complete,
                "status": "ok" if complete else "insufficient_evidence",
                "support": "Source lineage does not establish entailment"}

    def audit(self, access, now=None):
        require(access.owner, "Audit requires owner", "not_authorized")
        now = int(time.time()) if now is None else now
        require(type(now) is int and now >= 0, "Invalid audit time")
        rows = self.db.execute("SELECT id FROM shared_admissions WHERE workspace=? UNION SELECT id FROM staged_files WHERE workspace=? ORDER BY id LIMIT 1001",
                               (access.workspace, access.workspace)).fetchall()
        findings = []
        for row in rows[:1000]:
            event = self.expand(access, row[0])["event"]
            if event is None or event["kind"] != "assertion":
                continue
            receipts = {r[0]: json.loads(r[1]) for r in self.db.execute("SELECT source,body FROM receipts WHERE workspace=? AND id=?",
                        (access.workspace, event["id"]))}
            verified = min((receipts.get(digest(c), {}).get("checked_at", 0)
                            if receipts.get(digest(c), {}).get("fresh") else 0 for c in event["citations"]), default=0)
            if not verified or now - verified >= 90 * 86400:
                findings.append({"id": event["id"], "scope": event["scope"], "last_verified": verified or None,
                                 "reason": "never_verified" if not verified else "unverified_90_days"})
        for row in self.db.execute("SELECT DISTINCT event_id, verdict FROM feedback WHERE workspace=? AND verdict IN ('wrong','harmful') ORDER BY event_id LIMIT 100", (access.workspace,)):
            event = self.expand(access, row[0])["event"]
            if event is not None:
                findings.append({"id": row[0], "scope": event["scope"], "last_verified": None, "reason": "feedback_" + row[1]})
        return {"findings": findings, "complete": len(rows) <= 1000, "automatic_deletion": False,
                "ci_acceptance": "pending: CODEOWNERS and conflict checks are not yet qualified"}

    def doctor(self):
        failures = []
        retries = None
        try:
            check_config(self.config)
            if not fts5_available():
                failures.append("fts5_unavailable")
            try:
                check_rules(self.rules)
            except LumenError:
                failures.append("relation_schema_invalid")
            present = {row[0] for row in self.index.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
            if not {"lexical", "exact", "admissions", "pending", "metadata"} <= present:
                failures.append("index_structure_missing")
            self.index.execute("SELECT count(*) FROM lexical").fetchone()
            if self.index_watermark() != self.watermark():
                failures.append("index_pending")
            retries = self.index_retry_status()
            if retries["exhausted"]:
                failures.append("index_retry_exhausted")
            if self.db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                failures.append("ledger_integrity")
            if self.index.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                failures.append("index_integrity")
            if self.index.execute("SELECT COUNT(DISTINCT event_id) FROM lexical").fetchone()[0] != self.index.execute("SELECT COUNT(*) FROM admissions").fetchone()[0]:
                failures.append("index_admission_integrity")
            if self.db.execute("SELECT COUNT(*) FROM event_keys").fetchone()[0] + self.index.execute("SELECT COUNT(*) FROM pending").fetchone()[0] != self.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]:
                failures.append("projection_key_integrity")
            expected_count = 0
            for row in self.db.execute("SELECT body FROM events"):
                event = self._decode(row[0])
                expected = {(event['scope'], r['workspace'], r['id']) for r in references(event)}
                expected_count += len(expected)
                if expected:
                    actual = {tuple(r) for r in self.db.execute(
                        "SELECT scope,target_workspace,target_id FROM event_refs WHERE workspace=? AND id=?",
                        (event['workspace'], event['id']))}
                    if actual != expected:
                        failures.append("reference_integrity")
                        break
            if 'reference_integrity' not in failures and expected_count != self.db.execute("SELECT COUNT(*) FROM event_refs").fetchone()[0]:
                failures.append("reference_integrity")
            episodes = self.episode_files_status()
            if episodes["stale"]:
                failures.append("episode_projection_stale")
        except (sqlite3.Error, LumenError, OSError) as exc:
            failures.append(type(exc).__name__)
            episodes = None
        return {"healthy": not failures, "failures": failures, "episodes": episodes, "index_retries": retries,
                "relations_digest": digest(self.rules) if isinstance(self.rules, dict) else None,
                "optional": {"automatic_retirement": "disabled", "text_generation": "disabled"}}

    def _reconcile_deletions(self):
        """Startup completes a purge interrupted after its journal commit."""
        removed = False
        with self.transaction():
            for staged in self.db.execute("SELECT * FROM staged_files"):
                if staged["temporary"]:
                    Path(staged["temporary"]).unlink(missing_ok=True)
                if self.journal.blocked(staged["workspace"], staged["id"]):
                    path = Path(staged["path"])
                    if path.exists():
                        with path.open("rb") as stream:
                            content = stream.read(CONFIG["max_event_bytes"] + 65)
                        require(hashlib.sha256(content).hexdigest() == staged["sha256"],
                                "Managed staged file changed; owner cleanup required", "source_unavailable")
                        path.unlink()
                    self.db.execute("DELETE FROM staged_files WHERE path=?", (staged["path"],))
            self.db.execute("UPDATE staged_files SET temporary=NULL")
            # Every managed temporary is registered durably before content is written.
            for job in self.db.execute("SELECT temporary FROM export_jobs"):
                Path(job[0]).unlink(missing_ok=True)
            self.db.execute("DELETE FROM export_jobs")
            for workspace, eid in self.journal.snapshot()["tombstones"]:
                removed |= self.db.execute("DELETE FROM events WHERE workspace=? AND id=?", (workspace, eid)).rowcount > 0
                self.db.execute("DELETE FROM receipts WHERE workspace=? AND id=?", (workspace, eid))
                self.db.execute("DELETE FROM captures WHERE workspace=? AND event_id=?", (workspace, eid))
                self.index.execute("DELETE FROM lexical WHERE workspace=? AND event_id=?", (workspace, eid))
                self.index.execute("DELETE FROM exact WHERE workspace=? AND event_id=?", (workspace, eid))
                self.index.execute("DELETE FROM admissions WHERE workspace=? AND event_id=?", (workspace, eid))
                self.index.execute("DELETE FROM pending WHERE workspace=? AND event_id=?", (workspace, eid))
                self.db.execute("DELETE FROM event_keys WHERE workspace=? AND id=?", (workspace, eid))
                self.db.execute("DELETE FROM shared_admissions WHERE workspace=? AND id=?", (workspace, eid))
                self.db.execute("DELETE FROM event_refs WHERE (workspace=? AND id=?) OR (target_workspace=? AND target_id=?)",
                                (workspace, eid, workspace, eid))
                self.db.execute("DELETE FROM extractions WHERE episode_id=?", (eid,))
                self.db.execute("DELETE FROM feedback WHERE workspace=? AND event_id=?", (workspace, eid))
                self._invalidate_derivatives(workspace, [eid])
            if removed:
                self.db.execute("DELETE FROM outbox WHERE seq NOT IN (SELECT seq FROM events)")
                self.index.execute("DELETE FROM metadata WHERE key='watermark'")
        if removed:
            self._clean_pages()
            self._sweep_objects()
        # A killed purge must also finish managed-export cleanup before startup serves.
        for row in self.db.execute("SELECT path FROM managed_exports"):
            p = Path(row[0])
            if p.is_file():
                bundle = json.loads(p.read_text(encoding="utf-8"))
                kept = [e for e in bundle["events"] if not self.journal.blocked(e["workspace"], e["id"])]
                if len(kept) != len(bundle["events"]):
                    bundle["events"] = kept
                    self._write_export(p, lambda: bundle)
        self._purge_episode_files()

    def _clean_pages(self):
        for db in (self.db, self.index):
            result = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            require(result[0] == 0, "Physical cleanup blocked by reader", "index_pending")
            db.execute("VACUUM")
            result = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            require(result[0] == 0, "Physical cleanup blocked by reader", "index_pending")

    def purge(self, access, eid):
        require(access.owner, "Purge requires owner", "not_authorized")
        # Hold the writer lock from dependency closure through journal durability.
        with self.transaction():
            events = self.events(access)
            require(any(e["id"] == eid for e in events), "Source unavailable", "source_unavailable")
            ids = {eid}
            changed = True
            while changed:
                old = len(ids)
                for e in events:
                    if any(r["id"] in ids for r in references(e)):
                        ids.add(e["id"])
                        if e["kind"] == "revision" and e["successor"]:
                            ids.add(e["successor"]["id"])
                changed = len(ids) != old
            keys = [row[0] for row in self.db.execute("SELECT key,event_id FROM captures WHERE workspace=?", (access.workspace,)) if row[1] in ids]
            self.db.executemany("DELETE FROM pending_prompts WHERE key=?", [(k,) for k in keys])
            self.fault("before_tombstone")
            self.journal.add([(access.workspace, i) for i in sorted(ids)], keys)
            self.fault("after_tombstone")
        self._reconcile_deletions()
        self.reindex(access)
        self._clean_pages()
        return {"erased_ids": sorted(ids), "journal": self.journal.snapshot()["digest"],
                "active_store": "clean", "physical_cleanup": "sqlite checkpoint and vacuum completed",
                "outside_guarantee": ["unmanaged backups", "git history", "other checkouts", "already delivered context", "filesystem snapshots and SSD remanence"]}

    def export(self, access, destination):
        require(access.owner, "Export requires owner", "not_authorized")
        destination = Path(destination).resolve()
        require(not destination.is_relative_to(self.home.resolve()) or destination.parent == (self.home / "exports").resolve(),
                "Exports inside store must use exports directory")
        return self._write_export(destination, lambda: {
            "schema": 1, "format": "lumen-events", "capabilities": ["lexical"], "events": self.events(access)})

    def _write_export(self, destination, make_bundle):
        temporary = destination.with_name(destination.name + ".lumen-" + uuid.uuid4().hex + ".tmp")
        with self.transaction():
            require(not self.db.execute("SELECT 1 FROM export_jobs WHERE path=?", (str(destination),)).fetchone(),
                    "Export already pending; reopen store to recover", "index_pending")
            self.db.execute("INSERT OR IGNORE INTO managed_exports VALUES(?)", (str(destination),))
            self.db.execute("INSERT INTO export_jobs VALUES(?,?)", (str(destination), str(temporary)))
        self.fault("after_export_registration")
        with self.transaction():
            require(self.db.execute("SELECT 1 FROM export_jobs WHERE path=? AND temporary=?",
                    (str(destination), str(temporary))).fetchone(), "Export recovered by another writer; retry", "index_pending")
            bundle = make_bundle()
            bundle["events"] = [e for e in bundle["events"] if not self.journal.blocked(e["workspace"], e["id"])]
            atomic_json(destination, bundle, temporary=temporary, fault=self.fault)
            self.db.execute("DELETE FROM export_jobs WHERE path=?", (str(destination),))
        return {"path": str(destination), "digest": digest(bundle), "events": len(bundle["events"])}

    def restore(self, access, bundle, checkpoint=None, trusted_latest_digest=None):
        require(access.owner, "Restore requires owner", "not_authorized")
        require(checkpoint is not None and trusted_latest_digest is not None,
                "Latest independent deletion checkpoint required; backup time is not proof", "not_authorized")
        require(isinstance(bundle, dict) and set(bundle) == {"schema", "format", "capabilities", "events"} and
                bundle["schema"] == 1 and bundle["format"] == "lumen-events" and bundle["capabilities"] == ["lexical"],
                "Unsupported interchange capabilities", "unsupported_capability")
        require(isinstance(bundle["events"], list) and len(bundle["events"]) <= 100000, "Import bound exceeded", "budget_exhausted")
        self.journal.merge_checkpoint(checkpoint, trusted_latest_digest)
        with self.transaction():
            accepted = []
            for e in bundle["events"]:
                validate(e)
                require(access.permits(e), "Import outside grant", "not_authorized")
                if self.journal.blocked(e["workspace"], e["id"]) or any(self.journal.blocked(r["workspace"], r["id"]) for r in references(e)):
                    continue
                require(e["kind"] != "approval", "Restored approvals require independent authority", "not_authorized")
                require(not SECRET.search(canonical(e).decode()), "Secret in import", "not_authorized")
                accepted.append(e)
            closure(self.events(access) + accepted)
            for e in accepted:
                self._insert(e)
        self._reconcile_deletions()
        self.reindex(access)
        return {"restored": len(accepted), "journal": self.journal.snapshot()["digest"]}
