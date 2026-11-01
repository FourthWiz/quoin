"""Honesty checks for the OpenCode adapter's managed-deployment claims."""
from __future__ import annotations

import re
from pathlib import Path
from typing import List

DECISIONS = Path(__file__).resolve().parents[2] / "adapters" / "opencode" / "decisions.md"
SECTION = "## Workflow parity residuals"
CLOSED_ONLY_WHEN = (
    "A write through an absolute path",
    "There is no network isolation for snapshot or test runs",
    "A passing test result attests only to what the code",
)


def _section_bullets(text: str, heading: str) -> List[str]:
    """Bullets of one section: a bullet starts at `- ` and joins every
    continuation line up to the next bullet or heading."""
    lines = text.splitlines()
    start = lines.index(heading) + 1
    bullets: List[str] = []
    for line in lines[start:]:
        if line.startswith("#"):
            break
        if line.startswith("- "):
            bullets.append(line[2:].strip())
        elif line.strip() and bullets:
            bullets[-1] += " " + line.strip()
    return bullets


def test_the_parity_residual_bullets_state_when_each_limit_is_closed():
    bullets = _section_bullets(DECISIONS.read_text(encoding="utf-8"), SECTION)
    assert bullets
    for prefix in CLOSED_ONLY_WHEN:
        matching = [b for b in bullets if b.startswith(prefix)]
        assert len(matching) == 1, prefix
        assert "only when" in matching[0] and "otherwise" in matching[0], prefix
    for bullet in bullets:
        if re.search(r"\bclosed\b", bullet):
            assert "only when" in bullet, bullet
