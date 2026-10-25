"""
cost.py — Cost estimation from pricing.json.

Exposes:
    load_pricing() -> dict
    estimate_cost(model, tokens_in, tokens_out, cache_write, cache_read, pricing) -> Decimal | None

For Claude cells:
    When runtime-reported cost is present, harness records BOTH the runtime
    value AND the estimated value plus their delta as a sanity check.

For Codex cells:
    Returns None. Harness writes cost: not_available in cost.json.
    No token-derived estimate. See quoin/adapters/codex/cost.md.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Optional

# Default path to pricing.json relative to repo root
_DEFAULT_PRICING_PATH = Path("quoin/benchmarks/harness/pricing.json")

# Codex model name prefix (any model starting with this should return None)
_CODEX_MODEL_PREFIXES = ("gpt-", "o1-", "o3-", "o4-", "codex-")


# Mirrors quoin/scripts/cost_from_jsonl.py so the harness stays standalone; a
# parity test pins the two together.
NON_MODEL_SENTINELS = frozenset({"<synthetic>"})
MODEL_ID_NORMALIZERS = (
    (re.compile(r"\[1m\]$"), ""),
    (re.compile(r"-\d{8}$"), ""),
)


class UnpricedModelError(LookupError):
    """A Claude model has no entry in the pricing card."""

    def __init__(self, model: str):
        super().__init__(f"no pricing entry for model {model!r}")
        self.model = model


def is_costable(model) -> bool:
    """True iff `model` is a non-empty string that is not a non-model sentinel."""
    return bool(model) and model not in NON_MODEL_SENTINELS


def _is_codex_model(model: str) -> bool:
    return model.lower().startswith(_CODEX_MODEL_PREFIXES)


def resolve_model_pricing(model, pricing: dict) -> Optional[dict]:
    """Return the pricing entry for `model`: exact key first, then one retry on
    the normalised id. No prefix scan, so an unknown model never inherits a
    related model's rates."""
    if not is_costable(model):
        return None
    models_section = pricing.get("models", {})
    exact = models_section.get(model)
    if exact is not None:
        return exact
    normalized = model
    while True:
        changed = False
        for pattern, replacement in MODEL_ID_NORMALIZERS:
            rewritten = pattern.sub(replacement, normalized)
            if rewritten != normalized:
                normalized = rewritten
                changed = True
        if not changed:
            break
    if normalized == model:
        return None
    return models_section.get(normalized)


def load_pricing(pricing_path: Optional[Path] = None) -> dict:
    """
    Load the pricing.json card.

    Parameters
    ----------
    pricing_path:
        Optional path to pricing.json. Defaults to
        quoin/benchmarks/harness/pricing.json relative to cwd.

    Returns
    -------
    Parsed pricing dict, or empty dict on failure.
    """
    if pricing_path is None:
        pricing_path = _DEFAULT_PRICING_PATH

    if not pricing_path.exists():
        # Try relative to this file
        local_path = Path(__file__).parent / "pricing.json"
        if local_path.exists():
            pricing_path = local_path
        else:
            return {}

    try:
        return json.loads(pricing_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def estimate_cost(
    model: str,
    tokens_in: Optional[int],
    tokens_out: Optional[int],
    cache_write: Optional[int] = None,
    cache_read: Optional[int] = None,
    pricing: Optional[dict] = None,
    strict: bool = True,
) -> Optional[Decimal]:
    """
    Estimate cost in USD for a model invocation using the pricing card.

    For Codex models: always returns None (per D-12 and codex/cost.md).
    For Claude models: returns a Decimal cost estimate. A model with no entry in
      the pricing card raises UnpricedModelError, or returns None when
      strict=False.

    Parameters
    ----------
    model:
        The model identifier (dated snapshot form for Claude; model name for Codex).
    tokens_in:
        Number of input tokens (excluding cached tokens).
    tokens_out:
        Number of output tokens.
    cache_write:
        Number of cache-write input tokens (prompt cache writes).
    cache_read:
        Number of cache-read input tokens (prompt cache hits).
    pricing:
        The loaded pricing dict from load_pricing(). If None, loads from disk.
    strict:
        When True (default) an unpriced Claude model raises; when False it
        returns None.

    Returns
    -------
    Decimal cost in USD rounded to 6 decimal places, or None if not computable.
    """
    # Codex models: always return None (no token-derived estimates per codex/cost.md)
    if _is_codex_model(model):
        return None

    if pricing is None:
        pricing = load_pricing()

    model_pricing = resolve_model_pricing(model, pricing)
    if model_pricing is None:
        if strict:
            raise UnpricedModelError(model)
        return None

    input_rate = Decimal(str(model_pricing.get("input_per_1m_usd", 0)))
    output_rate = Decimal(str(model_pricing.get("output_per_1m_usd", 0)))
    cache_write_rate = Decimal(str(model_pricing.get("cache_write_per_1m_usd", 0)))
    cache_read_rate = Decimal(str(model_pricing.get("cache_read_per_1m_usd", 0)))

    million = Decimal("1000000")

    cost = Decimal("0")
    if tokens_in:
        cost += Decimal(str(tokens_in)) / million * input_rate
    if tokens_out:
        cost += Decimal(str(tokens_out)) / million * output_rate
    if cache_write:
        cost += Decimal(str(cache_write)) / million * cache_write_rate
    if cache_read:
        cost += Decimal(str(cache_read)) / million * cache_read_rate

    return cost.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


def estimate_model_usage(
    model_usage: dict, pricing: dict
) -> "tuple[Optional[Decimal], list[str]]":
    """Sum per-model estimates over a runtime `modelUsage` mapping.

    Returns (total, unpriced_models). Non-costable keys and buckets without
    usage are skipped. The total is None when any costable Claude model is
    unpriced. Haiku 5.5 buckets are priced at the base rates, a lower bound.
    """
    total = Decimal("0")
    unpriced: list[str] = []
    for model, bucket in (model_usage or {}).items():
        if not is_costable(model) or not isinstance(bucket, dict):
            continue
        tokens_in = bucket.get("inputTokens") or 0
        tokens_out = bucket.get("outputTokens") or 0
        cache_write = bucket.get("cacheCreationInputTokens") or 0
        cache_read = bucket.get("cacheReadInputTokens") or 0
        if not (tokens_in or tokens_out or cache_write or cache_read):
            continue
        if _is_codex_model(model):
            continue
        try:
            amount = estimate_cost(
                model, tokens_in, tokens_out, cache_write, cache_read, pricing
            )
        except UnpricedModelError:
            unpriced.append(model)
            continue
        if amount is not None:
            total += amount
    if unpriced:
        return None, sorted(unpriced)
    return total, []


def estimate_session_cost(
    model: str,
    model_usage: Optional[dict],
    tokens_in: Optional[int],
    tokens_out: Optional[int],
    cache_write: Optional[int],
    cache_read: Optional[int],
    pricing: dict,
) -> "tuple[Optional[Decimal], list[str]]":
    """Per-model estimate when `model_usage` is present, else a best-effort
    estimate from aggregate tokens for `model`. Returns (usd_or_None,
    unpriced_models); never raises for an unpriced model."""
    if model_usage:
        return estimate_model_usage(model_usage, pricing)
    amount = estimate_cost(
        model, tokens_in, tokens_out, cache_write, cache_read, pricing,
        strict=False,
    )
    if amount is None and is_costable(model) and not _is_codex_model(model):
        return None, [model]
    return amount, []
