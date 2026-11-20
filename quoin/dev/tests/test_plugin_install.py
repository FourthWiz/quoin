"""The quoin plugin: staging, install through the claude CLI, retiring the old
file-by-file ~/.claude deployment, and the plugin's own manifest and hooks."""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import stat
import subprocess
from pathlib import Path

import pytest

from quoin import __about__, installer

REPO = Path(__file__).resolve().parents[3]
PLUGIN_SRC = REPO / "quoin"


# ── manifest and hooks ───────────────────────────────────────────────────────

def test_plugin_version_tracks_the_package():
    data = json.loads((PLUGIN_SRC / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert data["name"] == installer.PLUGIN_NAME
    assert data["version"] == __about__.__version__
    assert data["skills"] == "./adapters/claude/skills/"
    assert data["hooks"] == "./hooks/hooks.json"


def test_marketplace_lists_the_plugin_from_its_own_folder():
    data = json.loads((PLUGIN_SRC / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    assert data["name"] == installer.MARKETPLACE_NAME
    assert [(p["name"], p["source"]) for p in data["plugins"]] == [(installer.PLUGIN_NAME, "./")]


def test_every_hook_command_names_an_executable_script():
    hooks = json.loads((PLUGIN_SRC / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    scripts = set()
    for stanzas in hooks.values():
        for stanza in stanzas:
            for hook in stanza["hooks"]:
                command = shlex.split(hook["command"])[0]
                assert command.startswith("${CLAUDE_PLUGIN_ROOT}/hooks/"), command
                scripts.add(command.rsplit("/", 1)[1])
    for name in scripts:
        path = PLUGIN_SRC / "hooks" / name
        assert path.is_file(), name
        assert path.stat().st_mode & stat.S_IXUSR, f"{name} is not executable"


def test_rules_hook_injects_the_short_rules_with_the_real_root(tmp_path):
    root = tmp_path / "plugin"
    (root / "hooks").mkdir(parents=True)
    hook = root / "hooks" / "rules.sh"
    hook.write_bytes((PLUGIN_SRC / "hooks" / "rules.sh").read_bytes())
    hook.chmod(0o755)
    (root / "CLAUDE.slim.md").write_text("Rules. Catalog: ${CLAUDE_PLUGIN_ROOT}/memory/workflow-catalog.md\n")
    out = subprocess.run([str(hook)], input="{}", capture_output=True, text=True, timeout=30)
    assert out.returncode == 0
    payload = json.loads(out.stdout)["hookSpecificOutput"]
    assert payload["hookEventName"] == "SessionStart"
    assert payload["additionalContext"] == f"Rules. Catalog: {root}/memory/workflow-catalog.md\n"


def test_rules_hook_is_silent_without_the_rules_file(tmp_path):
    (tmp_path / "hooks").mkdir()
    hook = tmp_path / "hooks" / "rules.sh"
    hook.write_bytes((PLUGIN_SRC / "hooks" / "rules.sh").read_bytes())
    hook.chmod(0o755)
    out = subprocess.run([str(hook)], input="{}", capture_output=True, text=True, timeout=30)
    assert out.returncode == 0 and out.stdout == ""


def test_short_rules_fit_the_hook_context_cap():
    assert len((PLUGIN_SRC / "CLAUDE.slim.md").read_text(encoding="utf-8")) < 10_000


# ── staging ──────────────────────────────────────────────────────────────────

def test_stage_copies_only_runtime_parts_and_stamps_a_content_version(tmp_path):
    stage = tmp_path / "stage"
    version = installer.stage_plugin(PLUGIN_SRC, stage)
    base, digest = version.split("+")
    assert base == __about__.__version__ and len(digest) == 12
    assert json.loads((stage / ".claude-plugin" / "plugin.json").read_text())["version"] == version
    assert sorted(p.name for p in stage.iterdir()) == sorted(
        p.split("/")[0] for p in installer.PLUGIN_PATHS
    )
    assert not list(stage.rglob("__pycache__"))
    assert (stage / "hooks" / "sessionend.sh").stat().st_mode & stat.S_IXUSR


def test_restaging_the_same_tree_keeps_the_version_and_a_change_moves_it(tmp_path):
    src = tmp_path / "src"
    for rel in installer.PLUGIN_PATHS:
        source = PLUGIN_SRC / rel
        target = src / rel
        if source.is_dir():
            target.mkdir(parents=True)
            (target / "keep.txt").write_text(rel)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(rel)
    (src / ".claude-plugin" / "plugin.json").write_text('{"name": "quoin", "version": "1.2.3"}')
    stage = tmp_path / "stage"
    first = installer.stage_plugin(src, stage)
    assert installer.stage_plugin(src, stage) == first
    (src / "scripts" / "keep.txt").write_text("changed")
    second = installer.stage_plugin(src, stage)
    assert second != first and second.startswith("1.2.3+")
    assert (stage / "scripts" / "keep.txt").read_text() == "changed"


def test_staging_refuses_an_incomplete_source(tmp_path):
    with pytest.raises(installer.InstallError, match="missing plugin files"):
        installer.stage_plugin(tmp_path, tmp_path / "stage")


def test_default_stage_dir_honours_the_override(tmp_path, monkeypatch):
    monkeypatch.setenv(installer.STAGE_DIR_ENV, str(tmp_path / "x"))
    assert installer.default_stage_dir() == tmp_path / "x"
    monkeypatch.delenv(installer.STAGE_DIR_ENV)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert installer.default_stage_dir() == tmp_path / "data" / "quoin" / "plugin"


# ── install through the claude CLI ───────────────────────────────────────────

def _fake_claude(tmp_path, monkeypatch, *, listed: list) -> Path:
    log = tmp_path / "claude.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "claude"
    script.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> '{log}'\n"
        "case \"$*\" in\n"
        f"  'plugin list --json') echo '{json.dumps(listed)}' ;;\n"
        "esac\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return log


def test_first_install_adds_the_marketplace_and_installs(tmp_path, monkeypatch):
    log = _fake_claude(tmp_path, monkeypatch, listed=[])
    assert installer.install_plugin(tmp_path / "stage") == "install"
    assert log.read_text().splitlines() == [
        f"plugin marketplace add {tmp_path / 'stage'} --scope user",
        "plugin marketplace update quoin",
        "plugin list --json",
        "plugin install quoin@quoin --scope user",
    ]


def test_reinstall_updates(tmp_path, monkeypatch):
    log = _fake_claude(tmp_path, monkeypatch, listed=[{"id": "quoin@quoin", "scope": "user"}])
    assert installer.install_plugin(tmp_path / "stage") == "update"
    assert log.read_text().splitlines()[-1] == "plugin update quoin@quoin --scope user"


def test_a_project_install_is_not_mistaken_for_a_user_install(tmp_path, monkeypatch):
    _fake_claude(tmp_path, monkeypatch, listed=[{"id": "quoin@quoin", "scope": "project"}])
    assert installer.install_plugin(tmp_path / "stage") == "install"


def test_a_failing_cli_call_is_reported(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "claude").write_text("#!/bin/sh\necho broken >&2\nexit 3\n")
    (bin_dir / "claude").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    with pytest.raises(installer.InstallError, match="marketplace add.*exit 3.*broken"):
        installer.install_plugin(tmp_path / "stage")


def test_no_claude_cli_is_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(installer.InstallError, match="not on PATH"):
        installer.install_plugin(tmp_path / "stage")


# ── retiring the old deployment ──────────────────────────────────────────────

def _legacy(dest: Path) -> dict:
    """An earlier file-by-file install: files, manifest, hook stanzas, rules block."""
    files = {
        "scripts/a.py": "a\n",
        "core/scripts/b.py": "b\n",
        "skills/run/SKILL.md": "run\n",
        "memory/m.md": "m\n",
        "QUICKSTART.md": "q\n",
        "skills/workflow-tasks/hooks/tasks.ts": "mod\n",
    }
    manifest = {}
    for rel, text in files.items():
        path = dest / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        manifest[rel] = hashlib.sha256(text.encode()).hexdigest()
    (dest / installer.MANIFEST_FILENAME).write_text(json.dumps({"schema": 1, "files": manifest}))
    hooks_dir = dest.resolve() / "hooks"
    hooks_dir.mkdir()
    settings = {
        "model": "opus",
        "hooks": {
            "SessionStart": [
                {"matcher": "startup", "hooks": [{"type": "command", "command": f"{hooks_dir}/sessionstart.sh"}]},
                {"matcher": "startup", "hooks": [{"type": "command", "command": "/opt/other.sh"}]},
            ],
            "WorktreeCreate": [
                {"matcher": "*", "hooks": [{"type": "command", "command": f"{hooks_dir}/worktreecreate.sh"}]},
            ],
        },
    }
    (dest / "settings.json").write_text(json.dumps(settings))
    (dest / "CLAUDE.md").write_text(
        "# mine\n- keep\n\n# === DEV WORKFLOW START ===\nrules\n# === DEV WORKFLOW END ===\n"
    )
    return files


def test_legacy_cleanup_removes_what_quoin_wrote_and_keeps_the_rest(tmp_path):
    dest = tmp_path / ".claude"
    _legacy(dest)
    (dest / "memory" / "m.md").write_text("edited by hand\n")
    assert installer.legacy_deployment_present(dest, dest / "CLAUDE.md")

    result = installer.remove_legacy_deployment(dest, dest / "CLAUDE.md")

    assert result.had_manifest and result.removed_files == 4
    assert result.kept_files == (str(dest / "memory" / "m.md"),)
    assert not (dest / "scripts").exists() and not (dest / "core").exists()
    assert not (dest / "skills" / "run").exists() and not (dest / "QUICKSTART.md").exists()
    assert (dest / "skills" / "workflow-tasks" / "hooks" / "tasks.ts").exists()
    assert not (dest / installer.MANIFEST_FILENAME).exists()

    settings = json.loads((dest / "settings.json").read_text())
    assert result.removed_hook_stanzas == 2
    assert settings["model"] == "opus"
    assert settings["hooks"] == {
        "SessionStart": [{"matcher": "startup", "hooks": [{"type": "command", "command": "/opt/other.sh"}]}]
    }
    assert list(dest.glob("settings.json.bak-*"))

    assert result.removed_rules_block
    assert (dest / "CLAUDE.md").read_text() == "# mine\n- keep\n"
    assert not installer.legacy_deployment_present(dest, dest / "CLAUDE.md")


def test_legacy_cleanup_is_a_no_op_on_a_clean_folder(tmp_path):
    dest = tmp_path / ".claude"
    dest.mkdir()
    (dest / "settings.json").write_text('{"model": "opus"}')
    before = (dest / "settings.json").read_text()
    result = installer.remove_legacy_deployment(dest, dest / "CLAUDE.md")
    assert result == installer.LegacyCleanup(0, (), 0, False, False)
    assert (dest / "settings.json").read_text() == before
    assert not list(dest.glob("settings.json.bak-*"))


def test_a_hook_outside_the_old_hooks_folder_is_not_ours(tmp_path):
    dest = tmp_path / ".claude"
    dest.mkdir()
    stanza = {"matcher": "*", "hooks": [{"type": "command", "command": "/elsewhere/hooks/stop.sh"}]}
    (dest / "settings.json").write_text(json.dumps({"hooks": {"Stop": [stanza]}}))
    assert installer.remove_legacy_deployment(dest, dest / "CLAUDE.md").removed_hook_stanzas == 0
    assert json.loads((dest / "settings.json").read_text())["hooks"]["Stop"] == [stanza]
