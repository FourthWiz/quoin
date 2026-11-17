"""Contract for /run's scope triage (Step 1b), its classes, routes and tags.

The session classifies from SKILL.md prose, so the signals each class fires
on are pinned here as text: every r1, r2 and r3 signal is listed under its own
class and no other, r0 is the default without a signal, and each class adds
exactly one thing. The brief's Scope block is also fed through the benchmark
harness's reader, so the format /run writes and the format the harness parses
cannot drift apart.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from quoin.benchmarks.harness.cells.quoin_claude import brief_route

QUOIN = Path(__file__).resolve().parents[2]
RUN_DIR = QUOIN / "adapters" / "claude" / "skills" / "run"
SKILL = (RUN_DIR / "SKILL.md").read_text(encoding="utf-8")
LIGHT = (RUN_DIR / "light-pipeline.md").read_text(encoding="utf-8")
REF = (RUN_DIR / "reference.md").read_text(encoding="utf-8")
ROUTES = (RUN_DIR / "routes.md").read_text(encoding="utf-8")
TAGS = "## Step 0 — Tags and resume"
SCOPE = "## Step 1b — Scope triage (inline)"
INTENT = (QUOIN / "core" / "skills" / "run.md").read_text(encoding="utf-8")
QUICKSTART = (QUOIN / "QUICKSTART.md").read_text(encoding="utf-8")

# Tags /run already strips or acts on; the class tags must not reuse any of them.
EXISTING_TAGS = ("small:", "medium:", "large:", "fast:", "hard:", "strict:", "parallel:", "max_rounds:")
CLASS_TAGS = ("r0:", "r1:", "r2:")

# Each class fires only on these signals (IVG-327 stage 2).
SIGNALS = {
    "r3": ("novel architecture", "data-migration strategy", "several consumers"),
    "r2": ("more than one repository", "must land in order"),
    "r1": ("auth", "permissions", "crypto", "input sanitising", "schema or migration", "public API"),
}


def _raw_section(heading, text=SKILL):
    """The text under a `## ` heading, up to the next one outside a code fence."""
    lines = text[text.index(heading):].splitlines()
    out, fenced = [lines[0]], False
    for line in lines[1:]:
        if line.startswith("```"):
            fenced = not fenced
        elif line.startswith("## ") and not fenced:
            break
        out.append(line)
    return "\n".join(out) + "\n"


def _section(heading, text=SKILL):
    """Like `_raw_section`, with whitespace collapsed so rewrapping prose does not matter."""
    return " ".join(_raw_section(heading, text).split())


def _class_bullets():
    """The rubric bullet of each class, keyed r0..r3."""
    scope = _section(SCOPE)
    bullets = {}
    for cls in ("r3", "r2", "r1", "r0"):
        match = re.search(r"- \*\*%s [^*]+:\*\*(.+?)(?= - \*\*r\d |$| Size, )" % cls, scope)
        assert match, f"Step 1b has no bullet for {cls}"
        bullets[cls] = match.group(1)
    return bullets


@pytest.mark.parametrize("cls,signal", [(c, s) for c, sigs in SIGNALS.items() for s in sigs])
def test_each_signal_is_listed_under_its_class(cls, signal):
    assert signal in _class_bullets()[cls]


@pytest.mark.parametrize("cls,signal", [(c, s) for c, sigs in SIGNALS.items() for s in sigs])
def test_no_signal_also_triggers_another_class(cls, signal):
    bullets = _class_bullets()
    for other, text in bullets.items():
        if other != cls:
            assert signal not in text, f"{signal!r} of {cls} also appears under {other}"


def test_classes_are_checked_from_the_top_and_default_to_r0():
    scope = _section(SCOPE)
    assert "No subagent, no extra model call" in scope
    assert "at most one quick look" in scope
    assert "exactly one class" in scope
    order = [scope.index("- **%s " % cls) for cls in ("r3", "r2", "r1", "r0")]
    assert order == sorted(order) and "Check from the top" in scope
    assert "needs a signal you can name" in scope
    assert "with none, the task is **r0**, also when unsure" in scope
    assert "r0 Plain:** everything else" in scope
    # Things the old sizing excluded from the direct route no longer escalate.
    assert "Size, file count, a dependency bump or a long request are not signals" in scope
    assert "AUTONOMOUS" in scope


def test_each_class_adds_exactly_one_thing():
    scope = _section(SCOPE)
    assert "**r0** → the plain route below: no brief" in scope
    assert "**r1, r2, r3** → read the routes file" in scope
    brief = _section("## Brief", ROUTES)
    assert "Each class adds exactly one thing" in brief
    assert "**r1** → the plain route plus the Step 7 review" in brief
    assert "**r2** → a short stage plan" in brief
    plain = _section("## Plain route (r0)")
    assert "No discovery, context pack, brief or review" in plain
    assert "r1 only" in ROUTES[ROUTES.index("## Step 7 — Review"):].splitlines()[0]
    assert "## Step 7" not in SKILL


def test_r0_reads_no_other_file_and_logs_no_skipped_markers():
    from quoin.core.scripts.run_phase import PHASES
    scope = _section(SCOPE)
    assert "no skipped-step markers, no other file" in scope
    plain = _section("## Plain route (r0)")
    assert "routes file" not in plain.split("**Reclassify upward**")[0]
    brief = _section("## Brief", ROUTES)
    r1 = brief[brief.index("**r1** →"):brief.index("**r2** →")]
    for phase in ("discover", "context", "frame"):
        assert phase in PHASES and "`%s`" % phase in r1
    assert "`review`" not in r1
    assert "In one Bash call" in brief and "with a heredoc" in brief
    assert "An r0 run never reads it" in ROUTES


def test_r0_records_its_commit_in_a_brief_written_at_commit_time(tmp_path):
    commit = _section("## Step 8 — Commit (inline)")
    assert "on r0 written as a new brief" in commit
    assert "`## Scope` with `route: r0`" in commit and "`branch:` and `commit:`" in commit
    assert "still records `## Verification`" in commit and "`## Request`" in commit
    # An r0 run that escalates keeps its request for run_hard.
    assert "When one fires on r0, first write the brief" in _section("## Escalation", REF)
    task = tmp_path / "task"
    task.mkdir()
    (task / "brief.md").write_text(
        "# task\n## Scope\nroute: r0\n## Verification\npytest: pass\n"
        "## Commit\nbranch: b\ncommit: abc\n", encoding="utf-8")
    assert brief_route(tmp_path) == "r0"


def test_r3_starts_the_heavy_pipeline_only_on_opt_in():
    scope = _section("## Brief", ROUTES)
    r3 = scope[scope.index("**r3** →"):]
    assert "explicit opt-in only" in r3
    assert "switch procedure" in r3 and "trigger `design`" in r3
    for condition in ("`AUTONOMOUS`", "`NO_SWITCH`", "`[from-hard]`", "`pipeline-switch.md`"):
        assert condition in r3
    assert "never switch" in r3
    assert "`runs-as: r2`" in r3 and "`runs-as: r1`" in r3
    assert "recommending `/run hard: <task>`" in r3
    switch = _section("## Switch to run_hard", LIGHT)
    assert "stages | design>" in switch
    assert "`design` never reaches this procedure under `AUTONOMOUS`" in switch
    # There is no r3 tag: hard: is the opt-in.
    assert "There is no `r3:` tag" in _section(TAGS, REF)


def _scope_block():
    block = re.search(r"```markdown\n[^`]*?(## Scope\n.+?)```", _raw_section("## Brief", ROUTES), re.S)
    assert block, "the routes file has no Scope block"
    return block.group(1)


def test_scope_block_records_class_and_signal():
    lines = _scope_block().splitlines()
    assert lines[1] == "route: <r1 | r2 | r3>"
    assert lines[2].startswith("signal: ") and '"forced by r1:"' in lines[2]


@pytest.mark.parametrize("cls", ["r0", "r1", "r2", "r3"])
def test_harness_reads_the_class_from_the_brief_format_run_writes(tmp_path, cls):
    task = tmp_path / "task"
    task.mkdir()
    text = "# task\n## Request\nfix it\n" + _scope_block().replace("<r1 | r2 | r3>", cls)
    (task / "brief.md").write_text(text, encoding="utf-8")
    assert brief_route(tmp_path) == cls


def test_class_tags_do_not_collide_with_tags_step0_already_handles():
    step0 = _raw_section(TAGS, REF)
    handled = set(re.findall(r"`([a-z_]+:)`", step0.split("**`r0:`** / **`r1:`**")[0])) - set(CLASS_TAGS)
    assert set(EXISTING_TAGS) - {"max_rounds:"} <= handled
    for tag in CLASS_TAGS:
        assert not any(tag in other or other in tag for other in handled)


def test_step0_strips_class_tags_and_keeps_them_out_of_the_task_name():
    step0 = _section(TAGS, REF)
    task_name_rule = _raw_section(TAGS, REF).split("\n\n")[1]
    for tag in CLASS_TAGS:
        assert "`%s`" % tag in task_name_rule
    item = re.search(r"\*\*`r0:`\*\* / \*\*`r1:`\*\* / \*\*`r2:`\*\*(.+?)(?: \d+\. \*\*|## |\Z)", step0)
    assert item, "Step 0 has no item for the class tags"
    body = item.group(1)
    assert "whitespace-delimited" in body and "before the task text" in body
    assert "AUTONOMOUS" in body.upper()
    assert "the highest wins" in body and "never reclassified" in body
    assert "`direct:`" not in REF + SKILL and "`light:`" not in REF + SKILL
    assert "the highest wins" in _section("## Step 0 — Route")


def test_run_hard_handoff_drops_class_tags():
    assert "removing `hard:`, `r0:`, `r1:` and `r2:`" in _section(TAGS, REF)
    assert "removing `hard:`, `r0:`, `r1:`, `r2:` and `isolate:`" in _section("## Step 0 — Route")


def test_scope_triage_sits_between_setup_and_the_routes():
    order = [SKILL.index(h) for h in ("## Step 0 — Route", "## Step 1 — Setup", SCOPE,
                                      "## Plain route (r0)", "## Implementer tier guard")]
    assert order == sorted(order)
    assert "## Step 1b — Size" not in SKILL and "## Direct route" not in SKILL


def test_plain_route_keeps_tests_brief_and_commit():
    plain = _section("## Plain route (r0)")
    assert "implementer tier guard still" in plain
    assert "**Verify.** Step 6" in plain and "**Commit.** Step 8" in plain
    r1 = _section("## r1 — plain route plus review", ROUTES)
    assert "`## Verification` and `## Commit` to this brief" in r1
    assert SKILL.index("## Step 1 — Setup") < SKILL.index("## Plain route")


def test_plain_route_reclassifies_upward_only_and_keeps_the_work():
    plain = _section("## Plain route (r0)")
    assert "never down; not when a tag forced the class" in plain
    assert '"Reclassify upward"' in plain
    assert "Two failed verifications are an Escalation trigger" in plain
    up = _section("## r1 — plain route plus review", ROUTES)
    assert "never down; not when a tag forced the class" in up
    assert "r0 → r1: the diff touches an r1 signal" in up
    assert "run Step 7 before committing" in up
    assert 'reference.md "Reclassify to r2"' in up
    move = _section("## Reclassify to r2", REF)
    assert "no reset, no stash" in move and "escalated: <r0 | r1> → r2" in move


def test_r2_stages_run_as_r0_or_r1_and_only_r1_stages_get_a_review():
    decompose = _raw_section("## Step 4b — Decompose", LIGHT)
    assert "   - Class: <r0 | r1>\n" in decompose
    flat = " ".join(decompose.split())
    assert "only r1 stages get the Step 7 review" in flat
    assert "Step 7 runs only for an r1 stage" in flat
    assert "Review each r1 stage (Step 7)" in flat
    assert "When Escalation does not allow switching" in flat


def test_r2_keeps_discovery_and_the_context_pack():
    assert "## Step 2 — Discover" in LIGHT and "## Step 4 — Context pack" in LIGHT
    assert "r2's discovery and context pack" in _section("## Important behaviours")


def test_resume_follows_the_recorded_class_and_maps_old_briefs():
    step0 = _section(TAGS, REF)
    assert "`route: r0` or `r1` (or `runs-as: r1`) resumes on the plain route" in step0
    assert "`r2` (or `runs-as: r2`) on the staged route" in step0
    assert "maps `route: direct` to r0 and `route: light` to r1, or to r2 when `architecture.md` exists" in step0
    assert "`--resume`" in _section("## Step 0 — Route")


def test_description_intent_doc_and_quickstart_name_the_classes():
    description = re.search(r"^description: (.+)$", SKILL, re.M).group(1)
    assert "scope triage" in description and "r0:/r1:/r2:" in description
    assert "R0 Plain" in INTENT and "`r0:`, `r1:` and `r2:`" in INTENT
    assert "`r0:`/`r1:`/`r2:`" in QUICKSTART


def test_escalation_switches_to_run_hard_once_unless_opted_out():
    assert 'reference.md "Escalation"' in _section("## Escalation")
    esc = _section("## Escalation", REF)
    assert "switch to `run_hard` through the switch procedure in the light pipeline file" in esc
    assert "`NO_SWITCH`" in esc and "pipeline-switch.md" in esc
    assert "at most once" in esc
    step0 = _section(TAGS, REF)
    assert "`no-switch:`" in step0 and "QUOIN_RUN_AUTO_SWITCH=0" in step0
    # The switch note outranks run_hard's run-state and autonomous markers on resume.
    assert step0.index("pipeline-switch.md") < step0.index("run-state-<task>.json")
    assert "`[from-hard]`" in step0


def test_switch_procedure_asks_once_and_keeps_the_work():
    switch = _section("## Switch to run_hard", LIGHT)
    for option in ("Switch to run_hard", "Continue as is", "Stop"):
        assert '"%s"' % option in switch
    assert "`AUTONOMOUS`, switch without asking" in switch
    assert "`from: light`" in switch and "`to: hard`" in switch
    assert "No reset, stash or commit of the partial diff" in switch
    assert "Keep the claim" in switch and "`[from-light] <task>`" in switch
    assert "`switched: to hard — <trigger>` to the brief's `## Scope` section" in switch
