import json
import os
import stat
import subprocess
import time

import pytest

import _streamdeck_helpers as h
from _streamdeck_helpers import deck_home, forbid_dangerous_subprocess  # noqa: F401

pytestmark = [pytest.mark.usefixtures("deck_home", "forbid_dangerous_subprocess"),
              pytest.mark.skipif(h.NODE is None, reason="node not found on the developer PATH")]

PLUGIN = h.PAYLOAD_DIR / "plugin" / "plugin.js"

HARNESS = r"""
const fs = require('node:fs'), path = require('node:path');
const p = require(process.argv[2]);
const out = {};
const dir = process.argv[3];
out.missing = p.loadConfig(path.join(dir, 'nowhere'));
fs.mkdirSync(path.join(dir, 'cfgdir'), {recursive: true});
fs.writeFileSync(path.join(dir, 'cfgdir', 'plugin-config.json'), '{bad');
out.invalid = p.loadConfig(path.join(dir, 'cfgdir'));
const cfg = {python: process.argv[4], runtime: path.join(dir, 'rt dir'), state_root: path.join(dir, 'sr'), invoke_timeout_ms: 300};
fs.writeFileSync(path.join(dir, 'cfgdir', 'plugin-config.json'), JSON.stringify(cfg));
out.loaded = p.loadConfig(path.join(dir, 'cfgdir'));
const [bin, argv, env] = p.buildInvocation(cfg, 'focus', {a: 1});
out.bin = bin; out.argv = argv; out.env = {r: env.QUOIN_DECK_RUNTIME, s: env.QUOIN_DECK_STATE_ROOT};
(async () => {
  const t = Date.now();
  try { await p.invoke({...cfg, python: process.argv[5]}, 'snapshot'); out.timeout = 'resolved'; }
  catch (e) { out.timeout = e.message; }
  out.elapsed = Date.now() - t;
  p.writeHealth(cfg, {updated: 1, error_type: 'timeout'});
  console.log(JSON.stringify(out));
})();
"""


def test_node_check():
    assert subprocess.run([h.NODE, "--check", str(PLUGIN)], capture_output=True).returncode == 0


def test_module_behaviour(tmp_path):
    sleeper = tmp_path / "sleepy-python"
    sleeper.write_text("#!/bin/sh\nsleep 5\n")
    sleeper.chmod(0o755)
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS)
    start = time.monotonic()
    result = subprocess.run([h.NODE, str(harness), str(PLUGIN), str(tmp_path), "/usr/bin/python3", str(sleeper)],
                            capture_output=True, text=True, timeout=30,
                            env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"})
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["missing"] is None and out["invalid"] is None and out["loaded"]["python"] == "/usr/bin/python3"
    runtime = str(tmp_path / "rt dir")
    assert out["argv"] == ["-E", "-s", runtime + "/current/dashboard.py", "focus", '{"a":1}']
    assert out["env"] == {"r": runtime, "s": str(tmp_path / "sr")}
    assert out["timeout"] == "timeout" and out["elapsed"] < 2000
    health = tmp_path / "sr" / "state" / "health.json"
    assert json.loads(health.read_text()) == {"updated": 1, "error_type": "timeout"}
    assert stat.S_IMODE(os.stat(health).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(health.parent).st_mode) == 0o700
    assert not list(health.parent.glob("*.tmp"))
    assert not (PLUGIN.parent / "health.json").exists()


GROUP_HARNESS = r"""
const p = require(process.argv[2]);
const cfg = {python: process.argv[3], runtime: '/r', state_root: '/s', invoke_timeout_ms: 300};
(async () => {
  const out = {};
  for (const action of ['menu', 'snapshot']) {
    const t = Date.now();
    try { out[action] = await p.invoke(cfg, action); } catch (e) { out[action] = 'ERR:' + e.message; }
    out[action + '_ms'] = Date.now() - t;
  }
  console.log(JSON.stringify(out));
})();
"""


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def test_only_snapshot_and_focus_are_bounded_and_killed_as_a_group(tmp_path):
    marker = tmp_path / "grandchild.pid"
    fake = tmp_path / "fake-python"
    # Interactive action: outlives the 300 ms bound, then answers. Bounded action: starts a grandchild, then hangs.
    fake.write_text(
        "#!/bin/sh\n"
        'case "$4" in\n'
        '  menu) sleep 1; echo menu-done;;\n'
        '  *) sleep 30 & echo $! > "%s"; wait;;\n'
        "esac\n" % marker
    )
    fake.chmod(0o755)
    harness = tmp_path / "group.js"
    harness.write_text(GROUP_HARNESS)
    result = subprocess.run([h.NODE, str(harness), str(PLUGIN), str(fake)], capture_output=True, text=True,
                            timeout=30, env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"})
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["menu"].strip() == "menu-done" and out["menu_ms"] >= 900
    assert out["snapshot"] == "ERR:timeout" and out["snapshot_ms"] < 2000
    grandchild = int(marker.read_text())
    deadline = time.monotonic() + 3
    while _pid_alive(grandchild) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _pid_alive(grandchild)


def test_static_shape():
    text = PLUGIN.read_text()
    assert "/usr/bin/python3" not in text and "versions/" not in text and "'versions'" not in text
    assert "e.message" not in text.split("function writeHealth")[1].split("function errorType")[0]
    assert "require.main === module" in text
    assert "base" not in [w for w in text.replace("(", " ").replace(")", " ").split() if w == "base"]
