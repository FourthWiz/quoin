"""Behaviour of the stdlib fake OpenCode executable, driven without any Quoin driver code."""
from __future__ import annotations

import importlib.util
import json
import os
import select
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
        self._starts = {}

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

    def _read_pids(self, name):
        path = self.state / name
        if not path.exists():
            return []
        return [int(x) for x in path.read_text().split()]

    def grandchildren(self):
        """Grandchild pids; each is remembered with its start time the first time it is seen alive."""
        pids = self._read_pids("grandchildren.txt")
        for pid in pids:
            if pid not in self._starts:
                start = _start_time(pid)
                if start is not None:
                    self._starts[pid] = start
        return pids

    def intermediates(self):
        return self._read_pids("intermediates.txt")

    def touch_stop(self):
        try:
            self.state.mkdir(parents=True, exist_ok=True)
            (self.state / "stop").write_text("")
        except OSError:
            pass

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
        # The stop marker ends every helper process on its own; a pid is
        # signalled only when it is still alive with the start time recorded
        # when it was first seen, so a recycled pid is never touched.
        self.touch_stop()
        for pid in self._read_pids("grandchildren.txt") + self._read_pids("intermediates.txt"):
            start = self._starts.get(pid)
            if start is None or not _alive(pid):
                continue
            deadline = time.time() + 2
            while time.time() < deadline and _alive(pid):
                time.sleep(0.05)
            if _alive(pid) and _start_time(pid) == start:
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


def _start_time(pid):
    env = dict(os.environ, LC_ALL="C", TZ="UTC")
    res = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, env=env)
    return res.stdout.strip() or None


def _ppid(pid):
    res = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True)
    return int(res.stdout.strip()) if res.stdout.strip() else None


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
    seen = b""
    deadline = time.time() + 10
    while b"permission requested" not in seen and time.time() < deadline:
        ready, _w, _x = select.select([proc.stderr], [], [], 0.2)
        if ready:
            chunk = os.read(proc.stderr.fileno(), 4096)
            if not chunk:
                break
            seen += chunk
    os.killpg(proc.pid, signal.SIGKILL)
    proc.communicate(timeout=5)
    assert b"permission requested" in seen


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


# ---------------------------------------------------------------------------
# hardening and stage-2 scenarios
# ---------------------------------------------------------------------------

def _lines(data):
    return [json.loads(l) for l in data.decode().splitlines() if l.strip().startswith("{")]


def _popen(h, *args):
    proc = subprocess.Popen(
        [str(h.shim), *args], cwd=str(h.cwd), env=h.env(), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    h.procs.append(proc)
    return proc


def _readable(stream, seconds):
    ready, _w, _x = select.select([stream], [], [], seconds)
    return bool(ready)


@pytest.mark.parametrize("bad", ["../evil", "a/b", "x" * 129, "bad id", "ses.dot", "ses\n"])
def test_session_ids_outside_the_safe_shape_are_unknown_sessions(make, bad):
    h = make("record_only")
    res = h.run("run", "--session", bad, "--", "x")
    assert res.returncode == 1 and res.stdout == b"" and b"Session not found" in res.stderr
    assert list((h.state / "sessions").iterdir()) == []
    assert not (h.state / "evil.json").exists()


def test_crash_steps_disable_core_dumps_first():
    code = (
        "import importlib.util, resource, sys\n"
        "spec = importlib.util.spec_from_file_location('f', sys.argv[1])\n"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        "m._no_core_dumps()\n"
        "print(resource.getrlimit(resource.RLIMIT_CORE))\n"
    )
    res = subprocess.run([sys.executable, "-c", code, str(FAKE_PATH)], capture_output=True, text=True,
                         timeout=TIMEOUT, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    assert res.stdout.strip() == "(0, 0)", res.stderr


def test_grandchild_writes_its_own_pid_before_the_ready_marker(make):
    h = make("grandchild")
    h.start("run", "--", "x")
    ready = h.wait_for(lambda: sorted(h.state.glob("grandchild-*.ready")))
    pid = int(ready[0].name[len("grandchild-"):-len(".ready")])
    assert pid in h.grandchildren()
    assert h.state.joinpath("grandchildren.txt").read_text().count("\n") == 1


def test_grandchild_exits_after_its_lifetime_cap(tmp_path):
    scenario = {"max_lifetime_s": 1, "attempts": [{"steps": [
        fake._step_start("prt_s1"), {"do": "spawn_grandchild"}, {"do": "exit", "code": 0}]}]}
    h = Harness(tmp_path, scenario)
    try:
        assert h.run("run", "--", "x").returncode == 0
        (gpid,) = h.wait_for(h.grandchildren)
        h.wait_for(lambda: not _alive(gpid), seconds=10)
    finally:
        h.cleanup()


def test_touch_stop_ends_helper_processes(tmp_path):
    scenario = {"attempts": [{"steps": [
        {"do": "spawn_grandchild"}, {"do": "spawn_grandchild", "detach": True},
        {"do": "sleep", "seconds": 0.5}, {"do": "touch_stop"}, {"do": "exit", "code": 0}]}]}
    h = Harness(tmp_path, scenario)
    try:
        assert h.run("run", "--", "x").returncode == 0
        assert (h.state / "stop").exists()
        pids = h.wait_for(lambda: len(h.grandchildren()) == 2 and h.grandchildren())
        for pid in pids:
            h.wait_for(lambda pid=pid: not _alive(pid), seconds=10)
    finally:
        h.cleanup()


def test_stdout_no_newline_leaves_the_line_open(tmp_path):
    steps = [{"do": "stdout_no_newline", "text": "partial"}]
    outputs = []
    for index, extra in enumerate(([], [{"do": "stdout_raw", "text": "-tail"}])):
        sub = tmp_path / ("s%d" % index)
        sub.mkdir()
        h = Harness(sub, {"attempts": [{"steps": steps + extra + [{"do": "exit", "code": 0}]}]})
        try:
            outputs.append(h.run("run", "--", "x").stdout)
        finally:
            h.cleanup()
    assert outputs == [b"partial", b"partial-tail\n"]


def test_close_stdout_ends_the_pipe_and_later_output_is_discarded(tmp_path):
    scenario = {"attempts": [{"steps": [
        fake._step_start("prt_s1"), {"do": "close_stdout"}, fake._step_finish("prt_f1", "stop"),
        {"do": "stderr", "text": "after close"}, {"do": "exit", "code": 0}]}]}
    h = Harness(tmp_path, scenario)
    try:
        res = h.run("run", "--", "x")
    finally:
        h.cleanup()
    assert res.returncode == 0 and res.stderr == b"after close\n"
    assert [e["type"] for e in _lines(res.stdout)] == ["step_start"]


def test_stdout_closed_hang_reaches_eof_while_the_process_lives(make):
    h = make("stdout_closed_hang")
    proc = _popen(h, "run", "--", "x")
    assert _readable(proc.stdout, 10)
    data = proc.stdout.read()  # returns at EOF, i.e. once the pipe is closed
    assert b"step_start" in data and proc.poll() is None
    os.killpg(proc.pid, signal.SIGKILL)
    proc.wait(timeout=5)


def test_reparented_grandchild_outlives_its_intermediate(make):
    h = make("grandchild_reparented")
    proc = h.start("run", "--", "x")
    (gpid,) = h.wait_for(h.grandchildren)
    (ipid,) = h.wait_for(h.intermediates)
    record = h.wait_for(h.invocations)[0]
    h.wait_for(lambda: not _alive(ipid), seconds=10)
    assert proc.poll() is None and _alive(gpid)
    assert _ppid(gpid) not in (ipid, record["pid"])
    assert os.getpgid(gpid) != record["pgid"] and os.getsid(gpid) == gpid


def test_emit_session_error_writes_an_id_less_error_envelope(tmp_path):
    scenario = {"attempts": [{"steps": [
        {"do": "emit_session_error", "status": 429, "message": "Rate limit"},
        {"do": "emit_session_error", "message": "boom"}, {"do": "exit", "code": 1}]}]}
    h = Harness(tmp_path, scenario)
    try:
        res = h.run("run", "--", "x")
    finally:
        h.cleanup()
    api, unknown = _lines(res.stdout)
    assert api["type"] == "error" and api["sessionID"].startswith("ses_")
    assert api["error"] == {"name": "APIError", "data": {
        "message": "Rate limit", "statusCode": 429, "isRetryable": True}}
    assert unknown["error"] == {"name": "UnknownError", "data": {"message": "boom"}}
    assert "part" not in api and isinstance(api["timestamp"], int)


def test_session_continuation_replay_reemits_attempt_one_lines(make):
    h = make("session_continuation_replay")
    first = h.run("run", "--", "go")
    assert first.returncode == -signal.SIGKILL
    (sid,) = [r["session_id"] for r in h.invocations() if r["attempt"] == 1]
    second = h.run("run", "--session", sid, "--", "go")
    assert second.returncode == 0
    one, two = first.stdout.decode().splitlines(), second.stdout.decode().splitlines()
    assert len(one) == 3 and two[:3] == one
    assert [json.loads(l)["type"] for l in two[3:]] == ["step_start", "text", "step_finish"]
    assert h.run("run", "--", "go").returncode == 97


def test_transient_error_then_continue_replays_the_id_less_error(make):
    h = make("transient_error_then_continue")
    first = h.run("run", "--", "go")
    assert first.returncode == 1
    one = first.stdout.decode().splitlines()
    assert json.loads(one[-1])["error"]["data"]["statusCode"] == 429
    assert json.loads(one[-2])["part"]["reason"] == "tool-calls"
    (sid,) = [r["session_id"] for r in h.invocations() if r["attempt"] == 1]
    second = h.run("run", "--session", sid, "--", "go")
    assert second.returncode == 0
    two = second.stdout.decode().splitlines()
    assert two[0] == one[-1]
    assert json.loads(two[-1])["part"]["reason"] == "stop"


def test_endless_line_has_no_newline_for_two_mebibytes(make):
    h = make("endless_line")
    res = h.run("run", "--", "x")
    assert res.returncode == 0
    assert res.stdout.index(b"\n") > 2 * 1024 * 1024
    assert res.stdout.startswith(b"x" * 1024) and b"step_finish" in res.stdout


def test_approval_notice_then_finish_writes_stderr_after_stdout_closes(make):
    h = make("approval_notice_then_finish")
    proc = _popen(h, "run", "--", "x")
    data = proc.stdout.read()  # EOF once stdout is closed
    assert b"step_finish" in data
    assert not _readable(proc.stderr, 0)  # the notice comes later
    err = proc.stderr.read()
    proc.wait(timeout=10)
    assert proc.returncode == 0 and b"permission requested" in err


def test_secret_echo_puts_the_literal_in_every_channel(tmp_path):
    secret = "sk-CUSTOMSECRET9876543210"
    h = Harness(tmp_path, fake.SCENARIOS["secret_echo"](secret))
    try:
        res = h.run("run", "--", "x")
    finally:
        h.cleanup()
    assert res.returncode == 1 and secret.encode() in res.stderr
    events = _lines(res.stdout)
    assert secret in events[1]["part"]["text"]
    assert secret in events[2]["part"]["state"]["title"]
    assert secret in events[3]["error"]["data"]["message"]


def test_secret_echo_straddle_starts_four_bytes_before_the_boundary(make):
    h = make("secret_echo_straddle")
    res = h.run("run", "--", "x")
    secret = b"sk-FAKESTRADDLE0123456789"
    assert res.returncode == 0 and res.stderr.index(secret) == 8192 - 4


def test_grandchild_holds_stdout_after_the_fake_exits(make):
    h = make("grandchild_holds_stdout")
    proc = _popen(h, "run", "--", "x")
    assert proc.wait(timeout=10) == 0
    (gpid,) = h.wait_for(h.grandchildren)
    assert _alive(gpid)
    os.set_blocking(proc.stdout.fileno(), False)
    assert b"step_finish" in (proc.stdout.read() or b"")
    assert _alive(gpid)  # the write end is still held: no EOF yet
    h.touch_stop()
    h.wait_for(lambda: not _alive(gpid), seconds=10)


def test_slow_finish_takes_real_time(tmp_path):
    h = Harness(tmp_path, fake.SCENARIOS["slow_finish"](0.25))
    try:
        started = time.time()
        res = h.run("run", "--", "x")
        elapsed = time.time() - started
    finally:
        h.cleanup()
    assert res.returncode == 0 and elapsed >= 0.5 and b"step_finish" in res.stdout


@pytest.mark.parametrize("name,marker", [
    ("task_background_then_boundary_crash", b'"background": true'),
    ("task_denied_tail_then_boundary_crash", b"The user has specified a rule"),
])
def test_task_then_boundary_crash_resumes_cleanly(make, name, marker):
    h = make(name)
    first = h.run("run", "--", "go")
    assert first.returncode == -signal.SIGKILL
    assert marker in first.stdout and b"step_finish" in first.stdout
    (sid,) = [r["session_id"] for r in h.invocations() if r["attempt"] == 1]
    second = h.run("run", "--session", sid, "--", "go")
    assert second.returncode == 0 and b'"reason": "stop"' in second.stdout


def test_grandchild_detached_finish_leaves_a_live_detached_process(make):
    h = make("grandchild_detached_finish")
    assert h.run("run", "--", "x").returncode == 0
    (gpid,) = h.wait_for(h.grandchildren)
    record = h.wait_for(h.invocations)[0]
    assert _alive(gpid) and os.getpgid(gpid) != record["pgid"]


def test_step_closed_then_hang_leaves_an_effect_and_a_live_process(make):
    h = make("step_closed_then_hang")
    proc = _popen(h, "run", "--", "x")
    h.wait_for(lambda: (h.state / "effects.log").exists())
    assert (h.cwd / "effect.txt").read_text() == "written once\n"
    assert _readable(proc.stdout, 10)
    lines = b""
    while lines.count(b"\n") < 2:
        lines += os.read(proc.stdout.fileno(), 4096)
    assert [e["type"] for e in _lines(lines)] == ["step_start", "step_finish"]
    assert proc.poll() is None


def test_effect_before_first_line_prints_nothing(make):
    h = make("effect_before_first_line")
    proc = _popen(h, "run", "--", "x")
    h.wait_for(lambda: (h.state / "effects.log").exists())
    assert not _readable(proc.stdout, 0.5) and proc.poll() is None


def test_five_repeated_scenario_runs_leave_no_stray_processes(make, tmp_path_factory):
    for _ in range(5):
        for name in ("grandchild_detached_finish", "grandchild_reparented", "grandchild_holds_stdout"):
            h = make(name)
            proc = _popen(h, "run", "--", "x")
            h.wait_for(lambda: h.grandchildren())
            h.cleanup()
            if proc.poll() is not None:
                proc.communicate(timeout=10)
    time.sleep(0.5)
    base = str(tmp_path_factory.getbasetemp())
    listing = subprocess.run(["ps", "-A", "-o", "pid=,stat=,command="], capture_output=True, text=True).stdout
    stray = [l for l in listing.splitlines()
             if base in l and not l.split(None, 2)[1].startswith("Z")]
    assert not stray, stray
