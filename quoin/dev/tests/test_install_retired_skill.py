"""Upgrading an install removes what quoin no longer ships.

The /checkpoint skill, its skillOverrides key, the pidfile helper script and
the checkpoint spec memory file are reclaimed on reinstall, but only when they
are quoin's own copies; a same-named skill from elsewhere, a user-changed
override and every foreign hook stanza are left exactly as they were.
"""
import json
from pathlib import Path

from quoin import installer
from quoin.installer import deploy_hooks

REPO_ROOT = Path(__file__).resolve().parents[3]
QUOIN_SRC = REPO_ROOT / "quoin"

QUOIN_SKILL_MD = (
    "---\nname: checkpoint\ndescription: Saves session-restore state (quoin)\n---\n"
    "# Checkpoint\nbody\n"
)


def _tree(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def _stanza(command, matcher="*", timeout=5):
    return {"matcher": matcher, "hooks": [{"type": "command", "command": command, "timeout": timeout}]}


def test_quoin_written_skill_dir_is_removed_with_its_backup(tmp_path):
    skill = tmp_path / "skills" / "checkpoint"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(QUOIN_SKILL_MD, encoding="utf-8")
    (skill / "SKILL.md.pre-T11").write_text("older copy", encoding="utf-8")
    installer.cleanup_obsolete_skills(tmp_path)
    assert not skill.exists()


def test_foreign_skill_dir_survives_byte_equal(tmp_path, capsys):
    skill = tmp_path / "skills" / "checkpoint"
    skill.mkdir(parents=True)
    foreign = "---\nname: checkpoint\ndescription: my own snapshot helper\n---\nmine\n"
    (skill / "SKILL.md").write_text(foreign, encoding="utf-8")
    before = _tree(tmp_path)
    installer.cleanup_obsolete_skills(tmp_path)
    assert _tree(tmp_path) == before
    assert "leaving" in capsys.readouterr().out


def test_dir_without_skill_md_is_left_alone(tmp_path):
    skill = tmp_path / "skills" / "checkpoint"
    skill.mkdir(parents=True)
    (skill / "notes.txt").write_text("keep", encoding="utf-8")
    installer.cleanup_obsolete_skills(tmp_path)
    assert (skill / "notes.txt").read_text(encoding="utf-8") == "keep"


def test_override_key_removed_only_while_it_holds_the_quoin_value():
    settings = {"skillOverrides": {"checkpoint": "on", "foo": "on"}}
    installer._merge_skill_overrides(settings)
    assert "checkpoint" not in settings["skillOverrides"]
    assert settings["skillOverrides"]["foo"] == "on"

    user_choice = {"skillOverrides": {"checkpoint": "name-only"}}
    installer._merge_skill_overrides(user_choice)
    assert user_choice["skillOverrides"]["checkpoint"] == "name-only"


def test_list_typed_override_does_not_crash_and_is_unchanged():
    settings = {"skillOverrides": ["checkpoint"]}
    installer._merge_skill_overrides(settings)
    assert settings == {"skillOverrides": ["checkpoint"]}


def test_stale_script_and_memory_file_are_removed(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "memory").mkdir()
    (tmp_path / "scripts" / "pidfile_helpers.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (tmp_path / "memory" / "checkpoint-spec.md").write_text("spec\n", encoding="utf-8")
    (tmp_path / "memory" / "lifecycle-guide.md").write_text("keep\n", encoding="utf-8")
    installer.cleanup_obsolete_scripts(tmp_path)
    installer.cleanup_obsolete_memory(tmp_path)
    assert not (tmp_path / "scripts" / "pidfile_helpers.sh").exists()
    assert not (tmp_path / "memory" / "checkpoint-spec.md").exists()
    assert (tmp_path / "memory" / "lifecycle-guide.md").read_text(encoding="utf-8") == "keep\n"


def test_second_pass_is_a_no_op(tmp_path):
    skill = tmp_path / "skills" / "checkpoint"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(QUOIN_SKILL_MD, encoding="utf-8")
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "checkpoint-spec.md").write_text("spec\n", encoding="utf-8")
    for _ in range(1):
        installer.cleanup_obsolete_skills(tmp_path)
        installer.cleanup_obsolete_memory(tmp_path)
    first = _tree(tmp_path)
    installer.cleanup_obsolete_skills(tmp_path)
    installer.cleanup_obsolete_memory(tmp_path)
    assert _tree(tmp_path) == first


def test_foreign_hook_stanzas_are_untouched_by_upgrade(tmp_path):
    dest = tmp_path / ".claude"
    dest.mkdir()
    hooks_dir = dest.resolve() / "hooks"
    deck = _stanza("'/Users/x/Library/QuoinStreamDeck/bin/quoin-deck-hook' claude", matcher="", timeout=3)
    plugin = _stanza("/opt/plugins/context-tracker/hooks/track.sh")
    other = _stanza("/opt/tools/echo_stdin_smoke.sh")
    settings = {
        "skillOverrides": {"checkpoint": "on"},
        "hooks": {
            "UserPromptSubmit": [other, deck, plugin],
            "SessionStart": [deck, other],
        },
    }
    (dest / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    deploy_hooks(QUOIN_SRC, dest)
    after = json.loads((dest / "settings.json").read_text(encoding="utf-8"))
    for event, foreign in (("UserPromptSubmit", [other, deck, plugin]), ("SessionStart", [deck, other])):
        rest = [s for s in after["hooks"][event]
                if not any(h["command"].startswith(str(hooks_dir)) for h in s["hooks"])]
        assert rest == foreign


def test_cli_runs_the_obsolete_cleanups_after_the_deploys():
    cli_text = (REPO_ROOT / "src" / "quoin" / "cli.py").read_text(encoding="utf-8")
    deploys = cli_text.index("installer.deploy_dashboard_assets(")
    for call in ("cleanup_obsolete_scripts", "cleanup_obsolete_skills", "cleanup_obsolete_memory"):
        assert cli_text.index(f"installer.{call}(dest_root)") > deploys


def test_files_removed_before_the_manifest_existed_are_deleted(tmp_path):
    core = tmp_path / "core" / "scripts"
    core.mkdir(parents=True)
    (core / "find_drive_conflicts.py").write_text("old\n", encoding="utf-8")
    (core / "path_resolve.py").write_text("keep\n", encoding="utf-8")
    installer.remove_pre_manifest_files(tmp_path)
    assert not (core / "find_drive_conflicts.py").exists()
    assert (core / "path_resolve.py").read_text(encoding="utf-8") == "keep\n"


# ── manifest-based pruning ───────────────────────────────────────────────────

def _deploy(dest, rel, text):
    path = dest / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return rel, installer._sha256_file(path)


def test_prune_removes_unchanged_files_and_keeps_edited_ones(tmp_path, capsys):
    previous = dict([
        _deploy(tmp_path, "scripts/gone.py", "old\n"),
        _deploy(tmp_path, "scripts/edited.py", "old\n"),
        _deploy(tmp_path, "skills/retired/SKILL.md", "quoin skill\n"),
        _deploy(tmp_path, "scripts/kept.py", "current\n"),
    ])
    (tmp_path / "scripts" / "edited.py").write_text("mine\n", encoding="utf-8")
    current = {"scripts/kept.py": previous["scripts/kept.py"]}

    pruned, retry = installer.prune_stale_deploys(tmp_path, previous, current)

    assert pruned == ["scripts/gone.py", "skills/retired/SKILL.md"]
    assert retry == {}
    assert not (tmp_path / "scripts" / "gone.py").exists()
    assert not (tmp_path / "skills" / "retired").exists()
    assert (tmp_path / "skills").is_dir()
    assert (tmp_path / "scripts" / "edited.py").read_text(encoding="utf-8") == "mine\n"
    assert (tmp_path / "scripts" / "kept.py").exists()
    assert "keeping" in capsys.readouterr().err


def test_prune_never_touches_paths_outside_quoin_subdirs(tmp_path):
    previous = dict([
        _deploy(tmp_path, "settings.json", "{}\n"),
        _deploy(tmp_path, "CLAUDE.md", "rules\n"),
        _deploy(tmp_path, "QUICKSTART.md", "card\n"),
        _deploy(tmp_path, "projects/x/notes.md", "n\n"),
    ])
    previous["../outside.txt"] = "0" * 64
    previous.update([_deploy(tmp_path, "skills/context-tracker/hooks.json", "{}\n")])
    before = _tree(tmp_path)
    pruned, retry = installer.prune_stale_deploys(
        tmp_path, previous, {}, protected=("skills/context-tracker/",))
    assert pruned == []
    assert list(retry) == ["skills/context-tracker/hooks.json"]
    assert _tree(tmp_path) == before


def _project_install(project, *, no_prune=False):
    import argparse
    import unittest.mock

    from quoin import cli

    args = argparse.Namespace(
        runtime="claude", scope=f"project:{project}", allow_hook_merge=True, check=False,
        source_dir=str(QUOIN_SRC), force_merge=False, dev=False, use_pip=False,
        no_prune=no_prune,
    )
    with unittest.mock.patch.object(installer, "check_prerequisites", lambda: []), \
            unittest.mock.patch("time.sleep"), \
            unittest.mock.patch("shutil.which", lambda *a, **k: None):
        assert cli._cmd_install(args) == 0
    return project / ".claude"


def test_reinstall_prunes_a_script_dropped_from_the_deploy_list(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)
    project = tmp_path / "project"
    project.mkdir()
    dest = _project_install(project)
    manifest = installer.read_deploy_manifest(dest)
    assert manifest["scripts/validate_artifact.py"] == installer._sha256_file(
        dest / "scripts" / "validate_artifact.py")
    (dest / "skills" / "synced").mkdir()
    (dest / "skills" / "synced" / "SKILL.md").write_text("not quoin\n", encoding="utf-8")

    dropped = installer.DEPLOYED_SCRIPTS[0]
    monkeypatch.setattr(installer, "DEPLOYED_SCRIPTS", installer.DEPLOYED_SCRIPTS[1:])
    _project_install(project, no_prune=True)
    assert (dest / "scripts" / dropped).exists()
    assert f"scripts/{dropped}" in installer.read_deploy_manifest(dest)

    _project_install(project)
    assert not (dest / "scripts" / dropped).exists()
    assert f"scripts/{dropped}" not in installer.read_deploy_manifest(dest)
    assert (dest / "skills" / "synced" / "SKILL.md").read_text(encoding="utf-8") == "not quoin\n"
    assert (dest / "settings.json").exists()
