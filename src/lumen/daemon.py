"""Local authenticated pipe transport; JSON bytes only, never pickle messages."""
from contextlib import contextmanager
import getpass
import csv
import hashlib
import json
import multiprocessing.connection as ipc
import os
from pathlib import Path
import secrets
import subprocess
import time
import select
import struct

from .model import Access, LumenError, canonical, require
from .recovery import atomic_json
from .service import Service
from .store import Store

MAX_FRAME = 1048576
AUTH_TIMEOUT = 5


class _AuthenticationConnection:
    """Bound the standard multiprocessing mutual-authentication exchange."""
    def __init__(self, connection, timeout):
        self.connection = connection
        self.deadline = time.monotonic() + timeout

    def send_bytes(self, data):
        require(len(data) <= 256, 'Authentication message exceeds bound', 'not_authorized')
        self.connection.send_bytes(data)

    def recv_bytes(self, maxlength=256):
        def remaining():
            value = self.deadline - time.monotonic()
            require(value > 0, 'Authentication timed out', 'source_unavailable')
            return value
        if os.name == 'nt':
            # AF_PIPE preserves message boundaries; do not wait forever for a response.
            require(self.connection.poll(remaining()), 'Authentication timed out', 'source_unavailable')
            return self.connection.recv_bytes(maxlength)
        # AF_UNIX is a byte stream: a readable partial header is not a full frame.
        fd = self.connection.fileno()
        return read_stream_frame(lambda timeout: bool(select.select([fd], [], [], timeout)[0]),
                                 lambda size: os.read(fd, size), remaining, maxlength)


def read_stream_frame(wait_readable, read, remaining, maxlength):
    """One multiprocessing frame from a byte stream: 4-byte length, or -1 then 8 bytes, then the body.

    Pure over its callables so the Unix path is unit-testable on any platform.
    """
    def read_exact(size):
        chunks = bytearray()
        while len(chunks) < size:
            require(wait_readable(remaining()), 'Authentication timed out', 'source_unavailable')
            chunk = read(size - len(chunks))
            if not chunk:
                raise EOFError('Authentication connection closed')
            chunks.extend(chunk)
        return bytes(chunks)
    size, = struct.unpack('!i', read_exact(4))
    if size == -1:
        size, = struct.unpack('!Q', read_exact(8))
    require(0 <= size <= maxlength, 'Authentication message exceeds bound', 'not_authorized')
    return read_exact(size)


def authenticate(connection, key, server, timeout=AUTH_TIMEOUT):
    bounded = _AuthenticationConnection(connection, timeout)
    first, second = (ipc.deliver_challenge, ipc.answer_challenge) if server else (ipc.answer_challenge, ipc.deliver_challenge)
    first(bounded, key)
    second(bounded, key)


def os_identity():
    if os.name == "nt":
        result = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, check=True)
        return next(csv.reader([result.stdout.strip()]))[1]
    return str(os.getuid())


def endpoint(home):
    key = hashlib.sha256(str(Path(home).resolve()).encode()).hexdigest()[:24]
    return (rf"\\.\pipe\lumen-{key}", "AF_PIPE") if os.name == "nt" else (str(Path(home) / "daemon.sock"), "AF_UNIX")


def restrict(path):
    if os.name == "nt":
        result = subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r",
                                 "*" + os_identity() + (":(OI)(CI)F" if Path(path).is_dir() else ":F")], capture_output=True)
        require(result.returncode == 0, "Cannot establish private store ACL", "not_authorized")
    else:
        os.chmod(path, 0o700 if Path(path).is_dir() else 0o600)


def enroll(home, workspace, scopes, project=None):
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    restrict(home)
    path = home / "transport.json"
    require(not path.exists(), "Transport already enrolled")
    config = {"schema": 1, "actor": os_identity(), "workspace": workspace,
              "scopes": sorted(scopes), "transport": secrets.token_hex(32),
              "owner": secrets.token_hex(32), "agent": secrets.token_hex(32), "project": project}
    atomic_json(path, config)
    restrict(path)
    return {"enrolled": True, "workspace": workspace, "scopes": sorted(scopes)}


@contextmanager
def ownership(home):
    path = Path(home) / "writer.lock"
    with path.open("a+b") as lock:
        if path.stat().st_size == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            acquire_lock(lock)
        except OSError as exc:
            raise LumenError("daemon_already_running", "Writer already owns this store") from exc
        try:
            yield
        finally:
            lock.seek(0)
            release_lock(lock)


def acquire_lock(lock):
    """Non-blocking exclusive lock on the writer file; OSError when another writer holds it."""
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)


def release_lock(lock):
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(lock, fcntl.LOCK_UN)


def serve(home, workspace=None):
    home = Path(home)
    config = json.loads((home / "transport.json").read_text())
    address, family = endpoint(home)
    policy = workspace.policy["config"] if workspace is not None else None
    with ownership(home), Store(home, policy) as store:
        if workspace is not None:
            store.rules = dict(workspace.relations)
        access = Access(config["workspace"], frozenset(config["scopes"]), config["actor"], True)
        store.reindex(access)
        require(config.get('project') is None or workspace is not None,
                'Enrolled project requires workspace policy', 'not_authorized')
        require(workspace is None or workspace.id == config['workspace'],
                'Workspace differs from enrollment', 'not_authorized')
        service = Service(store, workspace, config.get('project'))
        if family == "AF_UNIX" and Path(address).exists():
            Path(address).unlink()  # Own lock held; stale socket only, fixed home path.
        with ipc.Listener(address, family=family, authkey=None) as listener:
            if family == "AF_UNIX":
                os.chmod(address, 0o600)
            while True:
                try:
                    connection = listener.accept()
                except ipc.AuthenticationError:
                    continue
                with connection:
                    try:
                        authenticate(connection, bytes.fromhex(config['transport']), server=True)
                        if not connection.poll(5):
                            continue
                        request = json.loads(connection.recv_bytes(MAX_FRAME))
                        require(isinstance(request, dict), "Frame must be an object")
                        require(set(request) == {"token", "operation", "arguments"}, "Invalid frame")
                        token = request["token"]
                        require(isinstance(token, str) and len(token) == 64
                                and all(c in '0123456789abcdef' for c in token),
                                "Invalid caller", "not_authorized")
                        is_owner = secrets.compare_digest(token, config["owner"])
                        require(is_owner or secrets.compare_digest(token, config["agent"]), "Unknown caller", "not_authorized")
                        require(isinstance(request['operation'], str) and isinstance(request['arguments'], dict),
                                "Invalid operation or arguments")
                        grant = Access(config["workspace"], frozenset(config["scopes"]), config["actor"], is_owner)
                        response = service.call(grant, request["operation"], request["arguments"])
                        connection.send_bytes(canonical(response))
                        if request["operation"] == "capture_stop" and "error" not in response:
                            # Background consolidation: after the capture is acknowledged, only when new
                            # episodes exist and the minimum interval has passed; never on the hot path.
                            service.call(access, "consolidate", {})
                    except (OSError, EOFError, ValueError, RecursionError, LumenError, ipc.AuthenticationError):
                        # No request content or credentials enter logs.
                        continue


def call(home, operation, arguments, owner=False, timeout=10):
    require(type(timeout) in (int, float) and 0 < timeout <= 60, 'Invalid transport timeout')
    home = Path(home)
    config = json.loads((home / "transport.json").read_text())
    address, family = endpoint(home)
    try:
        with ipc.Client(address, family=family, authkey=None) as connection:
            authenticate(connection, bytes.fromhex(config['transport']), server=False, timeout=timeout)
            request = {"token": config["owner" if owner else "agent"], "operation": operation, "arguments": arguments}
            payload = canonical(request)
            require(len(payload) <= MAX_FRAME, "Request too large", "budget_exhausted")
            connection.send_bytes(payload)
            require(connection.poll(timeout), "Daemon response timed out", "source_unavailable")
            return json.loads(connection.recv_bytes(MAX_FRAME))
    except (OSError, EOFError, ipc.AuthenticationError) as exc:
        raise LumenError("source_unavailable", "Daemon unavailable") from exc
