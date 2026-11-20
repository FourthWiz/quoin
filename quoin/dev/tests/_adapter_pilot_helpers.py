"""Shared helper for adapter-pilot tests.

Provides ``assert_installer_selects_adapter(skill_name)``: the plugin ships the
Claude adapter SKILL.md for a given skill. The plugin manifest points Claude
Code at ``adapters/claude/skills/``, so the skill is shipped when its folder
holds a SKILL.md whose frontmatter name matches the folder.

Import form: top-level ``import _adapter_pilot_helpers``. The underscore prefix
keeps pytest from collecting it.
"""
import json
import pathlib
import re

_THIS = pathlib.Path(__file__).resolve()
PKG_DIR = _THIS.parent.parent.parent  # quoin/quoin/


def assert_installer_selects_adapter(skill_name: str) -> None:
    """Assert the plugin ships the Claude adapter SKILL.md for skill_name."""
    manifest = json.loads((PKG_DIR / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert manifest.get("skills") == "./adapters/claude/skills/", manifest.get("skills")

    adapter_path = PKG_DIR / "adapters" / "claude" / "skills" / skill_name / "SKILL.md"
    assert adapter_path.is_file(), f"Adapter SKILL.md missing for {skill_name!r}: {adapter_path}"

    head = adapter_path.read_text(encoding="utf-8").split("\n---", 1)[0]
    match = re.search(r"^name:\s*(\S+)\s*$", head, re.M)
    assert match and match.group(1) == skill_name, (
        f"{adapter_path} frontmatter name is {match.group(1) if match else None!r}, expected {skill_name!r}"
    )
