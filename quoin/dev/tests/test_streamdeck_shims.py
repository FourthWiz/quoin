"""Rendered hook shim, terminal entry, legacy forwarders and tombstones, executed for real."""
import ast
import os
import stat
import subprocess
import sys
import threading
import time

import pytest

import _streamdeck_lifecycle as lc
from _streamdeck_ops import (deck_home, forbid_dangerous_subprocess, forbid_real_home_io, forbid_real_seams,  # noqa: F401
                             lifecycle_home)
from quoin.streamdeck import manifest, paths as paths_mod, shims

pytestmark = pytest.mark.usefixtures("forbid_dangerous_subprocess", "forbid_real_home_io", "forbid_real_seams")

EVENT = b'{"session_id": "s", "hook_event_name": "Stop"}'


@pytest.fixture
def rt(tmp_path):
    """A runtime path with a space and a single quote, a `current` dir and a stand-in python."""
    runtime = tmp_path / "it's a runtime"
    (runtime / "current").mkdir(parents=True)
    python = tmp_path / "python3"
    python.symlink_to(sys.executable)
    marker = tmp_path / "marker.txt"
    return type("RT", (), dict(runtime=str(runtime), current=runtime / "current", python=str(python), marker=marker,
                               tmp=tmp_path))()


def write_exec(path, text):
    path.write_text(text)
    path.chmod(0o755)
    return str(path)


def standin_monitor(rt, body=None):
    code = body or ("import os, sys\nopen(%r, 'w').write(os.environ.get('QUOIN_DECK_RUNTIME', '') + '|' + "
                    "os.environ.get('QUOIN_DECK_VIA', '') + '|' + ' '.join(sys.argv[1:]) + '|' + sys.stdin.read())\n"
                    % str(rt.marker))
    (rt.current / "monitor.py").write_text(code)


def run(argv, stdin=b"", timeout=15, env=None):
    base = {"HOME": "/nonexistent", "PATH": "/usr/bin:/bin"}
    base.update(env or {})
    return subprocess.run(argv, input=stdin, capture_output=True, timeout=timeout, env=base)


def install(rt, name, text):
    path = rt.tmp / name
    return write_exec(path, text)


# ------------------------------------------------------------------ hook shim

def test_shim_runs_the_monitor_with_the_runtime_exported(rt):
    standin_monitor(rt)
    shim = install(rt, "shim", shims.render_hook_shim(rt.runtime, rt.python))
    proc = run([shim, "claude"], EVENT)
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, b"", b"")
    assert rt.marker.read_text() == "%s||claude|%s" % (rt.runtime, EVENT.decode())


def test_shim_with_a_missing_target_drains_stdin_and_exits_zero(rt):
    shim = install(rt, "shim", shims.render_hook_shim(rt.runtime, rt.python))
    big = b"x" * (1 << 20)
    proc = run([shim, "claude"], big)
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, b"", b"")


def test_shim_without_python_or_with_a_missing_python_exits_zero(rt):
    standin_monitor(rt)
    for python in (None, "/nonexistent/python3"):
        shim = install(rt, "shim", shims.render_hook_shim(rt.runtime, python))
        proc = run([shim, "claude"], b"y" * (1 << 20))
        assert (proc.returncode, proc.stdout, proc.stderr) == (0, b"", b"")
    assert not rt.marker.exists()


def test_shim_never_execs_and_is_marked_generated():
    text = shims.render_hook_shim("/rt", "/py/python3")
    assert text.count("exec") == 0
    assert text.splitlines()[1] == shims.HEADER
    assert text.rstrip().endswith("exit 0")


def test_shim_exits_zero_even_when_the_monitor_fails(rt):
    standin_monitor(rt, "import sys\nsys.stdout.write('noise')\nsys.exit(2)\n")
    shim = install(rt, "shim", shims.render_hook_shim(rt.runtime, rt.python))
    proc = run([shim, "claude"], EVENT)
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, b"", b"")


# ------------------------------------------------------------------ terminal entry

def test_terminal_entry_missing_target_prints_the_message(rt):
    entry = install(rt, "term", shims.render_terminal_entry(rt.runtime))
    proc = run([entry, "attach", "s", "/p"])
    assert proc.returncode == 1 and proc.stdout == b"" and shims.MISSING_MESSAGE.encode() in proc.stderr


def test_terminal_entry_hands_over_to_the_current_script(rt):
    (rt.current / "terminal.zsh").write_text('print -r -- "ran: $*"\n')
    entry = install(rt, "term", shims.render_terminal_entry(rt.runtime))
    proc = run([entry, "attach", "s p", "/p"])
    assert proc.returncode == 0 and proc.stdout == b"ran: attach s p /p\n"


# ------------------------------------------------------------------ forwarders

def test_monitor_forwarder_marks_the_legacy_route(rt):
    standin_monitor(rt)
    fwd = install(rt, "monitor.py", shims.render_forwarder("monitor.py", rt.runtime, rt.python))
    proc = run([rt.python, fwd, "claude"], EVENT)
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, b"", b"")
    assert rt.marker.read_text().split("|")[:3] == [rt.runtime, "legacy", "claude"]


def test_monitor_forwarder_missing_target_exits_zero(rt):
    fwd = install(rt, "monitor.py", shims.render_forwarder("monitor.py", rt.runtime, rt.python))
    proc = run([rt.python, fwd, "claude"], b"z" * (1 << 20))
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, b"", b"")


def test_monitor_forwarder_kills_a_slow_child(rt):
    standin_monitor(rt, "import time\nopen(%r, 'w').write('started')\ntime.sleep(5)\n" % str(rt.marker))
    fwd = install(rt, "monitor.py", shims.render_forwarder("monitor.py", rt.runtime, rt.python))
    start = time.monotonic()
    proc = run([rt.python, fwd, "claude"], EVENT)
    assert time.monotonic() - start < 3.5
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, b"", b"")


def test_monitor_forwarder_uses_sys_executable_when_the_embedded_python_is_gone(rt):
    standin_monitor(rt)
    fwd = install(rt, "monitor.py", shims.render_forwarder("monitor.py", rt.runtime, "/nonexistent/python3"))
    run([rt.python, fwd, "claude"], EVENT)
    assert rt.marker.exists()


def test_dashboard_forwarder_relays_snapshot_output(rt):
    (rt.current / "dashboard.py").write_text("import sys\nprint('[{\"a\": 1}]' if sys.argv[1] == 'snapshot' else 'x')\n")
    fwd = install(rt, "dashboard.py", shims.render_forwarder("dashboard.py", rt.runtime, rt.python))
    proc = run([rt.python, fwd, "snapshot"])
    assert proc.returncode == 0 and proc.stdout.decode().strip() == '[{"a": 1}]'


def test_dashboard_forwarder_failures(rt):
    (rt.current / "dashboard.py").write_text("import sys\nsys.stderr.write('boom\\nmore\\n')\nsys.exit(3)\n")
    fwd = install(rt, "dashboard.py", shims.render_forwarder("dashboard.py", rt.runtime, rt.python))
    proc = run([rt.python, fwd, "snapshot"])
    assert (proc.returncode, proc.stdout.decode().strip(), proc.stderr) == (0, "[]", b"")
    proc = run([rt.python, fwd, "focus", "{}"])
    assert proc.returncode == 1 and proc.stdout == b"" and len(proc.stderr.decode().strip().splitlines()) == 1
    (rt.current / "dashboard.py").unlink()
    assert run([rt.python, fwd, "snapshot"]).stdout.decode().strip() == "[]"
    assert run([rt.python, fwd, "focus", "{}"]).returncode == 1


def test_controller_forwarder_relays_arguments_and_exit_code(rt):
    (rt.current / "controller.py").write_text("import sys\nprint('args:' + ','.join(sys.argv[1:]))\nsys.exit(4)\n")
    fwd = install(rt, "controller.py", shims.render_forwarder("controller.py", rt.runtime, rt.python))
    proc = run([rt.python, fwd, "prev-pane", "x y"])
    assert proc.returncode == 4 and proc.stdout.decode().strip() == "args:prev-pane,x y"
    (rt.current / "controller.py").unlink()
    proc = run([rt.python, fwd, "prev-pane"])
    assert proc.returncode == 1 and shims.MISSING_MESSAGE.encode() in proc.stderr


def test_terminal_forwarder(rt):
    fwd = install(rt, "terminal.zsh", shims.render_forwarder("terminal.zsh", rt.runtime, rt.python))
    proc = run(["/bin/zsh", fwd, "attach"])
    assert proc.returncode == 1 and shims.MISSING_MESSAGE.encode() in proc.stderr
    (rt.current / "terminal.zsh").write_text('print -r -- "ran: $*"\n')
    proc = run(["/bin/zsh", fwd, "attach", "s"])
    assert proc.returncode == 0 and proc.stdout == b"ran: attach s\n"


def test_unknown_forwarder_name_is_refused(rt):
    with pytest.raises(ValueError):
        shims.render_forwarder("deckenv.py", rt.runtime, rt.python)


# ------------------------------------------------------------------ tombstones

@pytest.mark.parametrize("kind", shims.TOMBSTONE_KINDS)
def test_tombstones_are_inert_and_recognised(rt, kind):
    text = shims.render_tombstone(kind)
    assert manifest.TOMBSTONE_MARKER in text[:512]
    assert manifest.TOMBSTONE_MARKER in "\n".join(text.splitlines()[:2])
    path = install(rt, "stub", text)
    assert manifest.is_tombstone(path)
    argv = [path] if kind == "sh" else [rt.python, path]
    proc = run(argv, b"w" * (1 << 20))
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, b"", b"")


def test_bad_tombstone_kind_is_refused():
    with pytest.raises(ValueError):
        shims.render_tombstone("zsh")


# ------------------------------------------------------------------ static shape

def test_python_renders_parse_as_python_39(rt):
    for name in ("monitor.py", "dashboard.py", "controller.py"):
        ast.parse(shims.render_forwarder(name, rt.runtime, rt.python), feature_version=(3, 9))
        ast.parse(shims.render_forwarder(name, rt.runtime, None), feature_version=(3, 9))
    ast.parse(shims.render_tombstone("python"), feature_version=(3, 9))


def test_renders_carry_no_machine_specific_literals(rt):
    texts = [shims.render_hook_shim("/rt", "/py/python3"), shims.render_terminal_entry("/rt"),
             shims.render_tombstone("sh"), shims.render_tombstone("python")]
    texts += [shims.render_forwarder(n, "/rt", "/py/python3") for n in shims.FORWARDER_NAMES]
    for text in texts:
        for banned in ("/opt/homebrew", "/Users/", "versions/"):
            assert banned not in text


def test_entry_points(lifecycle_home):
    paths = lifecycle_home.resolve()
    points = shims.entry_points(paths)
    assert points == {"hook_shim": paths.hook_shim, "terminal_entry": paths.terminal_entry,
                      "root_monitor": os.path.join(paths.runtime, "monitor.py")}


# ------------------------------------------------------------------ the monitor forwarder when the child cannot start

def feed_the_forwarder(rt, python, total=2 * 1024 * 1024, chunk=64 * 1024):
    """Run the forwarder, writing `total` bytes to its stdin in chunks; report what it accepted and any broken pipe."""
    script = rt.tmp / "monitor-forwarder.py"
    write_exec(script, shims.render_forwarder("monitor.py", rt.runtime, python))
    proc = subprocess.Popen([rt.python, str(script), "claude"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env={"HOME": "/nonexistent", "PATH": "/usr/bin:/bin"})
    seen = {"accepted": 0, "broken": False}

    def writer():
        try:
            for _ in range(total // chunk):
                proc.stdin.write(b"x" * chunk)
                proc.stdin.flush()
                seen["accepted"] += chunk
        except (BrokenPipeError, OSError):
            seen["broken"] = True
        finally:
            try:
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                seen["broken"] = True

    thread = threading.Thread(target=writer)
    thread.start()
    out, err = proc.stdout.read(), proc.stderr.read()
    code = proc.wait(timeout=30)
    thread.join(30)
    return code, out, err, seen, total


@pytest.mark.parametrize("flavour", ["garbage bytes", "missing interpreter"])
def test_the_monitor_forwarder_drains_its_input_when_the_child_cannot_start(rt, flavour):
    sentinel = rt.tmp / "monitor-ran"
    (rt.current / "monitor.py").write_text("open(%r, 'w').write('ran')\n" % str(sentinel))
    broken = rt.tmp / "broken-python"
    if flavour == "garbage bytes":
        broken.write_bytes(b"\x00\x01\x02 not a program")
    else:
        broken.write_text("#!/nonexistent/interpreter\n")
    broken.chmod(0o755)
    code, out, _err, seen, total = feed_the_forwarder(rt, str(broken))
    assert code == 0 and out == b""
    assert not seen["broken"] and seen["accepted"] == total
    assert not sentinel.exists()
