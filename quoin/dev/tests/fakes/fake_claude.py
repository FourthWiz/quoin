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


# --- paired-run shim ---------------------------------------------------------
#
# ``make_paired_fake_claude`` writes a ``claude`` that behaves like the CLI
# closely enough for the paired runner: it reads its identity from the config
# directory it is given, announces skills and hooks the way the stream-json
# init event does, leaves CLI-like state behind, edits a file in its working
# directory and finishes with a result event. Each run appends a ``start`` line
# (written and synced first) and, on a normal exit, an ``end`` line to the log.
#
# Scenario keys: ``delay`` (seconds before the result), ``cost`` ({"plain",
# "quoin"} dollars), ``contaminated_plain`` (replay the recorded contaminated
# init on the plain arm), ``background`` ("devnull" or "inherit": leave a child
# running), ``no_result``, ``result_subtype``, ``write_skill`` (a skill directory to create in the
# config dir), ``commit_in`` (a repository that gets an empty commit), ``commit``, and ``runs``: a map from the 1-based run number to a
# dict that replaces the keys above for that run only. ``sleep_forever`` makes
# the run wait until it is signalled.

_PAIRED_SCRIPT = r'''
import json, os, subprocess, sys, time

T0 = time.monotonic()
scenario = json.load(open(SCENARIO))
LOG = scenario["log"]


def log(obj):
    with open(LOG, "a") as fh:
        fh.write(json.dumps(obj) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


log({"event": "start", "pid": os.getpid(), "pgid": os.getpgid(0), "argv": sys.argv[1:],
     "env_keys": sorted(os.environ), "home": os.environ.get("HOME"),
     "config_dir": os.environ.get("CLAUDE_CONFIG_DIR"), "cli_tmp": os.environ.get("CLAUDE_CODE_TMPDIR"),
     "cli_tmp_exists": os.path.isdir(os.environ.get("CLAUDE_CODE_TMPDIR") or ""), "cwd": os.getcwd(), "t0": T0})
run_no = sum(1 for line in open(LOG) if '"event": "start"' in line)
opts = dict(scenario)
opts.update(scenario.get("runs", {}).get(str(run_no), {}))

config = os.environ["CLAUDE_CONFIG_DIR"]
skills_dir = os.path.join(config, "skills")
skills = sorted(os.listdir(skills_dir)) if os.path.isdir(skills_dir) else []
arm = "quoin" if skills else "plain"


def emit(obj):
    sys.stdout.write((obj if isinstance(obj, str) else json.dumps(obj)) + "\n")
    sys.stdout.flush()


if opts.get("sleep_forever"):
    time.sleep(600)

if opts.get("contaminated_plain") and arm == "plain":
    for line in open(scenario["contaminated_fixture"]).read().splitlines():
        emit(line)
else:
    try:
        settings = json.load(open(os.path.join(config, "settings.json")))
    except (OSError, ValueError):
        settings = {}
    for event_name in sorted((settings.get("hooks") or {})):
        emit({"type": "system", "subtype": "hook_started", "hook_event": event_name})
    emit({"type": "system", "subtype": "init", "apiKeySource": "none",
          "skills": skills + ["run", "debug", "code-review"], "plugins": [], "mcp_servers": [],
          "memory_paths": {"auto": os.path.join(os.path.realpath(config), "projects", "p", "memory") + "/"},
          "claude_code_version": "0.0.0"})

# State a real CLI session leaves in its config directory.
os.makedirs(os.path.join(config, "plugins"), exist_ok=True)
json.dump([], open(os.path.join(config, "plugins", "blocklist.json"), "w"))
json.dump({"version": 2, "plugins": {}}, open(os.path.join(config, "plugins", "installed_plugins.json"), "w"))
json.dump({"numStartups": 1}, open(os.path.join(config, ".claude.json"), "w"))
os.makedirs(os.path.join(config, "projects", "p"), exist_ok=True)
open(os.path.join(config, "projects", "p", "session.jsonl"), "w").write("{}\n")

if opts.get("write_skill"):
    target = os.path.join(config, "skills", opts["write_skill"])
    os.makedirs(target, exist_ok=True)
    open(os.path.join(target, "SKILL.md"), "w").write("# injected\n")

with open("a.txt", "a") as fh:
    fh.write("edited by %s run %d\n" % (arm, run_no))
if opts.get("commit_in"):
    subprocess.run(["git", "-C", opts["commit_in"], "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                    "commit", "-q", "--allow-empty", "-m", "moved"], check=False)
if opts.get("commit"):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                    "commit", "-q", "-am", "agent"], check=False)

background = opts.get("background")
if background == "devnull":
    devnull = open(os.devnull, "r+")
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], stdin=devnull,
                             stdout=devnull, stderr=devnull, close_fds=True)
    log({"event": "child", "pid": child.pid})
elif background == "inherit":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], close_fds=False)
    log({"event": "child", "pid": child.pid})

time.sleep(opts.get("delay", 0.05))
emit({"type": "assistant", "message": {"model": "claude-opus-4-7", "content": [{"type": "text", "text": "done"}]}})
if not opts.get("no_result"):
    cost = (opts.get("cost") or {}).get(arm, 0.25)
    subtype = opts.get("result_subtype", "success")
    emit({"type": "result", "subtype": subtype, "is_error": subtype != "success",
          "terminal_reason": "completed" if subtype == "success" else "budget_exhausted",
          "total_cost_usd": cost, "permission_denials": [],
          "modelUsage": {"claude-opus-4-7": {"inputTokens": 10, "outputTokens": 5,
                                              "cacheReadInputTokens": 0,
                                              "cacheCreationInputTokens": 0, "costUSD": cost}}})
log({"event": "end", "pid": os.getpid(), "t1": time.monotonic()})
'''


def make_paired_fake_claude(tmp_path, scenario: dict | None = None):
    """Write the paired shim; return ``(bin_dir, log_path)``."""
    bin_dir = Path(tmp_path) / "pairedbin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    interpreter = bin_dir / "py"
    if not interpreter.exists():
        interpreter.symlink_to(os.path.realpath(sys.executable))
    log = bin_dir / "launch.jsonl"
    fixture = Path(__file__).resolve().parent.parent / "fixtures" / "benchmark_streams" / "init_contaminated_plain.jsonl"
    scenario_path = bin_dir / "scenario.json"
    scenario_path.write_text(json.dumps({"log": str(log), "contaminated_fixture": str(fixture),
                                         **(scenario or {})}))
    shim = bin_dir / "claude"
    shim.write_text("#!%s\nSCENARIO = %r\n%s" % (interpreter, str(scenario_path), _PAIRED_SCRIPT))
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir, log


def read_paired_log(log_path) -> list:
    path = Path(log_path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]
