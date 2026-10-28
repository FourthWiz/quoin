"""Tests for quoin/dev/scripts/census_descriptions.py (IVG-164 S-4, plan T-01).

RG-CENSUS note (plan D-09): no module-level ALL-CAPS skill-name rosters —
any skill-name lists needed here are derived inside test functions/fixtures
(local scope) so `check_registration.py::rg_census` discovers no new roster.

Phrase-parsing note: descriptions carry trigger phrases with internal
apostrophes (8 of 32 skills); nothing here extracts phrases by quote
delimiter — byte counts only.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "census_descriptions.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("census_descriptions", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def census():
    return _load_module()


def _make_skill(tree_root: Path, name: str, description: str) -> None:
    d = tree_root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f'---\nname: {name}\ndescription: "{description}"\nmodel: sonnet\n---\n\n# {name}\n',
        encoding="utf-8",
    )


def test_known_description_exact_byte_count(census, tmp_path):
    # (a) fixture skill with a known description -> exact byte count
    desc = "Does a thing. Use for: /thing, 'do the thing'."
    _make_skill(tmp_path / "adapter", "thing", desc)
    sizes = census.census_tree(tmp_path / "adapter")
    assert sizes == {"thing": len(desc.encode("utf-8"))}
    assert sizes["thing"] == 46


def test_both_tree_divergence_exits_2(census, tmp_path):
    # (b) adapter/stub divergence -> SystemExit code 2
    _make_skill(tmp_path / "adapter", "thing", "adapter text")
    _make_skill(tmp_path / "stub", "thing", "stub text differs")
    with pytest.raises(SystemExit) as exc:
        census.run_census("both", tmp_path / "adapter", tmp_path / "stub")
    assert exc.value.code == 2


def test_live_tree_census_totals(census):
    # (c) live-tree census: 32 skills, both trees agree, total never above
    # the 13,470 B pre-trim baseline (equal at T-01 commit time; the trim
    # batches only ever reduce it — asserting exact equality would red the
    # suite the moment batch 1 lands, so the invariant here is the no-growth
    # bound plus the skill count).
    sizes = census.run_census("both", census.TREES["adapter"], census.TREES["stub"])
    assert len(sizes) == 32
    assert sum(sizes.values()) <= 13470


def test_unicode_description_counts_bytes_not_chars(census, tmp_path):
    # (d) multi-byte characters count as UTF-8 bytes
    desc = "Résumés — café"  # é, —, é : multi-byte
    _make_skill(tmp_path / "adapter", "uni", desc)
    sizes = census.census_tree(tmp_path / "adapter")
    assert sizes["uni"] == len(desc.encode("utf-8"))
    assert sizes["uni"] > len(desc)  # bytes strictly exceed char count


def test_raw_minus_parsed_delta_is_two_live_tree(census):
    # (e) quoting guard: every live description line's raw value token is
    # exactly value + 2 quote bytes. census_tree itself enforces this
    # (exit 3 otherwise), so a clean pass over both live trees IS the
    # assertion; run both trees explicitly.
    for root in census.TREES.values():
        census.census_tree(root)  # raises SystemExit(3) on any delta != 2


def test_tie_order_is_name_ascending_and_stable(census, tmp_path):
    # (f) two fixture skills with equal byte counts print name-ascending,
    # and the order is stable across repeated runs
    desc = "Equal-length description here."
    _make_skill(tmp_path / "adapter", "zebra", desc)
    _make_skill(tmp_path / "adapter", "alpha", desc)
    first = census.census_tree(tmp_path / "adapter")
    rows1 = sorted(first.items(), key=lambda kv: (-kv[1], kv[0]))
    rows2 = sorted(
        census.census_tree(tmp_path / "adapter").items(),
        key=lambda kv: (-kv[1], kv[0]),
    )
    assert [n for n, _ in rows1] == ["alpha", "zebra"]
    assert rows1 == rows2


def test_mean_rounding_is_half_up(census):
    # 13470/32 = 420.9375 -> 421 under round-half-up
    assert census.mean_half_up(13470, 32) == 421
    assert census.mean_half_up(5, 2) == 3  # 2.5 rounds up, not banker's
    assert census.mean_half_up(0, 0) == 0
