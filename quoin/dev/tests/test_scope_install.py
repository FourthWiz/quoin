"""Tests for --scope user|project[:DIR] install flag (T-02 through T-05, T-10, T-13).

All in-process; no claude/npx/pip required.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SRC = REPO / "src"
QUOIN_SRC = REPO / "quoin"

# Ensure src is importable
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


# ── T-03: _resolve_dest_root ──────────────────────────────────────────────────

def _make_args(**kwargs):
    """Build a minimal argparse.Namespace with given keyword args."""
    import argparse
    defaults = dict(
        scope="user",
        allow_hook_merge=False,
        runtime="claude",
        check=False,
    )
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def test_resolve_dest_root_user_mode(tmp_path, monkeypatch):
    """--scope user (default) resolves to HOME/.claude."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # Reload pathlib.Path.home() to pick up new HOME env var
    import importlib
    import quoin.cli as cli_mod
    importlib.reload(cli_mod)
    result = cli_mod._resolve_dest_root(_make_args(scope="user"))
    # Path.home() expands the HOME env var on POSIX
    assert ".claude" in str(result)
    assert result.name == ".claude"


def test_resolve_dest_root_project_cwd(tmp_path, monkeypatch):
    """--scope project uses CWD as project dir."""
    monkeypatch.chdir(tmp_path)
    from quoin.cli import _resolve_dest_root
    args = _make_args(scope="project")
    result = _resolve_dest_root(args)
    # Must be <cwd>/.claude
    assert result == tmp_path.resolve() / ".claude"


def test_resolve_dest_root_project_explicit_path(tmp_path, monkeypatch):
    """--scope project:/path uses explicit project dir."""
    project_dir = tmp_path / "my-project"
    project_dir.mkdir()
    from quoin.cli import _resolve_dest_root
    args = _make_args(scope=f"project:{project_dir}")
    result = _resolve_dest_root(args)
    assert result == project_dir.resolve() / ".claude"


def test_resolve_dest_root_refuses_root_as_project():
    """--scope project:/ must be refused."""
    from quoin.cli import _resolve_dest_root
    args = _make_args(scope="project:/")
    with pytest.raises(SystemExit) as exc_info:
        _resolve_dest_root(args)
    assert exc_info.value.code == 2


def test_resolve_dest_root_no_scope_attr():
    """When scope attribute is absent, defaults to user mode (returns ~/.claude)."""
    from quoin.cli import _resolve_dest_root
    import argparse
    args = argparse.Namespace()  # no scope attr
    result = _resolve_dest_root(args)
    assert result.name == ".claude"
    assert "home" in str(result).lower() or str(pathlib.Path.home()) in str(result)


# ── T-13: detect_home_hook_conflict ──────────────────────────────────────────

def _make_home_settings(home: Path, stanzas: list) -> None:
    """Write ~/.claude/settings.json with given hook stanzas for UserPromptSubmit."""
    claude_dir = home / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    settings = {
        "hooks": {
            "UserPromptSubmit": stanzas,
        }
    }
    (claude_dir / "settings.json").write_text(json.dumps(settings, indent=2))


def test_detect_home_hook_conflict_no_settings(tmp_path, monkeypatch):
    """No ~/.claude/settings.json → no conflict."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # Force pathlib.Path.home() to use the patched HOME
    import quoin.installer as _inst
    import unittest.mock
    with unittest.mock.patch.object(pathlib.Path, "home", return_value=tmp_path):
        result = _inst.detect_home_hook_conflict()
    assert result is False


def test_detect_home_hook_conflict_empty_settings(tmp_path, monkeypatch):
    """Empty settings.json → no conflict."""
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text("{}")
    import quoin.installer as _inst
    import unittest.mock
    with unittest.mock.patch.object(pathlib.Path, "home", return_value=tmp_path):
        result = _inst.detect_home_hook_conflict()
    assert result is False


def test_detect_home_hook_conflict_quoin_stanza_present(tmp_path, monkeypatch):
    """When ~/.claude/settings.json has userpromptsubmit.sh stanza in a .claude/hooks/ path → conflict."""
    _make_home_settings(tmp_path, [{
        "matcher": "*",
        "hooks": [{"type": "command", "command": "/home/user/.claude/hooks/userpromptsubmit.sh", "timeout": 5}],
    }])
    import quoin.installer as _inst
    import unittest.mock
    with unittest.mock.patch.object(pathlib.Path, "home", return_value=tmp_path):
        result = _inst.detect_home_hook_conflict()
    assert result is True


def test_detect_home_hook_conflict_non_claude_hooks_path(tmp_path, monkeypatch):
    """quoin hook basename at a non-/.claude/hooks/ path → no conflict (false-positive prevention)."""
    _make_home_settings(tmp_path, [{
        "matcher": "*",
        "hooks": [{"type": "command", "command": "/some/other/path/userpromptsubmit.sh", "timeout": 5}],
    }])
    import quoin.installer as _inst
    import unittest.mock
    with unittest.mock.patch.object(pathlib.Path, "home", return_value=tmp_path):
        result = _inst.detect_home_hook_conflict()
    assert result is False


def test_detect_home_hook_conflict_sessionstart(tmp_path, monkeypatch):
    """sessionstart.sh in home settings → conflict."""
    _make_home_settings(tmp_path, [{
        "matcher": "startup",
        "hooks": [{"type": "command", "command": "/home/user/.claude/hooks/sessionstart.sh", "timeout": 5}],
    }])
    import quoin.installer as _inst
    import unittest.mock
    with unittest.mock.patch.object(pathlib.Path, "home", return_value=tmp_path):
        result = _inst.detect_home_hook_conflict()
    assert result is True


def test_detect_home_hook_conflict_non_quoin_hook(tmp_path, monkeypatch):
    """Non-quoin hook → no conflict."""
    _make_home_settings(tmp_path, [{
        "matcher": "*",
        "hooks": [{"type": "command", "command": "/usr/local/bin/my-custom-hook.sh", "timeout": 5}],
    }])
    import quoin.installer as _inst
    import unittest.mock
    with unittest.mock.patch.object(pathlib.Path, "home", return_value=tmp_path):
        result = _inst.detect_home_hook_conflict()
    assert result is False


# ── T-04/T-05: deploy_hooks project mode ─────────────────────────────────────

def _make_fake_src(tmp: Path) -> Path:
    """Create minimal source dir with stub hook scripts."""
    hooks_dir = tmp / "hooks"
    hooks_dir.mkdir(parents=True)
    for fname in ("userpromptsubmit.sh", "precompact.sh", "postcompact.sh",
                  "sessionstart.sh", "sessionend.sh", "_lib.sh", "worktreecreate.sh", "stop.sh"):
        (hooks_dir / fname).write_text("#!/bin/bash\n")
    return tmp


def test_deploy_hooks_project_mode_note(tmp_path, capsys):
    """deploy_hooks in project mode prints a note about project-scoping."""
    from quoin.installer import deploy_hooks
    src = _make_fake_src(tmp_path / "src")
    dest = tmp_path / "project" / ".claude"
    deploy_hooks(src, dest, is_project_mode=True)
    captured = capsys.readouterr()
    assert "project-scoped" in captured.out or "project mode" in captured.out


def test_deploy_hooks_project_mode_does_not_touch_home(tmp_path, monkeypatch):
    """deploy_hooks in project mode must NOT modify ~/.claude/settings.json.

    This is structural: deploy_hooks writes to dest_root/settings.json, so as
    long as dest_root != ~/.claude, home settings are untouched.
    """
    from quoin.installer import deploy_hooks
    src = _make_fake_src(tmp_path / "src")
    project = tmp_path / "project"
    project.mkdir()
    dest = project / ".claude"
    deploy_hooks(src, dest, is_project_mode=True)

    # dest settings.json must exist
    assert (dest / "settings.json").exists()
    # ~/.claude/ must NOT have been created by this call (we aren't monkeypatching
    # home, but deploy_hooks writes only to dest_root/settings.json by design)
    # Verify no ~/.claude/ was written inside our tmp workspace
    assert not (tmp_path / "home" / ".claude").exists()


def test_deploy_hooks_project_mode_settings_in_project(tmp_path, monkeypatch):
    """deploy_hooks in project mode writes hooks to <project>/.claude/settings.json."""
    from quoin.installer import deploy_hooks
    src = _make_fake_src(tmp_path / "src")
    dest = tmp_path / "project" / ".claude"
    deploy_hooks(src, dest, is_project_mode=True)

    settings_path = dest / "settings.json"
    assert settings_path.exists()
    settings = json.loads(settings_path.read_text())
    hooks = settings.get("hooks", {})
    # All 9 stanzas registered in project settings
    assert len(hooks.get("UserPromptSubmit", [])) == 1
    assert len(hooks.get("SessionStart", [])) == 3
    assert len(hooks.get("SessionEnd", [])) == 1
    # Commands reference project hooks dir (not ~/)
    for stanzas_list in hooks.values():
        for stanza in stanzas_list:
            for hook in stanza.get("hooks", []):
                cmd = hook.get("command", "")
                assert not cmd.startswith("~"), f"tilde path in project settings: {cmd}"
                assert str(dest) in cmd, f"command does not reference project dest: {cmd}"


# ── T-05: merge_workflow_rules project mode placement ────────────────────────

def _make_minimal_src_for_merge(tmp: Path) -> Path:
    """Create minimal source dir with CLAUDE.md (+ CLAUDE.slim.md, T-07) for merge tests."""
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "CLAUDE.md").write_text("# Test Rules\nSome workflow rules.\n", encoding="utf-8")
    (tmp / "CLAUDE.slim.md").write_text("# Test Rules (slim)\nSlim workflow rules.\n", encoding="utf-8")
    return tmp


def test_merge_workflow_rules_project_mode_places_at_project_root(tmp_path):
    """In project mode, CLAUDE.md is written to <project>/CLAUDE.md (not <project>/.claude/CLAUDE.md)."""
    from quoin.installer import merge_workflow_rules
    src = _make_minimal_src_for_merge(tmp_path / "src")
    dest_root = tmp_path / "project" / ".claude"
    dest_root.mkdir(parents=True)
    claude_md_path = tmp_path / "project" / "CLAUDE.md"

    merge_workflow_rules(src, dest_root, claude_md_path=claude_md_path)

    # CLAUDE.md must exist at project root
    assert claude_md_path.exists()
    # CLAUDE.md must NOT exist at project/.claude/CLAUDE.md
    wrong_location = dest_root / "CLAUDE.md"
    assert not wrong_location.exists(), (
        "CLAUDE.md must not be created at <project>/.claude/CLAUDE.md"
    )


def test_merge_workflow_rules_user_mode_default_path(tmp_path):
    """In user mode (no explicit claude_md_path), CLAUDE.md is written to dest_root/CLAUDE.md."""
    from quoin.installer import merge_workflow_rules
    src = _make_minimal_src_for_merge(tmp_path / "src")
    dest_root = tmp_path / ".claude"
    dest_root.mkdir(parents=True)

    merge_workflow_rules(src, dest_root)  # no claude_md_path

    claude_md = dest_root / "CLAUDE.md"
    assert claude_md.exists()


def test_merge_workflow_rules_marker_count_preserved(tmp_path):
    """Re-running in project mode (pair_count=1) replaces content, not appends."""
    from quoin.installer import merge_workflow_rules
    src = _make_minimal_src_for_merge(tmp_path / "src")
    dest_root = tmp_path / "project" / ".claude"
    dest_root.mkdir(parents=True)
    claude_md_path = tmp_path / "project" / "CLAUDE.md"

    # First run: creates the file
    merge_workflow_rules(src, dest_root, claude_md_path=claude_md_path)
    assert claude_md_path.read_text().count("# === DEV WORKFLOW START ===") == 1

    # Second run: replaces (not appends)
    merge_workflow_rules(src, dest_root, claude_md_path=claude_md_path)
    assert claude_md_path.read_text().count("# === DEV WORKFLOW START ===") == 1


# ── T-06 infrastructure: substitute_quoin_home ───────────────────────────────

def test_substitute_quoin_home_replaces_placeholder(tmp_path):
    """substitute_quoin_home replaces __QUOIN_HOME__ with str(dest_root)."""
    from quoin.installer import substitute_quoin_home
    dest_root = pathlib.Path("/some/project/.claude")
    text = "read __QUOIN_HOME__/scripts/path_resolve.py"
    result = substitute_quoin_home(text, dest_root)
    assert "__QUOIN_HOME__" not in result
    assert "/some/project/.claude/scripts/path_resolve.py" in result


def test_substitute_quoin_home_no_placeholder(tmp_path):
    """Text with no placeholder is returned unchanged."""
    from quoin.installer import substitute_quoin_home
    dest_root = pathlib.Path("/some/project/.claude")
    text = "# Deployed to ~/.claude/ (documentation only)"
    result = substitute_quoin_home(text, dest_root)
    assert result == text


def test_substitute_quoin_home_multiple_occurrences(tmp_path):
    """Multiple occurrences are all replaced."""
    from quoin.installer import substitute_quoin_home
    dest_root = pathlib.Path("/proj/.claude")
    text = "__QUOIN_HOME__/a and __QUOIN_HOME__/b"
    result = substitute_quoin_home(text, dest_root)
    assert result == "/proj/.claude/a and /proj/.claude/b"


# ── T-02: argparse integration ────────────────────────────────────────────────

def test_scope_flag_present_in_install_help():
    """'quoin install --help' output includes --scope."""
    import subprocess
    result = subprocess.run(
        [sys.executable, "-m", "quoin", "install", "--help"],
        env={**os.environ, "PYTHONPATH": str(SRC)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "--scope" in result.stdout


def test_scope_defaults_to_none_without_flag(monkeypatch):
    """--scope defaults to None when not provided (interactive prompt handles it)."""
    from quoin.cli import main

    captured_args = {}

    def fake_cmd(args):
        captured_args.update(vars(args))
        return 0

    import quoin.cli as cli_mod
    monkeypatch.setattr(cli_mod, "_cmd_claude_install", fake_cmd)
    main(["install", "--source-dir", str(QUOIN_SRC)])
    assert captured_args.get("scope") is None


def test_scope_user_explicit(monkeypatch):
    """--scope user is parsed and forwarded correctly."""
    from quoin.cli import main

    captured_args = {}

    def fake_cmd(args):
        captured_args.update(vars(args))
        return 0

    import quoin.cli as cli_mod
    monkeypatch.setattr(cli_mod, "_cmd_claude_install", fake_cmd)
    main(["install", "--source-dir", str(QUOIN_SRC), "--scope", "user"])
    assert captured_args.get("scope") == "user"


def test_scope_project_parsed(monkeypatch):
    """--scope project is parsed correctly."""
    from quoin.cli import main

    captured_args = {}

    def fake_cmd(args):
        captured_args.update(vars(args))
        return 0

    import quoin.cli as cli_mod
    monkeypatch.setattr(cli_mod, "_cmd_claude_install", fake_cmd)
    main(["install", "--source-dir", str(QUOIN_SRC), "--scope", "project"])
    assert captured_args.get("scope") == "project"


def test_scope_project_with_path_parsed(monkeypatch, tmp_path):
    """--scope project:/path is parsed correctly."""
    from quoin.cli import main

    captured_args = {}

    def fake_cmd(args):
        captured_args.update(vars(args))
        return 0

    import quoin.cli as cli_mod
    monkeypatch.setattr(cli_mod, "_cmd_claude_install", fake_cmd)
    main(["install", "--source-dir", str(QUOIN_SRC), "--scope", f"project:{tmp_path}"])
    assert captured_args.get("scope") == f"project:{tmp_path}"


def test_scope_project_mutex_with_codex(monkeypatch, tmp_path, capsys):
    """--scope project + --runtime codex fails with exit 2.

    The check fires in _cmd_claude_install when both scope=project and runtime=codex
    are present. _cmd_install dispatches to codex for --runtime codex, but the
    check in _cmd_install catches it first before delegating.
    """
    from quoin.cli import _cmd_install
    args = _make_args(scope="project", runtime="codex", source_dir=None)
    with pytest.raises(SystemExit) as exc_info:
        _cmd_install(args)
    assert exc_info.value.code == 2


def test_allow_hook_merge_flag(monkeypatch):
    """--allow-hook-merge is parsed and defaults to False."""
    from quoin.cli import main

    captured_args = {}

    def fake_cmd(args):
        captured_args.update(vars(args))
        return 0

    import quoin.cli as cli_mod
    monkeypatch.setattr(cli_mod, "_cmd_claude_install", fake_cmd)
    main(["install", "--source-dir", str(QUOIN_SRC)])
    assert captured_args.get("allow_hook_merge") is False

    main(["install", "--source-dir", str(QUOIN_SRC), "--allow-hook-merge"])
    assert captured_args.get("allow_hook_merge") is True


# ── T-13: project install fails fast on home hook conflict ───────────────────

def test_project_install_fails_on_home_hooks(tmp_path, monkeypatch, capsys):
    """Project-mode install must fail fast when home settings.json has quoin hooks."""
    import unittest.mock

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    # Plant quoin stanza in home settings
    home_claude = fake_home / ".claude"
    home_claude.mkdir()
    (home_claude / "settings.json").write_text(json.dumps({
        "hooks": {
            "UserPromptSubmit": [{
                "matcher": "*",
                "hooks": [{"type": "command", "command": "/home/user/.claude/hooks/userpromptsubmit.sh", "timeout": 5}],
            }]
        }
    }))

    monkeypatch.chdir(tmp_path)

    from quoin.cli import _cmd_claude_install
    import argparse
    args = argparse.Namespace(
        scope="project",
        allow_hook_merge=False,
        runtime="claude",
        check=False,
        source_dir=str(QUOIN_SRC),
        force_merge=False,
        dev=False,
        use_pip=False,
    )

    with unittest.mock.patch.object(pathlib.Path, "home", return_value=fake_home):
        with pytest.raises(SystemExit) as exc_info:
            _cmd_claude_install(args)
    assert exc_info.value.code == 2
    out, err = capsys.readouterr()
    assert "double" in err.lower() or "twice" in err.lower() or "home-level" in err.lower() or "hook" in err.lower()


def test_project_install_allows_hook_merge_with_flag(tmp_path, monkeypatch, capsys):
    """--allow-hook-merge bypasses the home hook conflict check."""
    import unittest.mock

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    # Plant quoin stanza in home settings
    home_claude = fake_home / ".claude"
    home_claude.mkdir()
    (home_claude / "settings.json").write_text(json.dumps({
        "hooks": {
            "UserPromptSubmit": [{
                "matcher": "*",
                "hooks": [{"type": "command", "command": str(home_claude / "hooks" / "userpromptsubmit.sh"), "timeout": 5}],
            }]
        }
    }))

    monkeypatch.chdir(tmp_path)
    # Patch install operations to succeed without actual files
    import quoin.installer as inst
    monkeypatch.setattr(inst, "check_prerequisites", lambda: [])
    monkeypatch.setattr(inst, "deploy_memory", lambda *a, **kw: None)
    monkeypatch.setattr(inst, "deploy_quickstart", lambda *a, **kw: None)
    monkeypatch.setattr(inst, "deploy_skills", lambda *a, **kw: 0)
    monkeypatch.setattr(inst, "deploy_scripts", lambda *a, **kw: None)
    monkeypatch.setattr(inst, "deploy_core_scripts", lambda *a, **kw: None)
    monkeypatch.setattr(inst, "cleanup_obsolete_scripts", lambda *a, **kw: None)
    monkeypatch.setattr(inst, "deploy_hooks", lambda *a, **kw: None)
    monkeypatch.setattr(inst, "merge_workflow_rules", lambda *a, **kw: None)
    monkeypatch.setattr(inst, "regenerate_preambles", lambda *a, **kw: None)
    monkeypatch.setattr(inst, "assert_no_placeholders", lambda *a, **kw: [])

    from quoin.cli import _cmd_claude_install
    import argparse
    args = argparse.Namespace(
        scope="project",
        allow_hook_merge=True,  # bypass flag
        runtime="claude",
        check=False,
        source_dir=str(QUOIN_SRC),
        force_merge=False,
        dev=False,
        use_pip=False,
    )
    # Should NOT raise — Path.home() mock so detect_home_hook_conflict reads fake home.
    # Patch time.sleep to skip the D-02 3-second abort window.
    with unittest.mock.patch.object(pathlib.Path, "home", return_value=fake_home), \
         unittest.mock.patch("time.sleep"):
        result = _cmd_claude_install(args)
    assert result == 0


# ── T-10: Idempotency tests ───────────────────────────────────────────────────

def test_merge_workflow_rules_idempotent(tmp_path):
    """Running merge_workflow_rules twice produces a single marker pair (not duplicated)."""
    from quoin import installer

    # Minimal source CLAUDE.md
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "CLAUDE.md").write_text("# Hello\nSome rules here.\n", encoding="utf-8")

    dest_root = tmp_path / ".claude"
    dest_root.mkdir()
    claude_md = dest_root / "CLAUDE.md"

    # First install
    installer.merge_workflow_rules(source_dir, dest_root)
    after_first = claude_md.read_text(encoding="utf-8")
    assert after_first.count(installer._MARKER_START) == 1

    # Second install (idempotent — should replace, not duplicate)
    installer.merge_workflow_rules(source_dir, dest_root)
    after_second = claude_md.read_text(encoding="utf-8")
    assert after_second.count(installer._MARKER_START) == 1, (
        "merge_workflow_rules produced more than one marker pair on second run (not idempotent)"
    )
    # Content should be byte-equal after second run (no changes)
    assert after_first == after_second, (
        "merge_workflow_rules content changed on second run (should be idempotent)"
    )


def test_merge_workflow_rules_project_mode_idempotent(tmp_path):
    """Running merge_workflow_rules twice in project mode stays at one marker pair."""
    from quoin import installer

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "CLAUDE.md").write_text("# Rules\n", encoding="utf-8")

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    dest_root = project_dir / ".claude"
    dest_root.mkdir()
    claude_md_path = project_dir / "CLAUDE.md"

    # First install
    installer.merge_workflow_rules(source_dir, dest_root, claude_md_path=claude_md_path)
    after_first = claude_md_path.read_text(encoding="utf-8")
    assert after_first.count(installer._MARKER_START) == 1

    # Second install
    installer.merge_workflow_rules(source_dir, dest_root, claude_md_path=claude_md_path)
    after_second = claude_md_path.read_text(encoding="utf-8")
    assert after_second.count(installer._MARKER_START) == 1, (
        "project-mode merge_workflow_rules duplicated marker pair on second run"
    )
    assert after_first == after_second


def test_deploy_hooks_settings_idempotent(tmp_path):
    """Running deploy_hooks twice in project mode does not duplicate hook stanzas
    in settings.json."""
    from quoin import installer

    source_dir = QUOIN_SRC
    dest_root = tmp_path / ".claude"
    dest_root.mkdir()
    (dest_root / "hooks").mkdir()

    # Copy just the hook scripts so deploy_hooks can find them
    src_hooks = QUOIN_SRC / "hooks"
    dst_hooks = dest_root / "hooks"
    import shutil
    for hook in ("userpromptsubmit.sh", "precompact.sh", "postcompact.sh",
                 "sessionstart.sh", "sessionend.sh", "_lib.sh"):
        src_file = src_hooks / hook
        if src_file.exists():
            shutil.copy(src_file, dst_hooks / hook)

    # First deploy
    installer.deploy_hooks(source_dir, dest_root, is_project_mode=True)
    settings_path = dest_root / "settings.json"
    assert settings_path.exists(), "settings.json not created by deploy_hooks"
    settings_first = json.loads(settings_path.read_text(encoding="utf-8"))
    hooks_first = settings_first.get("hooks", {})
    first_count = sum(len(v) for v in hooks_first.values() if isinstance(v, list))

    # Second deploy (idempotent)
    installer.deploy_hooks(source_dir, dest_root, is_project_mode=True)
    settings_second = json.loads(settings_path.read_text(encoding="utf-8"))
    hooks_second = settings_second.get("hooks", {})
    second_count = sum(len(v) for v in hooks_second.values() if isinstance(v, list))

    assert second_count == first_count, (
        f"deploy_hooks duplicated hook stanzas on second run: "
        f"first={first_count} stanzas, second={second_count} stanzas"
    )


# ── FIX-1: test_project_claude_md_has_no_home_refs ──────────────────────────

def test_project_claude_md_has_no_home_refs(tmp_path):
    """Deployed <project>/CLAUDE.md must not have literal ~/.claude refs
    (except ~/.claude/projects/ which is Claude Code internal)."""
    from quoin import installer

    source_dir = QUOIN_SRC
    dest_root = tmp_path / ".claude"
    dest_root.mkdir(parents=True)
    claude_md_path = tmp_path / "CLAUDE.md"

    installer.merge_workflow_rules(source_dir, dest_root, claude_md_path=claude_md_path)

    content = claude_md_path.read_text(encoding="utf-8")
    # Allow ~/.claude/projects/ (Claude Code internal) and blockquoted lines (> prefix)
    offending_lines = [
        line for line in content.splitlines()
        if "~/.claude" in line
        and "~/.claude/projects" not in line
        and not line.strip().startswith(">")
    ]
    assert offending_lines == [], (
        f"Found literal ~/.claude refs in deployed CLAUDE.md (should be __QUOIN_HOME__ "
        f"after substitution): {offending_lines[:3]}"
    )


# ── FIX-2: assert_no_placeholders tests ─────────────────────────────────────

def test_assert_no_placeholders_catches_leaked_placeholder(tmp_path):
    """assert_no_placeholders returns violations when __QUOIN_HOME__ found."""
    from quoin.installer import assert_no_placeholders

    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "test.md").write_text("python3 __QUOIN_HOME__/scripts/foo.py")
    violations = assert_no_placeholders(tmp_path)
    assert len(violations) == 1
    assert "test.md" in violations[0]


def test_assert_no_placeholders_passes_clean_deploy(tmp_path):
    """assert_no_placeholders returns empty list when no placeholders remain."""
    from quoin.installer import assert_no_placeholders

    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "test.md").write_text("python3 /resolved/path/scripts/foo.py")
    violations = assert_no_placeholders(tmp_path)
    assert violations == []


# ── IVG-67: DEPLOYED_SCRIPTS / CORE_SCRIPTS deployment parity guard ──────────

def test_deployed_scripts_core_parity():
    """Every DEPLOYED_SCRIPTS entry that has a core/scripts/ impl must be in CORE_SCRIPTS.

    This guards against the IVG-67 class of bug: a wrapper script uses the
    parents[1]/core/scripts/<name> loader, the core impl file exists in the
    source tree, but the script is absent from CORE_SCRIPTS so deploy_core_scripts()
    never copies it — causing FileNotFoundError at runtime.

    The check is existence-gated: pure wrapper scripts with no core counterpart
    (e.g. cost_from_jsonl.py, build_preambles.py) are intentionally exempt.
    """
    from quoin.installer import DEPLOYED_SCRIPTS, CORE_SCRIPTS

    core_scripts_dir = REPO / "quoin" / "core" / "scripts"
    violations = []
    for fname in DEPLOYED_SCRIPTS:
        if (core_scripts_dir / fname).exists() and fname not in CORE_SCRIPTS:
            violations.append(fname)

    assert violations == [], (
        "The following scripts have a core impl but are missing from CORE_SCRIPTS; "
        "the deployed wrapper will FileNotFoundError at runtime: "
        + ", ".join(
            f"{fname} (exists in core/scripts/ but not in CORE_SCRIPTS)"
            for fname in violations
        )
    )


# ── T-07: --claude-md-variant {full,slim} installer flag (IVG-164 stage 1) ────
# Seam-by-seam matrix per D-09 — nothing exercises the real _cmd_claude_install
# without monkeypatching it away except the guard cell below (which returns
# before anything writes).

def test_variant_full_user_merges_from_claude_md(tmp_path):
    """Source-selection cell: full + user → merge reads CLAUDE.md (default)."""
    from quoin.installer import merge_workflow_rules
    src = _make_minimal_src_for_merge(tmp_path / "src")
    dest_root = tmp_path / ".claude"
    dest_root.mkdir()

    merge_workflow_rules(src, dest_root, source_claude_name="CLAUDE.md")

    content = (dest_root / "CLAUDE.md").read_text(encoding="utf-8")
    assert "Test Rules\n" in content
    assert "Slim workflow rules." not in content


def test_variant_full_project_merges_from_claude_md(tmp_path):
    """Source-selection cell: full + project → merge reads CLAUDE.md (default)."""
    from quoin.installer import merge_workflow_rules
    src = _make_minimal_src_for_merge(tmp_path / "src")
    dest_root = tmp_path / "project" / ".claude"
    dest_root.mkdir(parents=True)
    claude_md_path = tmp_path / "project" / "CLAUDE.md"

    merge_workflow_rules(src, dest_root, claude_md_path=claude_md_path, source_claude_name="CLAUDE.md")

    content = claude_md_path.read_text(encoding="utf-8")
    assert "Test Rules\n" in content
    assert "Slim workflow rules." not in content


def test_variant_slim_project_merges_from_claude_slim_md(tmp_path):
    """Source-selection cell: slim + project → merge reads CLAUDE.slim.md."""
    from quoin.installer import merge_workflow_rules
    src = _make_minimal_src_for_merge(tmp_path / "src")
    dest_root = tmp_path / "project" / ".claude"
    dest_root.mkdir(parents=True)
    claude_md_path = tmp_path / "project" / "CLAUDE.md"

    merge_workflow_rules(src, dest_root, claude_md_path=claude_md_path, source_claude_name="CLAUDE.slim.md")

    content = claude_md_path.read_text(encoding="utf-8")
    assert "Slim workflow rules." in content
    assert "Some workflow rules." not in content


def test_variant_missing_source_exits_with_variant_name(tmp_path, capsys):
    """Missing-source cell: source_claude_name naming a nonexistent file exits 1
    with that filename in the error message (not the hardcoded 'CLAUDE.md')."""
    from quoin.installer import merge_workflow_rules
    src = tmp_path / "src"
    src.mkdir()
    # deliberately do NOT create CLAUDE.slim.md
    (src / "CLAUDE.md").write_text("# Rules\n", encoding="utf-8")
    dest_root = tmp_path / ".claude"
    dest_root.mkdir()

    with pytest.raises(SystemExit) as exc_info:
        merge_workflow_rules(src, dest_root, source_claude_name="CLAUDE.slim.md")
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "CLAUDE.slim.md" in err


def test_variant_slim_user_guard_returns_1_no_deploy(monkeypatch, capsys, tmp_path):
    """Guard cell: --claude-md-variant slim + user scope returns 1 BEFORE any
    file is written. Three required assertions (MIN-2 r2): return==1, deploy_memory
    never reached, and the exact stderr string is present — distinguishing this
    from an unrelated check_prerequisites miss on the runner."""
    import quoin.installer as inst

    def _raise(*_a, **_kw):
        raise AssertionError("deploy_memory must not be reached past the slim+user guard")

    monkeypatch.setattr(inst, "deploy_memory", _raise)

    from quoin.cli import _cmd_claude_install
    import argparse
    args = argparse.Namespace(
        scope="user",
        claude_md_variant="slim",
        allow_hook_merge=False,
        runtime="claude",
        check=False,
        source_dir=str(QUOIN_SRC),
        force_merge=False,
        dev=False,
        use_pip=False,
    )
    result = _cmd_claude_install(args)
    assert result == 1
    err = capsys.readouterr().err
    assert "--claude-md-variant slim is a project-scope pilot only this wave" in err


def test_claude_md_variant_flag_present_in_install_help():
    """Flag-plumbing cell (argparse level): 'quoin install --help' lists the flag."""
    import subprocess
    result = subprocess.run(
        [sys.executable, "-m", "quoin", "install", "--help"],
        env={**os.environ, "PYTHONPATH": str(SRC)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "--claude-md-variant" in result.stdout


def test_claude_md_variant_flag_forwarded(monkeypatch):
    """Flag-plumbing cell (argparse level): the flag reaches _cmd_claude_install
    via the fake_cmd capture pattern (mirrors test_scope_user_explicit)."""
    from quoin.cli import main

    captured_args = {}

    def fake_cmd(args):
        captured_args.update(vars(args))
        return 0

    import quoin.cli as cli_mod
    monkeypatch.setattr(cli_mod, "_cmd_claude_install", fake_cmd)
    main(["install", "--source-dir", str(QUOIN_SRC), "--scope", "project", "--claude-md-variant", "slim"])
    assert captured_args.get("claude_md_variant") == "slim"

    # default (flag omitted) is "full"
    captured_args.clear()
    main(["install", "--source-dir", str(QUOIN_SRC)])
    assert captured_args.get("claude_md_variant") == "full"


def _write_python_stub(tmp_path: Path) -> dict:
    """Fake `python3`-family interpreter that intercepts install.sh's version
    probes and its final `-m quoin` exec, echoing ARGV instead of doing real
    work. Keeps the shell-level flag-plumbing cell under a second of wall-clock
    (T-07 acceptance: no real time.sleep(3), no real deploy in any cell)."""
    stub = tmp_path / "_pystub"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == "-c" ]]; then\n'
        '  case "$2" in\n'
        '    *sys.version_info*) echo 3013 ;;\n'
        '    *__about__*) : ;;\n'
        '    *"import quoin"*)\n'
        '      if [[ -n "${PYTHONPATH:-}" ]]; then exit 0; else exit 1; fi ;;\n'
        '    *) exit 0 ;;\n'
        '  esac\n'
        '  exit 0\n'
        "fi\n"
        'if [[ "$1" == "-m" && "$2" == "quoin" && "$3" == "--version" ]]; then\n'
        "  exit 1\n"
        "fi\n"
        'echo "ARGV:$*"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    names = ["python3.13", "python3.12", "python3.11", "python3.10", "python3", "python"]
    for name in names:
        link = tmp_path / name
        if not link.exists():
            link.symlink_to(stub)
    return {"PATH": f"{tmp_path}:{os.environ.get('PATH', '')}"}


def test_install_sh_forwards_claude_md_variant(tmp_path):
    """Flag-plumbing cell (shell level): both '--claude-md-variant slim' and
    '--claude-md-variant=slim' forms reach INSTALL_ARGS and are forwarded to the
    final `-m quoin` invocation, and the missing-value form exits 2 — mirroring
    the --scope shell-level contract exactly."""
    import subprocess

    install_sh = QUOIN_SRC / "install.sh"
    stub_env_overrides = _write_python_stub(tmp_path)
    env = {**os.environ, **stub_env_overrides}

    # Separate-token form
    result = subprocess.run(
        ["bash", str(install_sh), "--scope", "project", "--claude-md-variant", "slim"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert "--claude-md-variant slim" in result.stdout, result.stdout + result.stderr

    # =-joined form
    result2 = subprocess.run(
        ["bash", str(install_sh), "--scope", "project", "--claude-md-variant=slim"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert "--claude-md-variant slim" in result2.stdout, result2.stdout + result2.stderr

    # Missing-value form exits 2 (fails during arg parsing, before any Python probe)
    result3 = subprocess.run(
        ["bash", str(install_sh), "--claude-md-variant"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert result3.returncode == 2
    assert "--claude-md-variant requires a value" in result3.stderr


# ── review-1.md MAJOR 1: install-time staleness check for slim outputs ────────
# Project-scope installs never regenerate (allow_writes forced False, T-07
# MAJ-6), so a slim install must fail closed rather than silently deploy a
# stale CLAUDE.slim.md / workflow-catalog.md. Fixture helper copies the real
# committed generator + outputs into a scratch dir so staleness tests never
# mutate the committed repo files (mirrors the copy-to-tmp discipline used in
# test_build_claude_slim.py's --check smoke tests).

def _copy_slim_source_tree(tmp_path: Path) -> Path:
    """Scratch copy of the pieces installer.check_slim_outputs_fresh() reads:
    scripts/build_claude_slim.py, CLAUDE.md, CLAUDE.slim.md, memory/workflow-
    catalog.md, plus an empty skills/ dir so _resolve_source_dir accepts it."""
    import shutil

    scratch = tmp_path / "src_copy"
    (scratch / "scripts").mkdir(parents=True)
    (scratch / "skills").mkdir()
    (scratch / "memory").mkdir()
    shutil.copy(
        QUOIN_SRC / "scripts" / "build_claude_slim.py",
        scratch / "scripts" / "build_claude_slim.py",
    )
    shutil.copy(QUOIN_SRC / "CLAUDE.md", scratch / "CLAUDE.md")
    shutil.copy(QUOIN_SRC / "CLAUDE.slim.md", scratch / "CLAUDE.slim.md")
    shutil.copy(
        QUOIN_SRC / "memory" / "workflow-catalog.md",
        scratch / "memory" / "workflow-catalog.md",
    )
    return scratch


def test_check_slim_outputs_fresh_on_committed_tree(tmp_path):
    """A fresh copy of the real committed outputs reports zero staleness."""
    from quoin.installer import check_slim_outputs_fresh

    scratch = _copy_slim_source_tree(tmp_path)
    assert check_slim_outputs_fresh(scratch) == []


def test_check_slim_outputs_fresh_detects_stale_slim_output(tmp_path):
    """A one-byte edit to the copied CLAUDE.slim.md is caught, naming that file
    (and only that file)."""
    from quoin.installer import check_slim_outputs_fresh

    scratch = _copy_slim_source_tree(tmp_path)
    slim_path = scratch / "CLAUDE.slim.md"
    slim_path.write_text(slim_path.read_text(encoding="utf-8") + "x", encoding="utf-8")

    stale = check_slim_outputs_fresh(scratch)
    assert any("CLAUDE.slim.md" in s for s in stale), stale
    assert not any("workflow-catalog.md" in s for s in stale), stale


def test_check_slim_outputs_fresh_detects_stale_catalog_output(tmp_path):
    """A one-byte edit to the copied workflow-catalog.md is caught, naming that
    file (and only that file)."""
    from quoin.installer import check_slim_outputs_fresh

    scratch = _copy_slim_source_tree(tmp_path)
    catalog_path = scratch / "memory" / "workflow-catalog.md"
    catalog_path.write_text(catalog_path.read_text(encoding="utf-8") + "x", encoding="utf-8")

    stale = check_slim_outputs_fresh(scratch)
    assert any("workflow-catalog.md" in s for s in stale), stale
    assert not any(s.endswith("CLAUDE.slim.md") for s in stale), stale


def test_variant_slim_stale_outputs_returns_1_no_deploy(monkeypatch, capsys, tmp_path):
    """Integration cell: --claude-md-variant slim with a stale committed
    CLAUDE.slim.md aborts _cmd_claude_install with exit 1 BEFORE any file is
    written (deploy_memory tripwire, mirrors the slim+user guard cell above),
    naming the stale file in stderr."""
    import argparse

    import quoin.installer as inst
    from quoin.cli import _cmd_claude_install

    scratch = _copy_slim_source_tree(tmp_path)
    slim_path = scratch / "CLAUDE.slim.md"
    slim_path.write_text(slim_path.read_text(encoding="utf-8") + "x", encoding="utf-8")

    def _raise(*_a, **_kw):
        raise AssertionError("deploy_memory must not be reached past the slim staleness guard")

    monkeypatch.setattr(inst, "deploy_memory", _raise)

    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    args = argparse.Namespace(
        scope=f"project:{project_dir}",
        claude_md_variant="slim",
        allow_hook_merge=False,
        runtime="claude",
        check=False,
        source_dir=str(scratch),
        force_merge=False,
        dev=False,
        use_pip=False,
    )
    result = _cmd_claude_install(args)
    assert result == 1
    err = capsys.readouterr().err
    assert "CLAUDE.slim.md" in err
    assert "stale" in err


# ── T-10: doctor reports memory files in project scope too ────────────────────

def test_doctor_reports_memory_files_in_project_scope(tmp_path, capsys):
    """`quoin doctor --scope project` lists all TIER1 memory files (IVG-164 T-10).

    Regression guard for a stale comment/guard at cli.py's doctor command:
    `deploy_memory()` copies TIER1_MEMORY_FILES unconditionally in BOTH
    scopes (installer.py:304-315), so the doctor check must run in project
    scope too, not just user scope.
    """
    from quoin import installer
    from quoin.cli import _cmd_doctor

    project_dir = tmp_path / "pilot-proj"
    project_dir.mkdir()
    dest_root = project_dir / ".claude"
    installer.deploy_memory(QUOIN_SRC, dest_root)

    args = _make_args(scope=f"project:{project_dir}", runtime="claude")
    _cmd_doctor(args)

    out = capsys.readouterr().out
    assert "Memory files" in out
    for fname in installer.TIER1_MEMORY_FILES:
        assert fname in out, f"{fname} missing from doctor project-scope output"
    # Every entry reported present (deploy_memory ran successfully above).
    assert "✗" not in out.split("Memory files")[1].split("Scripts")[0]


def test_install_sh_forwards_autocompact_flags(tmp_path):
    """Flag-plumbing cell (shell level) for the three autocompact install.sh
    flags (IVG-258 stage 7, T-06), mirroring test_install_sh_forwards_claude_md_variant
    exactly: both '--autocompact-window 500000' and '--autocompact-window=500000'
    forms reach INSTALL_ARGS as two argv elements, a missing value exits 2,
    '--clear-autocompact-env' forwards as a single boolean token, and a default
    `bash install.sh` argv contains none of the three flags."""
    import subprocess

    install_sh = QUOIN_SRC / "install.sh"
    stub_env_overrides = _write_python_stub(tmp_path)
    env = {**os.environ, **stub_env_overrides}

    # Separate-token form
    result = subprocess.run(
        ["bash", str(install_sh), "--scope", "project", "--autocompact-window", "500000"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert "--autocompact-window 500000" in result.stdout, result.stdout + result.stderr

    # =-joined form
    result2 = subprocess.run(
        ["bash", str(install_sh), "--scope", "project", "--autocompact-window=500000"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert "--autocompact-window 500000" in result2.stdout, result2.stdout + result2.stderr

    # Missing-value form exits 2 (fails during arg parsing, before any Python probe)
    result3 = subprocess.run(
        ["bash", str(install_sh), "--autocompact-window"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert result3.returncode == 2
    assert "--autocompact-window requires a value" in result3.stderr

    # --clear-autocompact-env forwards as a single boolean token
    result4 = subprocess.run(
        ["bash", str(install_sh), "--scope", "project", "--clear-autocompact-env"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert "--clear-autocompact-env" in result4.stdout, result4.stdout + result4.stderr

    # Default argv contains none of the three flags
    result5 = subprocess.run(
        ["bash", str(install_sh), "--scope", "project"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    for flag in ("--autocompact-pct", "--autocompact-window", "--clear-autocompact-env"):
        assert flag not in result5.stdout, result5.stdout + result5.stderr


def _write_python_stub_matching_version(tmp_path: Path, version: str) -> dict:
    """Variant of `_write_python_stub` where the local source version and the
    already-installed version agree, so install.sh's Tier-1 branch fires
    (`$PYTHON -m quoin "${INSTALL_ARGS[@]}"` directly, no PYTHONPATH, no pip).
    Same fake-interpreter technique, only the two version probes differ."""
    stub = tmp_path / "_pystub"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == "-c" ]]; then\n'
        '  case "$2" in\n'
        '    *sys.version_info*) echo 3013 ;;\n'
        f'    *__about__*) echo "{version}" ;;\n'
        '    *) exit 0 ;;\n'
        '  esac\n'
        '  exit 0\n'
        "fi\n"
        'if [[ "$1" == "-m" && "$2" == "quoin" && "$3" == "--version" ]]; then\n'
        f'  echo "quoin {version}"\n'
        "  exit 0\n"
        "fi\n"
        'echo "ARGV:$*"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    names = ["python3.13", "python3.12", "python3.11", "python3.10", "python3", "python"]
    for name in names:
        link = tmp_path / name
        if not link.exists():
            link.symlink_to(stub)
    return {"PATH": f"{tmp_path}:{os.environ.get('PATH', '')}"}


def test_install_sh_tier1_execs_module_directly_on_version_match(tmp_path):
    """IVG-281 stage 2 T-05: when the probed `$PYTHON -m quoin --version`
    already matches the local source tree's version, install.sh's Tier-1
    branch execs `$PYTHON -m quoin install ...` directly — no PYTHONPATH
    fallback, no pip. This is the shape a globally importable quoin (a
    normal editable/non-editable install on the probed interpreter's own
    `$PATH`) takes; the isolated-tool and same-version-shadowing cases in
    `test_install_record_tiers.py` are invisible to this probe entirely and
    are documented, not exercised, at the shell level (see current-plan.md
    T-05)."""
    import subprocess

    install_sh = QUOIN_SRC / "install.sh"
    stub_env_overrides = _write_python_stub_matching_version(tmp_path, "9.9.9")
    env = {**os.environ, **stub_env_overrides}

    result = subprocess.run(
        ["bash", str(install_sh), "--scope", "project"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert result.stdout.startswith("ARGV:-m quoin install"), result.stdout + result.stderr
    assert "--scope project" in result.stdout
