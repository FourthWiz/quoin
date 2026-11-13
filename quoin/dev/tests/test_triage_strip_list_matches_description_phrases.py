"""Triage description/body strip-list coupling guard.

triage's body "Step 1: Strip trigger phrases" list feeds its own Signal B
scoring and mirrors the frontmatter description's trigger phrases. The
description trim froze all 5 existing phrases verbatim (zero-deviation
default — no body edit anywhere; the stub tree has no body at all).

This test hardcodes the 5 frozen phrases as literals rather than
extracting them from the description — the 5th phrase contains an internal
apostrophe, so any quote-delimiter parse would truncate it to "I" (the
hazard is systemic: 8 of 33 skills carry apostrophes in trigger
phrases, so no phrase-level check may parse by quote delimiter).

Assertions:
  1. every frozen phrase is (case-insensitively) present in the ADAPTER-tree
     frontmatter description — catches a future description edit silently
     dropping a phrase the body still relies on;
  2. the ADAPTER-tree body strip-list (anchor-located, case/whitespace-
     normalized) is a SUBSET of the frozen set plus "/triage".
No stub-tree comparison — the stub is a frontmatter-only pointer with no body.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_ADAPTER_SKILL = (
    Path(__file__).resolve().parents[2]
    / "adapters" / "claude" / "skills" / "triage" / "SKILL.md"
)

# D-08-frozen literals (NOT extracted from the description — see module docstring)
FROZEN_PHRASES = [
    "what should I run",
    "which skill fits this",
    "route this",
    "pick the right command for me",
    "I'm not sure what to do next",
]


def _description_value(text: str) -> str:
    lines = text.split("\n")
    end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    data = yaml.safe_load("\n".join(lines[1:end]))
    return data["description"]


def _body_strip_list(text: str) -> list[str]:
    # Anchor-located (line offsets drift): find the lead-in line, then consume
    # the following '- "..."' bullets. Each entry is its own double-quoted
    # markdown bullet, so there is no delimiter collision with apostrophes.
    lines = text.split("\n")
    anchor = next(
        i for i, ln in enumerate(lines) if "The trigger list to strip:" in ln
    )
    items: list[str] = []
    for ln in lines[anchor + 1:]:
        stripped = ln.strip()
        if not stripped:
            if items:
                break
            continue
        if stripped.startswith('- "') and stripped.endswith('"'):
            items.append(stripped[3:-1])
        else:
            break
    return items


def test_frozen_phrases_present_in_description():
    text = _ADAPTER_SKILL.read_text(encoding="utf-8")
    description = _description_value(text)
    for phrase in FROZEN_PHRASES:
        assert phrase.lower() in description.lower(), (
            f"triage description dropped frozen phrase {phrase!r} "
            "(D-08 phrase-freeze violated)"
        )


def test_body_strip_list_is_subset_of_frozen_phrases():
    text = _ADAPTER_SKILL.read_text(encoding="utf-8")
    body_strip_list = _body_strip_list(text)
    assert len(body_strip_list) == 6, (
        f"expected 6 strip-list bullets (5 phrases + /triage), got "
        f"{len(body_strip_list)}: {body_strip_list!r} — anchor parse may be vacuous"
    )
    frozen = {p.lower() for p in FROZEN_PHRASES} | {"/triage"}
    assert {p.lower().strip() for p in body_strip_list} <= frozen, (
        "triage body strip-list contains a phrase the frontmatter description "
        "no longer carries (D-08 coupling broken)"
    )
