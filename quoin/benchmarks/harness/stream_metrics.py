"""
stream_metrics.py - Incremental counters over a Claude stream-json session.

The cells see every event as it arrives, while the in-memory event list is a
ring buffer that drops the oldest events on long sessions, so counting has to
happen incrementally. The counters are pure functions of the event stream and
never raise: a counting failure must not change the verdict of a paid session.
It is recorded as `tool_error_count_available: False` plus the error text.
"""

from __future__ import annotations

TOKEN_FIELDS = (
    ("input", "inputTokens"),
    ("output", "outputTokens"),
    ("cache_read", "cacheReadInputTokens"),
    ("cache_write", "cacheCreationInputTokens"),
)

EXTRA_KEYS = (
    "tool_error_count",
    "tool_error_count_available",
    "subagent_tool_error_count",
    "tool_result_count",
    "permission_denial_count",
    "model_usage",
    "model_usage_totals",
    "stream_metrics_error",
)

_ERROR_TEXT_LIMIT = 200


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def sum_model_usage(model_usage):
    """Sum per-model usage buckets into one totals dict, or None."""
    if not model_usage or not isinstance(model_usage, dict):
        return None
    totals = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    cost_total = None
    for bucket in model_usage.values():
        if not isinstance(bucket, dict):
            continue
        for name, key in TOKEN_FIELDS:
            value = bucket.get(key)
            if _is_number(value):
                totals[name] += value
        cost = bucket.get("costUSD")
        if _is_number(cost):
            cost_total = cost if cost_total is None else cost_total + cost
    totals["cost_usd"] = cost_total
    return totals


class StreamMetrics:
    """Counts tool errors, tool results and permission denials in a stream."""

    def __init__(self):
        self.tool_error_count = 0
        self.subagent_tool_error_count = 0
        self.tool_result_count = 0
        self.permission_denial_count = None
        self.model_usage = None
        self.result_seen = False
        self._failed = False
        self._error = None

    def observe(self, event):
        """Fold one stream event into the counters. Never raises."""
        if self._failed:
            return
        try:
            self._observe(event)
        except Exception as exc:
            self._failed = True
            self._error = ("%s: %s" % (type(exc).__name__, exc))[:_ERROR_TEXT_LIMIT]

    def _observe(self, event):
        if not isinstance(event, dict):
            return
        event_type = event.get("type")
        if event_type == "user":
            self._observe_user(event)
        elif event_type == "result":
            self._observe_result(event)

    def _observe_user(self, event):
        message = event.get("message")
        if not isinstance(message, dict):
            return
        content = message.get("content")
        if not isinstance(content, list):
            return
        is_subagent = event.get("parent_tool_use_id") is not None
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "tool_result":
                continue
            self.tool_result_count += 1
            if item.get("is_error") is True:
                self.tool_error_count += 1
                if is_subagent:
                    self.subagent_tool_error_count += 1

    def _observe_result(self, event):
        self.result_seen = True
        denials = event.get("permission_denials")
        self.permission_denial_count = len(denials) if isinstance(denials, list) else 0
        usage = event.get("modelUsage")
        if not isinstance(usage, dict):
            self.model_usage = None
            return
        kept = {}
        for model, bucket in usage.items():
            if not isinstance(bucket, dict):
                continue
            entry = {}
            for _, key in TOKEN_FIELDS:
                if _is_number(bucket.get(key)):
                    entry[key] = bucket[key]
            if _is_number(bucket.get("costUSD")):
                entry["costUSD"] = bucket["costUSD"]
            kept[model] = entry
        self.model_usage = kept

    def as_extra(self):
        """The metrics.json keys for this session. Never raises."""
        try:
            return {
                "tool_error_count": self.tool_error_count,
                "tool_error_count_available": not self._failed,
                "subagent_tool_error_count": self.subagent_tool_error_count,
                "tool_result_count": self.tool_result_count,
                "permission_denial_count": self.permission_denial_count,
                "model_usage": self.model_usage,
                "model_usage_totals": sum_model_usage(self.model_usage),
                "stream_metrics_error": self._error if self._failed else None,
            }
        except Exception as exc:
            extra = {key: None for key in EXTRA_KEYS}
            extra["tool_error_count_available"] = False
            extra["stream_metrics_error"] = ("%s: %s" % (type(exc).__name__, exc))[:_ERROR_TEXT_LIMIT]
            return extra
