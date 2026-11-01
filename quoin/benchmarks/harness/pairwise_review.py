"""
pairwise_review.py - Blind pairwise review of the two arms' patches.

A secondary measure that runs after judging. For each task a reviewer sees the
issue text and both patches in a seeded A/B order with the arm hidden, rates
both on correctness risk, minimality and test quality, and picks a preference.
The result is kept in its own file in the run directory and is never read by
the tiered report, so it cannot change `resolved`.

Spend goes through the run's spend ledger: one reservation before each call and
one settlement after it, under three checks (review total, run ceiling and
whole-ledger ceiling). The reviewer is injected, so tests use a fake.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol, Tuple

from . import arm_sandbox, paired_spend, patch_size, proc_group
from .session_state import pipeline_invalid_reason

PROMPT_VERSION = "pairwise-v1"
DEFAULT_SEED = 1729
STATE_FILE = "pairwise-review.json"
LOCK_FILE = "pairwise-review.lock"
BLINDING_MARKERS = (".workflow_artifacts", ".claude/", "CLAUDE.md", "AGENTS.md", "quoin")
RATIONALE_LIMIT = 4000
RAW_LIMIT = 20000
# About 100k tokens: well inside the reviewer's context window and the
# per-review cap, so one call cannot overshoot its reservation by much.
MAX_PROMPT_CHARS = 400_000
DEFAULT_REVIEW_TIMEOUT_S = 600
REVIEW_CELL = "pairwise-review"
REVIEW_INVOCATION = "pairwise-review"
REVIEW_GATE_ID = "tiered-pairwise-review"
REVIEW_ARM = "review"

PLAIN_CELL = "simple-claude"
QUOIN_CELL = "quoin-claude"

FINAL_STATUSES = ("reviewed", "invalid", "skipped")
DIMENSIONS = ("correctness_risk", "minimality", "test_quality")
REPLY_KEYS = frozenset(
    [f"{d}_{side}" for d in DIMENSIONS for side in ("a", "b")] + ["preference", "rationale"]
)
PREFERENCES = ("A", "B", "tie")

PROMPT_TEMPLATE = """You are a careful senior engineer comparing two candidate patches for the same software issue. You cannot run code. Judge from the issue text and the patches alone.

<issue>
{issue}
</issue>

<patch_a>
{patch_a}
</patch_a>

<patch_b>
{patch_b}
</patch_b>

Rate each patch on three dimensions, using integers from 1 to 5:
- correctness_risk: 1 means very low risk that the patch is wrong or incomplete, 5 means very high risk.
- minimality: 1 means the patch changes far more than the issue needs, 5 means it changes only what is needed.
- test_quality: 1 means no useful tests or poor tests, 5 means thorough, well targeted tests.

Then choose a preference: "A", "B" or "tie". Give a rationale of at most 150 words.

Reply with one JSON object in a fenced json block and nothing after it, using exactly these keys:
{{"correctness_risk_a": 1, "correctness_risk_b": 1, "minimality_a": 1, "minimality_b": 1, "test_quality_a": 1, "test_quality_b": 1, "preference": "A", "rationale": "..."}}
"""


_DELIMITER = re.compile(r"<(\s*/?\s*(?:issue|patch_a|patch_b)\b)", re.IGNORECASE)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def neutralise_delimiters(text: str) -> str:
    """Escape anything that reads as one of the prompt's section tags.

    Patch and issue text are untrusted; an unescaped `</patch_a>` inside a
    patch would end its section early.
    """
    return _DELIMITER.sub(r"&lt;\1", text)


def quoin_is_a(seed: int, task_id: str) -> bool:
    """Whether the quoin patch is shown as patch A for this task."""
    digest = hashlib.sha256(f"{seed}:{task_id}".encode("utf-8")).hexdigest()
    return int(digest, 16) & 1 == 1


def blind_patch(text: str) -> str:
    """Replace each binary block with a one-line note so no base85 data is sent."""
    out = []
    lines = text.split("\n")
    i = 0
    section_start = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            section_start = i
        if line.startswith("GIT binary patch") or (
            line.startswith("Binary files ") and line.rstrip().endswith("differ")
        ):
            header = lines[section_start] if lines[section_start].startswith("diff --git ") else ""
            try:
                path = patch_size._section_path(header[len("diff --git "):], lines[section_start + 1:i + 1])
                path = _CONTROL.sub("?", path) if path else "unknown file"
            except Exception:  # noqa: BLE001 - any unreadable header still gets the note
                path = "unknown file"
            out.append(f"[binary change to {path} omitted]")
            i += 1
            while i < len(lines) and not lines[i].startswith("diff --git "):
                i += 1
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


def marker_hit(text: str) -> Optional[str]:
    """The first blinding marker found in the text, ignoring case."""
    lowered = text.lower()
    for marker in BLINDING_MARKERS:
        if marker.lower() in lowered:
            return marker
    return None


def build_prompt(description: str, patch_a: str, patch_b: str) -> str:
    return PROMPT_TEMPLATE.format(
        issue=neutralise_delimiters(description),
        patch_a=neutralise_delimiters(patch_a),
        patch_b=neutralise_delimiters(patch_b),
    )


_FENCE = re.compile(r"```json\s*(.*?)```", re.DOTALL)


def _is_rating(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 5


def parse_reply(text: str) -> Tuple[Optional[dict], Optional[str]]:
    """Parse a reviewer reply; anything that does not fit the schema is invalid."""
    if not isinstance(text, str) or not text.strip():
        return None, "empty reply"
    fences = _FENCE.findall(text)
    candidate = fences[-1] if fences else text
    try:
        data = json.loads(candidate.strip())
    except ValueError as exc:
        return None, f"not valid JSON: {exc}"
    if not isinstance(data, dict):
        return None, "reply is not a JSON object"
    keys = set(data)
    # The ratings and the preference are what is scored; a reply that only
    # omits the free-text rationale is still a valid verdict.
    rationale_missing = "rationale" not in keys
    if rationale_missing:
        keys = keys | {"rationale"}
        data = {**data, "rationale": ""}
    if keys != REPLY_KEYS:
        missing = sorted(REPLY_KEYS - keys)
        extra = sorted(keys - REPLY_KEYS)
        return None, f"keys differ from the schema (missing {missing}, extra {extra})"
    for key in sorted(REPLY_KEYS - {"preference", "rationale"}):
        if not _is_rating(data[key]):
            return None, f"{key} must be an integer from 1 to 5"
    if data["preference"] not in PREFERENCES:
        return None, "preference must be A, B or tie"
    rationale = data["rationale"]
    if not isinstance(rationale, str) or (not rationale_missing and not rationale.strip()):
        return None, "rationale must be a non-empty string"
    parsed = dict(data)
    parsed["rationale_missing"] = rationale_missing
    parsed["rationale_truncated"] = False
    if len(rationale) > RATIONALE_LIMIT:
        parsed["rationale"] = rationale[:RATIONALE_LIMIT]
        parsed["rationale_truncated"] = True
    return parsed, None


def _read_json(path: Path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def eligibility(run_path, task_id: str) -> Tuple[bool, str]:
    """Whether both arms have a usable row and a non-empty patch for the task."""
    run_path = Path(run_path)
    dirs = {cell: run_path / cell / task_id for cell in (PLAIN_CELL, QUOIN_CELL)}
    if not all(d.is_dir() for d in dirs.values()):
        return False, "not-run"
    for cell, task_dir in dirs.items():
        metrics = _read_json(task_dir / "metrics.json") or {}
        if pipeline_invalid_reason(cell, task_dir, metrics) is not None:
            return False, "invalid-row"
    for task_dir in dirs.values():
        patch = task_dir / "diff.patch"
        if not patch.is_file():
            return False, "missing-patch"
    for task_dir in dirs.values():
        if (task_dir / "diff.patch").stat().st_size == 0:
            return False, "empty-patch"
    return True, ""


@dataclass
class ReviewReply:
    text: str
    cost_usd: Optional[float]
    error: Optional[str]


class Reviewer(Protocol):
    def review(self, prompt: str, *, model: str, max_budget_usd: float, timeout_s: float) -> ReviewReply:
        ...


def build_reviewer_argv(model: str, effort: str, max_budget_usd: float) -> list:
    """The reviewer command line; the prompt goes on stdin, never in argv."""
    return [
        "claude", "--print", "--output-format", "json", "--model", model,
        "--effort", effort, "--max-budget-usd", str(max_budget_usd),
        "--tools", "", "--strict-mcp-config", "--no-session-persistence",
    ]


class ClaudeCliReviewer:
    """One no-tools `claude --print` call per review, in an isolated sandbox."""

    def __init__(self, effort: str, run_id: str, auth: Tuple[str, str]):
        self.effort = effort
        self.run_id = run_id
        self.auth = auth

    def review(self, prompt: str, *, model: str, max_budget_usd: float, timeout_s: float) -> ReviewReply:
        sandbox = None
        try:
            try:
                sandbox = arm_sandbox.create_sandbox("review")
                work = Path(sandbox.home) / "work"
                work.mkdir(exist_ok=True)
                env = arm_sandbox.build_session_env(sandbox, REVIEW_CELL, self.run_id, self.auth)
                argv = build_reviewer_argv(model, self.effort, max_budget_usd)
                proc = subprocess.Popen(
                    argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, cwd=str(work), env=env, start_new_session=True,
                )
            except Exception as exc:  # noqa: BLE001 - reported as a reviewer error, never raised
                return ReviewReply("", None, f"reviewer setup failed: {exc}")
            try:
                stdout, stderr = proc.communicate(input=prompt, timeout=timeout_s)
            except subprocess.TimeoutExpired:
                proc_group.kill_and_reap(proc, proc.pid, True)
                return ReviewReply("", None, "timeout")
            except BaseException:
                # Ctrl-C or any other failure must not leave the reviewer running.
                proc_group.kill_and_reap(proc, proc.pid, True)
                raise
            return _reply_from_output(proc.returncode, stdout, stderr)
        finally:
            if sandbox is not None:
                arm_sandbox.remove_sandbox(sandbox)


def _reply_from_output(returncode, stdout, stderr) -> ReviewReply:
    cost = None
    text = ""
    error = None
    try:
        data = json.loads(stdout)
    except (TypeError, ValueError):
        data = None
    if isinstance(data, dict):
        raw_cost = data.get("total_cost_usd")
        if isinstance(raw_cost, (int, float)) and not isinstance(raw_cost, bool):
            cost = float(raw_cost)
        result = data.get("result")
        text = result if isinstance(result, str) else ""
        if data.get("is_error"):
            error = "reviewer reported an error"
    else:
        error = "reviewer output was not JSON"
    if returncode != 0:
        tail = (stderr or "").strip()[-300:]
        error = f"exit {returncode}" + (f": {tail}" if tail else "")
    return ReviewReply(text, cost, error)


# ---------------------------------------------------------------------------
# State file, lock and the run loop
# ---------------------------------------------------------------------------

def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_state(run_path: Path, state: dict) -> None:
    target = Path(run_path) / STATE_FILE
    tmp = Path(run_path) / (STATE_FILE + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, target)


def load_state(run_path) -> Optional[dict]:
    return _read_json(Path(run_path) / STATE_FILE)


def _settled_attempts(ledger) -> set:
    from quoin.benchmarks.scripts import spend_ledger

    if not Path(ledger).exists():
        return set()
    return {
        row["attempt_id"] for row in spend_ledger._read_rows(Path(ledger))
        if row.get("kind") == "settlement"
    }


def _clip(text, limit):
    return text if len(text) <= limit else text[:limit]


def run_review(
    run_path, suite, *, reviewer, ledger, run_id, seed, model, effort, cap, total_cap, authorised,
    timeout_s=DEFAULT_REVIEW_TIMEOUT_S, allow_partial=False,
):
    """Review every eligible task once; returns (exit_code, state).

    Exit codes: 0 done, 1 some tasks ended in `error`, 2 refusal, 3 spend stop.
    `authorised` is the one effective ceiling that feeds both run-level checks.
    """
    from quoin.benchmarks.scripts import spend_ledger

    run_path = Path(run_path)
    lock = run_path / LOCK_FILE
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        print(f"REFUSED: review-lock: {lock} exists; another review is running "
              "(remove it only if no review process is alive)", file=__import__("sys").stderr)
        return 2, None
    try:
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.close(fd)
        return _run_locked(
            run_path, suite, reviewer, ledger, run_id, seed, model, effort, cap, total_cap,
            authorised, timeout_s, spend_ledger,
        )
    finally:
        with contextlib.suppress(OSError):
            lock.unlink()


def _run_locked(run_path, suite, reviewer, ledger, run_id, seed, model, effort, cap, total_cap,
                authorised, timeout_s, spend_ledger):
    import sys

    state = load_state(run_path)
    identity = {"prompt_version": PROMPT_VERSION, "seed": seed, "model": model, "effort": effort}
    if state is not None:
        for key, value in identity.items():
            if state.get(key) != value:
                print(f"REFUSED: review-resume: stored {key} {state.get(key)!r} differs from {value!r}",
                      file=sys.stderr)
                return 2, state
    else:
        state = dict(identity)
        state["tasks"] = {}
    state["caps"] = {"per_review_usd": cap, "total_usd": total_cap, "authorised_usd": authorised}
    records = state.setdefault("tasks", {})

    # A reply that was recorded but never settled (a crash between the two writes)
    # gets exactly one settlement from the recorded cost.
    settled = _settled_attempts(ledger)
    for task_id, rec in records.items():
        att = rec.get("attempt_id")
        if att and att not in settled and rec.get("status") in ("reviewed", "invalid", "error") and "cost_usd" in rec:
            paired_spend.settle(
                ledger, att, run_id, task_id, REVIEW_CELL, cap, rec.get("cost_usd"),
                invocation=REVIEW_INVOCATION, gate_id=REVIEW_GATE_ID, arm=REVIEW_ARM,
            )
            settled.add(att)

    tasks = [t for t in suite.get("tasks", []) if isinstance(t, dict) and t.get("id")]
    stop_clause = None
    for task in tasks:
        task_id = task["id"]
        rec = records.get(task_id)
        ok, reason = eligibility(run_path, task_id)
        if not ok:
            records[task_id] = {"status": "ineligible", "reason": reason}
            _write_state(run_path, state)
            continue
        plain_bytes = (run_path / PLAIN_CELL / task_id / "diff.patch").read_bytes()
        quoin_bytes = (run_path / QUOIN_CELL / task_id / "diff.patch").read_bytes()
        hashes = {"plain": _sha256_bytes(plain_bytes), "quoin": _sha256_bytes(quoin_bytes)}
        if (rec and rec.get("status") in FINAL_STATUSES
                and rec.get("patch_sha256") == hashes):
            continue
        is_a = quoin_is_a(seed, task_id)
        quoin_text = blind_patch(quoin_bytes.decode("utf-8", errors="replace"))
        plain_text = blind_patch(plain_bytes.decode("utf-8", errors="replace"))
        base = {"quoin_is_a": is_a, "patch_sha256": hashes}
        hit = marker_hit(quoin_text) or marker_hit(plain_text)
        if hit is not None:
            records[task_id] = dict(base, status="skipped", reason="blinding-marker", marker=hit)
            _write_state(run_path, state)
            continue
        patch_a, patch_b = (quoin_text, plain_text) if is_a else (plain_text, quoin_text)
        prompt = build_prompt(task.get("description") or "", patch_a, patch_b)
        if len(prompt) > MAX_PROMPT_CHARS:
            records[task_id] = dict(base, status="skipped", reason="patch-too-large", prompt_chars=len(prompt))
            _write_state(run_path, state)
            continue
        if stop_clause is None:
            stop_clause = _spend_stop(spend_ledger, ledger, run_id, cap, total_cap, authorised)
        if stop_clause is not None:
            records[task_id] = dict(base, status="not_reviewed", reason=stop_clause)
            _write_state(run_path, state)
            continue
        att = paired_spend.reserve(
            ledger, run_id, task_id, REVIEW_CELL, cap,
            invocation=REVIEW_INVOCATION, gate_id=REVIEW_GATE_ID, arm=REVIEW_ARM,
        )
        base["attempt_id"] = att
        base["prompt_sha256"] = _sha256_bytes(prompt.encode("utf-8"))
        records[task_id] = dict(base, status="pending")
        _write_state(run_path, state)
        try:
            reply = reviewer.review(prompt, model=model, max_budget_usd=cap, timeout_s=timeout_s)
        except Exception as exc:  # noqa: BLE001 - a reviewer crash is an error row, not a run crash
            reply = ReviewReply("", None, f"reviewer raised: {exc}")
        cost = reply.cost_usd
        rec = dict(base, cost_usd=cost)
        if reply.error:
            rec.update(status="error", error=_clip(str(reply.error), 500))
        else:
            parsed, why = parse_reply(reply.text)
            if parsed is None:
                rec.update(status="invalid", reason=why, raw=_clip(reply.text or "", RAW_LIMIT))
            else:
                rec.update(status="reviewed", reply=parsed)
        records[task_id] = rec
        _write_state(run_path, state)
        paired_spend.settle(
            ledger, att, run_id, task_id, REVIEW_CELL, cap, cost,
            invocation=REVIEW_INVOCATION, gate_id=REVIEW_GATE_ID, arm=REVIEW_ARM,
        )
    if stop_clause is not None:
        print(f"review stopped: {stop_clause}", file=sys.stderr)
        return 3, state
    if any(r.get("status") == "error" for r in records.values()):
        return 1, state
    return 0, state


def _spend_stop(spend_ledger, ledger, run_id, cap, total_cap, authorised) -> Optional[str]:
    """The first failing spend clause for one more review, or None."""
    try:
        reviews = spend_ledger.recorded_total(Path(ledger), run_id, invocation=REVIEW_INVOCATION)
        if reviews + cap > total_cap:
            return f"review-total: {reviews:.2f} + {cap:.2f} exceeds the ${total_cap:.2f} review total cap"
        run_total = spend_ledger.recorded_total(Path(ledger), run_id)
        if run_total + cap > authorised:
            return f"run-ceiling: run total {run_total:.2f} + {cap:.2f} exceeds the ${authorised:.2f} authorised ceiling"
        ok, message = spend_ledger.precheck(Path(ledger), [cap], authorised)
        if not ok:
            return f"ledger-precheck: {message}"
    except Exception as exc:  # noqa: BLE001 - an unreadable ledger must stop spending
        return f"ledger-unreadable: {exc}"
    return None


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

REPORT_STATUSES = ("reviewed", "invalid", "skipped", "ineligible", "not_reviewed", "error", "pending")


def _arm_ratings(rec: dict) -> Optional[dict]:
    reply = rec.get("reply")
    if not isinstance(reply, dict):
        return None
    quoin_side, plain_side = ("a", "b") if rec.get("quoin_is_a") else ("b", "a")
    return {
        arm: {d: reply.get(f"{d}_{side}") for d in DIMENSIONS}
        for arm, side in (("quoin", quoin_side), ("plain", plain_side))
    }


def _arm_preference(rec: dict) -> Optional[str]:
    reply = rec.get("reply")
    if not isinstance(reply, dict):
        return None
    pref = reply.get("preference")
    if pref == "tie":
        return "tie"
    quoin_label = "A" if rec.get("quoin_is_a") else "B"
    return "quoin" if pref == quoin_label else "plain"


def _review_spend(run_path: Path):
    """(spend_usd or None, note or None); never raises."""
    from quoin.benchmarks.harness.tiered_report import read_manifest
    from quoin.benchmarks.scripts import spend_ledger

    manifest = read_manifest(run_path)
    ledger = manifest.get("spend_ledger")
    if not ledger:
        return None, "the run manifest records no spend ledger"
    run_id = manifest.get("run_id") or Path(run_path).name
    try:
        if not Path(ledger).is_file():
            return None, f"spend ledger not found: {ledger}"
        return spend_ledger.recorded_total(Path(ledger), run_id, invocation=REVIEW_INVOCATION), None
    except Exception as exc:  # noqa: BLE001 - the report is still built without a spend figure
        return None, f"spend ledger unreadable: {exc}"[:200]


def build_review_report(run_path, suite) -> Optional[dict]:
    """The pairwise-review section's data, or None when no review file exists."""
    state = load_state(run_path)
    if state is None:
        return None
    records = state.get("tasks") or {}
    tier_of = {t["id"]: (t.get("tier") or "unknown") for t in suite.get("tasks", []) if isinstance(t, dict) and t.get("id")}
    tier_order = []
    for tier in tier_of.values():
        if tier not in tier_order:
            tier_order.append(tier)

    def empty_counts():
        return {
            "quoin_preferred": 0, "plain_preferred": 0, "tie": 0, "invalid": 0, "skipped": 0,
            "ineligible": 0, "not_reviewed": 0, "error": 0, "pending": 0,
        }

    overall = empty_counts()
    by_tier = {tier: empty_counts() for tier in tier_order}
    position = {"A_preferred": 0, "B_preferred": 0, "tie": 0}
    per_task = []
    for task_id, tier in tier_of.items():
        rec = records.get(task_id)
        if rec is None:
            continue
        status = rec.get("status")
        pref = _arm_preference(rec) if status == "reviewed" else None
        key = {"quoin": "quoin_preferred", "plain": "plain_preferred", "tie": "tie"}.get(pref, status)
        for counts in (overall, by_tier[tier]):
            if key in counts:
                counts[key] += 1
        if status == "reviewed":
            raw_pref = rec["reply"]["preference"]
            position["A_preferred" if raw_pref == "A" else "B_preferred" if raw_pref == "B" else "tie"] += 1
        per_task.append({
            "task_id": task_id, "tier": tier, "status": status, "reason": rec.get("reason"),
            "preference": pref, "ratings": _arm_ratings(rec), "quoin_is_a": rec.get("quoin_is_a"),
            "rationale_truncated": bool((rec.get("reply") or {}).get("rationale_truncated")),
        })
    spend, note = _review_spend(Path(run_path))
    report = {
        "seed": state.get("seed"), "model": state.get("model"), "effort": state.get("effort"),
        "prompt_version": state.get("prompt_version"), "caps": state.get("caps"),
        "review_spend_usd": spend, "counts": overall, "counts_by_tier": by_tier,
        "position": position, "tasks": per_task,
    }
    if note:
        report["review_spend_note"] = note
    return report


def render_review_markdown(report: dict, format_table) -> str:
    def table_text(rows):
        return format_table([[str(cell).replace("|", "\\|") for cell in row] for row in rows])

    lines = ["", "## Blind pairwise review (secondary measure)", ""]
    lines.append(
        "A reviewer saw the issue text and both patches in a seeded A/B order with the arm "
        "hidden. This review never affects `resolved`; it is reported separately and read "
        "as supporting evidence only. Replies that do not fit the schema are recorded as "
        "invalid; invalid replies are final and are not retried."
    )
    lines.append("")
    spend = report.get("review_spend_usd")
    spend_text = "n/a" if spend is None else "$%.2f" % spend
    if report.get("review_spend_note"):
        spend_text += " (%s)" % report["review_spend_note"]
    caps = report.get("caps") or {}
    lines.append(
        "Seed %s, model %s, effort %s, prompt %s. Review spend: %s. Caps: %s per review, %s in total."
        % (report.get("seed"), report.get("model"), report.get("effort"), report.get("prompt_version"),
           spend_text, caps.get("per_review_usd"), caps.get("total_usd"))
    )
    lines.append("")
    lines.append("### Preference counts")
    lines.append("")
    header = ["Tier", "Quoin preferred", "Plain preferred", "Tie", "Invalid", "Skipped", "Ineligible",
              "Not reviewed", "Error"]
    table = [header, ["---"] * len(header)]
    for label, c in [*report["counts_by_tier"].items(), ("overall", report["counts"])]:
        table.append([label, c["quoin_preferred"], c["plain_preferred"], c["tie"], c["invalid"],
                      c["skipped"], c["ineligible"], c["not_reviewed"], c["error"]])
    lines.append(table_text(table))
    lines.append("")
    pos = report["position"]
    lines.append(
        "Position check: patch A was preferred %d times, patch B %d times, ties %d; a large "
        "imbalance suggests position bias." % (pos["A_preferred"], pos["B_preferred"], pos["tie"])
    )
    lines.append("")
    lines.append("### Ratings per task")
    lines.append("")
    lines.append(
        "Correctness risk: 1 is lowest risk, 5 is highest. Minimality: 1 is bloated, 5 is minimal. "
        "Test quality: 1 is poor or absent, 5 is thorough."
    )
    lines.append("")
    header = ["Task", "Tier", "Status", "Preferred", "Quoin risk/min/test", "Plain risk/min/test"]
    table = [header, ["---"] * len(header)]
    for t in report["tasks"]:
        r = t.get("ratings")

        def fmt(arm):
            return "n/a" if not r else "/".join(str(r[arm][d]) for d in DIMENSIONS)

        table.append([t["task_id"], t["tier"], t["status"] + (f" ({t['reason']})" if t.get("reason") else ""),
                      t.get("preference") or "n/a", fmt("quoin"), fmt("plain")])
    lines.append(table_text(table))
    lines.append("")
    return "\n".join(lines)
