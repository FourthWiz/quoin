"""Nothing the agents say or do may be persisted by the deck."""
import json
import os
import pathlib
import subprocess
import sys

import pytest

import _streamdeck_helpers as h
from _streamdeck_helpers import deck_home, forbid_dangerous_subprocess, payload, stage_runtime  # noqa: F401

pytestmark = pytest.mark.usefixtures("deck_home", "forbid_dangerous_subprocess")

CLAUDE_EVENTS = (
    "SessionStart", "SessionEnd", "UserPromptSubmit", "PreToolUse", "PermissionRequest",
    "PostToolUse", "Stop", "PreCompact", "PostCompact", "PostToolUseFailure",
    "Notification", "StopFailure", "Elicitation", "ElicitationResult", "SubagentStart", "SubagentStop",
)
CODEX_EVENTS = (
    "SessionStart", "UserPromptSubmit", "PreToolUse", "PermissionRequest", "PostToolUse",
    "Stop", "PreCompact", "PostCompact", "Notification", "Interrupt",
)
FIELDS = ("prompt", "tool_input", "tool_response", "last_assistant_message",
          "transcript_path", "message", "notification", "title")
ZENV = {"ZELLIJ_SESSION_NAME": "sess", "ZELLIJ_PANE_ID": "3"}


def sentinel(n):
    return "SENTINEL-%02d-7f3a9c" % n


def event_payload(name, n):
    value = {"session_id": "sid-%d" % n, "hook_event_name": name, "cwd": "/work",
             "tool_name": "Bash", "tool_use_id": "tu-%d" % n}
    for index, field in enumerate(FIELDS):
        text = "%s-%s" % (sentinel(n), field)
        value[field] = {"command": text} if field == "tool_input" else text
    value["notification_type"] = "permission_prompt"
    return value


def all_sentinels(count):
    return [("%s-%s" % (sentinel(n), f)).encode() for n in range(count) for f in FIELDS] + \
           [sentinel(n).encode() for n in range(count)]


def scan(roots, needles):
    hits = []
    for root in roots:
        for dirpath, _, names in os.walk(str(root)):
            for name in names:
                path = pathlib.Path(dirpath) / name
                try:
                    data = path.read_bytes()
                except OSError:
                    continue
                for needle in needles:
                    if needle in data:
                        hits.append((str(path), needle))
    return hits


@pytest.fixture
def staged(tmp_path):
    return stage_runtime(tmp_path)


def feed_events(staged):
    count = 0
    for kind, names in (("claude", CLAUDE_EVENTS), ("codex", CODEX_EVENTS)):
        for name in names:
            body = json.dumps(event_payload(name, count)).encode()
            rc, out, err = staged.run(staged.command("monitor.py", kind), body, ZENV)
            assert (rc, out, err) == (0, b"", b""), (kind, name)
            count += 1
    return count


def test_no_sentinel_reaches_disk(staged):
    count = feed_events(staged)
    assert len(list((staged.state_dir / "agents").glob("*.json"))) >= 2
    roots = [staged.runtime, staged.state_root, staged.home]
    needles = all_sentinels(count)
    # Records are garbage-collected by the snapshot below, so check them while they exist.
    assert scan(roots, needles) == []

    rc, out, err = staged.run(staged.command("dashboard.py", "snapshot"))
    assert rc == 0, err

    failing = staged.fakes / "zellij"
    failing.write_text("#!/bin/sh\necho boom >&2\nexit 2\n")
    failing.chmod(0o755)
    target = json.dumps({"session": "sess", "pane": "3", "socket": str(staged.zsock)})
    staged.run(staged.command("dashboard.py", "focus", target))
    staged.run(staged.command("dashboard.py", "no-such-action"))

    assert scan(roots, needles) == []


def test_error_log_holds_the_type_only(payload, deck_home):
    deckenv = payload("deckenv")
    env = deckenv.current()
    deckenv.ensure_dir(env.state_dir)
    deckenv.log_error_type_only(env, ValueError(sentinel(99) + " in the message"))
    log = pathlib.Path(env.errors_log).read_text()
    assert "ValueError" in log
    assert sentinel(99) not in log


@pytest.mark.skipif(h.NODE is None, reason="node not found on the developer PATH")
def test_plugin_health_and_failed_invoke_persist_nothing(staged, tmp_path):
    harness = tmp_path / "health.js"
    harness.write_text(r"""
const p = require(process.argv[2]);
const cfg = {python: process.argv[3], runtime: process.argv[4], state_root: process.argv[5], invoke_timeout_ms: 500};
(async () => {
  try { await p.invoke(cfg, 'focus', {prompt: process.argv[6]}); } catch (e) {}
  p.writeHealth(cfg, {updated: 1, error_type: 'timeout'});
})();
""")
    broken = tmp_path / "broken-python"
    broken.write_text("#!/bin/sh\necho " + sentinel(98) + " >&2\nexit 3\n")
    broken.chmod(0o755)
    result = subprocess.run(
        [h.NODE, str(harness), str(h.PAYLOAD_DIR / "plugin" / "plugin.js"), str(broken),
         str(staged.runtime), str(staged.state_root), sentinel(98)],
        capture_output=True, text=True, timeout=30, env={"HOME": str(staged.home), "PATH": "/usr/bin:/bin"})
    assert result.returncode == 0, result.stderr
    assert (staged.state_root / "state" / "health.json").exists()
    assert scan([staged.runtime, staged.state_root, staged.home, h.PAYLOAD_DIR / "plugin"],
                [sentinel(98).encode()]) == []
