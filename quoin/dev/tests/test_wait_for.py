"""Behavior tests for the detached-start and bounded foreground wait helper."""
from __future__ import annotations

import importlib.util
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
CORE_PATH = REPO_ROOT / "quoin" / "core" / "scripts" / "wait_for.py"
WRAPPER_PATH = REPO_ROOT / "quoin" / "scripts" / "wait_for.py"
INSTALLER_PY = REPO_ROOT / "src" / "quoin" / "installer.py"


def _load():
    spec = importlib.util.spec_from_file_location("wait_for_under_test", CORE_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


wf = _load()


def _cli(*args, script=CORE_PATH, env=None):
    return subprocess.run(
        [sys.executable, str(script), *map(str, args)],
        capture_output=True, text=True, timeout=60, env=env,
    )


def _start(tmp_path, *cmd, token="tok1", extra=()):
    rc = tmp_path / "job.rc"
    r = _cli("start", "--rc-file", rc, "--token", token, "--log", tmp_path / "job.log", *extra, "--", *cmd)
    return rc, r


def _wait_ready(rc, token="tok1", limit=20):
    end = time.time() + limit
    out = None
    while time.time() < end:
        out = _cli("wait", "--file", rc, "--token", token, "--max-secs", "2", "--poll-secs", "1")
        if out.stdout.startswith("READY"):
            return out
    return out


def _fake_clock():
    t = [1000.0]
    return (lambda: t[0]), (lambda s: t.__setitem__(0, t[0] + s))


def test_ready_with_matching_token(tmp_path):
    f = tmp_path / "f.rc"
    f.write_text("tokA 5\n")
    assert wf.do_wait(str(f), "tokA", 10, 1, 3600) == ("READY|5", 0)


def test_waiting_on_deadline_with_injected_clock(tmp_path):
    clock, sleep = _fake_clock()
    line, code = wf.do_wait(str(tmp_path / "none.rc"), "tokA", 30, 5, 3600, clock, sleep)
    assert code == 1 and line.startswith("WAITING|")


def test_stale_token_ignored_until_right_token(tmp_path):
    f = tmp_path / "f.rc"
    f.write_text("old 9\n")
    clock, sleep = _fake_clock()
    assert wf.do_wait(str(f), "new", 10, 5, 3600, clock, sleep)[1] == 1
    f.write_text("new 3\n")
    assert wf.do_wait(str(f), "new", 10, 5, 3600, clock, sleep) == ("READY|3", 0)


@pytest.mark.parametrize("content", ["", "garbage", "tokA notanint", "tokA 1 2 3"])
def test_malformed_content_is_waiting(tmp_path, content):
    f = tmp_path / "f.rc"
    f.write_text(content)
    clock, sleep = _fake_clock()
    assert wf.do_wait(str(f), "tokA", 6, 3, 3600, clock, sleep)[1] == 1


def test_no_token_reads_last_integer_field(tmp_path):
    f = tmp_path / "f.rc"
    f.write_text("whatever 4\n")
    assert wf.do_wait(str(f), None, 5, 1, 3600) == ("READY|4", 0)


@pytest.mark.parametrize("token", ["a b", "../x", "", "a/b"])
def test_token_validation_rejects(tmp_path, token):
    r = _cli("start", "--rc-file", tmp_path / "f.rc", "--token", token, "--log", tmp_path / "l", "--", sys.executable, "-c", "pass")
    assert r.returncode == 2 and r.stdout.startswith("ERROR|")


def test_clamps_and_env_budget(monkeypatch, tmp_path):
    seen = {}

    def fake_wait(target, token, max_secs, poll_secs, budget_secs, *a, **k):
        seen.update(max=max_secs, poll=poll_secs, budget=budget_secs)
        return "WAITING|0", 1

    monkeypatch.setattr(wf, "do_wait", fake_wait)
    wf.main(["wait", "--token", "t1", "--file", str(tmp_path / "x"), "--max-secs", "9999", "--poll-secs", "0", "--budget-secs", "1"])
    assert seen == {"max": 570, "poll": 1, "budget": 60}
    wf.main(["wait", "--token", "t1", "--file", str(tmp_path / "x")])
    assert seen == {"max": 540, "poll": 5, "budget": 3600}
    monkeypatch.setenv("QUOIN_WAIT_BUDGET_SECS", "99999")
    wf.main(["wait", "--token", "t1", "--file", str(tmp_path / "x")])
    assert seen["budget"] == 14400
    monkeypatch.setenv("QUOIN_WAIT_BUDGET_SECS", "junk")
    wf.main(["wait", "--token", "t1", "--file", str(tmp_path / "x")])
    assert seen["budget"] == 3600


def test_start_end_to_end_exit_code(tmp_path):
    rc, r = _start(tmp_path, sys.executable, "-c", "import sys; sys.exit(7)")
    assert r.returncode == 0 and r.stdout.startswith("STARTED|")
    out = _wait_ready(rc)
    assert out.stdout.strip() == "READY|7" and out.returncode == 0
    assert not Path(str(rc) + ".tmp").exists()
    assert re.match(r"^tok1 -?\d+$", rc.read_text().strip())


def test_nonexistent_executable_gives_127(tmp_path):
    rc, r = _start(tmp_path, str(tmp_path / "no-such-binary"))
    assert r.returncode == 0
    assert _wait_ready(rc).stdout.strip() == "READY|127"


def test_bad_cwd_is_an_error(tmp_path):
    rc, r = _start(tmp_path, sys.executable, "-c", "pass", extra=("--cwd", str(tmp_path / "nope")))
    assert r.returncode == 2 and r.stdout.startswith("ERROR|")
    assert not rc.exists()


def test_empty_command_is_an_error(tmp_path):
    r = _cli("start", "--rc-file", tmp_path / "f.rc", "--token", "t", "--log", tmp_path / "l", "--")
    assert r.returncode == 2 and r.stdout.startswith("ERROR|")


def test_start_removes_old_rc_and_start_files(tmp_path):
    rc = tmp_path / "job.rc"
    rc.write_text("old 1\n")
    Path(str(rc) + ".start").write_text("old 1 1\n")
    _cli("start", "--rc-file", rc, "--token", "fresh", "--log", tmp_path / "l", "--", sys.executable, "-c", "import time; time.sleep(30)")
    assert not rc.exists()
    pid = int(Path(str(rc) + ".start").read_text().split()[1])
    os.killpg(pid, signal.SIGKILL)


def _spawn_sleeper(tmp_path, token="tok1"):
    marker = tmp_path / "child.up"
    code = "import pathlib,time; pathlib.Path({!r}).write_text('x'); time.sleep(60)".format(str(marker))
    rc, r = _start(tmp_path, sys.executable, "-c", code, token=token)
    assert r.stdout.startswith("STARTED|")
    end = time.time() + 15
    while not marker.exists() and time.time() < end:
        time.sleep(0.05)
    assert marker.exists()
    return rc, int(r.stdout.strip().split("|")[1])


def _real_sleep_short(s):
    time.sleep(min(s, 0.05))


def test_killed_runner_is_dead(tmp_path):
    rc, pid = _spawn_sleeper(tmp_path)
    try:
        os.kill(pid, signal.SIGKILL)
        end = time.time() + 10
        res = None
        while time.time() < end:
            res = wf.do_wait(str(rc), "tok1", 5, 1, 3600, sleep=_real_sleep_short)
            if res[1] == 3:
                break
        assert res[1] == 3 and res[0].startswith("DEAD|")
        assert not rc.exists()
    finally:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_budget_exceeded_expires_and_stops_group(tmp_path):
    rc, pid = _spawn_sleeper(tmp_path)
    try:
        start_epoch = int(Path(str(rc) + ".start").read_text().split()[2])
        line, code = wf.do_wait(str(rc), "tok1", 5, 1, 60, clock=lambda: start_epoch + 1000.0, sleep=time.sleep)
        assert code == 4 and line.startswith("EXPIRED|")
        # the runner's SIGTERM handler leaves an rc of 143
        out = _wait_ready(rc, limit=10)
        assert out.stdout.strip() == "READY|143"
        end = time.time() + 10
        while time.time() < end:
            try:
                os.killpg(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        with pytest.raises(ProcessLookupError):
            os.killpg(pid, 0)
    finally:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_start_never_uses_a_shell():
    assert "shell=True" not in CORE_PATH.read_text(encoding="utf-8")
    assert "os.system" not in CORE_PATH.read_text(encoding="utf-8")


def test_wrapper_cli_parity(tmp_path):
    f = tmp_path / "f.rc"
    f.write_text("tokA 5\n")
    a = _cli("wait", "--file", f, "--token", "tokA", script=CORE_PATH)
    b = _cli("wait", "--file", f, "--token", "tokA", script=WRAPPER_PATH)
    assert a.stdout == b.stdout == "READY|5\n" and a.returncode == b.returncode == 0


def test_installer_registers_in_both_script_tuples():
    spec = importlib.util.spec_from_file_location("installer_under_test", INSTALLER_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert "wait_for.py" in mod.DEPLOYED_SCRIPTS
    assert "wait_for.py" in mod.CORE_SCRIPTS


def test_core_imports_under_python_38(tmp_path):
    exe = shutil.which("python3.8")
    if not exe:
        pytest.skip("no python3.8 on PATH")
    code = (
        "import importlib.util,sys;"
        "s=importlib.util.spec_from_file_location('m',sys.argv[1]);"
        "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);print('ok')"
    )
    r = subprocess.run([exe, "-c", code, str(CORE_PATH)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "ok"


def test_wait_requires_token(tmp_path):
    out = _cli("wait", "--file", str(tmp_path / "x.rc"))
    assert out.stdout.strip() == "ERROR|missing --token"
    assert out.returncode == 2


def test_token_must_start_alphanumeric(tmp_path):
    line, code = wf.do_start(str(tmp_path / "a.rc"), "-bad", str(tmp_path / "a.log"), None, ["true"])
    assert (line, code) == ("ERROR|bad token", 2)


def test_start_refuses_missing_directory_without_spawning(tmp_path):
    line, code = wf.do_start(str(tmp_path / "nodir" / "a.rc"), "tok1", str(tmp_path / "a.log"), None, ["true"])
    assert code == 2 and line.startswith("ERROR|directory missing")
    line, code = wf.do_start(str(tmp_path / "a.rc"), "tok1", str(tmp_path / "nodir" / "a.log"), None, ["true"])
    assert code == 2 and line.startswith("ERROR|directory missing")


@pytest.mark.parametrize("pid", [0, 1])
def test_start_record_with_unsafe_pid_is_ignored(tmp_path, pid):
    rc = tmp_path / "a.rc"
    Path(str(rc) + ".start").write_text("tok1 %d 1\n" % pid)
    called = []
    ticks = iter(range(10**9, 10**9 + 100))
    res = wf.do_wait(str(rc), "tok1", 1, 1, 60, clock=lambda: next(ticks), sleep=lambda s: None,
                     pid_alive=lambda p: called.append(p) or False)
    assert res[1] == 1 and not called


def test_terminate_group_skips_foreign_group(monkeypatch):
    sent = []
    monkeypatch.setattr(wf.os, "getpgid", lambda pid: pid + 1)
    monkeypatch.setattr(wf.os, "killpg", lambda pid, sig: sent.append((pid, sig)))
    wf._terminate_group(4242, lambda s: None)
    assert sent == []
