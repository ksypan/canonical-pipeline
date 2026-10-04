"""Run the local regression suite with network connections disabled."""
import os
from pathlib import Path
import socket
import unittest
from unittest.mock import patch


def deny_network(*args, **kwargs):
    raise RuntimeError("Network disabled during offline regression tests")


def main():
    root = Path(__file__).resolve().parent
    os.chdir(root)
    with patch.object(socket.socket, "connect", deny_network), \
         patch.object(socket.socket, "connect_ex", deny_network), \
         patch.object(socket, "create_connection", deny_network), \
         patch.object(socket, "getaddrinfo", deny_network):
        suite = unittest.defaultTestLoader.discover(str(root / "tests"))
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
