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
    assert res["argv"][1:] == ["-m", "quoin"]
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
    monkeypatch.setenv("QUOIN_AUTO_RESUME_PROBE_TIMEOUT_MS", "250")
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


# ── timeout remedy text per caller ───────────────────────────────────────────


def test_timeout_remedy_start_stop_names_knob(ar, tmp_path, monkeypatch):
    monkeypatch.setenv("QUOIN_AUTO_RESUME_PROBE_TIMEOUT_MS", "250")
    for caller in ("start", "stop"):
        res = _resolve_with_mode(ar, tmp_path, monkeypatch, "sleep", caller=caller)
        assert "QUOIN_AUTO_RESUME_PROBE_TIMEOUT_MS" in res["message"]
        assert "<task>" not in res["message"]


def test_timeout_remedy_handoff_and_cli_check_no_knob_mention(ar, tmp_path, monkeypatch):
    monkeypatch.setenv("QUOIN_AUTO_RESUME_PROBE_TIMEOUT_MS", "250")
    for caller in ("handoff", "cli-check"):
        res = _resolve_with_mode(ar, tmp_path, monkeypatch, "sleep", caller=caller)
        # No literal <task> placeholder (review-1.md issue 3): the remedy is
        # generic instead of naming a task this resolver-level call has no
        # access to.
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
