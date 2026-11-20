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

from quoin.core.scripts import run_phase

QUOIN = Path(__file__).resolve().parents[2]
RUN_DIR = QUOIN / "adapters" / "claude" / "skills" / "run"
SKILL = (RUN_DIR / "SKILL.md").read_text(encoding="utf-8")
LIGHT = (RUN_DIR / "light-pipeline.md").read_text(encoding="utf-8")
REF = (RUN_DIR / "reference.md").read_text(encoding="utf-8")
ROUTES = (RUN_DIR / "routes.md").read_text(encoding="utf-8")
REF_PATH = "${CLAUDE_PLUGIN_ROOT}/skills/run/light-pipeline.md"
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


def test_reference_file_uses_the_home_placeholder_for_deployed_paths():
    assert "${CLAUDE_PLUGIN_ROOT}/scripts/run_phase.py" in LIGHT
    assert "~/.claude/" not in LIGHT


def _fake_source(tmp_path, with_light=True):
    skill = tmp_path / "src" / "adapters" / "claude" / "skills" / "run"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Run\nsee ${CLAUDE_PLUGIN_ROOT}/skills/run/light-pipeline.md\n", encoding="utf-8")
    if with_light:
        (skill / "light-pipeline.md").write_text("# light\n${CLAUDE_PLUGIN_ROOT}/scripts/run_phase.py\n", encoding="utf-8")
    return tmp_path / "src", skill


def test_light_phase_lists_stay_in_sync():
    from quoin.benchmarks.harness import phase_report
    from quoin.core.scripts import status_graph
    ts = (QUOIN / "plugins" / "workflow-tasks" / "hooks" / "tasks.ts").read_text(encoding="utf-8")
    block = ts[ts.index("const LIGHT_MARKER_PHASE"):]
    ts_keys = set(re.findall(r"^\s+'?([a-z-]+)'?:", block[:block.index("}")], re.M))
    phases = set(run_phase.PHASES)
    assert phases == set(phase_report.PHASES) == set(status_graph._LIGHT_MARKER_PHASE) == ts_keys
    assert "AskUserQuestion" not in _section(LIGHT, "## Step 3 — Frame")


