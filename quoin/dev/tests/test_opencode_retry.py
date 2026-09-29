"""Tests for the pure retry policy and usage accounting."""
from __future__ import annotations

import ast
import inspect
from datetime import datetime, timezone
from decimal import Decimal

import pytest

import _opencode_helpers as helpers
from _opencode_merge_helpers import loaded
from quoin.opencode_adapter import merge, retry
from quoin.opencode_adapter.retry import Failure, GiveUp, Retry, RetryPolicy, Usage, aggregate

WALL = datetime(2026, 9, 30, 12, 0, 0, tzinfo=timezone.utc)
SRC = helpers.SOURCE_DIR.parent / "src" / "quoin" / "opencode_adapter" / "retry.py"


class Clock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


def policy(max_retries=3, max_elapsed=None, jitter=0.5, clock=None, **kw):
    return RetryPolicy(
        max_retries=max_retries, max_elapsed=max_elapsed,
        rng=lambda: jitter, clock=clock or Clock(), wall_clock=lambda: WALL, **kw,
    )


# ------------------------------------------------------------- transience


@pytest.mark.parametrize(
    "failure",
    [
        Failure("connect"), Failure("timeout"),
        Failure("http", 429), Failure("http", 500), Failure("http", 503), Failure("http", 599),
    ],
)
def test_transient_failures_retry(failure):
    assert isinstance(policy().decide(1, 0.0, failure), Retry)


@pytest.mark.parametrize(
    "failure",
    [
        Failure("auth"), Failure("policy"), Failure("config"),
        Failure("http", 400), Failure("http", 401), Failure("http", 403), Failure("http", 404),
        Failure("http", 499), Failure("http", 600), Failure("http", 200),
    ],
)
def test_other_failures_do_not_retry(failure):
    assert policy().decide(1, 0.0, failure) == GiveUp("not-transient")


def test_a_mutating_tool_in_flight_blocks_every_retry():
    for kind, status in (("connect", None), ("timeout", None), ("http", 503)):
        failure = Failure(kind, status, mutating_tool_in_flight=True)
        assert policy().decide(1, 0.0, failure) == GiveUp("mutating-tool-in-flight")
    # the mutating check comes before the transient check
    assert policy().decide(1, 0.0, Failure("auth", mutating_tool_in_flight=True)) == GiveUp(
        "mutating-tool-in-flight"
    )


def test_failure_validation():
    with pytest.raises(ValueError):
        Failure("nope")
    with pytest.raises(ValueError):
        Failure("http")
    with pytest.raises(ValueError):
        Failure("http", True)
    with pytest.raises(ValueError):
        Failure("http", "503")
    with pytest.raises(ValueError):
        Failure("connect", retry_after=7)
    with pytest.raises(ValueError):
        GiveUp("because")


# ------------------------------------------------------------ Retry-After


@pytest.mark.parametrize(
    "header,expected",
    [("7", 7.0), (" 7 ", 7.0), ("0", 0.0), ("120", 30.0), ("9" * 5000, 30.0)],
)
def test_retry_after_seconds_are_honoured_without_jitter_and_capped(header, expected):
    for jitter in (0.0, 0.999):
        got = policy(jitter=jitter).decide(1, 0.0, Failure("http", 429, retry_after=header))
        assert got == Retry(expected)


def test_retry_after_http_date_reads_the_injected_wall_clock():
    ahead = "Wed, 30 Sep 2026 12:00:10 GMT"
    assert policy().decide(1, 0.0, Failure("http", 503, retry_after=ahead)) == Retry(10.0)
    past = "Wed, 30 Sep 2026 11:00:00 GMT"
    assert policy().decide(1, 0.0, Failure("http", 503, retry_after=past)) == Retry(0.0)
    far = "Wed, 30 Sep 2026 14:00:00 GMT"
    assert policy().decide(1, 0.0, Failure("http", 503, retry_after=far)) == Retry(30.0)
    naive = "30 Sep 2026 12:00:05 -0000"
    assert policy().decide(1, 0.0, Failure("http", 503, retry_after=naive)) == Retry(5.0)


@pytest.mark.parametrize("header", ["soon", "-5", "1.5", "", "1e3", "٣"])
def test_an_unusable_retry_after_falls_back_to_backoff(header):
    got = policy(jitter=0.5).decide(1, 0.0, Failure("http", 429, retry_after=header))
    assert got == Retry(0.5)  # 0.5 * min(30, 1 * 2**0)


# --------------------------------------------------------------- backoff


@pytest.mark.parametrize("attempt", [1, 2, 3, 4, 5, 6])
def test_full_jitter_bounds(attempt):
    ceiling = min(30.0, 1.0 * 2 ** (attempt - 1))
    low = policy(max_retries=10, jitter=0.0).decide(attempt, 0.0, Failure("timeout"))
    high = policy(max_retries=10, jitter=0.999).decide(attempt, 0.0, Failure("timeout"))
    assert low == Retry(0.0)
    assert isinstance(high, Retry) and high.delay == pytest.approx(0.999 * ceiling)
    assert high.delay < ceiling


def test_the_cap_is_reached_at_high_attempts():
    got = policy(max_retries=20, jitter=1.0).decide(12, 0.0, Failure("connect"))
    assert got == Retry(30.0)


def test_base_and_cap_are_configurable():
    got = policy(max_retries=5, jitter=1.0, base=0.25, cap=2.0).decide(3, 0.0, Failure("connect"))
    assert got == Retry(1.0)
    got = policy(max_retries=5, jitter=1.0, base=0.25, cap=0.5).decide(3, 0.0, Failure("connect"))
    assert got == Retry(0.5)


# ------------------------------------------------------------- the caps


def test_the_attempt_cap():
    p = policy(max_retries=2)
    assert isinstance(p.decide(1, 0.0, Failure("connect")), Retry)
    assert isinstance(p.decide(2, 0.0, Failure("connect")), Retry)
    assert p.decide(3, 0.0, Failure("connect")) == GiveUp("attempts-exhausted")


def test_no_retries_configured_gives_up_at_once():
    assert policy(max_retries=0).decide(1, 0.0, Failure("connect")) == GiveUp("attempts-exhausted")


def test_attempt_must_be_a_positive_integer():
    for bad in (0, -1, True, 1.0, "1"):
        with pytest.raises(ValueError):
            policy().decide(bad, 0.0, Failure("connect"))


def test_the_elapsed_cap():
    clock = Clock()
    p = policy(max_elapsed=10.0, jitter=0.5, clock=clock)
    started = p.start()
    clock.now = 1.0
    assert p.decide(1, started, Failure("connect")) == Retry(0.5)
    clock.now = 10.0
    assert p.decide(1, started, Failure("connect")) == GiveUp("time-exhausted")
    clock.now = 20.0
    assert p.decide(1, started, Failure("connect")) == GiveUp("time-exhausted")


def test_a_delay_that_would_cross_the_limit_gives_up():
    clock = Clock(9.0)
    p = policy(max_elapsed=10.0, clock=clock)
    header = Failure("http", 429, retry_after="1")
    assert p.decide(1, 0.0, header) == GiveUp("time-exhausted")  # 9 + 1 >= 10
    assert isinstance(p.decide(1, 0.0, Failure("http", 429, retry_after="0")), Retry)


def test_without_a_time_limit_it_is_never_time_exhausted():
    clock = Clock(1e9)
    got = policy(max_elapsed=None, clock=clock).decide(1, 0.0, Failure("connect"))
    assert isinstance(got, Retry)


def test_start_reads_the_injected_clock():
    assert policy(clock=Clock(42.0)).start() == 42.0


# ---------------------------------------------------------- from_limits


def test_from_limits_reads_both_limits():
    p = RetryPolicy.from_limits(
        {"max_transient_retries": 3, "max_run_seconds": 60}, rng=lambda: 0.0, clock=Clock(),
    )
    assert (p.max_retries, p.max_elapsed) == (3, 60.0)


def test_from_limits_defaults():
    p = RetryPolicy.from_limits({})
    assert p.max_retries == 0 and p.max_elapsed is None
    assert p.decide(1, p.start(), Failure("connect")) == GiveUp("attempts-exhausted")
    only_retries = RetryPolicy.from_limits({"max_transient_retries": 2})
    assert only_retries.max_elapsed is None


@pytest.mark.parametrize("bad", [True, False, 0, -1, "3", 2.5, None])
@pytest.mark.parametrize("name", ["max_transient_retries", "max_run_seconds"])
def test_from_limits_rejects_bad_values(name, bad):
    with pytest.raises(ValueError):
        RetryPolicy.from_limits({name: bad})


def test_default_clocks_and_rng_are_usable():
    p = RetryPolicy.from_limits({"max_transient_retries": 1})
    got = p.decide(1, p.start(), Failure("connect"))
    assert isinstance(got, Retry) and 0.0 <= got.delay <= 1.0
    date = "Wed, 30 Sep 2099 12:00:00 GMT"
    assert p.decide(1, p.start(), Failure("http", 429, retry_after=date)) == Retry(30.0)


def test_from_limits_agrees_with_the_effective_limits():
    effective = merge.merge(loaded())
    limits = {
        name: effective.values["limits." + name].value
        for name in ("max_transient_retries", "max_run_seconds")
    }
    assert all(type(v) is int and v >= 1 for v in limits.values())
    p = RetryPolicy.from_limits(limits)
    assert p.max_retries == limits["max_transient_retries"]
    assert p.max_elapsed == float(limits["max_run_seconds"])


# ----------------------------------------------------------------- usage


def test_usage_sums_known_values():
    total = aggregate([Usage(10, Decimal("0.5")), Usage(5, Decimal("0.25"))])
    assert total == Usage(15, Decimal("0.75"))


def test_an_unknown_part_makes_the_total_unknown_per_field():
    assert aggregate([Usage(10, Decimal("1")), Usage(None, Decimal("2"))]) == Usage(None, Decimal("3"))
    assert aggregate([Usage(10, Decimal("1")), Usage(4, None)]) == Usage(14, None)
    assert aggregate([Usage(None, None), Usage(1, Decimal("1"))]) == Usage(None, None)


def test_an_empty_aggregate_is_a_known_zero():
    assert aggregate([]) == Usage(0, Decimal(0))
    assert aggregate(iter([])) == Usage(0, Decimal(0))


@pytest.mark.parametrize(
    "tokens,cost",
    [
        (True, None), (-1, None), (1.5, None), ("1", None),
        (None, 0.5), (None, 1), (None, Decimal("-1")), (None, Decimal("NaN")), (None, Decimal("Infinity")),
    ],
)
def test_usage_validation(tokens, cost):
    with pytest.raises(ValueError):
        Usage(tokens, cost)


# ------------------------------------------------------ module discipline


def test_module_imports_and_calls_are_minimal():
    tree = ast.parse(SRC.read_text(encoding="utf-8"), feature_version=(3, 10))
    allowed = {"__future__", "dataclasses", "datetime", "decimal", "email.utils", "random", "re", "time", "typing"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert {a.name for a in node.names} <= allowed
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module in allowed, node.module
        elif isinstance(node, ast.Name):
            assert node.id not in ("open", "print", "input"), node.id
        elif isinstance(node, ast.Attribute):
            assert node.attr not in ("sleep", "environ", "getenv"), node.attr


def test_no_public_callable_takes_a_profile():
    checked = 0
    for name, member in vars(retry).items():
        if name.startswith("_") or getattr(member, "__module__", None) != retry.__name__:
            continue
        callables = [member]
        if inspect.isclass(member):
            callables = [
                fn for key, fn in vars(member).items()
                if callable(fn) or isinstance(fn, (classmethod, staticmethod))
            ]
            callables.append(member)
        for fn in callables:
            target = fn.__func__ if isinstance(fn, (classmethod, staticmethod)) else fn
            try:
                params = inspect.signature(target).parameters
            except (TypeError, ValueError):
                continue
            checked += 1
            assert not [p for p in params if "profile" in p.lower()], (name, list(params))
    assert checked > 8
