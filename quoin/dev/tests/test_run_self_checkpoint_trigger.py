"""Behavioural test for the `/run` self-checkpoint trigger block.

The block is extracted verbatim from `run/SKILL.md` and run in its own shell,
the way a Bash tool call would run it: only the environment carries over, so
a value computed by an earlier call (the Pre-phase guard's output variable)
must not influence the answer. A stub guard prints whatever the case asks for.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[2]
RUN_SKILL = PKG / "adapters" / "claude" / "skills" / "run" / "SKILL.md"
LIB_SH = PKG / "hooks" / "_lib.sh"
GUARD_REAL = PKG / "scripts" / "context_budget_guard.py"

BULLET = "- **Self-checkpoint trigger.**"
NEXT_BULLET = "- **Self-checkpoint at very high fill.**"
STALE_VAR = "_cbg" + "_out"


def _bullet() -> str:
    text = RUN_SKILL.read_text(encoding="utf-8")
    start = text.index(BULLET)
    return text[start:text.index(NEXT_BULLET, start)]


def _block() -> str:
    fences = re.findall(r"[ \t]*```sh\n(.*?)\n[ \t]*```", _bullet(), re.DOTALL)
    assert len(fences) == 1
    return "\n".join(line.strip() for line in fences[0].splitlines())


def test_block_is_not_vacuous():
    block = _block()
    assert "COMPACT_FIRST_BPS" in block
    assert "context_budget_guard.py" in block
    assert STALE_VAR not in block
    assert "autonomous" not in block.lower()


def _home(tmp_path, stub=True):
    home = tmp_path / "quoinhome"
    (home / "hooks").mkdir(parents=True)
    (home / "scripts").mkdir()
    shutil.copy(str(LIB_SH), str(home / "hooks" / "_lib.sh"))
    if stub:
        (home / "scripts" / "context_budget_guard.py").write_text(
            "import os, sys\n"
            "print(os.environ.get('STUB_GUARD_LINE', ''))\n"
            "sys.exit(int(os.environ.get('STUB_GUARD_EXIT', '0')))\n", encoding="utf-8")
    return home


def _run(tmp_path, home, extra_env=None, cwd=None):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path / "userhome")}
    (tmp_path / "userhome").mkdir(exist_ok=True)
    if extra_env:
        env.update(extra_env)
    script = _block().replace("__QUOIN_HOME__", str(home))
    proc = subprocess.run(["sh", "-c", script], capture_output=True, text=True, env=env,
                          cwd=str(cwd or tmp_path), timeout=60)
    return proc.stdout.strip()


CASES = [
    ("OK|9100|/t.jsonl", 0, {}, "DUE"),
    ("OVER|9100|/t.jsonl", 1, {}, "DUE"),
    ("OK|9000|/t.jsonl", 0, {}, "DUE"),
    ("OK|8999|/t.jsonl", 0, {}, "SKIP"),
    ("OK|disabled|", 0, {}, "SKIP"),
    ("OK|0|", 0, {}, "SKIP"),
    ("", 0, {}, "SKIP"),
    ("Traceback (most recent call last):", 1, {}, "SKIP"),
    ("OVER|abc|x", 1, {}, "SKIP"),
    ("OK|8500|x", 0, {"QUOIN_COMPACT_FIRST_BPS": "8000"}, "DUE"),
]


@pytest.mark.parametrize("line,code,env,want", CASES, ids=[c[0] or "empty" for c in CASES])
def test_stub_matrix(tmp_path, line, code, env, want):
    home = _home(tmp_path)
    extra = {"STUB_GUARD_LINE": line, "STUB_GUARD_EXIT": str(code), **env}
    assert _run(tmp_path, home, extra) == "SELF_CHECKPOINT_" + want


def test_stale_variable_from_an_earlier_call_is_ignored(tmp_path):
    home = _home(tmp_path)
    extra = {STALE_VAR: "OK|9900|x", "STUB_GUARD_LINE": "OK|100|x"}
    assert _run(tmp_path, home, extra) == "SELF_CHECKPOINT_SKIP"


def test_real_guard_disabled_by_env(tmp_path):
    home = tmp_path / "quoinhome"
    (home / "hooks").mkdir(parents=True)
    shutil.copy(str(LIB_SH), str(home / "hooks" / "_lib.sh"))
    (home / "scripts").mkdir()
    shutil.copy(str(GUARD_REAL), str(home / "scripts" / "context_budget_guard.py"))
    core = PKG / "core" / "scripts"
    # the wrapper delegates to the core implementation next to it
    if (core / "context_budget_guard.py").is_file():
        (home / "core" / "scripts").mkdir(parents=True)
        shutil.copy(str(core / "context_budget_guard.py"), str(home / "core" / "scripts" / "context_budget_guard.py"))
    assert _run(tmp_path, home, {"QUOIN_DISABLE_PHASE_BUDGET": "1"}) == "SELF_CHECKPOINT_SKIP"


def test_real_guard_without_a_transcript_skips(tmp_path):
    home = tmp_path / "quoinhome"
    (home / "hooks").mkdir(parents=True)
    shutil.copy(str(LIB_SH), str(home / "hooks" / "_lib.sh"))
    (home / "scripts").mkdir()
    shutil.copy(str(GUARD_REAL), str(home / "scripts" / "context_budget_guard.py"))
    assert _run(tmp_path, home) == "SELF_CHECKPOINT_SKIP"


def test_missing_library_still_prints_a_token(tmp_path):
    home = _home(tmp_path)
    (home / "hooks" / "_lib.sh").unlink()
    extra = {"STUB_GUARD_LINE": "OK|9500|x"}
    assert _run(tmp_path, home, extra) == "SELF_CHECKPOINT_DUE"


def test_due_then_probe_with_no_active_record_skips_the_save(tmp_path):
    text = RUN_SKILL.read_text(encoding="utf-8")
    start = text.index(NEXT_BULLET)
    section = text[start:text.index("- **No prompt hook reports context.**", start)]
    fences = [f for f in re.findall(r"[ \t]*```sh\n(.*?)\n[ \t]*```", section, re.DOTALL)
              if "command -v run_state_probe" in f]
    assert len(fences) == 1
    home = _home(tmp_path)
    project = tmp_path / "proj"
    (project / ".workflow_artifacts" / "memory").mkdir(parents=True)
    probe = "\n".join(l.strip() for l in fences[0].splitlines()).replace("__QUOIN_HOME__", str(home))
    assert _run(tmp_path, home, {"STUB_GUARD_LINE": "OK|9500|x"}, cwd=project) == "SELF_CHECKPOINT_DUE"
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path / "userhome")}
    out = subprocess.run(["sh", "-c", probe], capture_output=True, text=True, env=env, cwd=str(project)).stdout.strip()
    assert out == "PROBE_INACTIVE"
