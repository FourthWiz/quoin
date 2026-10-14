"""Current-session resolution via CLAUDE_CODE_SESSION_ID.

The age guard, the context-budget guard and get_session_uuid must identify the
running session from the harness-exported environment variable instead of
guessing the newest transcript by mtime, which picks a concurrent session's
file. Scripts run as subprocesses with an explicit environment so the ambient
variable of the test runner never leaks in.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
AGE = REPO_ROOT / "quoin" / "scripts" / "session_age_guard.py"
BUDGET = REPO_ROOT / "quoin" / "scripts" / "context_budget_guard.py"
UUID_CLI = REPO_ROOT / "quoin" / "scripts" / "get_session_uuid.py"
SOURCES = {
    "age": AGE,
    "budget": REPO_ROOT / "quoin" / "core" / "scripts" / "context_budget_guard.py",
    "uuid": REPO_ROOT / "quoin" / "core" / "scripts" / "get_session_uuid.py",
}

ENV_UUID = "11111111-2222-3333-4444-555555555555"
SIBLING_UUID = "99999999-8888-7777-6666-555555555555"
PROJECT_ROOT = "/fake/project"
PROJECT_HASH = "-fake-project"
GUARDS = ["age", "budget"]


def _env(home: Path, session_id=None) -> dict:
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
    if session_id is not None:
        env["CLAUDE_CODE_SESSION_ID"] = session_id
    return env


def _jsonl(home: Path, uuid: str, project_hash: str = PROJECT_HASH, mtime=None) -> Path:
    d = home / ".claude" / "projects" / project_hash
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{uuid}.jsonl"
    p.write_text('{"type": "x"}\n')
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


def _run_guard(kind, home, session_id=None, extra=None):
    script = AGE if kind == "age" else BUDGET
    cmd = [sys.executable, str(script), "--project-root", PROJECT_ROOT, *(extra or [])]
    r = subprocess.run(cmd, capture_output=True, text=True, env=_env(home, session_id))
    out = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
    return r.returncode, out.split("|"), r.stderr


def _chosen_path(parts) -> str:
    return parts[2] if len(parts) > 2 else ""


def _run_uuid(home, session_id=None, phase="implement"):
    cmd = [sys.executable, str(UUID_CLI), "--project-path", PROJECT_ROOT, "--phase", phase]
    r = subprocess.run(cmd, capture_output=True, text=True, env=_env(home, session_id))
    assert r.returncode == 0
    return r.stdout.strip()


@pytest.mark.parametrize("kind", GUARDS)
def test_env_uuid_in_project_dir_is_chosen(tmp_path, kind):
    target = _jsonl(tmp_path, ENV_UUID)
    _, parts, _ = _run_guard(kind, tmp_path, ENV_UUID)
    assert _chosen_path(parts) == str(target)


@pytest.mark.parametrize("kind", GUARDS)
def test_env_uuid_beats_newer_sibling(tmp_path, kind):
    """The reported bug: a concurrent session wrote last."""
    current = _jsonl(tmp_path, ENV_UUID, mtime=1_000_000_000)
    _jsonl(tmp_path, SIBLING_UUID, mtime=2_000_000_000)
    _, parts, _ = _run_guard(kind, tmp_path, ENV_UUID)
    assert _chosen_path(parts) == str(current)
    # Without the variable the legacy mtime choice is the sibling.
    _, parts, _ = _run_guard(kind, tmp_path, None)
    assert Path(_chosen_path(parts)).stem == SIBLING_UUID


@pytest.mark.parametrize("kind", GUARDS)
def test_env_uuid_found_under_other_project_hash(tmp_path, kind):
    target = _jsonl(tmp_path, ENV_UUID, project_hash="-fake-project-worktree")
    _jsonl(tmp_path, SIBLING_UUID)
    _, parts, _ = _run_guard(kind, tmp_path, ENV_UUID)
    assert _chosen_path(parts) == str(target)


@pytest.mark.parametrize("kind", GUARDS)
def test_env_uuid_duplicate_across_projects_picks_newest(tmp_path, kind):
    _jsonl(tmp_path, ENV_UUID, project_hash="-other-a", mtime=1_000_000_000)
    newer = _jsonl(tmp_path, ENV_UUID, project_hash="-other-b", mtime=2_000_000_000)
    _jsonl(tmp_path, ENV_UUID, project_hash="-other-c", mtime=1_500_000_000)
    _, parts, _ = _run_guard(kind, tmp_path, ENV_UUID)
    assert _chosen_path(parts) == str(newer)


@pytest.mark.parametrize("kind", GUARDS)
def test_env_uuid_found_when_project_dir_missing(tmp_path, kind):
    target = _jsonl(tmp_path, ENV_UUID, project_hash="-elsewhere")
    _, parts, _ = _run_guard(kind, tmp_path, ENV_UUID)
    assert _chosen_path(parts) == str(target)


@pytest.mark.parametrize("kind", GUARDS)
def test_env_uuid_missing_everywhere_fails_open(tmp_path, kind):
    _jsonl(tmp_path, SIBLING_UUID)
    code, parts, err = _run_guard(kind, tmp_path, ENV_UUID)
    assert code == 0
    assert parts[0] == "OK"
    assert _chosen_path(parts) == ""
    assert "fail-OPEN" in err


@pytest.mark.parametrize("kind", GUARDS)
@pytest.mark.parametrize("value", [None, ""])
def test_unset_or_empty_env_uses_mtime(tmp_path, kind, value):
    _jsonl(tmp_path, ENV_UUID, mtime=1_000_000_000)
    newest = _jsonl(tmp_path, SIBLING_UUID, mtime=2_000_000_000)
    _, parts, _ = _run_guard(kind, tmp_path, value)
    assert _chosen_path(parts) == str(newest)


@pytest.mark.parametrize("kind", GUARDS)
@pytest.mark.parametrize("value", ["../../etc/passwd", "abc", "*", ENV_UUID + "/.."])
def test_malformed_env_is_ignored(tmp_path, kind, value):
    newest = _jsonl(tmp_path, SIBLING_UUID)
    _, parts, err = _run_guard(kind, tmp_path, value)
    assert _chosen_path(parts) == str(newest)
    assert "malformed" in err


@pytest.mark.parametrize("kind", GUARDS)
def test_explicit_current_uuid_beats_env(tmp_path, kind):
    _jsonl(tmp_path, ENV_UUID)
    explicit = _jsonl(tmp_path, SIBLING_UUID)
    _, parts, _ = _run_guard(kind, tmp_path, ENV_UUID, ["--current-uuid", SIBLING_UUID])
    assert _chosen_path(parts) == str(explicit)


def test_budget_explicit_transcript_beats_env(tmp_path):
    _jsonl(tmp_path, ENV_UUID)
    t = tmp_path / "explicit.jsonl"
    t.write_text("x" * 10)
    _, parts, _ = _run_guard("budget", tmp_path, ENV_UUID, ["--transcript", str(t)])
    assert _chosen_path(parts) == str(t)


def test_budget_disable_checked_before_env(tmp_path):
    env = _env(tmp_path, "not-a-uuid")
    env["QUOIN_DISABLE_PHASE_BUDGET"] = "1"
    r = subprocess.run([sys.executable, str(BUDGET), "--project-root", PROJECT_ROOT],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0
    assert r.stdout.strip() == "OK|disabled|"


# --- get_session_uuid ------------------------------------------------------

def test_uuid_env_returned_even_without_file(tmp_path):
    assert _run_uuid(tmp_path, ENV_UUID) == ENV_UUID


def test_uuid_env_beats_newer_sibling(tmp_path):
    _jsonl(tmp_path, ENV_UUID, mtime=1_000_000_000)
    _jsonl(tmp_path, SIBLING_UUID, mtime=2_000_000_000)
    assert _run_uuid(tmp_path, ENV_UUID) == ENV_UUID
    assert _run_uuid(tmp_path, None) == SIBLING_UUID


@pytest.mark.parametrize("value", ["", "abc", "../../etc/passwd"])
def test_uuid_unset_empty_malformed_use_legacy(tmp_path, value):
    _jsonl(tmp_path, SIBLING_UUID)
    assert _run_uuid(tmp_path, value) == SIBLING_UUID


def test_uuid_environ_kwarg_in_process(tmp_path):
    spec = importlib.util.spec_from_file_location("_t_gsu", SOURCES["uuid"])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.get_session_uuid(
        project_path=PROJECT_ROOT, home=str(tmp_path), environ={"CLAUDE_CODE_SESSION_ID": ENV_UUID}
    ) == ENV_UUID
    assert mod.get_session_uuid(
        project_path=PROJECT_ROOT, home=str(tmp_path), phase="plan", environ={}
    ).startswith("unknown-plan-")


# --- inline-copy drift guard ----------------------------------------------

def test_uuid_regex_parity_across_scripts():
    patterns = set()
    for name, path in SOURCES.items():
        spec = importlib.util.spec_from_file_location(f"_t_parity_{name}", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        patterns.add(mod._UUID_RE.pattern)
    assert len(patterns) == 1
