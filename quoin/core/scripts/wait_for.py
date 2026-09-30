#!/usr/bin/env python3
"""wait_for.py - start a command detached, then wait for it in bounded foreground calls.

A non-interactive session that ends its turn while a long command is still
running loses that command's result. This helper lets a session start the
command detached and then poll for its exit code with calls that each stay
under the tool timeout, until a terminal outcome is printed.

Subcommands
  start --rc-file F --token TOK --log L [--cwd DIR] -- CMD...
      Run CMD (an argv list, never a shell) in a detached session. Output goes
      to L. When CMD ends, `TOK <rc>` is written atomically to F. Prints
      STARTED|<runner pid> (exit 0) or ERROR|<reason> (exit 2). Any older rc
      file and start file for F are removed first.

  wait --file F [--token TOK] [--max-secs N] [--poll-secs P] [--budget-secs B]
      Poll F until it holds `TOK <rc>`. Prints one line:
        READY|<rc>        exit 0  the command ended with that code
        WAITING|<secs>    exit 1  per-call deadline reached; call wait again
        DEAD|<secs>       exit 3  the runner vanished without leaving an rc
        EXPIRED|<secs>    exit 4  total time since start passed the budget;
                                  the runner's process group is sent SIGTERM
      DEAD and EXPIRED are terminal: stop waiting and treat the run as failed.
      Defaults: --max-secs 540 (1..570), --poll-secs 5 (1..60), --budget-secs
      from QUOIN_WAIT_BUDGET_SECS else 3600 (60..14400).

The runner records an rc on every exit path it controls: 127 for a missing
executable, 126 for a permission error, 125 for an internal failure, 143 when
terminated by SIGTERM. Only SIGKILL leaves no rc, which wait reports as DEAD.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
from typing import Callable, List, Optional, Tuple

_TOKEN_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_RC_LINE = re.compile(r"^(\S+)\s+(-?\d+)$")


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


def _atomic_write(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _remove(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------- runner


def _run(rc_file: str, token: str, log: str, cwd: Optional[str], cmd: List[str]) -> int:
    state = {"written": False}

    def write_rc(rc: int) -> None:
        if state["written"]:
            return
        state["written"] = True
        _atomic_write(rc_file, "%s %d\n" % (token, rc))

    def on_term(signum, frame):  # noqa: ANN001
        try:
            write_rc(143)
        finally:
            os._exit(143)

    signal.signal(signal.SIGTERM, on_term)
    rc = 125
    try:
        with open(log, "ab") as out:
            try:
                child = subprocess.Popen(
                    cmd,
                    cwd=cwd,
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=subprocess.STDOUT,
                )
                rc = child.wait()
            except FileNotFoundError:
                rc = 127
            except PermissionError:
                rc = 126
            except Exception as exc:  # noqa: BLE001
                rc = 125
                out.write(("runner error: %s\n" % type(exc).__name__).encode("utf-8"))
    except Exception:  # noqa: BLE001
        rc = 125
    finally:
        write_rc(rc)
    return 0


# ---------------------------------------------------------------- start


def do_start(rc_file: str, token: str, log: str, cwd: Optional[str], cmd: List[str]) -> Tuple[str, int]:
    if not _TOKEN_RE.match(token or ""):
        return "ERROR|bad token", 2
    if not cmd:
        return "ERROR|empty command", 2
    if cwd is not None and not os.path.isdir(cwd):
        return "ERROR|cwd is not a directory", 2
    _remove(rc_file)
    _remove(rc_file + ".start")
    _remove(rc_file + ".tmp")
    argv = [sys.executable, os.path.abspath(__file__), "_run", "--rc-file", rc_file,
            "--token", token, "--log", log]
    if cwd is not None:
        argv += ["--cwd", cwd]
    argv += ["--"] + list(cmd)
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return "ERROR|spawn", 2
    _atomic_write(rc_file + ".start", "%s %d %d\n" % (token, proc.pid, int(time.time())))
    return "STARTED|%d" % proc.pid, 0


# ---------------------------------------------------------------- wait


def _read_rc(path: str, token: Optional[str]) -> Optional[int]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    for line in lines:
        if token is not None:
            m = _RC_LINE.match(line.strip())
            if m and m.group(1) == token:
                return int(m.group(2))
        else:
            parts = line.split()
            if parts:
                try:
                    return int(parts[-1])
                except ValueError:
                    continue
    return None


def _read_start(path: str, token: Optional[str]) -> Optional[Tuple[int, int]]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            parts = fh.read().split()
        if len(parts) != 3 or (token is not None and parts[0] != token):
            return None
        return int(parts[1]), int(parts[2])
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def do_wait(
    rc_file: str,
    token: Optional[str],
    max_secs: int,
    poll_secs: int,
    budget_secs: int,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    pid_alive: Callable[[int], bool] = _pid_alive,
) -> Tuple[str, int]:
    began = clock()
    while True:
        rc = _read_rc(rc_file, token)
        if rc is not None:
            return "READY|%d" % rc, 0
        elapsed = int(clock() - began)
        start = _read_start(rc_file + ".start", token) if token is not None else None
        if start is not None:
            pid, start_epoch = start
            if not pid_alive(pid):
                sleep(2)
                rc = _read_rc(rc_file, token)
                if rc is not None:
                    return "READY|%d" % rc, 0
                return "DEAD|%d" % elapsed, 3
            if clock() - start_epoch > budget_secs:
                try:
                    os.killpg(pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
                return "EXPIRED|%d" % elapsed, 4
        if elapsed >= max_secs:
            return "WAITING|%d" % elapsed, 1
        sleep(max(0.0, min(float(poll_secs), max_secs - (clock() - began))))


# ---------------------------------------------------------------- cli


def _opt(args: List[str], name: str) -> Optional[str]:
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            return args[i + 1]
    return None


def _split_cmd(args: List[str]) -> Tuple[List[str], List[str]]:
    if "--" in args:
        i = args.index("--")
        return args[:i], args[i + 1:]
    return args, []


def _int_opt(args: List[str], name: str, default: int, lo: int, hi: int) -> int:
    raw = _opt(args, name)
    try:
        value = int(raw) if raw is not None else default
    except ValueError:
        value = default
    return _clamp(value, lo, hi)


def _emit(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        sys.stdout.write(__doc__ or "")
        return 0
    sub, rest = args[0], args[1:]
    if sub in ("start", "_run"):
        opts, cmd = _split_cmd(rest)
        rc_file, token, log = _opt(opts, "--rc-file"), _opt(opts, "--token"), _opt(opts, "--log")
        cwd = _opt(opts, "--cwd")
        if sub == "start":
            if not rc_file or not log:
                _emit("ERROR|missing --rc-file or --log")
                return 2
            line, code = do_start(rc_file, token or "", log, cwd, cmd)
            _emit(line)
            return code
        if not (rc_file and token and log and cmd):
            return 2
        return _run(rc_file, token, log, cwd, cmd)
    if sub == "wait":
        target = _opt(rest, "--file")
        if not target:
            _emit("ERROR|missing --file")
            return 2
        try:
            env_budget = int(os.environ.get("QUOIN_WAIT_BUDGET_SECS", "3600"))
        except ValueError:
            env_budget = 3600
        line, code = do_wait(
            target,
            _opt(rest, "--token"),
            _int_opt(rest, "--max-secs", 540, 1, 570),
            _int_opt(rest, "--poll-secs", 5, 1, 60),
            _int_opt(rest, "--budget-secs", env_budget, 60, 14400),
        )
        _emit(line)
        return code
    _emit("ERROR|usage")
    return 2


if __name__ == "__main__":
    sys.exit(main())
