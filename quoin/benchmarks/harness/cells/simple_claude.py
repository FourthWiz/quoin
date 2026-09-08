"""
simple_claude.py — Cell adapter for the 'simple-claude' benchmark cell.

Invokes Claude Code CLI in non-interactive mode inside an isolated git worktree
of the fixture repo. No Quoin workflow artifacts; pure Claude Code baseline.

Model snapshot pinning:
    Starting with the 4.6 generation, every Claude model ID is itself a
    pinned snapshot — including the dateless form (verified against
    Anthropic's docs, platform.claude.com/docs/en/models/opus-4-7/overview
    and .../sonnet-4-6/overview: "Every Claude model ID is a pinned
    snapshot, including the dateless IDs used from the 4.6 generation on").
    There is no separate dated snapshot to discover for these models — the
    bare ID below IS the permanent pin, confirmed by a live probe
    (`claude --print --output-format json --model claude-opus-4-7 ...`
    returns `canonicalModel: "claude-opus-4-7"` with no date suffix
    anywhere in the response, and the installed CLI binary's own static
    model table has no dated string for this alias either). No further
    pinning action is possible or needed for this model generation.

Rate-limit handling:
    The adapter catches 429 / overloaded responses from the Claude API and
    retries with exponential backoff. Waited time does NOT count against the
    per-task wall-clock budget (the budget timer pauses during backoff sleeps).
"""

from __future__ import annotations

import io
import json
import os
import select
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

from ..config import BudgetSpec
from ..cost import estimate_cost, load_pricing

# ---------------------------------------------------------------------------
# This dateless ID IS the permanent pin (D-11) for 4.6+-generation models —
# see the module docstring. Verified 2026-09-06 against a live probe
# (canonicalModel: "claude-opus-4-7", no date suffix anywhere in the
# response) and against Anthropic's docs, which state this explicitly as
# policy. There is no dated snapshot to swap in later.
# ---------------------------------------------------------------------------
PINNED_MODEL: str = "claude-opus-4-7"

# Overridable via environment for testing
_MODEL_ENV_VAR = "QUOIN_BENCH_CLAUDE_MODEL"


def _get_model() -> str:
    return os.environ.get(_MODEL_ENV_VAR, PINNED_MODEL)


# Environment variable that flags a gate invocation (D-08). Read from the
# environment, deliberately NOT from a threaded kwarg — a kwarg would carry
# the same silent-omission failure mode this flag exists to guard against.
ENV_BENCHMARK_GATE = "QUOIN_BENCHMARK_GATE"


def _gate_mode() -> bool:
    return os.environ.get(ENV_BENCHMARK_GATE) == "1"


# Verified against CLI 2.1.261 (D-14): the result event carries
# `total_cost_usd`; a bare `cost_usd` key does not appear anywhere in the
# binary. Read the real field first, with the legacy name as a fallback so
# this stays compatible with both older and newer CLI builds.
def _extract_cost_usd(event: dict) -> Optional[float]:
    cost_val = event.get("total_cost_usd")
    if cost_val is None:
        cost_val = event.get("cost_usd")
    if cost_val is None:
        return None
    return float(cost_val)


# Strings the CLI itself emits when `--max-budget-usd` halts a session
# (verified against CLI 2.1.261, D-08). Detected on combined stdout-event
# text and captured stderr, since neither the exact event `type` nor the
# exact stream carrying the message is documented — a substring match on
# the CLI's own enforcement strings is what fires without over-fitting to
# either channel.
_BUDGET_HALT_MARKERS = (
    "Reached maximum budget ($",
    "Session cost is not a number",
)


def _detect_budget_halt(text: str) -> bool:
    return any(marker in text for marker in _BUDGET_HALT_MARKERS)


# How often the wall-clock check re-evaluates while stdout is silent
# (below), and how many stream-json events a session retains in memory.
_POLL_INTERVAL_SECONDS = 1.0
_MAX_RETAINED_EVENTS = 2000

# How many stderr LINES the drain thread keeps as a diagnostic tail. Not
# a detection mechanism — the budget-halt marker is checked per-line as it
# arrives (see `_drain_stream`), so this bound only limits how much of a
# runaway stderr stream stays resident in memory.
_MAX_RETAINED_STDERR_LINES = 500


def _wait_readable(stream, timeout: float) -> bool:
    """True once `stream` has a line ready, `timeout` elapses, or `stream`
    cannot be used with `select()` at all (no real fd — a test double, for
    instance) — in the last case this returns True immediately so the
    caller falls back to a plain blocking `readline()`, matching pre-fix
    behaviour for anything that isn't a real OS pipe.

    Makes the wall-clock check in `invoke()` below timer-driven rather
    than event-driven: without this, `readline()` on a silent stdout
    blocks indefinitely, so the budget check above it in the loop is only
    re-evaluated when a stdout line actually arrives — and none ever will
    if the child is itself blocked writing a full stderr pipe (the
    deadlock this fix, together with `_drain_stream`, closes).
    """
    if stream is None:
        return True
    try:
        fd = stream.fileno()
    except (AttributeError, ValueError, OSError, io.UnsupportedOperation):
        return True
    try:
        ready, _, _ = select.select([fd], [], [], max(timeout, 0.0))
    except (OSError, ValueError):
        return True
    return bool(ready)


def _read_ready_chunk(fd: int) -> bytes:
    """A single read from `fd`, called only after `select()` has already
    reported it readable.

    For a pipe, `os.read()` then returns whatever bytes are CURRENTLY
    available (or `b""` at EOF) without waiting for a complete line —
    unlike `TextIOWrapper.readline()`, which loops internally until it
    sees a newline or EOF and can block on a partial line the child has
    written but not yet completed. That gap is what let a child holding
    stdout open past its wall-clock budget (a partial line written, then
    the child sleeping) delay `invoke()`'s return until the child exited
    on its own, even though `select()` had already woken this loop.
    """
    try:
        return os.read(fd, 65536)
    except (OSError, ValueError):
        return b""


def _split_ready_lines(buffer: str) -> tuple[list[str], str]:
    """Split `buffer` on newlines, returning `(complete_lines, remainder)`
    — `remainder` is the trailing partial line (or `""` when `buffer`
    ended exactly on a newline), to be prefixed onto the next chunk."""
    parts = buffer.split("\n")
    remainder = parts.pop()
    return parts, remainder


def _drain_stream(stream, buffer, halt_event: Optional["threading.Event"] = None) -> None:
    """Continuously read `stream` into `buffer` on a background thread.

    Started right after `Popen` so the OS pipe buffer backing `stderr`
    never fills: an unread stderr pipe blocks the child's own `write(2)`
    call once its buffer (commonly 64 KB) is full, which then blocks this
    process's `stdout` reads forever too — both pipes lead to the same
    child. Runs until EOF or the stream closes out from under it (the
    main thread killing/reaping the process).

    `buffer` may be a plain list or a bounded `deque` — either way this
    only ever calls `.append()`, so a caller bounding memory via
    `deque(maxlen=...)` needs no change here. When `halt_event` is given,
    each line is checked for a budget-halt marker (D-08) AS IT ARRIVES and
    `halt_event.set()` fires on the first match — this is what lets a
    caller bound `buffer`'s size without losing the ability to detect a
    marker that has since scrolled out of a capped tail.
    """
    if stream is None:
        return
    try:
        for line in iter(stream.readline, ""):
            buffer.append(line)
            if halt_event is not None and not halt_event.is_set() and _detect_budget_halt(line):
                halt_event.set()
    except Exception:
        # A background thread — nothing upstream is positioned to handle
        # an exception here, and a stream that misbehaves (closed out
        # from under us, or a test double with an unexpected shape) must
        # never crash it. Whatever was drained before the failure is kept.
        pass


def _build_claude_argv(prompt: str, model: str, max_budget_usd: Optional[float]) -> list[str]:
    """Assemble the `claude` argv shared by both Claude cells.

    A separate, importable function so the "the assembled argv actually
    contains --max-budget-usd" refusal (D-08's CRIT-2 fix) can be tested
    against a stubbed builder that silently drops the flag — the
    anti-silent-omission proof. Both cells already spawn with `--print`,
    which `--max-budget-usd` requires.
    """
    argv = [
        "claude",
        "--print",
        "--output-format", "stream-json",
        "--verbose",
        "--permission-mode", "acceptEdits",
        "--model", model,
    ]
    if max_budget_usd is not None:
        argv += ["--max-budget-usd", str(max_budget_usd)]
    argv.append(prompt)
    return argv


def _build_prompt(task_spec: dict) -> str:
    """Build the prompt to send to Claude Code for a given task."""
    source = task_spec.get("source", "")
    source_id = task_spec.get("source_id", "")
    description = task_spec.get("description", "")

    if source == "evalplus_humaneval_plus":
        return (
            f"Solve the following HumanEval+ programming task. "
            f"Write your solution as a Python function in a file called solution.py.\n\n"
            f"Task ID: {source_id}\n"
            f"Task description: {description}\n\n"
            f"Your solution should pass all tests in the evalplus test suite for this task."
        )
    elif source == "swebench_lite":
        return (
            f"Fix the following GitHub issue from the SWE-bench Lite benchmark.\n\n"
            f"Instance ID: {source_id}\n"
            f"Description: {description}\n\n"
            f"Implement the fix in the repository. When done, your changes will be "
            f"evaluated by the SWE-bench harness."
        )
    elif source == "quoin_scenario":
        # The suite entry's `description` is the scenario's Prompt verbatim
        # plus the fixture's concretely-named target subsystem (T-06, D-05).
        # `scenario_file` is a FALLBACK only, read relative to
        # quoin/benchmarks/ — the scenario doc itself names no subsystem
        # (round-4 fix, MAJ-1: the file must not win when both are present,
        # or every arm would plan a refactor of whatever subsystem it
        # picked, confounding the quality comparison).
        target_subsystem = task_spec.get("target_subsystem", "")
        if description:
            prompt = description
        else:
            scenario_file = task_spec.get("scenario_file", "")
            scenario_path = Path("quoin/benchmarks") / scenario_file if scenario_file else None
            prompt = (
                scenario_path.read_text(encoding="utf-8")
                if scenario_path and scenario_path.exists()
                else ""
            )
        if target_subsystem and target_subsystem not in prompt:
            raise ValueError(
                f"assembled prompt for task {task_spec.get('id')!r} does not "
                f"contain its target_subsystem {target_subsystem!r} — the "
                "quality comparison would be across different subsystems"
            )
        return prompt
    else:
        return f"Solve task: {description} (source_id={source_id})"


def invoke(
    task_spec: dict,
    workdir: Path,
    budget: BudgetSpec,
    run_id: str,
    max_budget_usd: Optional[float] = None,
    run_output_dir: Optional[Path] = None,
) -> dict:
    """
    Invoke Claude Code CLI in simple (no-quoin) mode.

    Parameters
    ----------
    task_spec:
        Task dict from suite-v1.json.
    workdir:
        Isolated git worktree path for this task.
    budget:
        Wall-clock and USD budget constraints.
    run_id:
        The benchmark run ID (used for logging).
    max_budget_usd:
        Optional CLI-enforced spend cap (D-08), passed through to `claude
        --max-budget-usd`. In gate mode (`QUOIN_BENCHMARK_GATE=1`), a `None`
        value or an assembled argv missing the flag refuses to spawn.
    run_output_dir:
        This task's own result directory. When given, every stream-json
        event is streamed to `run_output_dir/transcript.jsonl` as it
        arrives — the FULL transcript, independent of the in-memory ring
        buffer's cap. When omitted, no file is written here and the
        caller (`result_writer.write_run_result`) writes the (possibly
        truncated) ring-buffer contents instead, as before.

    Returns
    -------
    dict with transcript_events, prompt, diff_patch, cost data, etc.
    """
    model = _get_model()
    prompt = _build_prompt(task_spec)
    task_id = task_spec["id"]

    result: dict = {
        "prompt": prompt,
        "transcript_events": [],
        "diff_patch": "",
        "cost_available": False,
        "cost_runtime_usd": None,
        "cost_estimated_usd": None,
        "cost_delta_usd": None,
        "tokens_in": None,
        "tokens_out": None,
        "tokens_cache_read": None,
        "tokens_cache_write": None,
        "turn_count": 0,
        "gate_intervention_count": 0,
        "verdict": None,
        "extra": {},
    }

    cmd = _build_claude_argv(prompt, model, max_budget_usd)

    # Fail closed at the point of spend (D-08's CRIT-2 fix). A cap that is
    # merely threaded is not a bound: this refusal is evaluated on the
    # ASSEMBLED argv, not the raw parameter, so a silently-dropped flag is
    # caught too. Outside gate mode the record is advisory only.
    gate_mode = _gate_mode()
    budget_cap_armed = max_budget_usd is not None and "--max-budget-usd" in cmd
    result["extra"]["budget_cap_armed"] = budget_cap_armed
    result["extra"]["max_budget_usd_applied"] = max_budget_usd
    if gate_mode and not budget_cap_armed:
        result["verdict"] = "error"
        result["extra"]["failure_reason"] = "budget-cap-unarmed"
        return result

    budget_seconds = budget.wall_clock_seconds
    wall_start = time.monotonic()
    # Time spent in rate-limit backoff, excluded from the wall-clock budget —
    # but only up to one budget's worth (see `min(backoff_total,
    # budget_seconds)` below). A chunk carrying many 429 events would
    # otherwise let the retry_delay sum grow without any bound at all, since
    # a fully-excluded backoff never shrinks `remaining`; capping it at
    # `budget_seconds` means a session can spend at most ~2x its budget
    # (once on real work, once amortised on backoff) before it's killed.
    backoff_total = 0.0

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(workdir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        # Ring buffer, not an unbounded list: peak retained memory is
        # bounded regardless of session length. A `--verbose` stream-json
        # session running the full wall clock can emit far more than this
        # many events, and nothing downstream needs the full history in
        # MEMORY — `turn_count` and the budget-halt check are both
        # maintained incrementally below, not recomputed from a full
        # concatenation at the end. The full ORDERED event stream, if
        # anyone needs it, is what `transcript_file` below writes.
        events: deque = deque(maxlen=_MAX_RETAINED_EVENTS)
        events_seen = 0
        total_cost_usd: Optional[float] = None
        tokens_in = tokens_out = tokens_cache_read = tokens_cache_write = 0
        turn_count = 0
        session_errored = False
        retry_delay = 1.0
        budget_halted = False

        # Stream every retained event to transcript.jsonl AS IT ARRIVES,
        # when the caller gave us this task's own result directory — the
        # file on disk is then the FULL transcript regardless of the ring
        # buffer's cap above, closing the silent-truncation gap: nothing
        # previously recorded that the ring buffer had dropped anything,
        # and the file was only ever written from the (possibly
        # truncated) buffer after the fact.
        transcript_file = None
        if run_output_dir is not None:
            try:
                run_output_dir.mkdir(parents=True, exist_ok=True)
                transcript_file = open(run_output_dir / "transcript.jsonl", "w", encoding="utf-8")
            except OSError:
                transcript_file = None
        # Set at OPEN time, not after the loop: an exception raised anywhere
        # below (a JSON error, a killed-process TimeoutExpired, the git-diff
        # subprocess) must still leave `extra["transcript_streamed"]`
        # correctly True whenever a file was actually opened, so a generic
        # exception handler downstream (`runner.py`) can tell a
        # partially-streamed transcript apart from an empty one and never
        # clobber it with the (possibly-empty) ring buffer.
        result["extra"]["transcript_streamed"] = transcript_file is not None

        # Drain stderr on a daemon thread started at spawn: a session
        # that writes past one pipe buffer's worth of stderr over a long
        # run would otherwise block in the child's own write(2), which
        # stops it emitting stdout too — and the wall-clock check below
        # never gets a chance to fire because it sits above a blocking
        # `readline()` that then never returns. The retained buffer is a
        # bounded diagnostic tail (`deque(maxlen=...)`), not an unbounded
        # accumulator — a session emitting tens of MB of stderr must not
        # grow this process's memory by a matching amount. The budget-halt
        # marker is checked per-line, incrementally, as each line is
        # drained (via `stderr_halt_event`), so bounding the buffer never
        # costs detection accuracy.
        stderr_buffer: deque = deque(maxlen=_MAX_RETAINED_STDERR_LINES)
        stderr_halt_event = threading.Event()
        stderr_thread = threading.Thread(
            target=_drain_stream, args=(proc.stderr, stderr_buffer, stderr_halt_event), daemon=True,
        )
        stderr_thread.start()

        # A real OS pipe has a usable fd — that's the case this fix
        # targets. A test double or anything else without one (matching
        # `_wait_readable`'s own fallback) keeps the pre-fix line-buffered
        # read, since there is no partial-line-blocking hazard to close
        # for something that was never a real pipe in the first place.
        try:
            stdout_fd: Optional[int] = proc.stdout.fileno()
        except (AttributeError, ValueError, OSError, io.UnsupportedOperation):
            stdout_fd = None

        def _handle_stream_line(raw_line: str) -> str:
            """Parse and record one stream-json line. Returns "rate_limited"
            when the line is a 429/overloaded event the caller must back off
            on, "skip" for a blank or unparseable line, "ok" otherwise."""
            nonlocal total_cost_usd, tokens_in, tokens_out, tokens_cache_read
            nonlocal tokens_cache_write, turn_count, session_errored, budget_halted
            nonlocal events_seen

            line = raw_line.strip()
            if not line:
                return "skip"

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                return "skip"

            # Handle rate-limit / overloaded events
            event_type = event.get("type", "")
            if event_type in ("error", "api_error"):
                error_msg = str(event.get("error", ""))
                if "429" in error_msg or "overloaded" in error_msg.lower():
                    return "rate_limited"

            events.append(event)
            events_seen += 1
            if transcript_file is not None:
                transcript_file.write(json.dumps(event, ensure_ascii=False) + "\n")
                transcript_file.flush()
            if not budget_halted and _detect_budget_halt(
                str(event.get("result", "")) + str(event.get("error", ""))
            ):
                budget_halted = True

            # Extract cost and token data from stream-json events
            if event_type == "result":
                cost_val = _extract_cost_usd(event)
                if cost_val is not None:
                    total_cost_usd = cost_val
                usage = event.get("usage", {})
                tokens_in = usage.get("input_tokens", tokens_in)
                tokens_out = usage.get("output_tokens", tokens_out)
                tokens_cache_read = usage.get("cache_read_input_tokens", tokens_cache_read)
                tokens_cache_write = usage.get("cache_creation_input_tokens", tokens_cache_write)
                # The terminal result event is authoritative on whether the
                # session actually completed (verified 2026-09-06: an
                # authentication failure still emits a `type: "assistant"`
                # event carrying the error text, e.g. "Not logged in ·
                # Please run /login" — turn_count alone cannot distinguish
                # that from a real completion, per the D-15 rehearsal
                # finding that isolated CLAUDE_CONFIG_DIR loses auth).
                if event.get("is_error"):
                    session_errored = True

            if event_type == "assistant":
                turn_count += 1

            return "ok"

        try:
            read_buffer = ""
            try:
                timed_out = False
                while True:
                    # Timer-driven, not event-driven: re-evaluated at least
                    # every _POLL_INTERVAL_SECONDS even when stdout is
                    # silent, via the bounded select() below — not only when
                    # a stdout line happens to arrive.
                    elapsed = time.monotonic() - wall_start - min(backoff_total, budget_seconds)
                    remaining = budget_seconds - elapsed
                    if remaining <= 0:
                        timed_out = True
                        break

                    if not _wait_readable(proc.stdout, min(_POLL_INTERVAL_SECONDS, remaining)):
                        continue

                    if stdout_fd is not None:
                        # A single read of whatever is CURRENTLY available —
                        # never `readline()`, which can block on a partial
                        # line the child has written but not yet completed
                        # even though `select()` already reported the fd
                        # readable (see `_read_ready_chunk`'s docstring).
                        chunk = _read_ready_chunk(stdout_fd)
                        if not chunk:
                            if proc.poll() is not None:
                                break
                            # An EOF'd fd is reported readable by select()
                            # FOREVER, so `os.read` returning b"" here with
                            # the child still alive would otherwise spin
                            # this loop at ~100% CPU with no sleep at all
                            # (round-4 fix: MAJOR 14 — measured 99% parent
                            # CPU for the remainder of the wall clock).
                            # Stop selecting on stdout for one interval and
                            # let the outer loop's own wall-clock check
                            # bound how long this can repeat.
                            time.sleep(min(_POLL_INTERVAL_SECONDS, max(remaining, 0.0)))
                            continue
                        read_buffer += chunk.decode("utf-8", errors="replace")
                        ready_lines, read_buffer = _split_ready_lines(read_buffer)
                    else:
                        line = proc.stdout.readline()
                        if not line and proc.poll() is not None:
                            break
                        ready_lines = [line] if line else []

                    for raw_line in ready_lines:
                        # Re-evaluated per LINE, not just once per chunk: a
                        # single 64 KB chunk can carry many rate-limit
                        # events, and a bare `time.sleep(retry_delay)` per
                        # event previously ran with no wall-clock check
                        # between them — a chunk full of 429s could burn
                        # minutes past the budget before the outer loop ever
                        # got a turn (round-3 CRITICAL regression: measured
                        # 426s against a 3s budget).
                        elapsed = time.monotonic() - wall_start - min(backoff_total, budget_seconds)
                        remaining = budget_seconds - elapsed
                        if remaining <= 0:
                            timed_out = True
                            break

                        outcome = _handle_stream_line(raw_line)
                        if outcome == "rate_limited":
                            # Exponential backoff; pause does not count
                            # against budget, and is itself capped by
                            # whatever budget remains so a single backoff
                            # can never itself run past the wall clock.
                            backoff_start = time.monotonic()
                            time.sleep(min(retry_delay, remaining))
                            backoff_total += time.monotonic() - backoff_start
                            retry_delay = min(retry_delay * 2, 60.0)

                    if timed_out:
                        break

                # A final line with no trailing newline is never flushed
                # through `_split_ready_lines` — at EOF it sits in
                # `read_buffer` as the "remainder" the split machinery
                # hands back for the NEXT chunk, and there is no next
                # chunk. That silently dropped the terminal `result`
                # event, and with it the arm's cost, whenever the child's
                # last write wasn't newline-terminated. This must run
                # unconditionally, before branching on `timed_out` — a
                # session that hits the wall clock mid-stream still has a
                # buffered terminal event worth recovering, and an earlier
                # fix that only flushed on the clean-exit path left the
                # timeout branch dropping it exactly as before.
                if read_buffer.strip():
                    _handle_stream_line(read_buffer)
                    read_buffer = ""

                if timed_out:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    result["verdict"] = "timeout"
                else:
                    proc.wait(timeout=10)
            finally:
                # Guaranteed regardless of how the block above exits — a
                # raised TimeoutExpired, a JSON error, or any other escape
                # must not leave a paid `claude` process (or its stderr
                # drain thread) orphaned.
                if proc.poll() is None:
                    proc.kill()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        pass
                stderr_thread.join(timeout=5)
        finally:
            if transcript_file is not None:
                transcript_file.close()

        stderr_output = "".join(stderr_buffer)

        # A CLI-enforced budget halt (D-08) is reported as capped, not
        # silently short — this cell's own refusal above only catches an
        # UNARMED cap; this catches the cap actually firing mid-session.
        # `stderr_halt_event` is set incrementally as stderr lines arrive
        # (never from re-scanning the bounded tail below, which may have
        # already scrolled the marker out).
        if result["verdict"] is None:
            if budget_halted or stderr_halt_event.is_set():
                result["verdict"] = "budget_stopped"
                result["extra"]["failure_reason"] = "budget-halt-detected"

        # Get git diff from workdir
        try:
            diff_proc = subprocess.run(
                ["git", "diff", "HEAD"],
                cwd=str(workdir),
                capture_output=True,
                text=True,
                timeout=30,
            )
            result["diff_patch"] = diff_proc.stdout
        except Exception:
            result["diff_patch"] = ""

        # The ring buffer silently drops the OLDEST events once
        # `events_seen` exceeds its cap — record that it happened (rather
        # than let a truncated in-memory value look complete) and mark the
        # truncation inside the value itself, since not every caller reads
        # `extra`.
        transcript_events_dropped = max(0, events_seen - _MAX_RETAINED_EVENTS)
        result["extra"]["transcript_events_dropped"] = transcript_events_dropped
        result["extra"]["transcript_streamed"] = transcript_file is not None
        if transcript_events_dropped:
            events.appendleft({
                "type": "truncation_notice",
                "dropped_event_count": transcript_events_dropped,
            })

        result["transcript_events"] = list(events)
        result["turn_count"] = turn_count
        result["tokens_in"] = tokens_in if tokens_in else None
        result["tokens_out"] = tokens_out if tokens_out else None
        result["tokens_cache_read"] = tokens_cache_read if tokens_cache_read else None
        result["tokens_cache_write"] = tokens_cache_write if tokens_cache_write else None
        # For the scenario judge (T-06): whether the run produced at least
        # one GENUINE assistant turn — an errored terminal result (e.g. an
        # authentication failure) does not count, even though it still
        # emits an assistant-typed event. Named distinctly from the
        # top-level `turn_count` metrics key so merging `extra` into
        # metrics.json never collides with a base key (T-01).
        result["extra"]["had_assistant_event"] = turn_count > 0 and not session_errored
        result["extra"]["session_errored"] = session_errored

        # Cost handling
        if total_cost_usd is not None:
            result["cost_available"] = True
            result["cost_runtime_usd"] = total_cost_usd
            # Also compute estimated value for sanity check
            try:
                pricing = load_pricing()
                estimated = estimate_cost(
                    model=model,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    cache_write=tokens_cache_write,
                    cache_read=tokens_cache_read,
                    pricing=pricing,
                )
                if estimated is not None:
                    result["cost_estimated_usd"] = float(estimated)
                    result["cost_delta_usd"] = total_cost_usd - float(estimated)
            except Exception:
                pass  # Estimation failure is non-fatal
        else:
            result["cost_available"] = False

    except FileNotFoundError:
        # Claude CLI not found — mark as error
        result["verdict"] = "error"
        result["transcript_events"] = [
            {"type": "error", "error": "claude CLI not found on PATH"}
        ]
    except Exception as exc:
        result["verdict"] = "error"
        result["transcript_events"] = [{"type": "error", "error": str(exc)}]

    return result
