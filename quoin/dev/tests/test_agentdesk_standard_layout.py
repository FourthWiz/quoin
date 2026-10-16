"""Tests for the Standard layout: generated agents tab plus git and status tabs.

Covers the git and status pane commands, the generated layout and its marker,
resolution of the saved layout file (a customised file always wins), and how
setup writes the layout. Every environment is isolated: HOME, XDG_CONFIG_HOME,
TMPDIR and ZELLIJ_SOCKET_DIR live under the test's tmp directory, and PATH is
a stub directory plus /usr/bin:/bin.
"""
import hashlib
import os
import pty
import re
import select
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from _agentdesk_kdl_helpers import parse_kdl

REPO_ROOT = Path(__file__).resolve().parents[3]
AGENTDESK_DIR = REPO_ROOT / "quoin" / "tools" / "agentdesk"
AGENTDESK_ZSH = AGENTDESK_DIR / "agentdesk.zsh"
SETUP_SH = AGENTDESK_DIR / "setup-agentdesk.sh"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "agentdesk"

pytestmark = pytest.mark.skipif(
    shutil.which("zsh") is None, reason="zsh not available on this system"
)

ZELLIJ = shutil.which("zellij")
MARKER_RE = re.compile(r"^// agentdesk: generated standard v1 sha256=[0-9a-f]{64}$")
LEGACY_CURRENT_SHA = "9b3169af3a18c510c1425017fed5cbb37046b6fcc3abbfdf9cd83d0688163618"


# ── helpers ────────────────────────────────────────────────────────────────────

def make_env(tmp_path: Path, name: str = "") -> dict:
    base = tmp_path / name if name else tmp_path
    home = base / "home"
    stub = base / "stubbin"
    tmpdir = base / "tmpdir"
    sock = base / "sock"
    for d in (home, stub, tmpdir, sock):
        d.mkdir(parents=True, exist_ok=True)
    return {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "PATH": f"{stub}:/usr/bin:/bin",
        "TERM": "xterm-256color",
        "SHELL": "/bin/sh",
        "TMPDIR": str(tmpdir),
        "ZELLIJ_SOCKET_DIR": str(sock),
    }


def stub_bin(env: dict) -> Path:
    return Path(env["PATH"].split(":")[0])


def run_zsh(tmp_path, body, env=None, cwd=None, stdin=subprocess.DEVNULL, source=True):
    env = env if env is not None else make_env(tmp_path)
    script = f'source "{AGENTDESK_ZSH}"; {body}' if source else body
    return subprocess.run(
        ["zsh", "-f", "-c", script], env=env, capture_output=True, text=True,
        cwd=str(cwd or tmp_path), stdin=stdin, timeout=60,
    )


def standard_text(tmp_path, bars="default", env=None) -> str:
    done = run_zsh(tmp_path, f"_agentdesk_gen_standard_layout --stdout --bars {bars}", env)
    assert done.returncode == 0, done.stderr
    return done.stdout


def props(node: dict) -> dict:
    out, args, i = {}, node["args"], 0
    while i < len(args):
        a = args[i]
        if a.endswith("=") and i + 1 < len(args):
            out[a[:-1]] = args[i + 1]
            i += 2
        elif "=" in a:
            k, v = a.split("=", 1)
            out[k] = v
            i += 1
        else:
            i += 1
    return out


def layout_tabs(text: str) -> list:
    layout = parse_kdl(text)[0]
    return [c for c in layout["children"] if c["name"] == "tab"]


def tab_names(text: str) -> list:
    return [props(t).get("name") for t in layout_tabs(text)]


def leaf_panes(node: dict) -> list:
    out = []
    for c in node["children"]:
        if c["name"] == "pane":
            if any(g["name"] == "command" for g in c["children"]):
                out.append(c)
            out.extend(leaf_panes(c))
    return out


def pane_command(emitter: str, tmp_path, env=None) -> str:
    """The `-lc` argument of the first command pane an emitter prints, unescaped."""
    done = run_zsh(tmp_path, emitter, env)
    assert done.returncode == 0, done.stderr
    panes = []

    def walk(nodes):
        for n in nodes:
            if n["name"] == "pane" and any(c["name"] == "args" for c in n["children"]):
                panes.append(n)
            walk(n["children"])

    walk(parse_kdl(done.stdout))
    args = [c for c in panes[0]["children"] if c["name"] == "args"][0]["args"]
    assert args[0] == "-lc"
    return args[1]


def git_init_dirs(root: Path, *names: str):
    for n in names:
        (root / n / ".git").mkdir(parents=True)


def lazygit_stub(env: dict, capture: Path):
    path = stub_bin(env) / "lazygit"
    path.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{capture}"\n')
    path.chmod(0o755)


def read_until(fd, marker: str, timeout: float = 15.0) -> str:
    buf = ""
    end = time.time() + timeout
    while time.time() < end and marker not in buf:
        ready, _, _ = select.select([fd], [], [], 0.2)
        if ready:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk.decode(errors="replace")
    return buf


def run_in_pty(argv, env, cwd, steps):
    """Run argv under a pty; steps is a list of (wait_for, bytes_to_send)."""
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(cwd)
        os.execvpe(argv[0], argv, env)
    seen = ""
    try:
        for wait_for, send in steps:
            seen += read_until(fd, wait_for)
            assert wait_for in seen, f"never saw {wait_for!r}; output:\n{seen}"
            if send:
                os.write(fd, send)
    finally:
        seen += read_until(fd, "\x00", timeout=1.0)
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.kill(pid, 9)
        except OSError:
            pass
        os.waitpid(pid, 0)
    return seen


# ── git pane ───────────────────────────────────────────────────────────────────

def git_pane(tmp_path, root: Path, env: dict):
    env = dict(env, PROJECT_ROOT=str(root))
    return run_zsh(tmp_path, "_agentdesk_git_pane", env, cwd=root)


def test_git_pane_root_repo_uses_lazygit_on_root(tmp_path):
    env = make_env(tmp_path)
    cap = tmp_path / "cap.txt"
    lazygit_stub(env, cap)
    root = tmp_path / "proj"
    (root / ".git").mkdir(parents=True)
    done = git_pane(tmp_path, root, env)
    assert done.returncode == 0, done.stderr
    assert cap.read_text().split() == ["-p", str(root)]


def test_git_pane_nested_in_unrelated_repo_does_not_call_lazygit(tmp_path):
    env = make_env(tmp_path)
    cap = tmp_path / "cap.txt"
    lazygit_stub(env, cap)
    (tmp_path / "parent" / ".git").mkdir(parents=True)
    root = tmp_path / "parent" / "sub"
    root.mkdir()
    done = git_pane(tmp_path, root, env)
    assert done.returncode == 0, done.stderr
    assert not cap.exists()
    assert "No git repos found" in done.stdout


def test_git_pane_single_child_repo_uses_lazygit_on_child(tmp_path):
    env = make_env(tmp_path)
    cap = tmp_path / "cap.txt"
    lazygit_stub(env, cap)
    root = tmp_path / "proj"
    git_init_dirs(root, "child")
    done = git_pane(tmp_path, root, env)
    assert done.returncode == 0, done.stderr
    args = cap.read_text().split()
    assert args[0] == "-p"
    assert Path(args[1]).resolve() == (root / "child").resolve()


def test_git_pane_two_repos_shows_refresh_view(tmp_path):
    env = make_env(tmp_path)
    cap = tmp_path / "cap.txt"
    lazygit_stub(env, cap)
    root = tmp_path / "proj"
    git_init_dirs(root, "a", "b")
    done = git_pane(tmp_path, root, env)
    assert done.returncode == 0, done.stderr
    assert not cap.exists()
    assert "Git status - refreshes every 5 s" in done.stdout
    assert "===== a =====" in done.stdout and "===== b =====" in done.stdout


def test_git_pane_without_lazygit_names_the_install_command(tmp_path):
    env = make_env(tmp_path)
    root = tmp_path / "proj"
    (root / ".git").mkdir(parents=True)
    done = git_pane(tmp_path, root, env)
    assert done.returncode == 0, done.stderr
    assert "brew install lazygit" in done.stdout


def test_git_pane_empty_root(tmp_path):
    env = make_env(tmp_path)
    root = tmp_path / "proj"
    root.mkdir()
    done = git_pane(tmp_path, root, env)
    assert done.returncode == 0, done.stderr
    assert "No git repos found" in done.stdout


def test_git_pane_refresh_interval_comes_from_environment(tmp_path):
    env = make_env(tmp_path)
    root = tmp_path / "proj"
    git_init_dirs(root, "a", "b")
    ok = git_pane(tmp_path, root, dict(env, AGENTDESK_GIT_REFRESH="12"))
    assert "refreshes every 12 s" in ok.stdout
    bad = git_pane(tmp_path, root, dict(env, AGENTDESK_GIT_REFRESH="soon"))
    assert "refreshes every 5 s" in bad.stdout
    zero = git_pane(tmp_path, root, dict(env, AGENTDESK_GIT_REFRESH="0"))
    assert "refreshes every 5 s" in zero.stdout


def test_git_pane_command_without_helper_function_falls_back(tmp_path):
    env = make_env(tmp_path)
    root = tmp_path / "proj"
    root.mkdir()
    cmd = pane_command("_agentdesk_kdl_git_tab default", tmp_path, env)
    assert "; zsh" in cmd and cmd.rstrip().endswith("zsh")
    done = subprocess.run(
        ["zsh", "-c", cmd], env=dict(env, PROJECT_ROOT=str(root)),
        capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60,
    )
    assert done.returncode == 0, done.stderr
    assert "Run quoin install to get the live git view." in done.stdout


def test_git_pane_command_with_helper_file_uses_the_function(tmp_path):
    env = make_env(tmp_path)
    helper_dir = Path(env["HOME"]) / ".config" / "agentdesk"
    helper_dir.mkdir(parents=True)
    shutil.copy(AGENTDESK_ZSH, helper_dir / "agentdesk.zsh")
    root = tmp_path / "proj"
    git_init_dirs(root, "a", "b")
    cmd = pane_command("_agentdesk_kdl_git_tab default", tmp_path, env)
    done = subprocess.run(
        ["zsh", "-c", cmd], env=dict(env, PROJECT_ROOT=str(root)),
        capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60,
    )
    assert done.returncode == 0, done.stderr
    assert "===== a =====" in done.stdout


def test_git_pane_checks_lazygit_before_the_fallback(tmp_path):
    done = run_zsh(tmp_path, "typeset -f _agentdesk_git_pane")
    src = done.stdout
    assert src.index("lazygit") < src.index("brew install lazygit")


def status_cmd(tmp_path, env, token_emitter):
    return pane_command(token_emitter, tmp_path, env)


def run_cmd(cmd, env, root):
    return subprocess.run(
        ["zsh", "-c", cmd], env=dict(env, PROJECT_ROOT=str(root)),
        capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60,
    )


def test_status_pane_without_script_prints_install_hint(tmp_path):
    env = make_env(tmp_path)
    root = tmp_path / "proj"
    root.mkdir()
    for emitter in ("_agentdesk_kdl_status_tab default", "_agentdesk_kdl_pane 8 Status status"):
        cmd = status_cmd(tmp_path, env, emitter)
        done = run_cmd(cmd, env, root)
        assert done.returncode == 0, done.stderr
        assert "status_graph.py not found" in done.stdout


def test_status_pane_passes_the_expected_flags(tmp_path):
    env = make_env(tmp_path)
    scripts = Path(env["HOME"]) / ".claude" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "status_graph.py").write_text(
        "import sys\nprint('ARGV ' + ' '.join(sys.argv[1:]))\n"
    )
    root = tmp_path / "proj"
    root.mkdir()
    full = run_cmd(status_cmd(tmp_path, env, "_agentdesk_kdl_status_tab default"), env, root)
    assert re.search(r"^ARGV --watch$", full.stdout, re.M), full.stdout
    compact = run_cmd(status_cmd(tmp_path, env, "_agentdesk_kdl_pane 8 Status status"), env, root)
    assert re.search(r"^ARGV --compact --watch$", compact.stdout, re.M), compact.stdout


def test_git_pane_survives_ctrl_c(tmp_path):
    env = make_env(tmp_path)
    root = tmp_path / "proj"
    git_init_dirs(root, "a", "b")
    script = f'source "{AGENTDESK_ZSH}"; _agentdesk_git_pane; echo AFTER_$?'
    seen = run_in_pty(
        ["zsh", "-f", "-c", script], dict(env, PROJECT_ROOT=str(root)), str(root),
        [("Press any key", b"\x03"), ("AFTER_0", b"")],
    )
    assert "AFTER_0" in seen


def test_status_pane_survives_ctrl_c(tmp_path):
    env = make_env(tmp_path)
    scripts = Path(env["HOME"]) / ".claude" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "status_graph.py").write_text(
        "import time\nprint('WATCHING', flush=True)\ntime.sleep(60)\n"
    )
    root = tmp_path / "proj"
    root.mkdir()
    cmd = status_cmd(tmp_path, env, "_agentdesk_kdl_status_tab default")
    seen = run_in_pty(
        ["zsh", "-c", cmd], dict(env, PROJECT_ROOT=str(root)), str(root),
        [("WATCHING", b"\x03"), ("", b"echo MARK_$((1+1))\n"), ("MARK_2", b"exit\n")],
    )
    assert "MARK_2" in seen


def test_internal_tokens_are_not_accepted_from_users(tmp_path):
    for tok in ("git", "status-full"):
        assert run_zsh(tmp_path, f"_agentdesk_layout_from_value claude+{tok}").returncode == 1
        assert run_zsh(tmp_path, f"_agentdesk_parse_custom_tokens {tok}").returncode == 1


# ── generated Standard layout ──────────────────────────────────────────────────

REMOVED_NAMES = [
    "review", "repos", "shell", "Spend", "Tests/logs", "Workflow artifacts",
    "Project shell", "Repos list",
]


@pytest.mark.parametrize("bars", ["default", "compact"])
def test_standard_tabs_and_removed_names(tmp_path, bars):
    text = standard_text(tmp_path, bars)
    assert tab_names(text) == ["main", "git", "status"]
    for name in REMOVED_NAMES:
        assert f'"{name}"' not in text, name


def main_block(text: str) -> str:
    start = text.index('    tab name="main"')
    ends = [text.find(m, start + 1) for m in ('\n    tab name=', '\n\n    swap_tiled_layout')]
    return text[start:min(e for e in ends if e != -1)]


@pytest.mark.parametrize("bars", ["default", "compact"])
def test_standard_main_tab_equals_the_three_token_layout(tmp_path, bars):
    std = standard_text(tmp_path, bars)
    gen = run_zsh(tmp_path, f"_agentdesk_gen_layout --bars {bars} claude claude shell")
    assert gen.returncode == 0, gen.stderr
    other = Path(gen.stdout.strip()).read_text()
    assert main_block(std) == main_block(other)
    main = layout_tabs(std)[0]
    assert props(main).get("hide_floating_panes") == "true"
    names = [props(p)["name"] for p in leaf_panes(main)]
    assert names[:3] == ["Claude · A", "Claude · B", "Shell"]
    assert main_block(std).count("spend_monitor.py") == 1
    assert "--compact" in main_block(std)


def test_standard_git_and_status_tabs(tmp_path):
    tabs = layout_tabs(standard_text(tmp_path))
    git, status = tabs[1], tabs[2]
    git_panes = leaf_panes(git)
    assert [props(p)["name"] for p in git_panes] == ["Git"]
    git_args = [c for c in git_panes[0]["children"] if c["name"] == "args"][0]["args"][1]
    assert "_agentdesk_git_pane" in git_args and git_args.endswith("; zsh")
    status_panes = leaf_panes(status)
    assert [props(p)["name"] for p in status_panes] == ["Status"]
    status_args = [c for c in status_panes[0]["children"] if c["name"] == "args"][0]["args"][1]
    assert 'status_graph.py" --watch' in status_args
    assert "status_graph.py not found" in status_args


def test_standard_marker_and_hash(tmp_path):
    text = standard_text(tmp_path)
    first, _, rest = text.partition("\n")
    assert MARKER_RE.match(first), first
    assert first.rsplit("=", 1)[1] == hashlib.sha256(rest.encode()).hexdigest()
    assert text.rstrip("\n").endswith("\n}")
    assert text.endswith("}\n")
    assert standard_text(tmp_path) == text


def test_standard_output_does_not_depend_on_environment(tmp_path):
    base = standard_text(tmp_path)
    other = make_env(tmp_path, "other")
    lazygit_stub(other, tmp_path / "unused")
    other["PATH"] = f"{stub_bin(other)}:/usr/bin:/bin"
    other["PROJECT_ROOT"] = str(tmp_path / "elsewhere")
    assert standard_text(tmp_path, env=other) == base


def test_standard_compact_has_one_bar_per_tab(tmp_path):
    text = standard_text(tmp_path, "compact")
    assert MARKER_RE.match(text.split("\n", 1)[0])
    blocks = text.split("\n    tab name=")[1:]
    assert len(blocks) == 3
    for block in blocks:
        block = block.split("\n\n    swap_tiled_layout")[0]
        assert block.count("zellij:compact-bar") == 1, block
        assert "status-bar" not in block and "tab-bar" not in block


def test_standard_error_path_leaves_nothing_behind(tmp_path):
    env = make_env(tmp_path)
    done = run_zsh(tmp_path, "_agentdesk_gen_standard_layout --bars bogus", env)
    assert done.returncode == 1 and done.stdout == ""
    assert list(Path(env["TMPDIR"]).glob("agentdesk-standard-*")) == []


def test_standard_file_mode_prints_a_temp_path(tmp_path):
    env = make_env(tmp_path)
    done = run_zsh(tmp_path, "_agentdesk_gen_standard_layout", env)
    path = Path(done.stdout.strip())
    assert path.parent == Path(env["TMPDIR"]) and path.suffix == ".kdl"
    assert path.read_text() == standard_text(tmp_path, env=env)
    assert len(list(Path(env["TMPDIR"]).glob("agentdesk-standard-*"))) == 1


def test_dashboard_injection_works_on_the_standard_layout(tmp_path):
    env = make_env(tmp_path)
    layout = tmp_path / "std.kdl"
    layout.write_text(standard_text(tmp_path, env=env))
    body = (
        f"sed '$d' '{layout}' > '{tmp_path}/inj.kdl'; "
        f"_agentdesk_kdl_dashboard_tab '{layout}' /tmp/url >> '{tmp_path}/inj.kdl'"
    )
    assert run_zsh(tmp_path, body, env).returncode == 0
    text = (tmp_path / "inj.kdl").read_text()
    assert tab_names(text) == ["main", "git", "status", "Dashboard"]
    dash = layout_tabs(text)[3]
    assert any(c["name"] == "pane" and any(g["name"] == "plugin" for g in c["children"])
               for c in dash["children"])


@pytest.mark.skipif(not ZELLIJ, reason="zellij not on PATH")
@pytest.mark.parametrize("bars", ["default", "compact"])
def test_standard_layout_passes_zellij_check(tmp_path, bars):
    env = make_env(tmp_path)
    env["PATH"] = f"{Path(ZELLIJ).parent}:{env['PATH']}"
    layout = tmp_path / "std.kdl"
    layout.write_text(standard_text(tmp_path, bars, env))
    done = subprocess.run(
        [ZELLIJ, "--layout", str(layout), "setup", "--check"], env=env,
        capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60,
    )
    assert done.returncode == 0, done.stderr + done.stdout


# ── kind of the saved file and resolution ──────────────────────────────────────

def fixed_file(env: dict) -> Path:
    return Path(env["HOME"]) / ".config" / "zellij" / "layouts" / "agent-desk.kdl"


def legacy_current_text(tmp_path, env) -> str:
    done = subprocess.run(
        ["bash", "-c", f'source "{SETUP_SH}"; set +euo pipefail; _agentdesk_legacy_layout'],
        env=env, capture_output=True, text=True, cwd=str(tmp_path), timeout=60,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


def kind_of(tmp_path, env, path: Path) -> str:
    done = run_zsh(tmp_path, f'_agentdesk_fixed_layout_kind "{path}"', env)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def test_kind_matrix(tmp_path):
    env = make_env(tmp_path)
    target = fixed_file(env)
    target.parent.mkdir(parents=True)
    assert kind_of(tmp_path, env, target) == "missing"

    generated = standard_text(tmp_path, env=env)
    target.write_text(generated)
    assert kind_of(tmp_path, env, target) == "generated"

    target.write_text(generated.replace("Claude · A", "Claude · Z", 1))
    assert kind_of(tmp_path, env, target) == "custom"

    target.write_text(generated.split("\n", 1)[1])
    assert kind_of(tmp_path, env, target) == "custom"

    current = legacy_current_text(tmp_path, env)
    assert len(current.encode()) == 3453 and 'tab name="review"' in current
    target.write_text(current)
    assert kind_of(tmp_path, env, target) == "legacy"

    older = (FIXTURES / "legacy-layout-v1.kdl").read_text()
    assert len(older.encode()) == 3165
    assert hashlib.sha256(older.encode()).hexdigest().startswith("837e5440")
    target.write_text(older)
    assert kind_of(tmp_path, env, target) == "legacy"

    target.write_text("layout {\n}\n")
    assert kind_of(tmp_path, env, target) == "custom"


def resolve_path(tmp_path, env, bars="") -> Path:
    done = run_zsh(tmp_path, f"_agentdesk_fixed_layout_path {bars}", env)
    assert done.returncode == 0, done.stderr
    return Path(done.stdout.strip())


def test_fixed_layout_path_resolution(tmp_path):
    env = make_env(tmp_path)
    target = fixed_file(env)
    target.parent.mkdir(parents=True)

    target.write_text("layout {\n}\n")
    assert resolve_path(tmp_path, env) == target

    for content in (None, standard_text(tmp_path, env=env), legacy_current_text(tmp_path, env)):
        if content is None:
            target.unlink()
        else:
            target.write_text(content)
        path = resolve_path(tmp_path, env)
        assert path != target and path.parent == Path(env["TMPDIR"])
        assert tab_names(path.read_text()) == ["main", "git", "status"]
        path.unlink()

    target.unlink()
    compact = resolve_path(tmp_path, env, "compact")
    assert "zellij:compact-bar" in compact.read_text()
    compact.unlink()


def test_fixed_layout_path_falls_back_to_the_file_when_generation_fails(tmp_path):
    env = make_env(tmp_path)
    done = run_zsh(
        tmp_path,
        "_agentdesk_gen_standard_layout() { return 1; }; _agentdesk_fixed_layout_path",
        env,
    )
    assert done.stdout.strip() == str(fixed_file(env))


def test_fixed_layout_literal_appears_once_in_launcher_code():
    lines = [
        l for l in AGENTDESK_ZSH.read_text().splitlines()
        if "$HOME/.config/zellij/layouts/agent-desk.kdl" in l and not l.lstrip().startswith("#")
    ]
    assert len(lines) == 1, lines


# ── setup writes the layout ────────────────────────────────────────────────────

def run_setup_fn(tmp_path, env, setup_script: Path, target: Path):
    return subprocess.run(
        ["bash", "-c",
         f'source "{setup_script}"; set +euo pipefail; write_standard_layout "{target}"'],
        env=env, capture_output=True, text=True, cwd=str(tmp_path), timeout=120,
        stdin=subprocess.DEVNULL,
    )


def setup_env(tmp_path):
    env = make_env(tmp_path)
    env["PATH"] = f"{stub_bin(env)}:/usr/bin:/bin"
    return env


def test_setup_writes_the_generated_layout_and_is_idempotent(tmp_path):
    env = setup_env(tmp_path)
    target = fixed_file(env)
    target.parent.mkdir(parents=True)
    first = run_setup_fn(tmp_path, env, SETUP_SH, target)
    assert first.returncode == 0, first.stderr
    assert target.read_text() == standard_text(tmp_path, env=env)
    second = run_setup_fn(tmp_path, env, SETUP_SH, target)
    assert "Unchanged" in second.stdout
    assert list(target.parent.glob("*.bak")) == []


def test_setup_replaces_an_older_generated_file_with_a_backup(tmp_path):
    env = setup_env(tmp_path)
    target = fixed_file(env)
    target.parent.mkdir(parents=True)
    old_body = "layout {\n}\n"
    marker = f"// agentdesk: generated standard v1 sha256={hashlib.sha256(old_body.encode()).hexdigest()}\n"
    target.write_text(marker + old_body)
    done = run_setup_fn(tmp_path, env, SETUP_SH, target)
    assert done.returncode == 0, done.stderr
    assert target.read_text() == standard_text(tmp_path, env=env)
    backups = list(target.parent.glob("agent-desk.kdl.agentdesk-layout.*.bak"))
    assert len(backups) == 1 and backups[0].read_text() == marker + old_body


@pytest.mark.parametrize("which", ["current", "older"])
def test_setup_replaces_known_legacy_files(tmp_path, which):
    env = setup_env(tmp_path)
    target = fixed_file(env)
    target.parent.mkdir(parents=True)
    old = (legacy_current_text(tmp_path, env) if which == "current"
           else (FIXTURES / "legacy-layout-v1.kdl").read_text())
    target.write_text(old)
    done = run_setup_fn(tmp_path, env, SETUP_SH, target)
    assert done.returncode == 0, done.stderr
    assert target.read_text() == standard_text(tmp_path, env=env)
    backups = list(target.parent.glob("*.bak"))
    assert len(backups) == 1 and backups[0].read_text() == old


def test_setup_keeps_customised_files_and_writes_new_beside(tmp_path):
    env = setup_env(tmp_path)
    target = fixed_file(env)
    target.parent.mkdir(parents=True)
    generated = standard_text(tmp_path, env=env)
    for custom in ("layout {\n}\n", generated.replace("Shell", "Terminal", 1)):
        target.write_text(custom)
        new = Path(str(target) + ".new")
        new.unlink(missing_ok=True)
        done = run_setup_fn(tmp_path, env, SETUP_SH, target)
        assert done.returncode == 0, done.stderr
        assert target.read_text() == custom
        assert new.read_text() == generated
        assert "mv " in done.stdout
        again = run_setup_fn(tmp_path, env, SETUP_SH, target)
        assert "Kept your customised layout" not in again.stdout
        new.write_text("stale\n")
        rewritten = run_setup_fn(tmp_path, env, SETUP_SH, target)
        assert new.read_text() == generated
        assert "Kept your customised layout" in rewritten.stdout


def lone_setup(tmp_path) -> Path:
    lone = tmp_path / "lone"
    lone.mkdir()
    shutil.copy(SETUP_SH, lone / "setup-agentdesk.sh")
    return lone / "setup-agentdesk.sh"


def test_setup_without_generator_writes_legacy_only_when_missing(tmp_path):
    env = setup_env(tmp_path)
    target = fixed_file(env)
    target.parent.mkdir(parents=True)
    script = lone_setup(tmp_path)
    done = run_setup_fn(tmp_path, env, script, target)
    assert done.returncode == 0, done.stderr
    assert target.read_text() == legacy_current_text(tmp_path, env)
    assert 'tab name="review"' in target.read_text()

    generated = standard_text(tmp_path, env=env)
    target.write_text(generated)
    done = run_setup_fn(tmp_path, env, script, target)
    assert target.read_text() == generated
    assert list(target.parent.glob("*.bak")) == []


def test_setup_generator_failure_leaves_the_target_alone(tmp_path):
    env = setup_env(tmp_path)
    target = fixed_file(env)
    target.parent.mkdir(parents=True)
    target.write_text("layout {\n}\n")
    script = lone_setup(tmp_path)
    (script.parent / "agentdesk.zsh").write_text(
        "_agentdesk_gen_standard_layout() { return 1; }\n"
    )
    done = run_setup_fn(tmp_path, env, script, target)
    assert done.returncode == 0, done.stderr
    assert target.read_text() == "layout {\n}\n"
    assert "Warning" in done.stdout
    assert not Path(str(target) + ".new").exists()


# ── picker and help ────────────────────────────────────────────────────────────

def pick(tmp_path, text):
    return subprocess.run(
        ["zsh", "-f", "-c", f'source "{AGENTDESK_ZSH}"; _agentdesk_pick_layout'],
        env=make_env(tmp_path), input=text, capture_output=True, text=True,
        cwd=str(tmp_path), timeout=60,
    )


def test_picker_menu_lists_the_status_preset_and_custom(tmp_path):
    done = pick(tmp_path, "1\n")
    assert "1) Standard (agents / git / status" in done.stderr
    assert "6) claude + shell + status + spend" in done.stderr
    assert "7) Custom" in done.stderr


def test_picker_option_six_is_the_status_preset(tmp_path):
    assert pick(tmp_path, "6\n").stdout.split() == ["claude", "shell", "status"]


def test_picker_option_seven_takes_typed_tokens(tmp_path):
    assert pick(tmp_path, "7\nclaude, shell\n").stdout.split() == ["claude", "shell"]


def test_picker_still_accepts_a_direct_token_list(tmp_path):
    assert pick(tmp_path, "claude, codex\n").stdout.split() == ["claude", "codex"]


def test_help_describes_the_standard_layout(tmp_path):
    done = run_zsh(tmp_path, "agentdesk --help")
    assert done.returncode == 0, done.stderr
    for text in ("agent-desk.kdl.new", "lazygit", "6 claude + shell + status", "7 custom"):
        assert text in done.stdout, text
