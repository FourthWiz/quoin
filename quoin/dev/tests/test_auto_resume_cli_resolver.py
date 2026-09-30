"""Tests for the shared interpreter resolver (`resolve_cli`) in
`auto_resume.py` (IVG-281).

A fake `#!/bin/sh` interpreter stands in for a real Python so the probe
path is exercised end to end (real subprocess, real process group, real
timeout) without depending on any particular interpreter being installed.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import stat
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
_CORE_PATH = REPO_ROOT / "quoin" / "core" / "scripts" / "auto_resume.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("auto_resume_resolver_under_test", _CORE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def ar():
    return _load_module()


_FAKE_INTERPRETER = """#!/bin/sh
mode="${FAKE_MODE:-ok}"
case "$mode" in
  ok)
    echo "QUOIN_VERSION=${FAKE_V:-1.0.0}"
    exit 0
    ;;
  noisy)
    echo "sitecustomize: loaded something"
    echo "QUOIN_VERSION=${FAKE_V:-1.0.0}"
    echo "trailing noise"
    exit 0
    ;;
  fail)
    echo "ImportError: no module named quoin" 1>&2
    exit 1
    ;;
  no_token)
    echo "nothing useful here"
    exit 0
    ;;
  sleep)
    sleep 30
    exit 0
    ;;
  grandchild)
    ( sleep 30 & echo $! > "$FAKE_PIDFILE" )
    sleep 30
    exit 0
    ;;
esac
"""


def _write_fake_interpreter(dir_path: Path) -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    path = dir_path / "python"
    path.write_text(_FAKE_INTERPRETER, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _write_record(record_path: Path, **overrides) -> dict:
    record = {
        "schema": 1,
        "python": overrides.pop("python"),
        "version": "1.0.0",
        "pythonpath": None,
        "quoin_file": "/fake/quoin/__init__.py",
        "source_dir": "/fake/source",
        "source_version": None,
        "installed_at": "2026-09-29T00:00:00Z",
    }
    record.update(overrides)
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(json.dumps(record), encoding="utf-8")
    return record


def _deploy_layout(tmp_path: Path):
    """Builds `tmp/deploy/core/scripts/` so `_deploy_root()` resolves it —
    the guard requires `parent.name == 'scripts'` and `parents[1].name ==
    'core'`, so the module must actually live at that relative depth."""
    scripts_dir = tmp_path / "deploy" / "core" / "scripts"
    scripts_dir.mkdir(parents=True)
    run_state_src = _CORE_PATH.parent / "run_state.py"
    (scripts_dir / "run_state.py").write_text(run_state_src.read_text(encoding="utf-8"), encoding="utf-8")
    return scripts_dir / "auto_resume.py", tmp_path / "deploy"


def _load_at(path: Path):
    spec = importlib.util.spec_from_file_location("auto_resume_at_" + path.parent.name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ar_at_deploy(tmp_path: Path):
    module_path, deploy_root = _deploy_layout(tmp_path)
    module_path.write_text(_CORE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    return _load_at(module_path), deploy_root


# ── legacy path (no record) ─────────────────────────────────────────────────


def test_legacy_usable_via_which(ar, tmp_path, monkeypatch):
    monkeypatch.setattr(ar, "_runtime_record_path", lambda: tmp_path / "absent.json")
    monkeypatch.setattr(ar, "_which", lambda name: "/usr/bin/quoin")
    res = ar.resolve_cli(tmp_path, "start")
    assert res == {
        "source": "legacy", "status": "usable", "argv": ["/usr/bin/quoin"], "env_extra": {},
        "kind": None, "message": None, "record_path": str(tmp_path / "absent.json"),
        "record": None, "probed_version": None, "pythonpath": None,
    }


def test_legacy_usable_via_local_bin(ar, tmp_path, monkeypatch):
    fake_home = tmp_path / "fake-home"
    bin_dir = fake_home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    quoin_bin = bin_dir / "quoin"
    quoin_bin.write_text("#!/bin/sh\n")
    quoin_bin.chmod(0o755)
    monkeypatch.setattr(ar, "_runtime_record_path", lambda: None)
    monkeypatch.setattr(ar, "_which", lambda name: None)
    monkeypatch.setattr(ar, "_home", lambda: fake_home)
    res = ar.resolve_cli(tmp_path, "start")
    assert res["source"] == "legacy"
    assert res["status"] == "usable"
    assert res["argv"] == [str(quoin_bin)]


def test_legacy_missing(ar, tmp_path, monkeypatch):
    monkeypatch.setattr(ar, "_runtime_record_path", lambda: None)
    monkeypatch.setattr(ar, "_which", lambda name: None)
    monkeypatch.setattr(ar, "_home", lambda: tmp_path / "no-such-home")
    res = ar.resolve_cli(tmp_path, "start")
    assert res["source"] == "legacy"
    assert res["status"] == "missing"
    assert res["argv"] is None


# ── record validation ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "make_content",
    [
        lambda py: "not json",
        lambda py: json.dumps([1, 2, 3]),
        lambda py: json.dumps({"schema": 2, "python": py, "version": "1.0.0"}),
        lambda py: json.dumps({"schema": 1, "python": "relative/python", "version": "1.0.0"}),
        lambda py: json.dumps({"schema": 1, "python": py}),  # missing version
        lambda py: json.dumps({"schema": 1, "python": py, "version": "1.0.0", "pythonpath": "relative/path"}),
    ],
    ids=["garbage-json", "list-json", "wrong-schema", "relative-python", "missing-version", "relative-pythonpath"],
)
def test_record_invalid_variants(ar, tmp_path, monkeypatch, make_content):
    record_path = tmp_path / "quoin-runtime.json"
    record_path.write_text(make_content("/usr/bin/python3"), encoding="utf-8")
    monkeypatch.setattr(ar, "_runtime_record_path", lambda: record_path)
    which_calls = []
    monkeypatch.setattr(ar, "_which", lambda name: which_calls.append(name) or "/should/not/be/used")
    res = ar.resolve_cli(tmp_path, "start")
    assert res["status"] == "stale"
    assert res["kind"] == "record-invalid"
    assert which_calls == []  # a present (even if invalid) record never falls back to PATH


def test_record_oversize_is_invalid(ar, tmp_path, monkeypatch):
    record_path = tmp_path / "quoin-runtime.json"
    record_path.write_text("x" * (ar._RECORD_MAX_BYTES + 1), encoding="utf-8")
    monkeypatch.setattr(ar, "_runtime_record_path", lambda: record_path)
    res = ar.resolve_cli(tmp_path, "start")
    assert res["kind"] == "record-invalid"


def test_source_version_mismatch_no_probe(ar, tmp_path, monkeypatch):
    record_path = tmp_path / "quoin-runtime.json"
    _write_record(record_path, python="/usr/bin/python3", version="1.0.0", source_version="0.9.0")
    monkeypatch.setattr(ar, "_runtime_record_path", lambda: record_path)
    probe_calls = []
    monkeypatch.setattr(ar, "_probe", lambda *a, **kw: probe_calls.append(1) or {"status": "ok", "rc": 0, "stdout": "", "stderr_tail": ""})
    res = ar.resolve_cli(tmp_path, "start")
    assert res["kind"] == "version-mismatch"
    assert "0.9.0" in res["message"] and "1.0.0" in res["message"]
    assert probe_calls == []


def test_version_mismatch_with_missing_interpreter_reports_only_mismatch(ar, tmp_path, monkeypatch):
    """D-22: source_version != version is checked before the interpreter
    existence check, so a record with both problems reports only the
    version mismatch, with probed_version left null (not probed)."""
    record_path = tmp_path / "quoin-runtime.json"
    _write_record(
        record_path, python=str(tmp_path / "does-not-exist" / "python"),
        version="1.0.0", source_version="0.9.0",
    )
    monkeypatch.setattr(ar, "_runtime_record_path", lambda: record_path)
    res = ar.resolve_cli(tmp_path, "start")
    assert res["kind"] == "version-mismatch"
    assert res["probed_version"] is None


def test_interpreter_missing(ar, tmp_path, monkeypatch):
    record_path = tmp_path / "quoin-runtime.json"
    _write_record(record_path, python=str(tmp_path / "nope" / "python"))
    monkeypatch.setattr(ar, "_runtime_record_path", lambda: record_path)
    res = ar.resolve_cli(tmp_path, "start")
    assert res["kind"] == "interpreter-missing"


def test_interpreter_not_executable(ar, tmp_path, monkeypatch):
    python_path = tmp_path / "python"
    python_path.write_text("#!/bin/sh\n")
    python_path.chmod(0o644)  # no execute bit
    record_path = tmp_path / "quoin-runtime.json"
    _write_record(record_path, python=str(python_path))
    monkeypatch.setattr(ar, "_runtime_record_path", lambda: record_path)
    res = ar.resolve_cli(tmp_path, "start")
    assert res["kind"] == "interpreter-not-executable"


# ── probing (real subprocess via the fake interpreter) ──────────────────────


def _resolve_with_mode(ar, tmp_path, monkeypatch, mode, caller="start", version="1.0.0", record_version="1.0.0"):
    interp = _write_fake_interpreter(tmp_path / "fakebin")
    record_path = tmp_path / "quoin-runtime.json"
    _write_record(record_path, python=str(interp), version=record_version)
    monkeypatch.setattr(ar, "_runtime_record_path", lambda: record_path)
    monkeypatch.setenv("FAKE_MODE", mode)
    monkeypatch.setenv("FAKE_V", version)
    return ar.resolve_cli(tmp_path, caller)


def test_probe_ok_usable(ar, tmp_path, monkeypatch):
    res = _resolve_with_mode(ar, tmp_path, monkeypatch, "ok")
    assert res["status"] == "usable"
    assert res["argv"][1:] == ["-c", ar._SPAWN_BOOTSTRAP]
    assert res["probed_version"] == "1.0.0"


def test_probe_noisy_stdout_still_usable(ar, tmp_path, monkeypatch):
    res = _resolve_with_mode(ar, tmp_path, monkeypatch, "noisy")
    assert res["status"] == "usable"
    assert res["probed_version"] == "1.0.0"


def test_probe_import_failed(ar, tmp_path, monkeypatch):
    res = _resolve_with_mode(ar, tmp_path, monkeypatch, "fail")
    assert res["kind"] == "import-failed"
    assert "quoin" in res["message"].lower() or "importerror" in res["message"].lower()


def test_probe_no_token(ar, tmp_path, monkeypatch):
    res = _resolve_with_mode(ar, tmp_path, monkeypatch, "no_token")
    assert res["kind"] == "import-failed"


def test_probe_version_mismatch_message_names_both(ar, tmp_path, monkeypatch):
    res = _resolve_with_mode(ar, tmp_path, monkeypatch, "ok", version="2.0.0", record_version="1.0.0")
    assert res["kind"] == "version-mismatch"
    assert "2.0.0" in res["message"] and "1.0.0" in res["message"]


def test_probe_timeout_bounded_and_within_wall_clock(ar, tmp_path, monkeypatch):
    monkeypatch.setenv("QUOIN_AUTO_RESUME_PROBE_TIMEOUT_MS", "250")
    start = time.monotonic()
    res = _resolve_with_mode(ar, tmp_path, monkeypatch, "sleep", caller="start")
    elapsed = time.monotonic() - start
    assert res["kind"] == "probe-timeout"
    assert elapsed < 1.5  # 250ms budget + 1s kill slack, generous margin


def test_probe_timeout_grandchild_process_group_killed(ar, tmp_path, monkeypatch):
    """D-05/R-08: `killpg` must kill the whole process group the probe
    started, not just its own pid — otherwise a backgrounded grandchild
    (like the fake interpreter's own `sleep 30 &`) outlives the timeout."""
    pidfile = tmp_path / "grandchild.pid"
    monkeypatch.setenv("FAKE_PIDFILE", str(pidfile))
    monkeypatch.setenv("QUOIN_AUTO_RESUME_PROBE_TIMEOUT_MS", "250")
    start = time.monotonic()
    res = _resolve_with_mode(ar, tmp_path, monkeypatch, "grandchild", caller="start")
    elapsed = time.monotonic() - start
    assert res["kind"] == "probe-timeout"
    assert elapsed < 2.0

    # Give the grandchild a moment to have written its pid, then confirm
    # it is actually gone — a dropped killpg would leave it running for
    # the full 30s `sleep`.
    for _ in range(20):
        if pidfile.exists():
            break
        time.sleep(0.05)
    assert pidfile.exists(), "fake interpreter never wrote its grandchild pid"
    grandchild_pid = int(pidfile.read_text().strip())
    try:
        os.kill(grandchild_pid, 0)
    except ProcessLookupError:
        pass  # gone, as expected
    else:
        pytest.fail(f"grandchild pid {grandchild_pid} is still alive after the probe timeout")


def test_handoff_caller_retries_once_then_probe_timeout(ar, tmp_path, monkeypatch):
    # "handoff" ignores QUOIN_AUTO_RESUME_PROBE_TIMEOUT_MS (fixed 8s budget
    # by design — see _probe_budget_ms) — patch the budget function
    # directly so this test doesn't wait out two real 8s timeouts.
    monkeypatch.setattr(ar, "_probe_budget_ms", lambda caller: 250)
    calls = []
    real_probe = ar._probe

    def _counting_probe(*a, **kw):
        calls.append(1)
        return real_probe(*a, **kw)

    monkeypatch.setattr(ar, "_probe", _counting_probe)
    res = _resolve_with_mode(ar, tmp_path, monkeypatch, "sleep", caller="handoff")
    assert res["kind"] == "probe-timeout"
    assert len(calls) == 2


def test_stop_caller_does_not_retry(ar, tmp_path, monkeypatch):
    monkeypatch.setenv("QUOIN_AUTO_RESUME_PROBE_TIMEOUT_MS", "250")
    calls = []
    real_probe = ar._probe

    def _counting_probe(*a, **kw):
        calls.append(1)
        return real_probe(*a, **kw)

    monkeypatch.setattr(ar, "_probe", _counting_probe)
    res = _resolve_with_mode(ar, tmp_path, monkeypatch, "sleep", caller="stop")
    assert res["kind"] == "probe-timeout"
    assert len(calls) == 1


# ── neutral cwd for the probe ─────────────────────────────────────────────────
#
# A project containing a top-level `quoin.py` or `quoin/` package must never
# have that file imported by the probe just because the resolver used to run
# it with the project root as cwd (`-c` puts the working directory first on
# sys.path, ahead of the recorded interpreter's own site-packages). These
# tests use a real Python interpreter (not the fake `#!/bin/sh` one) so the
# actual import resolution is exercised, with the record's `pythonpath`
# pointed at this worktree's own `src/` so the probe can find the real
# installed package when it isn't shadowed.


def _real_quoin_version() -> str:
    about = REPO_ROOT / "src" / "quoin" / "__about__.py"
    match = re.search(r"""__version__\s*=\s*["']([^"']+)["']""", about.read_text(encoding="utf-8"))
    assert match is not None, "could not read quoin.__version__ from __about__.py"
    return match.group(1)


def _write_real_record(record_path: Path) -> None:
    record = {
        "schema": 1,
        "python": sys.executable,
        "version": _real_quoin_version(),
        "pythonpath": str(REPO_ROOT / "src"),
        "quoin_file": str(REPO_ROOT / "src" / "quoin" / "__init__.py"),
        "source_dir": str(REPO_ROOT),
        "source_version": None,
        "installed_at": "2026-09-29T00:00:00Z",
    }
    record_path.write_text(json.dumps(record), encoding="utf-8")


def test_probe_ignores_hostile_flat_module_at_project_root(ar, tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    project_root.mkdir()
    marker = tmp_path / "marker.txt"  # outside project_root: proves the hostile code ran
    (project_root / "quoin.py").write_text(
        f"open({str(marker)!r}, 'w').write('imported')\n", encoding="utf-8",
    )
    record_path = tmp_path / "quoin-runtime.json"
    _write_real_record(record_path)
    monkeypatch.setattr(ar, "_runtime_record_path", lambda: record_path)

    res = ar.resolve_cli(project_root, "cli-check")

    assert not marker.exists(), "the project root's own quoin.py was imported by the probe"
    assert res["status"] == "usable"
    assert res["probed_version"] == _real_quoin_version()


def test_probe_ignores_hostile_package_at_project_root(ar, tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    (project_root / "quoin").mkdir(parents=True)
    marker = tmp_path / "marker.txt"
    (project_root / "quoin" / "__init__.py").write_text(
        f"open({str(marker)!r}, 'w').write('imported')\n__version__ = 'hostile'\n", encoding="utf-8",
    )
    (project_root / "quoin" / "cli.py").write_text("", encoding="utf-8")
    record_path = tmp_path / "quoin-runtime.json"
    _write_real_record(record_path)
    monkeypatch.setattr(ar, "_runtime_record_path", lambda: record_path)

    res = ar.resolve_cli(project_root, "cli-check")

    assert not marker.exists(), "the project root's own quoin/ package was imported by the probe"
    assert res["status"] == "usable"
    assert res["probed_version"] == _real_quoin_version()


# Round-2 major fix (a planted package in a shared, world-writable working
# directory — the system temp dir on Linux, or on macOS whenever TMPDIR is
# unset — could be imported and run as the hand-off's own user). Two
# independent layers are pinned separately below: _neutral_cwd() must never
# resolve to a shared/unsafe directory, and the probe's own sys.path strip
# must stop the import even if something else forced it to run from one
# anyway (defense in depth — see _PROBE_SNIPPET / _SPAWN_BOOTSTRAP).


def test_probe_ignores_hostile_package_even_if_forced_to_run_from_it(ar, tmp_path, monkeypatch):
    """Belt-and-suspenders: even if _neutral_cwd() were somehow made to
    return a directory a hostile package was planted in — never the real
    shared temp directory here, only a tmp_path stand-in for it — the
    probe snippet's own sys.path[0] strip must still stop the import."""
    hostile_dir = tmp_path / "would-be-shared-tmp"
    hostile_dir.mkdir()
    marker = tmp_path / "marker.txt"
    (hostile_dir / "quoin").mkdir()
    (hostile_dir / "quoin" / "__init__.py").write_text(
        f"open({str(marker)!r}, 'w').write('imported')\n__version__ = 'hostile'\n", encoding="utf-8",
    )
    (hostile_dir / "quoin" / "cli.py").write_text("", encoding="utf-8")
    record_path = tmp_path / "quoin-runtime.json"
    _write_real_record(record_path)
    monkeypatch.setattr(ar, "_runtime_record_path", lambda: record_path)
    monkeypatch.setattr(ar, "_neutral_cwd", lambda: str(hostile_dir))

    res = ar.resolve_cli(tmp_path / "project", "cli-check")

    assert not marker.exists(), "sys.path[0] strip did not stop the hostile import"
    assert res["status"] == "usable"
    assert res["probed_version"] == _real_quoin_version()


def test_neutral_cwd_never_the_shared_temp_dir(ar, tmp_path, monkeypatch):
    """_neutral_cwd() must not resolve to tempfile.gettempdir() — a shared
    directory other local users can write to on Linux (and on macOS
    whenever TMPDIR is unset) — even though it always exists."""
    fake_shared_tmp = tmp_path / "fake-shared-tmp"
    fake_shared_tmp.mkdir()
    os.chmod(fake_shared_tmp, 0o1777)
    monkeypatch.setattr(ar.tempfile, "gettempdir", lambda: str(fake_shared_tmp))

    cwd = ar._neutral_cwd()

    assert cwd is not None
    assert cwd != str(fake_shared_tmp)


def test_neutral_cwd_is_the_filesystem_root(ar):
    """The cwd is the filesystem root: root-owned, never group- or
    other-writable on a sane host, and never repository or user content."""
    assert ar._neutral_cwd() == os.path.abspath(os.sep)


def test_neutral_cwd_unaffected_by_a_group_writable_deploy_root(tmp_path):
    """Hosts with user-private groups and a 002 umask leave ~/.claude
    group-writable by the user's own group. That is not an exposure, and it
    must not stop the hand-off: the deploy root is simply not the cwd."""
    ar_at, deploy_root = _ar_at_deploy(tmp_path)
    os.chmod(deploy_root, 0o775)
    assert ar_at._neutral_cwd() == os.path.abspath(os.sep)


def test_neutral_cwd_unaffected_by_a_symlinked_deploy_root(tmp_path):
    ar_at, deploy_root = _ar_at_deploy(tmp_path)
    real_dir = tmp_path / "real-elsewhere"
    real_dir.mkdir()
    import shutil as _shutil
    _shutil.rmtree(deploy_root)
    deploy_root.symlink_to(real_dir)
    assert ar_at._neutral_cwd() == os.path.abspath(os.sep)


def test_neutral_cwd_independent_of_deploy_root(ar):
    import unittest.mock
    with unittest.mock.patch.object(ar, "_deploy_root", return_value=None):
        assert ar._neutral_cwd() == os.path.abspath(os.sep)


def _fake_stat(mode: int, uid: int):
    return os.stat_result((mode, 1, 1, 1, uid, 0, 4096, 0, 0, 0))


def test_neutral_cwd_rejects_a_root_other_users_can_write(ar, monkeypatch):
    """Fail closed, never fall back to a shared directory, if the root
    itself is writable by group or other (or is not a plain directory)."""
    monkeypatch.setattr(ar.os, "lstat", lambda p: _fake_stat(stat.S_IFDIR | 0o777, 0))
    assert ar._neutral_cwd() is None
    monkeypatch.setattr(ar.os, "lstat", lambda p: _fake_stat(stat.S_IFDIR | 0o775, 0))
    assert ar._neutral_cwd() is None
    monkeypatch.setattr(ar.os, "lstat", lambda p: _fake_stat(stat.S_IFLNK | 0o755, 0))
    assert ar._neutral_cwd() is None


def test_neutral_cwd_rejects_a_root_owned_by_another_ordinary_user(ar, monkeypatch):
    other_uid = os.getuid() + 1 if os.getuid() != 0 else 1234
    monkeypatch.setattr(ar.os, "lstat", lambda p: _fake_stat(stat.S_IFDIR | 0o755, other_uid))
    assert ar._neutral_cwd() is None
    monkeypatch.setattr(ar.os, "lstat", lambda p: _fake_stat(stat.S_IFDIR | 0o755, os.getuid()))
    assert ar._neutral_cwd() == os.path.abspath(os.sep)


def test_snippets_import_nothing_but_sys_and_os_before_the_path_strip(ar):
    """`runpy` is not frozen before Python 3.11, so importing it before the
    strip would resolve it through the cwd; only the start-up modules
    `sys` and `os` may be imported first, and the strip must precede every
    other statement."""
    import ast
    for snippet in (ar._PROBE_SNIPPET, ar._SPAWN_BOOTSTRAP):
        tree = ast.parse(snippet)
        stripped = False
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Subscript) and isinstance(t.value, ast.Attribute)
                and t.value.attr == "path" for t in node.targets
            ):
                stripped = True
                continue
            if stripped:
                continue
            assert isinstance(node, (ast.Import, ast.Assign)), ast.dump(node)
            if isinstance(node, ast.Import):
                assert {a.name for a in node.names} <= {"sys", "os"}, ast.dump(node)
        assert stripped, snippet


def test_resolve_cli_stale_when_no_safe_cwd_available(ar, tmp_path, monkeypatch):
    """The probe must be refused, not silently run from an unsafe or
    made-up directory, when _neutral_cwd() can't find a safe one."""
    interp = _write_fake_interpreter(tmp_path / "fakebin")
    record_path = tmp_path / "quoin-runtime.json"
    _write_record(record_path, python=str(interp))
    monkeypatch.setattr(ar, "_runtime_record_path", lambda: record_path)
    monkeypatch.setattr(ar, "_neutral_cwd", lambda: None)
    monkeypatch.setenv("FAKE_MODE", "ok")

    res = ar.resolve_cli(tmp_path / "project", "cli-check")

    assert res["status"] == "stale"
    assert res["kind"] == "no-safe-cwd"
    assert "chmod go-w /" in res["message"]
    assert "owned by root" in res["message"]
    assert "quoin install" not in res["message"]


def test_neutral_cwd_without_getuid_does_not_raise(ar, monkeypatch):
    """Some platforms have no os.getuid at all (e.g. Windows); the check
    must still run (falling back to root-only ownership) rather than
    raising AttributeError."""
    monkeypatch.delattr(os, "getuid", raising=False)

    result = ar._neutral_cwd()

    assert result is None or isinstance(result, str)


def test_neutral_cwd_root_owned_and_locked_down_is_safe(ar, monkeypatch):
    root = os.path.abspath(os.sep)

    class _FakeStat:
        st_mode = stat.S_IFDIR | 0o755
        st_uid = 0

    monkeypatch.setattr(os, "lstat", lambda path: _FakeStat())
    monkeypatch.delattr(os, "getuid", raising=False)

    assert ar._neutral_cwd() == root


def test_neutral_cwd_owned_by_other_user_is_unsafe(ar, monkeypatch):
    class _FakeStat:
        st_mode = stat.S_IFDIR | 0o755
        st_uid = 501

    monkeypatch.setattr(os, "lstat", lambda path: _FakeStat())
    monkeypatch.setattr(os, "getuid", lambda: 502, raising=False)

    assert ar._neutral_cwd() is None


def test_probe_project_root_is_quoin_repo_itself_still_resolves(ar, tmp_path, monkeypatch):
    """This worktree's own root has a top-level `quoin/` directory (the
    hooks/skills/scripts source tree, no `__init__.py`) — using this tool
    on a checkout of itself must not be broken by the cwd fix."""
    record_path = tmp_path / "quoin-runtime.json"
    _write_real_record(record_path)
    monkeypatch.setattr(ar, "_runtime_record_path", lambda: record_path)

    res = ar.resolve_cli(REPO_ROOT, "cli-check")

    assert res["status"] == "usable"
    assert res["probed_version"] == _real_quoin_version()


def test_probe_cwd_is_never_project_root(ar, tmp_path, monkeypatch):
    interp = _write_fake_interpreter(tmp_path / "fakebin")
    record_path = tmp_path / "quoin-runtime.json"
    _write_record(record_path, python=str(interp))
    monkeypatch.setattr(ar, "_runtime_record_path", lambda: record_path)
    project_root = tmp_path / "project"
    project_root.mkdir()
    captured_cwds = []
    real_probe = ar._probe

    def _capturing_probe(argv0, env, cwd, timeout_s):
        captured_cwds.append(cwd)
        return real_probe(argv0, env, cwd, timeout_s)

    monkeypatch.setattr(ar, "_probe", _capturing_probe)
    monkeypatch.setenv("FAKE_MODE", "ok")
    res = ar.resolve_cli(project_root, "start")
    assert res["status"] == "usable"
    assert captured_cwds and all(cwd != str(project_root) for cwd in captured_cwds)


def test_probe_missing_project_root_no_longer_misreported_as_broken_interpreter(ar, tmp_path, monkeypatch):
    """A missing --project-root used to reach Popen's cwd directly and
    raise OSError, mapped to interpreter-not-executable — misleading,
    since the interpreter itself was fine. Running the probe from a
    neutral cwd (the quoin deploy root, verified private — see
    _neutral_cwd) removes this case along with the shadowing risk: cwd no
    longer depends on project_root at all."""
    interp = _write_fake_interpreter(tmp_path / "fakebin")
    record_path = tmp_path / "quoin-runtime.json"
    _write_record(record_path, python=str(interp))
    monkeypatch.setattr(ar, "_runtime_record_path", lambda: record_path)
    monkeypatch.setenv("FAKE_MODE", "ok")
    missing_project_root = tmp_path / "does-not-exist-at-all"
    res = ar.resolve_cli(missing_project_root, "start")
    assert res["status"] == "usable"
    assert res["kind"] is None


# ── timeout remedy text per caller ───────────────────────────────────────────


def test_timeout_remedy_start_stop_names_knob(ar, tmp_path, monkeypatch):
    monkeypatch.setenv("QUOIN_AUTO_RESUME_PROBE_TIMEOUT_MS", "250")
    for caller in ("start", "stop"):
        res = _resolve_with_mode(ar, tmp_path, monkeypatch, "sleep", caller=caller)
        assert "QUOIN_AUTO_RESUME_PROBE_TIMEOUT_MS" in res["message"]
        assert "<task>" not in res["message"]


def test_timeout_remedy_handoff_and_cli_check_no_knob_mention(ar, tmp_path, monkeypatch):
    # Both callers ignore the timeout knob (fixed 8s budget) — patch the
    # budget function directly so this test doesn't wait out real 8s/16s
    # timeouts on every run.
    monkeypatch.setattr(ar, "_probe_budget_ms", lambda caller: 250)
    for caller in ("handoff", "cli-check"):
        res = _resolve_with_mode(ar, tmp_path, monkeypatch, "sleep", caller=caller)
        # No literal <task> placeholder: the remedy is generic instead of
        # naming a task this resolver-level call has no access to.
        assert "retry the auto-resume hand-off" in res["message"]
        assert "<task>" not in res["message"]
        assert "QUOIN_AUTO_RESUME_PROBE_TIMEOUT_MS" not in res["message"]


# ── knob clamps ──────────────────────────────────────────────────────────────


def test_knob_clamps(ar, monkeypatch):
    monkeypatch.setenv("QUOIN_AUTO_RESUME_PROBE_TIMEOUT_MS", "99999")
    assert ar._probe_budget_ms("start") == 3000
    assert ar._probe_budget_ms("stop") == 7000
    monkeypatch.setenv("QUOIN_AUTO_RESUME_PROBE_TIMEOUT_MS", "10")
    assert ar._probe_budget_ms("start") == 250
    assert ar._probe_budget_ms("stop") == 250
    monkeypatch.setenv("QUOIN_AUTO_RESUME_PROBE_TIMEOUT_MS", "junk")
    assert ar._probe_budget_ms("start") == 1500
    assert ar._probe_budget_ms("stop") == 3000
    monkeypatch.delenv("QUOIN_AUTO_RESUME_PROBE_TIMEOUT_MS", raising=False)
    assert ar._probe_budget_ms("handoff") == 8000
    assert ar._probe_budget_ms("cli-check") == 8000


# ── memo ──────────────────────────────────────────────────────────────────


def test_memo_one_probe_two_calls_then_reset(ar, tmp_path, monkeypatch):
    interp = _write_fake_interpreter(tmp_path / "fakebin")
    record_path = tmp_path / "quoin-runtime.json"
    _write_record(record_path, python=str(interp))
    monkeypatch.setattr(ar, "_runtime_record_path", lambda: record_path)
    monkeypatch.setenv("FAKE_MODE", "ok")
    monkeypatch.setenv("FAKE_V", "1.0.0")
    calls = []
    real_probe = ar._probe

    def _counting_probe(*a, **kw):
        calls.append(1)
        return real_probe(*a, **kw)

    monkeypatch.setattr(ar, "_probe", _counting_probe)
    r1 = ar.resolve_cli(tmp_path, "start")
    r2 = ar.resolve_cli(tmp_path, "start")
    assert r1 == r2
    assert len(calls) == 1
    ar._reset_cli_memo()
    ar.resolve_cli(tmp_path, "start")
    assert len(calls) == 2


# ── pythonpath env composition ──────────────────────────────────────────────


def test_pythonpath_env_composition_no_prior(ar, tmp_path, monkeypatch):
    monkeypatch.delenv("PYTHONPATH", raising=False)
    record_path = tmp_path / "quoin-runtime.json"
    interp = _write_fake_interpreter(tmp_path / "fakebin")
    _write_record(record_path, python=str(interp), pythonpath=str(tmp_path / "src"))
    monkeypatch.setattr(ar, "_runtime_record_path", lambda: record_path)
    monkeypatch.setenv("FAKE_MODE", "ok")
    monkeypatch.setenv("FAKE_V", "1.0.0")
    res = ar.resolve_cli(tmp_path, "start")
    assert res["env_extra"]["PYTHONPATH"] == str(tmp_path / "src")
    assert res["env_extra"]["QUOIN_HANDOFF_PYTHONPATH"] == str(tmp_path / "src")


def test_pythonpath_env_composition_with_prior(ar, tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/pre/existing")
    record_path = tmp_path / "quoin-runtime.json"
    interp = _write_fake_interpreter(tmp_path / "fakebin")
    _write_record(record_path, python=str(interp), pythonpath=str(tmp_path / "src"))
    monkeypatch.setattr(ar, "_runtime_record_path", lambda: record_path)
    monkeypatch.setenv("FAKE_MODE", "ok")
    monkeypatch.setenv("FAKE_V", "1.0.0")
    res = ar.resolve_cli(tmp_path, "start")
    assert res["env_extra"]["PYTHONPATH"] == str(tmp_path / "src") + os.pathsep + "/pre/existing"


# ── _deploy_root guard ───────────────────────────────────────────────────────


def test_deploy_root_none_for_non_matching_layout(ar, tmp_path):
    other = tmp_path / "somewhere" / "else.py"
    other.parent.mkdir(parents=True)
    import unittest.mock
    with unittest.mock.patch("os.path.abspath", return_value=str(other)):
        assert ar._deploy_root() is None


def test_deploy_root_matches_layout(tmp_path):
    ar_at, deploy_root = _ar_at_deploy(tmp_path)
    assert ar_at._deploy_root() == deploy_root
    assert ar_at._runtime_record_path() == deploy_root / "quoin-runtime.json"


# ── totality: resolve_cli never raises ──────────────────────────────────────


def test_totality_probe_raises(ar, tmp_path, monkeypatch):
    record_path = tmp_path / "quoin-runtime.json"
    _write_record(record_path, python=sys.executable)
    monkeypatch.setattr(ar, "_runtime_record_path", lambda: record_path)
    monkeypatch.setattr(ar, "_probe", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    res = ar.resolve_cli(tmp_path, "start")
    assert res["kind"] == "resolver-error"
    assert "install" in res["message"] or "doctor" in res["message"]
    # not memoized — a second call re-runs the resolver
    calls = []
    monkeypatch.setattr(ar, "_probe", lambda *a, **kw: calls.append(1) or {"status": "ok", "rc": 0, "stdout": "QUOIN_VERSION=1.0.0", "stderr_tail": ""})
    res2 = ar.resolve_cli(tmp_path, "start")
    assert calls == [1]
    assert res2["status"] == "usable"


def test_totality_record_read_raises_permission_error(ar, tmp_path, monkeypatch):
    record_path = tmp_path / "quoin-runtime.json"
    _write_record(record_path, python=sys.executable)
    monkeypatch.setattr(ar, "_runtime_record_path", lambda: record_path)

    def _boom(self, *a, **kw):
        raise PermissionError("no access")

    monkeypatch.setattr(Path, "read_text", _boom)
    res = ar.resolve_cli(tmp_path, "start")
    # PermissionError is an OSError, caught inside _load_runtime_record —
    # this is record-invalid, not resolver-error (only a non-OSError inside
    # the uncached resolver reaches the outer totality wrapper).
    assert res["kind"] == "record-invalid"


def test_totality_tempfile_creation_raises_oserror(ar, tmp_path, monkeypatch):
    interp = _write_fake_interpreter(tmp_path / "fakebin")
    record_path = tmp_path / "quoin-runtime.json"
    _write_record(record_path, python=str(interp))
    monkeypatch.setattr(ar, "_runtime_record_path", lambda: record_path)
    monkeypatch.setattr(ar.tempfile, "mkstemp", lambda *a, **kw: (_ for _ in ()).throw(OSError("no space")))
    res = ar.resolve_cli(tmp_path, "start")
    assert res["kind"] == "resolver-error"
    assert "install" in res["message"] or "doctor" in res["message"]


# ── cli-check subcommand ─────────────────────────────────────────────────────


class _Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_cli_check_prints_one_json_line_no_files_created(ar, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ar, "_runtime_record_path", lambda: None)
    monkeypatch.setattr(ar, "_which", lambda name: None)
    monkeypatch.setattr(ar, "_home", lambda: tmp_path / "no-home")
    memory_dir = tmp_path / ".workflow_artifacts" / "memory"
    rc = ar._cmd_cli_check(_Args(project_root=str(tmp_path)))
    assert rc == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1
    data = json.loads(out[0])
    assert data["source"] == "legacy"
    assert data["status"] == "missing"
    assert not memory_dir.exists()


def test_cli_check_exception_path_prints_error_json(ar, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ar, "resolve_cli", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("kaboom")))
    rc = ar._cmd_cli_check(_Args(project_root=str(tmp_path)))
    assert rc == 0
    data = json.loads(capsys.readouterr().out.strip())
    assert data["status"] == "error"
