"""Shared, non-collected helpers for the opencode test modules.

Not a test module itself (no test_ prefix); imported as `import _opencode_helpers`.
"""
from __future__ import annotations

import importlib.util
import socket
import sys
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
