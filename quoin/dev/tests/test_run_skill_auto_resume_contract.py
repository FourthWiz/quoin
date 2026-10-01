"""IVG-280 T-07: wording/structure contract pins for the run-continuation
integration spread across run/SKILL.md and memory/autonomous-mode.md.

Per-region two-slicer idiom (lesson 2026-06-04): each region is located by
its own header/anchor string and asserted against WITHIN that slice, so a
dropped region FAILs rather than passing on a bare whole-file occurrence
count.

Scope note: two items from the architecture's contract-test wishlist are
NOT covered here and are recorded as an explicit deviation (mirroring
T-03/T-04's own recorded scope deviations) rather than faked:
  - "no Agent-dispatch prompt template in run SKILL.md begins with /run":
    this file has no structural anchor (no `prompt:`/`prompt=` key) for a
    prompt-template scan to key on — Agent dispatches here are described in
    prose, not a fixed syntax. A future structural convention change should
    add this check.
  - "every non-human prompt source recorded in the finding's (a2) section
    appears in `auto_resume.NON_HUMAN_PROMPT_PREFIXES` or is marked 'does
    not fire'": no such constant exists in `auto_resume.py` yet (probe (a2)
    was NOT DETERMINED — zero confirmed sources to check against; T-08's
    hook-shell-suite wrapper is where a `NON_HUMAN_PROMPT_PREFIXES` parity
    test belongs once one exists). This file instead pins the weaker,
    presently-true invariant: `<task-notification>` — the D-26 seed — is
    the only non-human prefix used anywhere in the shipped hooks.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
RUN_SKILL = REPO_ROOT / "quoin" / "adapters" / "claude" / "skills" / "run" / "SKILL.md"
AUTONOMOUS_MODE = REPO_ROOT / "quoin" / "memory" / "autonomous-mode.md"
USERPROMPTSUBMIT_SH = REPO_ROOT / "quoin" / "hooks" / "userpromptsubmit.sh"


def _load(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _slice(text: str, start: str, end: str) -> str:
    s = text.find(start)
    assert s != -1, f"region start not found: {start!r}"
    e = text.find(end, s + len(start))
    assert e != -1, f"region end not found (after start {start!r}): {end!r}"
    return text[s:e]


# ---------------------------------------------------------------------------
# Region slicers (run/SKILL.md)
# ---------------------------------------------------------------------------

def _setup_arm_region(text: str) -> str:
    return _slice(
        text,
        "### Arm the in-session continuation (only under `AUTONOMOUS`, fresh entry only)",
        "### Parse input and determine task profile",
    )


def _resume_step0_region(text: str) -> str:
    return _slice(text, "**Step 0 (T-09)", "**Step 0b (T-18)")


def _session_age_guard_region(text: str) -> str:
    return _slice(text, "### Pre-flight: session-age guard", "### Check git state")


def _budget_item4_region(text: str) -> str:
    return _slice(
        text,
        "4. **`_AUTONOMOUS`** (`[autonomous]` / `--autonomous`)",
        "- Exit 1 but `$_cbg_out` does NOT start with `OVER|`",
    )


def _error_handling_region(text: str) -> str:
    return _slice(text, "## Error handling", "## Hook cooperation (autonomous)")


def _hook_coop_bullet1_region(text: str) -> str:
    return _slice(
        text,
        "- **Self-checkpoint before the advisory band.**",
        "- **There is no block to catch",
    )


def _hook_coop_bullet3_region(text: str) -> str:
    return _slice(
        text,
        "- **Hooks fail open; the orchestrator never assumes otherwise.**",
        "- **Hard constraint.**",
    )


def _hard_constraint_region(text: str) -> str:
    return _slice(text, "- **Hard constraint.**", "## Gate boundaries reference")


_REGION_FNS = {
    "setup_arm": _setup_arm_region,
    "resume_step0": _resume_step0_region,
    "session_age_guard": _session_age_guard_region,
    "budget_item4": _budget_item4_region,
    "error_handling": _error_handling_region,
    "hook_coop_bullet1": _hook_coop_bullet1_region,
    "hook_coop_bullet3": _hook_coop_bullet3_region,
    "hard_constraint": _hard_constraint_region,
}


def test_every_region_is_locatable_and_non_empty():
    text = _load(RUN_SKILL)
    for name, fn in _REGION_FNS.items():
        region = fn(text)
        assert region.strip(), f"region {name!r} sliced to empty text"


def test_setup_arm_region_calls_entry_fresh():
    region = _setup_arm_region(_load(RUN_SKILL))
    assert "auto_resume.py arm" in region
    assert "--entry fresh" in region
    assert "--session-id" in region


def test_resume_step0_region_calls_entry_resume():
    region = _resume_step0_region(_load(RUN_SKILL))
    assert "auto_resume.py arm" in region
    assert "--entry resume" in region


def test_session_age_guard_region_handoff_reason():
    region = _session_age_guard_region(_load(RUN_SKILL))
    assert "auto_resume.py handoff" in region
    assert "--reason session-age" in region
    assert '--on-fail-halt "session age cap"' in region


def test_budget_item4_region_handoff_reason():
    region = _budget_item4_region(_load(RUN_SKILL))
    assert "auto_resume.py handoff" in region
    assert "--reason budget" in region


def test_error_handling_region_context_and_pause():
    region = _error_handling_region(_load(RUN_SKILL))
    assert "auto_resume.py handoff" in region
    assert "--reason context" in region
    assert '--on-fail-halt "context exhaustion"' in region
    assert "auto_resume.py pause" in region
    assert "empty stdout" in region


# ---------------------------------------------------------------------------
# Cross-region uniqueness (the same slicers above are the source of truth —
# a call site appearing outside its named region is a contract violation).
# ---------------------------------------------------------------------------

def test_arm_called_exactly_twice_in_the_file():
    text = _load(RUN_SKILL)
    assert text.count("auto_resume.py arm") == 2


def test_entry_fresh_only_in_setup_region():
    text = _load(RUN_SKILL)
    assert text.count("--entry fresh") == 1
    assert "--entry fresh" in _setup_arm_region(text)


def test_entry_resume_only_in_resume_step0_region():
    text = _load(RUN_SKILL)
    assert text.count("--entry resume") == 1
    assert "--entry resume" in _resume_step0_region(text)


def test_reason_budget_only_in_item4_region():
    text = _load(RUN_SKILL)
    assert text.count("--reason budget") == 1
    assert "--reason budget" in _budget_item4_region(text)


def test_reason_session_age_only_in_guard_region():
    text = _load(RUN_SKILL)
    assert text.count("--reason session-age") == 1
    assert "--reason session-age" in _session_age_guard_region(text)


def test_reason_context_only_in_error_handling_region():
    text = _load(RUN_SKILL)
    assert text.count("--reason context") == 1
    assert "--reason context" in _error_handling_region(text)


def test_pause_only_in_error_handling_region():
    text = _load(RUN_SKILL)
    assert text.count("auto_resume.py pause") == 1
    assert "auto_resume.py pause" in _error_handling_region(text)


# ---------------------------------------------------------------------------
# Superseded phrases must be gone (not merely reworded elsewhere).
# ---------------------------------------------------------------------------

def test_superseded_phrases_absent():
    text = _load(RUN_SKILL)
    assert "supervisor relaunch via `/run --resume --autonomous`" not in text
    assert "is unchanged: it reuses this contract" not in text


# ---------------------------------------------------------------------------
# Hook cooperation bullets 1 and 3, and the hard-constraint bullet.
# ---------------------------------------------------------------------------

def test_hook_coop_bullet1_no_longer_cites_budget_item4_line_range():
    region = _hook_coop_bullet1_region(_load(RUN_SKILL))
    assert ":321-326" not in region
    assert "boundary_checkpoint.py" in region


def test_hook_coop_bullet3_names_stop_and_sessionstart_paths():
    region = _hook_coop_bullet3_region(_load(RUN_SKILL))
    assert "Stop-hook continuation" in region
    assert "SessionStart hand-off" in region
    assert "autonomous-mode.md" in region
    assert "auto-resume-<task>.json" in region
    assert "no block to catch" in region


def test_hard_constraint_bullet_no_new_hook_script_wording():
    region = _hard_constraint_region(_load(RUN_SKILL))
    normalized = re.sub(r"\s+", " ", region)
    assert "adds no hook script at runtime" in normalized
    assert "ships with quoin like every" in normalized
    assert "NEVER writes to any file under" in region
    assert "`hooks/`" in region
    assert "NEVER modifies or lowers a `QUOIN_*_BPS` constant" in region


# ---------------------------------------------------------------------------
# autonomous-mode.md coverage: all four knobs, the bound formula, all nine
# halt reasons.
# ---------------------------------------------------------------------------

_KNOBS = (
    "QUOIN_AUTO_RESUME",
    "QUOIN_AUTO_RESUME_MAX",
    "QUOIN_AUTO_RESUME_IDLE_SECS",
    "QUOIN_AUTO_RESUME_HANDOFF_AT",
)

_HALT_REASONS = (
    "auto-resume cap",
    "no forward progress",
    "relaunch cap",
    "session age cap",
    "context exhaustion",
    "paused by user",
    "supervisor stopped by signal",
    "supervisor error",
    "phase completion not repaired: <phase>",
)


def _autonomous_mode_section(text: str) -> str:
    """This is currently the LAST `## ` section in the file, so the slice
    runs to end-of-file; re-anchor to the next heading if that ever changes."""
    start = text.find("## In-session continuation and hand-off")
    assert start != -1, "'## In-session continuation and hand-off' section not found"
    return text[start:]


def test_autonomous_mode_section_exists_and_names_all_knobs():
    text = _load(AUTONOMOUS_MODE)
    assert "## In-session continuation and hand-off" in text
    section = _autonomous_mode_section(text)
    for knob in _KNOBS:
        assert knob in section, f"knob {knob!r} not documented in the new section"


def test_autonomous_mode_section_states_bound_formula():
    section = _autonomous_mode_section(_load(AUTONOMOUS_MODE))
    assert "attempts_before + 2 <= QUOIN_AUTO_RESUME_MAX" in section
    assert "QUOIN_AUTO_RESUME_MAX - attempts_before - 1" in section


def test_autonomous_mode_section_names_all_halt_reasons():
    section = _autonomous_mode_section(_load(AUTONOMOUS_MODE))
    for reason in _HALT_REASONS:
        assert f"`{reason}`" in section, f"halt reason {reason!r} not documented"


def test_autonomous_mode_section_no_autonomous_hooks_pairing():
    """I-02: no line in this memory file pairs 'autonomous' with 'hooks/'."""
    for line in _load(AUTONOMOUS_MODE).splitlines():
        low = line.lower()
        if "autonomous" in low and "hooks/" in low:
            raise AssertionError(f"line pairs 'autonomous' and 'hooks/': {line!r}")


# ---------------------------------------------------------------------------
# Non-human prompt prefix — see module docstring for the documented scope
# reduction from the architecture's `NON_HUMAN_PROMPT_PREFIXES` wishlist.
# ---------------------------------------------------------------------------

def test_only_recorded_non_human_prefix_is_task_notification():
    text = _load(USERPROMPTSUBMIT_SH)
    prefixes = re.findall(r"\(<([a-zA-Z0-9_-]+)>\)", text)
    assert prefixes == ["task-notification"], (
        f"userpromptsubmit.sh's non-human prefix exemption list is {prefixes}; "
        "expected exactly the D-26 seed ['task-notification']. If probe (a2) "
        "was re-run and found additional stable prefixes, update this pin "
        "alongside the exemption regex."
    )
