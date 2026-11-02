"""workctl HTTP transport against a local server: redirects, size cap, timeout, proxy env."""
from __future__ import annotations

import http.server
import threading
import time

import pytest

from quoin.workctl.envelope import BrokerError
from quoin.workctl.transport import HttpTransport, Request


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/stolen")
            self.end_headers()
        elif self.path == "/big":
            body = b"x" * 5000
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/slow":
            time.sleep(1.5)
            self.send_response(200)
            self.end_headers()
        else:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true}')

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield "http://127.0.0.1:%d" % srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def test_redirect_is_not_followed(server):
    response = HttpTransport().send(Request("GET", server + "/redirect"), timeout=5, max_bytes=1000)
    assert response.status == 302


def test_response_over_cap_is_refused(server):
    with pytest.raises(BrokerError) as exc:
        HttpTransport().send(Request("GET", server + "/big"), timeout=5, max_bytes=1000)
    assert exc.value.code == "UNAVAILABLE" and exc.value.retryable is False


def test_timeout_maps_to_unavailable(server):
    with pytest.raises(BrokerError) as exc:
        HttpTransport().send(Request("GET", server + "/slow"), timeout=0.3, max_bytes=1000)
    assert exc.value.code == "UNAVAILABLE" and exc.value.retryable is True


def test_proxy_environment_is_ignored(server, monkeypatch):
    for name in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    response = HttpTransport().send(Request("GET", server + "/ok"), timeout=5, max_bytes=1000)
    assert response.status == 200 and response.body == b'{"ok": true}'
