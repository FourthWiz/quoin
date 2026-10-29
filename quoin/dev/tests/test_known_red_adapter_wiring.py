"""IVG-144 T-16: adapter-lint token greps over gate/review SKILL.md.

Cheap literal-token asserts (mirroring test_gate_affected_area_tokens.py /
test_gate_ci_mirror_tokens.py) that verify the known-red manifest wiring
landed in the active adapter files (R-05, lessons 2026-07-16 #6). Also guards:
  - the MAJ-3 no-self-run invariant at the adapter-text level (no `subprocess`
    self-run string paired with known_red.py; an explicit MAJ-3 marker present);
  - that the known-red wiring does NOT introduce a new drift-guarded named-check
    literal into the gate audit `## Automated checks` enumeration (it rides the
    free `## Warnings (non-blocking)` section instead).
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
ADAPTERS = REPO_ROOT / "quoin" / "adapters" / "claude" / "skills"
GATE_SKILL = ADAPTERS / "gate" / "SKILL.md"
REVIEW_SKILL = ADAPTERS / "review" / "SKILL.md"

# Tokens the full-suite gate wiring introduces (identity/count/staleness).
GATE_TOKENS = (
    "known_red.py",
    "--pytest-output",
    "--junit",
    "--observed-rc",
    "--selectors",
    "--full-suite",
    "known-baseline",
)
# Review's affected-area path uses no junit and no full-suite (MIN-B / R-08).
REVIEW_TOKENS = (
    "known_red.py",
    "--pytest-output",
    "--observed-rc",
    "--selectors",
    "known-baseline",
)


def test_gate_adapter_contains_known_red_tokens():
    text = GATE_SKILL.read_text(encoding="utf-8")
    for tok in GATE_TOKENS:
        assert tok in text, f"gate SKILL.md missing known-red wiring token {tok!r}"


def test_review_adapter_contains_known_red_tokens():
    text = REVIEW_SKILL.read_text(encoding="utf-8")
    for tok in REVIEW_TOKENS:
        assert tok in text, f"review SKILL.md missing known-red wiring token {tok!r}"


def test_no_self_run_string_in_adapters():
    """MAJ-3: known_red.py must never be described as running pytest itself."""
    for skill in (GATE_SKILL, REVIEW_SKILL):
        text = skill.read_text(encoding="utf-8")
        assert "subprocess" not in text, f"{skill} must not describe a known_red.py self-run"
        # explicit no-self-run marker present
        assert "MAJ-3" in text, f"{skill} missing the MAJ-3 no-self-run marker"


def test_no_new_named_check_in_drift_guarded_audit_enum():
    """The known-red wiring must not add a new named check to the drift-guarded
    `## Automated checks` audit enumeration (it uses the free
    `## Warnings (non-blocking)` section instead — plan T-13d / T-16)."""
    text = GATE_SKILL.read_text(encoding="utf-8")
    marker = "Compose the format-aware body per format-kit.md"
    idx = text.find(marker)
    assert idx != -1, "could not locate the gate audit-enumeration region"
    # The audit enumeration paragraph begins at the marker; scan to the next H2.
    tail = text[idx:]
    end = tail.find("\n## ")
    audit_region = tail if end == -1 else tail[:end]
    assert "known_red" not in audit_region, (
        "known-red wiring must not introduce a new drift-guarded named check "
        "into the gate audit `## Automated checks` enumeration"
    )
