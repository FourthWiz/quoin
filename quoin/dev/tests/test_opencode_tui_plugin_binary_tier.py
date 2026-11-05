"""A terminal interface plugin loading in the pinned OpenCode binary.

Starts the `opencode` terminal interface under a pseudo-terminal in a throwaway
home, with a loopback provider fake and `OPENCODE_TUI_CONFIG` pointing at a
`tui.json` whose `plugin` list names one pre-compiled `file://` probe module.
The probe appends a marker line when it activates, recording whether its
`solid-js` and `@opentui/solid` imports resolved against the host, and another
when the host draws its `sidebar_content` slot. Skips unless the pinned binary
is on PATH.

Equivalent manual command, from a scratch project directory with the same
isolated HOME/XDG variables:
    QUOIN_TUI_PROBE_MARKER=<marker> OPENCODE_TUI_CONFIG=<dir>/tui.json opencode --prompt hello
"""
from __future__ import annotations

import fcntl
import json
import os
import pty
import select
import shutil
import signal
import struct
import subprocess
import termios
import time
from pathlib import Path

import pytest

import _opencode_helpers as helpers

PROBE = Path(__file__).resolve().parent / "fixtures" / "opencode" / "tui-probe-plugin.mjs"
PROBE_ID = "quoin-tui-probe"


def _skip_reason():
    import test_opencode_binary_contract as contract

    return contract.skip_reason(contract._OPENCODE_BIN, contract._QUALIFIED_VERSIONS, contract._BINARY_VERSION)  # noqa: SLF001


pytestmark = pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or "")

_USAGE = {"completion_tokens": 5, "prompt_tokens": 10, "total_tokens": 15}


def _marker_records(marker):
    if not marker.exists():
        return []
    out = []
    for line in marker.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def _drain(fd, sink, until, predicate):
    """Read the pty into `sink` until `predicate()` holds or `until` passes.
    The child blocks once the pty buffer fills, so it must be drained."""
    while time.monotonic() < until:
        if predicate():
            return True
        ready, _, _ = select.select([fd], [], [], 0.2)
        if ready:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                return predicate()
            if not chunk:
                return predicate()
            sink.write(chunk)
    return predicate()


def run_tui(tmp_path, timeout=60.0):
    """Start the terminal interface with the probe; return (marker records, transcript path)."""
    fake = helpers.load_module(helpers.OPENCODE_DIR / "fake_openai_server.py", "fake_openai_server_tui")
    home, xdg, project = tmp_path / "home", tmp_path / "xdg", tmp_path / "proj"
    for d in (home, xdg, project / ".git"):
        d.mkdir(parents=True)
    marker = tmp_path / "probe-marker.jsonl"
    transcript = tmp_path / "pty.out"
    plugin_dir = tmp_path / "tui-plugin"
    plugin_dir.mkdir()
    probe = plugin_dir / PROBE.name
    shutil.copyfile(PROBE, probe)
    tui_config = tmp_path / "tui.json"
    tui_config.write_text(json.dumps({"plugin": [probe.as_uri()]}), encoding="utf-8")
    scenarios = {"schema_version": 1, "scenarios": {"reply": {"turns": [{
        "depth": 0, "finish_reason": "stop", "status": 200, "stream": True, "usage": _USAGE,
        "chunks": [{"role": "assistant", "content": "probe reply"}],
    }]}}}
    with fake.FakeProviderServer(scenarios=scenarios, host="127.0.0.1", port=0) as provider:
        base_url = provider.base_url if provider.base_url.endswith("/v1") else provider.base_url + "/v1"
        config = {
            "$schema": "https://opencode.ai/config.json",
            "provider": {"quoin-fake": {
                "npm": "@ai-sdk/openai-compatible",
                "options": {"baseURL": base_url, "apiKey": "loopback-only-value"},
                "models": {"reply": {"name": "reply", "tool_call": True}},
            }},
            "model": "quoin-fake/reply",
            "small_model": "quoin-fake/reply",
        }
        (project / "opencode.json").write_text(json.dumps(config), encoding="utf-8")
        env = {
            key: value for key, value in os.environ.items()
            if not key.startswith("OPENCODE_")
        }
        env.update({
            "HOME": str(home), "XDG_CONFIG_HOME": str(xdg),
            "XDG_DATA_HOME": str(tmp_path / "data"), "XDG_CACHE_HOME": str(tmp_path / "cache"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "OPENCODE_DISABLE_AUTOUPDATE": "1",
            "OPENCODE_TUI_CONFIG": str(tui_config),
            "QUOIN_TUI_PROBE_MARKER": str(marker),
            "TERM": "xterm-256color", "COLUMNS": "200", "LINES": "50",
        })
        master, slave = pty.openpty()
        # Wide enough that the session view shows its sidebar.
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 200, 0, 0))
        proc = subprocess.Popen(
            [shutil.which("opencode"), "--prompt", "hello"],
            cwd=str(project), env=env, stdin=slave, stdout=slave, stderr=slave,
            start_new_session=True, close_fds=True,
        )
        os.close(slave)
        try:
            with open(transcript, "wb") as sink:
                end = time.monotonic() + timeout
                rendered = lambda: any(r.get("event") == "render" for r in _marker_records(marker))  # noqa: E731
                activated = lambda: any(r.get("event") == "activate" for r in _marker_records(marker))  # noqa: E731
                if _drain(master, sink, end, activated):
                    # The slot draws only in a session view; give it a bounded wait.
                    _drain(master, sink, min(end, time.monotonic() + 30), rendered)
        finally:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(10)
            os.close(master)
    return _marker_records(marker), transcript


def test_tui_plugin_loads_with_host_solid_imports(tmp_path):
    records, transcript = run_tui(tmp_path)
    activations = [r for r in records if r.get("event") == "activate"]
    assert activations, transcript.read_bytes()[-4000:].decode("utf-8", "replace")
    activation = activations[0]
    assert activation["probe"] == PROBE_ID
    assert activation["solid_js"] == {
        "createSignal": "function", "createComponent": "function", "createMemo": "function",
    }, activation
    assert activation["opentui_solid"]["createElement"] == "function", activation
    assert activation["opentui_solid"]["insert"] == "function", activation
    assert activation["api"]["slots"] == "object", activation
    renders = [r for r in records if r.get("event") == "render"]
    assert renders and renders[0]["slot"] == "sidebar_content", records
    assert renders[0]["session_id"], records
