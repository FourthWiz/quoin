"""Sandbox homes, session environments, the arm tree check and the quoin install."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from quoin.benchmarks.harness import arm_identity as ai
from quoin.benchmarks.harness import arm_sandbox as sb
from quoin.benchmarks.scripts import run_three_arm_gate as gate

REPO_ROOT = Path(__file__).resolve().parents[3]
AUTH = ("CLAUDE_CODE_OAUTH_TOKEN", "tok")
OPERATOR_ENV = {
    "PATH": "/usr/bin", "LANG": "en_US.UTF-8", "HOME": "/Users/operator",
    "CLAUDE_CONFIG_DIR": "/Users/operator/.claude", "CLAUDECODE": "1",
    "QUOIN_DISABLE_X": "1", "GIT_DIR": "/x", "PYTHONPATH": "/y",
    "CLAUDE_CODE_OAUTH_TOKEN": "tok", "HTTPS_PROXY": "http://p",
}


@pytest.fixture(autouse=True)
def _remove_created_sandboxes(monkeypatch):
    made = []
    real = sb.create_sandbox

    def tracking(*a, **k):
        box = real(*a, **k)
        made.append(box)
        return box

    monkeypatch.setattr(sb, "create_sandbox", tracking)
    yield
    for box in made:
        if box.cli_tmp is not None and os.path.isdir(box.cli_tmp):
            sb._force_remove(box.cli_tmp)


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   check=True, capture_output=True)


def _stand_in_repo(tmp_path):
    root = tmp_path / "arm"
    (root / "quoin").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "quoin" / "a.txt").write_text("a")
    (root / "src" / "b.txt").write_text("b")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "one")
    head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True,
                          text=True, check=True).stdout.strip()
    return root, head


def test_sandbox_is_private_and_has_base_settings(tmp_path):
    box = sb.create_sandbox("plain", parent=tmp_path)
    assert os.lstat(box.home).st_mode & 0o777 == 0o700
    assert box.home.name.startswith(sb.SANDBOX_PREFIX + "plain_")
    settings = json.loads((box.claude_dir / "settings.json").read_text())
    assert settings == {"permissions": sb.SANDBOX_BASE_PERMISSIONS}
    assert "hooks" not in settings
    assert ai.check_plain_sandbox(box.home) == []


def test_symlink_parent_is_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(RuntimeError):
        sb.create_sandbox("plain", parent=link)
    assert list(real.iterdir()) == []


@pytest.mark.parametrize("cell,with_sandbox", [
    ("simple-claude", True), ("quoin-claude", True), ("quoin-claude", False),
])
def test_session_env_is_exactly_the_allowlist(tmp_path, cell, with_sandbox):
    box = sb.create_sandbox("plain", parent=tmp_path) if with_sandbox else None
    env = sb.build_session_env(box, cell, "run1", AUTH, environ=OPERATOR_ENV)
    expected = {"PATH", "LANG", "HTTPS_PROXY", "CLAUDE_CODE_OAUTH_TOKEN", "HOME",
                "GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"}
    if with_sandbox:
        expected |= {"CLAUDE_CONFIG_DIR", "TMPDIR", "CLAUDE_CODE_TMPDIR"}
        assert env["TMPDIR"] == str(box.home / "tmp")
        assert env["CLAUDE_CODE_TMPDIR"] == str(box.cli_tmp)
        assert env["CLAUDE_CONFIG_DIR"] == str(box.claude_dir)
        assert env["HOME"] == str(box.home)
    else:
        assert env["HOME"] == str(Path.home())
    expected |= {"ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                 "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_SMALL_FAST_MODEL"}
    assert "CLAUDE_CODE_SUBAGENT_MODEL" not in env
    if cell == "quoin-claude":
        expected |= {"QUOIN_GATE_AUTO_APPROVE", "QUOIN_BENCHMARK_RUN", "QUOIN_AUTO_RESUME"}
        assert env["QUOIN_BENCHMARK_RUN"] == "run1"
        assert env["QUOIN_AUTO_RESUME"] == "0"
    else:
        assert "QUOIN_AUTO_RESUME" not in env
    assert set(env) == expected
    assert sb.session_env_keys(env) == sorted(expected)


def test_each_session_scratches_in_its_own_sandbox_not_the_shared_temp_dir(tmp_path):
    operator_tmp = tmp_path / "operator_tmp"
    operator_tmp.mkdir()
    parent = tmp_path / "run"
    parent.mkdir(mode=0o700)
    plain = sb.create_sandbox("plain", parent)
    quoin = sb.create_sandbox("quoin", parent)
    environ = dict(OPERATOR_ENV, TMPDIR=str(operator_tmp))
    plain_env = sb.build_session_env(plain, "simple-claude", "r", AUTH, environ=environ)
    quoin_env = sb.build_session_env(quoin, "quoin-claude", "r", AUTH, environ=environ)
    assert plain_env["TMPDIR"] != str(operator_tmp)
    assert os.path.isdir(plain_env["TMPDIR"]) and os.lstat(plain_env["TMPDIR"]).st_mode & 0o077 == 0
    # What the plain session sees when it lists its scratch area or its config
    # location must hold nothing of the quoin sandbox.
    assert os.listdir(plain_env["TMPDIR"]) == []
    for value in plain_env.values():
        assert str(quoin.home) not in value
    for value in quoin_env.values():
        assert str(plain.home) not in value
    assert plain_env["CLAUDE_CODE_TMPDIR"] != quoin_env["CLAUDE_CODE_TMPDIR"]
    for box, env in ((plain, plain_env), (quoin, quoin_env)):
        cli_tmp = env["CLAUDE_CODE_TMPDIR"]
        assert os.lstat(cli_tmp).st_mode & 0o077 == 0 and os.listdir(cli_tmp) == []
        assert cli_tmp != str(operator_tmp) and not cli_tmp.startswith(str(box.home))
        assert len(cli_tmp) <= sb.CLI_TMP_MAX_LENGTH
    assert not str(plain.tmp_dir).startswith(str(quoin.home))
    assert not str(quoin.tmp_dir).startswith(str(plain.home))


def test_fingerprint_does_not_follow_planted_symlinks(tmp_path):
    box = sb.create_sandbox("plain", parent=tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.md").write_text("one")
    (box.claude_dir / "skills").symlink_to(outside)
    first = sb.config_fingerprint(box.claude_dir)
    (outside / "x.md").write_text("two")
    assert sb.config_fingerprint(box.claude_dir) == first
    (box.claude_dir / "skills").unlink()
    assert sb.config_fingerprint(box.claude_dir) != first


def test_remove_sandbox_refuses_a_swapped_symlink(tmp_path):
    box = sb.create_sandbox("plain", parent=tmp_path)
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("k")
    moved = tmp_path / "moved"
    box.home.rename(moved)
    box.home.symlink_to(victim)
    note = sb.remove_sandbox(box)
    assert note is not None
    assert (victim / "keep.txt").read_text() == "k"


def test_select_auth():
    assert sb.select_auth({"ANTHROPIC_API_KEY": "k"}) == ("ANTHROPIC_API_KEY", "k")
    with pytest.raises(ValueError, match="no auth variable"):
        sb.select_auth({})
    with pytest.raises(ValueError, match="both auth variables"):
        sb.select_auth({"ANTHROPIC_API_KEY": "k", "CLAUDE_CODE_OAUTH_TOKEN": "t"})


def test_check_arm_tree(tmp_path):
    root, head = _stand_in_repo(tmp_path)
    assert sb.check_arm_tree(root, head) == []
    assert any(p.startswith("head-mismatch") for p in sb.check_arm_tree(root, "0" * 40))
    (root / "quoin" / "new.txt").write_text("x")
    assert any(p.startswith("dirty-tree") for p in sb.check_arm_tree(root, head))


def test_install_refuses_bad_tree_without_running(tmp_path):
    root, head = _stand_in_repo(tmp_path)
    box = sb.create_sandbox("quoin", parent=tmp_path)

    def boom(*a, **k):
        raise AssertionError("installer must not run")

    wrong = sb.install_quoin(box, root, "0" * 40, run=boom)
    assert wrong["ok"] is False and "head-mismatch" in wrong["reason"]
    (root / "src" / "dirty.txt").write_text("x")
    dirty = sb.install_quoin(box, root, head, run=boom)
    assert dirty["ok"] is False and "dirty-tree" in dirty["reason"]


def test_real_install_into_sandbox(tmp_path, monkeypatch):
    real_claude = Path.home() / ".claude"
    watched = [real_claude / "settings.json", real_claude / "CLAUDE.md"]
    before = [p.read_bytes() if p.exists() else None for p in watched]
    monkeypatch.setattr(sb, "check_arm_tree", lambda *a, **k: [])
    box = sb.create_sandbox("quoin", parent=tmp_path)
    result = sb.install_quoin(box, REPO_ROOT, "stub", python=sys.executable)
    assert result["ok"], result
    assert ai.check_quoin_sandbox(box.home, REPO_ROOT) == []
    assert ai.check_plain_sandbox(box.home) != []
    assert [p.read_bytes() if p.exists() else None for p in watched] == before
    settings = json.loads((box.claude_dir / "settings.json").read_text())
    assert set(sb.SANDBOX_BASE_PERMISSIONS["allow"]) <= set(settings["permissions"]["allow"])
    assert set(sb.SANDBOX_BASE_PERMISSIONS["deny"]) <= set(settings["permissions"]["deny"])


def test_remove_sandbox(tmp_path):
    box = sb.create_sandbox("plain", parent=tmp_path)
    (box.claude_dir / "locked").mkdir()
    (box.claude_dir / "locked" / "f").write_text("x")
    (box.claude_dir / "locked").chmod(0o500)
    (box.cli_tmp / "claude-1" / "s").mkdir(parents=True)
    (box.cli_tmp / "claude-1" / "s").chmod(0o500)
    assert sb.remove_sandbox(box) is None
    assert not box.home.exists() and not box.cli_tmp.exists()
    stranger = tmp_path / "keep"
    stranger.mkdir()
    message = sb.remove_sandbox(sb.Sandbox("plain", stranger, stranger / ".claude"))
    assert message and stranger.exists()


def test_fingerprint_ignores_cli_state_and_sees_identity_changes(tmp_path):
    box = sb.create_sandbox("plain", parent=tmp_path)
    base = sb.config_fingerprint(box.claude_dir)
    (box.claude_dir / "plugins").mkdir()
    (box.claude_dir / "plugins" / "blocklist.json").write_text("{}")
    (box.claude_dir / "plugins" / "installed_plugins.json").write_text('{"version": 1, "plugins": {}}')
    (box.claude_dir / ".claude.json").write_text("{}")
    (box.claude_dir / "projects").mkdir()
    (box.claude_dir / "projects" / "p.jsonl").write_text("x")
    assert sb.config_fingerprint(box.claude_dir) == base

    def changed(mutate):
        mutate()
        value = sb.config_fingerprint(box.claude_dir)
        assert value != base
        return value

    settings_path = box.claude_dir / "settings.json"
    original = settings_path.read_text()
    (box.claude_dir / "skills" / "x").mkdir(parents=True)
    (box.claude_dir / "skills" / "x" / "SKILL.md").write_text("x")
    assert sb.config_fingerprint(box.claude_dir) != base
    import shutil
    shutil.rmtree(box.claude_dir / "skills")
    assert sb.config_fingerprint(box.claude_dir) == base
    changed(lambda: (box.claude_dir / "commands").mkdir() or (box.claude_dir / "commands" / "c.md").write_text("c"))
    shutil.rmtree(box.claude_dir / "commands")
    data = json.loads(original)
    data["hooks"] = {"SessionStart": []}
    changed(lambda: settings_path.write_text(json.dumps(data)))
    settings_path.write_text(original)
    data = json.loads(original)
    data["enabledPlugins"] = {"p": True}
    changed(lambda: settings_path.write_text(json.dumps(data)))


def test_installed_plugin_names(tmp_path):
    claude = tmp_path / ".claude"
    claude.mkdir()
    assert sb.installed_plugin_names(claude) == []
    (claude / "plugins").mkdir()
    path = claude / "plugins" / "installed_plugins.json"
    path.write_text('{"version": 1, "plugins": {}}')
    assert sb.installed_plugin_names(claude) == []
    path.write_text('{"version": 1, "plugins": {"demo@market": [{}]}}')
    assert sb.installed_plugin_names(claude) == ["demo@market"]
    path.write_text("not json")
    assert sb.installed_plugin_names(claude) == ["<unparsable>"]


def test_fingerprint_does_not_open_a_planted_fifo(tmp_path):
    box = sb.create_sandbox("plain", parent=tmp_path)
    before = sb.config_fingerprint(box.claude_dir)
    (box.claude_dir / "skills").mkdir()
    os.mkfifo(box.claude_dir / "skills" / "pipe")
    result = {}
    import threading

    thread = threading.Thread(target=lambda: result.update(fp=sb.config_fingerprint(box.claude_dir)),
                              daemon=True)
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive(), "fingerprint blocked on a FIFO"
    assert result["fp"] != before
    sb.remove_sandbox(box)
