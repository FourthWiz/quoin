"""Unit tests for skillOverrides injection via deploy_hooks() and _merge_skill_overrides()."""
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
QUOIN_SRC = REPO_ROOT / "quoin"

from quoin.installer import SKILL_OVERRIDES, _merge_skill_overrides, deploy_hooks  # noqa: E402


class TestSkillOverridesInstall:

    def test_fresh_install_sets_correct_overrides(self, tmp_path):
        dest = tmp_path / ".claude"
        dest.mkdir()
        deploy_hooks(QUOIN_SRC, dest)
        settings = json.loads((dest / "settings.json").read_text())
        overrides = settings.get("skillOverrides", {})
        for skill, tier in SKILL_OVERRIDES.items():
            assert overrides.get(skill) == tier, (
                f"Expected skillOverrides[{skill!r}] == {tier!r}, got {overrides.get(skill)!r}"
            )

    def test_skill_overrides_idempotent_on_reinstall(self, tmp_path):
        dest = tmp_path / ".claude"
        dest.mkdir()
        deploy_hooks(QUOIN_SRC, dest)
        first = json.loads((dest / "settings.json").read_text()).get("skillOverrides", {})
        deploy_hooks(QUOIN_SRC, dest)
        second = json.loads((dest / "settings.json").read_text()).get("skillOverrides", {})
        assert first == second
        assert len(second) == len(SKILL_OVERRIDES)

    def test_user_added_non_canonical_overrides_preserved(self, tmp_path):
        dest = tmp_path / ".claude"
        dest.mkdir()
        existing = {"skillOverrides": {"my_custom_skill": "off", "some_other_skill": "name-only"}}
        (dest / "settings.json").write_text(json.dumps(existing))
        deploy_hooks(QUOIN_SRC, dest)
        overrides = json.loads((dest / "settings.json").read_text()).get("skillOverrides", {})
        assert overrides.get("my_custom_skill") == "off"
        assert overrides.get("some_other_skill") == "name-only"
        for skill, tier in SKILL_OVERRIDES.items():
            assert overrides.get(skill) == tier

    def test_user_changed_canonical_overrides_reset_to_canonical(self, tmp_path):
        dest = tmp_path / ".claude"
        dest.mkdir()
        existing = {"skillOverrides": {"thorough_plan": "off", "run": "name-only", "end_of_day": "on"}}
        (dest / "settings.json").write_text(json.dumps(existing))
        deploy_hooks(QUOIN_SRC, dest)
        overrides = json.loads((dest / "settings.json").read_text()).get("skillOverrides", {})
        assert overrides["thorough_plan"] == "name-only"
        assert overrides["run"] == "on"
        assert overrides["end_of_day"] == "name-only"

    def test_merge_skill_overrides_returns_changed_count(self):
        settings = {}
        count = _merge_skill_overrides(settings)
        assert count == len(SKILL_OVERRIDES)

    def test_merge_skill_overrides_idempotent_returns_zero(self):
        settings = {}
        _merge_skill_overrides(settings)
        count = _merge_skill_overrides(settings)
        assert count == 0

    def test_merge_skill_overrides_preserves_user_keys(self):
        settings = {"skillOverrides": {"user_skill": "off"}}
        _merge_skill_overrides(settings)
        assert settings["skillOverrides"]["user_skill"] == "off"

    def test_deploy_hooks_prints_skill_overrides_summary(self, tmp_path, capsys):
        dest = tmp_path / ".claude"
        dest.mkdir()
        deploy_hooks(QUOIN_SRC, dest)
        out = capsys.readouterr().out
        assert "skill overrides" in out.lower()
        assert str(len(SKILL_OVERRIDES)) in out
