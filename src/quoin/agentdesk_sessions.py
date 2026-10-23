"""Agent Desk session discovery: list, attach, kill and prune from any shell.

Agent Desk sessions live in a private Zellij socket directory chosen by the
launcher (`agentdesk.zsh`). This module resolves that directory with the same
rule, asks `zellij list-sessions -n` for the sessions in it, and ties each one
to the project folder it was launched from, using a small per-session record
written by the launcher (or, for sessions launched before records existed, the
`<folder>-agents[_N]` naming convention).

Stdlib only; nothing here does I/O at import time.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Callable, Optional

NO_SESSIONS = "No active zellij sessions found"
ZELLIJ_TIMEOUT = 10
RECORD_MAX_BYTES = 4096
ORPHAN_MIN_AGE = 120
RECORD_TMP_PREFIX = ".agentdesk-record."

KEY_HINTS = """\
Keys inside an Agent Desk session (need Ghostty macos-option-as-alt):
  detach   Alt s, then d
  quit     Alt s, then q
Ctrl+Q is intentionally unbound. From another terminal, `quoin sessions`
lists sessions and `quoin attach` gets you back in.
"""

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_ROW = re.compile(r"^(?P<name>.*?) \[Created (?P<age>.*?) ago\](?P<rest>.*)$")
_AGENTS_NAME = re.compile(r".+-agents(_\d+)?")


class SessionsError(Exception):
    """An error to show the user; `code` is the process exit status."""

    def __init__(self, message: str, code: int = 2):
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass(frozen=True)
class Session:
    name: str
    age: str
    state: str  # "current" | "running" | "exited"

    @property
    def live(self) -> bool:
        return self.state != "exited"


@dataclass(frozen=True)
class Record:
    root: str
    socket_dir: str


@dataclass(frozen=True)
class Row:
    session: Session
    record: Optional[Record]
    match: Optional[str]  # "record" | "name" | None


@dataclass
class Listing:
    sock: str
    zellij: str
    env: dict
    raw_names: list
    rows: list


# ----------------------------------------------------------------- environment

def current_uid() -> int:
    return os.getuid()


def socket_dir(environ, uid=None) -> str:
    """The launcher's rule: ZELLIJ_SOCKET_DIR, else XDG_RUNTIME_DIR or /tmp plus a per-user name."""
    explicit = environ.get("ZELLIJ_SOCKET_DIR")
    if isinstance(explicit, str) and explicit:
        return explicit
    base = environ.get("XDG_RUNTIME_DIR")
    if not (isinstance(base, str) and base):
        base = "/tmp"
    return "%s/zellij-agentdesk-%d" % (base, current_uid() if uid is None else uid)


def zellij_env(environ, sock: str) -> dict:
    env = dict(environ)
    env["ZELLIJ_SOCKET_DIR"] = sock
    return env


def find_zellij(environ) -> Optional[str]:
    return shutil.which("zellij", path=environ.get("PATH"))


# --------------------------------------------------------------------- listing

def parse_list_output(text: str) -> list:
    sessions = []
    for line in _ANSI.sub("", text).splitlines():
        m = _ROW.match(line)
        if not m:
            continue
        rest = m.group("rest")
        if "(current)" in rest:
            state = "current"
        elif "EXITED" in rest:
            state = "exited"
        else:
            state = "running"
        sessions.append(Session(m.group("name"), m.group("age"), state))
    return sessions


def list_sessions(zellij, env, run=subprocess.run) -> list:
    argv = [zellij, "list-sessions", "-n"]
    try:
        proc = run(argv, capture_output=True, text=True, timeout=ZELLIJ_TIMEOUT,
                   env=env, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise SessionsError("zellij list-sessions timed out after %d s" % ZELLIJ_TIMEOUT)
    except OSError as exc:
        raise SessionsError("could not run zellij: %s" % exc)
    out = proc.stdout or ""
    err = proc.stderr or ""
    if NO_SESSIONS in out or NO_SESSIONS in err:
        return []
    if proc.returncode != 0:
        detail = err.strip() or out.strip() or "exit status %d" % proc.returncode
        raise SessionsError("zellij list-sessions failed: %s" % detail)
    return parse_list_output(out)


def delete_session(zellij, env, name, force=False, run=subprocess.run):
    """Run `zellij delete-session`; returns (ok, error_text)."""
    argv = [zellij, "delete-session"] + (["--force"] if force else []) + [name]
    try:
        proc = run(argv, capture_output=True, text=True, timeout=ZELLIJ_TIMEOUT,
                   env=env, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return False, "zellij delete-session timed out after %d s" % ZELLIJ_TIMEOUT
    except OSError as exc:
        return False, "could not run zellij: %s" % exc
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip() or (proc.stdout or "").strip()
        return False, detail or "exit status %d" % proc.returncode
    return True, ""


# ---------------------------------------------------------------------- naming

def sanitize_name(raw: str) -> str:
    """Mirror of the launcher's `_sanitize_session_name`."""
    chars = [c if (c.isalnum() or c in "_.-") else "-" for c in raw]
    name = re.sub(r"-{2,}", "-", "".join(chars))
    if name.startswith("-"):
        name = name[1:]
    if name.endswith("-"):
        name = name[:-1]
    return name


def project_base(root: str) -> str:
    return sanitize_name(os.path.basename(root)) or "agentdesk"


# ---------------------------------------------------------------------- records

def records_dir(environ) -> Optional[str]:
    home = environ.get("HOME")
    if not (isinstance(home, str) and home):
        return None
    return os.path.join(home, ".config", "agentdesk", "sessions")


def _valid_record_name(name: str) -> bool:
    return bool(name) and name not in (".", "..") and "/" not in name and "\0" not in name


def read_record(name, environ, sock) -> Optional[Record]:
    if not _valid_record_name(name):
        return None
    base = records_dir(environ)
    if base is None:
        return None
    path = os.path.join(base, name)
    try:
        # lstat first: opening a FIFO would block until a writer shows up.
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return None
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_size > RECORD_MAX_BYTES:
                return None
            data = os.read(fd, RECORD_MAX_BYTES + 1)
        finally:
            os.close(fd)
        if len(data) > RECORD_MAX_BYTES:
            return None
        text = data.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if text.endswith("\n"):
        text = text[:-1]
    parts = text.split("\n")
    if len(parts) != 2 or "\0" in text:
        return None
    root, rec_sock = parts
    if not (os.path.isabs(root) and os.path.isabs(rec_sock)):
        return None
    if os.path.normpath(rec_sock) != os.path.normpath(sock):
        return None
    return Record(root, rec_sock)


def remove_record(name, environ, sock) -> bool:
    if read_record(name, environ, sock) is None:
        return False
    try:
        os.unlink(os.path.join(records_dir(environ), name))
    except OSError:
        return False
    return True


# -------------------------------------------------------------------- matching

def agentdesk_filter(sessions, records) -> list:
    """Live rows always stay; an exited row needs a record or an `-agents` name."""
    kept = []
    for s in sessions:
        if s.live or records.get(s.name) is not None or _AGENTS_NAME.fullmatch(s.name):
            kept.append(s)
    return kept


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def match_rows(sessions, records, cwd, home) -> list:
    rows = []
    cwd_real = os.path.normpath(os.path.realpath(cwd)) if cwd else None
    home_real = os.path.realpath(home) if home else None
    ancestors = []
    if cwd_real is not None:
        cur = cwd_real
        while cur != os.sep and cur != home_real:
            ancestors.append(cur)
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent
    patterns = [re.compile(re.escape(project_base(a)) + r"-agents(_\d+)?") for a in ancestors]
    for s in sessions:
        rec = records.get(s.name)
        match = None
        if cwd_real is not None:
            if rec is not None:
                if _under(cwd_real, os.path.normpath(rec.root)):
                    match = "record"
            elif any(p.fullmatch(s.name) for p in patterns):
                match = "name"
        rows.append(Row(s, rec, match))
    return rows


def load(environ, cwd, run=subprocess.run) -> Listing:
    zellij = find_zellij(environ)
    if zellij is None:
        raise SessionsError("zellij is not installed or not on PATH")
    sock = socket_dir(environ)
    if not os.path.isdir(sock):
        raise SessionsError(
            "no Agent Desk socket directory at %s (no Agent Desk session has been "
            "started from this machine, or ZELLIJ_SOCKET_DIR points elsewhere)" % sock)
    env = zellij_env(environ, sock)
    sessions = list_sessions(zellij, env, run=run)
    records = {}
    for s in sessions:
        rec = read_record(s.name, environ, sock)
        if rec is not None:
            records[s.name] = rec
    kept = agentdesk_filter(sessions, records)
    rows = match_rows(kept, records, cwd, environ.get("HOME"))
    return Listing(sock, zellij, env, [s.name for s in sessions], rows)


# ---------------------------------------------------------------------- output

def _row_json(row: Row) -> dict:
    s = row.session
    return {"name": s.name, "age": s.age, "state": s.state, "live": s.live,
            "root": row.record.root if row.record else None, "match": row.match}


def _state_label(state: str) -> str:
    return "EXITED" if state == "exited" else state


def _format_rows(rows) -> list:
    width = max((len(r.session.name) for r in rows), default=0)
    lines = []
    for r in rows:
        root = r.record.root if r.record else "-"
        line = "%s  created %s ago  %s  %s" % (
            r.session.name.ljust(width), r.session.age, _state_label(r.session.state), root)
        if r.match == "name":
            line += "  (by name)"
        lines.append(line)
    return lines


def _safe_cwd_note(cwd, err):
    if cwd is None:
        print("note: the current directory no longer exists; not matching sessions to a folder", file=err)


def _names(rows) -> str:
    return ", ".join(r.session.name for r in rows) or "(none)"


# -------------------------------------------------------------------- commands

def run_sessions(args, environ, cwd, out, err, run=subprocess.run) -> int:
    sub = getattr(args, "sessions_command", None)
    try:
        if sub == "kill":
            return _run_kill(args, environ, cwd, out, err, run)
        if sub == "prune":
            return _run_prune(args, environ, cwd, out, err, run)
        return _run_list(args, environ, cwd, out, err, run)
    except SessionsError as exc:
        print(exc.message, file=err)
        return exc.code


def _run_list(args, environ, cwd, out, err, run) -> int:
    list_all = bool(getattr(args, "list_all", False))
    as_json = bool(getattr(args, "list_json", False))
    if cwd is None and not list_all:
        _safe_cwd_note(cwd, err)
        list_all = True
    listing = load(environ, cwd, run=run)
    rows = listing.rows if list_all else [r for r in listing.rows if r.match]
    if as_json:
        print(json.dumps([_row_json(r) for r in rows], indent=2), file=out)
        return 0
    if not rows:
        if list_all:
            print("No Agent Desk sessions in %s." % listing.sock, file=out)
        else:
            print("No Agent Desk sessions for %s. Run `quoin sessions --all` to see all %d sessions."
                  % (cwd, len(listing.rows)), file=out)
        return 0
    header = "Agent Desk sessions in %s" % listing.sock
    if not list_all:
        header += " for %s" % cwd
    print(header, file=out)
    for line in _format_rows(rows):
        print(line, file=out)
    return 0


def _run_kill(args, environ, cwd, out, err, run) -> int:
    listing = load(environ, cwd, run=run)
    name = args.name
    row = next((r for r in listing.rows if r.session.name == name), None)
    if row is None:
        print("no Agent Desk session named %s. Known sessions: %s" % (name, _names(listing.rows)), file=err)
        return 1
    force = bool(getattr(args, "force", False))
    if row.session.live and not force:
        print("%s is running; pass --force to stop and delete it" % name, file=err)
        return 1
    ok, error = delete_session(listing.zellij, listing.env, name, force=force and row.session.live, run=run)
    if not ok:
        print(error, file=err)
        return 1
    remove_record(name, environ, listing.sock)
    print("Deleted %s" % name, file=out)
    return 0


def _run_prune(args, environ, cwd, out, err, run) -> int:
    listing = load(environ, cwd, run=run)
    dry = bool(getattr(args, "dry_run", False))
    removed, failed, records_removed = [], [], []
    for row in listing.rows:
        if row.session.state != "exited":
            continue
        name = row.session.name
        if dry:
            removed.append(name)
            continue
        ok, error = delete_session(listing.zellij, listing.env, name, force=False, run=run)
        if ok:
            removed.append(name)
            remove_record(name, environ, listing.sock)
        else:
            failed.append({"name": name, "error": error})
    base = records_dir(environ)
    try:
        entries = sorted(os.listdir(base)) if base else []
    except OSError:
        entries = []
    now = time.time()
    for entry in entries:
        if entry.startswith(RECORD_TMP_PREFIX):
            continue
        if entry in listing.raw_names or entry in removed:
            continue
        if read_record(entry, environ, listing.sock) is None:
            continue
        try:
            if now - os.lstat(os.path.join(base, entry)).st_mtime <= ORPHAN_MIN_AGE:
                continue
            if not dry:
                os.unlink(os.path.join(base, entry))
        except OSError:
            continue
        records_removed.append(entry)
    if getattr(args, "prune_json", False):
        print(json.dumps({"removed": removed, "failed": failed,
                          "records_removed": records_removed, "dry_run": dry}, indent=2), file=out)
    else:
        verb = "Would remove" if dry else "Removed"
        if removed:
            print("%s: %s" % (verb, ", ".join(removed)), file=out)
        for f in failed:
            print("Failed: %s: %s" % (f["name"], f["error"]), file=err)
        if records_removed:
            print("%s %d stale record(s)" % (verb, len(records_removed)), file=out)
        if not (removed or failed or records_removed):
            print("Nothing to prune.", file=out)
    return 1 if failed else 0


def _is_tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _exec_attach(listing: Listing, name: str, out, err):
    out.flush()
    err.flush()
    os.execvpe(listing.zellij, ["zellij", "attach", name], listing.env)


def run_attach(args, environ, cwd, out, err, run=subprocess.run) -> int:
    if environ.get("ZELLIJ"):
        print("already inside a Zellij session; detach first (Alt s, then d)", file=err)
        return 1
    try:
        listing = load(environ, cwd, run=run)
    except SessionsError as exc:
        print(exc.message, file=err)
        return exc.code
    name = getattr(args, "name", None)
    if cwd is None and not name:
        _safe_cwd_note(cwd, err)
    if name:
        row = next((r for r in listing.rows if r.session.name == name), None)
        if row is None:
            print("no Agent Desk session named %s. Known sessions: %s" % (name, _names(listing.rows)), file=err)
            return 1
        _exec_attach(listing, name, out, err)
        return 0
    live = [r for r in listing.rows if r.session.live]
    if not live:
        print("No live Agent Desk sessions. Run `quoin sessions --all` to see EXITED ones.", file=err)
        return 1
    matches = [r for r in live if r.match]
    if len(matches) == 1:
        _exec_attach(listing, matches[0].session.name, out, err)
        return 0
    cands = matches or live
    for i, line in enumerate(_format_rows(cands), 1):
        print("%d) %s" % (i, line), file=out)
    if not _is_tty():
        print("Several sessions match; run `quoin attach NAME`.", file=err)
        return 1
    for _ in range(3):
        try:
            answer = input("Attach to [1-%d]: " % len(cands)).strip()
        except EOFError:
            return 1
        if not answer:
            return 1
        if answer.isdigit() and 1 <= int(answer) <= len(cands):
            _exec_attach(listing, cands[int(answer) - 1].session.name, out, err)
            return 0
    return 1
