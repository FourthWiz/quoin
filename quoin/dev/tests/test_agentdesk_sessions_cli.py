"""CLI tests for `quoin sessions` and `quoin attach`.

Every test drives cli.main() against a POSIX-sh zellij stub that logs its argv
and the socket directory it was given. XDG_RUNTIME_DIR points at a per-test tmp
dir; a module-level guard fails any test whose stub saw the real user's
/tmp/zellij-agentdesk-UID directory.
"""
import json
import os
import shutil
import stat
import time
from pathlib import Path

import pytest

from quoin import agentdesk_sessions as ads
from quoin import cli

STUB = r"""#!/bin/sh
printf '%s\t%s\n' "$ZELLIJ_SOCKET_DIR" "$*" >> "$CAP_DIR/argv.log"
case "$1" in
  list-sessions)
    cat "$CAP_DIR/list.out" 2>/dev/null
    [ -f "$CAP_DIR/list.err" ] && cat "$CAP_DIR/list.err" >&2
    if [ -f "$CAP_DIR/list.rc" ]; then exit "$(cat "$CAP_DIR/list.rc")"; fi
    exit 0 ;;
  delete-session)
    for last in "$@"; do :; done
    if [ -f "$CAP_DIR/delete-$last.rc" ]; then
      [ -f "$CAP_DIR/delete-$last.err" ] && cat "$CAP_DIR/delete-$last.err" >&2
      exit "$(cat "$CAP_DIR/delete-$last.rc")"
    fi
    exit 0 ;;
esac
exit 0
"""

REAL_SOCK = "/tmp/zellij-agentdesk-%d" % os.getuid()
_CAPS = []


class Ctx:
    def __init__(self, tmp_path, monkeypatch):
        self.tmp = tmp_path
        self.mp = monkeypatch
        self.cap = tmp_path / "cap"
        self.cap.mkdir()
        self.xdg = tmp_path / "xdg"
        self.xdg.mkdir()
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        (self.bin / "zellij").write_text(STUB)
        (self.bin / "zellij").chmod(0o755)
        self.home = Path(os.environ["HOME"])
        for k in ("ZELLIJ", "ZELLIJ_SESSION_NAME", "ZELLIJ_SOCKET_DIR"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(self.xdg))
        monkeypatch.setenv("PATH", "%s:/usr/bin:/bin" % self.bin)
        monkeypatch.setenv("CAP_DIR", str(self.cap))
        self.sock = self.xdg / ("zellij-agentdesk-%d" % os.getuid())
        self.sock.mkdir()
        _CAPS.append(self.cap)

    def listing(self, *rows):
        (self.cap / "list.out").write_text("".join(r + "\n" for r in rows))

    def fail_listing(self, rc, err):
        (self.cap / "list.rc").write_text(str(rc))
        (self.cap / "list.err").write_text(err)

    def fail_delete(self, name, rc=1, err="cannot delete"):
        (self.cap / ("delete-%s.rc" % name)).write_text(str(rc))
        (self.cap / ("delete-%s.err" % name)).write_text(err)

    def record(self, name, root, sock=None):
        d = self.home / ".config" / "agentdesk" / "sessions"
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text("%s\n%s" % (os.path.realpath(root), sock or self.sock))
        return d / name

    def calls(self):
        f = self.cap / "argv.log"
        return [l.split("\t", 1) for l in f.read_text().splitlines()] if f.exists() else []

    def deletes(self):
        return [argv for _, argv in self.calls() if argv.startswith("delete-session")]

    def run(self, capsys, *args, cwd=None):
        if cwd is not None:
            self.mp.chdir(cwd)
        rc = cli.main(list(args))
        cap = capsys.readouterr()
        return rc, cap.out, cap.err


@pytest.fixture(autouse=True)
def ctx(tmp_path, monkeypatch):
    c = Ctx(tmp_path, monkeypatch)
    yield c
    for cap in _CAPS:
        log = cap / "argv.log"
        if log.exists():
            for line in log.read_text().splitlines():
                assert line.split("\t", 1)[0] != REAL_SOCK, "test reached the real socket dir"
    _CAPS.clear()


ROW = "%s [Created %s ago] %s"


def live(name, age="3m"):
    return ROW % (name, age, "")


def current(name, age="1m"):
    return ROW % (name, age, "(current)")


def exited(name, age="2h"):
    return ROW % (name, age, "(EXITED - attach to resurrect)")


@pytest.fixture
def proj(tmp_path):
    p = tmp_path / "X" / "proj"
    (p / "sub" / "dir").mkdir(parents=True)
    (tmp_path / "X" / "other").mkdir()
    return p


# ---------------------------------------------------------------- AC1: socket

def test_socket_dir_from_xdg(ctx, capsys, tmp_path):
    ctx.listing(live("a-agents"))
    rc, out, _ = ctx.run(capsys, "sessions", "--all", cwd=tmp_path)
    assert rc == 0
    assert {s for s, _ in ctx.calls()} == {str(ctx.sock)}
    assert str(ctx.sock) in out


def test_preset_socket_dir_is_passed_verbatim(ctx, capsys, tmp_path, monkeypatch):
    own = tmp_path / "own-sock"
    own.mkdir()
    monkeypatch.setenv("ZELLIJ_SOCKET_DIR", str(own))
    ctx.listing(live("a-agents"))
    rc, _, _ = ctx.run(capsys, "sessions", "--all", cwd=tmp_path)
    assert rc == 0
    assert {s for s, _ in ctx.calls()} == {str(own)}


def test_default_tmp_socket_dir_uses_fake_uid(ctx, capsys, tmp_path, monkeypatch):
    fake = 3900000000 + os.getpid() % 100000
    path = Path("/tmp/zellij-agentdesk-%d" % fake)
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    monkeypatch.setattr(ads, "current_uid", lambda: fake)
    path.mkdir(exist_ok=False)
    try:
        ctx.listing(live("a-agents"))
        rc, _, _ = ctx.run(capsys, "sessions", "--all", cwd=tmp_path)
        assert rc == 0
        assert {s for s, _ in ctx.calls()} == {str(path)}
    finally:
        shutil.rmtree(path, ignore_errors=True)


def test_listing_argv_is_exact(ctx, capsys, tmp_path):
    ctx.listing(live("a-agents"))
    ctx.run(capsys, "sessions", "--all", cwd=tmp_path)
    assert [a for _, a in ctx.calls()] == ["list-sessions -n"]


# ----------------------------------------------------------------- AC2: listing

def test_all_json_four_entries(ctx, capsys, tmp_path):
    ctx.listing(current("proj-agents", "1m"), live("proj-agents_2", "5h"),
                exited("other-agents", "2d"), exited("deck-1", "3m"))
    ctx.record("deck-1", tmp_path)
    rc, out, _ = ctx.run(capsys, "sessions", "--all", "--json", cwd=tmp_path)
    assert rc == 0
    got = json.loads(out)
    assert [(e["name"], e["age"], e["state"], e["live"]) for e in got] == [
        ("proj-agents", "1m", "current", True), ("proj-agents_2", "5h", "running", True),
        ("other-agents", "2d", "exited", False), ("deck-1", "3m", "exited", False)]
    assert got[3]["root"] == os.path.realpath(tmp_path)


def test_foreign_exited_dropped_foreign_live_kept(ctx, capsys, tmp_path):
    ctx.listing(exited("scratch"), live("notes"))
    rc, out, _ = ctx.run(capsys, "sessions", "--all", "--json", cwd=tmp_path)
    got = json.loads(out)
    assert [e["name"] for e in got] == ["notes"] and got[0]["match"] is None
    rc, out, _ = ctx.run(capsys, "sessions", "--all", cwd=tmp_path)
    assert "scratch" not in out and "notes" in out
    rc, out, _ = ctx.run(capsys, "sessions", cwd=tmp_path)
    assert "scratch" not in out


def test_text_output_marks_name_matches(ctx, capsys, proj):
    ctx.listing(live("proj-agents"))
    rc, out, _ = ctx.run(capsys, "sessions", cwd=proj)
    assert rc == 0 and "(by name)" in out and "created 3m ago" in out and "running" in out


# -------------------------------------------------------------------- AC3 / AC4

def test_record_match_equal_and_under_but_not_other(ctx, capsys, proj, tmp_path):
    ctx.listing(live("proj-agents"))
    ctx.record("proj-agents", proj)
    for cwd in (proj, proj / "sub" / "dir"):
        rc, out, _ = ctx.run(capsys, "sessions", "--json", cwd=cwd)
        assert [e["name"] for e in json.loads(out)] == ["proj-agents"]
        assert json.loads(out)[0]["match"] == "record"
    rc, out, _ = ctx.run(capsys, "sessions", "--json", cwd=tmp_path / "X" / "other")
    assert "proj-agents" not in [e["name"] for e in json.loads(out)]


def test_name_match_for_legacy_session_and_not_projector(ctx, capsys, proj):
    ctx.listing(live("proj-agents_2"), live("projector-agents"))
    rc, out, _ = ctx.run(capsys, "sessions", "--json", cwd=proj)
    got = json.loads(out)
    assert [(e["name"], e["match"]) for e in got] == [("proj-agents_2", "name")]


# ------------------------------------------------------------------------ AC5

def test_no_match_message_mentions_all(ctx, capsys, proj, tmp_path):
    ctx.listing(live("zzz-agents"))
    rc, out, _ = ctx.run(capsys, "sessions", cwd=proj)
    assert rc == 0 and "--all" in out and "1 sessions" in out
    rc, out, _ = ctx.run(capsys, "sessions", "--json", cwd=proj)
    assert rc == 0 and json.loads(out) == []


def test_all_with_nothing_listed(ctx, capsys, tmp_path):
    (ctx.cap / "list.out").write_text("")
    ctx.fail_listing(1, "No active zellij sessions found.\n")
    rc, out, _ = ctx.run(capsys, "sessions", "--all", cwd=tmp_path)
    assert rc == 0 and "No Agent Desk sessions in" in out


def test_missing_socket_dir_exits_2(ctx, capsys, tmp_path):
    shutil.rmtree(ctx.sock)
    rc, out, err = ctx.run(capsys, "sessions", "--all", cwd=tmp_path)
    assert rc == 2 and str(ctx.sock) in err and ctx.calls() == []


def test_missing_binary_exits_2(ctx, capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    rc, _, err = ctx.run(capsys, "sessions", "--all", cwd=tmp_path)
    assert rc == 2 and "not installed" in err


def test_listing_failure_exits_2_with_stderr(ctx, capsys, tmp_path):
    ctx.fail_listing(3, "boom\n")
    for extra in ([], ["--json"]):
        rc, out, err = ctx.run(capsys, "sessions", "--all", *extra, cwd=tmp_path)
        assert rc == 2 and "boom" in err


def test_deleted_cwd_does_not_crash(ctx, capsys, tmp_path, monkeypatch):
    ctx.listing(live("notes"))
    gone = tmp_path / "gone"
    gone.mkdir()
    monkeypatch.chdir(gone)
    gone.rmdir()
    rc = cli.main(["sessions"])
    cap = capsys.readouterr()
    assert rc == 0 and "notes" in cap.out and "no longer exists" in cap.err
    rc = cli.main(["sessions", "--all", "--json"])
    assert rc == 0


def test_list_flags_with_subcommand_rejected(ctx, capsys, tmp_path):
    with pytest.raises(SystemExit) as ei:
        cli.main(["sessions", "--all", "kill", "x"])
    assert ei.value.code == 2
    capsys.readouterr()
    assert ctx.calls() == []


# ------------------------------------------------------------------------ AC11

KEYS = ("Alt s, then d", "Alt s, then q", "Ctrl+Q")


@pytest.mark.parametrize("argv", [["sessions", "--help"], ["attach", "--help"],
                                  ["sessions", "kill", "--help"], ["sessions", "prune", "--help"]])
def test_help_has_key_hints(argv, capsys):
    with pytest.raises(SystemExit) as ei:
        cli.main(argv)
    assert ei.value.code == 0
    out = capsys.readouterr().out
    for k in KEYS:
        assert k in out


# -------------------------------------------------------------------- AC9: kill

def test_kill_exited_deletes_and_removes_record(ctx, capsys, tmp_path):
    ctx.listing(exited("proj-agents_2"))
    rec = ctx.record("proj-agents_2", tmp_path)
    rc, out, _ = ctx.run(capsys, "sessions", "kill", "proj-agents_2", cwd=tmp_path)
    assert rc == 0 and not rec.exists()
    assert ctx.deletes() == ["delete-session proj-agents_2"]
    assert {s for s, a in ctx.calls() if a.startswith("delete")} == {str(ctx.sock)}


def test_kill_running_needs_force(ctx, capsys, tmp_path):
    ctx.listing(live("proj-agents"))
    rc, _, err = ctx.run(capsys, "sessions", "kill", "proj-agents", cwd=tmp_path)
    assert rc == 1 and "--force" in err and ctx.deletes() == []
    rc, _, _ = ctx.run(capsys, "sessions", "kill", "proj-agents", "--force", cwd=tmp_path)
    assert rc == 0 and ctx.deletes() == ["delete-session --force proj-agents"]


def test_kill_unknown_and_foreign_exited(ctx, capsys, tmp_path):
    ctx.listing(live("notes"), exited("scratch"))
    for name in ("nosuch", "scratch"):
        rc, _, err = ctx.run(capsys, "sessions", "kill", name, cwd=tmp_path)
        assert rc == 1 and "notes" in err
    assert ctx.deletes() == []


def test_kill_failure_keeps_record(ctx, capsys, tmp_path):
    ctx.listing(exited("deck-1"))
    rec = ctx.record("deck-1", tmp_path)
    ctx.fail_delete("deck-1", 1, "cannot delete")
    rc, _, err = ctx.run(capsys, "sessions", "kill", "deck-1", cwd=tmp_path)
    assert rc == 1 and rec.exists() and "cannot delete" in err


def test_kill_ignores_record_for_another_socket(ctx, capsys, tmp_path):
    ctx.listing(exited("deck-1"))
    rec = ctx.record("deck-1", tmp_path, sock="/elsewhere/zellij-agentdesk-1")
    rc, _, _ = ctx.run(capsys, "sessions", "kill", "deck-1", cwd=tmp_path)
    # Not an Agent Desk-shaped name and no valid record: treated as foreign.
    assert rc == 1 and rec.exists() and ctx.deletes() == []


# ------------------------------------------------------------------- prune

def test_prune_deletes_only_agentdesk_exited(ctx, capsys, tmp_path):
    ctx.listing(exited("a-agents"), exited("deck-1"), live("run-agents"), exited("scratch"))
    r1 = ctx.record("deck-1", tmp_path)
    r2 = ctx.record("a-agents", tmp_path)
    rc, out, _ = ctx.run(capsys, "sessions", "prune", "--json", cwd=tmp_path)
    got = json.loads(out)
    assert rc == 0 and sorted(got["removed"]) == ["a-agents", "deck-1"] and got["failed"] == []
    assert sorted(ctx.deletes()) == ["delete-session a-agents", "delete-session deck-1"]
    assert not r1.exists() and not r2.exists()


def test_prune_partial_failure(ctx, capsys, tmp_path):
    ctx.listing(exited("a-agents"), exited("b-agents"))
    ctx.fail_delete("a-agents", 2, "nope")
    rc, out, err = ctx.run(capsys, "sessions", "prune", "--json", cwd=tmp_path)
    got = json.loads(out)
    assert rc == 1 and got["removed"] == ["b-agents"]
    assert got["failed"] == [{"name": "a-agents", "error": "nope"}]
    assert len(ctx.deletes()) == 2


def test_prune_text_output(ctx, capsys, tmp_path):
    ctx.listing(exited("a-agents"))
    rc, out, _ = ctx.run(capsys, "sessions", "prune", cwd=tmp_path)
    assert rc == 0 and "Removed: a-agents" in out


def _old(path, age=600):
    t = time.time() - age
    os.utime(path, (t, t))


def test_orphan_records(ctx, capsys, tmp_path):
    ctx.listing(live("listed"))
    d = ctx.home / ".config" / "agentdesk" / "sessions"
    old = ctx.record("old-orphan", tmp_path)
    young = ctx.record("young-orphan", tmp_path)
    other = ctx.record("other-sock", tmp_path, sock="/elsewhere/zellij-agentdesk-1")
    listed = ctx.record("listed", tmp_path)
    tmpf = d / (ads.RECORD_TMP_PREFIX + "abc123")
    tmpf.write_text("%s\n%s" % (tmp_path, ctx.sock))
    for p in (old, other, listed, tmpf):
        _old(p)
    rc, out, _ = ctx.run(capsys, "sessions", "prune", "--json", "--dry-run", cwd=tmp_path)
    got = json.loads(out)
    assert got["records_removed"] == ["old-orphan"] and got["dry_run"] is True
    assert old.exists() and ctx.deletes() == []
    rc, out, _ = ctx.run(capsys, "sessions", "prune", "--json", cwd=tmp_path)
    assert json.loads(out)["records_removed"] == ["old-orphan"]
    assert not old.exists()
    assert young.exists() and other.exists() and listed.exists() and tmpf.exists()


# ------------------------------------------------------------------- attach

class _Exec(Exception):
    pass


@pytest.fixture
def execs(monkeypatch):
    seen = []

    def fake(file, argv, env):
        seen.append((file, argv, env))
        raise _Exec()

    monkeypatch.setattr(os, "execvpe", fake)
    return seen


def test_attach_by_name_execs(ctx, capsys, execs, tmp_path):
    ctx.listing(live("foo"))
    with pytest.raises(_Exec):
        ctx.run(capsys, "attach", "foo", cwd=tmp_path)
    file, argv, env = execs[0]
    assert argv == ["zellij", "attach", "foo"]
    assert env["ZELLIJ_SOCKET_DIR"] == str(ctx.sock)
    assert file == str(ctx.bin / "zellij")


def test_attach_exited_agentdesk_row_execs(ctx, capsys, execs, tmp_path):
    ctx.listing(exited("old-agents"))
    with pytest.raises(_Exec):
        ctx.run(capsys, "attach", "old-agents", cwd=tmp_path)
    assert execs[0][1] == ["zellij", "attach", "old-agents"]


def test_attach_foreign_exited_and_unknown_do_not_exec(ctx, capsys, execs, tmp_path):
    ctx.listing(exited("scratch"), live("notes"))
    for name in ("scratch", "nosuch"):
        rc, _, err = ctx.run(capsys, "attach", name, cwd=tmp_path)
        assert rc == 1 and "notes" in err
    assert execs == []


def test_attach_uses_passed_environ_for_exec(ctx, capsys, execs, tmp_path, monkeypatch):
    ctx.listing(live("foo"))
    monkeypatch.setenv("MARKER_VAR", "yes")
    with pytest.raises(_Exec):
        ctx.run(capsys, "attach", "foo", cwd=tmp_path)
    assert execs[0][2]["MARKER_VAR"] == "yes"


def test_attach_single_match_autopicks(ctx, capsys, execs, proj):
    ctx.listing(live("proj-agents"), live("notes"))
    with pytest.raises(_Exec):
        ctx.run(capsys, "attach", cwd=proj)
    assert execs[0][1][-1] == "proj-agents"


def _two(ctx, proj):
    ctx.listing(live("proj-agents"), live("proj-agents_2"), exited("proj-agents_3"))


def test_attach_several_without_tty_lists_and_fails(ctx, capsys, execs, proj, monkeypatch):
    _two(ctx, proj)
    monkeypatch.setattr(ads, "_is_tty", lambda: False)
    rc, out, err = ctx.run(capsys, "attach", cwd=proj)
    assert rc == 1 and "proj-agents_2" in out and "proj-agents_3" not in out and execs == []


def test_attach_tty_pick_second(ctx, capsys, execs, proj, monkeypatch):
    _two(ctx, proj)
    monkeypatch.setattr(ads, "_is_tty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "2")
    with pytest.raises(_Exec):
        ctx.run(capsys, "attach", cwd=proj)
    assert execs[0][1][-1] == "proj-agents_2"


def test_attach_invalid_answers_and_eof_fail(ctx, capsys, execs, proj, monkeypatch):
    _two(ctx, proj)
    monkeypatch.setattr(ads, "_is_tty", lambda: True)
    answers = iter(["x", "9", "0"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    assert ctx.run(capsys, "attach", cwd=proj)[0] == 1

    def eof(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    assert ctx.run(capsys, "attach", cwd=proj)[0] == 1
    assert execs == []


def test_attach_no_match_offers_all_live(ctx, capsys, execs, tmp_path, monkeypatch):
    ctx.listing(live("a-agents"), live("b-agents"), exited("c-agents"))
    monkeypatch.setattr(ads, "_is_tty", lambda: False)
    rc, out, _ = ctx.run(capsys, "attach", cwd=tmp_path)
    assert rc == 1 and "a-agents" in out and "b-agents" in out and "c-agents" not in out


def test_attach_no_live_sessions(ctx, capsys, execs, tmp_path):
    ctx.listing(exited("c-agents"))
    rc, _, err = ctx.run(capsys, "attach", cwd=tmp_path)
    assert rc == 1 and "--all" in err and execs == []


def test_exited_matches_are_never_autopicked(ctx, capsys, execs, proj, monkeypatch):
    ctx.listing(exited("proj-agents"), live("notes"))
    monkeypatch.setattr(ads, "_is_tty", lambda: False)
    rc, _, _ = ctx.run(capsys, "attach", cwd=proj)
    assert rc == 1 and execs == []


def test_attach_inside_zellij_refuses(ctx, capsys, execs, tmp_path, monkeypatch):
    monkeypatch.setenv("ZELLIJ", "0")
    ctx.listing(live("foo"))
    rc, _, err = ctx.run(capsys, "attach", "foo", cwd=tmp_path)
    assert rc == 1 and "inside" in err and execs == [] and ctx.calls() == []


def test_attach_survives_deleted_cwd(ctx, capsys, execs, tmp_path, monkeypatch):
    ctx.listing(live("a-agents"), live("b-agents"))
    gone = tmp_path / "gone"
    gone.mkdir()
    monkeypatch.chdir(gone)
    gone.rmdir()
    monkeypatch.setattr(ads, "_is_tty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "2")
    with pytest.raises(_Exec):
        cli.main(["attach"])
    assert execs[0][1][-1] == "b-agents"
    assert "no longer exists" in capsys.readouterr().err
