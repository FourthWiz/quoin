"""Retry policy and usage accounting for a runtime launcher.

Pure decisions only: nothing here waits, reads the environment, opens a
file or talks to a network. The caller reports what failed and how many
attempts it has made; the policy answers with a delay to wait or a reason to
stop. Time comes from injected clocks, in seconds throughout.

A retry never moves work to another profile: no function in this module
takes or returns a profile, so that fallback cannot be expressed.
"""
from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Union

FAILURE_KINDS = ("connect", "timeout", "http", "auth", "policy", "config")
GIVE_UP_REASONS = (
    "mutating-tool-in-flight",
    "not-transient",
    "attempts-exhausted",
    "time-exhausted",
)

_DELTA_SECONDS = re.compile(r"[0-9]+")
_MAX_DIGITS = 12
_MAX_EXPONENT = 62


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class Failure:
    """What went wrong with one attempt.

    `status` is the HTTP status for an `http` failure; `retry_after` is the
    raw `Retry-After` header text; `mutating_tool_in_flight` says a tool that
    changes state was running, so repeating the request could repeat its
    effect.
    """

    kind: str
    status: Optional[int] = None
    retry_after: Optional[str] = None
    mutating_tool_in_flight: bool = False

    def __post_init__(self) -> None:
        if self.kind not in FAILURE_KINDS:
            raise ValueError("unknown failure kind")
        if self.status is not None and not _is_int(self.status):
            raise ValueError("status must be an integer")
        if self.kind == "http" and self.status is None:
            raise ValueError("an http failure needs a status")
        if self.retry_after is not None and not isinstance(self.retry_after, str):
            raise ValueError("retry_after must be text")
        if not isinstance(self.mutating_tool_in_flight, bool):
            raise ValueError("mutating_tool_in_flight must be a boolean")


@dataclass(frozen=True)
class Retry:
    """Wait `delay` seconds, then try again."""

    delay: float


@dataclass(frozen=True)
class GiveUp:
    """Stop, for one of the closed `GIVE_UP_REASONS`."""

    reason: str

    def __post_init__(self) -> None:
        if self.reason not in GIVE_UP_REASONS:
            raise ValueError("unknown give-up reason")


def _limit(limits: Mapping[str, int], name: str) -> Optional[int]:
    if name not in limits:
        return None
    value = limits[name]
    if not _is_int(value) or value < 1:
        raise ValueError("%s must be an integer of at least 1" % name)
    return value


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded retries with full jitter.

    `max_retries` is the number of retries allowed after the first attempt
    (0 means none); `max_elapsed` is the run's time budget in seconds, or
    `None` for no time cap. `clock` is monotonic seconds; `wall_clock` is an
    aware UTC time used only to read an HTTP-date `Retry-After`.
    """

    max_retries: int
    max_elapsed: Optional[float] = None
    base: float = 1.0
    cap: float = 30.0
    rng: Callable[[], float] = field(default_factory=lambda: random.Random().random)
    clock: Callable[[], float] = time.monotonic
    wall_clock: Callable[[], datetime] = _utc_now

    def __post_init__(self) -> None:
        if not _is_int(self.max_retries) or self.max_retries < 0:
            raise ValueError("max_retries must be a non-negative integer")
        if self.max_elapsed is not None and (
            isinstance(self.max_elapsed, bool) or self.max_elapsed <= 0
        ):
            raise ValueError("max_elapsed must be positive")
        if self.base <= 0 or self.cap <= 0:
            raise ValueError("base and cap must be positive")

    @classmethod
    def from_limits(
        cls,
        limits: Mapping[str, int],
        *,
        base: float = 1.0,
        cap: float = 30.0,
        rng: Optional[Callable[[], float]] = None,
        clock: Optional[Callable[[], float]] = None,
        wall_clock: Optional[Callable[[], datetime]] = None,
    ) -> "RetryPolicy":
        """Build a policy from the effective `max_transient_retries` and
        `max_run_seconds` limits. A missing retry limit means no retries; a
        missing time limit means only the attempt cap applies."""
        retries = _limit(limits, "max_transient_retries")
        seconds = _limit(limits, "max_run_seconds")
        injected: Dict[str, Any] = {}
        if rng is not None:
            injected["rng"] = rng
        if clock is not None:
            injected["clock"] = clock
        if wall_clock is not None:
            injected["wall_clock"] = wall_clock
        return cls(
            max_retries=retries or 0,
            max_elapsed=float(seconds) if seconds is not None else None,
            base=base, cap=cap, **injected,
        )

    def start(self) -> float:
        """The monotonic reading to pass to `decide` as `started_at`."""
        return self.clock()

    def decide(
        self, attempt: int, started_at: float, failure: Failure
    ) -> Union[Retry, GiveUp]:
        """Decide what to do after `attempt` attempts (at least 1) have failed."""
        if not _is_int(attempt) or attempt < 1:
            raise ValueError("attempt counts attempts already made and starts at 1")
        if failure.mutating_tool_in_flight:
            return GiveUp("mutating-tool-in-flight")
        if not _transient(failure):
            return GiveUp("not-transient")
        if attempt > self.max_retries:
            return GiveUp("attempts-exhausted")
        delay = self._retry_after(failure)
        if delay is None:
            ceiling = min(self.cap, self.base * 2.0 ** min(attempt - 1, _MAX_EXPONENT))
            delay = self.rng() * ceiling
        if self.max_elapsed is not None:
            elapsed = self.clock() - started_at
            if elapsed >= self.max_elapsed or elapsed + delay >= self.max_elapsed:
                return GiveUp("time-exhausted")
        return Retry(delay)

    def _retry_after(self, failure: Failure) -> Optional[float]:
        text = failure.retry_after
        if text is None:
            return None
        text = text.strip()
        if _DELTA_SECONDS.fullmatch(text):
            digits = text.lstrip("0") or "0"
            value = float(int(digits)) if len(digits) <= _MAX_DIGITS else float("inf")
            return min(value, self.cap)
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
        if when is None:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = (when - self.wall_clock()).total_seconds()
        return min(max(seconds, 0.0), self.cap)


def _transient(failure: Failure) -> bool:
    if failure.kind in ("connect", "timeout"):
        return True
    status = failure.status
    if failure.kind == "http" and status is not None:
        return status == 429 or 500 <= status <= 599
    return False


@dataclass(frozen=True)
class Usage:
    """Tokens and cost for some work; `None` means unknown, never zero."""

    tokens: Optional[int]
    cost: Optional[Decimal]

    def __post_init__(self) -> None:
        if self.tokens is not None and (not _is_int(self.tokens) or self.tokens < 0):
            raise ValueError("tokens must be a non-negative integer or None")
        if self.cost is not None:
            if not isinstance(self.cost, Decimal):
                raise ValueError("cost must be a Decimal or None")
            if not self.cost.is_finite() or self.cost < 0:
                raise ValueError("cost must be finite and non-negative")


def aggregate(usages: Iterable[Usage]) -> Usage:
    """Sum usage. A field is unknown when any part of it is unknown; an empty
    input is a known zero."""
    tokens: Optional[int] = 0
    cost: Optional[Decimal] = Decimal(0)
    for usage in usages:
        tokens = None if tokens is None or usage.tokens is None else tokens + usage.tokens
        cost = None if cost is None or usage.cost is None else cost + usage.cost
    return Usage(tokens, cost)
