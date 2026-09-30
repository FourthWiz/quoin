"""Behaviour of the stdlib fake OpenCode executable, driven without any Quoin driver code."""
from __future__ import annotations

import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="process groups and signals are POSIX-only")

REPO_ROOT = Path(__file__).resolve().parents[3]
FAKE_PATH = Path(__file__).resolve().parent / "fakes" / "fake_opencode.py"
FIXTURES = REPO_ROOT / "quoin" / "adapters" / "opencode" / "fixtures" / "runtime-events"
TIMEOUT = 20


def _load_fake():
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec = importlib.util.spec_from_file_location("fake_opencode_under_test", FAKE_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.dont_write_bytecode = previous


fake = _load_fake()


class Harness:
    def __init__(self, tmp_path: Path, scenario: dict):
        self.tmp = tmp_path
        self.state = tmp_path / "state"
        self.cwd = tmp_path / "work"
        self.cwd.mkdir()
        scenario_path = fake.write_scenario(tmp_path / "scenario.json", scenario)
        self.shim = fake.write_shim(tmp_path / "bin", scenario_path, self.state)
        self.procs = []

    def env(self):
        env = dict(os.environ)
        env["FAKE_TEST_SECRET"] = "s3cr3t-value-123"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return env

    def run(self, *args, timeout=TIMEOUT):
        return subprocess.run(
            [str(self.shim), *args], cwd=str(self.cwd), env=self.env(),
            capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL,
        )

    def start(self, *args):
        proc = subprocess.Popen(
            [str(self.shim), *args], cwd=str(self.cwd), env=self.env(),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.procs.append(proc)
        return proc

    def invocations(self):
        path = self.state / "invocations.jsonl"
        if not path.exists():
            return []
        return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]

    def grandchildren(self):
        path = self.state / "grandchildren.txt"
        if not path.exists():
            return []
        return [int(x) for x in path.read_text().split()]

    def wait_for(self, predicate, seconds=10):
        deadline = time.time() + seconds
        while time.time() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.05)
        raise AssertionError("condition not reached")

    def cleanup(self):
        for proc in self.procs:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        for pid in self.grandchildren():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.fixture()
def make(tmp_path):
    made = []

    def _make(name, *builder_args, **builder_kwargs):
        scenario = fake.SCENARIOS[name](*builder_args, **builder_kwargs)
        sub = tmp_path / ("h%d" % len(made))
        sub.mkdir()
        harness = Harness(sub, scenario)
        made.append(harness)
        return harness

    yield _make
    for harness in made:
        harness.cleanup()


def _alive(pid):
    """Running, not merely present: a zombie still answers signal 0."""
    res = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    stat = res.stdout.strip()
    return bool(stat) and not stat.startswith("Z")


def _ready(h, pid):
    return (h.state / ("grandchild-%d.ready" % pid)).exists()


def _strip_ts(lines):
    out = []
    for raw in lines:
        try:
            obj = json.loads(raw)
        except ValueError:
            out.append(raw)
            continue
        if isinstance(obj, dict):
            obj.pop("timestamp", None)
        out.append(json.dumps(obj, sort_keys=True))
    return out


def test_replay_stdout_equals_rebased_fixture(make):
    h = make("replay", "plain-complete.jsonl")
    before = int(time.time() * 1000)
    res = h.run("run", "--format", "json", "--", "do it")
    after = int(time.time() * 1000)
    assert res.returncode == 0
    got = res.stdout.decode().splitlines()
    want = (FIXTURES / "plain-complete.jsonl").read_text().splitlines()
    assert _strip_ts(got) == _strip_ts(want)
    stamps = [json.loads(l)["timestamp"] for l in got]
    assert before <= stamps[0] and stamps[-1] <= after + 10_000


def test_version_output_and_mismatch(make):
    h = make("record_only")
    res = h.run("--version")
    assert res.returncode == 0 and res.stdout.decode().strip() == "1.18.32"
    assert h.invocations()[0]["attempt"] is None
    h2 = make("version_mismatch")
    assert h2.run("--version").stdout.decode().strip() == "1.18.31"


def test_crash_scenarios_give_negative_returncode(make):
    for name, sig in (("crash_after_start", signal.SIGKILL), ("crash_mid_stream", signal.SIGSEGV),
                      ("crash_in_open_step", signal.SIGABRT), ("crash_after_last_finish", signal.SIGKILL)):
        h = make(name)
        res = h.run("run", "--", "x")
        assert res.returncode == -sig, name
        assert b"step_start" in res.stdout


def test_hang_is_killed_by_timeout(make):
    h = make("hang")
    proc = h.start("run", "--", "x")
    with pytest.raises(subprocess.TimeoutExpired):
        proc.wait(timeout=0.5)
    os.killpg(proc.pid, signal.SIGKILL)
    proc.wait(timeout=5)


def test_early_exit_and_missing_step_finish(make):
    h = make("exit_early")
    res = h.run("run", "--", "x")
    assert res.returncode == 1 and res.stdout == b""
    h2 = make("exit0_without_step_finish")
    res2 = h2.run("run", "--", "x")
    assert res2.returncode == 0 and b"step_finish" not in res2.stdout


def test_grandchild_shares_group_and_group_kill_removes_both(make):
    h = make("grandchild")
    proc = h.start("run", "--", "x")
    (gpid,) = h.wait_for(h.grandchildren)
    h.wait_for(lambda: _ready(h, gpid))
    record = h.wait_for(h.invocations)[0]
    assert os.getpgid(gpid) == record["pgid"] == proc.pid
    os.killpg(record["pgid"], signal.SIGKILL)
    proc.wait(timeout=5)
    h.wait_for(lambda: not _alive(gpid))


def test_ignore_term_child_survives_sigterm_then_dies_to_sigkill(make):
    h = make("grandchild_ignore_term")
    proc = h.start("run", "--", "x")
    (gpid,) = h.wait_for(h.grandchildren)
    h.wait_for(lambda: _ready(h, gpid))
    os.killpg(proc.pid, signal.SIGTERM)
    time.sleep(0.5)
    assert proc.poll() is None and _alive(gpid)
    os.killpg(proc.pid, signal.SIGKILL)
    proc.wait(timeout=5)
    h.wait_for(lambda: not _alive(gpid))


def test_detached_grandchild_escapes_the_group_kill(make):
    h = make("grandchild_detached")
    proc = h.start("run", "--", "x")
    (gpid,) = h.wait_for(h.grandchildren)
    h.wait_for(lambda: _ready(h, gpid))
    record = h.wait_for(h.invocations)[0]
    assert os.getpgid(gpid) != record["pgid"] and os.getsid(gpid) == gpid
    os.killpg(record["pgid"], signal.SIGKILL)
    proc.wait(timeout=5)
    time.sleep(0.3)
    assert _alive(gpid)
    os.kill(gpid, signal.SIGKILL)
    h.wait_for(lambda: not _alive(gpid))


def test_invocation_record_has_cwd_and_env_names_only(make):
    h = make("record_only")
    h.run("run", "--format", "json", "--agent", "writer", "--", "hello world")
    (rec,) = h.invocations()
    assert rec["cwd"] == os.path.realpath(str(h.cwd)) or rec["cwd"] == str(h.cwd)
    assert "FAKE_TEST_SECRET" in rec["env_names"]
    assert "s3cr3t-value-123" not in json.dumps(rec)
    assert rec["attempt"] == 1 and rec["parsed"]["agent"] == "writer"
    assert rec["parsed"]["message"] == ["hello world"]
    assert rec["pid"] and rec["pgid"]


def test_unknown_session_exits_before_stdout(make):
    h = make("record_only")
    res = h.run("run", "--session", "ses_nope", "--", "x")
    assert res.returncode == 1 and res.stdout == b"" and b"Session not found" in res.stderr


def test_session_continuation_survives_version_probe(make):
    h = make("session_continuation")
    first = h.run("run", "--", "go")
    assert first.returncode == -signal.SIGKILL
    assert (h.cwd / "effect.txt").read_text() == "written once\n"
    assert h.run("--version").returncode == 0
    (sid,) = [r["session_id"] for r in h.invocations() if r["attempt"] == 1]
    second = h.run("run", "--session", sid, "--", "go")
    assert second.returncode == 0 and sid.encode() in second.stdout
    runs = [r for r in h.invocations() if r["attempt"] is not None]
    assert [r["attempt"] for r in runs] == [1, 2]
    assert runs[1]["parsed"]["session"] == sid
    assert (h.state / "effects.log").read_text().splitlines() == ["effect.txt"]


def test_continuation_without_session_is_rejected(make):
    h = make("session_continuation")
    h.run("run", "--", "go")
    assert h.run("run", "--", "go").returncode == 97


@pytest.mark.parametrize("name", ["native_error", "doom_loop_rejected", "doom_loop_denied"])
def test_error_scenarios_exit_one_after_stdout(make, name):
    h = make(name)
    res = h.run("run", "--", "x")
    assert res.returncode == 1 and res.stdout


def test_agent_fallback_notice_on_stderr(make):
    h = make("agent_fallback")
    res = h.run("run", "--agent", "ghost", "--", "x")
    assert res.returncode == 0
    assert b"Falling back to default agent" in res.stderr
    assert b"step_finish" in res.stdout


def test_approval_scenarios(make):
    assert b"rejected permission" in make("approval_tool_error").run("run", "--", "x").stdout
    h = make("approval_stderr_notice")
    proc = subprocess.Popen(
        [str(h.shim), "run", "--", "x"], cwd=str(h.cwd), env=h.env(), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    h.procs.append(proc)
    h.wait_for(lambda: h.invocations())
    time.sleep(0.5)
    os.killpg(proc.pid, signal.SIGKILL)
    _out, err = proc.communicate(timeout=5)
    assert b"permission requested" in err


@pytest.mark.parametrize("name", [
    "task_errored_then_clean_finish", "task_denied_tail", "task_sync_completed",
    "task_background", "task_promoted", "approval_task_error", "denied_tool_then_clean_finish",
])
def test_fixture_backed_scenarios_replay(make, name):
    res = make(name).run("run", "--", "x")
    assert res.returncode == 0 and res.stdout.count(b"\n") >= 2


def test_all_named_scenarios_build():
    for name, builder in fake.SCENARIOS.items():
        args = ("plain-complete.jsonl",) if name == "replay" else ()
        scenario = builder(*args)
        assert scenario["attempts"], name
        json.dumps(scenario)


def test_stdout_raw_and_oversized_steps(tmp_path):
    scenario = {"attempts": [{"steps": [
        {"do": "stdout_raw", "text": "not json"},
        {"do": "stdout_oversized", "bytes": 5000},
        {"do": "stderr", "text": "warn"},
        {"do": "exit", "code": 3},
    ]}]}
    h = Harness(tmp_path, scenario)
    try:
        res = h.run("run", "--", "x")
    finally:
        h.cleanup()
    assert res.returncode == 3
    lines = res.stdout.split(b"\n")
    assert lines[0] == b"not json" and len(lines[1]) == 5000
    assert res.stderr == b"warn\n"
