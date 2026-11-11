"""Guards against an orchestrator dead-waiting on a subagent's backgrounded test run.

The orchestrator is notified when a subagent's turn ends, not when a test
process the subagent started exits. Three rules close that gap:

- the implement dispatch template (run, run_hard) requires an explicit Bash
  `timeout: 600000` on every test call, because the harness moves any call
  past its ~120 s default to the background when no timeout is given;
- implement closes a commit boundary only once the suite result is read in
  the same turn;
- run and run_hard carry a watchdog: a bounded background waiter keyed on the
  subagent's own test process, followed by a nudge.

The waiter recipe is also executed under bash and zsh against a real process.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILLS = REPO_ROOT / "quoin" / "adapters" / "claude" / "skills"
RUN = SKILLS / "run" / "SKILL.md"
RUN_HARD = SKILLS / "run_hard" / "SKILL.md"
IMPLEMENT = SKILLS / "implement" / "SKILL.md"
CORE = REPO_ROOT / "quoin" / "core" / "skills"

MANDATE = "explicit Bash `timeout: 600000`"


def _flat(text: str) -> str:
    return " ".join(text.split())


def _between(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i:text.index(end, i + len(start))]


def run_guard() -> str:
    return _between(RUN.read_text(encoding="utf-8"), "## Implementer tier guard", "## Step 5")


def run_hard_phase4() -> str:
    return _between(RUN_HARD.read_text(encoding="utf-8"), "## Phase 4 — Implement", "**Small-profile cost honesty.**")


# ---------------------------------------------------------------------------
# Dispatch template: explicit timeout, unique junit token, no pending yield
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("region", [run_guard, run_hard_phase4], ids=["run", "run_hard"])
def test_dispatch_template_mandates_explicit_timeout(region):
    flat = _flat(region())
    assert "Run every test command in the FOREGROUND with an " + MANDATE in flat
    assert "Never end your turn while a test run is pending; read its result first." in flat
    assert "`impl-<task>-<stamp>`" in flat
    assert "--junitxml" in flat and "--junit-out" in flat
    assert "~120 s to the background" in flat


def test_run_step6_inline_runs_use_explicit_timeout():
    text = RUN.read_text(encoding="utf-8")
    step6 = _flat(_between(text, "## Step 6 — Verify", "## Step 8"))
    assert MANDATE in step6


# ---------------------------------------------------------------------------
# implement: same-turn commit boundary
# ---------------------------------------------------------------------------

def test_implement_same_turn_commit_boundary():
    text = IMPLEMENT.read_text(encoding="utf-8")
    region = _flat(_between(text, "### Automated verify-fix loop", "## Commit messages"))
    assert "**Same-turn commit boundary.**" in region
    assert "closes only when its suite result is read in the same turn" in region
    assert MANDATE in region
    assert "Never end a turn, return to a parent or commit while a test run is pending" in region
    assert "### Headless children never yield with pending work" in region


def test_core_docs_carry_the_rules():
    implement = _flat((CORE / "implement.md").read_text(encoding="utf-8"))
    assert "commit boundary is closed only when the result of its test run has been read in the same turn" in implement
    for name in ("run.md", "run_hard.md"):
        flat = _flat((CORE / name).read_text(encoding="utf-8"))
        assert "explicit long tool timeout" in flat, name
        assert "bounded waiter" in flat, name
        assert "specific test process" in flat, name


# ---------------------------------------------------------------------------
# Watchdog recipe text
# ---------------------------------------------------------------------------

def _waiter_block(region: str) -> str:
    blocks = re.findall(r"```bash\n(.*?)```", region, re.S)
    hits = [textwrap.dedent(b) for b in blocks if "WAITER|" in b]
    assert len(hits) == 1, hits
    return hits[0]


@pytest.mark.parametrize("region", [run_guard, run_hard_phase4], ids=["run", "run_hard"])
def test_watchdog_recipe_is_keyed_and_bounded(region):
    text = region()
    flat = _flat(text)
    block = _waiter_block(text)
    assert 'pgrep -f "[i]mpl-<task>-<stamp>"' in block
    assert '-lt 360' in block and "sleep 10" in block
    assert 'echo "WAITER|timeout"' in block and 'echo "WAITER|exited"' in block
    assert "pgrep -f pytest" not in block
    assert "run_in_background: true" in flat
    assert "never key on a bare `pgrep -f pytest`" in flat.lower()
    assert "bounded at one hour" in flat
    assert "SendMessage" in flat
    assert "notification fires when" in flat
    # A headless child never yields on a background waiter.
    assert "must not end its turn on a background waiter" in flat
    assert "`54` in place of `360`" in flat


def test_run_and_run_hard_waiters_match():
    assert _waiter_block(run_guard()) == _waiter_block(run_hard_phase4())


# ---------------------------------------------------------------------------
# Execute the waiter under bash and zsh
# ---------------------------------------------------------------------------

_SHELLS = [pytest.param("bash", id="bash"), pytest.param("zsh", id="zsh")]


def _runnable(token: str, bound: int) -> str:
    block = _waiter_block(run_guard())
    block = block.replace("[i]mpl-<task>-<stamp>", "[" + token[0] + "]" + token[1:])
    return block.replace("-lt 360", f"-lt {bound}").replace("sleep 10", "sleep 0.2")


def _run(shell: str, script: str) -> str:
    if shutil.which(shell) is None:
        pytest.skip(f"{shell} not installed")
    if shutil.which("pgrep") is None:
        pytest.skip("pgrep not installed")
    flags = ["-f", "-c"] if shell == "zsh" else ["-c"]
    proc = subprocess.run([shell, *flags, script], capture_output=True, text=True, timeout=60)
    return proc.stdout + proc.stderr


def _token() -> str:
    return f"impl-watchdog-test-{time.time_ns()}"


@pytest.mark.parametrize("shell", _SHELLS)
def test_waiter_never_matches_itself(shell):
    # No process carries the token: the waiter must not see its own command line.
    out = _run(shell, _runnable(_token(), bound=5))
    assert "WAITER|exited" in out, out


@pytest.mark.parametrize("shell", _SHELLS)
def test_waiter_returns_when_the_keyed_process_exits(shell):
    token = _token()
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1.5)", token])
    try:
        start = time.monotonic()
        out = _run(shell, _runnable(token, bound=100))
        assert "WAITER|exited" in out, out
        assert time.monotonic() - start >= 1.0, "waiter returned before the process exited"
    finally:
        proc.kill()
        proc.wait()


@pytest.mark.parametrize("shell", _SHELLS)
def test_waiter_times_out_on_its_bound(shell):
    token = _token()
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", token])
    try:
        out = _run(shell, _runnable(token, bound=3))
        assert "WAITER|timeout" in out, out
    finally:
        proc.kill()
        proc.wait()
