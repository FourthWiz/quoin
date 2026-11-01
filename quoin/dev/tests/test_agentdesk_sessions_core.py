"""Unit tests for quoin.agentdesk_sessions: parsing, records and matching."""
import os
import subprocess
import types

import pytest

from quoin import agentdesk_sessions as ads
from quoin.agentdesk_sessions import Record, Session


def _proc(out="", err="", rc=0):
    return types.SimpleNamespace(stdout=out, stderr=err, returncode=rc)


# ------------------------------------------------------------------- parsing

def test_parse_states():
    text = (
        "proj-agents [Created 2h 3m ago] \n"
        "here-agents [Created 5s ago] (current)\n"
        "old-agents_2 [Created 1day ago] (EXITED - attach to resurrect)\n"
    )
    got = [(s.name, s.age, s.state) for s in ads.parse_list_output(text)]
    assert got == [
        ("proj-agents", "2h 3m", "running"),
        ("here-agents", "5s", "current"),
        ("old-agents_2", "1day", "exited"),
    ]


def test_parse_strips_ansi():
    text = "\x1b[32;1mproj-agents\x1b[m [Created 1m ago] \x1b[31;1m(EXITED - attach to resurrect)\x1b[m\n"
    (s,) = ads.parse_list_output(text)
    assert (s.name, s.state, s.live) == ("proj-agents", "exited", False)


def test_name_containing_exited_stays_running():
    (s,) = ads.parse_list_output("EXITED-agents [Created 1m ago] \n")
    assert s.state == "running" and s.live


def test_garbage_lines_ignored():
    assert ads.parse_list_output("hello\n\nnot a row [Created\n") == []


def test_list_sentinel_gives_empty_whatever_the_exit_code():
    for rc in (0, 1):
        run = lambda *a, **k: _proc(err="No active zellij sessions found.\n", rc=rc)
        assert ads.list_sessions("zellij", {}, run=run) == []
    run = lambda *a, **k: _proc(out="No active zellij sessions found.\n", rc=1)
    assert ads.list_sessions("zellij", {}, run=run) == []


def test_list_other_failure_raises_exit_2():
    run = lambda *a, **k: _proc(err="boom\n", rc=3)
    with pytest.raises(ads.SessionsError) as ei:
        ads.list_sessions("zellij", {}, run=run)
    assert ei.value.code == 2 and "boom" in ei.value.message


def test_list_timeout_and_oserror_raise():
    def timeout(*a, **k):
        raise subprocess.TimeoutExpired("zellij", 10)

    def missing(*a, **k):
        raise FileNotFoundError("zellij")

    for run in (timeout, missing):
        with pytest.raises(ads.SessionsError) as ei:
            ads.list_sessions("zellij", {}, run=run)
        assert ei.value.code == 2


def test_list_call_shape():
    seen = {}

    def run(argv, **kw):
        seen["argv"], seen["kw"] = argv, kw
        return _proc(out="a [Created 1s ago] \n")

    ads.list_sessions("/z/zellij", {"X": "1"}, run=run)
    assert seen["argv"] == ["/z/zellij", "list-sessions", "-n"]
    assert seen["kw"]["timeout"] == ads.ZELLIJ_TIMEOUT and seen["kw"]["env"] == {"X": "1"}


def test_delete_session_shapes():
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return _proc(err="nope", rc=2)

    assert ads.delete_session("z", {}, "n", force=True, run=run) == (False, "nope")
    assert calls == [["z", "delete-session", "--force", "--", "n"]]
    assert ads.delete_session("z", {}, "n", run=lambda *a, **k: _proc()) == (True, "")


# ------------------------------------------------------------------ environ

def test_socket_dir_rules():
    assert ads.socket_dir({"ZELLIJ_SOCKET_DIR": "/s"}, uid=1) == "/s"
    assert ads.socket_dir({"XDG_RUNTIME_DIR": "/x"}, uid=7) == "/x/zellij-agentdesk-7"
    assert ads.socket_dir({}, uid=7) == "/tmp/zellij-agentdesk-7"
    assert ads.socket_dir({"ZELLIJ_SOCKET_DIR": "", "XDG_RUNTIME_DIR": ""}, uid=7) == "/tmp/zellij-agentdesk-7"


def test_socket_dir_uses_current_uid_when_unset(monkeypatch):
    monkeypatch.setattr(ads, "current_uid", lambda: 4242)
    assert ads.socket_dir({}) == "/tmp/zellij-agentdesk-4242"


def test_zellij_env_copies():
    base = {"A": "1"}
    env = ads.zellij_env(base, "/s")
    assert env == {"A": "1", "ZELLIJ_SOCKET_DIR": "/s"} and base == {"A": "1"}


def test_engine_socket_dir_unchanged():
    from quoin.streamdeck import engine
    assert engine._socket_dir({"ZELLIJ_SOCKET_DIR": "/s"}) == "/s"
    assert engine._socket_dir({"XDG_RUNTIME_DIR": "/x"}, uid=5) == "/x/zellij-agentdesk-5"
    assert engine._socket_dir({}, uid=5) == "/tmp/zellij-agentdesk-5"


# ------------------------------------------------------------------- naming

@pytest.mark.parametrize("raw,want", [
    ("proj", "proj"), ("my proj", "my-proj"), ("a.b", "a.b"), (".hidden", ".hidden"),
    ("-lead", "lead"), ("trail-", "trail"), ("a---b", "a-b"), ("a!!b", "a-b"),
    ("x_y", "x_y"), ("---", ""), ("", ""),
])
def test_sanitize_name(raw, want):
    assert ads.sanitize_name(raw) == want


def test_project_base():
    assert ads.project_base("/r/my proj") == "my-proj"
    assert ads.project_base("/") == "agentdesk"
    assert ads.project_base("") == "agentdesk"


# ------------------------------------------------------------------ records

SOCK = "/tmp/zz/zellij-agentdesk-1"


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    (h / ".config" / "agentdesk" / "sessions").mkdir(parents=True)
    return h


def _put(home, name, content, mode="w"):
    p = home / ".config" / "agentdesk" / "sessions" / name
    if isinstance(content, bytes):
        p.write_bytes(content)
    else:
        p.write_text(content)
    return p


def _env(home):
    return {"HOME": str(home)}


def test_read_record_ok_variants(home):
    _put(home, "a", "/x/proj\n" + SOCK)
    _put(home, "b", "/x/proj\n" + SOCK + "\n")
    _put(home, "c", "/x/proj\n" + SOCK + "/")
    _put(home, "d", "/x/проект\n" + SOCK)
    assert ads.read_record("a", _env(home), SOCK) == Record("/x/proj", SOCK)
    assert ads.read_record("b", _env(home), SOCK).root == "/x/proj"
    assert ads.read_record("c", _env(home), SOCK) is not None
    assert ads.read_record("d", _env(home), SOCK).root == "/x/проект"


def test_read_record_rejections(home):
    sessions = home / ".config" / "agentdesk" / "sessions"
    _put(home, "oneline", "/x/proj")
    _put(home, "three", "/x/proj\n%s\nextra" % SOCK)
    _put(home, "rel", "x/proj\n" + SOCK)
    _put(home, "relsock", "/x/proj\nsock")
    _put(home, "other", "/x/proj\n/elsewhere")
    _put(home, "big", "/x/" + "a" * 5000 + "\n" + SOCK)
    _put(home, "bad", b"/x/\xff\xfe\n" + SOCK.encode())
    _put(home, "nul", "/x/p\0q\n" + SOCK)
    (sessions / "dir").mkdir()
    real = _put(home, "real", "/x/proj\n" + SOCK)
    os.symlink(real, sessions / "link")
    os.mkfifo(sessions / "fifo")
    for name in ("oneline", "three", "rel", "relsock", "other", "big", "bad", "nul",
                 "dir", "link", "fifo", "missing", "", ".", "..", "a/b", "a\0b"):
        assert ads.read_record(name, _env(home), SOCK) is None, name


def test_read_record_without_home():
    assert ads.read_record("a", {}, SOCK) is None
    assert ads.records_dir({"HOME": ""}) is None


def test_remove_record_only_when_valid_for_socket(home):
    p = _put(home, "a", "/x/proj\n" + SOCK)
    assert ads.remove_record("a", _env(home), "/other") is False and p.exists()
    assert ads.remove_record("a", _env(home), SOCK) is True and not p.exists()
    assert ads.remove_record("a", _env(home), SOCK) is False


# ----------------------------------------------------------------- matching

def _s(name, state="running"):
    return Session(name, "1m", state)


def test_agentdesk_filter():
    rows = [_s("notes"), _s("scratch", "exited"), _s("x-agents", "exited"), _s("y-agents_3", "exited"),
            _s("deck-1", "exited"), _s("keep", "current")]
    kept = ads.agentdesk_filter(rows, {"deck-1": Record("/r", SOCK)})
    assert [s.name for s in kept] == ["notes", "x-agents", "y-agents_3", "deck-1", "keep"]


def _match(sessions, records, cwd, home):
    return {r.session.name: r.match for r in ads.match_rows(sessions, records, str(cwd), str(home))}


def test_record_rule_equal_under_sibling(tmp_path):
    proj = tmp_path / "proj"
    (proj / "sub").mkdir(parents=True)
    (tmp_path / "proj2").mkdir()
    home = tmp_path / "h"
    home.mkdir()
    rec = {"s": Record(os.path.realpath(proj), SOCK)}
    sessions = [_s("s")]
    assert _match(sessions, rec, proj, home) == {"s": "record"}
    assert _match(sessions, rec, proj / "sub", home) == {"s": "record"}
    assert _match(sessions, rec, tmp_path / "proj2", home) == {"s": None}


def test_record_session_is_never_matched_by_name(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    home = tmp_path / "h"
    home.mkdir()
    rec = {"proj-agents": Record("/somewhere/else", SOCK)}
    assert _match([_s("proj-agents")], rec, proj, home) == {"proj-agents": None}


def test_name_rule_from_subdir_and_ancestors(tmp_path):
    deep = tmp_path / "proj" / "a" / "b"
    deep.mkdir(parents=True)
    home = tmp_path / "h"
    home.mkdir()
    got = _match([_s("proj-agents"), _s("proj-agents_2"), _s("b-agents"), _s("projector-agents"),
                  _s("other-agents")], {}, deep, home)
    assert got == {"proj-agents": "name", "proj-agents_2": "name", "b-agents": "name",
                   "projector-agents": None, "other-agents": None}


def test_name_rule_stops_at_symlinked_home(tmp_path):
    real_home = tmp_path / "real-home"
    work = real_home / "work"
    work.mkdir(parents=True)
    link = tmp_path / "home-link"
    os.symlink(real_home, link)
    # The base of HOME itself must not be considered, even though HOME is given as a symlink.
    got = _match([_s("real-home-agents"), _s("home-link-agents"), _s("work-agents")], {}, work, link)
    assert got == {"real-home-agents": None, "home-link-agents": None, "work-agents": "name"}


def test_match_without_cwd_is_none():
    rows = ads.match_rows([_s("a-agents")], {}, None, "/h")
    assert rows[0].match is None
