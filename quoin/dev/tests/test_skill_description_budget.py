"""Skill descriptions are listed in every session, so their size is capped."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
ADAPTER = _ROOT / "adapters" / "claude" / "skills"
STUBS = _ROOT / "skills"

PER_SKILL_BYTES = 200
TOTAL_BYTES = 5200


def _description(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    match = re.search(r'^description:\s*"(.*)"\s*$', text, re.MULTILINE)
    assert match, f"no one-line quoted description in {path}"
    return match.group(1)


def _names() -> list[str]:
    return sorted(p.parent.name for p in ADAPTER.glob("*/SKILL.md"))


def test_skill_set_is_not_empty():
    assert len(_names()) >= 31


@pytest.mark.parametrize("name", _names())
def test_description_within_budget_and_names_its_command(name):
    desc = _description(ADAPTER / name / "SKILL.md")
    assert len(desc.encode("utf-8")) <= PER_SKILL_BYTES
    invocation = "/next-steps" if name == "next_steps" else "/" + name
    assert invocation in desc
    assert _description(STUBS / name / "SKILL.md") == desc


def test_total_description_bytes_within_budget():
    total = sum(len(_description(ADAPTER / n / "SKILL.md").encode("utf-8")) for n in _names())
    assert total <= TOTAL_BYTES, total


def test_revise_fast_is_listed_by_name_only():
    from quoin.installer import SKILL_OVERRIDES

    assert SKILL_OVERRIDES["revise-fast"] == "name-only"
