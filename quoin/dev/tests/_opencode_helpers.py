"""Shared, non-collected helpers for the opencode test modules.

Not a test module itself (no test_ prefix); imported as `import _opencode_helpers`.
"""
from __future__ import annotations

import contextlib
import importlib.util
import socket
import sys
import threading
from pathlib import Path

import pytest

OPENCODE_DIR = Path(__file__).resolve().parent.parent.parent / "adapters" / "opencode"

SEEDED_SECRET = "sk-test-SEEDED-SECRET-0000"
SEEDED_SECRET_ESCAPED = 'sk-test-"quo\\te"-SECRET-1111'


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    old = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = old
    return module


def secret_forms(secret):
    import json

    return [
        secret,
        "Bearer " + secret,
        json.dumps(secret)[1:-1],
        json.dumps(secret, ensure_ascii=True)[1:-1],
    ]


_LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")


def install_loopback_guard(monkeypatch):
    real_socket_connect = socket.socket.connect
    real_socket_connect_ex = socket.socket.connect_ex
    real_create_connection = socket.create_connection

    def _check(address):
        if isinstance(address, tuple) and address:
            host = address[0]
            if host not in _LOOPBACK_HOSTS:
                raise AssertionError("non-loopback connect: %r" % (host,))

    def fake_connect(self, address):
        _check(address)
        return real_socket_connect(self, address)

    def fake_connect_ex(self, address):
        _check(address)
        return real_socket_connect_ex(self, address)

    def fake_create_connection(address, *args, **kwargs):
        _check(address)
        return real_create_connection(address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", fake_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", fake_connect_ex)
    monkeypatch.setattr(socket, "create_connection", fake_create_connection)


@pytest.fixture
def loopback_only(monkeypatch):
    install_loopback_guard(monkeypatch)
    yield


def bind_closed_port():
    """Bind an ephemeral loopback port, then close it immediately.

    A probe target of `127.0.0.1:PORT` right after this returns a refused
    connection deterministically, without ever leaving loopback.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class RawListener:
    """A minimal raw-socket TCP listener for tests that need to control the
    bytes on the wire below HTTP: a TLS-handshake failure, or a connection
    that accepts but never answers.
    """

    def __init__(self, on_accept):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]
        self.stop_event = threading.Event()
        self._thread = threading.Thread(target=self._serve, args=(on_accept,), daemon=True)
        self._thread.start()

    def _serve(self, on_accept):
        try:
            conn, _addr = self._sock.accept()
        except OSError:
            return
        try:
            on_accept(conn, self.stop_event)
        finally:
            with contextlib.suppress(OSError):
                conn.close()

    def stop(self):
        self.stop_event.set()
        with contextlib.suppress(OSError):
            self._sock.close()
        self._thread.join(timeout=2)


def start_tls_failure_listener():
    """Accept one connection and write a fixed non-TLS banner, then close.

    The client's TLS handshake fails deterministically with ssl.SSLError,
    because classification keys on the exception type, not message text
    (D-14) — no certificate generation is needed.
    """

    def _on_accept(conn, _stop_event):
        with contextlib.suppress(OSError):
            conn.sendall(b"not a tls server\n")

    return RawListener(_on_accept)


def start_hang_listener():
    """Accept one connection, read nothing, and never write a response.

    Waiting on the listener's own `stop_event` (rather than reading from the
    socket) means the request bytes the client already sent are never
    consumed, so the connection stays open and silent until either the test
    calls `stop()` or the bounded wait elapses — whichever is first — and
    the serving thread never outlives the test.
    """

    def _on_accept(conn, stop_event):
        stop_event.wait(timeout=5)

    return RawListener(_on_accept)
