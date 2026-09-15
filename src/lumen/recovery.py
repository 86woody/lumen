"""Independent deletion state. Checkpoints require a trusted owner channel."""
import json
import os
from pathlib import Path
import sqlite3

from .model import canonical, digest, require


class DeletionJournal:
    def __init__(self, home):
        directory = Path(home) / "deletion-journal"
        directory.mkdir(exist_ok=True)
        self.db = sqlite3.connect(directory / "journal.db", isolation_level=None, timeout=10)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS tombstones(workspace TEXT,id TEXT, PRIMARY KEY(workspace,id))")
        self.db.execute("CREATE TABLE IF NOT EXISTS capture_keys(key TEXT PRIMARY KEY)")

    def snapshot(self):
        body = {"schema": 1, "tombstones": [list(r) for r in self.db.execute(
            "SELECT workspace,id FROM tombstones ORDER BY workspace,id")],
            "capture_keys": [r[0] for r in self.db.execute("SELECT key FROM capture_keys ORDER BY key")]}
        return {**body, "digest": digest(body)}

    def blocked(self, workspace, eid):
        return self.db.execute("SELECT 1 FROM tombstones WHERE workspace=? AND id=?", (workspace, eid)).fetchone() is not None

    def capture_blocked(self, key):
        return self.db.execute("SELECT 1 FROM capture_keys WHERE key=?", (key,)).fetchone() is not None

    def add(self, tombstones, keys=()):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.executemany("INSERT OR IGNORE INTO tombstones VALUES(?,?)", tombstones)
            self.db.executemany("INSERT OR IGNORE INTO capture_keys VALUES(?)", [(k,) for k in keys])
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def merge_checkpoint(self, checkpoint, trusted_digest):
        require(isinstance(checkpoint, dict) and set(checkpoint) == {"schema", "tombstones", "capture_keys", "digest"},
                "Invalid deletion checkpoint")
        body = {k: v for k, v in checkpoint.items() if k != "digest"}
        require(checkpoint["schema"] == 1 and checkpoint["digest"] == digest(body) == trusted_digest,
                "Trusted latest deletion checkpoint required", "not_authorized")
        self.add(checkpoint["tombstones"], checkpoint["capture_keys"])

    def close(self):
        self.db.close()


def atomic_json(path, value, *, temporary=None, fault=lambda _: None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(temporary) if temporary is not None else path.with_name(path.name + ".tmp")
    with temporary.open("xb") as stream:
        stream.write(canonical(value))
        stream.flush()
        os.fsync(stream.fileno())
    fault("after_export_fsync")
    os.replace(temporary, path)
    fault("after_export_replace")
    if os.name != "nt":
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
