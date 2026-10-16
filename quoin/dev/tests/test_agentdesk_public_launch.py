"""Tests for the public non-interactive agentdesk launch interface.

Covers --panes, --no-prompt, --if-exists and the AGENTDESK_API_VERSION marker,
plus regression checks for the legacy invocation forms. Everything runs in a
hermetic HOME/TMPDIR with a recording zellij stub; nothing touches the real
home directory, the shared zellij socket dir, or a real zellij.
"""
import os
import pty
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
AGENTDESK_ZSH = REPO_ROOT / "quoin" / "tools" / "agentdesk" / "agentdesk.zsh"

pytestmark = pytest.mark.skipif(
    shutil.which("zsh") is None, reason="zsh not available on this system"
)

SUBPROCESS_TIMEOUT = 30
DASHBOARD_PROMPT = "Open quoin dashboard"
PICKER_PROMPT = "Select a layout"
COLLISION_PROMPT = "Attach to it, or start a new session"
REUSE_NOTICE = "Reusing last layout"

ZELLIJ_STUB = r"""#!/bin/sh
cap="$CAP_DIR"
mkdir -p "$cap"
{ printf '%s\t' "$@"; printf '\n'; } >> "$cap/argv.log"
case "$1" in
  list-sessions)
    [ -f "$cap/sessions.txt" ] && while IFS= read -r s; do
      printf '%s [Created 3m ago] (EXITED - attach to resume)\n' "$s"
    done < "$cap/sessions.txt"
    exit 0 ;;
  attach)
    printf '%s' "$2" > "$cap/attach.txt"
    exit 0 ;;
esac
prev=""
for a in "$@"; do
  if [ "$prev" = "--new-session-with-layout" ]; then
    cp "$a" "$cap/layout.kdl"
    n=$(ls "$cap"/layout-*.kdl 2>/dev/null | wc -l | tr -d ' ')
    cp "$a" "$cap/layout-$((n + 1)).kdl"
    printf '%s' "$a" > "$cap/layout_path.txt"
    printf '[%s]\n' "$AGENTDESK_DASHBOARD_URL_FILE" >> "$cap/dash_env.log"
    if [ -n "$AGENTDESK_DASHBOARD_URL_FILE" ]; then
      printf 'http://127.0.0.1:1\n' > "$AGENTDESK_DASHBOARD_URL_FILE"
    fi
  fi
  prev="$a"
done
exit 0
"""

OPEN_STUB = '#!/bin/sh\necho "$@" >> "$CAP_DIR/open.log"\nexit 0\n'


@pytest.fixture
def rt_dir():
    """Short private runtime dir: keeps ZELLIJ_SOCKET_DIR under the length guard."""
    path = tempfile.mkdtemp(prefix="ad-", dir="/tmp")
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _env(tmp_path, rt_dir, sessions=(), extra=None, with_server=False):
    home = tmp_path / "home"
    (home / ".config" / "zellij" / "layouts").mkdir(parents=True, exist_ok=True)
    (home / ".config" / "zellij" / "layouts" / "agent-desk.kdl").write_text(
        "layout {\n}\n"
    )
    if with_server:
        scripts = home / ".claude" / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        (scripts / "dashboard_server.py").write_text("# stub dashboard server\n")
    (tmp_path / "tmpdir").mkdir(exist_ok=True)
    (tmp_path / "proj").mkdir(exist_ok=True)
    cap = tmp_path / "cap"
    cap.mkdir(exist_ok=True)
    (cap / "sessions.txt").write_text("".join(f"{s}\n" for s in sessions))
    mock_bin = tmp_path / "mock_bin"
    mock_bin.mkdir(exist_ok=True)
    for name in ("claude", "codex", "ccr"):
        (mock_bin / name).write_text("#!/bin/sh\nexit 0\n")
    (mock_bin / "zellij").write_text(ZELLIJ_STUB)
    for name in ("open", "xdg-open"):
        (mock_bin / name).write_text(OPEN_STUB)
    for f in mock_bin.iterdir():
        f.chmod(0o755)

    env = dict(os.environ)
    for key in (
        "ZELLIJ", "ZELLIJ_SESSION_NAME", "ZELLIJ_SOCKET_DIR", "AGENTDESK_PICK",
        "AGENTDESK_DASHBOARD", "AGENTDESK_DASHBOARD_URL_FILE", "XDG_CONFIG_HOME",
        "AGENTDESK_COMPACT_BAR",
    ):
        env.pop(key, None)
    env.update(
        HOME=str(home),
        XDG_RUNTIME_DIR=rt_dir,
        TMPDIR=str(tmp_path / "tmpdir"),
        CAP_DIR=str(cap),
        PATH=f"{mock_bin}:{os.environ['PATH']}",
    )
    if extra:
        env.update(extra)
    return env


def _argv(args):
    return ["zsh", "-c", 'source "$0"; shift; agentdesk "$@"', str(AGENTDESK_ZSH), "_", *args]


def _check_env(env, tmp_path, rt_dir):
    assert env["HOME"].startswith(str(tmp_path))
    assert env["TMPDIR"].startswith(str(tmp_path))
    assert env["XDG_RUNTIME_DIR"] == rt_dir


class Result:
    rt_dir = ""

    def __init__(self, rc, out, err, cap, timed_out=False):
        self.rc, self.out, self.err, self.cap = rc, out, err, cap
        self.timed_out = timed_out

    @property
    def text(self):
        return self.out + self.err

    @property
    def argv_lines(self):
        f = self.cap / "argv.log"
        return f.read_text().splitlines() if f.exists() else []


def _finish(proc, tmp_path, cap):
    try:
        proc.wait(timeout=SUBPROCESS_TIMEOUT)
        timed_out = False
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        timed_out = True
    out = (tmp_path / "stdout.txt").read_text()
    err = (tmp_path / "stderr.txt").read_text()
    return Result(proc.returncode, out, err, cap, timed_out)


def _run_pipe(args, env, tmp_path, rt_dir):
    """Stdin is an open, never-written pipe: any read blocks until timeout."""
    _check_env(env, tmp_path, rt_dir)
    with open(tmp_path / "stdout.txt", "w") as so, open(tmp_path / "stderr.txt", "w") as se:
        proc = subprocess.Popen(
            _argv(args), stdin=subprocess.PIPE, stdout=so, stderr=se, env=env,
            cwd=str(tmp_path / "proj"),
        )
        res = _finish(proc, tmp_path, tmp_path / "cap")
        proc.stdin.close()
    assert not res.timed_out, f"blocked on stdin; stderr={res.err}"
    res.rt_dir = rt_dir
    return res


def _run_pty(args, env, tmp_path, rt_dir, input_text=""):
    """Stdin is a pty slave; master stays open. Only input_text is ever written."""
    _check_env(env, tmp_path, rt_dir)
    master, slave = pty.openpty()
    try:
        with open(tmp_path / "stdout.txt", "w") as so, open(tmp_path / "stderr.txt", "w") as se:
            proc = subprocess.Popen(
                _argv(args), stdin=slave, stdout=so, stderr=se, env=env,
                cwd=str(tmp_path / "proj"), close_fds=True,
            )
            os.close(slave)
            slave = -1
            if input_text:
                os.write(master, input_text.encode())
            res = _finish(proc, tmp_path, tmp_path / "cap")
    finally:
        if slave != -1:
            os.close(slave)
        os.close(master)
    assert not res.timed_out, f"blocked on stdin; stderr={res.err}"
    res.rt_dir = rt_dir
    return res


def _kdl(cap):
    text = (cap / "layout.kdl").read_text()
    tabs = re.findall(r'tab name="([^"]+)"', text)
    main = text.split('tab name="main"', 1)[1].split("tab name=", 1)[0]
    # Floating panes (Token Spend) are not part of the tiled main panes.
    main = main.split("floating_panes {", 1)[0]
    panes = re.findall(r'pane name="([^"]+)"', main)
    return text, tabs, panes


def _floating(cap):
    text = (cap / "layout.kdl").read_text()
    main = text.split('tab name="main"', 1)[1].split("tab name=", 1)[0]
    if "floating_panes {" not in main:
        return []
    floating = main.split("floating_panes {", 1)[1]
    return re.findall(r'pane name="([^"]+)"', floating)


def _assert_created(res, session):
    flat = [a for line in res.argv_lines for a in line.split("\t")]
    assert "--new-session-with-layout" in flat, f"no create call; stderr={res.err}"
    i = flat.index("--session")
    assert flat[i + 1] == session, f"session {flat[i + 1]!r} != {session!r}; stderr={res.err}"
    assert (res.cap / "layout.kdl").exists()
    rt = Path(res.rt_dir)
    assert any(p.name.startswith("zellij-agentdesk") for p in rt.iterdir()), (
        "socket dir was not created in the private runtime dir"
    )


def _flat_argv(res):
    return [a for line in res.argv_lines for a in line.split("\t")]


def _state_file(tmp_path):
    return tmp_path / "home" / ".config" / "agentdesk" / "last-layout"


NAMED = ["--name", "deck-1", "--panes", "codex,codex,shell", "--no-prompt"]
PROMPTS = (DASHBOARD_PROMPT, PICKER_PROMPT, COLLISION_PROMPT, REUSE_NOTICE)


def test_hermetic_socket_dir_within_length_guard(rt_dir):
    sock = Path(rt_dir) / "zellij-agentdesk-UID"
    assert len(str(sock)) + len("deck-1_1") + 20 <= 100


def test_api_version_marker_static_and_sourced(tmp_path, rt_dir):
    lines = [l for l in AGENTDESK_ZSH.read_text().splitlines() if re.match(r"^AGENTDESK_API_VERSION=", l)]
    assert lines == ["AGENTDESK_API_VERSION=1"]
    env = _env(tmp_path, rt_dir)
    out = subprocess.run(
        ["zsh", "-c", 'source "$0"; print $AGENTDESK_API_VERSION', str(AGENTDESK_ZSH)],
        env=env, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
    )
    assert out.stdout.strip() == "1"


def test_help_documents_public_interface(tmp_path, rt_dir):
    env = _env(tmp_path, rt_dir)
    res = _run_pipe(["--help"], env, tmp_path, rt_dir)
    assert res.rc == 0
    for s in ("--panes", "--no-prompt", "--if-exists", "attach|new|fail",
              "AGENTDESK_API_VERSION", "--mode solo|duo|trio", "--pick",
              "AGENTDESK_PICK", "prompts to attach", "Non-interactive",
              "--dashboard", "--no-dashboard", "--ask-dashboard",
              "AGENTDESK_DASHBOARD=1|0", "--clean-state"):
        assert s in res.out, s


def _assert_named_layout(res):
    assert res.rc == 0, res.text
    _assert_created(res, "deck-1")
    text, tabs, panes = _kdl(res.cap)
    assert panes == ["Codex · A", "Codex · B", "Shell"]
    assert 'split_direction="vertical"' in text
    assert tabs == ["main"]
    assert _floating(res.cap) == ["Token Spend"]
    # Stream Deck finds agent panes by a case-insensitive substring of the title.
    assert [p for p in panes if "codex" in p.lower()] == ["Codex · A", "Codex · B"]
    for p in PROMPTS:
        assert p not in res.text


def test_panes_named_no_tty_layout_and_no_prompt(tmp_path, rt_dir):
    res = _run_pipe(NAMED, _env(tmp_path, rt_dir), tmp_path, rt_dir)
    _assert_named_layout(res)


def test_panes_named_pty_layout_and_no_prompt(tmp_path, rt_dir):
    res = _run_pty(NAMED, _env(tmp_path, rt_dir), tmp_path, rt_dir)
    _assert_named_layout(res)


def test_panes_plus_separator_equivalent(tmp_path, rt_dir):
    res = _run_pipe(["--name", "deck-1", "--panes", "codex+codex+shell"],
                    _env(tmp_path, rt_dir), tmp_path, rt_dir)
    assert res.rc == 0, res.text
    _assert_created(res, "deck-1")
    assert _kdl(res.cap)[2] == ["Codex · A", "Codex · B", "Shell"]


def test_panes_does_not_create_or_change_last_layout(tmp_path, rt_dir):
    env = _env(tmp_path, rt_dir)
    state = _state_file(tmp_path)
    assert not state.exists()
    res = _run_pipe(["--name", "deck-1", "--panes", "codex,shell"], env, tmp_path, rt_dir)
    assert res.rc == 0, res.text
    assert not state.exists()

    state.parent.mkdir(parents=True, exist_ok=True)
    seeded = "name:deck-1=claude\nproj:/some/where=codex\n"
    state.write_text(seeded)
    before = state.read_bytes()
    res = _run_pipe(["--name", "deck-1", "--panes", "codex,shell"], env, tmp_path, rt_dir)
    assert res.rc == 0, res.text
    assert state.read_bytes() == before


def test_panes_ignores_saved_layout_for_same_name(tmp_path, rt_dir):
    env = _env(tmp_path, rt_dir)
    state = _state_file(tmp_path)
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text("name:deck-1=claude\n")
    res = _run_pipe(["--name", "deck-1", "--panes", "codex,shell"], env, tmp_path, rt_dir)
    assert res.rc == 0, res.text
    assert _kdl(res.cap)[2] == ["Codex", "Shell"]


def test_panes_spend_token_deduplicated(tmp_path, rt_dir):
    res = _run_pipe(["--name", "deck-1", "--panes", "codex,spend"],
                    _env(tmp_path, rt_dir), tmp_path, rt_dir)
    assert res.rc == 0, res.text
    text, tabs, panes = _kdl(res.cap)
    assert "Spend" not in tabs
    assert _floating(res.cap) == ["Token Spend"]
    assert panes == ["Codex"]


ERROR_CASES = [
    pytest.param(["--panes"], "--panes requires a value", id="panes-no-value"),
    pytest.param(["--panes", ""], "empty entry", id="panes-empty"),
    pytest.param(["--panes", "codex,,shell"], "empty entry", id="panes-double-comma"),
    pytest.param(["--panes", "codex,"], "empty entry", id="panes-trailing-comma"),
    pytest.param(["--panes", "spend"], "other than spend", id="spend-only"),
    pytest.param(["--panes", "spend,spend"], "other than spend", id="spend-twice"),
    pytest.param(["--panes", "vim"], 'unknown pane "vim"', id="unknown-pane"),
    pytest.param(["--panes", "dashboard"], 'unknown pane "dashboard"', id="internal-dashboard"),
    pytest.param(["--panes", "codex", "--mode", "duo"], "cannot be combined with --mode", id="panes-mode"),
    pytest.param(["--panes", "codex", "claude"], "positional window-type tokens", id="panes-tokens"),
    pytest.param(["--panes", "codex", "--pick"], "cannot be combined with --pick", id="panes-pick"),
    pytest.param(["--no-prompt", "--pick"], "cannot be combined with --no-prompt", id="noprompt-pick"),
    pytest.param(["--if-exists"], "--if-exists requires a value", id="ifexists-no-value"),
    pytest.param(["--if-exists", "maybe"], 'invalid --if-exists value "maybe"', id="ifexists-bad"),
]


@pytest.mark.parametrize("args,fragment", ERROR_CASES)
def test_public_flag_errors(tmp_path, rt_dir, args, fragment):
    res = _run_pipe(args, _env(tmp_path, rt_dir), tmp_path, rt_dir)
    assert res.rc == 1, res.text
    assert fragment in res.err
    assert not (tmp_path / "cap" / "argv.log").exists()
    assert not (tmp_path / "proj" / ".workflow_artifacts").exists()


@pytest.mark.parametrize("runner", ["pipe", "pty"])
@pytest.mark.parametrize("policy", ["attach", "new", "fail"])
def test_if_exists_matrix(tmp_path, rt_dir, policy, runner):
    env = _env(tmp_path, rt_dir, sessions=["deck-1"])
    run = _run_pipe if runner == "pipe" else _run_pty
    res = run(NAMED + ["--if-exists", policy], env, tmp_path, rt_dir)
    flat = _flat_argv(res)
    if policy == "attach":
        assert res.rc == 0, res.text
        assert (tmp_path / "cap" / "attach.txt").read_text() == "deck-1"
        assert "--new-session-with-layout" not in flat
    elif policy == "new":
        assert res.rc == 0, res.text
        _assert_created(res, "deck-1_1")
        assert not (tmp_path / "cap" / "attach.txt").exists()
    else:
        assert res.rc == 1
        assert "already exists (--if-exists fail)" in res.err
        assert not (tmp_path / "cap" / "attach.txt").exists()
        assert "--new-session-with-layout" not in flat
    for p in PROMPTS:
        assert p not in res.text


@pytest.mark.parametrize("runner", ["pipe", "pty"])
def test_no_prompt_default_policy_is_attach(tmp_path, rt_dir, runner):
    env = _env(tmp_path, rt_dir, sessions=["deck-1"])
    run = _run_pipe if runner == "pipe" else _run_pty
    res = run(["--name", "deck-1", "--panes", "codex", "--no-prompt"], env, tmp_path, rt_dir)
    assert res.rc == 0, res.text
    assert (tmp_path / "cap" / "attach.txt").read_text() == "deck-1"


def test_if_exists_without_collision_is_noop(tmp_path, rt_dir):
    res = _run_pipe(["--name", "deck-1", "--panes", "codex", "--if-exists", "fail"],
                    _env(tmp_path, rt_dir), tmp_path, rt_dir)
    assert res.rc == 0, res.text
    _assert_created(res, "deck-1")


def test_if_exists_on_tty_suppresses_collision_prompt_only(tmp_path, rt_dir):
    env = _env(tmp_path, rt_dir, sessions=["deck-1"])
    res = _run_pty(["--name", "deck-1", "--panes", "codex,codex,shell", "--if-exists", "new"],
                   env, tmp_path, rt_dir, input_text="\n")
    assert res.rc == 0, res.text
    assert DASHBOARD_PROMPT in res.text
    assert COLLISION_PROMPT not in res.text
    _assert_created(res, "deck-1_1")


def test_no_prompt_without_panes_uses_fixed_layout_on_tty(tmp_path, rt_dir):
    env = _env(tmp_path, rt_dir)
    res = _run_pty(["--name", "deck-1", "--no-prompt"], env, tmp_path, rt_dir)
    assert res.rc == 0, res.text
    _assert_created(res, "deck-1")
    expected = str(tmp_path / "home" / ".config" / "zellij" / "layouts" / "agent-desk.kdl")
    assert (tmp_path / "cap" / "layout_path.txt").read_text() == expected
    assert PICKER_PROMPT not in res.text


def test_no_prompt_without_panes_reuses_saved_layout_on_tty(tmp_path, rt_dir):
    env = _env(tmp_path, rt_dir)
    state = _state_file(tmp_path)
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text("name:deck-1=claude+shell\n")
    res = _run_pty(["--name", "deck-1", "--no-prompt"], env, tmp_path, rt_dir)
    assert res.rc == 0, res.text
    assert _kdl(res.cap)[2] == ["Claude Code", "Shell"]


def test_no_prompt_ignores_agentdesk_pick_env(tmp_path, rt_dir):
    env = _env(tmp_path, rt_dir, extra={"AGENTDESK_PICK": "1"})
    res = _run_pty(["--name", "deck-1", "--no-prompt"], env, tmp_path, rt_dir)
    assert res.rc == 0, res.text
    assert PICKER_PROMPT not in res.text


def test_no_prompt_ignores_inherited_dashboard_env(tmp_path, rt_dir):
    url_file = tmp_path / "tmpdir" / "dash-url"
    env = _env(tmp_path, rt_dir, extra={
        "AGENTDESK_DASHBOARD": "1",
        "AGENTDESK_DASHBOARD_URL_FILE": str(url_file),
    })
    res = _run_pipe(["--name", "deck-1", "--no-prompt", "--panes", "shell"], env, tmp_path, rt_dir)
    assert res.rc == 0, res.text
    text, tabs, _ = _kdl(res.cap)
    assert "Dashboard" not in tabs
    time.sleep(1)
    assert not (tmp_path / "cap" / "open.log").exists()


def test_legacy_positional_name_form(tmp_path, rt_dir):
    res = _run_pipe(["deck-2"], _env(tmp_path, rt_dir), tmp_path, rt_dir)
    assert res.rc == 0, res.text
    _assert_created(res, "deck-2")
    expected = str(tmp_path / "home" / ".config" / "zellij" / "layouts" / "agent-desk.kdl")
    assert (tmp_path / "cap" / "layout_path.txt").read_text() == expected


def test_legacy_agentdesk_pick_env_shows_picker(tmp_path, rt_dir):
    env = _env(tmp_path, rt_dir, extra={"AGENTDESK_PICK": "1"})
    res = _run_pty(["--name", "deck-1"], env, tmp_path, rt_dir, input_text="\n1\n")
    assert PICKER_PROMPT in res.text, res.text


def test_legacy_non_tty_collision_still_suffixes(tmp_path, rt_dir):
    env = _env(tmp_path, rt_dir, sessions=["deck-1"])
    res = _run_pipe(["--name", "deck-1"], env, tmp_path, rt_dir)
    assert res.rc == 0, res.text
    assert "Session 'deck-1' already exists; starting new session 'deck-1_1' instead." in res.out
    assert "(To resume the existing session: agentdesk-attach deck-1)" in res.out
    _assert_created(res, "deck-1_1")


def test_legacy_name_plus_tokens_still_errors(tmp_path, rt_dir):
    res = _run_pipe(["--name", "x", "claude"], _env(tmp_path, rt_dir), tmp_path, rt_dir)
    assert res.rc == 1
    assert "cannot mix session name with window-type tokens" in res.err


def test_legacy_mode_still_persists_layout(tmp_path, rt_dir):
    res = _run_pipe(["--mode", "duo", "--name", "m"], _env(tmp_path, rt_dir), tmp_path, rt_dir)
    assert res.rc == 0, res.text
    assert "name:m=claude+shell" in _state_file(tmp_path).read_text()


# ── dashboard memory, overrides and no shell exports ─────────────────────────

def _seed(tmp_path, *lines):
    state = _state_file(tmp_path)
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text("".join(f"{l}\n" for l in lines))


def _state_lines(tmp_path):
    state = _state_file(tmp_path)
    return state.read_text().splitlines() if state.exists() else []


def _tab_names(cap, n=None):
    path = cap / ("layout.kdl" if n is None else f"layout-{n}.kdl")
    return re.findall(r'tab name="([^"]+)"', path.read_text())


def _layout_count(cap):
    return len(list(cap.glob("layout-*.kdl")))


def _launch(tmp_path, rt_dir, args, input_text="", pty_mode=True, extra=None,
            with_server=True):
    env = _env(tmp_path, rt_dir, extra=extra, with_server=with_server)
    run = _run_pty if pty_mode else _run_pipe
    if pty_mode:
        return run(args, env, tmp_path, rt_dir, input_text=input_text)
    return run(args, env, tmp_path, rt_dir)


def _quiet(res):
    assert DASHBOARD_PROMPT not in res.text, res.text
    assert PICKER_PROMPT not in res.text, res.text


@pytest.mark.parametrize("answer,stored,expect_tab", [("n", "no", False), ("y", "yes", True)])
def test_dashboard_answer_is_asked_once(tmp_path, rt_dir, answer, stored, expect_tab):
    first = _launch(tmp_path, rt_dir, ["--name", "deck-1"], f"{answer}\n\n")
    assert first.rc == 0, first.text
    assert DASHBOARD_PROMPT in first.text
    assert f"dash:name:deck-1={stored}" in _state_lines(tmp_path)
    assert "name:deck-1=__FIXED__" in _state_lines(tmp_path)
    assert _layout_count(first.cap) == 1
    assert ("Dashboard" in _tab_names(first.cap)) is expect_tab

    second = _launch(tmp_path, rt_dir, ["--name", "deck-1"], "")
    assert second.rc == 0, second.text
    _quiet(second)
    assert _layout_count(second.cap) == 2
    assert ("Dashboard" in _tab_names(second.cap)) is expect_tab


@pytest.mark.parametrize("stored,env_value,expect_tab", [
    ("no", "1", True), ("yes", "0", False),
])
def test_dashboard_env_overrides_stored_answer(tmp_path, rt_dir, stored, env_value, expect_tab):
    _seed(tmp_path, "name:deck-1=__FIXED__", f"dash:name:deck-1={stored}")
    res = _launch(tmp_path, rt_dir, ["--name", "deck-1"],
                  extra={"AGENTDESK_DASHBOARD": env_value})
    assert res.rc == 0, res.text
    _quiet(res)
    assert ("Dashboard" in _tab_names(res.cap)) is expect_tab
    assert f"dash:name:deck-1={stored}" in _state_lines(tmp_path)


@pytest.mark.parametrize("stored,flag,expect_tab", [
    ("yes", "--no-dashboard", False), ("no", "--dashboard", True),
])
def test_dashboard_flag_overrides_stored_answer(tmp_path, rt_dir, stored, flag, expect_tab):
    _seed(tmp_path, "name:deck-1=__FIXED__", f"dash:name:deck-1={stored}")
    res = _launch(tmp_path, rt_dir, ["--name", "deck-1", flag])
    assert res.rc == 0, res.text
    _quiet(res)
    assert ("Dashboard" in _tab_names(res.cap)) is expect_tab
    assert f"dash:name:deck-1={stored}" in _state_lines(tmp_path)


def test_no_prompt_ignores_stored_yes_and_dashboard_flag_adds_tab(tmp_path, rt_dir):
    _seed(tmp_path, "name:deck-1=__FIXED__", "dash:name:deck-1=yes")
    res = _launch(tmp_path, rt_dir, ["--name", "deck-1", "--no-prompt"])
    assert res.rc == 0, res.text
    _quiet(res)
    assert "Dashboard" not in _tab_names(res.cap)
    res = _launch(tmp_path, rt_dir, ["--name", "deck-1", "--no-prompt", "--dashboard"])
    assert res.rc == 0, res.text
    _quiet(res)
    assert "Dashboard" in _tab_names(res.cap)


def test_panes_launch_asks_but_never_stores_answer(tmp_path, rt_dir):
    res = _launch(tmp_path, rt_dir, ["--name", "deck-1", "--panes", "shell"], "y\n")
    assert res.rc == 0, res.text
    assert DASHBOARD_PROMPT in res.text
    assert not [l for l in _state_lines(tmp_path) if l.startswith("dash:")]


def test_ask_dashboard_with_panes_asks_but_never_stores(tmp_path, rt_dir):
    _seed(tmp_path, "dash:name:deck-1=no")
    res = _launch(
        tmp_path, rt_dir, ["--name", "deck-1", "--panes", "shell", "--ask-dashboard"], "y\n"
    )
    assert res.rc == 0, res.text
    assert DASHBOARD_PROMPT in res.text
    assert "Dashboard" in _tab_names(res.cap)
    assert _state_lines(tmp_path) == ["dash:name:deck-1=no"]


def test_help_says_panes_answer_not_remembered(tmp_path, rt_dir):
    res = _run_pipe(["--help"], _env(tmp_path, rt_dir), tmp_path, rt_dir)
    assert re.search(r"--panes the question is still asked.*\n?.*not remembered", res.text)


def _dash_url_files(tmp_path):
    return list((tmp_path / "tmpdir").glob("agentdesk-dash-url-*"))


def test_dashboard_yes_with_failed_launch_leaves_no_url_file(tmp_path, rt_dir):
    # Over-long session name trips the socket-length guard after the dashboard
    # answer is yes.
    res = _launch(tmp_path, rt_dir, ["--name", "x" * 120, "--dashboard"], "\n")
    assert res.rc != 0, res.text
    assert "too long" in res.text
    assert _dash_url_files(tmp_path) == []


def test_dashboard_yes_with_cancelled_picker_leaves_no_url_file(tmp_path, rt_dir):
    res = _launch(tmp_path, rt_dir, ["--name", "deck-1", "--pick", "--dashboard"], "7\nbogus\n\n")
    assert res.rc != 0 and "cancelled" in res.text, res.text
    assert _dash_url_files(tmp_path) == []


def test_dashboard_yes_with_missing_layout_file_leaves_no_url_file(tmp_path, rt_dir):
    _seed(tmp_path, "name:deck-1=__FIXED__")
    env = _env(tmp_path, rt_dir, with_server=True)
    (tmp_path / "home" / ".config" / "zellij" / "layouts" / "agent-desk.kdl").unlink()
    # A missing file would normally be regenerated, so force only the
    # generator to fail; TMPDIR stays valid so the URL-file check is real.
    argv = ["zsh", "-c",
            'source "$0"; shift; _agentdesk_gen_standard_layout() { return 1; }; agentdesk "$@"',
            str(AGENTDESK_ZSH), "_", "--name", "deck-1", "--dashboard"]
    with open(tmp_path / "stdout.txt", "w") as so, open(tmp_path / "stderr.txt", "w") as se:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=so, stderr=se, env=env,
                                cwd=str(tmp_path / "proj"))
        res = _finish(proc, tmp_path, tmp_path / "cap")
        proc.stdin.close()
    assert not res.timed_out, res.err
    assert res.rc != 0 and "Zellij layout not found" in res.text, res.text
    assert _dash_url_files(tmp_path) == []


def test_ask_dashboard_asks_again_and_stores(tmp_path, rt_dir):
    _seed(tmp_path, "name:deck-1=__FIXED__", "dash:name:deck-1=no")
    res = _launch(tmp_path, rt_dir, ["--name", "deck-1", "--ask-dashboard"], "y\n")
    assert res.rc == 0, res.text
    assert DASHBOARD_PROMPT in res.text
    assert "dash:name:deck-1=yes" in _state_lines(tmp_path)
    assert "Dashboard" in _tab_names(res.cap)


def test_pick_re_asks_dashboard_and_stores(tmp_path, rt_dir):
    _seed(tmp_path, "name:deck-1=__FIXED__", "dash:name:deck-1=no")
    res = _launch(tmp_path, rt_dir, ["--name", "deck-1", "--pick"], "y\n\n")
    assert res.rc == 0, res.text
    assert DASHBOARD_PROMPT in res.text and PICKER_PROMPT in res.text
    assert "dash:name:deck-1=yes" in _state_lines(tmp_path)


def test_stale_dashboard_env_from_old_shell_is_ignored(tmp_path, rt_dir):
    _seed(tmp_path, "name:deck-1=__FIXED__", "dash:name:deck-1=no")
    res = _launch(tmp_path, rt_dir, ["--name", "deck-1"], extra={
        "AGENTDESK_DASHBOARD": "1", "AGENTDESK_DASHBOARD_URL_FILE": "/stale",
    })
    assert res.rc == 0, res.text
    _quiet(res)
    assert "Dashboard" not in _tab_names(res.cap)
    assert (res.cap / "dash_env.log").read_text().splitlines() == ["[]"]


def test_non_tty_stored_yes_does_not_ask_or_add_tab(tmp_path, rt_dir):
    _seed(tmp_path, "dash:name:deck-1=yes")
    res = _launch(tmp_path, rt_dir, ["--name", "deck-1"], pty_mode=False)
    assert res.rc == 0, res.text
    _quiet(res)
    assert "Dashboard" not in _tab_names(res.cap)


def test_non_tty_dashboard_flag_needs_server(tmp_path, rt_dir):
    res = _launch(tmp_path, rt_dir, ["--name", "deck-1", "--dashboard"], pty_mode=False)
    assert res.rc == 0, res.text
    assert "Dashboard" in _tab_names(res.cap)
    other = tmp_path / "second"
    other.mkdir()
    (other / "proj").mkdir()
    res = _launch(other, rt_dir, ["--name", "deck-1", "--dashboard"], pty_mode=False,
                  with_server=False)
    assert res.rc == 0, res.text
    assert "not found" in res.err
    assert "Dashboard" not in _tab_names(res.cap)


def test_server_missing_still_stores_yes_answer(tmp_path, rt_dir):
    res = _launch(tmp_path, rt_dir, ["--name", "deck-1"], "y\n\n", with_server=False)
    assert res.rc == 0, res.text
    assert "not found" in res.err
    assert "Dashboard" not in _tab_names(res.cap)
    assert "dash:name:deck-1=yes" in _state_lines(tmp_path)


def _resolve_env(tmp_path, rt_dir, extra):
    return _env(tmp_path, rt_dir, extra=extra)


@pytest.mark.parametrize("url_file,expected", [("", "yes env"), ("/something", "no default")])
def test_resolver_treats_empty_url_file_as_unset(tmp_path, rt_dir, url_file, expected):
    env = _resolve_env(tmp_path, rt_dir, {
        "AGENTDESK_DASHBOARD": "1", "AGENTDESK_DASHBOARD_URL_FILE": url_file,
    })
    out = subprocess.run(
        ["zsh", "-c", 'source "$0"; _agentdesk_dashboard_resolve "" "" "" "" "" dash:x',
         str(AGENTDESK_ZSH)],
        env=env, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
        stdin=subprocess.DEVNULL,
    )
    assert out.stdout.strip() == expected, out.stderr


@pytest.mark.parametrize("args", [
    ["--dashboard", "--no-dashboard"],
    ["--ask-dashboard", "--no-prompt"],
    ["--ask-dashboard", "--dashboard"],
])
def test_dashboard_flag_conflicts(tmp_path, rt_dir, args):
    res = _launch(tmp_path, rt_dir, args, pty_mode=False)
    assert res.rc == 1, res.text
    assert "Error" in res.err
    assert res.argv_lines == []


def _two_launches(tmp_path, rt_dir, extra=None):
    env = _env(tmp_path, rt_dir, extra=extra, with_server=True)
    script = (
        'source "$0"\n'
        'agentdesk --name a --panes shell --dashboard\n'
        'agentdesk --name b --panes shell\n'
        "typeset -m 'AGENTDESK_DASHBOARD*'\n"
    )
    out = subprocess.run(
        ["zsh", "-c", script, str(AGENTDESK_ZSH)], env=env, capture_output=True,
        text=True, cwd=str(tmp_path / "proj"), timeout=SUBPROCESS_TIMEOUT,
        stdin=subprocess.DEVNULL,
    )
    assert out.returncode == 0, out.stderr
    leaked = [l for l in out.stdout.splitlines()
              if re.match(r"^(export )?AGENTDESK_DASHBOARD", l)]
    return tmp_path / "cap", leaked


def test_dashboard_flag_does_not_leak_into_shell(tmp_path, rt_dir):
    cap, leaked = _two_launches(tmp_path, rt_dir)
    assert "Dashboard" in _tab_names(cap, 1)
    assert "Dashboard" not in _tab_names(cap, 2)
    assert leaked == []


def test_user_env_dashboard_is_left_untouched(tmp_path, rt_dir):
    cap, leaked = _two_launches(tmp_path, rt_dir, extra={"AGENTDESK_DASHBOARD": "1"})
    assert "Dashboard" in _tab_names(cap, 1)
    assert "Dashboard" in _tab_names(cap, 2)
    # Only the user's own variable remains; the URL-file variable stays unset.
    assert [l.removeprefix("export ") for l in leaked] == ["AGENTDESK_DASHBOARD=1"]


# ── generated layout: launch call, bar mode, dashboard tab ─────────────────────

LAYOUT_ARGS = ["--name", "deck-1", "--panes", "claude,shell", "--no-prompt"]


def _main_tiled(cap):
    text = (cap / "layout.kdl").read_text()
    return text.split('tab name="main"', 1)[1].split("floating_panes {", 1)[0]


def test_create_call_turns_auto_layout_off(tmp_path, rt_dir):
    res = _run_pipe(NAMED, _env(tmp_path, rt_dir), tmp_path, rt_dir)
    flat = _flat_argv(res)
    i = flat.index("--session")
    assert flat[i + 1] == "deck-1"
    assert flat[i + 2 : i + 5] == ["options", "--auto-layout", "false"]


def test_attach_call_has_no_options(tmp_path, rt_dir):
    env = _env(tmp_path, rt_dir, sessions=["deck-1"])
    res = _run_pty(NAMED + ["--if-exists", "attach"], env, tmp_path, rt_dir)
    assert res.rc == 0, res.text
    flat = _flat_argv(res)
    assert "--auto-layout" not in flat and "options" not in flat
    assert (res.cap / "attach.txt").read_text() == "deck-1"


def test_stream_deck_invocation_creates_the_documented_layout(tmp_path, rt_dir):
    args = ["--name", "deck-1", "--panes", "codex,codex,shell", "--no-prompt",
            "--if-exists", "attach"]
    res = _run_pipe(args, _env(tmp_path, rt_dir), tmp_path, rt_dir)
    _assert_named_layout(res)


@pytest.mark.parametrize("mode,names,sized", [
    ("solo", ["Claude Code"], False),
    ("duo", ["Claude Code", "Shell"], True),
    ("trio", ["Claude Code", "Codex", "Shell"], True),
])
def test_modes_produce_the_documented_shapes(tmp_path, rt_dir, mode, names, sized):
    res = _run_pipe(["--name", "deck-1", "--mode", mode, "--no-prompt"],
                    _env(tmp_path, rt_dir), tmp_path, rt_dir)
    assert res.rc == 0, res.text
    assert _kdl(res.cap)[2] == names
    tiled = _main_tiled(res.cap)
    assert ('size="' in tiled) is sized
    if mode == "trio":
        assert 'size="75%" split_direction="vertical"' in tiled
        assert 'pane name="Shell" size="25%"' in tiled


@pytest.mark.parametrize("extra,args,compact", [
    (None, LAYOUT_ARGS + ["--compact-bar"], True),
    ({"AGENTDESK_COMPACT_BAR": "1"}, LAYOUT_ARGS, True),
    ({"AGENTDESK_COMPACT_BAR": "yes"}, LAYOUT_ARGS, False),
    ({"AGENTDESK_COMPACT_BAR": "0"}, LAYOUT_ARGS, False),
    (None, LAYOUT_ARGS, False),
])
def test_compact_bar_opt_in(tmp_path, rt_dir, extra, args, compact):
    res = _run_pipe(args, _env(tmp_path, rt_dir, extra=extra), tmp_path, rt_dir)
    assert res.rc == 0, res.text
    text = (res.cap / "layout.kdl").read_text()
    if compact:
        assert "zellij:compact-bar" in text and "zellij:status-bar" not in text
    else:
        assert "zellij:status-bar" in text and "compact-bar" not in text


def test_compact_bar_is_not_remembered(tmp_path, rt_dir):
    res = _run_pty(["--name", "deck-1", "--mode", "duo", "--compact-bar"],
                   _env(tmp_path, rt_dir), tmp_path, rt_dir, input_text="n\n")
    assert res.rc == 0, res.text
    state = _state_file(tmp_path)
    assert state.exists()
    assert "compact" not in state.read_text().lower()
    again = _run_pty(["--name", "deck-1"], _env(tmp_path, rt_dir), tmp_path, rt_dir)
    assert "zellij:status-bar" in (again.cap / "layout.kdl").read_text()


def test_clean_state_conflicts_with_compact_bar(tmp_path, rt_dir):
    res = _run_pipe(["--clean-state", "--compact-bar"], _env(tmp_path, rt_dir),
                    tmp_path, rt_dir)
    assert res.rc == 1
    assert "--clean-state cannot be combined with --compact-bar" in res.err


def test_inherited_compact_bar_env_does_not_reach_launches(tmp_path, rt_dir, monkeypatch):
    monkeypatch.setenv("AGENTDESK_COMPACT_BAR", "1")
    res = _run_pipe(LAYOUT_ARGS, _env(tmp_path, rt_dir), tmp_path, rt_dir)
    text = (res.cap / "layout.kdl").read_text()
    assert "zellij:status-bar" in text and "compact-bar" not in text
    explicit = _run_pipe(LAYOUT_ARGS, _env(tmp_path, rt_dir,
                         extra={"AGENTDESK_COMPACT_BAR": "1"}), tmp_path, rt_dir)
    assert "zellij:compact-bar" in (explicit.cap / "layout.kdl").read_text()


def test_help_describes_the_layout(tmp_path, rt_dir):
    res = _run_pipe(["--help"], _env(tmp_path, rt_dir), tmp_path, rt_dir)
    for s in ("--compact-bar", "AGENTDESK_COMPACT_BAR", "Alt [", "floating",
              "Alt f, or pane mode then w"):
        assert s in res.out, s
    assert "The Spend tab is still added" not in res.out


def _dashboard_tab_text(text):
    return text.split('tab name="Dashboard"', 1)[1]


@pytest.mark.parametrize("flag,bar", [(None, "zellij:status-bar"), ("--compact-bar", "zellij:compact-bar")])
def test_dashboard_tab_on_a_generated_layout_has_bars(tmp_path, rt_dir, flag, bar):
    args = LAYOUT_ARGS + ["--dashboard"] + ([flag] if flag else [])
    res = _run_pipe(args, _env(tmp_path, rt_dir, with_server=True), tmp_path, rt_dir)
    assert res.rc == 0, res.text
    text = (res.cap / "layout.kdl").read_text()
    assert text.rstrip("\n").split("\n")[-1] == "}"
    tab = _dashboard_tab_text(text)
    assert bar in tab
    if flag is None:
        assert "zellij:tab-bar" in tab
    assert "dashboard_server.py" in tab
    zellij = shutil.which("zellij")
    if zellij:
        env = _env(tmp_path, rt_dir)
        env["PATH"] = f"{Path(zellij).parent}:/usr/bin:/bin"
        done = subprocess.run(
            [zellij, "--layout", str(res.cap / "layout.kdl"), "setup", "--check"],
            env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60,
        )
        assert done.returncode == 0, done.stderr + done.stdout


# ── Standard layout resolution at launch ───────────────────────────────────────

STANDARD_TABS = ["main", "git", "status"]


def _fixed_layout_file(tmp_path):
    return tmp_path / "home" / ".config" / "zellij" / "layouts" / "agent-desk.kdl"


def _generated_standard_text(tmp_path, rt_dir, bars="default"):
    env = _env(tmp_path, rt_dir)
    done = subprocess.run(
        ["zsh", "-c",
         f'source "{AGENTDESK_ZSH}"; _agentdesk_gen_standard_layout --stdout --bars {bars}'],
        env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


def _captured_layout_path(res):
    return Path((res.cap / "layout_path.txt").read_text())


def test_missing_layout_file_is_regenerated_and_cleaned_up(tmp_path, rt_dir):
    env = _env(tmp_path, rt_dir)
    _fixed_layout_file(tmp_path).unlink()
    res = _run_pipe(["--name", "deck-1", "--no-prompt"], env, tmp_path, rt_dir)
    assert res.rc == 0, res.text
    assert _tab_names(res.cap) == STANDARD_TABS
    used = _captured_layout_path(res)
    assert used != _fixed_layout_file(tmp_path)
    assert not used.exists(), "temporary layout was not removed on exit"


def test_generated_layout_file_is_replaced_by_a_fresh_render(tmp_path, rt_dir):
    env = _env(tmp_path, rt_dir)
    _fixed_layout_file(tmp_path).write_text(_generated_standard_text(tmp_path, rt_dir))
    res = _run_pipe(["--name", "deck-1", "--no-prompt"], env, tmp_path, rt_dir)
    assert res.rc == 0, res.text
    assert _captured_layout_path(res) != _fixed_layout_file(tmp_path)
    assert _tab_names(res.cap) == STANDARD_TABS


def test_compact_bar_applies_to_the_generated_standard_layout(tmp_path, rt_dir):
    env = _env(tmp_path, rt_dir)
    _fixed_layout_file(tmp_path).write_text(_generated_standard_text(tmp_path, rt_dir))
    res = _run_pipe(["--name", "deck-1", "--no-prompt", "--compact-bar"], env, tmp_path, rt_dir)
    assert res.rc == 0, res.text
    text = (res.cap / "layout.kdl").read_text()
    assert "zellij:compact-bar" in text and "zellij:status-bar" not in text


def test_stored_fixed_value_reuses_the_generated_standard(tmp_path, rt_dir):
    env = _env(tmp_path, rt_dir)
    _fixed_layout_file(tmp_path).unlink()
    _seed(tmp_path, "name:deck-1=__FIXED__")
    res = _run_pipe(["--name", "deck-1"], env, tmp_path, rt_dir)
    assert res.rc == 0, res.text
    assert REUSE_NOTICE in res.text
    assert _tab_names(res.cap) == STANDARD_TABS
    assert not _captured_layout_path(res).exists()


def test_non_tty_pick_uses_the_generated_standard(tmp_path, rt_dir):
    env = _env(tmp_path, rt_dir)
    _fixed_layout_file(tmp_path).unlink()
    res = _run_pipe(["--name", "deck-1", "--pick"], env, tmp_path, rt_dir)
    assert res.rc == 0, res.text
    assert _tab_names(res.cap) == STANDARD_TABS
    assert "name:deck-1=__FIXED__" in _state_lines(tmp_path)


def test_customised_layout_file_is_used_untouched(tmp_path, rt_dir):
    res = _run_pipe(["--name", "deck-1", "--no-prompt"], _env(tmp_path, rt_dir), tmp_path, rt_dir)
    assert res.rc == 0, res.text
    assert _captured_layout_path(res) == _fixed_layout_file(tmp_path)
    assert _fixed_layout_file(tmp_path).read_text() == "layout {\n}\n"
