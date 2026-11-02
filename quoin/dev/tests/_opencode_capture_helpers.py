"""Shared, non-collected helpers for the OpenCode security tests.

Everything here stays on loopback. `CaptureServer` stands in for a provider
gateway and keeps only digests of what it receives, `CaptureProxy` records
proxy traffic and refuses to forward it, `secret_forms` / `scan_for_secrets`
look for a seeded secret on any surface, and `EgressGuard` fails a test when a
listener that must stay silent heard anything, when the fake executable blocked
an egress attempt, or when a protected file changed.

Coverage note: `loopback_only` patches sockets inside the pytest process only.
Egress from the fake executable and its children is covered by the fake's own
allowlist plus the `blocked` check in `EgressGuard.check`, not by the patch.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import secrets as stdlib_secrets
import select
import socket
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

import pytest

import _opencode_helpers as helpers

_fake_server = helpers.load_module(
    helpers.OPENCODE_DIR / "fake_openai_server.py", "fake_openai_server_capture"
)

WORK_LABEL = "work-gateway"
PERSONAL_LABEL = "personal-provider"

# The work provider the delivery controls name. Its credential reference is a
# keychain entry, so its compiled variable can only be filled from the
# resolver; `corp-gw-b` reads an environment variable of the same name as its
# compiled variable, which would not prove resolver delivery as cleanly.
WORK_PROVIDER_ID = "corp-gw"
WORK_PROVIDERS = ("corp-gw", "corp-gw-b")

_MINIMAL_SCENARIOS = {"scenarios": {"default_ok": {"turns": [{"status": 200}]}}}


def _sha256(value: Union[str, bytes]) -> str:
    data = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# capture listeners
# ---------------------------------------------------------------------------


class CaptureServer(_fake_server.FakeProviderServer):
    """A loopback gateway that answers every POST with a canned 200 and keeps
    `{method, path, model, body_sha256, auth_sha256}` per request, never the
    body or the raw Authorization value."""

    def __init__(self, label: str = WORK_LABEL) -> None:
        super().__init__(scenarios=copy.deepcopy(_MINIMAL_SCENARIOS), host="127.0.0.1", port=0)
        self.label = label

    def captures(self) -> List[Dict[str, Any]]:
        return self.snapshot_requests()

    def _record(self, method, path, scenario, turn_index, body, headers, outcome, body_size=None):
        # Only digests are stored; the base class would keep the parsed body.
        raise AssertionError("CaptureServer records through _handle only")

    def _store(self, method: str, path: str, model: Optional[str], raw_body: bytes, auth: Optional[str]) -> None:
        entry = {
            "method": method,
            "path": path,
            "model": model,
            "body_sha256": _sha256(raw_body),
            "auth_sha256": _sha256(auth) if auth is not None else None,
        }
        with self._lock:
            self._requests.append(entry)

    def _handle(self, handler: BaseHTTPRequestHandler, method: str) -> None:
        path = handler.path.split("?", 1)[0]
        length = int(handler.headers.get("Content-Length") or 0)
        raw = handler.rfile.read(length) if length else b""
        model = None
        if raw:
            try:
                parsed = json.loads(raw.decode("utf-8"))
                if isinstance(parsed, dict) and isinstance(parsed.get("model"), str):
                    model = parsed["model"]
            except (UnicodeDecodeError, ValueError):
                model = None
        auth = None
        for key, value in handler.headers.items():
            if key.lower() == "authorization":
                auth = value
        self._store(method, path, model, raw, auth)
        if method == "POST":
            reply = {
                "id": "chatcmpl-capture",
                "object": "chat.completion",
                "created": 0,
                "model": model or "capture",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
            self._write_json(handler, 200, reply)
        else:
            self._write_json(handler, 200, {"object": "list", "data": []})


class CaptureProxy:
    """A loopback proxy that records `{method, target}` and answers 403 for
    everything, except a `CONNECT host:port` listed in `tunnel_to` (a mapping
    from the requested target to a loopback `(host, port)`), which it splices."""

    def __init__(self, tunnel_to: Optional[Mapping[str, Tuple[str, int]]] = None) -> None:
        self.tunnel_to: Dict[str, Tuple[str, int]] = dict(tunnel_to or {})
        for host, _port in self.tunnel_to.values():
            if host not in ("127.0.0.1", "localhost", "::1"):
                raise ValueError("a tunnel may only lead to loopback")
        self._lock = threading.Lock()
        self._records: List[Dict[str, str]] = []
        self._stop = threading.Event()
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, fmt, *args):  # noqa: A003 - stdlib signature
                pass

            def _refuse(self) -> None:
                proxy._add(self.command, self.path)
                self.send_response(403)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_CONNECT(self):  # noqa: N802
                proxy._add("CONNECT", self.path)
                destination = proxy.tunnel_to.get(self.path)
                if destination is None:
                    self.send_response(403)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                try:
                    remote = socket.create_connection(destination, timeout=3)
                except OSError:
                    self.send_response(502)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200, "Connection established")
                self.end_headers()
                self.wfile.flush()
                proxy._splice(self.connection, remote)

            do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = do_PATCH = do_OPTIONS = _refuse  # noqa: N815

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d" % self.port

    def _add(self, method: str, target: str) -> None:
        with self._lock:
            self._records.append({"method": method, "target": target})

    def records(self) -> List[Dict[str, str]]:
        with self._lock:
            return copy.deepcopy(self._records)

    def _splice(self, client: socket.socket, remote: socket.socket) -> None:
        pair = [client, remote]
        try:
            while not self._stop.is_set():
                ready, _, _ = select.select(pair, [], [], 0.2)
                for source in ready:
                    payload = source.recv(4096)
                    if not payload:
                        return
                    (remote if source is client else client).sendall(payload)
        except OSError:
            pass
        finally:
            try:
                remote.close()
            except OSError:
                pass

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)

    def __enter__(self) -> "CaptureProxy":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()


# ---------------------------------------------------------------------------
# seeded secrets
# ---------------------------------------------------------------------------


def make_seed(prefix: str = "") -> str:
    """A random secret generated at test time."""
    return prefix + stdlib_secrets.token_hex(16)


def secret_forms(seed: str) -> Dict[str, str]:
    """The raw value plus each form a leak could take, by name. The two
    fragments are only ever matched as substrings."""
    forms = {
        "raw": seed,
        "base64": base64.b64encode(seed.encode("utf-8")).decode("ascii"),
        "base64url": base64.urlsafe_b64encode(seed.encode("utf-8")).decode("ascii"),
        "url-encoded": urllib.parse.quote(seed, safe=""),
        "json-escaped": json.dumps(seed)[1:-1],
    }
    if len(seed) >= 16:
        forms["head8"] = seed[:8]
        forms["tail8"] = seed[-8:]
    return forms


Corpus = Union[Path, str, Tuple[str, str]]


def _walk(path: Path, exclude: Sequence[Path]) -> Iterable[Path]:
    if os.path.islink(path):
        return
    if any(path == e or e in path.parents for e in exclude):
        return
    if path.is_file():
        yield path
        return
    if path.is_dir():
        for base, dirs, names in os.walk(str(path), followlinks=False):
            base_path = Path(base)
            dirs[:] = sorted(
                d for d in dirs
                if not os.path.islink(base_path / d)
                and not any((base_path / d) == e for e in exclude)
            )
            for name in sorted(names):
                file_path = base_path / name
                if os.path.islink(file_path) or any(file_path == e for e in exclude):
                    continue
                yield file_path


def scan_for_secrets(
    corpus: Iterable[Corpus], forms: Mapping[str, str], *, exclude: Sequence[Path] = ()
) -> List[Tuple[str, str]]:
    """Return `(where, form name)` for every form found in the corpus.

    Items are files or directories (symlinks are never followed), or
    `(label, text)` pairs. The corpus must hold at least one non-empty file or
    text: a scan of nothing proves nothing."""
    excluded = [Path(e) for e in exclude]
    hits: List[Tuple[str, str]] = []
    scanned_bytes = 0
    scanned_items = 0

    def check(where: str, text: str) -> None:
        for name, form in forms.items():
            if form and form in text:
                hits.append((where, name))

    for item in corpus:
        if isinstance(item, tuple):
            label, text = item
            scanned_items += 1
            scanned_bytes += len(text)
            check(label, text)
            continue
        for file_path in _walk(Path(item), excluded):
            data = file_path.read_bytes()
            scanned_items += 1
            scanned_bytes += len(data)
            check(str(file_path), data.decode("utf-8", errors="replace"))
    assert scanned_items > 0 and scanned_bytes > 0, "the secret scan had an empty corpus"
    return hits


# ---------------------------------------------------------------------------
# egress and write guard
# ---------------------------------------------------------------------------


def _snapshot(paths: Iterable[Path]) -> Dict[str, Tuple[str, int]]:
    state: Dict[str, Tuple[str, int]] = {}
    for root in paths:
        root = Path(root)
        if not os.path.lexists(root):
            state[str(root)] = ("absent", 0)
            continue
        files = [root] if root.is_file() or os.path.islink(root) else sorted(_walk(root, ()))
        if not files:
            state[str(root)] = ("empty", 0)
        for file_path in files:
            if os.path.islink(file_path):
                state[str(file_path)] = ("link:" + os.readlink(file_path), 0)
                continue
            stat = file_path.stat()
            state[str(file_path)] = (_sha256(file_path.read_bytes()), stat.st_mtime_ns)
    return state


class EgressGuard:
    """Holds the two gateways and the proxy, and checks at the end of a test
    that nothing reached the wrong place."""

    def __init__(self, allow_personal: bool = False) -> None:
        self.work = CaptureServer(WORK_LABEL)
        self.personal = CaptureServer(PERSONAL_LABEL)
        self.proxy = CaptureProxy()
        self.allow_personal = allow_personal
        self.allow_proxy = False
        self._protected: Dict[str, Tuple[str, int]] = {}
        self._network_logs: List[Tuple[Path, bool]] = []
        self._roots: List[Path] = []
        self.skip_teardown_check = False

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> "EgressGuard":
        self.work.start()
        self.personal.start()
        self.proxy.start()
        return self

    def stop(self) -> None:
        for part in (self.work, self.personal, self.proxy):
            part.stop()

    # -- configuration -------------------------------------------------------
    def allowed_targets(self) -> List[str]:
        """Loopback `host:port` pairs the test permits: the work gateway
        always; the personal listener and the proxy only on request."""
        targets = ["127.0.0.1:%d" % self.work.port]
        if self.allow_personal:
            targets.append("127.0.0.1:%d" % self.personal.port)
        if self.allow_proxy:
            targets.append("127.0.0.1:%d" % self.proxy.port)
        return targets

    def protect(self, *paths: Path) -> None:
        """Snapshot paths that must stay unchanged until `check`."""
        self._roots.extend(Path(p) for p in paths)
        self._protected.update(_snapshot(paths))

    def watch_network_log(self, path: Path, *, required: bool = False) -> None:
        """Register the fake's `network.jsonl`. With `required`, a missing log
        fails the check: a test that ran network verbs must have left one."""
        self._network_logs.append((Path(path), required))

    # -- verdict -------------------------------------------------------------
    def check(self) -> None:
        problems: List[str] = []
        if not self.allow_personal and self.personal.captures():
            problems.append("the personal listener received %d request(s)" % len(self.personal.captures()))
        if not self.allow_proxy and self.proxy.records():
            problems.append("the proxy recorded %d request(s)" % len(self.proxy.records()))
        for path, required in self._network_logs:
            if not path.exists():
                if required:
                    problems.append("network log %s is missing" % path.name)
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    problems.append("network log %s line %d is not JSON" % (path.name, number))
                    continue
                if isinstance(record, dict) and record.get("blocked") is True:
                    problems.append("network log %s line %d holds a blocked egress attempt" % (path.name, number))
        if self._protected:
            now = _snapshot(self._roots)
            for key, before in self._protected.items():
                if now.get(key) != before:
                    problems.append("protected path changed: %s" % key)
            for key in now:
                if key not in self._protected:
                    problems.append("protected path gained a file: %s" % key)
        assert not problems, "; ".join(problems)


def with_allowed_targets(scenario: Any, targets: Sequence[str]) -> Dict[str, Any]:
    """The scenario dict with a top-level `allowed_targets` list. The scenario
    path is baked into the shim, so the list survives the launcher's
    environment scrub; an environment variable would not."""
    import _opencode_driver_helpers as dh

    doc = copy.deepcopy(dh.scenario_dict(scenario))
    doc["allowed_targets"] = list(targets)
    return doc


@pytest.fixture
def egress_guard(monkeypatch):
    """The capture listeners with `loopback_only` active; the check runs at
    teardown. A test that expects a failure calls `check` itself under
    `pytest.raises` and sets `skip_teardown_check` afterwards."""
    helpers.install_loopback_guard(monkeypatch)
    guard = EgressGuard().start()
    try:
        yield guard
        if not guard.skip_teardown_check:
            guard.check()
    finally:
        guard.stop()


# ---------------------------------------------------------------------------
# world helpers
# ---------------------------------------------------------------------------


def bind_work_providers(world: Any, server: CaptureServer) -> None:
    """Point both work providers at `server`, approve loopback, and rewrite
    the profile and its qualification records (a record is built from the
    provider's `base_url`)."""
    base_url = "http://127.0.0.1:%d/v1" % server.port
    providers = world.profile["providers"]
    for provider_id in WORK_PROVIDERS:
        providers[provider_id]["base_url"] = base_url
    policy = world.profile.setdefault("policy", {})
    hosts = list(policy.get("allowed_hosts") or [])
    if "127.0.0.1" not in hosts:
        hosts.append("127.0.0.1")
    policy["allowed_hosts"] = hosts
    world.write()
    world.write_records()


def install_personal_profile(world: Any, seed: str) -> Path:
    """Add the personal profile to the world's home, create its directories,
    plant `seed` in its credential file and return that file's path."""
    from _opencode_merge_helpers import PROFILE_PERSONAL, _install, write_probe_records
    from quoin.opencode_adapter import config, merge, profile_dirs

    name = _install(world.env, world.home, PROFILE_PERSONAL)
    empty_project = Path(world.tmp) / "personal-empty-project"
    empty_project.mkdir(parents=True, exist_ok=True)
    effective = merge.merge(
        config.load_all(project_root=empty_project, profile=name, env=world.env, home=world.home)
    )
    write_probe_records(world.env, world.home, effective)
    dirs = profile_dirs.prepare(profile_dirs.adapter_root(world.env, world.home), name, "personal")
    auth = Path(dirs.data) / "opencode" / "auth.json"
    auth.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(auth.parent, 0o700)
    auth.write_text(json.dumps({"openrouter": {"type": "api", "key": seed}}), encoding="utf-8")
    os.chmod(auth, 0o600)
    return auth
