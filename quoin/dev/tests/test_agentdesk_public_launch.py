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
    printf '%s' "$a" > "$cap/layout_path.txt"
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


def _env(tmp_path, rt_dir, sessions=(), extra=None):
    home = tmp_path / "home"
    (home / ".config" / "zellij" / "layouts").mkdir(parents=True, exist_ok=True)
    (home / ".config" / "zellij" / "layouts" / "agent-desk.kdl").write_text(
        "layout {\n}\n"
    )
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
    panes = re.findall(r'pane name="([^"]+)"', main)
    return text, tabs, panes


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
              "AGENTDESK_PICK", "prompts to attach", "Non-interactive"):
        assert s in res.out, s


def _assert_named_layout(res):
    assert res.rc == 0, res.text
    _assert_created(res, "deck-1")
    text, tabs, panes = _kdl(res.cap)
    assert panes == ["Codex", "Codex", "Shell"]
    assert 'split_direction="vertical"' in text
    assert tabs == ["main", "Spend"]
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
    assert _kdl(res.cap)[2] == ["Codex", "Codex", "Shell"]


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
    assert tabs.count("Spend") == 1
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
