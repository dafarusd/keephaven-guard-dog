"""Run the whole suite with every non-loopback connection refused at the socket layer:
python -m tests.offline  (proof the tests need no network)."""
import os
import socket
import sys
import unittest

_connect = socket.socket.connect


def loopback_only(self, addr):
    host = addr[0] if isinstance(addr, tuple) else addr
    if isinstance(host, str) and host not in ("127.0.0.1", "::1", "localhost") and not host.startswith("/"):
        raise OSError(f"offline test run: refused connection to {host}")
    return _connect(self, addr)


socket.socket.connect = loopback_only
os.environ["GUARD_DOG_NO_NETWORK"] = "1"
sys.exit(not unittest.main(module=None, argv=["offline", "discover", "-s", "tests", "-t", "."], exit=False).result.wasSuccessful())
