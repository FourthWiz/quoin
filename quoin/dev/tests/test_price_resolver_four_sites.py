"""test_price_resolver_four_sites.py — IVG-260 T-12.

Model-price membership is tested at four independent sites:
  1. cost_from_jsonl.cost_for_entry
  2. cost_from_jsonl.parse_session
  3. spend_monitor.parse_session_today
  4. agent_transcript_cost.price_agent_jsonl

Normalizing at fewer than all four yields split-brain output — a non-zero
cost alongside priceable=false, or the reverse. This file asserts all four
agree, in both directions, for a normalized slug, a genuinely-unknown live
slug, and the "<synthetic>" non-model sentinel.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
from datetime import datetime, timezone

import pytest

# ---------------------------------------------------------------------------
# Load all three adapter modules, each via its own established test idiom.
# ---------------------------------------------------------------------------
REPO_ROOT = pathlib.Path(__file__).parent.parent.parent.parent  # quoin/ repo root
SCRIPTS_DIR = pathlib.Path(__file__).parent.parent.parent / "scripts"  # quoin/quoin/scripts/
CORE_SCRIPTS_DIR = pathlib.Path(__file__).parent.parent.parent / "core" / "scripts"

sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(CORE_SCRIPTS_DIR))
import cost_from_jsonl as cfj  # noqa: E402
import agent_transcript_cost as atc  # noqa: E402


def _load_spend_monitor():
    """Load spend_monitor core module directly (mirrors test_spend_monitor.py:27-41)."""
    key = "_test_price_resolver_spend_monitor_core"
    if key in sys.modules:
        return sys.modules[key]
    core_path = REPO_ROOT / "quoin" / "core" / "scripts" / "spend_monitor.py"
    spec = importlib.util.spec_from_file_location(key, core_path)
    assert spec is not None, f"Cannot create spec for {core_path}"
    assert spec.loader is not None, f"Spec has no loader for {core_path}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod


sm = _load_spend_monitor()

_USAGE = {
    "input_tokens": 1000, "output_tokens": 500,
    "cache_creation_input_tokens": 200, "cache_read_input_tokens": 300,
}


def _ts_today() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_row(path: pathlib.Path, model: str, usage: dict, with_timestamp: bool = True) -> None:
    row = {"message": {"model": model, "usage": usage}}
    if with_timestamp:
        row["timestamp"] = _ts_today()
    path.write_text(json.dumps(row) + "\n")


# ---------------------------------------------------------------------------
# Positive direction — a normalized (dated) slug
# ---------------------------------------------------------------------------
def test_four_sites_agree_on_normalized_slug(tmp_path):
    model = "claude-sonnet-4-6-20260101"
    base_model = "claude-sonnet-4-6"

    # Site 1: cost_for_entry — normalized slug costs the same as the base slug.
    assert cfj.cost_for_entry(model, _USAGE) == cfj.cost_for_entry(base_model, _USAGE)

    # Site 2: parse_session.
    jf = tmp_path / "00000000-0000-0000-0000-0000000000f1.jsonl"
    _write_row(jf, model, _USAGE, with_timestamp=False)
    r2 = cfj.parse_session(jf)
    assert r2["priceable"] is True
    assert r2["unknown_models"] == []

    # Site 3: spend_monitor.parse_session_today (needs a timestamp inside
    # today's UTC window).
    jf3 = tmp_path / "today1.jsonl"
    _write_row(jf3, model, _USAGE, with_timestamp=True)
    day_start, day_end = sm._local_day_bounds()
    r3 = sm.parse_session_today(jf3, day_start, day_end)
    assert r3["priceable"] is True
    assert r3["unknown_models"] == []

    # Site 4: agent_transcript_cost.price_agent_jsonl.
    r4 = atc.price_agent_jsonl(jf)
    assert r4["priceable"] is True
    assert r4["usd"] > 0


# ---------------------------------------------------------------------------
# Negative direction — a genuinely unknown, live, non-Anthropic slug
# ---------------------------------------------------------------------------
def test_four_sites_agree_on_unknown_slug(tmp_path):
    model = "deepseek/deepseek-v4-pro"

    cost, _ = cfj.cost_for_entry(model, _USAGE)
    assert cost == 0.0

    jf = tmp_path / "00000000-0000-0000-0000-0000000000f2.jsonl"
    _write_row(jf, model, _USAGE, with_timestamp=False)
    r2 = cfj.parse_session(jf)
    assert r2["totalCost"] == 0.0
    assert r2["priceable"] is False
    assert r2["unknown_models"] == [model]

    jf3 = tmp_path / "today2.jsonl"
    _write_row(jf3, model, _USAGE, with_timestamp=True)
    day_start, day_end = sm._local_day_bounds()
    r3 = sm.parse_session_today(jf3, day_start, day_end)
    assert r3["priceable"] is False
    assert model in r3["unknown_models"]

    r4 = atc.price_agent_jsonl(jf)
    assert r4["priceable"] is False
    assert r4["usd"] is None


# ---------------------------------------------------------------------------
# The <synthetic> sentinel — closes the coverage gap architecture D-07
# requires: the same four-site agreement discipline, now including the
# sentinel direction.
# ---------------------------------------------------------------------------
_SENTINEL_USAGE = {
    "input_tokens": 0, "output_tokens": 0,
    "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
}


def test_four_sites_agree_on_synthetic_sentinel(tmp_path):
    model = "<synthetic>"

    cost, _ = cfj.cost_for_entry(model, _SENTINEL_USAGE)
    assert cost == 0.0

    jf = tmp_path / "00000000-0000-0000-0000-0000000000f3.jsonl"
    _write_row(jf, model, _SENTINEL_USAGE, with_timestamp=False)
    r2 = cfj.parse_session(jf)
    assert r2["priceable"] is True
    assert r2["sentinel_rows"] == 1
    assert r2["unknown_models"] == []

    jf3 = tmp_path / "today3.jsonl"
    _write_row(jf3, model, _SENTINEL_USAGE, with_timestamp=True)
    day_start, day_end = sm._local_day_bounds()
    r3 = sm.parse_session_today(jf3, day_start, day_end)
    assert r3["priceable"] is True
    assert r3["unknown_models"] == []

    r4 = atc.price_agent_jsonl(jf)
    assert r4["priceable"] is True
    assert r4["usd"] == 0.0


# ---------------------------------------------------------------------------
# This file must not pin a CLOSED PRICES key set (same discipline as
# test_cost_from_jsonl.py::test_no_closed_prices_key_set_enumeration_in_suite).
# ---------------------------------------------------------------------------
def test_no_closed_prices_key_set_enumeration_in_this_file():
    import re

    text = pathlib.Path(__file__).read_text(encoding="utf-8")
    closed_set_re = re.compile(
        r'len\(\s*(?:cfj\.|_cfj\.|sm\.)?PRICES\s*\)\s*==|'
        r'set\(\s*(?:cfj\.|_cfj\.|sm\.)?PRICES(?:\.keys\(\))?\s*\)\s*==|'
        r'(?:cfj\.|_cfj\.|sm\.)?PRICES\.keys\(\)\s*==\s*\{'
    )
    assert not closed_set_re.search(text)
