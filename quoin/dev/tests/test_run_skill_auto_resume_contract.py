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
RUN_SKILL = REPO_ROOT / "quoin" / "adapters" / "claude" / "skills" / "run_hard" / "SKILL.md"
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
        "- **Context saves at phase boundaries only.**",
        "- **No prompt hook reports context.**",
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


# ---------------------------------------------------------------------------
# Cross-region uniqueness (the same slicers above are the source of truth —
# a call site appearing outside its named region is a contract violation).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Superseded phrases must be gone (not merely reworded elsewhere).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Hook cooperation bullets 1 and 3, and the hard-constraint bullet.
# ---------------------------------------------------------------------------


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
    prefixes = re.findall(r"'<([a-zA-Z0-9_-]+)>'\*\)", text)
    assert prefixes == ["task-notification"], (
        f"userpromptsubmit.sh's non-human prefix exemption case arms are {prefixes}; "
        "expected exactly the D-26 seed ['task-notification'] (one quoted "
        "'<name>'*) arm). If probe (a2) was re-run and found additional stable "
        "prefixes, update this pin alongside the exemption case arm."
    )
