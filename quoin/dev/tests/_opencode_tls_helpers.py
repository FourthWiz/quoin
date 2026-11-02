"""Shared, non-collected helpers for the identity-check tests: certificates
generated at test time with openssl, a loopback TLS server and a loopback
CONNECT tunnel. Nothing here opens a non-loopback socket."""
from __future__ import annotations

import select
import shutil
import socket
import ssl
import subprocess
import threading
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import pytest

OPENSSL = shutil.which("openssl")


def require_openssl() -> None:
    if OPENSSL is None:
        pytest.skip("openssl is not installed")


def _run(*args: str) -> None:
    subprocess.run([OPENSSL, *args], check=True, capture_output=True)


def make_ca(tmp: Path, name: str = "ca") -> Tuple[Path, Path]:
    require_openssl()
    key, cert = tmp / (name + ".key"), tmp / (name + ".pem")
    _run("req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
         "-keyout", str(key), "-out", str(cert), "-days", "2", "-subj", "/CN=%s" % name,
         "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign")
    return cert, key


def make_leaf(tmp: Path, ca: Tuple[Path, Path], names: Sequence[str], name: str = "leaf") -> Tuple[Path, Path]:
    require_openssl()
    key, csr, cert, ext = (tmp / (name + suffix) for suffix in (".key", ".csr", ".pem", ".ext"))
    ext.write_text("subjectAltName=" + ",".join("DNS:" + n for n in names) + "\n")
    _run("req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
         "-keyout", str(key), "-out", str(csr), "-subj", "/CN=%s" % names[0])
    _run("x509", "-req", "-in", str(csr), "-CA", str(ca[0]), "-CAkey", str(ca[1]), "-CAcreateserial",
         "-out", str(cert), "-days", "2", "-extfile", str(ext))
    return cert, key


def make_ca_and_leaf(tmp: Path, names: Sequence[str]):
    ca = make_ca(tmp)
    return ca[0], make_leaf(tmp, ca, names)


class TlsServer:
    """Completes a TLS handshake on 127.0.0.1 and closes."""

    def __init__(self, cert: Path, key: Path) -> None:
        self._context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._context.load_cert_chain(str(cert), str(key))
        self._listener = socket.socket()
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(8)
        self._listener.settimeout(0.2)
        self.port = self._listener.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except (socket.timeout, OSError):
                continue
            try:
                conn.settimeout(2)
                with self._context.wrap_socket(conn, server_side=True):
                    pass
            except (ssl.SSLError, OSError):
                try:
                    conn.close()
                except OSError:
                    pass

    def __enter__(self) -> "TlsServer":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join(2)
        self._listener.close()


class ConnectTunnel:
    """A loopback proxy. `CONNECT host:port` is answered 200 and spliced to
    `upstream`, or 403 (`mode="deny"`), or left hanging (`mode="hang"`)."""

    def __init__(self, upstream: Optional[Tuple[str, int]] = None, mode: str = "ok") -> None:
        self.upstream, self.mode = upstream, mode
        self.targets: List[str] = []
        self._listener = socket.socket()
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(8)
        self._listener.settimeout(0.2)
        self.port = self._listener.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except (socket.timeout, OSError):
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(3)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(1024)
                if not chunk:
                    return
                data += chunk
            self.targets.append(data.split()[1].decode("ascii", "replace"))
            if self.mode == "hang":
                self._stop.wait(5)
                return
            if self.mode == "deny" or self.upstream is None:
                conn.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
                return
            remote = socket.create_connection(self.upstream, timeout=3)
            conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            pair = [conn, remote]
            while not self._stop.is_set():
                ready, _, _ = select.select(pair, [], [], 0.2)
                for source in ready:
                    payload = source.recv(4096)
                    if not payload:
                        remote.close()
                        return
                    (remote if source is conn else conn).sendall(payload)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def __enter__(self) -> "ConnectTunnel":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join(2)
        self._listener.close()
