"""/run keeps the r2 (staged) steps in a reference file read only for r2.

SKILL.md is expanded whole into the first turn of every /run session, so the
steps only an r2 task uses (2, 3, 4, 4b with its parallel waves) live in
light-pipeline.md next to it, while the brief and the review (Step 7) that r1
needs live in routes.md, which r0 never reads. These tests pin the split: where
the Read is triggered, that the plain route never depends on text that only
the light pipeline file holds,
and that the installer ships, substitutes and prunes the file like SKILL.md.
"""
from __future__ import annotations

import re
from pathlib import Path

from quoin import installer
from quoin.core.scripts import run_phase

QUOIN = Path(__file__).resolve().parents[2]
RUN_DIR = QUOIN / "adapters" / "claude" / "skills" / "run"
SKILL = (RUN_DIR / "SKILL.md").read_text(encoding="utf-8")
LIGHT = (RUN_DIR / "light-pipeline.md").read_text(encoding="utf-8")
REF = (RUN_DIR / "reference.md").read_text(encoding="utf-8")
ROUTES = (RUN_DIR / "routes.md").read_text(encoding="utf-8")
REF_PATH = "__QUOIN_HOME__/skills/run/light-pipeline.md"
FILE_NAME = re.compile(r"light-pipeline\.md|light\s+pipeline\s+file")

MOVED_HEADINGS = (
    "## Step 2 — Discover",
    "## Step 3 — Frame",
    "## Step 4 — Context pack",
    "## Step 4b — Decompose",
)
LIGHT_ONLY_STEPS = {"2", "3", "4", "4b"}


def _headings(text):
    """`## ` headings outside fenced blocks (the brief and stage templates are fenced)."""
    out, fenced = [], False
    for line in text.splitlines():
        if line.startswith("```"):
            fenced = not fenced
        elif line.startswith("## ") and not fenced:
            out.append(line)
    return out


def _section(text, heading):
    start = text.index(heading)
    nxt = [text.index(h, start + 1) for h in _headings(text) if text.find(h, start + 1) != -1]
    end = min([i for i in nxt if i > start], default=len(text))
    return text[start:end]


def _units(text):
    """Paragraphs, with each numbered or bulleted list item as its own unit."""
    units = []
    for para in re.split(r"\n\s*\n", text):
        units.extend(u for u in re.split(r"\n(?=\d+\. |- )", para) if u.strip())
    return units


def _steps_cited(unit):
    cited = set()
    for match in re.finditer(r"\bSteps? ((?:\d+[a-z]?(?:–\d+[a-z]?)?(?:, | and | or )?)+)", unit):
        for token in re.findall(r"\d+[a-z]?(?:–\d+[a-z]?)?", match.group(1)):
            if "–" in token:
                lo, hi = token.split("–")
                lo_n, hi_n = int(re.match(r"\d+", lo).group()), int(re.match(r"\d+", hi).group())
                cited |= {str(n) for n in range(lo_n, hi_n + 1)}
                cited |= {hi} | {lo}
            else:
                cited.add(token)
    return cited


def test_moved_steps_live_only_in_the_reference_file():
    skill_headings, light_headings = _headings(SKILL), _headings(LIGHT)
    for heading in MOVED_HEADINGS:
        assert not any(h.startswith(heading) for h in skill_headings), heading
        assert sum(h.startswith(heading) for h in light_headings) == 1, heading
    assert "**Parallel waves (`PARALLEL` only).**" in LIGHT
    assert "**Parallel waves" not in SKILL
    # r1 needs the review without reading the r2 file, and r0 reads neither.
    assert any(h.startswith("## Step 7 — Review") for h in _headings(ROUTES))
    assert not any(h.startswith("## Step 7") for h in light_headings + skill_headings)


def test_r2_triggers_name_the_reference_file():
    pointer = " ".join(_section(ROUTES, "## Brief").split())
    assert REF_PATH in pointer and "one Read" in pointer
    assert "**r2** → a short stage plan: read the light pipeline file" in pointer
    scope = " ".join(_section(SKILL, "## Step 1b — Scope triage").split())
    assert "**r1, r2, r3** → read the routes file" in scope
    intro = " ".join(LIGHT[:LIGHT.index("## Resuming")].split())
    for trigger in ("the routes file picks r2", "r3 runs as r2", "reclassifies to r2",
                    "`--resume`", "`stage N of <task>`"):
        assert trigger in intro, trigger
    # Step 0 sends --resume and the rarer tags to reference.md, whose resume
    # item names the light pipeline file.
    step0 = _section(SKILL, "## Step 0 — Route")
    assert "`--resume`" in step0 and "reference.md" in step0
    tags = _section(REF, "## Step 0 — Tags and resume")
    resume = next(u for u in _units(tags) if u.startswith("2. **`--resume`**"))
    assert FILE_NAME.search(resume)


def test_reclassify_to_r2_reads_the_reference_file_before_editing_the_brief():
    assert '"Reclassify upward"' in " ".join(_section(SKILL, "## Plain route").split())
    up = " ".join(_section(ROUTES, "## r1 — plain route plus review").split())
    assert 'reference.md "Reclassify to r2"' in up
    move = " ".join(_section(REF, "## Reclassify to r2").split())
    assert move.index("Read the light pipeline file") < move.index("set the brief's `## Scope`")
    assert "no reset, no stash" in move and "`route: r2`" in move


# SKILL.md sections a plain-route session follows; it never reads the light pipeline file.
DIRECT_PATH_SECTIONS = (
    "## Step 0 — Route", "## Step 1 — Setup", "## Step 1b — Scope triage", "## Plain route",
    "## Implementer tier guard", "## Step 5 — Implement", "## Step 6 — Verify",
    "## Step 8 — Commit", "## Escalation",
)
# Clauses that only tell the session to skip a light step; they need none of its text.
SKIP_CLAUSES = re.compile(
    r"skip\s+Steps?\s+[0-9a-z,\s–]+|Step\s+\d+[a-z]?\s+only\s+logs\s+its\s+marker\s+as\s+skipped"
)


def _light_steps_needed(unit):
    return _steps_cited(SKIP_CLAUSES.sub("", unit)) & LIGHT_ONLY_STEPS


def test_plain_route_instructions_never_depend_on_reference_only_content():
    """A plain-route instruction citing Step 2, 3, 4 or 4b only skips it or defers to the file.

    The plain route reads SKILL.md alone, so a unit it follows that cites an
    r2-only step must either just skip it, or be an r2 branch that names the
    reference file. Inside the Plain route section only the reclassify
    paragraph, which is the hand-over to r2, may rely on naming the file.
    """
    offenders = []
    for heading in DIRECT_PATH_SECTIONS:
        in_direct = heading == "## Plain route"
        for unit in _units(_section(SKILL, heading)):
            if not _light_steps_needed(unit):
                continue
            names_file = bool(FILE_NAME.search(unit))
            if names_file and (not in_direct or unit.startswith("**Reclassify upward**")):
                continue
            offenders.append(f"{heading}: " + " ".join(unit.split())[:120])
    assert not offenders, offenders


def test_dependency_check_is_not_vacuous():
    plain = _section(SKILL, "## Plain route")
    # A plain-route step that runs an r2-only step must be caught, with or
    # without the file named, and a skip clause must not hide it.
    for bad in ("4. **Commit.** Run Step 4, then Step 8 as written.",
                "4. **Commit.** Run Step 4b (light pipeline file), then Step 8.",
                "Log `context` as `--skipped`; then run Step 4."):
        unit = next(u for u in _units(plain + "\n\n" + bad) if u == bad)
        assert _light_steps_needed(unit), bad
        assert not unit.startswith("**Reclassify upward**")
    assert not _light_steps_needed("skip Steps 2, 3, 4 and 4b; run the guard")


def test_direct_route_sections_cite_no_reference_only_heading_text():
    # Headings unique to the reference file (outside its templates) never
    # appear in SKILL.md, so no SKILL.md instruction can point at them by name.
    only_light = [h for h in _headings(LIGHT) if h not in _headings(SKILL)]
    assert only_light
    for heading in only_light:
        assert heading.removeprefix("## ") not in SKILL, heading


def test_reference_file_hands_back_to_skill_md_in_order():
    order = [LIGHT.index(h) for h in MOVED_HEADINGS]
    assert order == sorted(order)
    intro = " ".join(LIGHT[:LIGHT.index("## Resuming")].split())
    assert ("the implementer tier guard and Steps 5, 6, 7 (r1 stages only; Step 7 is in the "
            "routes file) and 8 in SKILL.md") in intro
    # SKILL.md sends r1 tasks and r1 stages from Step 6 on to Step 7.
    step6 = " ".join(_section(SKILL, "## Step 6 — Verify").split())
    assert "Then Step 7 (the routes file) for r1 and each r1 stage; Step 8 otherwise." in step6
    # The r2 phase markers live with their steps; the review's stay in SKILL.md.
    for marker in ('"<task>" discover --skipped', 'Marker: `frame`', 'Marker: `context`',
                   'Marker: `decompose`'):
        assert marker in LIGHT, marker
    for marker in ('Marker: `review`', 'log `review-fixes`'):
        assert marker in ROUTES, marker
    assert ROUTES.rstrip().endswith("Then Step 8 in SKILL.md.")


def test_reference_file_uses_the_home_placeholder_for_deployed_paths():
    assert "__QUOIN_HOME__/scripts/run_phase.py" in LIGHT
    assert "~/.claude/" not in LIGHT


def _fake_source(tmp_path, with_light=True):
    skill = tmp_path / "src" / "adapters" / "claude" / "skills" / "run"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Run\nsee __QUOIN_HOME__/skills/run/light-pipeline.md\n", encoding="utf-8")
    if with_light:
        (skill / "light-pipeline.md").write_text("# light\n__QUOIN_HOME__/scripts/run_phase.py\n", encoding="utf-8")
    return tmp_path / "src", skill


def test_installer_deploys_and_substitutes_the_reference_file(tmp_path):
    source, _ = _fake_source(tmp_path)
    dest = tmp_path / ".claude"
    assert installer.deploy_skills(source, dest) == 1
    deployed = (dest / "skills" / "run" / "light-pipeline.md").read_text(encoding="utf-8")
    assert deployed == f"# light\n{dest.resolve()}/scripts/run_phase.py\n"


def test_reinstall_prunes_the_reference_file_once_it_is_no_longer_shipped(tmp_path):
    source, skill = _fake_source(tmp_path)
    dest = tmp_path / ".claude"
    installer.start_deploy_record()
    installer.deploy_skills(source, dest)
    previous = installer.take_deploy_record()
    assert "skills/run/light-pipeline.md" in previous

    (skill / "light-pipeline.md").unlink()
    installer.start_deploy_record()
    installer.deploy_skills(source, dest)
    current = installer.take_deploy_record()
    pruned, _ = installer.prune_stale_deploys(dest, previous, current)
    assert pruned == ["skills/run/light-pipeline.md"]
    assert not (dest / "skills" / "run" / "light-pipeline.md").exists()
    assert (dest / "skills" / "run" / "SKILL.md").exists()


def test_drift_check_covers_the_reference_file(tmp_path):
    source, _ = _fake_source(tmp_path)
    dest = tmp_path / ".claude"
    installer.deploy_skills(source, dest)
    (dest / "skills" / "run" / "light-pipeline.md").write_text("edited\n", encoding="utf-8")
    drift = installer.compute_drift(source, dest, categories=("skills",))
    assert any(e.deployed_path.endswith("light-pipeline.md") and e.reason == "stale" for e in drift)


def test_scope_triage_writes_the_brief_with_no_prompt_triage_step():
    brief = _section(ROUTES, "## Brief")
    assert brief.index("## Request") < brief.index("## Scope")
    assert "## Triage" not in SKILL + ROUTES and "triage" not in run_phase.PHASES


def test_light_phase_lists_stay_in_sync():
    from quoin.benchmarks.harness import phase_report
    from quoin.core.scripts import status_graph
    ts = (QUOIN / "plugins" / "workflow-tasks" / "hooks" / "tasks.ts").read_text(encoding="utf-8")
    block = ts[ts.index("const LIGHT_MARKER_PHASE"):]
    ts_keys = set(re.findall(r"^\s+'?([a-z-]+)'?:", block[:block.index("}")], re.M))
    phases = set(run_phase.PHASES)
    assert phases == set(phase_report.PHASES) == set(status_graph._LIGHT_MARKER_PHASE) == ts_keys
    assert "AskUserQuestion" not in _section(LIGHT, "## Step 3 — Frame")


def test_installer_ships_the_routes_file():
    assert "routes.md" in installer.SKILL_COMPANION_FILES
    assert "__QUOIN_HOME__/skills/run/routes.md" in SKILL
