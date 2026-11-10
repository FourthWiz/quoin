"""Contract for /run's Size step, its direct route and the route tags.

/run's tags are read by the session from SKILL.md prose, so the parsing rules
are pinned here as text. The brief's Size block is also fed through the
benchmark harness's reader, so the format /run writes and the format the
harness parses cannot drift apart.
"""
from __future__ import annotations

import re
from pathlib import Path

from quoin.benchmarks.harness.cells.quoin_claude import brief_route

QUOIN = Path(__file__).resolve().parents[2]
SKILL = (QUOIN / "adapters" / "claude" / "skills" / "run" / "SKILL.md").read_text(encoding="utf-8")
INTENT = (QUOIN / "core" / "skills" / "run.md").read_text(encoding="utf-8")
QUICKSTART = (QUOIN / "QUICKSTART.md").read_text(encoding="utf-8")

# Tags /run already strips or acts on; the route tags must not reuse any of them.
EXISTING_TAGS = ("small:", "medium:", "large:", "fast:", "hard:", "strict:", "parallel:", "max_rounds:")
ROUTE_TAGS = ("direct:", "light:")


def _raw_section(heading):
    """The text under a `## ` heading, up to the next one outside a code fence."""
    lines = SKILL[SKILL.index(heading):].splitlines()
    out, fenced = [lines[0]], False
    for line in lines[1:]:
        if line.startswith("```"):
            fenced = not fenced
        elif line.startswith("## ") and not fenced:
            break
        out.append(line)
    return "\n".join(out) + "\n"


def _section(heading):
    """Like `_raw_section`, with whitespace collapsed so rewrapping prose does not matter."""
    return " ".join(_raw_section(heading).split())


def test_route_tags_do_not_collide_with_tags_step0_already_handles():
    step0 = _raw_section("## Step 0 — Route")
    handled = set(re.findall(r"`([a-z_]+:)`", step0.split("**`direct:`** / **`light:`**")[0])) - set(ROUTE_TAGS)
    assert set(EXISTING_TAGS) - {"max_rounds:"} <= handled
    for tag in ROUTE_TAGS:
        assert not any(tag in other or other in tag for other in handled)


def test_step0_strips_route_tags_and_keeps_them_out_of_the_task_name():
    step0 = _section("## Step 0 — Route")
    task_name_rule = _raw_section("## Step 0 — Route").split("\n\n")[1]
    for tag in ROUTE_TAGS:
        assert "`%s`" % tag in task_name_rule
    item = re.search(r"\*\*`direct:`\*\* / \*\*`light:`\*\*(.+?)(?: \d+\. \*\*|## |\Z)", step0)
    assert item, "Step 0 has no item for the route tags"
    body = item.group(1)
    assert "whitespace-delimited" in body and "before the task text" in body
    assert "`redirect:`" in body
    assert "AUTONOMOUS" in body.upper()
    assert "`light:` wins" in body


def test_run_hard_handoff_drops_route_tags():
    step0 = _section("## Step 0 — Route")
    assert "removing `hard:`, `direct:` and `light:`" in step0


def test_size_step_sits_between_routing_and_discover_and_is_not_called_triage():
    size = SKILL.index("## Step 1b — Size (inline)")
    assert SKILL.index("## Step 0 — Route") < size < SKILL.index("## Step 2 — Discover")
    heading_line = SKILL[size:SKILL.index("\n", size)]
    assert "triage" not in heading_line.lower()


def test_size_criteria_default_to_light_and_need_no_subagent():
    size = _section("## Step 1b — Size (inline)")
    assert "No subagent, no extra model call" in size
    assert "concrete defect or change with clear expected behaviour" in size
    assert "one module plus its tests" in size
    for excluded in ("public API", "schema or migration", "security-sensitive",
                     "dependency change", "cross-repo change"):
        assert excluded in size
    assert "unsure" in size and "**light**" in size
    assert "AUTONOMOUS" in size


def _size_block():
    size = _raw_section("## Step 1b — Size (inline)")
    block = re.search(r"```markdown\n(## Size\n.+?)```", size, re.S)
    assert block, "Step 1b has no Size block"
    return block.group(1)


def test_size_block_records_route_and_reason():
    block = _size_block()
    assert "route: <direct | light>" in block
    assert block.splitlines()[2].startswith("reason: ")


def test_harness_reads_the_route_from_the_brief_format_run_writes(tmp_path):
    for route in ("direct", "light"):
        task = tmp_path / route / "task"
        task.mkdir(parents=True)
        text = "# task\n## Goal\nfix it\n" + _size_block().replace("<direct | light>", route)
        (task / "brief.md").write_text(text, encoding="utf-8")
        assert brief_route(tmp_path / route) == route


def test_light_brief_template_carries_the_size_section():
    frame = _raw_section("## Step 3 — Frame (inline)")
    assert "## Size\nroute: light\nreason: " in frame


def test_direct_route_keeps_branch_tests_brief_and_commit_but_no_subagents():
    direct = _section("## Direct route (inline)")
    assert "no discovery, no context-pack subagent and no review subagent" in direct
    assert "implementer tier guard still applies" in direct
    assert "`## Verification`" in direct and "`## Size` section" in direct
    assert "Step 6 as written" in direct and "Step 8 as written" in direct
    # Skipped steps still get markers, so the phase report sees the direct route's shape.
    from quoin.core.scripts.run_phase import PHASES
    for phase in ("discover", "context", "review", "frame", "implement", "verify", "commit"):
        assert phase in PHASES and "`%s`" % phase in direct
    assert "`--skipped`" in direct
    # The branch comes from Step 1, which runs before Size.
    assert SKILL.index("## Step 1 — Setup") < SKILL.index("## Direct route (inline)")


def test_direct_route_escalates_one_way_and_keeps_the_work():
    direct = _section("## Direct route (inline)")
    assert "one way, never back" in direct
    for trigger in ("outgrows one module", "verification fails twice", "excluded category"):
        assert trigger in direct
    assert "no reset" in direct
    assert "only to failures on the light pipeline" in direct
    assert "`route: light`" in direct and "escalated: from direct" in direct


def test_resume_follows_the_recorded_route():
    step0 = _section("## Step 0 — Route")
    assert "`route: direct` resumes on the Direct route" in step0


def test_description_intent_doc_and_quickstart_mention_the_direct_route():
    description = re.search(r"^description: (.+)$", SKILL, re.M).group(1)
    assert "direct fix" in description and "direct:/light:" in description
    assert "direct route" in INTENT and "`direct:` and `light:`" in INTENT
    assert "`direct:` or `light:`" in QUICKSTART
