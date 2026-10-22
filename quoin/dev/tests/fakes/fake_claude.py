"""A stand-in for the ``claude`` executable, driven by a scenario.

``make_fake_claude`` writes an executable named ``claude`` whose shebang is a
space-free symlink to the interpreter, so the process environment the cell
passes reaches the script unchanged (no wrapper shell adds variables). The
script appends one JSON line describing the launch to ``log`` (argument
vector, working directory, environment, process and group ids) and then plays
the scenario steps in order.

Steps: ``{"emit": obj_or_string}`` writes one stdout line, ``{"emit_file":
path}`` writes a file's lines, ``{"sleep": seconds}``, ``{"bg_devnull":
{"marker": path, "delay": s}}`` starts a background child with all stdio
redirected that writes the marker after the delay, ``{"bg_inherit":
{"seconds": s}}`` starts a background child that keeps the inherited pipes
open, ``{"log": text}`` appends a note to the log, ``{"exit": code}``.
"""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

# macOS adds both to any child started with an explicit environment.
PLATFORM_INJECTED_ENV = frozenset({"__CF_USER_TEXT_ENCODING", "LC_CTYPE"})

_SCRIPT = r'''
import json, os, subprocess, sys, time

scenario = json.load(open(SCENARIO))
with open(scenario["log"], "a") as fh:
    fh.write(json.dumps({"event": "start", "argv": sys.argv[1:], "cwd": os.getcwd(),
                         "env": dict(os.environ), "pid": os.getpid(),
                         "pgid": os.getpgid(0), "sid": os.getsid(0)}) + "\n")

def note(text):
    with open(scenario["log"], "a") as fh:
        fh.write(json.dumps({"event": "note", "text": text}) + "\n")

def emit(line):
    sys.stdout.write(line + "\n")
    sys.stdout.flush()

for step in scenario["steps"]:
    if "emit" in step:
        value = step["emit"]
        emit(value if isinstance(value, str) else json.dumps(value))
    elif "emit_file" in step:
        for line in open(step["emit_file"]).read().splitlines():
            emit(line)
    elif "sleep" in step:
        time.sleep(step["sleep"])
    elif "bg_devnull" in step:
        cfg = step["bg_devnull"]
        code = ("import time; time.sleep(%r); open(%r, 'w').write('late')"
                % (cfg["delay"], cfg["marker"]))
        devnull = open(os.devnull, "r+")
        subprocess.Popen([sys.executable, "-c", code], stdin=devnull, stdout=devnull,
                         stderr=devnull, close_fds=True)
    elif "bg_inherit" in step:
        code = "import time; time.sleep(%r)" % step["bg_inherit"]["seconds"]
        subprocess.Popen([sys.executable, "-c", code], close_fds=False)
    elif "log" in step:
        note(step["log"])
    elif "exit" in step:
        sys.stdout.flush()
        os._exit(step["exit"])
'''


def make_fake_claude(tmp_path, scenario: dict) -> Path:
    """Create ``<tmp_path>/fakebin/claude``; return the directory to put on PATH."""
    bin_dir = Path(tmp_path) / "fakebin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    interpreter = bin_dir / "py"
    if not interpreter.exists():
        interpreter.symlink_to(os.path.realpath(sys.executable))
    log = bin_dir / "launch.jsonl"
    scenario_path = bin_dir / "scenario.json"
    scenario_path.write_text(json.dumps({"log": str(log), **scenario}))
    shim = bin_dir / "claude"
    shim.write_text("#!%s\nSCENARIO = %r\n%s" % (interpreter, str(scenario_path), _SCRIPT))
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir


def read_log(bin_dir) -> list:
    path = Path(bin_dir) / "launch.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]
