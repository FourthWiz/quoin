"""/run keeps the light-pipeline steps in a reference file read only on the light route.

SKILL.md is expanded whole into the first turn of every /run session, so the
steps only the light route uses (2, 3, 4, 4b with its parallel waves, and 7)
live in light-pipeline.md next to it. These tests pin the split: where the
Read is triggered, that the direct route never depends on text that only the
reference file holds, and that the installer ships, substitutes and prunes
the file like SKILL.md.
"""
from __future__ import annotations

import re
from pathlib import Path

from quoin import installer

QUOIN = Path(__file__).resolve().parents[2]
RUN_DIR = QUOIN / "adapters" / "claude" / "skills" / "run"
SKILL = (RUN_DIR / "SKILL.md").read_text(encoding="utf-8")
LIGHT = (RUN_DIR / "light-pipeline.md").read_text(encoding="utf-8")
REF_PATH = "__QUOIN_HOME__/skills/run/light-pipeline.md"
FILE_NAME = re.compile(r"light-pipeline\.md|light\s+pipeline\s+file")

MOVED_HEADINGS = (
    "## Step 2 — Discover",
    "## Step 3 — Frame",
    "## Step 4 — Context pack",
    "## Step 4b — Decompose",
    "## Step 7 — Review",
)
LIGHT_ONLY_STEPS = {"2", "3", "4", "4b", "7"}


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


def test_light_route_triggers_name_the_reference_file():
    pointer = _section(SKILL, "## Light pipeline")
    assert REF_PATH in pointer and "one Read" in pointer
    for trigger in ("Step 1b picks light", "Direct route\nescalates", "`--resume`", "`stage N of <task>`"):
        assert trigger in pointer, trigger
    size = _section(SKILL, "## Step 1b — Size")
    assert re.search(r"Light → read the light pipeline file", size)
    step0 = _section(SKILL, "## Step 0 — Route")
    resume = next(u for u in _units(step0) if u.startswith("2. **`--resume`**"))
    assert FILE_NAME.search(resume)


def test_escalation_reads_the_reference_file_before_writing_the_light_brief():
    direct = " ".join(_section(SKILL, "## Direct route").split())
    read = direct.index("Read the light pipeline file first")
    assert read < direct.index("rewrite the brief in one write as the full Step 3 brief")


# SKILL.md sections a direct-route session follows (it never reads the reference file).
DIRECT_PATH_SECTIONS = (
    "## Step 0 — Route", "## Step 1 — Setup", "## Step 1b — Size", "## Direct route",
    "## Implementer tier guard", "## Step 5 — Implement", "## Step 6 — Verify",
    "## Step 8 — Commit", "## Escalation", "## Important behaviours",
)
# Clauses that only tell the session to skip a light step; they need none of its text.
SKIP_CLAUSES = re.compile(
    r"skip\s+Steps?\s+[0-9a-z,\s–]+|Step\s+\d+[a-z]?\s+only\s+logs\s+its\s+marker\s+as\s+skipped"
)


def _light_steps_needed(unit):
    return _steps_cited(SKIP_CLAUSES.sub("", unit)) & LIGHT_ONLY_STEPS


def test_direct_route_instructions_never_depend_on_reference_only_content():
    """A direct-route instruction citing Step 2, 3, 4, 4b or 7 only skips it or defers to the file.

    The direct route reads SKILL.md alone, so a unit it follows that cites a
    light-only step must either just skip it, or be a light-route branch that
    names the reference file. Inside the Direct route section only the
    escalation paragraph, which is the hand-over to the light route, may rely on
    naming the file.
    """
    offenders = []
    for heading in DIRECT_PATH_SECTIONS:
        in_direct = heading == "## Direct route"
        for unit in _units(_section(SKILL, heading)):
            if not _light_steps_needed(unit):
                continue
            names_file = bool(FILE_NAME.search(unit))
            if names_file and (not in_direct or unit.startswith("**Escalate to light**")):
                continue
            offenders.append(f"{heading}: " + " ".join(unit.split())[:120])
    assert not offenders, offenders


def test_dependency_check_is_not_vacuous():
    direct = _section(SKILL, "## Direct route")
    # A Direct-route step that runs a light-only step must be caught, with or
    # without the file named, and a skip clause must not hide it.
    for bad in ("4. **Commit.** Run Step 7, then Step 8 as written.",
                "4. **Commit.** Run Step 7 (light pipeline file), then Step 8.",
                "Log `review` as `--skipped`; then run Step 7."):
        unit = next(u for u in _units(direct + "\n\n" + bad) if u == bad)
        assert _light_steps_needed(unit), bad
        assert not unit.startswith("**Escalate to light**")
    assert not _light_steps_needed("skip Steps 2, 3, 4, 4b and 7; run the guard")


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
    handback = LIGHT.index("implementer tier guard and Steps 5 and 6 in\nSKILL.md")
    assert LIGHT.index("## Step 4 — Context pack") < handback < LIGHT.index("## Step 4b — Decompose")
    # SKILL.md sends the light route from Step 6 back to Step 7.
    step6 = _section(SKILL, "## Step 6 — Verify")
    assert "Light route: Step 7" in step6 and FILE_NAME.search(step6)
    assert LIGHT.rstrip().endswith("Then Step 8 in SKILL.md.")
    # The light route's phase markers moved with their steps, unchanged.
    for marker in ('"<task>" discover --skipped', 'Marker: `frame`', 'Marker: `context`',
                   'Marker: `decompose`', 'Marker: `review`', 'log `review-fixes`'):
        assert marker in LIGHT, marker


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
