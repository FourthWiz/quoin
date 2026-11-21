"""build_agent_skills.py: the kept skills as a plain Agent Skills folder."""

import re
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PKG / "core" / "scripts"))
import build_agent_skills as bas  # noqa: E402

NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def test_builds_every_kept_skill_with_standard_names_and_frontmatter(tmp_path):
    out = tmp_path / ".agents" / "skills"
    result = bas.build(out, PKG)
    assert result["skills"] == [bas.agent_name(s) for s in bas.KEPT_SKILLS]
    for name in result["skills"]:
        text = (out / name / "SKILL.md").read_text()
        assert NAME_RE.match(name)
        head, body = text.split("\n---\n", 1)
        lines = head.splitlines()[1:]
        assert [line.split(":", 1)[0] for line in lines] == ["name", "description"]
        assert lines[0] == f"name: {name}"
        assert "${CLAUDE_PLUGIN_ROOT}" not in text


def test_plugin_root_points_at_the_support_folder(tmp_path):
    out = tmp_path / "skills"
    bas.build(out, PKG)
    support = out / bas.SUPPORT
    assert (support / "scripts").is_dir() and (support / "core" / "scripts").is_dir() and (support / "memory").is_dir()
    run = (out / "run" / "SKILL.md").read_text()
    assert str(support) in run
    assert (out / "run" / "light-pipeline.md").is_file()
    assert "${CLAUDE_PLUGIN_ROOT}" not in (out / "run" / "light-pipeline.md").read_text()


def test_rebuild_replaces_the_previous_folder(tmp_path):
    out = tmp_path / "skills"
    bas.build(out, PKG)
    (out / "run" / "stale.md").write_text("old")
    bas.build(out, PKG)
    assert not (out / "run" / "stale.md").exists()


def test_names_follow_the_agent_skills_rule():
    assert bas.agent_name("init_workflow") == "init-workflow"
    assert bas.agent_name("review-cross") == "review-cross"
