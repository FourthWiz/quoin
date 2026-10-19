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


def test_stale_script_and_memory_file_are_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(installer, "OBSOLETE_MEMORY_FILES", ("checkpoint-spec.md",))
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


def test_second_pass_is_a_no_op(tmp_path, monkeypatch):
    monkeypatch.setattr(installer, "OBSOLETE_MEMORY_FILES", ("checkpoint-spec.md",))
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
