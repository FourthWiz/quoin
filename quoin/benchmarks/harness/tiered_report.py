"""
tiered_report.py - Tiered plain-vs-quoin comparison built from a run directory.

Denominators come from the suite, not from the task directories that happen to
exist: a task-arm with no directory is a `not_run` row and every verdict other
than `pass` counts as a failure. Token sums use the per-model usage the stream
reports, which includes subagent work that the top-level usage totals omit.
Everything here is read-only over the run directory and uses only the standard
library plus the cost card and the stream counters.
"""

from __future__ import annotations

import json
import math
import re
from decimal import Decimal
from pathlib import Path

from .cost import (
    UnpricedModelError, estimate_cost, estimate_model_usage, is_costable,
    load_pricing, resolve_model_pricing,
)
from .cost import _CODEX_MODEL_PREFIXES as CODEX_MODEL_PREFIXES
from . import patch_size as _patch_size
from .session_state import (
    INVALID_REASONS, is_judge_pending, pipeline_invalid_reason,
)
from .stream_metrics import TOKEN_FIELDS, bucket_has_usage, sum_model_usage

PLAIN_CELL = "simple-claude"
QUOIN_CELL = "quoin-claude"
ARMS = (PLAIN_CELL, QUOIN_CELL)
DEFAULT_TIER_ORDER = ("easy", "medium", "large")
OUTCOMES = ("pass", "fail", "error", "timeout", "budget_stopped", "not_run", "invalid")
IDENTITY_TRIP_REASONS = frozenset({
    "plain-arm-contaminated", "quoin-arm-missing-quoin", "quoin-arm-contaminated",
})
SYSTEMIC_REASONS = frozenset({"auth-failure", "no-assistant-turn"})
# Refusals a quoin cell makes before it spawns anything: no session ran, so the
# row is spawned again on resume, and the run stops because the next row would
# be refused the same way.
QUOIN_PRE_SPAWN_REASONS = frozenset({
    "install-failed", "commit-unresolvable", "commit-mismatch", "expected-commit-unarmed",
})
# Reasons that end a paired run, and reasons whose recorded row is re-spawned
# on resume; the paired orchestrator and this report must agree on both.
ABORT_REASONS = IDENTITY_TRIP_REASONS | SYSTEMIC_REASONS | QUOIN_PRE_SPAWN_REASONS | {"budget-cap-unarmed"}
RESPAWN_REASONS = IDENTITY_TRIP_REASONS | QUOIN_PRE_SPAWN_REASONS | {"budget-cap-unarmed"}
_PLAIN_COMPONENT = re.compile(r"^[A-Za-z0-9_.-]+$")
_ERROR_TEXT_LIMIT = 200
RETRIES_STATEMENT = "Final pass rate equals first-attempt pass rate (retries=0)."
UNKNOWN_SHA = "unknown"

TOKEN_TYPES = tuple(name for name, _ in TOKEN_FIELDS)
_USAGE_KEYS = (
    ("input", "tokens_in"),
    ("output", "tokens_out"),
    ("cache_read", "tokens_cache_read"),
    ("cache_write", "tokens_cache_write"),
)
_SOURCES = ("model_usage", "usage", "unavailable")
_USD_SOURCES = ("runtime", "estimated", "unavailable")


def wilson_interval(k, n, z=1.959963984540054):
    """Closed-form Wilson score interval for k successes in n trials."""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2.0 * n)) / denom
    half = z * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def _unquote(text):
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1]
    return text


def read_manifest(run_path):
    """Parse run-manifest.yaml as `_write_manifest` writes it; {} when unreadable."""
    try:
        text = (Path(run_path) / "run-manifest.yaml").read_text(encoding="utf-8")
    except (OSError, ValueError):
        return {}
    manifest = {}
    current_list = None
    for raw in text.splitlines():
        if not raw.strip():
            continue
        stripped = raw.strip()
        if raw.startswith("  - ") or stripped.startswith("- "):
            if current_list is not None:
                current_list.append(_unquote(stripped[2:]))
            continue
        key, sep, value = raw.partition(":")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if value == "":
            current_list = []
            manifest[key] = current_list
            continue
        current_list = None
        manifest[key] = None if value == "null" else _unquote(value)
    return manifest


def _read_json(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return value


def _finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _zero_tokens():
    return {name: 0 for name in TOKEN_TYPES}


def _not_run_row():
    return {
        "verdict": "not_run", "ran": False, "judge_pending": False, "row_issue": None,
        "tokens": _zero_tokens(), "tokens_source": "unavailable",
        "usd": 0.0, "usd_source": "unavailable",
        "wall_clock_s": 0.0, "tool_errors": 0, "tool_errors_available": False,
        "stream_metrics_error": None, "tree_sha": None, "failure_reason": None,
        "session_errored": False, "pre_spawn_error": False, "models": [],
        "unpriced_models": [], "invalid_reason": None, "post_plan_stop": False,
        "workflow_route": None,
        "attempts": 1, "first_attempt_pass": False, "final_pass": False,
        "pending_counted_as_error": False, "cap_usd": None, "overshoot_usd": None,
        "permission_denials": None,
        "patch_size": _patch_size.measure_patch(None),
    }


def _estimate_usd(model_usage, usage_tokens, manifest_model, pricing):
    """Pricing-card estimate as `(usd_or_None, unpriced_models)`.

    The estimate is None when any part is not priceable; `unpriced_models`
    names the Claude models that have no pricing entry.
    """
    if model_usage:
        used = {
            m: b for m, b in model_usage.items()
            if isinstance(b, dict) and bucket_has_usage(b)
        }
        if any(not m for m in used):
            return None, []
        total, unpriced = estimate_model_usage(used, pricing)
        if unpriced:
            return None, unpriced
        has_costable = any(is_costable(m) for m in used)
        return (float(total) if has_costable and total is not None else None), []
    if not manifest_model:
        return None, []
    try:
        amount = estimate_cost(
            manifest_model, usage_tokens["input"], usage_tokens["output"],
            usage_tokens["cache_write"], usage_tokens["cache_read"], pricing,
            strict=False,
        )
    except UnpricedModelError as exc:  # pragma: no cover - strict=False never raises
        return None, [exc.model]
    return (None if amount is None else float(amount)), []


def _unpriced_for_row(row, recorded, pricing):
    """Claude models this row used that the pricing card cannot price: the
    estimate's own findings, the cell's record, and every costable model the
    stream reported (so a row with runtime cost still names them)."""
    found = set(row.get("unpriced_models") or [])
    if isinstance(recorded, list):
        found.update(m for m in recorded if isinstance(m, str) and is_costable(m))
    for model in row.get("models") or []:
        if (
            is_costable(model)
            and not model.lower().startswith(CODEX_MODEL_PREFIXES)
            and resolve_model_pricing(model, pricing) is None
        ):
            found.add(model)
    return sorted(found)


def _patch_size_of(task_dir):
    try:
        data = (Path(task_dir) / "diff.patch").read_bytes()
    except FileNotFoundError:
        data = None
    except OSError:
        return _patch_size._empty("unreadable")
    return _patch_size.measure_patch(data)


def load_row(task_dir, manifest_model, pricing, cell=None):
    """One task-arm row; a missing directory is a `not_run` row.

    With `cell`, a quoin row from the Verified runner whose session did not run
    the pipeline becomes an `invalid` row whatever its judge file says.
    """
    if task_dir is None or not Path(task_dir).is_dir():
        return _not_run_row()
    task_dir = Path(task_dir)
    row = _not_run_row()
    row["ran"] = True
    row["judge_pending"] = bool(is_judge_pending(task_dir))
    row["pending_counted_as_error"] = False
    row["patch_size"] = _patch_size_of(task_dir)

    judge = _read_json(task_dir / "judge.json")
    verdict = judge.get("verdict") if judge else None
    if verdict in OUTCOMES:
        row["verdict"] = verdict
    elif judge is not None:
        row["verdict"] = "error"
        row["row_issue"] = "unknown_verdict"
    else:
        row["verdict"] = "error"
        if not row["judge_pending"]:
            row["row_issue"] = "unreadable"
    if row["judge_pending"]:
        row["verdict"] = "error"
        row["pending_counted_as_error"] = True
    row["first_attempt_pass"] = row["final_pass"] = row["verdict"] == "pass"

    metrics = _read_json(task_dir / "metrics.json") or {}
    cost = _read_json(task_dir / "cost.json") or {}

    model_usage = metrics.get("model_usage")
    if not isinstance(model_usage, dict):
        model_usage = None
    totals = sum_model_usage(model_usage)
    if totals is not None:
        row["tokens"] = {name: totals[name] for name in TOKEN_TYPES}
        row["tokens_source"] = "model_usage"
        row["models"] = sorted(model_usage)
    elif any(metrics.get(key) is not None for _, key in _USAGE_KEYS):
        row["tokens"] = {name: _num(metrics.get(key)) for name, key in _USAGE_KEYS}
        row["tokens_source"] = "usage"

    runtime_usd = cost.get("cost_runtime_usd")
    if (
        cost.get("cost_available")
        and isinstance(runtime_usd, (int, float)) and not isinstance(runtime_usd, bool)
        and math.isfinite(runtime_usd)
    ):
        row["usd"] = float(runtime_usd)
        row["usd_source"] = "runtime"
    elif row["tokens_source"] != "unavailable":
        estimate, unpriced = _estimate_usd(
            model_usage if totals is not None else None, row["tokens"], manifest_model, pricing,
        )
        row["unpriced_models"] = unpriced
        if estimate is not None:
            row["usd"] = estimate
            row["usd_source"] = "estimated"

    row["unpriced_models"] = _unpriced_for_row(
        row, metrics.get("unpriced_models"), pricing
    )

    row["wall_clock_s"] = float(_num(metrics.get("wall_clock_seconds")))
    row["tool_errors_available"] = metrics.get("tool_error_count_available") is True
    if row["tool_errors_available"]:
        row["tool_errors"] = int(_num(metrics.get("tool_error_count")))
    row["stream_metrics_error"] = metrics.get("stream_metrics_error")
    row["tree_sha"] = metrics.get("tree_sha")
    row["failure_reason"] = metrics.get("failure_reason")
    row["session_errored"] = metrics.get("session_errored") is True
    row["pre_spawn_error"] = metrics.get("pre_spawn_error") is True
    row["cap_usd"] = _finite(metrics.get("cap_usd"))
    row["overshoot_usd"] = _finite(metrics.get("overshoot_usd"))
    denials = metrics.get("permission_denial_count")
    row["permission_denials"] = (
        denials if isinstance(denials, int) and not isinstance(denials, bool) and denials >= 0 else None
    )
    route = metrics.get("workflow_route")
    row["workflow_route"] = route if isinstance(route, dict) else None
    session = _read_json(task_dir / "session.json") or {}
    terminal = session.get("terminal_state") or metrics.get("terminal_state")
    row["post_plan_stop"] = (
        terminal in ("timeout", "budget_stopped")
        and metrics.get("workflow_artifacts_has_plan") is True
    )
    invalid_reason = pipeline_invalid_reason(cell, task_dir, metrics) if cell else None
    if invalid_reason is not None:
        row["verdict"] = "invalid"
        row["invalid_reason"] = invalid_reason
        row["row_issue"] = None
        row["judge_pending"] = False
        row["pending_counted_as_error"] = False
        row["first_attempt_pass"] = row["final_pass"] = False
    return row


def _sum_mean(total, n):
    return {"sum": total, "mean": (total / n) if n else 0.0}


def _patch_size_stats(rows):
    """Patch-size sums and means over the given (non-invalid) rows."""
    sizes = [r["patch_size"] for r in rows]
    measured = [p for p in sizes if p["status"] in ("ok", "empty")]
    n = len(measured)

    def total(group, key):
        return sum(p[group][key] for p in measured)

    return {
        "n_measured": n,
        "non_test": {k: total("non_test", k) for k in ("added", "removed", "files")},
        "test": {k: total("test", k) for k in ("added", "removed", "files")},
        "binary_files": sum(p["binary_files"] for p in measured),
        "mean_lines_changed": (sum(p["lines_changed"] for p in measured) / n) if n else None,
        "mean_non_test_added": (total("non_test", "added") / n) if n else None,
        "mean_test_added": (total("test", "added") / n) if n else None,
        "n_empty": sum(1 for p in sizes if p["status"] == "empty"),
        "n_missing": sum(1 for p in sizes if p["status"] == "missing"),
        "n_unreadable": sum(1 for p in sizes if p["status"] == "unreadable"),
    }


def _patch_size_delta(plain, quoin):
    def sub(a, b):
        return None if a is None or b is None else a - b

    return {
        "non_test": {k: quoin["non_test"][k] - plain["non_test"][k] for k in ("added", "removed", "files")},
        "test": {k: quoin["test"][k] - plain["test"][k] for k in ("added", "removed", "files")},
        "mean_lines_changed": sub(quoin["mean_lines_changed"], plain["mean_lines_changed"]),
        "mean_non_test_added": sub(quoin["mean_non_test_added"], plain["mean_non_test_added"]),
        "mean_test_added": sub(quoin["mean_test_added"], plain["mean_test_added"]),
    }


def arm_stats(all_rows):
    """Statistics over the rows that make up the suite denominator for one arm.

    Rows where the quoin arm did not run the pipeline are not evidence, so they
    are left out of every figure but the outcome counts, which are taken from
    all of them (`n_suite` is the full count, `n_invalid` the excluded part).
    """
    rows = [r for r in all_rows if r["verdict"] != "invalid"]
    n = len(rows)
    passes = sum(1 for r in rows if r["verdict"] == "pass")
    rate = passes / n if n else 0.0
    lo, hi = wilson_interval(passes, n)
    outcomes = {key: 0 for key in OUTCOMES}
    for r in all_rows:
        outcomes[r["verdict"]] = outcomes.get(r["verdict"], 0) + 1
    caps = [r["cap_usd"] for r in rows if r["cap_usd"] is not None]
    overshoots = [r["overshoot_usd"] for r in rows if r["overshoot_usd"] is not None]
    denials = [r["permission_denials"] for r in rows if r["permission_denials"] is not None]
    return {
        "n": n,
        "n_suite": len(all_rows),
        "n_invalid": len(all_rows) - n,
        "passes": passes,
        "cap_usd": max(caps) if caps else None,
        "overshoot_usd_sum": sum(o for o in overshoots if o > 0),
        "rows_over_cap": sum(1 for o in overshoots if o > 0),
        "permission_denials_sum": sum(denials),
        "rows_with_denials": sum(1 for d in denials if d > 0),
        "first_attempt_pass_rate": rate,
        "final_pass_rate": rate,
        "wilson_ci_95": [lo, hi],
        "outcomes": outcomes,
        "tokens": {t: _sum_mean(sum(r["tokens"][t] for r in rows), n) for t in TOKEN_TYPES},
        "tokens_source_counts": {s: sum(1 for r in rows if r["tokens_source"] == s) for s in _SOURCES},
        "usd": _sum_mean(sum(r["usd"] for r in rows), n),
        "usd_source_counts": {s: sum(1 for r in rows if r["usd_source"] == s) for s in _USD_SOURCES},
        "wall_clock_s": _sum_mean(sum(r["wall_clock_s"] for r in rows), n),
        "tool_errors": {
            "sum": sum(r["tool_errors"] for r in rows),
            "available_n": sum(1 for r in rows if r["tool_errors_available"]),
        },
        "patch_size": _patch_size_stats(rows),
    }


def delta(plain, quoin):
    """Quoin minus plain for the headline figures."""
    return {
        "first_attempt_pass_rate_pp": 100 * (quoin["first_attempt_pass_rate"] - plain["first_attempt_pass_rate"]),
        "final_pass_rate_pp": 100 * (quoin["final_pass_rate"] - plain["final_pass_rate"]),
        "tokens": {
            t: {
                "sum": quoin["tokens"][t]["sum"] - plain["tokens"][t]["sum"],
                "mean": quoin["tokens"][t]["mean"] - plain["tokens"][t]["mean"],
            }
            for t in TOKEN_TYPES
        },
        "usd": {
            "sum": quoin["usd"]["sum"] - plain["usd"]["sum"],
            "mean": quoin["usd"]["mean"] - plain["usd"]["mean"],
        },
        "wall_clock_s": {
            "sum": quoin["wall_clock_s"]["sum"] - plain["wall_clock_s"]["sum"],
            "mean": quoin["wall_clock_s"]["mean"] - plain["wall_clock_s"]["mean"],
        },
        "tool_errors_sum": quoin["tool_errors"]["sum"] - plain["tool_errors"]["sum"],
        "patch_size": _patch_size_delta(plain["patch_size"], quoin["patch_size"]),
    }


def _group(rows_by_arm):
    plain = arm_stats(rows_by_arm[PLAIN_CELL])
    quoin = arm_stats(rows_by_arm[QUOIN_CELL])
    return {"n": plain["n"], PLAIN_CELL: plain, QUOIN_CELL: quoin, "delta": delta(plain, quoin)}


def _tier_order(suite, tasks):
    tiers = ((suite.get("tiered") or {}).get("tiers")) if isinstance(suite.get("tiered"), dict) else None
    order = list(tiers) if isinstance(tiers, dict) and tiers else list(DEFAULT_TIER_ORDER)
    for task in tasks:
        tier = task.get("tier") or "unknown"
        if tier not in order:
            order.append(tier)
    return order


def _pair_entry(task, rows):
    entry = {"task_id": task["id"], "tier": task.get("tier") or "unknown", "source_id": task.get("source_id")}
    for arm in ARMS:
        r = rows[arm]
        entry[arm] = {
            "verdict": r["verdict"], "first_attempt_pass": r["first_attempt_pass"],
            "final_pass": r["final_pass"], "attempts": r["attempts"],
            "usd": r["usd"], "usd_source": r["usd_source"],
            "tokens": dict(r["tokens"]), "tokens_source": r["tokens_source"],
            "wall_clock_s": r["wall_clock_s"], "tool_errors": r["tool_errors"],
            "tool_errors_available": r["tool_errors_available"], "tree_sha": r["tree_sha"],
            "invalid_reason": r["invalid_reason"], "post_plan_stop": r["post_plan_stop"],
            "workflow_route": r["workflow_route"],
            "patch_size": r["patch_size"],
        }
    a, b = rows[PLAIN_CELL]["tree_sha"], rows[QUOIN_CELL]["tree_sha"]
    entry["tree_sha_match"] = (a == b) if (a and b) else None
    return entry


def _complete_pairs(entries):
    invalid = [e for e in entries if "invalid" in (e[PLAIN_CELL]["verdict"], e[QUOIN_CELL]["verdict"])]
    done = [
        e for e in entries
        if e not in invalid
        and e[PLAIN_CELL]["verdict"] != "not_run" and e[QUOIN_CELL]["verdict"] != "not_run"
    ]
    n = len(done)
    out = {"n_pairs": n, "pairs_not_run": len(entries) - n - len(invalid), "pairs_invalid": len(invalid)}
    for arm in ARMS:
        passes = sum(1 for e in done if e[arm]["verdict"] == "pass")
        out[arm] = {
            "passes": passes,
            "pass_rate": passes / n if n else 0.0,
            "usd_sum": sum(e[arm]["usd"] for e in done),
            "tokens_sum": {t: sum(e[arm]["tokens"][t] for e in done) for t in TOKEN_TYPES},
            "wall_clock_s_sum": sum(e[arm]["wall_clock_s"] for e in done),
        }
    p, q = out[PLAIN_CELL], out[QUOIN_CELL]
    out["delta"] = {
        "pass_rate_pp": 100 * (q["pass_rate"] - p["pass_rate"]),
        "usd_sum": q["usd_sum"] - p["usd_sum"],
        "tokens_sum": {t: q["tokens_sum"][t] - p["tokens_sum"][t] for t in TOKEN_TYPES},
        "wall_clock_s_sum": q["wall_clock_s_sum"] - p["wall_clock_s_sum"],
    }
    return out


def _label(arm, task_id):
    return "%s/%s" % (arm, task_id)


HAIKU_LONG_PROMPT_NOTE = (
    "Haiku 5.5 long-prompt tier not applied to table estimates; runtime cost is authoritative"
)


def _manifest_dict(manifest, key):
    """A dict the manifest writer stored as a JSON string, or None."""
    value = manifest.get(key)
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _model_mismatch_rows(manifest, scoped, rows):
    """Rows whose models are not the pinned top-level model plus the bench
    sonnet and haiku models. Returns None when the manifest has no pin."""
    bench = _manifest_dict(manifest, "bench_models")
    if not bench:
        return None
    override = manifest.get("model_override")
    top = override if isinstance(override, str) and override else bench.get("opus")
    allowed = {top, bench.get("sonnet"), bench.get("haiku")}
    out = []
    for task in scoped:
        for arm in ARMS:
            r = rows[arm][task["id"]]
            if not r["ran"]:
                continue
            costable = [m for m in r["models"] if is_costable(m)]
            if not costable:
                continue
            if top not in costable or any(m not in allowed for m in costable):
                out.append(_label(arm, task["id"]))
    return out


def _validity(run_path, manifest, suite_sha, suite_ids, scoped, rows, entries, invalid_task_ids=()):
    lists = {k: [] for k in (
        "identity_trip_rows", "systemic_error_rows", "session_errored_rows", "pre_spawn_error_rows",
        "judge_pending_rows", "unreadable_rows", "unknown_verdict_rows", "stream_metrics_error_rows",
    )}
    for task in scoped:
        for arm in ARMS:
            r = rows[arm][task["id"]]
            if not r["ran"]:
                continue
            label = _label(arm, task["id"])
            reason = r["failure_reason"]
            if reason in IDENTITY_TRIP_REASONS:
                lists["identity_trip_rows"].append(label)
            if reason in SYSTEMIC_REASONS:
                lists["systemic_error_rows"].append(label)
            if r["session_errored"]:
                lists["session_errored_rows"].append(label)
            if r["pre_spawn_error"]:
                lists["pre_spawn_error_rows"].append(label)
            if r["judge_pending"]:
                lists["judge_pending_rows"].append(label)
            if r["row_issue"] == "unreadable":
                lists["unreadable_rows"].append(label)
            if r["row_issue"] == "unknown_verdict":
                lists["unknown_verdict_rows"].append(label)
            if r["stream_metrics_error"]:
                lists["stream_metrics_error_rows"].append(label)

    unpriced = sorted({
        m for task in scoped for arm in ARMS for m in rows[arm][task["id"]]["unpriced_models"]
    })
    lower_bound_rows = [
        _label(arm, task["id"]) for task in scoped for arm in ARMS
        if rows[arm][task["id"]]["usd_source"] == "estimated"
        and "claude-haiku-5-5" in rows[arm][task["id"]]["models"]
    ]
    mismatch_rows = _model_mismatch_rows(manifest, scoped, rows)

    quoin_rows = [(task, rows[QUOIN_CELL][task["id"]]) for task in scoped]
    split = {reason: [] for reason in INVALID_REASONS}
    for task, r in quoin_rows:
        if r["verdict"] == "invalid" and r["invalid_reason"] in split:
            split[r["invalid_reason"]].append(task["id"])
    invalid_quoin = sorted(i for ids in split.values() for i in ids)
    ran_quoin = [task["id"] for task, r in quoin_rows if r["ran"]]
    post_plan_timeout = [
        t["id"] for t, r in quoin_rows
        if r["post_plan_stop"] and r["failure_reason"] != "budget-halt-detected"
        and r["verdict"] == "timeout"
    ]
    post_plan_budget = [
        t["id"] for t, r in quoin_rows if r["post_plan_stop"] and r["verdict"] == "budget_stopped"
    ]
    route_counts = {"fast": 0, "full": 0, "unknown": 0}
    for _task, r in quoin_rows:
        if not r["ran"]:
            continue
        route = (r["workflow_route"] or {}).get("route")
        route_counts[route if route in ("fast", "full") else "unknown"] += 1

    unexpected = []
    for arm in ARMS:
        arm_dir = Path(run_path) / arm
        if arm_dir.is_dir():
            for child in sorted(arm_dir.iterdir()):
                if child.is_dir() and child.name not in suite_ids:
                    unexpected.append(_label(arm, child.name))

    manifest_sha = manifest.get("suite_sha")
    if not manifest_sha or not suite_sha or UNKNOWN_SHA in (manifest_sha, suite_sha):
        sha_match = None
    else:
        sha_match = manifest_sha == suite_sha

    invalid = []
    if invalid_quoin:
        invalid.append(
            "quoin arm did not run the pipeline in %d/%d sessions"
            % (len(invalid_quoin), len(ran_quoin))
        )
    if lists["identity_trip_rows"]:
        invalid.append("arm identity check failed for %s" % ", ".join(lists["identity_trip_rows"]))
    for reason in manifest.get("run_invalid_reasons") or []:
        invalid.append(reason)
    if sha_match is False:
        invalid.append("suite SHA differs from the one recorded at run start")
    if unpriced:
        invalid.append("unpriced model: %s" % ", ".join(unpriced))

    provisional = []
    if lists["judge_pending_rows"]:
        provisional.append(
            "%d rows await judging (run --judge-only) and count as errors until then"
            % len(lists["judge_pending_rows"])
        )
    if lists["systemic_error_rows"]:
        provisional.append("%d rows hit a systemic error (auth or no assistant turn)" % len(lists["systemic_error_rows"]))
    if lists["unreadable_rows"]:
        provisional.append("%d rows have an unreadable judge file" % len(lists["unreadable_rows"]))
    if lists["unknown_verdict_rows"]:
        provisional.append("%d rows carry an unknown verdict" % len(lists["unknown_verdict_rows"]))

    mismatches = [e["task_id"] for e in entries if e["tree_sha_match"] is False]
    models = {}
    source_counts = {}
    for arm in ARMS:
        arm_rows = [rows[arm][t["id"]] for t in scoped]
        models[arm] = sorted({m for r in arm_rows for m in r["models"]})
        source_counts[arm] = {s: sum(1 for r in arm_rows if r["tokens_source"] == s) for s in _SOURCES}

    out = {
        "valid": not invalid,
        "invalid_reasons": invalid,
        "final": not provisional,
        "provisional_reasons": provisional,
        "suite_sha_manifest": manifest_sha,
        "suite_sha_computed": suite_sha,
        "suite_sha_match": sha_match,
        "arm_config_mode": manifest.get("arm_config_mode") or "not recorded",
        "contamination_checks": manifest.get("contamination_checks") or "not recorded",
        "models": models,
        "manifest_models": {
            PLAIN_CELL: manifest.get("simple_claude_model"),
            QUOIN_CELL: manifest.get("quoin_claude_model"),
        },
        "tokens_source_counts": source_counts,
        "effort": manifest.get("effort") or "not recorded",
        "bench_models": _manifest_dict(manifest, "bench_models"),
        "model_override": manifest.get("model_override"),
        "observed_models": _manifest_dict(manifest, "observed_models"),
        "model_mismatch_rows": mismatch_rows if mismatch_rows is not None else [],
        "model_mismatch_checked": mismatch_rows is not None,
        "invalid_quoin_rows": invalid_quoin,
        "no_pipeline_artifacts_rows": sorted(split["no-pipeline-artifacts"]),
        "stopped_before_plan_rows": sorted(split["stopped-before-plan"]),
        "no_pipeline_evidence_rows": sorted(split["no-pipeline-evidence"]),
        "post_plan_timeout_rows": sorted(post_plan_timeout),
        "post_plan_budget_stop_rows": sorted(post_plan_budget),
        "quoin_route_counts": route_counts,
        "unpriced_models": unpriced,
        "estimate_lower_bound_rows": lower_bound_rows,
        "estimate_lower_bound_note": HAIKU_LONG_PROMPT_NOTE,
    }
    out.update(lists)
    extra_skills = manifest.get("asymmetric_extra_skills")
    if isinstance(extra_skills, str):
        extra_skills = [part.strip() for part in extra_skills.split(",") if part.strip()]
    out["asymmetric_extra_skills"] = list(extra_skills) if isinstance(extra_skills, list) else []
    out["invalid_task_ids"] = list(invalid_task_ids)
    out["unexpected_task_dirs"] = unexpected
    out["tree_sha_mismatches"] = mismatches
    return out


def _plain_component(task_id):
    return (
        isinstance(task_id, str) and task_id not in (".", "..")
        and bool(_PLAIN_COMPONENT.match(task_id))
    )


def build_tiered_report(run_path, suite, manifest, suite_sha=None):
    """Build the tiered comparison dict stored under `summary["tiered"]`."""
    run_path = Path(run_path)
    pricing = load_pricing()
    candidates = [t for t in suite.get("tasks", []) if isinstance(t, dict) and t.get("id")]
    all_tasks = [t for t in candidates if _plain_component(t["id"])]
    invalid_task_ids = [str(t["id"])[:80] for t in candidates if not _plain_component(t["id"])]
    order = _tier_order(suite, all_tasks)

    tiers_filter = manifest.get("tiers")
    if not (isinstance(tiers_filter, list) and tiers_filter):
        tiers_filter = None
    scoped = [t for t in all_tasks if tiers_filter is None or (t.get("tier") or "unknown") in tiers_filter]

    models = {PLAIN_CELL: manifest.get("simple_claude_model"), QUOIN_CELL: manifest.get("quoin_claude_model")}
    rows = {arm: {} for arm in ARMS}
    for task in scoped:
        for arm in ARMS:
            task_dir = run_path / arm / task["id"]
            rows[arm][task["id"]] = load_row(
                task_dir if task_dir.is_dir() else None, models[arm], pricing, cell=arm,
            )

    tiers = {}
    counts = []
    for tier in order:
        in_tier = [t for t in scoped if (t.get("tier") or "unknown") == tier]
        if not in_tier:
            continue
        tiers[tier] = _group({arm: [rows[arm][t["id"]] for t in in_tier] for arm in ARMS})
        counts.append("%s %d" % (tier, len(in_tier)))
    overall = _group({arm: [rows[arm][t["id"]] for t in scoped] for arm in ARMS})

    entries = [_pair_entry(t, {arm: rows[arm][t["id"]] for arm in ARMS}) for t in scoped]
    try:
        retries = int(manifest.get("max_retries") or 0)
    except (TypeError, ValueError):
        retries = 0

    report = {
        "schema_version": 1,
        "arms": {"plain": PLAIN_CELL, "quoin": QUOIN_CELL},
        "denominator": "suite",
        "tiers_filter": tiers_filter,
        "retries": retries,
        "final_equals_first_attempt": retries == 0,
        "statement": RETRIES_STATEMENT if retries == 0 else None,
        "tiers": tiers,
        "overall": overall,
        "complete_pairs": _complete_pairs(entries),
        "pairs": entries,
        "validity": _validity(
            run_path, manifest, suite_sha, {t["id"] for t in all_tasks}, scoped, rows, entries,
            invalid_task_ids,
        ),
        "permission_denials_by_arm": {
            arm: {
                "sum": overall[arm]["permission_denials_sum"],
                "rows_with_denials": overall[arm]["rows_with_denials"],
            }
            for arm in ARMS
        },
        "caveat": _caveat(len(scoped), counts, overall),
    }
    return report


def _caveat(n_tasks, counts, overall):
    text = "Caveat: n=%d tasks (%s) is directional, not statistically significant." % (
        n_tasks, ", ".join(counts))
    if any(overall[arm]["n_invalid"] for arm in ARMS):
        text += " Effective n after excluding invalid rows: plain %d, quoin %d." % (
            overall[PLAIN_CELL]["n"], overall[QUOIN_CELL]["n"])
    return text


def _pct(rate):
    return "%.1f%%" % (100 * rate)


def _pp(value):
    return "%+.1f pp" % value


def _int(value):
    return "%d" % round(value)


def _sint(value):
    return "%+d" % round(value)


def _group_rows_outcomes(label, group):
    rows = []
    for arm in ARMS:
        s = group[arm]
        lo, hi = s["wilson_ci_95"]
        o = s["outcomes"]
        rows.append([
            label, arm, str(s["n"]), str(s["passes"]), _pct(s["first_attempt_pass_rate"]),
            _pct(s["final_pass_rate"]), "[%.3f, %.3f]" % (lo, hi),
            str(o["fail"]), str(o["error"]), str(o["timeout"]), str(o["budget_stopped"]), str(o["not_run"]),
            str(o["invalid"]),
        ])
    d = group["delta"]
    rows.append([
        label, "delta", "", "%+d" % (group[QUOIN_CELL]["passes"] - group[PLAIN_CELL]["passes"]),
        _pp(d["first_attempt_pass_rate_pp"]), _pp(d["final_pass_rate_pp"]), "", "", "", "", "", "", "",
    ])
    return rows


def _group_rows_cost(label, group):
    rows = []
    for arm in ARMS:
        s = group[arm]
        rows.append([
            label, arm, "$%.2f" % s["usd"]["sum"], "$%.2f" % s["usd"]["mean"],
            *[_int(s["tokens"][t]["sum"]) for t in TOKEN_TYPES],
            "%.1f" % s["wall_clock_s"]["sum"], "%.1f" % s["wall_clock_s"]["mean"],
            str(s["tool_errors"]["sum"]),
        ])
    d = group["delta"]
    rows.append([
        label, "delta", "%+.2f" % d["usd"]["sum"], "%+.2f" % d["usd"]["mean"],
        *[_sint(d["tokens"][t]["sum"]) for t in TOKEN_TYPES],
        "%+.1f" % d["wall_clock_s"]["sum"], "%+.1f" % d["wall_clock_s"]["mean"],
        "%+d" % d["tool_errors_sum"],
    ])
    return rows


def _mean_text(value, signed=False):
    if value is None:
        return "n/a"
    return ("%+.1f" if signed else "%.1f") % value


def _route_text(quoin_entry):
    route = quoin_entry.get("workflow_route")
    if not isinstance(route, dict) or not route.get("route"):
        return "n/a"
    return route["route"] + (" (%s)" % route["profile"] if route.get("profile") else "")


def render_tiered_markdown(report, format_table):
    """Render the tiered section; `format_table` is the shared Markdown table helper."""
    validity = report["validity"]

    def table_text(rows):
        return format_table([[str(cell).replace("|", "\\|") for cell in row] for row in rows])

    lines = ["", "## Tiered comparison: plain (%s) vs quoin (%s)" % (PLAIN_CELL, QUOIN_CELL), ""]

    if not validity["valid"]:
        lines.append("**Results are invalid:** %s" % "; ".join(validity["invalid_reasons"]))
        lines.append("")
    if not validity["final"]:
        lines.append(
            "**Results are provisional:** %s; failure counts below include these rows."
            % "; ".join(validity["provisional_reasons"])
        )
        lines.append("")

    lines.append(
        "Denominators come from the suite: every task in scope counts for each arm and every "
        "non-pass, including not_run, counts as a failure. The per-cell tables above count only "
        "task directories that are present."
    )
    lines.append("")
    if report.get("statement"):
        lines.append(report["statement"])
        lines.append("")
    lines.append(report["caveat"])
    lines.append("")

    groups = [(tier, g) for tier, g in report["tiers"].items()] + [("overall", report["overall"])]

    lines.append("### Outcomes by tier")
    lines.append("")
    header = ["Tier", "Arm", "N", "Passes", "First-attempt rate", "Final rate", "95% CI",
              "fail", "error", "timeout", "budget_stopped", "not_run", "invalid"]
    table = [header, ["---"] * len(header)]
    for label, group in groups:
        table.extend(_group_rows_outcomes(label, group))
    lines.append(table_text(table))

    lines.append("### Cost, tokens and time by tier")
    lines.append("")
    header = ["Tier", "Arm", "USD sum", "USD mean", "Input", "Output", "Cache read", "Cache write",
              "Wall s sum", "Wall s mean", "Tool errors"]
    table = [header, ["---"] * len(header)]
    for label, group in groups:
        table.extend(_group_rows_cost(label, group))
    lines.append(table_text(table))
    lines.append("_Unavailable values count as 0 in sums and means._")
    lines.append("")
    for arm in ARMS:
        stats = report["overall"][arm]
        cap = "no cap recorded" if stats["cap_usd"] is None else "cap $%.2f per task" % stats["cap_usd"]
        lines.append(
            "Overshoot (%s): %s, $%.2f above cap in total, %d rows over cap  "
            % (arm, cap, stats["overshoot_usd_sum"], stats["rows_over_cap"])
        )
    lines.append("")
    denials = report["permission_denials_by_arm"]
    lines.append(
        "Permission denials: "
        + "; ".join(
            "%s %d (in %d rows)" % (arm, denials[arm]["sum"], denials[arm]["rows_with_denials"])
            for arm in ARMS
        )
        + "  "
    )
    lines.append("")

    cp = report["complete_pairs"]
    if cp["pairs_not_run"] > 0 or cp["pairs_invalid"] > 0:
        p, q, d = cp[PLAIN_CELL], cp[QUOIN_CELL], cp["delta"]
        lines.append("### Complete pairs")
        lines.append("")
        lines.append(
            "Only tasks where both arms ran (%d pairs; %d suite tasks have a missing arm%s)."
            % (cp["n_pairs"], cp["pairs_not_run"],
               ", %d have an invalid quoin row" % cp["pairs_invalid"] if cp["pairs_invalid"] else "")
        )
        lines.append("")
        header = ["Measure", PLAIN_CELL, QUOIN_CELL, "delta"]
        table = [
            header, ["---"] * len(header),
            ["Pass rate", _pct(p["pass_rate"]), _pct(q["pass_rate"]), _pp(d["pass_rate_pp"])],
            ["USD sum", "$%.2f" % p["usd_sum"], "$%.2f" % q["usd_sum"], "%+.2f" % d["usd_sum"]],
            ["Output tokens", _int(p["tokens_sum"]["output"]), _int(q["tokens_sum"]["output"]),
             _sint(d["tokens_sum"]["output"])],
            ["Cache read tokens", _int(p["tokens_sum"]["cache_read"]), _int(q["tokens_sum"]["cache_read"]),
             _sint(d["tokens_sum"]["cache_read"])],
            ["Wall s sum", "%.1f" % p["wall_clock_s_sum"], "%.1f" % q["wall_clock_s_sum"],
             "%+.1f" % d["wall_clock_s_sum"]],
        ]
        lines.append(table_text(table))

    lines.append("### Per-task pairs")
    lines.append("")
    lines.append(
        "The JSON holds all four token types per row; this table shows output and cache read tokens."
    )
    lines.append("")
    header = ["Task", "Tier", "Plain", "Quoin", "Plain USD", "Quoin USD", "Plain out tok", "Quoin out tok",
              "Plain cache read", "Quoin cache read", "Plain wall s", "Quoin wall s",
              "Plain tool err", "Quoin tool err", "Tree match", "Quoin route"]
    table = [header, ["---"] * len(header)]
    for e in report["pairs"]:
        a, b = e[PLAIN_CELL], e[QUOIN_CELL]

        def usd(x):
            return "$%.2f" % x["usd"] if x["usd_source"] != "unavailable" else "n/a"

        def tok(x, t):
            return _int(x["tokens"][t]) if x["tokens_source"] != "unavailable" else "n/a"

        def err(x):
            return str(x["tool_errors"]) if x["tool_errors_available"] else "n/a"

        match = {True: "yes", False: "NO", None: "n/a"}[e["tree_sha_match"]]
        table.append([
            e["task_id"], e["tier"], a["verdict"], b["verdict"], usd(a), usd(b),
            tok(a, "output"), tok(b, "output"), tok(a, "cache_read"), tok(b, "cache_read"),
            "%.1f" % a["wall_clock_s"], "%.1f" % b["wall_clock_s"], err(a), err(b), match,
            _route_text(b),
        ])
    lines.append(table_text(table))

    lines.append("### Patch size by tier")
    lines.append("")
    header = ["Tier", "Arm", "N measured", "Non-test +", "Non-test -", "Non-test files",
              "Test +", "Test -", "Test files", "Mean lines changed", "Empty", "Missing"]
    table = [header, ["---"] * len(header)]
    for label, group in groups:
        for arm in ARMS:
            ps = group[arm]["patch_size"]
            table.append([
                label, arm, str(ps["n_measured"]),
                str(ps["non_test"]["added"]), str(ps["non_test"]["removed"]), str(ps["non_test"]["files"]),
                str(ps["test"]["added"]), str(ps["test"]["removed"]), str(ps["test"]["files"]),
                _mean_text(ps["mean_lines_changed"]), str(ps["n_empty"]), str(ps["n_missing"]),
            ])
        d = group["delta"]["patch_size"]
        table.append([
            label, "delta", "",
            _sint(d["non_test"]["added"]), _sint(d["non_test"]["removed"]), _sint(d["non_test"]["files"]),
            _sint(d["test"]["added"]), _sint(d["test"]["removed"]), _sint(d["test"]["files"]),
            _mean_text(d["mean_lines_changed"], signed=True), "", "",
        ])
    lines.append(table_text(table))
    lines.append("_%s Patches that are empty or missing are counted separately; an empty patch counts as 0 lines._"
                 % _patch_size.TEST_FILE_RULE)
    lines.append("")

    lines.append("### Patch size per task")
    lines.append("")
    header = ["Task", "Tier", "Plain non-test +/-", "Quoin non-test +/-", "Plain test +/-",
              "Quoin test +/-", "Plain files", "Quoin files", "Plain status", "Quoin status"]
    table = [header, ["---"] * len(header)]
    for e in report["pairs"]:
        a, b = e[PLAIN_CELL]["patch_size"], e[QUOIN_CELL]["patch_size"]
        table.append([
            e["task_id"], e["tier"],
            "+%d/-%d" % (a["non_test"]["added"], a["non_test"]["removed"]),
            "+%d/-%d" % (b["non_test"]["added"], b["non_test"]["removed"]),
            "+%d/-%d" % (a["test"]["added"], a["test"]["removed"]),
            "+%d/-%d" % (b["test"]["added"], b["test"]["removed"]),
            str(a["files"]), str(b["files"]), a["status"], b["status"],
        ])
    lines.append(table_text(table))

    lines.append("### Validity")
    lines.append("")
    if not validity["valid"]:
        verdict = "Valid: NO (%s)" % "; ".join(validity["invalid_reasons"])
    elif not validity["final"]:
        verdict = "Valid: yes (provisional)"
    else:
        verdict = "Valid: yes"
    lines.append(verdict + "  ")
    match = validity["suite_sha_match"]
    lines.append(
        "Suite SHA: manifest %s, computed %s, match %s  "
        % (validity["suite_sha_manifest"], validity["suite_sha_computed"],
           "unknown" if match is None else ("yes" if match else "NO"))
    )
    lines.append("Arm config mode: %s  " % validity["arm_config_mode"])
    if validity["asymmetric_extra_skills"]:
        lines.append(
            "Asymmetry: the operator's home holds skills the quoin arm sees and the plain arm does not: %s  "
            % ", ".join(validity["asymmetric_extra_skills"])
        )
    lines.append("Contamination checks: %s  " % validity["contamination_checks"])
    lines.append("Effort: %s  " % validity["effort"])
    bench = validity["bench_models"]
    lines.append(
        "Bench models: %s  "
        % (", ".join("%s=%s" % (k, bench[k]) for k in sorted(bench)) if bench else "not recorded")
    )
    observed = validity["observed_models"]
    for arm in ARMS:
        seen = (observed or {}).get(arm)
        lines.append(
            "Observed models (%s): %s  "
            % (arm, ", ".join(seen) if seen else "not recorded")
        )
    for arm in ARMS:
        lines.append("Models (%s): %s  " % (arm, ", ".join(validity["models"][arm]) or "none recorded"))
    for arm in ARMS:
        sc = validity["tokens_source_counts"][arm]
        lines.append(
            "Token sources (%s): model_usage %d, usage %d, unavailable %d  "
            % (arm, sc["model_usage"], sc["usage"], sc["unavailable"])
        )
    for key in ("identity_trip_rows", "systemic_error_rows", "session_errored_rows", "pre_spawn_error_rows",
                "judge_pending_rows", "unreadable_rows", "unknown_verdict_rows", "stream_metrics_error_rows",
                "unexpected_task_dirs", "tree_sha_mismatches", "model_mismatch_rows", "unpriced_models",
                "estimate_lower_bound_rows", "invalid_quoin_rows", "no_pipeline_artifacts_rows",
                "stopped_before_plan_rows", "no_pipeline_evidence_rows", "post_plan_timeout_rows",
                "post_plan_budget_stop_rows"):
        items = validity[key]
        suffix = ": %s" % ", ".join(items) if items else ""
        lines.append("%s: %d%s  " % (key, len(items), suffix))
    rc = validity["quoin_route_counts"]
    lines.append("Quoin routes: fast %d, full %d, unknown %d  " % (rc["fast"], rc["full"], rc["unknown"]))
    if not validity["model_mismatch_checked"]:
        lines.append("Model mismatch check: not recorded in this run's manifest  ")
    lines.append("Note: %s  " % validity["estimate_lower_bound_note"])
    if validity["invalid_task_ids"]:
        lines.append(
            "Skipped suite task ids that are not a single plain path component: %s  "
            % ", ".join(validity["invalid_task_ids"])
        )
    lines.append("")
    return "\n".join(lines)
