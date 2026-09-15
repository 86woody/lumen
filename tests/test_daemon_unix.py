"""The Unix daemon path, exercised on any platform through its callables and patched modules.

These are unit checks of the byte-stream framing, the flock ownership branch and the
AF_UNIX endpoint. They do not establish Linux or macOS support; the CI workflow does
that when it runs there.
"""
import os
from pathlib import Path
import struct
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from lumen import daemon
from lumen.model import LumenError


class PosixOS:
    """daemon.os with name posix; everything else forwards to the real module, so pathlib is untouched."""
    name = "posix"

    def __init__(self, **overrides):
        self.overrides = overrides

    def __getattr__(self, attribute):
        if attribute in self.overrides:
            return self.overrides[attribute]
        return getattr(os, attribute)


class Stream:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.reads = []

    def readable(self, timeout):
        return bool(self.chunks) and self.chunks[0] is not None

    def read(self, size):
        self.reads.append(size)
        chunk = self.chunks.pop(0)
        if chunk == b"":
            return b""
        head, rest = chunk[:size], chunk[size:]
        if rest:
            self.chunks.insert(0, rest)
        return head


class UnixFramingTests(unittest.TestCase):
    def frame(self, chunks, maxlength=256, remaining=lambda: 1.0):
        stream = Stream(chunks)
        return daemon.read_stream_frame(stream.readable, stream.read, remaining, maxlength), stream

    def test_partial_header_and_body_are_reassembled(self):
        body = b"#CHALLENGE#" + b"x" * 20
        encoded = struct.pack("!i", len(body)) + body
        result, stream = self.frame([encoded[:1], encoded[:3][1:], encoded[3:9], encoded[9:]])
        self.assertEqual(result, body)
        self.assertEqual(stream.reads[0], 4)

    def test_long_form_header_and_bound(self):
        body = b"y" * 100
        encoded = struct.pack("!i", -1) + struct.pack("!Q", len(body)) + body
        self.assertEqual(self.frame([encoded])[0], body)
        oversized = struct.pack("!i", 257) + b"z" * 257
        with self.assertRaises(LumenError) as caught:
            self.frame([oversized])
        self.assertEqual(caught.exception.code, "not_authorized")
        with self.assertRaises(LumenError):
            self.frame([struct.pack("!i", -1) + struct.pack("!Q", 1 << 40)])

    def test_closed_stream_and_timeout(self):
        with self.assertRaises(EOFError):
            self.frame([struct.pack("!i", 8)[:2], b""])
        with self.assertRaises(LumenError) as caught:
            self.frame([None])
        self.assertEqual(caught.exception.code, "source_unavailable")

        def expired():
            raise LumenError("source_unavailable", "Authentication timed out")
        with self.assertRaises(LumenError):
            self.frame([struct.pack("!i", 4) + b"abcd"], remaining=expired)

    def test_authentication_connection_uses_the_stream_path_off_windows(self):
        body = b"#CHALLENGE#abc"
        encoded = struct.pack("!i", len(body)) + body
        stream = Stream([encoded])

        class Connection:
            def fileno(self):
                return 7
        bounded = daemon._AuthenticationConnection(Connection(), 2)
        fake = PosixOS(read=lambda fd, size: stream.read(size))
        with patch.object(daemon, "os", fake),                 patch.object(daemon.select, "select", lambda r, w, x, t: ([7] if stream.readable(t) else [], [], [])):
            self.assertEqual(bounded.recv_bytes(256), body)
        with self.assertRaises(LumenError):
            bounded.send_bytes(b"x" * 257)


class UnixOwnershipTests(unittest.TestCase):
    def test_flock_branch_acquires_releases_and_maps_contention(self):
        calls = []

        def flock(handle, flags):
            calls.append(flags)
            if flags == 6 and getattr(flock, "held", False):
                raise OSError("already locked")
        fake = types.SimpleNamespace(LOCK_EX=2, LOCK_NB=4, LOCK_UN=8, flock=flock)
        with tempfile.TemporaryDirectory() as td, patch.dict(sys.modules, {"fcntl": fake}), patch.object(daemon, "os", PosixOS()):
            with daemon.ownership(td):
                self.assertEqual(calls, [6])
                self.assertEqual((Path(td) / "writer.lock").read_bytes(), b"0")
            self.assertEqual(calls, [6, 8])
            flock.held = True
            with self.assertRaises(LumenError) as caught:
                with daemon.ownership(td):
                    pass
            self.assertEqual(caught.exception.code, "daemon_already_running")

    def test_endpoint_and_restrict_off_windows(self):
        with tempfile.TemporaryDirectory() as td:
            modes = []
            fake = PosixOS(chmod=lambda path, mode: modes.append((Path(path).name, mode)), getuid=lambda: 1234)
            with patch.object(daemon, "os", fake):
                address, family = daemon.endpoint(td)
                self.assertEqual(family, "AF_UNIX")
                self.assertEqual(Path(address), Path(td) / "daemon.sock")
                daemon.restrict(td)
                file = Path(td) / "transport.json"
                file.write_text("{}")
                daemon.restrict(file)
                self.assertEqual([m for _, m in modes], [0o700, 0o600])
                self.assertEqual(daemon.os_identity(), "1234")

    def test_native_branch_serializes_writers(self):
        with tempfile.TemporaryDirectory() as td:
            with daemon.ownership(td):
                with self.assertRaises(LumenError):
                    with daemon.ownership(td):
                        pass


if __name__ == "__main__":
    unittest.main()
