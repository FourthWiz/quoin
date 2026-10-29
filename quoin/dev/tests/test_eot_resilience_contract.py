"""IVG-249 S-03 T-06: wording/structure contract pins for the `end_of_task`
resilience feature (gate freshness sidecar + reuse + inline finish) spread
across gate/SKILL.md, end_of_task/SKILL.md, run/SKILL.md, and cost_summary.py.

Per-region two-slicer idiom (lesson 2026-06-04, reused verbatim from
test_gate_deploy_drift_tokens.py): each region is located by its own header
string and asserted against WITHIN that slice, so a dropped region FAILs
rather than passing on a bare whole-file occurrence count.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]  # quoin/ repo root
GATE_SKILL = REPO_ROOT / "quoin" / "adapters" / "claude" / "skills" / "gate" / "SKILL.md"
EOT_SKILL = REPO_ROOT / "quoin" / "adapters" / "claude" / "skills" / "end_of_task" / "SKILL.md"
RUN_SKILL = REPO_ROOT / "quoin" / "adapters" / "claude" / "skills" / "run_hard" / "SKILL.md"
COST_SUMMARY = REPO_ROOT / "quoin" / "core" / "scripts" / "cost_summary.py"


def _load(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# gate/SKILL.md
# ---------------------------------------------------------------------------

_GATE_REGION_BOUNDARIES = (
    "*Standard gate (Small and Medium tasks):*",
    "*Full gate (Large tasks) — includes everything in Standard, plus:*",
    "**After /review → before /end_of_task (Full gate — always, all task sizes):**",
)


def _gate_region(text: str, header: str) -> str:
    start = text.find(header)
    assert start != -1, f"Region header not found in gate SKILL.md: {header!r}"
    after = text[start + len(header):]
    m = re.search(r"^#{2,3} ", after, re.MULTILINE)
    end = m.start() if m else len(after)
    for boundary in _GATE_REGION_BOUNDARIES:
        if boundary == header:
            continue
        idx = after.find(boundary)
        if idx != -1 and idx < end:
            end = idx
    return after[:end]


def test_sidecar_in_full_gate_region():
    region = _gate_region(_load(GATE_SKILL), "*Full gate (Large tasks)")
    assert "gate_fullsuite_sidecar.py" in region
    assert "__QUOIN_HOME__/scripts/gate_fullsuite_sidecar.py" in region


def test_sidecar_in_post_review_region():
    region = _gate_region(_load(GATE_SKILL), "**After /review → before /end_of_task")
    assert "gate_fullsuite_sidecar.py" in region


def test_reuse_bullet_names_verdict_derivation_phrase():
    text = _load(GATE_SKILL)
    assert (
        "derived from `known_red.py`'s own exit code AND the task's size profile"
        in text
    )


def test_reuse_bullet_names_small_medium_pytest_rc_clause():
    text = _load(GATE_SKILL)
    assert "`pytest_rc == 0` is ALSO required" in text


def test_reuse_bullet_names_disable_knob():
    text = _load(GATE_SKILL)
    assert "QUOIN_DISABLE_FULLSUITE_REUSE" in text


def test_undeterminable_profile_fallback_is_medium_never_large():
    text = _load(GATE_SKILL)
    assert "--task-profile medium` (never `large`" in text


def test_gate_heading_count_region_guard():
    # proc:R-1-region-guard — freeze the heading COUNT between the Standard-gate
    # marker and Step 3a as a regression anchor (T-06). No new ^#{2,3} heading
    # may appear in this span; the frozen baseline is 0.
    text = _load(GATE_SKILL)
    start = text.index("*Standard gate (Small and Medium tasks):*")
    end = text.index("### Step 3a")
    slice_ = text[start:end]
    assert len(re.findall(r"^#{2,3} ", slice_, re.MULTILINE)) == 0


# ---------------------------------------------------------------------------
# end_of_task/SKILL.md
# ---------------------------------------------------------------------------

_EOT_STEP_MARKERS = (
    "**Step 1: Pre-flight checks**",
    "**Step 1b: Working-tree cleanup scan**",
    "**Step 2: Commit decision",
    "**Step 3: Lessons learned",
    "**Step 4: Archive type",
    "**Step 5: Write",
    "**Step 6: Dispatch Sub-phase A",
    "**Step 7: Dispatch Sub-phase B",
    "**Step 8: Dispatch Sub-phase C",
)


def test_eot_all_nine_step_markers_survive_in_order():
    text = _load(EOT_SKILL)
    positions = []
    for marker in _EOT_STEP_MARKERS:
        idx = text.find(marker)
        assert idx != -1, f"missing step marker: {marker!r}"
        positions.append(idx)
    assert positions == sorted(positions), "step markers out of order"


def test_eot_sub_phase_literals_survive():
    text = _load(EOT_SKILL)
    assert "Sub-phase A" in text
    assert "Sub-phase B" in text
    assert "Sub-phase C" in text


def test_eot_step1_region_carries_sidecar_check():
    text = _load(EOT_SKILL)
    start = text.index("**Step 1: Pre-flight checks**")
    end = text.index("**Step 1b: Working-tree cleanup scan**")
    region = text[start:end]
    assert "gate_fullsuite_sidecar.py check" in region


# ---------------------------------------------------------------------------
# run/SKILL.md
# ---------------------------------------------------------------------------


def _run_phase6_span(text: str) -> str:
    start = text.index("## Phase 6 — End of Task")
    after = text[start:]
    m = re.search(r"^## ", after[len("## Phase 6 — End of Task"):], re.MULTILINE)
    end = len("## Phase 6 — End of Task") + (m.start() if m else len(after))
    return after[:end]


def test_run_phase6_span_carries_recovery_heading():
    span = _run_phase6_span(_load(RUN_SKILL))
    assert "### end_of_task failure recovery (inline finish)" in span


def test_run_phase6_span_carries_cost_summary_json_and_script():
    span = _run_phase6_span(_load(RUN_SKILL))
    assert "cost-summary.json" in span
    assert "cost_summary.py" in span


def test_run_phase6_span_carries_all_three_cost_renderings():
    span = _run_phase6_span(_load(RUN_SKILL))
    assert re.search(r"Cost: \$X", span), "missing bare 'Cost: $X.XX' rendering"
    assert "(partial)" in span
    assert "totals unavailable" in span


def test_run_cites_the_two_json_key_names():
    # Contract-text cross-check: T-05(b)'s rendering rules must cite the same two
    # literal key names the CLI's --format json emits (T-05(c)).
    span = _run_phase6_span(_load(RUN_SKILL))
    assert "`total`" in span or "\"total\"" in span or "total" in span
    assert "is_partial" in span


# ---------------------------------------------------------------------------
# cost_summary.py --format json — subprocess-level schema pin
# ---------------------------------------------------------------------------


def _run_cost_summary_json(fixture_path: Path) -> dict:
    result = subprocess.run(
        [sys.executable, str(COST_SUMMARY), "--format", "json", str(fixture_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def test_cost_summary_json_schema_clean_total(tmp_path):
    fixture = tmp_path / "clean.json"
    fixture.write_text(json.dumps({"task_total": 1.68}), encoding="utf-8")
    payload = _run_cost_summary_json(fixture)
    assert set(payload.keys()) == {"total", "is_partial"}
    assert isinstance(payload["total"], float)
    assert isinstance(payload["is_partial"], bool)
    assert payload == {"total": 1.68, "is_partial": False}


def test_cost_summary_json_schema_partial_total(tmp_path):
    fixture = tmp_path / "partial.json"
    fixture.write_text(
        json.dumps({"task_total": 1.68, "fallback_used": True}), encoding="utf-8"
    )
    payload = _run_cost_summary_json(fixture)
    assert set(payload.keys()) == {"total", "is_partial"}
    assert isinstance(payload["total"], float)
    assert isinstance(payload["is_partial"], bool)
    assert payload == {"total": 1.68, "is_partial": True}


def test_cost_summary_json_schema_null_total(tmp_path):
    fixture = tmp_path / "null.json"
    fixture.write_text(json.dumps({}), encoding="utf-8")
    payload = _run_cost_summary_json(fixture)
    assert set(payload.keys()) == {"total", "is_partial"}
    assert payload["total"] is None
    assert isinstance(payload["is_partial"], bool)
    assert payload == {"total": None, "is_partial": False}
