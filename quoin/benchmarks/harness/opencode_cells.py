"""Shared logic for the ``simple-opencode`` and ``quoin-opencode`` benchmark cells.

Both cells drive the same pinned ``opencode run --format json`` binary with the
same model, endpoint and native configuration file, read from the environment
once per run. The only difference is that the Quoin cell installs the Quoin
OpenCode scaffold into the work tree first and asks for the Quoin workflow.
That keeps the comparison paired: a report refuses to compare cells whose
model or endpoint differ, because a different model would confound the result.

This module also evaluates the frozen pilot thresholds in
``opencode-qualification.json`` and renders a report that keeps task quality
and overhead apart. It never invents a count: a threshold without enough runs
is ``pending``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

BENCHMARK_DIR = Path(__file__).resolve().parent.parent
QUALIFICATION_SPEC = BENCHMARK_DIR / "opencode-qualification.json"

PINNED_OPENCODE_VERSION = "1.18.32"
OPENCODE_CELLS = ("simple-opencode", "quoin-opencode")

ENV_MODEL = "QUOIN_BENCH_OPENCODE_MODEL"
ENV_BASE_URL = "QUOIN_BENCH_OPENCODE_BASE_URL"
ENV_ROUTE = "QUOIN_BENCH_OPENCODE_ROUTE"
ENV_CONFIG = "QUOIN_BENCH_OPENCODE_CONFIG"
ENV_CONTEXT_LIMIT = "QUOIN_BENCH_OPENCODE_CONTEXT_LIMIT"
ENV_OUTPUT_LIMIT = "QUOIN_BENCH_OPENCODE_OUTPUT_LIMIT"
ENV_PERMISSIONS = "QUOIN_BENCH_OPENCODE_PERMISSIONS"
ENV_TASK_SEED = "QUOIN_BENCH_TASK_SEED"
ENV_BINARY = "QUOIN_BENCH_OPENCODE_BIN"

SUCCESS_VERDICTS = frozenset({"pass"})


class PairingError(ValueError):
    """Two cells were not run against the same model and endpoint."""


# ---------------------------------------------------------------------------
# provenance
# ---------------------------------------------------------------------------


def config_digest(path: Optional[str]) -> Optional[str]:
    """sha256 of the native configuration file, or None when there is none."""
    if not path:
        return None
    try:
        return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def redact_endpoint(url: Optional[str]) -> Optional[str]:
    """scheme://host[:port]/path only: credentials, query and fragment never land in results."""
    if not url:
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if not parts.scheme or not parts.hostname:
        return None
    netloc = parts.hostname + (f":{port}" if port else "")
    return f"{parts.scheme}://{netloc}{parts.path.rstrip('/')}"


def configured_endpoint(config_path: str, model: str) -> Optional[str]:
    """The base URL the native config gives the model's provider, or None.

    This is the endpoint OpenCode actually calls, so pairing is checked against
    it rather than against an operator-typed label. Plain JSON only; a config
    the harness cannot read cannot be shown to be paired.
    """
    try:
        data = json.loads(Path(config_path).read_text(encoding="utf-8"))
        provider = data["provider"][model.split("/", 1)[0]]
        url = provider["options"]["baseURL"]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    return url if isinstance(url, str) else None


def _int_or_none(value: Optional[str]) -> Optional[int]:
    try:
        return int(value) if value not in (None, "") else None
    except ValueError:
        return None


def _repo_commit(workdir: Path) -> Optional[str]:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(workdir),
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = proc.stdout.strip()
    return out if proc.returncode == 0 and out else None


def _adapter_version() -> Optional[str]:
    try:
        from quoin.__about__ import __version__
    except ImportError:
        return None
    return __version__


def build_provenance(
    cell: str,
    workdir: Path,
    env: Mapping[str, str],
    runtime_version: Optional[str] = None,
) -> Dict[str, Any]:
    """Everything a reader needs to reproduce or pair a run."""
    if cell not in OPENCODE_CELLS:
        raise ValueError(f"not an OpenCode cell: {cell!r}")
    return {
        "cell": cell,
        "runtime": "opencode",
        "runtime_version": runtime_version,
        "runtime_version_pinned": PINNED_OPENCODE_VERSION,
        "adapter": "quoin-opencode" if cell == "quoin-opencode" else "none",
        "adapter_version": _adapter_version() if cell == "quoin-opencode" else None,
        "provider_route": env.get(ENV_ROUTE) or None,
        "endpoint": redact_endpoint(env.get(ENV_BASE_URL)),
        "model": env.get(ENV_MODEL) or None,
        "config_digest": config_digest(env.get(ENV_CONFIG)),
        "context_limit": _int_or_none(env.get(ENV_CONTEXT_LIMIT)),
        "output_limit": _int_or_none(env.get(ENV_OUTPUT_LIMIT)),
        "repo_commit": _repo_commit(workdir),
        "permissions": env.get(ENV_PERMISSIONS) or None,
        "task_seed": env.get(ENV_TASK_SEED) or None,
    }


def missing_pairing_inputs(env: Mapping[str, str]) -> List[str]:
    """Environment variables a paired run cannot do without."""
    return [name for name in (ENV_MODEL, ENV_BASE_URL, ENV_CONFIG) if not env.get(name)]


def check_pairing(provenances: Iterable[Mapping[str, Any]]) -> None:
    """Refuse a comparison unless every cell used one model and one endpoint.

    Raises PairingError naming the first mismatch. Missing values count as a
    mismatch: an unrecorded model cannot be shown to be the same model.
    """
    records = list(provenances)
    cells = {r.get("cell") for r in records}
    absent = [c for c in OPENCODE_CELLS if c not in cells]
    if absent:
        raise PairingError(f"paired comparison needs both cells; missing {', '.join(absent)}")
    for key in ("model", "endpoint"):
        values = {r.get(key) for r in records}
        if None in values or "" in values:
            raise PairingError(f"paired comparison refused: {key} not recorded for every run")
        if len(values) > 1:
            raise PairingError(
                f"paired comparison refused: cells used different {key} values "
                f"({', '.join(sorted(str(v) for v in values))})"
            )


# ---------------------------------------------------------------------------
# threshold evaluation and reporting
# ---------------------------------------------------------------------------


def load_spec(path: Path = QUALIFICATION_SPEC) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _matches(row: Mapping[str, Any], scenario: Optional[str], cell: Optional[str]) -> bool:
    if row.get("scenario") != scenario:
        return False
    return cell is None or row.get("cell") == cell


def _failure_modes(rows: Sequence[Mapping[str, Any]]) -> Dict[str, int]:
    modes: Dict[str, int] = {}
    for row in rows:
        if row.get("verdict") in SUCCESS_VERDICTS:
            continue
        key = str(row.get("failure_reason") or row.get("verdict") or "unknown")
        modes[key] = modes.get(key, 0) + 1
    return dict(sorted(modes.items()))


def _count_entry(rows, threshold, cell, label) -> Dict[str, Any]:
    """Judge the frozen sample: the first ``required_runs`` matching rows, in order.

    Runs beyond the sample are reported as surplus and never change the
    verdict, so rerunning until a good streak appears cannot pass a threshold.
    """
    selected = [r for r in rows if _matches(r, threshold.get("scenario"), cell)]
    required = int(threshold["required_runs"])
    minimum = int(threshold["min_successes"])
    sample = selected[:required]
    successes = sum(1 for r in sample if r.get("verdict") in SUCCESS_VERDICTS)
    failures = len(sample) - successes
    if failures > required - minimum:
        status = "fail"
    elif len(sample) == required:
        status = "pass"
    else:
        status = "pending"
    return {
        "id": threshold["id"] if cell is None or not threshold.get("per_cell") else f"{threshold['id']}:{cell}",
        "label": label, "status": status, "runs": len(sample), "successes": successes,
        "required_runs": required, "min_successes": minimum,
        "surplus_runs": len(selected) - len(sample),
        "failure_modes": _failure_modes(sample),
    }


def evaluate_thresholds(
    rows: Sequence[Mapping[str, Any]],
    invariants_passed: Optional[bool],
    spec: Optional[Mapping[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Judge each frozen threshold as pass, fail or pending.

    Unsuccessful runs count. A count threshold fails as soon as its sample's
    failures exceed the allowance, passes once the full sample is within it,
    and is pending otherwise. A ``per_cell`` threshold is judged separately
    for each cell, so two half-size samples are never pooled into one.
    ``invariants_passed`` is None when the deterministic suite has not run.
    """
    spec = spec if spec is not None else load_spec()
    results: List[Dict[str, Any]] = []
    for threshold in spec["thresholds"]:
        if threshold.get("kind") == "all_pass":
            results.append({
                "id": threshold["id"], "label": threshold["label"],
                "status": "pending" if invariants_passed is None else (
                    "pass" if invariants_passed else "fail"),
            })
        elif threshold.get("per_cell"):
            for cell in spec["cells"]:
                results.append(_count_entry(rows, threshold, cell, f"{threshold['label']} ({cell})"))
        else:
            results.append(_count_entry(rows, threshold, threshold.get("cell"), threshold["label"]))
    return results


def _mean(values: List[float]) -> Optional[float]:
    return round(sum(values) / len(values), 3) if values else None


def summarize_cells(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Per-cell quality and overhead, kept in separate sections.

    Quality comes from validators, tests and the review rubric; overhead from
    setup effort, wall time, turns, tool errors, tokens and cost. Unknown
    token or cost values are counted as unknown, never as zero.
    """
    out: Dict[str, Dict[str, Any]] = {}
    for cell in sorted({str(r.get("cell")) for r in rows}):
        mine = [r for r in rows if r.get("cell") == cell]
        passed = sum(1 for r in mine if r.get("verdict") in SUCCESS_VERDICTS)
        findings = [float(r["review_findings"]) for r in mine if r.get("review_findings") is not None]
        known_cost = [float(r["cost_usd"]) for r in mine if r.get("cost_usd") is not None]
        tokens = [float(r["tokens"]) for r in mine if r.get("tokens") is not None]
        out[cell] = {
            "quality": {
                "runs": len(mine),
                "completed": passed,
                "unsuccessful": len(mine) - passed,
                "mean_review_findings": _mean(findings),
            },
            "overhead": {
                "mean_setup_seconds": _mean([float(r["setup_seconds"]) for r in mine if r.get("setup_seconds") is not None]),
                "mean_wall_seconds": _mean([float(r["wall_seconds"]) for r in mine if r.get("wall_seconds") is not None]),
                "mean_turns": _mean([float(r["turns"]) for r in mine if r.get("turns") is not None]),
                "tool_errors": sum(int(r.get("tool_errors") or 0) for r in mine),
                "mean_tokens": _mean(tokens),
                "runs_with_unknown_tokens": len(mine) - len(tokens),
                "known_cost_usd": round(sum(known_cost), 6) if known_cost else None,
                "runs_with_unknown_cost": len(mine) - len(known_cost),
            },
        }
    return out


def build_report(
    rows: Sequence[Mapping[str, Any]],
    invariants_passed: Optional[bool],
    spec: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Pairing check, thresholds and per-cell summary for one result set."""
    pairing: Dict[str, Any]
    try:
        unrecorded = [r for r in rows if not isinstance(r.get("provenance"), Mapping)]
        if unrecorded:
            raise PairingError(
                f"paired comparison refused: {len(unrecorded)} run(s) have no provenance record"
            )
        check_pairing([r["provenance"] for r in rows])
        pairing = {"status": "paired"}
    except PairingError as exc:
        pairing = {"status": "refused", "reason": str(exc)}
    return {
        "pairing": pairing,
        "thresholds": evaluate_thresholds(rows, invariants_passed, spec),
        "cells": summarize_cells(rows) if pairing["status"] == "paired" else {},
    }


def render_markdown(report: Mapping[str, Any]) -> str:
    lines = ["# OpenCode qualification report", ""]
    lines.append("## Pilot thresholds")
    lines.append("")
    lines.append("| Threshold | Status | Successes / runs | Required | Surplus runs | Failure modes |")
    lines.append("|---|---|---|---|---|---|")
    for t in report["thresholds"]:
        if "runs" in t:
            counts = f"{t['successes']} / {t['runs']}"
            required = f">= {t['min_successes']} of {t['required_runs']}"
            modes = ", ".join(f"{k}: {v}" for k, v in t["failure_modes"].items()) or "none"
            surplus = str(t["surplus_runs"])
        else:
            counts, required, modes, surplus = "n/a", "all pass", "n/a", "n/a"
        lines.append(f"| {t['label']} | {t['status']} | {counts} | {required} | {surplus} | {modes} |")
    lines.append("")
    pairing = report["pairing"]
    if pairing["status"] != "paired":
        lines.append(f"Paired comparison refused: {pairing['reason']}")
        lines.append("")
        return "\n".join(lines)
    for section in ("quality", "overhead"):
        lines.append(f"## Task {section}" if section == "quality" else "## Overhead")
        lines.append("")
        cells = report["cells"]
        keys = list(next(iter(cells.values()))[section].keys()) if cells else []
        lines.append("| Metric | " + " | ".join(cells) + " |")
        lines.append("|---|" + "---|" * len(cells))
        for key in keys:
            values = ["unknown" if cells[c][section][key] is None else str(cells[c][section][key]) for c in cells]
            lines.append(f"| {key} | " + " | ".join(values) + " |")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# cell invocation
# ---------------------------------------------------------------------------

QUOIN_PROMPT_PREFIX = (
    "Use the Quoin workflow installed in this repository's .opencode/ directory. "
    "Keep workflow artifacts under .workflow_artifacts/ and follow the command "
    "guidance it provides.\n\n"
)

# Paths the Quoin cell's scaffold install writes; kept out of the scored diff.
SCAFFOLD_PATHS = (".opencode", ".quoin")

# Inherited settings that would add configuration on top of the pinned file.
STRIPPED_ENV = ("OPENCODE_CONFIG_CONTENT", "OPENCODE_CONFIG_DIR", "OPENCODE_PERMISSION")


def build_prompt(task_spec: Mapping[str, Any], quoin_enabled: bool) -> str:
    if task_spec.get("prompt"):
        body = str(task_spec["prompt"])
    else:
        from .cells.simple_codex import _build_codex_prompt

        body = _build_codex_prompt(dict(task_spec))
    return (QUOIN_PROMPT_PREFIX + body) if quoin_enabled else body


def isolated_env(env: Mapping[str, str], root: Path) -> Dict[str, Any]:
    """Run environment with the user's global OpenCode and Claude state hidden.

    HOME and the XDG directories point at a fresh directory, so neither cell
    loads ``~/.config/opencode``, ``~/.claude/CLAUDE.md`` or global skills;
    the pinned config file is the only configuration both cells share.
    Provider credentials passed as environment variables are kept.
    """
    run_env = {k: v for k, v in env.items() if k not in STRIPPED_ENV}
    for name in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        path = root / name.lower()
        path.mkdir(parents=True, exist_ok=True)
        run_env[name] = str(path)
    run_env["OPENCODE_DISABLE_CLAUDE_CODE"] = "1"
    run_env["OPENCODE_DISABLE_EXTERNAL_SKILLS"] = "1"
    run_env["OPENCODE_CONFIG"] = env[ENV_CONFIG]
    return {"env": run_env, "removed": sorted(k for k in STRIPPED_ENV if k in env)}


_VERSION_RE = re.compile(r"\b(\d+\.\d+\.\d+)\b")


def _runtime_version(binary: str, env: Mapping[str, str]) -> Optional[str]:
    try:
        proc = subprocess.run([binary, "--version"], capture_output=True, text=True,
                              timeout=30, check=False, env=dict(env))
    except (OSError, subprocess.SubprocessError):
        return None
    match = _VERSION_RE.search(proc.stdout) if proc.returncode == 0 else None
    return match.group(1) if match else None


def _is_tool_error(event: Any, ev: Any) -> bool:
    if event.type is ev.EventType.ERROR:
        return True
    payload = event.payload
    return (event.type is ev.EventType.PROGRESS and getattr(payload, "kind", None) == "tool"
            and getattr(payload, "status", None) == "error")


def digest_events(lines: Iterable[str], run_id: str) -> Dict[str, Any]:
    """Turns, tool errors and usage from raw ``--format json`` lines.

    Uses the adapter's own event translation, so unknown usage stays unknown.
    """
    from quoin.opencode_adapter import events as ev

    # The adapter's event schema only accepts its own run-id shape; derive a
    # stable one from the benchmark run id so the translation is reproducible.
    adapter_run_id = "oc-20000101T000000Z-" + hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:8]
    raw_events: List[Dict[str, Any]] = []
    runtime_events = []
    untranslated = 0
    for line in lines:
        parsed = ev.parse_line(line)
        if parsed.obj is None:
            continue
        raw_events.append(json.loads(json.dumps(parsed.obj, default=str)))
        try:
            runtime_events.extend(ev.translate(
                parsed.obj, run_id=adapter_run_id, attempt=1, observed_at=ev.iso_now(time.time),
            ))
        except ValueError:
            untranslated += 1
    steps = ev.summarize_steps(runtime_events)
    usage = ev.usage_totals([e for e in runtime_events if e.type is ev.EventType.USAGE])
    parts = (usage.input_tokens, usage.output_tokens)
    return {
        "transcript_events": raw_events,
        "turn_count": steps.finishes,
        "tool_errors": sum(1 for e in runtime_events if _is_tool_error(e, ev)),
        "untranslated_events": untranslated,
        "tokens_in": usage.input_tokens,
        "tokens_out": usage.output_tokens,
        "tokens_cache_read": usage.cache_read_tokens,
        "tokens_cache_write": usage.cache_write_tokens,
        "tokens_known": all(p is not None for p in parts),
        "cost": None if usage.cost is None else str(usage.cost),
    }


def _install_quoin(workdir: Path, env: Mapping[str, str]) -> Optional[str]:
    """Install the Quoin OpenCode scaffold; returns an error string on failure."""
    cmd = [sys.executable, "-m", "quoin", "install", "--runtime", "opencode",
           "--project-root", str(workdir)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300,
                              check=False, env=dict(env))
    except (OSError, subprocess.SubprocessError) as exc:
        return f"quoin-install-error: {type(exc).__name__}"
    if proc.returncode != 0:
        return f"quoin-install-exit-{proc.returncode}"
    return None


def _run_bounded(cmd: List[str], cwd: Path, env: Mapping[str, str], seconds: float):
    """Run in its own process group; on timeout kill the whole group.

    Returns (stdout, returncode, timed_out). OpenCode starts helper processes
    that can hold stdout open, so killing only the leader is not enough.
    """
    from .proc_group import kill_and_reap

    proc = subprocess.Popen(cmd, cwd=str(cwd), env=dict(env), stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                            text=True, start_new_session=True)
    try:
        out, _ = proc.communicate(timeout=seconds)
        return out or "", proc.returncode, False
    except subprocess.TimeoutExpired:
        kill_and_reap(proc, proc.pid, group=True)
        try:
            out, _ = proc.communicate(timeout=10)
        except (subprocess.TimeoutExpired, ValueError):
            out = ""
        return out or "", None, True


def _diff(workdir: Path) -> str:
    """Tracked and new files, minus the Quoin scaffold."""
    excludes = [f":(exclude){p}" for p in SCAFFOLD_PATHS]
    try:
        subprocess.run(["git", "add", "-N", "--", ".", *excludes], cwd=str(workdir),
                       capture_output=True, timeout=30, check=False)
        proc = subprocess.run(["git", "diff", "HEAD", "--", ".", *excludes], cwd=str(workdir),
                              capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout


def invoke_opencode(
    cell: str,
    task_spec: Mapping[str, Any],
    workdir: Path,
    wall_clock_seconds: float,
    run_id: str,
    env: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """Run one task in an OpenCode cell and return the cell-adapter result dict."""
    env = dict(os.environ if env is None else env)
    quoin_enabled = cell == "quoin-opencode"
    binary = env.get(ENV_BINARY) or "opencode"
    result: Dict[str, Any] = {
        "prompt": "", "transcript_events": [], "diff_patch": "",
        "cost_available": False, "cost_runtime_usd": None,
        "cost_estimated_usd": None, "cost_delta_usd": None,
        "tokens_in": None, "tokens_out": None,
        "tokens_cache_read": None, "tokens_cache_write": None,
        "turn_count": 0, "gate_intervention_count": 0, "verdict": None,
        "extra": {"scenario": task_spec.get("scenario")},
    }
    extra = result["extra"]

    def refuse(reason: str) -> Dict[str, Any]:
        result["verdict"] = "error"
        extra["failure_reason"] = reason
        return result

    # Provenance first, so even a refused run carries what was configured.
    version = _runtime_version(binary, env)
    extra["provenance"] = build_provenance(cell, workdir, env, version)
    if task_spec.get("source") == "swebench_verified":
        return refuse("verified-tasks-unsupported")
    missing = missing_pairing_inputs(env)
    if missing:
        return refuse("missing-pairing-inputs: " + ", ".join(missing))
    actual = configured_endpoint(env[ENV_CONFIG], env[ENV_MODEL])
    if actual is None:
        return refuse("endpoint-not-in-config")
    if redact_endpoint(actual) != redact_endpoint(env[ENV_BASE_URL]):
        return refuse("endpoint-mismatch")
    if version != PINNED_OPENCODE_VERSION:
        extra["runtime_version_unqualified"] = True

    result["prompt"] = prompt = build_prompt(task_spec, quoin_enabled)
    setup_start = time.monotonic()
    if quoin_enabled:
        error = _install_quoin(workdir, env)
        if error:
            return refuse(error)
    extra["setup_seconds"] = round(time.monotonic() - setup_start, 3)

    root = Path(tempfile.mkdtemp(prefix="quoin-bench-opencode-"))
    try:
        isolation = isolated_env(env, root)
        extra["stripped_env"] = isolation["removed"]
        cmd = [binary, "run", "--format", "json", "--model", env[ENV_MODEL], prompt]
        wall_start = time.monotonic()
        try:
            stdout, returncode, timed_out = _run_bounded(cmd, workdir, isolation["env"], wall_clock_seconds)
        except FileNotFoundError:
            return refuse("opencode-not-found")
        extra["wall_seconds"] = round(time.monotonic() - wall_start, 3)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    digest = digest_events(stdout.splitlines(), run_id)
    result["transcript_events"] = digest["transcript_events"]
    result["turn_count"] = digest["turn_count"]
    for key in ("tokens_in", "tokens_out", "tokens_cache_read", "tokens_cache_write"):
        result[key] = digest[key]
    extra["tool_errors"] = digest["tool_errors"]
    extra["tokens_known"] = digest["tokens_known"]
    extra["native_cost"] = digest["cost"]
    extra["untranslated_events"] = digest["untranslated_events"]
    if timed_out:
        result["verdict"] = "timeout"
    elif returncode != 0:
        result["verdict"] = "error"
        extra["failure_reason"] = f"opencode-exit-{returncode}"
    result["diff_patch"] = _diff(workdir)
    return result


# ---------------------------------------------------------------------------
# reading a finished run
# ---------------------------------------------------------------------------


def rows_from_run_dir(run_dir: Path) -> List[Dict[str, Any]]:
    """Report rows for every OpenCode-cell task under ``<run>/<cell>/<task>/``.

    Reads the files the result writer produces: ``metrics.json`` (which holds
    the cell's ``extra``) and ``judge.json`` (the verdict). Rows keep run order
    by task directory name.
    """
    rows: List[Dict[str, Any]] = []
    for cell in OPENCODE_CELLS:
        cell_dir = Path(run_dir) / cell
        if not cell_dir.is_dir():
            continue
        for task_dir in sorted(p for p in cell_dir.iterdir() if p.is_dir()):
            try:
                metrics = json.loads((task_dir / "metrics.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            try:
                judge = json.loads((task_dir / "judge.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                judge = {}
            tin, tout = metrics.get("tokens_in"), metrics.get("tokens_out")
            rows.append({
                "cell": cell,
                "task_id": metrics.get("task_id", task_dir.name),
                "scenario": metrics.get("scenario"),
                "verdict": judge.get("verdict"),
                "failure_reason": metrics.get("failure_reason"),
                "review_findings": judge.get("review_findings"),
                "setup_seconds": metrics.get("setup_seconds"),
                "wall_seconds": metrics.get("wall_seconds", metrics.get("wall_clock_seconds")),
                "turns": metrics.get("turn_count"),
                "tool_errors": metrics.get("tool_errors"),
                "tokens": tin + tout if tin is not None and tout is not None else None,
                "cost_usd": metrics.get("native_cost"),
                "provenance": metrics.get("provenance"),
            })
    return rows
