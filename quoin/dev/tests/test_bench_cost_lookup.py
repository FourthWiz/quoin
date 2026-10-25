"""Exact-then-normalised pricing lookup and per-model estimates in the benchmark harness."""
from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from quoin.benchmarks.harness import cost

SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"
PRICING_PATH = (
    Path(__file__).resolve().parent.parent.parent / "benchmarks" / "harness" / "pricing.json"
)


@pytest.fixture(scope="module")
def pricing():
    return json.loads(PRICING_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def cfj():
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import cost_from_jsonl

    return cost_from_jsonl


def test_unpriced_claude_model_raises_naming_it(pricing):
    # Before the exact lookup this id priced as the 4.7 entry via a prefix scan.
    with pytest.raises(cost.UnpricedModelError) as excinfo:
        cost.estimate_cost("claude-opus-5-6", 1000, 500, pricing=pricing)
    assert excinfo.value.model == "claude-opus-5-6"
    assert "claude-opus-5-6" in str(excinfo.value)


@pytest.mark.parametrize("model", ["claude-opus-5-5[1m]", "claude-haiku-5-5-20261001"])
def test_normaliser_resolves_suffixed_ids(pricing, model):
    entry = cost.resolve_model_pricing(model, pricing)
    assert entry is not None
    assert cost.estimate_cost(model, 1000, 500, pricing=pricing) is not None


def test_non_model_sentinel_is_ignored(pricing):
    assert cost.resolve_model_pricing("<synthetic>", pricing) is None
    total, unpriced = cost.estimate_model_usage(
        {"<synthetic>": {"inputTokens": 5}, "claude-opus-5-5": {"inputTokens": 1_000_000}},
        pricing,
    )
    assert unpriced == []
    assert total == Decimal("4.000000")


def test_codex_model_returns_none(pricing):
    assert cost.estimate_cost("gpt-5.5", 1000, 500, pricing=pricing) is None


def test_estimate_model_usage_sums_and_lists_unpriced(pricing):
    usage = {
        "claude-opus-5-5": {"inputTokens": 1_000_000},
        "claude-sonnet-5-5": {"outputTokens": 1_000_000},
    }
    total, unpriced = cost.estimate_model_usage(usage, pricing)
    assert unpriced == []
    assert total == Decimal("14.000000")

    usage["claude-opus-9-9"] = {"inputTokens": 10}
    total, unpriced = cost.estimate_model_usage(usage, pricing)
    assert total is None
    assert unpriced == ["claude-opus-9-9"]


def test_estimate_session_cost_falls_back_to_aggregate_tokens(pricing):
    amount, unpriced = cost.estimate_session_cost(
        "claude-opus-5-5", None, 1_000_000, 0, 0, 0, pricing
    )
    assert (amount, unpriced) == (Decimal("4.000000"), [])
    amount, unpriced = cost.estimate_session_cost(
        "claude-opus-9-9", None, 10, 0, 0, 0, pricing
    )
    assert amount is None and unpriced == ["claude-opus-9-9"]


def test_harness_lookup_mirrors_the_authoritative_script(pricing, cfj):
    assert cost.NON_MODEL_SENTINELS == cfj.NON_MODEL_SENTINELS
    assert [(p.pattern, r) for p, r in cost.MODEL_ID_NORMALIZERS] == [
        (p.pattern, r) for p, r in cfj.MODEL_ID_NORMALIZERS
    ]
    probes = [
        "claude-opus-5-5", "claude-opus-5-5[1m]", "claude-haiku-5-5-20261001",
        "claude-sonnet-4-6", "claude-sonnet-4-6[1m]", "claude-opus-5-6",
        "<synthetic>", "", "claude-nonexistent-9999",
    ]
    for model in probes:
        assert cost.is_costable(model) == cfj.is_costable(model), model
        harness = cost.resolve_model_pricing(model, pricing)
        reference = cfj.resolve_prices(model)
        if harness is not None:
            # Every model the card prices is priced identically by the script.
            assert reference is not None, model
            assert harness["input_per_1m_usd"] == reference["input"], model


def test_resolved_rates_equal_the_authoritative_script(pricing, cfj):
    for model, rates in pricing["models"].items():
        reference = cfj.resolve_prices(model)
        found = cost.resolve_model_pricing(model, pricing)
        assert found is rates
        assert found["input_per_1m_usd"] == reference["input"]
        assert found["output_per_1m_usd"] == reference["output"]
        assert found["cache_write_per_1m_usd"] == reference["cache_create"]
        assert found["cache_read_per_1m_usd"] == reference["cache_read"]


def test_no_prefix_scan_left_in_the_harness_cost_module():
    source = (PRICING_PATH.parent / "cost.py").read_text(encoding="utf-8")
    assert 'rsplit("-", 1)[0]' not in source
