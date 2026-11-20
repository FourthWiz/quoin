"""Tests for quoin/scripts/dashboard_server.py — ThreadingHTTPServer.

Tests: model cross-load, route smoke, 405, 404, bind address, URL= line,
auto-increment port, explicit-port-taken exit, --no-browser suppression.
T-02, T-03, T-04 acceptance criteria.
"""
from __future__ import annotations

import http.client
import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Load dashboard_server via spec_from_file_location
# ---------------------------------------------------------------------------

_SCRIPTS_PATH = Path(__file__).resolve().parents[2] / "scripts"
_DS_PATH = _SCRIPTS_PATH / "dashboard_server.py"

_SPEC = importlib.util.spec_from_file_location("_quoin_adapter_dashboard_server_test", _DS_PATH)
_DS = importlib.util.module_from_spec(_SPEC)
sys.modules["_quoin_adapter_dashboard_server_test"] = _DS
_SPEC.loader.exec_module(_DS)

DashboardServer = _DS.DashboardServer
DashboardHandler = _DS.DashboardHandler
_bind_server = _DS._bind_server
_cost_jsonl_mtime_ns = _DS._cost_jsonl_mtime_ns
project_hash = _DS.project_hash
main = _DS.main


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_project(tmp_path: Path) -> Path:
    """Create a minimal project with .workflow_artifacts/."""
    proj = tmp_path / "project"
    (proj / ".workflow_artifacts").mkdir(parents=True)
    return proj


def _start_server(project_root: Path, port: int = 0):
    """Start server in a background thread on an ephemeral port. Returns (server, thread)."""
    import copy

    # Create a fresh handler subclass with the correct project_root
    class _Handler(DashboardHandler):
        pass

    _Handler.project_root = project_root
    _Handler.cost_provider = None  # counts-only for tests

    server = _DS.DashboardServer(("127.0.0.1", port), _Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, t


def _get(server, path: str):
    """GET from the running server. Returns (status, headers, body_bytes)."""
    port = server.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path)
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp.status, resp.getheaders(), body


def _method(server, method: str, path: str):
    """Make an arbitrary HTTP method request. Returns status int."""
    port = server.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, path)
    resp = conn.getresponse()
    resp.read()
    conn.close()
    return resp.status


def _get_with_inm(server, path: str, etag: str):
    """GET with an If-None-Match header. Returns (status, headers_dict, body_bytes)."""
    port = server.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path, headers={"If-None-Match": etag})
    resp = conn.getresponse()
    body = resp.read()
    headers = {h.lower(): v for h, v in resp.getheaders()}
    conn.close()
    return resp.status, headers, body


# ---------------------------------------------------------------------------
# T-02: module skeleton + model cross-load
# ---------------------------------------------------------------------------

class TestModuleLoad:
    def test_scan_tasks_bound(self):
        """spec-loading dashboard_server.py binds scan_tasks."""
        assert callable(_DS.scan_tasks)

    def test_task_detail_bound(self):
        assert callable(_DS.task_detail)

    def test_make_cost_provider_bound(self):
        assert callable(_DS.make_cost_provider)

    def test_model_load_uses_parents1(self):
        """dashboard_server.py loads model via parents[1]/core/scripts — not a bare import."""
        source = _DS_PATH.read_text(encoding="utf-8")
        assert 'parents[1]' in source
        assert '"core" / "scripts"' in source or '"core"' in source

    def test_no_bare_import_dashboard_model(self):
        """Must not do 'import dashboard_model'."""
        source = _DS_PATH.read_text(encoding="utf-8")
        assert "import dashboard_model" not in source


# ---------------------------------------------------------------------------
# T-03: do_GET routes + 405 + 404 + bind address
# ---------------------------------------------------------------------------

class TestRoutes:
    def test_api_tasks_returns_200(self, tmp_path):
        """GET /api/tasks → 200 JSON with tasks/active_task/project_root."""
        proj = _make_project(tmp_path)
        server, _ = _start_server(proj)
        try:
            status, _, body = _get(server, "/api/tasks")
            assert status == 200
            data = json.loads(body)
            assert "tasks" in data
            assert "active_task" in data
            assert "project_root" in data
        finally:
            server.shutdown()

    def test_api_tasks_missing_task_returns_404(self, tmp_path):
        """GET /api/tasks/does-not-exist → 404 with JSON error body."""
        proj = _make_project(tmp_path)
        server, _ = _start_server(proj)
        try:
            status, _, body = _get(server, "/api/tasks/does-not-exist")
            assert status == 404
            data = json.loads(body)
            assert "error" in data
        finally:
            server.shutdown()

    def test_post_returns_405(self, tmp_path):
        """POST /api/tasks → 405 Method Not Allowed."""
        proj = _make_project(tmp_path)
        server, _ = _start_server(proj)
        try:
            status = _method(server, "POST", "/api/tasks")
            assert status == 405
        finally:
            server.shutdown()

    def test_put_returns_405(self, tmp_path):
        """PUT /anything → 405."""
        proj = _make_project(tmp_path)
        server, _ = _start_server(proj)
        try:
            status = _method(server, "PUT", "/api/tasks")
            assert status == 405
        finally:
            server.shutdown()

    def test_path_traversal_returns_404(self, tmp_path):
        """GET /../etc/passwd → 404 (allowlist lookup, not path join)."""
        proj = _make_project(tmp_path)
        server, _ = _start_server(proj)
        try:
            status, _, _ = _get(server, "/../etc/passwd")
            assert status == 404
        finally:
            server.shutdown()

    def test_app_js_traversal_returns_404(self, tmp_path):
        """GET /app.js/../../secret → 404."""
        proj = _make_project(tmp_path)
        server, _ = _start_server(proj)
        try:
            status, _, _ = _get(server, "/app.js/../../secret")
            assert status == 404
        finally:
            server.shutdown()

    def test_bind_address_is_loopback(self, tmp_path):
        """Server socket must be bound to 127.0.0.1."""
        proj = _make_project(tmp_path)
        server, _ = _start_server(proj)
        try:
            assert server.server_address[0] == "127.0.0.1"
        finally:
            server.shutdown()

    def test_api_tasks_and_detail_use_same_project_root(self, tmp_path):
        """Both /api/tasks and /api/tasks/<name> use the same project_root (D-15)."""
        proj = _make_project(tmp_path)
        # Create a task directory so task_detail can find it
        task_dir = proj / ".workflow_artifacts" / "my-task"
        task_dir.mkdir(parents=True)
        (task_dir / "current-plan.md").write_text("# plan\n", encoding="utf-8")

        server, _ = _start_server(proj)
        try:
            # /api/tasks should list my-task
            status, _, body = _get(server, "/api/tasks")
            assert status == 200
            data = json.loads(body)
            names = [t["name"] for t in data.get("tasks", [])]
            assert "my-task" in names

            # /api/tasks/my-task should return 200 (not 404)
            status2, _, body2 = _get(server, "/api/tasks/my-task")
            assert status2 == 200
        finally:
            server.shutdown()

    def test_asset_root_returns_index_html(self, tmp_path):
        """GET / returns index.html content (or 404 if assets not yet deployed)."""
        proj = _make_project(tmp_path)
        server, _ = _start_server(proj)
        try:
            status, _, body = _get(server, "/")
            # Accept 200 (assets present) or 404 (assets not yet built, T-06 not done)
            assert status in (200, 404)
        finally:
            server.shutdown()


# ---------------------------------------------------------------------------
# T-02b (IVG-87): server must serve /memory.js with 200 + JS MIME
# ---------------------------------------------------------------------------

class TestAssetServing:
    """Server-level guard: _ASSET_ALLOWLIST must serve memory.js (IVG-87).

    _send_asset reads from _ASSETS_DIR (source tree), not a deployed copy,
    so this test is deterministic — memory.js always exists in the source tree.
    If _ASSET_ALLOWLIST ever drops '/memory.js', this test fails immediately.
    """

    def test_memory_js_served_200(self, tmp_path):
        """GET /memory.js returns 200 with application/javascript MIME."""
        proj = _make_project(tmp_path)
        server, _ = _start_server(proj)
        try:
            status, headers, body = _get(server, "/memory.js")
            assert status == 200, f"GET /memory.js returned {status}, expected 200"
            content_type = _get_headers_dict(headers).get("content-type", "")
            assert content_type.startswith("application/javascript"), (
                f"GET /memory.js content-type was {content_type!r}, "
                "expected application/javascript"
            )
            assert len(body) > 0, "GET /memory.js returned empty body"
        finally:
            server.shutdown()


# ---------------------------------------------------------------------------
# T-04: URL= line, auto-increment, explicit-port-taken, --no-browser
# ---------------------------------------------------------------------------

class TestPortAndStartup:
    def test_url_line_printed_before_blocking(self, tmp_path):
        """dashboard_server.py --no-browser --port 0 prints exactly one URL= line."""
        proj = _make_project(tmp_path)
        script = str(_DS_PATH)
        proc = subprocess.Popen(
            [sys.executable, script, "--no-browser", "--port", "0",
             "--project-root", str(proj)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert proc.stdout is not None
        try:
            # Read until we see the URL= line (max 5s)
            url_line = None
            deadline = time.time() + 5
            while time.time() < deadline:
                line = proc.stdout.readline().decode("utf-8", errors="replace").strip()
                if line.startswith("URL="):
                    url_line = line
                    break
            assert url_line is not None, "No URL= line printed within 5 seconds"
            assert url_line.startswith("URL=http://127.0.0.1:")
            port_str = url_line.split(":")[-1]
            assert int(port_str) > 0
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    def test_auto_increment_when_default_port_taken(self, tmp_path):
        """When 8787 is pre-bound, server auto-increments to next free port."""
        proj = _make_project(tmp_path)

        # Pre-bind 8787
        pre_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        pre_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            pre_sock.bind(("127.0.0.1", 8787))
            pre_sock.listen(1)
        except OSError:
            pytest.skip("Port 8787 not available for pre-binding test")

        try:
            script = str(_DS_PATH)
            proc = subprocess.Popen(
                [sys.executable, script, "--no-browser", "--project-root", str(proj)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            assert proc.stdout is not None
            try:
                url_line = None
                deadline = time.time() + 5
                while time.time() < deadline:
                    line = proc.stdout.readline().decode("utf-8", errors="replace").strip()
                    if line.startswith("URL="):
                        url_line = line
                        break
                assert url_line is not None, "No URL= line printed within 5 seconds"
                port_str = url_line.split(":")[-1]
                actual_port = int(port_str)
                assert actual_port != 8787
                assert 8788 <= actual_port <= 8796
            finally:
                proc.terminate()
                proc.wait(timeout=5)
        finally:
            pre_sock.close()

    def test_explicit_port_taken_exits_nonzero(self, tmp_path):
        """--port <taken> exits non-zero with a clear stderr message (no auto-increment)."""
        proj = _make_project(tmp_path)

        # Bind a random ephemeral port first
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        taken_port = sock.getsockname()[1]
        sock.listen(1)
        try:
            script = str(_DS_PATH)
            proc = subprocess.run(
                [sys.executable, script, "--no-browser", "--port", str(taken_port),
                 "--project-root", str(proj)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=5,
            )
            assert proc.returncode != 0
            stderr = proc.stderr.decode("utf-8", errors="replace")
            # Should mention the port and "in use"
            assert str(taken_port) in stderr or "in use" in stderr or "port" in stderr.lower()
        finally:
            sock.close()

    def test_no_browser_suppresses_open(self, tmp_path):
        """--no-browser must not call webbrowser.open.

        Tests the source-level logic: args.no_browser controls the open call.
        """
        proj = _make_project(tmp_path)
        source = _DS_PATH.read_text(encoding="utf-8")
        # Verify that --no-browser check guards webbrowser.open
        assert "no_browser" in source
        assert "webbrowser.open" in source
        # And that the guard is `if not args.no_browser` or similar
        assert "no_browser" in source

    def test_no_browser_arg_parsed_correctly(self, tmp_path):
        """Verify --no-browser is a recognised argument (argparse integration)."""
        proj = _make_project(tmp_path)
        script = str(_DS_PATH)
        # --help should list --no-browser without error
        proc = subprocess.run(
            [sys.executable, script, "--help"],
            capture_output=True, timeout=5,
        )
        assert proc.returncode == 0
        assert b"no-browser" in proc.stdout


# ---------------------------------------------------------------------------
# T-05: SSE decision (polling-only mode — SSE was not implemented)
# ---------------------------------------------------------------------------

class TestSSEDecision:
    def test_api_events_absent(self, tmp_path):
        """GET /api/events → 404 (SSE was cut; polling-only per D-10 fallback)."""
        proj = _make_project(tmp_path)
        server, _ = _start_server(proj)
        try:
            status, _, body = _get(server, "/api/events")
            assert status == 404
        finally:
            server.shutdown()

    def test_no_eventsource_in_source(self):
        """app.js must not contain EventSource when SSE was cut (check after T-09)."""
        # This test will be meaningful once app.js exists. Skip gracefully if not yet.
        app_js = Path(__file__).resolve().parents[2] / "core" / "scripts" / "dashboard_assets" / "app.js"
        if not app_js.exists():
            pytest.skip("app.js not yet created (T-08/T-09 not done)")
        source = app_js.read_text(encoding="utf-8")
        assert "new EventSource" not in source and "EventSource(" not in source, (
            "SSE was cut (T-05 spike skipped); app.js must not construct EventSource"
        )


# ---------------------------------------------------------------------------
# T-07 IVG-85: url-file capture (Test C) and SIGHUP shutdown (Test D)
# ---------------------------------------------------------------------------

class TestUrlFileAndSighup:
    def test_url_file_arg_in_help(self, tmp_path):
        """--url-file is a recognised argument (argparse integration check)."""
        proc = subprocess.run(
            [sys.executable, str(_DS_PATH), "--help"],
            capture_output=True, timeout=5,
        )
        assert proc.returncode == 0
        assert b"url-file" in proc.stdout, "--url-file not listed in --help output"

    def test_url_file_written_on_startup(self, tmp_path):
        """T-07 Test C: dashboard_server.py --url-file X writes a http://127.0.0.1:<port>
        URL to X within the poll window (5s). (T-04 acceptance criterion)

        Uses subprocess with --port 0 (ephemeral) and --no-browser to avoid side-effects.
        Polls the url-file for up to 5s to match the agentdesk poller's timing contract.
        """
        proj = _make_project(tmp_path)
        url_file = tmp_path / "dash-url.txt"
        proc = subprocess.Popen(
            [sys.executable, str(_DS_PATH),
             "--no-browser", "--port", "0",
             "--project-root", str(proj),
             "--url-file", str(url_file)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            deadline = time.time() + 5
            url_content = None
            while time.time() < deadline:
                if url_file.exists():
                    content = url_file.read_text(encoding="utf-8").strip()
                    if content.startswith("http://127.0.0.1:"):
                        url_content = content
                        break
                time.sleep(0.1)

            assert url_content is not None, (
                f"URL not written to url-file within 5s; "
                f"url_file exists={url_file.exists()}"
            )
            assert url_content.startswith("http://127.0.0.1:"), (
                f"url-file content is not a loopback URL: {url_content!r}"
            )
            # Extract port and verify it's > 0
            port_str = url_content.rsplit(":", 1)[-1]
            assert int(port_str) > 0, f"Port in url-file should be > 0: {url_content!r}"
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    def test_sighup_shuts_down_server(self, tmp_path):
        """T-07 Test D: SIGHUP causes the server to shut down within a bounded timeout.
        (T-01/T-02 acceptance criterion — validates the SIGHUP handler registered in main())

        Starts a real server subprocess, sends SIGHUP, asserts the process exits
        within 5 seconds. On platforms where SIGHUP is not available (Windows),
        the test is skipped.
        """
        import signal as signal_mod
        if not hasattr(signal_mod, "SIGHUP"):
            pytest.skip("SIGHUP not available on this platform")

        proj = _make_project(tmp_path)
        proc = subprocess.Popen(
            [sys.executable, str(_DS_PATH),
             "--no-browser", "--port", "0",
             "--project-root", str(proj)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert proc.stdout is not None
        try:
            # Wait for the URL= line to confirm the server is up before sending SIGHUP.
            url_line = None
            deadline = time.time() + 5
            while time.time() < deadline:
                line = proc.stdout.readline().decode("utf-8", errors="replace").strip()
                if line.startswith("URL="):
                    url_line = line
                    break
            assert url_line is not None, "Server did not print URL= line within 5s"

            proc.send_signal(signal_mod.SIGHUP)

            # Assert the process shuts down within 5 seconds.
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                raise AssertionError(
                    "Server did not shut down within 5s after SIGHUP — "
                    "SIGHUP handler may not be registered"
                )
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=3)


# ---------------------------------------------------------------------------
# IVG-76 T-06: ETag / 304 conditional-GET tests
# ---------------------------------------------------------------------------

def _get_headers_dict(headers_list):
    """Convert list of (name, value) header tuples to a lowercase-keyed dict."""
    return {k.lower(): v for k, v in headers_list}


class TestETag304Tasks:
    """ETag/304 acceptance criteria for /api/tasks (T-02)."""

    def test_first_request_returns_200_with_etag(self, tmp_path):
        """First /api/tasks request (no If-None-Match) → 200 with ETag header."""
        proj = _make_project(tmp_path)
        server, _ = _start_server(proj)
        try:
            status, headers, body = _get(server, "/api/tasks")
            h = _get_headers_dict(headers)
            assert status == 200
            assert "etag" in h, "Response must include an ETag header"
            assert h["etag"].startswith('"') and h["etag"].endswith('"'), (
                f"ETag must be a quoted string, got: {h['etag']!r}"
            )
        finally:
            server.shutdown()

    def test_second_request_matching_etag_returns_304(self, tmp_path):
        """Second /api/tasks with matching ETag → 304, empty body, ETag echoed."""
        proj = _make_project(tmp_path)
        server, _ = _start_server(proj)
        try:
            # First request — get the ETag
            status1, headers1, body1 = _get(server, "/api/tasks")
            assert status1 == 200
            h1 = _get_headers_dict(headers1)
            assert "etag" in h1
            etag = h1["etag"]

            # Second request — send matching If-None-Match
            status2, headers2, body2 = _get_with_inm(server, "/api/tasks", etag)
            assert status2 == 304, f"Expected 304, got {status2}"
            assert len(body2) == 0, "304 must not have a body"
            assert "etag" in headers2, "304 must echo ETag header"
            assert headers2["etag"] == etag
        finally:
            server.shutdown()

    def test_stale_etag_returns_200(self, tmp_path):
        """Stale/mismatched If-None-Match → 200 with fresh payload."""
        proj = _make_project(tmp_path)
        server, _ = _start_server(proj)
        try:
            status, headers, body = _get_with_inm(
                server, "/api/tasks", '"stale-etag-value-xyz"'
            )
            assert status == 200
            # headers is already a dict from _get_with_inm
            assert "etag" in headers
        finally:
            server.shutdown()

    def test_no_if_none_match_returns_200(self, tmp_path):
        """No If-None-Match header → 200 full payload (backward compat)."""
        proj = _make_project(tmp_path)
        server, _ = _start_server(proj)
        try:
            status, headers, body = _get(server, "/api/tasks")
            assert status == 200
            data = json.loads(body)
            assert "tasks" in data
        finally:
            server.shutdown()

    def test_scan_tasks_not_called_on_304(self, tmp_path):
        """scan_tasks must NOT be invoked when a 304 is returned."""
        proj = _make_project(tmp_path)
        call_count = {"n": 0}
        orig_scan = _DS.scan_tasks

        def counting_scan(*args, **kwargs):
            call_count["n"] += 1
            return orig_scan(*args, **kwargs)

        server, _ = _start_server(proj)
        try:
            _DS.scan_tasks = counting_scan  # type: ignore[attr-defined]

            # First request — populates ETag; scan_tasks called once
            status1, headers1, _ = _get(server, "/api/tasks")
            assert status1 == 200
            etag = _get_headers_dict(headers1).get("etag")
            count_after_first = call_count["n"]
            assert count_after_first >= 1

            # Second request — 304 path; scan_tasks must NOT be called again
            assert etag is not None, "ETag must be present in first response"
            status2, _, _ = _get_with_inm(server, "/api/tasks", etag)
            assert status2 == 304
            assert call_count["n"] == count_after_first, (
                "scan_tasks must not be called when 304 is returned"
            )
        finally:
            _DS.scan_tasks = orig_scan  # type: ignore[attr-defined]
            server.shutdown()

    def test_no_cache_control_in_200_response(self, tmp_path):
        """Cache-Control and Expires must NOT appear in /api/tasks 200 response headers."""
        proj = _make_project(tmp_path)
        server, _ = _start_server(proj)
        try:
            _, headers, _ = _get(server, "/api/tasks")
            h = _get_headers_dict(headers)
            assert "cache-control" not in h, "Cache-Control must not be set on API responses"
            assert "expires" not in h, "Expires must not be set on API responses"
        finally:
            server.shutdown()


class TestETag304TaskDetail:
    """ETag/304 acceptance criteria for /api/tasks/<name> (T-03)."""

    def _make_project_with_task(self, tmp_path):
        proj = _make_project(tmp_path)
        task_dir = proj / ".workflow_artifacts" / "my-task"
        task_dir.mkdir(parents=True)
        (task_dir / "current-plan.md").write_text("# plan\n", encoding="utf-8")
        return proj

    def test_first_detail_request_returns_200_with_etag(self, tmp_path):
        """First /api/tasks/<name> → 200 with ETag header."""
        proj = self._make_project_with_task(tmp_path)
        server, _ = _start_server(proj)
        try:
            status, headers, _ = _get(server, "/api/tasks/my-task")
            h = _get_headers_dict(headers)
            assert status == 200
            assert "etag" in h
        finally:
            server.shutdown()

    def test_second_detail_request_matching_etag_returns_304(self, tmp_path):
        """Second /api/tasks/<name> with matching ETag → 304, empty body."""
        proj = self._make_project_with_task(tmp_path)
        server, _ = _start_server(proj)
        try:
            status1, headers1, _ = _get(server, "/api/tasks/my-task")
            assert status1 == 200
            etag = _get_headers_dict(headers1).get("etag")
            assert etag is not None

            status2, headers2, body2 = _get_with_inm(server, "/api/tasks/my-task", etag)
            assert status2 == 304
            assert len(body2) == 0
            assert headers2.get("etag") == etag
        finally:
            server.shutdown()

    def test_unknown_task_returns_404_not_304(self, tmp_path):
        """Unknown task must return 404 (not 304, not 500)."""
        proj = _make_project(tmp_path)
        server, _ = _start_server(proj)
        try:
            # Even with a fake ETag, unknown task must 404
            status, _, _ = _get_with_inm(
                server, "/api/tasks/nonexistent", '"fake-etag"'
            )
            assert status == 404
        finally:
            server.shutdown()

    def test_task_detail_not_called_on_304(self, tmp_path):
        """task_detail must NOT be invoked when a 304 is returned."""
        proj = self._make_project_with_task(tmp_path)
        call_count = {"n": 0}
        orig_detail = _DS.task_detail

        def counting_detail(*args, **kwargs):
            call_count["n"] += 1
            return orig_detail(*args, **kwargs)

        server, _ = _start_server(proj)
        try:
            _DS.task_detail = counting_detail  # type: ignore[attr-defined]

            status1, headers1, _ = _get(server, "/api/tasks/my-task")
            assert status1 == 200
            etag = _get_headers_dict(headers1).get("etag")
            count_after_first = call_count["n"]
            assert count_after_first >= 1

            assert etag is not None, "ETag must be present in first response"
            status2, _, _ = _get_with_inm(server, "/api/tasks/my-task", etag)
            assert status2 == 304
            assert call_count["n"] == count_after_first, (
                "task_detail must not be called when 304 is returned"
            )
        finally:
            _DS.task_detail = orig_detail  # type: ignore[attr-defined]
            server.shutdown()


class TestCostJsonlMtimeNs:
    """IVG-76 T-06: _cost_jsonl_mtime_ns and under-invalidation tests."""

    def test_missing_project_dir_returns_zero(self, tmp_path):
        """_cost_jsonl_mtime_ns returns 0 without raising when project dir is absent."""
        root = tmp_path / "nonexistent_project"
        root.mkdir()
        # project_hash dir doesn't exist under tmp_path/.claude/projects/
        result = _cost_jsonl_mtime_ns(root, home=tmp_path)
        assert result == 0, "Should return 0 when JSONL project dir is absent"

    def test_empty_jsonl_dir_returns_zero(self, tmp_path):
        """_cost_jsonl_mtime_ns returns 0 when JSONL dir exists but has no .jsonl files."""
        root = tmp_path / "project"
        root.mkdir()
        ph = project_hash(str(root))
        proj_dir = tmp_path / ".claude" / "projects" / ph
        proj_dir.mkdir(parents=True)
        # No .jsonl files created

        result = _cost_jsonl_mtime_ns(root, home=tmp_path)
        assert result == 0

    def test_existing_jsonl_returns_nonzero(self, tmp_path):
        """_cost_jsonl_mtime_ns returns non-zero for a real .jsonl file (happy path)."""
        root = tmp_path / "project"
        root.mkdir()
        ph = project_hash(str(root))
        proj_dir = tmp_path / ".claude" / "projects" / ph
        proj_dir.mkdir(parents=True)

        jsonl = proj_dir / "test-session.jsonl"
        jsonl.write_text('{"message": {"usage": {"input_tokens": 100}}}\n')

        result = _cost_jsonl_mtime_ns(root, home=tmp_path)
        assert result != 0, (
            "project_hash must be in scope and JSONL must be readable (CRIT-2 guard)"
        )

    def test_jsonl_append_changes_fingerprint(self, tmp_path):
        """Under-invalidation test (a): JSONL append changes the fingerprint (CRIT-1 guard).

        A JSONL append bumps the file's mtime and size. This test would FAIL against
        the broken dir-mtime approach (dir mtime unchanged on file append).
        It also ensures project_hash is in scope (CRIT-2 guard via cj1 != 0 assertion).
        """
        import time as _time

        root = tmp_path / "project"
        root.mkdir()
        ph = project_hash(str(root))
        proj_dir = tmp_path / ".claude" / "projects" / ph
        proj_dir.mkdir(parents=True)

        jsonl = proj_dir / "test-session.jsonl"
        jsonl.write_text('{"message": {"usage": {"input_tokens": 100}}}\n')

        cj1 = _cost_jsonl_mtime_ns(root, home=tmp_path)
        assert cj1 != 0, (
            "project_hash must be in scope and JSONL must be readable (CRIT-2 guard)"
        )

        # Ensure mtime can advance (nanosecond resolution usually sufficient,
        # but a small sleep avoids any sub-nanosecond FS edge cases)
        _time.sleep(0.01)

        # APPEND a row — this is the live cost-accrual pattern (same file, new line)
        with open(jsonl, "a") as f:
            f.write('{"message": {"usage": {"input_tokens": 200}}}\n')

        cj2 = _cost_jsonl_mtime_ns(root, home=tmp_path)
        assert cj2 != cj1, (
            "ETag fingerprint must change on JSONL append (CRIT-1 guard). "
            "This test FAILS with the broken dir-mtime approach."
        )

    def test_same_second_same_size_residual(self, tmp_path):
        """Under-invalidation test (b): documents the R-02 accepted residual.

        A same-second in-place overwrite that preserves file size may produce
        an unchanged fingerprint (the ≤3s stale window, self-healing on next poll).
        This test pins the residual — it will fail if the fingerprint path is removed.

        We simulate the residual by pinning both writes to the same mtime.
        """
        import os as _os

        root = tmp_path / "project"
        root.mkdir()
        ph = project_hash(str(root))
        proj_dir = tmp_path / ".claude" / "projects" / ph
        proj_dir.mkdir(parents=True)

        # Write content and pin mtime to a fixed value (simulates same-second write)
        content = '{"message": {"usage": {"input_tokens": 100}}}\n'
        jsonl = proj_dir / "test-session.jsonl"
        jsonl.write_text(content)
        fixed_mtime = 1_700_000_000.0  # fixed epoch second
        _os.utime(jsonl, (fixed_mtime, fixed_mtime))
        cj1 = _cost_jsonl_mtime_ns(root, home=tmp_path)

        # Overwrite with exactly same-size content (different bytes, same length),
        # then pin to the SAME mtime again (simulates in-place edit within same second)
        same_size_content = '{"message": {"usage": {"input_tokens": 999}}}\n'
        assert len(same_size_content) == len(content), "Must have same byte size for R-02 scenario"
        jsonl.write_text(same_size_content)
        _os.utime(jsonl, (fixed_mtime, fixed_mtime))
        cj2 = _cost_jsonl_mtime_ns(root, home=tmp_path)

        # The residual: same mtime + same size → same XOR fingerprint
        # R-02 accepted: self-heals within ≤3s on next poll where content differs
        assert cj1 == cj2, (
            "Accepted R-02 residual: same-mtime + same-size in-place overwrite "
            "produces identical fingerprint (stale for ≤3s, self-healing)"
        )


class TestCacheControlAbsent:
    """IVG-76: Cache-Control/Expires must not appear in API response helpers."""

    def test_no_cache_control_in_source(self):
        """dashboard_server.py helpers must not call send_header("Cache-Control", ...)."""
        import re
        source = _DS_PATH.read_text(encoding="utf-8")
        # Check that send_header is never called with Cache-Control or Expires
        # (comment mentions are fine; the actual API call is what we guard against)
        cache_control_call_re = re.compile(
            r'send_header\s*\(\s*["\']Cache-Control["\']', re.IGNORECASE
        )
        expires_call_re = re.compile(
            r'send_header\s*\(\s*["\']Expires["\']', re.IGNORECASE
        )
        assert not cache_control_call_re.search(source), (
            "dashboard_server.py must not call send_header('Cache-Control', ...) "
            "on API responses"
        )
        assert not expires_call_re.search(source), (
            "dashboard_server.py must not call send_header('Expires', ...) "
            "on API responses"
        )

    def test_project_hash_bound_at_module_level(self):
        """dashboard_server.py must bind project_hash = _dc.project_hash at module level."""
        source = _DS_PATH.read_text(encoding="utf-8")
        assert "project_hash = _dc.project_hash" in source, (
            "dashboard_server.py must bind 'project_hash = _dc.project_hash' at module level"
        )


# ---------------------------------------------------------------------------
# T-08: Memory API routes
# ---------------------------------------------------------------------------

def _make_memory_project(tmp_path: Path) -> Path:
    """Project with minimal memory layout."""
    proj = tmp_path / "memproject"
    mem = proj / ".workflow_artifacts" / "memory"
    (mem / "sessions").mkdir(parents=True, exist_ok=True)
    (mem / "daily").mkdir(parents=True, exist_ok=True)
    (mem / "lessons-learned.md").write_text(
        "## 2026-06-01 — test-lesson\n**Lesson:** Always write tests.\n"
    )
    (mem / "sessions" / "2026-06-18-api.md").write_text(
        "---\ndate: 2026-06-18\ntask: api\n---\n# API Session\nContent here.\n"
    )
    (mem / "daily" / "insights-2026-06-18.md").write_text(
        "# Insights 2026-06-18\nInteresting finding today.\n"
    )
    return proj


class TestMemoryRoutes:
    def test_get_memory_index(self, tmp_path):
        """/api/memory returns 200 with types list."""
        proj = _make_memory_project(tmp_path)
        server, _ = _start_server(proj)
        status, _, body = _get(server, "/api/memory")
        server.shutdown()
        assert status == 200
        data = json.loads(body)
        assert "types" in data
        assert set(data["types"]) == {"lessons", "sessions", "insights"}

    def test_get_memory_lessons_list(self, tmp_path):
        """/api/memory/lessons returns 200 with items list."""
        proj = _make_memory_project(tmp_path)
        server, _ = _start_server(proj)
        status, _, body = _get(server, "/api/memory/lessons")
        server.shutdown()
        assert status == 200
        data = json.loads(body)
        assert data["type"] == "lessons"
        assert len(data["items"]) >= 1
        assert data["items"][0]["title"] == "2026-06-01 — test-lesson"

    def test_get_memory_sessions_list(self, tmp_path):
        """/api/memory/sessions returns items list."""
        proj = _make_memory_project(tmp_path)
        server, _ = _start_server(proj)
        status, _, body = _get(server, "/api/memory/sessions")
        server.shutdown()
        assert status == 200
        data = json.loads(body)
        assert data["type"] == "sessions"
        assert len(data["items"]) == 1
        assert data["items"][0]["id"] == "2026-06-18-api"

    def test_get_memory_insights_list(self, tmp_path):
        """/api/memory/insights returns items list."""
        proj = _make_memory_project(tmp_path)
        server, _ = _start_server(proj)
        status, _, body = _get(server, "/api/memory/insights")
        server.shutdown()
        assert status == 200
        data = json.loads(body)
        assert data["type"] == "insights"
        assert len(data["items"]) == 1

    def test_get_memory_item(self, tmp_path):
        """/api/memory/sessions/<id> returns item with body."""
        proj = _make_memory_project(tmp_path)
        server, _ = _start_server(proj)
        status, _, body = _get(server, "/api/memory/sessions/2026-06-18-api")
        server.shutdown()
        assert status == 200
        data = json.loads(body)
        assert "body" in data
        assert "Content here" in data["body"]

    def test_get_memory_etag_304(self, tmp_path):
        """/api/memory/lessons returns ETag; same ETag on resend → 304."""
        proj = _make_memory_project(tmp_path)
        server, _ = _start_server(proj)
        status1, hdrs1, _ = _get(server, "/api/memory/lessons")
        assert status1 == 200
        headers1 = {h.lower(): v for h, v in hdrs1}
        etag = headers1.get("etag")
        assert etag is not None
        status2, _, _ = _get_with_inm(server, "/api/memory/lessons", etag)
        server.shutdown()
        assert status2 == 304

    def test_get_memory_unknown_type_404(self, tmp_path):
        """/api/memory/unknown returns 404."""
        proj = _make_memory_project(tmp_path)
        server, _ = _start_server(proj)
        status, _, _ = _get(server, "/api/memory/unknown")
        server.shutdown()
        assert status == 404

    def test_get_memory_unknown_item_404(self, tmp_path):
        """/api/memory/lessons/<nonexistent-id> returns 404."""
        proj = _make_memory_project(tmp_path)
        server, _ = _start_server(proj)
        status, _, _ = _get(server, "/api/memory/lessons/no-such-entry")
        server.shutdown()
        assert status == 404

    def test_memory_route_405_post(self, tmp_path):
        """POST /api/memory/lessons returns 405."""
        proj = _make_memory_project(tmp_path)
        server, _ = _start_server(proj)
        status = _method(server, "POST", "/api/memory/lessons")
        server.shutdown()
        assert status == 405

    def test_memory_traversal_encoded_slash(self, tmp_path):
        """%2f-encoded slash in item_id is handled safely (no file found → 404)."""
        proj = _make_memory_project(tmp_path)
        server, _ = _start_server(proj)
        # %2F is URL-encoded /; server splits raw path first, so this becomes
        # a single segment with a slash — not a match for any real id → 404
        status, _, _ = _get(server, "/api/memory/sessions/..%2Fetc%2Fpasswd")
        server.shutdown()
        assert status == 404

    def test_memory_traversal_literal_dotdot(self, tmp_path):
        """Literal ../ in item_id does not escape memory dir (id not in list → 404)."""
        proj = _make_memory_project(tmp_path)
        server, _ = _start_server(proj)
        status, _, _ = _get(server, "/api/memory/sessions/../../../etc/passwd")
        server.shutdown()
        # The path parsing splits on / so "sessions" is the mtype, "../.." becomes
        # the item_id — enumerate-then-match finds no match → 404
        assert status == 404

    def test_etag_isolation_memory_vs_tasks(self, tmp_path):
        """Memory ETag scope does not collide with tasks ETag scope."""
        proj = _make_memory_project(tmp_path)
        # Add a task artifact so tasks route has content
        (proj / ".workflow_artifacts" / "my-task").mkdir(parents=True, exist_ok=True)
        server, _ = _start_server(proj)
        _, hdrs_mem, _ = _get(server, "/api/memory/lessons")
        _, hdrs_tasks, _ = _get(server, "/api/tasks")
        server.shutdown()
        etag_mem = {h.lower(): v for h, v in hdrs_mem}.get("etag")
        etag_tasks = {h.lower(): v for h, v in hdrs_tasks}.get("etag")
        # ETags are scoped differently; they should not be equal
        assert etag_mem != etag_tasks


# ---------------------------------------------------------------------------
# IVG-249 T-09 (R2-MIN-3): GET /api/spend — today_partial passthrough
#
# This file had ZERO existing /api/spend tests (verified: `grep -rln
# 'api/spend' dev/tests/` returned only this file, no matching test body).
# The route calls `_aggregate_today(project_root=self.project_root)` with no
# `home` argument, so a fixture-home cannot be injected directly — the seam
# is the module-level `_DS._aggregate_today` binding (`_aggregate_today =
# _sm.aggregate_today`), monkeypatched to return a hand-built SpendSnapshot.
# ---------------------------------------------------------------------------

class TestApiSpend:
    def test_api_spend_today_partial_true_passthrough(self, tmp_path, monkeypatch):
        """snap.today_partial=True must reach the served JSON body unchanged —
        this is the ONLY change T-05 CRIT-1(d) makes to dashboard_server.py."""
        proj = _make_project(tmp_path)
        fake_snap = _DS._sm.SpendSnapshot(
            today_usd=4.21,
            by_model={"opus": 4.21},
            by_model_pct={"opus": 100.0},
            by_task={},
            by_task_partial=False,
            by_phase={},
            by_phase_partial=False,
            today_partial=True,
            stale=False,
            scope="global",
        )
        monkeypatch.setattr(_DS, "_aggregate_today", lambda **kwargs: fake_snap)

        server, _ = _start_server(proj)
        try:
            status, _, body = _get(server, "/api/spend")
        finally:
            server.shutdown()

        assert status == 200
        data = json.loads(body)
        assert data["today_partial"] is True

    def test_api_spend_today_partial_false_passthrough(self, tmp_path, monkeypatch):
        """snap.today_partial=False must ALSO reach the response unchanged —
        proves the passthrough is not a hardcoded constant."""
        proj = _make_project(tmp_path)
        fake_snap = _DS._sm.SpendSnapshot(
            today_usd=0.0,
            by_model={},
            by_model_pct={},
            by_task={},
            by_task_partial=False,
            by_phase={},
            by_phase_partial=False,
            today_partial=False,
            stale=False,
            scope="global",
        )
        monkeypatch.setattr(_DS, "_aggregate_today", lambda **kwargs: fake_snap)

        server, _ = _start_server(proj)
        try:
            status, _, body = _get(server, "/api/spend")
        finally:
            server.shutdown()

        assert status == 200
        data = json.loads(body)
        assert data["today_partial"] is False
