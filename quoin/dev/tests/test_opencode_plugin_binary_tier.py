"""The Quoin lifecycle plugin against the pinned OpenCode binary.

Runs `opencode serve` in a throwaway home with the built plugin named in the
project config and a loopback provider fake, drives one session that writes a
file behind an approval prompt and is then compacted, and a second session
whose provider call fails, then checks the plugin's log translates into the
expected runtime events. Skips unless the pinned binary is on PATH.

Set QUOIN_CAPTURE_PLUGIN_FIXTURE to a file path to also write the captured log
there, with the temporary project path replaced by `<project>`; that is how
the committed real-plugin fixture is refreshed.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

import pytest

import _opencode_helpers as helpers
from quoin.opencode_adapter import events as ev
from quoin.opencode_adapter import manifest, plugin_events

REPO_ROOT = Path(__file__).resolve().parents[3]
PLUGIN_JS = REPO_ROOT / "quoin" / "adapters" / "opencode" / "plugin" / "quoin-opencode-plugin.js"
RUN_ID = "oc-20261102T120000Z-0a1b2c3d"


def _skip_reason():
    binary = shutil.which("opencode")
    if binary is None:
        return "no 'opencode' binary on PATH"
    pinned = manifest.read_pinned_version(REPO_ROOT / "quoin")
    try:
        reported = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        reported = ""
    if pinned not in reported:
        return "'opencode --version' did not report the pinned release %r" % pinned
    return None


pytestmark = pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or "")

_USAGE = {"completion_tokens": 5, "prompt_tokens": 10, "total_tokens": 15}


def _tool_turn(depth, call_id, name, args):
    return {
        "depth": depth, "finish_reason": "tool_calls", "status": 200, "stream": True, "usage": _USAGE,
        "chunks": [{"role": "assistant", "tool_calls": [{
            "index": 0, "id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }]}],
    }


def _text_turn(depth, text):
    return {
        "depth": depth, "finish_reason": "stop", "status": 200, "stream": True, "usage": _USAGE,
        "chunks": [{"role": "assistant", "content": text}],
    }


def _scenarios(project):
    return {"schema_version": 1, "scenarios": {
        "write_ok": {"turns": [
            _tool_turn(0, "call_w1", "write", {"filePath": str(project / "hello.txt"), "content": "hi\n"}),
            _text_turn(1, "done"),
        ]},
        "summary": {"turns": [_text_turn(0, "summary text")]},
        "bad_request": {"turns": [{
            "status": 400, "stream": False,
            "body": {"error": {"message": "bad request", "type": "invalid_request_error"}},
        }]},
    }}


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Server:
    def __init__(self, port):
        self.base = "http://127.0.0.1:%d" % port

    def call(self, method, path, body=None, timeout=60):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base + path, method=method, data=data,
                                     headers={"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else None


def _wait_for(predicate, timeout=30.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.2)
    return False


def _records(log):
    return plugin_events.read_log(log).records


def capture(tmp_path):
    """Drive the real binary with the built plugin; return (log path, project path)."""
    fake = helpers.load_module(helpers.OPENCODE_DIR / "fake_openai_server.py", "fake_openai_server_plugin")
    home, xdg, project = tmp_path / "home", tmp_path / "xdg", tmp_path / "proj"
    for d in (home, xdg, project / ".git"):
        d.mkdir(parents=True)
    log = tmp_path / "plugin.jsonl"
    with fake.FakeProviderServer(scenarios=_scenarios(project), host="127.0.0.1", port=0) as provider:
        base_url = provider.base_url if provider.base_url.endswith("/v1") else provider.base_url + "/v1"
        config = {
            "$schema": "https://opencode.ai/config.json",
            "provider": {"quoin-fake": {
                "npm": "@ai-sdk/openai-compatible",
                "options": {"baseURL": base_url, "apiKey": "loopback-only-value"},
                "models": {name: {"name": name, "tool_call": True}
                           for name in ("write_ok", "summary", "bad_request")},
            }},
            "model": "quoin-fake/write_ok",
            "small_model": "quoin-fake/summary",
            "permission": {"edit": "ask"},
            "plugin": [PLUGIN_JS.as_uri()],
        }
        (project / "opencode.json").write_text(json.dumps(config), encoding="utf-8")
        env = {
            **os.environ,
            "HOME": str(home), "XDG_CONFIG_HOME": str(xdg),
            "XDG_DATA_HOME": str(tmp_path / "data"), "XDG_CACHE_HOME": str(tmp_path / "cache"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "OPENCODE_DISABLE_AUTOUPDATE": "1",
            "QUOIN_OPENCODE_RUN_ID": RUN_ID,
            "QUOIN_OPENCODE_PLUGIN_LOG": str(log),
        }
        port = _free_port()
        out = open(tmp_path / "serve.out", "wb")
        proc = subprocess.Popen(
            [shutil.which("opencode"), "serve", "--port", str(port), "--hostname", "127.0.0.1"],
            cwd=str(project), env=env, stdout=out, stderr=subprocess.STDOUT,
        )
        server = _Server(port)
        try:
            def ready():
                try:
                    server.call("GET", "/config", timeout=2)
                    return True
                except Exception:
                    return False
            assert _wait_for(ready, 60), (tmp_path / "serve.out").read_text(errors="replace")

            sid = server.call("POST", "/session", {})["id"]

            def reply_to_approval():
                def asked():
                    return [r for r in _records(log) if r["native_type"] == "permission.asked"]
                if _wait_for(lambda: bool(asked()), 60):
                    permission_id = asked()[0]["data"]["permission_id"]
                    server.call("POST", "/session/%s/permissions/%s" % (sid, permission_id),
                                {"response": "once"})

            replier = threading.Thread(target=reply_to_approval, daemon=True)
            replier.start()
            server.call("POST", "/session/%s/message" % sid, {
                "parts": [{"type": "text", "text": "write hello"}],
                "model": {"providerID": "quoin-fake", "modelID": "write_ok"},
            }, timeout=90)
            replier.join(10)
            server.call("POST", "/session/%s/summarize" % sid,
                        {"providerID": "quoin-fake", "modelID": "summary"}, timeout=90)
            assert _wait_for(lambda: any(r["native_type"] == "session.compacted" for r in _records(log)))

            failing = server.call("POST", "/session", {})["id"]
            try:
                server.call("POST", "/session/%s/message" % failing, {
                    "parts": [{"type": "text", "text": "fail"}],
                    "model": {"providerID": "quoin-fake", "modelID": "bad_request"},
                }, timeout=60)
            except Exception:
                pass
            assert _wait_for(lambda: any(r["native_type"] == "session.error" for r in _records(log)))
        finally:
            proc.terminate()
            try:
                proc.wait(15)
            except subprocess.TimeoutExpired:
                proc.kill()
            out.close()
    return log, project


def test_real_plugin_log_translates_into_runtime_events(tmp_path):
    log, project = capture(tmp_path)
    read = plugin_events.read_log(log)
    assert read.counters == {}, read.counters
    types = [r["native_type"] for r in read.records]
    for expected in (
        "session.created", "permission.asked", "permission.replied", "tool.execute.after",
        "session.idle", "experimental.session.compacting", "session.compacted", "session.error",
    ):
        assert expected in types, (expected, types)
    assert all(r["plugin_version"] == "0.1.0" for r in read.records)

    events = plugin_events.translate(read.records, run_id=RUN_ID)
    assert len(events) == len(read.records)
    for event in events:
        assert ev.RuntimeEvent.from_json(event.to_json()) == event
    asked, replied = [e.payload for e in events if getattr(e.payload, "kind", None) == "approval"]
    assert (asked.status, asked.tool, replied.status) == ("asked", "edit", "once")
    assert asked.request_id and asked.request_id == replied.request_id
    assert len({r["instance"] for r in read.records}) == 1
    error = next(e for e in events if e.type is ev.EventType.ERROR)
    assert error.payload.failure_kind == "config" and "bad request" in error.payload.message
    assert (project / "hello.txt").read_text() == "hi\n"

    target = os.environ.get("QUOIN_CAPTURE_PLUGIN_FIXTURE")
    if target:
        text = log.read_text(encoding="utf-8").replace(str(project).lstrip("/"), "<project>")
        Path(target).write_text(text, encoding="utf-8")
