#!/usr/bin/env python3
"""dashboard_server.py — ThreadingHTTPServer for the quoin workflow dashboard.

Serves:
  GET /api/tasks                     → scan_tasks() JSON
  GET /api/tasks/<name>              → task_detail() JSON
  GET /api/spend                     → aggregate_today() snapshot JSON (T-06)
  GET /                              → index.html
  GET /dashboard.css                 → dashboard.css
  GET /app.js                        → app.js
  POST/PUT/DELETE/PATCH any path     → 405 Method Not Allowed
  anything else                      → 404

Security: binds 127.0.0.1 only; GET-only for data; fixed asset allowlist (no path join).
"""
from __future__ import annotations

import argparse
import http.server
import importlib.util
import json
import os
import signal
import socketserver
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import unquote, urlparse, parse_qs

# ---------------------------------------------------------------------------
# Cross-load portable dashboard_model (D-02: parents[1]/core/scripts/)
# ---------------------------------------------------------------------------

def _load_module(module_key: str, path: Path):
    """Load a module from an absolute path, registering in sys.modules."""
    if module_key in sys.modules:
        return sys.modules[module_key]
    spec = importlib.util.spec_from_file_location(module_key, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot create spec for {module_key} at {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_key] = mod
    spec.loader.exec_module(mod)
    return mod


_CORE_SCRIPTS = Path(__file__).resolve().parents[1] / "core" / "scripts"
_SCRIPTS_DIR = Path(__file__).resolve().parent

_dm = _load_module(
    "_dashboard_server_model",
    _CORE_SCRIPTS / "dashboard_model.py",
)
scan_tasks = _dm.scan_tasks
task_detail = _dm.task_detail
compute_version_token = _dm.compute_version_token
list_memory = _dm.list_memory
read_memory_item = _dm.read_memory_item
memory_version_key = _dm.memory_version_key
MEMORY_TYPES = _dm.MEMORY_TYPES

# Load adapter cost provider (same-dir sibling, D-03)
_dc = _load_module(
    "_dashboard_server_cost",
    _SCRIPTS_DIR / "dashboard_cost.py",
)
make_cost_provider = _dc.make_cost_provider
project_hash = _dc.project_hash          # Required: _cost_jsonl_mtime_ns uses this bare name

# Load portable spend_monitor (T-06: /api/spend endpoint)
_sm = _load_module(
    "_dashboard_server_spend_monitor",
    _CORE_SCRIPTS / "spend_monitor.py",
)
_aggregate_today = _sm.aggregate_today

# ---------------------------------------------------------------------------
# Cost-freshness helper (T-04b — ADAPTER LOCAL: dashboard_server.py only)
# ---------------------------------------------------------------------------

def _cost_jsonl_mtime_ns(project_root: Path, home: Path | None = None) -> int:
    """Return a fingerprint int derived from the project's JSONL session files.

    Globs ~/.claude/projects/<project_hash>/*.jsonl and combines:
      max(st_mtime_ns), file count, total size via XOR.

    Changes whenever a JSONL file is appended to (mtime + size change),
    a new session file is created (count change), or a file is deleted.

    Returns 0 on empty directory, missing directory, or any OSError
    (fail-open: degrades to artifact-only ETag — no crash).

    The ``home`` parameter defaults to Path.home() when None, enabling
    deterministic unit tests via a synthetic tmp-based HOME.

    Catches OSError only — NOT bare Exception — so NameError/AttributeError
    surfaces in tests and is not silently swallowed.
    """
    try:
        ph = project_hash(str(project_root))
        proj_dir = (home or Path.home()) / ".claude" / "projects" / ph
        jsonl_files = list(proj_dir.glob("*.jsonl"))
        if not jsonl_files:
            return 0
        max_mtime = max(p.stat().st_mtime_ns for p in jsonl_files)
        fcount = len(jsonl_files)
        total_sz = sum(p.stat().st_size for p in jsonl_files)
        return max_mtime ^ (fcount * 1_000_000_000) ^ total_sz
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# Asset serving (D-07: fixed allowlist, no path join)
# ---------------------------------------------------------------------------

_ASSETS_DIR = _CORE_SCRIPTS / "dashboard_assets"

_ASSET_ALLOWLIST = {
    "/":             "index.html",
    "/index.html":   "index.html",
    "/dashboard.css": "dashboard.css",
    "/app.js":       "app.js",
    "/memory.js":    "memory.js",
}

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css":  "text/css; charset=utf-8",
    ".js":   "application/javascript; charset=utf-8",
}


# ---------------------------------------------------------------------------
# Shutdown state (shared between main thread and signal handler)
# ---------------------------------------------------------------------------

_stop_event = threading.Event()
_server_ref: list = []  # [server_instance] once bound


# ---------------------------------------------------------------------------
# Request handler (D-07, D-08)
# ---------------------------------------------------------------------------

class DashboardHandler(BaseHTTPRequestHandler):
    """Handles GET requests for the dashboard API and static assets."""

    # Project root injected at server startup (D-15 shared root invariant)
    project_root: Path = None  # type: ignore[assignment]
    # NOTE (stage-4 T-12 fix): cost_provider MUST be assigned as
    # staticmethod(fn), never a bare function. A plain function object is a
    # descriptor — assigning it directly to a class attribute causes
    # `self.cost_provider` to auto-bind as a method (injecting `self` as an
    # extra first positional arg), which raises TypeError on every call.
    # dashboard_model._task_summary swallows that TypeError
    # (`except Exception: ... using counts mode`), so this previously
    # degraded EVERY real (non-None) cost_provider to silent counts-mode —
    # discovered via the T-12 end-to-end propagation test, which wires a
    # real provider through the actual HTTP server for the first time.
    cost_provider = None

    def log_message(self, format, *args):
        """Suppress all access log output — URL= line is the only reliable stdout marker."""
        pass

    def _send_json(self, code: int, data, etag: str | None = None) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if etag is not None:
            self.send_header("ETag", etag)
        # NOTE: Cache-Control and Expires are intentionally NOT set here.
        # The JS-managed If-None-Match/304 mechanism is the sole conditional
        # caching layer. Adding Cache-Control would allow the browser to
        # transparently serve 200 from its cache, masking 304 from onload.
        self.end_headers()
        self.wfile.write(body)

    def _send_not_modified(self, etag: str) -> None:
        """Send a 304 Not Modified response with ETag header and no body.

        Per RFC 7232 §4.1, a 304 response MUST NOT contain a message body.
        Content-Length: 0 is explicit to avoid keep-alive ambiguity.
        Cache-Control and Expires are intentionally omitted (same policy as
        _send_json — If-None-Match is the sole conditional mechanism).
        """
        self.send_response(304)
        self.send_header("ETag", etag)
        self.send_header("Content-Length", "0")
        self.end_headers()
        # No body written — 304 must not carry a message body

    def _send_asset(self, filename: str) -> None:
        path = _ASSETS_DIR / filename
        try:
            content = path.read_bytes()
        except (IOError, OSError):
            self._send_json(404, {"error": "asset not found"})
            return
        suffix = Path(filename).suffix
        content_type = _CONTENT_TYPES.get(suffix, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            path = parsed.path

            if path == "/api/tasks" or path == "/api/tasks/":
                qs = parse_qs(parsed.query)
                include_fin_str = qs.get("include_finalized", ["false"])[0].lower()
                include_finalized = include_fin_str in ("true", "1", "yes")

                # T-02: ETag / 304 short-circuit for /api/tasks
                # _cost_jsonl_mtime_ns is called on every request (including the 304
                # path) because the token must be fresh to correctly identify whether
                # a 304 is valid.  The glob+stat walk over JSONL files is O(session-
                # file-count in ~/.claude/projects/<hash>/) — typically a handful of
                # files; comparable cost to the existing per-request artifact stat-walk.
                cj = _cost_jsonl_mtime_ns(self.project_root)
                scope = f"tasks:fin={include_finalized}|cj={cj}"
                etag = compute_version_token(self.project_root, scope)
                inm = self.headers.get("If-None-Match")
                if inm is not None and inm == etag:
                    self._send_not_modified(etag)
                    return

                result = scan_tasks(
                    self.project_root,
                    include_finalized=include_finalized,
                    cost_provider=self.cost_provider,
                )
                self._send_json(200, result, etag=etag)
                return

            if path.startswith("/api/tasks/"):
                name = unquote(path[len("/api/tasks/"):])
                if not name:
                    self._send_json(404, {"error": "task not found"})
                    return

                # T-03: ETag / 304 short-circuit for /api/tasks/<name>
                # Token is computed over the whole artifacts tree (cheap, single walk),
                # scoped by task name and cost freshness.  Intentionally coarse — any
                # artifact change anywhere bumps every task's detail token.  This is
                # acceptable: the cost we eliminate is the full task_detail + JSON
                # serialize, and a whole-tree walk is already what the watcher does
                # every 2s.  Per-task-subtree scoping is rejected as premature
                # optimization (see D-02 in plan).
                cj = _cost_jsonl_mtime_ns(self.project_root)
                scope = f"task:{name}|cj={cj}"
                etag = compute_version_token(self.project_root, scope)
                inm = self.headers.get("If-None-Match")
                if inm is not None and inm == etag:
                    self._send_not_modified(etag)
                    return

                try:
                    result = task_detail(
                        self.project_root,
                        name,
                        cost_provider=self.cost_provider,
                    )
                    self._send_json(200, result, etag=etag)
                except KeyError:
                    self._send_json(404, {"error": "task not found"})
                return

            # Memory routes — MUST be checked before the /api/ 404 fallback
            if path == "/api/memory" or path.startswith("/api/memory/"):
                mv = memory_version_key(self.project_root)

                if path == "/api/memory":
                    scope = f"memory:index|mv={mv}"
                    etag = compute_version_token(self.project_root, scope)
                    inm = self.headers.get("If-None-Match")
                    if inm is not None and inm == etag:
                        self._send_not_modified(etag)
                        return
                    self._send_json(200, {"types": sorted(MEMORY_TYPES)}, etag=etag)
                    return

                # Split raw path segments BEFORE unquote (CRIT-1 path-traversal defence)
                tail = path[len("/api/memory/"):]
                parts = tail.split("/")
                mtype = unquote(parts[0])
                if mtype not in MEMORY_TYPES:
                    self._send_json(404, {"error": "unknown memory type"})
                    return

                if len(parts) == 1:
                    scope = f"memory:{mtype}|mv={mv}"
                    etag = compute_version_token(self.project_root, scope)
                    inm = self.headers.get("If-None-Match")
                    if inm is not None and inm == etag:
                        self._send_not_modified(etag)
                        return
                    items = list_memory(self.project_root, mtype)
                    self._send_json(200, {"type": mtype, "items": items}, etag=etag)
                    return

                # item-level route
                item_id = unquote(parts[1])
                scope = f"memory:{mtype}/{item_id}|mv={mv}"
                etag = compute_version_token(self.project_root, scope)
                inm = self.headers.get("If-None-Match")
                if inm is not None and inm == etag:
                    self._send_not_modified(etag)
                    return
                try:
                    item = read_memory_item(self.project_root, mtype, item_id)
                    self._send_json(200, item, etag=etag)
                except KeyError:
                    self._send_json(404, {"error": "memory item not found"})
                return

            if path == "/api/spend":
                # ETag uses JSONL mtime (spend changes when sessions change)
                cj = _cost_jsonl_mtime_ns(self.project_root)
                scope = f"spend:cj={cj}"
                etag = compute_version_token(self.project_root, scope)
                inm = self.headers.get("If-None-Match")
                if inm is not None and inm == etag:
                    self._send_not_modified(etag)
                    return
                snap = _aggregate_today(project_root=self.project_root)
                attributed_usd = round(sum(snap.by_phase.values()), 6)
                unattributed_usd = round(max(0.0, snap.today_usd - attributed_usd), 6)
                data = {
                    "today_usd": snap.today_usd,
                    "by_model": snap.by_model,
                    "by_model_pct": snap.by_model_pct,
                    "by_task": snap.by_task,
                    "by_task_partial": snap.by_task_partial,
                    "by_phase": snap.by_phase,
                    "by_phase_partial": snap.by_phase_partial,
                    "today_partial": snap.today_partial,
                    "attributed_usd": attributed_usd,
                    "unattributed_usd": unattributed_usd,
                    "scope": snap.scope,
                    "stale": snap.stale,
                }
                self._send_json(200, data, etag=etag)
                return

            if path.startswith("/api/"):
                self._send_json(404, {"error": "not found"})
                return

            # Asset routing (fixed allowlist — no path join)
            filename = _ASSET_ALLOWLIST.get(path)
            if filename is not None:
                self._send_asset(filename)
                return

            self._send_json(404, {"error": "not found"})

        except Exception as exc:
            try:
                self._send_json(500, {"error": "internal server error"})
            except Exception:
                pass

    def do_POST(self):
        self._method_not_allowed()

    def do_PUT(self):
        self._method_not_allowed()

    def do_DELETE(self):
        self._method_not_allowed()

    def do_PATCH(self):
        self._method_not_allowed()

    def _method_not_allowed(self):
        self._send_json(405, {"error": "method not allowed"})


# ---------------------------------------------------------------------------
# ThreadingHTTPServer subclass (127.0.0.1 bind)
# ---------------------------------------------------------------------------

class DashboardServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """Thread-per-request HTTP server bound to loopback."""
    daemon_threads = True
    allow_reuse_address = True


# ---------------------------------------------------------------------------
# Port binding helper (D-01)
# ---------------------------------------------------------------------------

def _bind_server(port: int, explicit: bool, handler_class) -> DashboardServer:
    """Bind to port, auto-incrementing from 8787 when using the default port.

    explicit=True: fail immediately on in-use port (exit 2).
    explicit=False (default 8787): try 8787..8796 (cap 10).
    port==0: ask the OS for an ephemeral port.
    """
    if port == 0:
        return DashboardServer(("127.0.0.1", 0), handler_class)

    if explicit:
        try:
            return DashboardServer(("127.0.0.1", port), handler_class)
        except OSError:
            print(
                f"dashboard_server: port {port} is already in use "
                "(explicit --port; no auto-increment)",
                file=sys.stderr,
            )
            sys.exit(2)

    # Default path: auto-increment
    max_tries = 10
    for p in range(port, port + max_tries):
        try:
            return DashboardServer(("127.0.0.1", p), handler_class)
        except OSError:
            continue
    print(
        f"dashboard_server: no free port found in {port}..{port + max_tries - 1}",
        file=sys.stderr,
    )
    sys.exit(2)


# ---------------------------------------------------------------------------
# Project root resolution (D-06 / D-15)
# ---------------------------------------------------------------------------

def _find_project_root(start: Path) -> Path:
    """Walk up from start to find a directory containing .workflow_artifacts/."""
    candidate = start.resolve()
    for _ in range(20):
        if (candidate / ".workflow_artifacts").is_dir():
            return candidate
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    return start.resolve()


# ---------------------------------------------------------------------------
# Watcher thread (D-09)
# ---------------------------------------------------------------------------

def _make_watcher(artifacts_dir: Path, provider, stop_event: threading.Event):
    """Return a daemon thread that polls artifacts_dir every ~2s and pre-warms the provider."""

    def _watch():
        last_mtime = None
        while not stop_event.wait(2.0):
            try:
                if artifacts_dir.is_dir():
                    current = max(
                        (p.stat().st_mtime for p in artifacts_dir.rglob("*") if p.is_file()),
                        default=None,
                    )
                    if current is not None and current != last_mtime:
                        last_mtime = current
                        # Pre-warm: call provider with empty rows (touches the cache)
                        try:
                            provider("_watcher_prewarm", [])
                        except Exception:
                            pass
            except Exception:
                pass

    t = threading.Thread(target=_watch, daemon=True, name="dashboard-watcher")
    return t


# ---------------------------------------------------------------------------
# main(argv)
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="dashboard_server.py",
        description="quoin workflow dashboard HTTP server (127.0.0.1 only)",
    )
    parser.add_argument(
        "--port", type=int, default=8787,
        help="Port to listen on (default 8787; 0 = ephemeral OS port)",
    )
    parser.add_argument(
        "--no-browser", action="store_true",
        help="Do not open a browser window after startup",
    )
    parser.add_argument(
        "--project-root", default=".",
        help="Project root to scan (default: walk up from cwd to .workflow_artifacts/)",
    )
    parser.add_argument(
        "--url-file", default=None,
        help="Write bound URL to this file atomically after server starts (used by agentdesk poller)",
    )

    args = parser.parse_args(argv)

    # Resolve project root (D-15)
    raw_root = Path(args.project_root).resolve()
    if (raw_root / ".workflow_artifacts").is_dir():
        project_root = raw_root
    else:
        project_root = _find_project_root(raw_root)

    # Build cost provider once at startup (D-03/D-05)
    provider = make_cost_provider(project_root)

    # Inject into handler class (class-level, thread-safe for reads).
    # cost_provider MUST be wrapped in staticmethod() — see the class-body
    # NOTE above — otherwise `self.cost_provider` auto-binds as a method and
    # every call silently raises, degrading to counts-mode (stage-4 T-12 fix).
    DashboardHandler.project_root = project_root
    DashboardHandler.cost_provider = staticmethod(provider)

    # Determine if port was explicitly set (not the default 8787)
    explicit_port = args.port != 8787 and args.port != 0

    # Bind server (D-01)
    server = _bind_server(args.port, explicit=explicit_port, handler_class=DashboardHandler)
    _server_ref.append(server)

    actual_port = server.server_address[1]
    url = f"http://127.0.0.1:{actual_port}"

    # D-09 lifecycle ordering: bind → print URL → url-file → browser → watcher → signals → serve
    print(f"URL={url}", flush=True)

    # T-04: Write bound URL to url-file atomically so the agentdesk foreground poller
    # can discover it without racing on a partial write. Uses os.replace (atomic rename).
    if args.url_file:
        import tempfile
        url_file_path = args.url_file
        url_file_dir = os.path.dirname(url_file_path) or "."
        try:
            fd, tmp_path = tempfile.mkstemp(dir=url_file_dir, prefix=".urltmp-")
            try:
                with os.fdopen(fd, "w") as fh:
                    fh.write(url + "\n")
                os.replace(tmp_path, url_file_path)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception as exc:
            print(f"dashboard_server: warning: could not write url-file {url_file_path!r}: {exc}", file=sys.stderr)

    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    # Daemon watcher thread (D-09/D-16: starts after URL line, non-blocking)
    artifacts_dir = project_root / ".workflow_artifacts"
    watcher = _make_watcher(artifacts_dir, provider, _stop_event)
    watcher.start()

    # Signal handlers — must be installed AFTER URL line (D-09)
    def _shutdown_handler(signum, frame):
        _stop_event.set()
        # Shutdown from a separate thread to avoid deadlocking serve_forever
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _shutdown_handler)
    signal.signal(signal.SIGTERM, _shutdown_handler)
    # T-01/T-02: zellij pane close delivers SIGHUP to the PTY foreground process group.
    # The dashboard pane uses `exec python3 ...` so Python is the direct foreground
    # process — SIGHUP reaches us without an intermediate zsh waiter.
    # Guard with hasattr keeps this import-safe on Windows (no SIGHUP there).
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _shutdown_handler)

    # Blocking: serve until SIGINT/SIGTERM
    server.serve_forever()
    watcher.join(timeout=3.0)


if __name__ == "__main__":
    main()
