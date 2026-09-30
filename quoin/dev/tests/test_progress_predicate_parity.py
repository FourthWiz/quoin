"""The supervisor and the auto-resume script share one progress rule but
cannot import each other; these tests compare their real outputs."""
from __future__ import annotations

import ast
import importlib.util
import itertools
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
_AR_PATH = REPO_ROOT / "quoin" / "core" / "scripts" / "auto_resume.py"
_SUP_PATH = REPO_ROOT / "src" / "quoin" / "supervisor.py"


@pytest.fixture(scope="module")
def sup():
    sys.path.insert(0, str(REPO_ROOT / "src"))
    try:
        from quoin import supervisor
    finally:
        sys.path.pop(0)
    return supervisor


@pytest.fixture(scope="module")
def ar():
    spec = importlib.util.spec_from_file_location("auto_resume_parity", _AR_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_signals_equal(sup, ar):
    assert sup.COMPLETION_SIGNALS == ar.COMPLETION_SIGNALS


def test_pure_functions_agree_on_grid(sup, ar):
    for streak, used, allowance, progressed, repair in itertools.product(
        range(4), range(4), range(4), (False, True), ((), ("implement",))
    ):
        assert sup.next_streak(progressed, repair, streak, used, allowance) == ar.next_streak(
            progressed, repair, streak, used, allowance
        )
    for streak, repair in itertools.product(range(4), ((), ("implement",))):
        assert sup.abort_reason(streak, repair) == ar.abort_reason(streak, repair)
    snaps = [(), (("a", "1"),), (("a", "2"),), (("b", "1"),), (("a", "1"), ("b", "1"))]
    for x, y in itertools.product(snaps, snaps):
        assert sup.heads_changed(x, y) == ar.heads_changed(x, y)


def test_repair_pending_agrees(sup, ar, tmp_path):
    layouts = [[], ["implement.tasks.done"], ["implement.tasks.done", "implement.done"],
               ["implement.batch-1.done", "review.tasks.done"]]
    for i, files in enumerate(layouts):
        d = tmp_path / f"d{i}"
        d.mkdir()
        for f in files:
            (d / f).write_text("x")
        assert sup.repair_pending_phases(d) == ar.repair_pending_phases(d)
    assert sup.repair_pending_phases(tmp_path / "none") == ar.repair_pending_phases(tmp_path / "none")


def test_knob_parity(sup, ar, monkeypatch):
    for value in (None, "-1", "0", "3", "9", "x"):
        env = {} if value is None else {sup.REPAIR_RELAUNCHES_ENV: value}
        if value is None:
            monkeypatch.delenv(sup.REPAIR_RELAUNCHES_ENV, raising=False)
        else:
            monkeypatch.setenv(sup.REPAIR_RELAUNCHES_ENV, value)
        assert sup.repair_allowance_from_env(env) == ar._repair_allowance()


needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git missing")


def _git(d, *args):
    subprocess.run(
        ["git", "-C", str(d), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True,
    )


def _repo(d, branch):
    d.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", branch, str(d)], check=True)
    _git(d, "commit", "-q", "--allow-empty", "-m", "i")
    return d


@needs_git
def test_probe_parity_on_real_repos(sup, ar, tmp_path, monkeypatch):
    wrap = tmp_path / "wrap"
    wrap.mkdir()
    _repo(wrap / "alpha", "feat/demo")
    _repo(wrap / "beta", "main")
    root_repo = _repo(tmp_path / "rootrepo", "feat/demo")
    for p in (wrap, root_repo):
        got = sup.probe_task_heads("demo", p)
        assert got and got == ar._probe_task_heads("demo", p)
    monkeypatch.setenv(sup.HEAD_PROBE_ENV, "0")
    assert sup.probe_task_heads("demo", wrap) == () == ar._probe_task_heads("demo", wrap)


def test_probe_issues_identical_argv(sup, ar, tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    seen = []

    class _R:
        returncode = 0
        stdout = "c" * 40 + "\nfeat/demo\n"

    def runner(argv, **kw):
        seen.append(list(argv))
        return _R()

    a = sup.probe_task_heads("demo", tmp_path, runner=runner)
    b = ar._probe_task_heads("demo", tmp_path, runner=runner)
    assert a == b and len(seen) == 2 and seen[0] == seen[1]


# --- neither module may write a .done file ----------------------------------

_WRITE_NAMES = {"write_text", "write_bytes", "touch", "open", "_atomic_write_text", "replace", "rename"}
_READ_ONLY_FUNCS = {
    "repair_pending_phases", "count_completion_sentinels", "_count_done", "_sentinel_exists",
}


def _call_name(call):
    f = call.func
    return f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")


def _strings(node):
    for n in ast.walk(node):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            yield n.value


def _scan(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    write_calls = 0
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _call_name(node) in _WRITE_NAMES:
            write_calls += 1
            for arg in list(node.args) + [k.value for k in node.keywords]:
                if any(".done" in s for s in _strings(arg)):
                    offenders.append(node.lineno)
    return write_calls, offenders


def test_auto_resume_never_writes_done():
    calls, offenders = _scan(_AR_PATH)
    assert calls > 0, "scan matched no write call; it is vacuous"
    assert offenders == []
    for line in _AR_PATH.read_text(encoding="utf-8").splitlines():
        assert not (".done" in line and "write-atomic" in line)


def test_supervisor_never_writes_done():
    tree = ast.parse(_SUP_PATH.read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    assert calls, "scan visited no call node; it is vacuous"
    _, offenders = _scan(_SUP_PATH)
    assert offenders == []
    # any ".done" string inside a function body must be in a read-only helper
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        if fn.name in _READ_ONLY_FUNCS:
            continue
        for call in [n for n in ast.walk(fn) if isinstance(n, ast.Call)]:
            for arg in list(call.args) + [k.value for k in call.keywords]:
                assert not any(s.endswith(".done") for s in _strings(arg)), (fn.name, call.lineno)
