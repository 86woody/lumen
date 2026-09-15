import os
import socket
import unittest


class NetworkGuardTests(unittest.TestCase):
    def test_offline_runner_denies_ip_before_connect(self):
        # Normal suite has no network operations. Guard suite proves the guard is active.
        if os.environ.get("LUMEN_NETWORK_DISABLED") != "1":
            self.assertNotIn("LUMEN_NETWORK_DISABLED", os.environ)
            return
        with socket.socket() as sock:
            with self.assertRaisesRegex(PermissionError, "Lumen evaluation"):
                sock.connect(("127.0.0.1", 1))
