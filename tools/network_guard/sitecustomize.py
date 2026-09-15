"""Evaluation guard. Local IPC remains allowed; AF_INET/AF_INET6 are denied."""
import os
import sys

if os.environ.get("LUMEN_NETWORK_DISABLED") == "1":
    def guard(event, args):
        if event in {"socket.connect", "socket.bind"}:
            connection = args[0]
            if int(connection.family) in (2, 10, 23):
                raise PermissionError("Lumen evaluation prohibits IP networking")
        if event == "socket.getaddrinfo":
            raise PermissionError("Lumen evaluation prohibits DNS")
    sys.addaudithook(guard)
