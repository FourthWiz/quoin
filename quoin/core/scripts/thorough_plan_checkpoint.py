#!/usr/bin/env python3
"""Phase-boundary checkpoint helper for /thorough_plan (IVG-98).

Writes a durable checkpoint file at each planning-loop phase boundary
(plan / critic / revise) so a killed /thorough_plan session can be resumed
by re-invoking `/thorough_plan {task}` in a fresh session.

Behaviour
---------
1. Resolve memory dir: `{project-root}/.workflow_artifacts/memory`; mkdir -p checkpoints/.
2. Compose stage token: `thorough-plan:round-{round}-{phase}`.
3. Write (or update) the ORCHESTRATOR-DEDICATED session-state file FIRST (older mtime).
4. Write (overwrite) the checkpoint file SECOND (newer mtime) — M-01 write-order invariant.
5. Write pending-restore sentinel LAST (only when --sid is non-empty and not "unknown").
6. Print checkpoint path to stdout; exit 0 always (fail-OPEN — never non-zero).

Design decisions: D-01..D-07 in the task plan (ivg-98-phase-boundary-checkpoint).

Public API
----------
  main(argv=None) -> int  (CLI entry point)

Exit codes
----------
  0 — always (fail-OPEN design)
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Next-phase logic
# ---------------------------------------------------------------------------

_NEXT_PHASE = {
    "plan": "critic",
    "critic": "revise",
    "revise": "critic",  # critic for next round (or convergence check)
}

VALID_PHASES = frozenset(_NEXT_PHASE.keys())


def _next_phase(phase: str) -> str:
    return _NEXT_PHASE.get(phase, "unknown")


# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------

def _atomic_write(path: Path, content: str) -> None:
    """Write content to path atomically via a .tmp sibling file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.rename(path)


def _update_session_state(
    ss_path: Path,
    stage: str,
    task: str,
    branch: str,
    sid: str,
) -> None:
    """Create or update the orchestrator-dedicated session-state file.

    Uses atomic write (.tmp + rename) to avoid partial reads.
    If file already exists: rewrites `## Current stage` line only; all other
    blocks are preserved (atomic full-file rewrite, not in-place line edit).
    If file does NOT exist: creates a minimal template (M-02 creation-if-absent).
    """
    if ss_path.exists():
        text = ss_path.read_text(encoding="utf-8")
        lines = text.splitlines(keepends=True)
        out = []
        skip_next = False
        for line in lines:
            if skip_next:
                out.append(f"{stage}\n")
                skip_next = False
                continue
            if line.rstrip() == "## Current stage":
                out.append(line)
                skip_next = True
            else:
                out.append(line)
        _atomic_write(ss_path, "".join(out))
    else:
        # M-02: create the file with a minimal template
        template = (
            f"## Active task: {task}\n\n"
            f"## Current stage\n{stage}\n\n"
            f"## Branch\n{branch}\n\n"
            f"## Session ID\n{sid}\n\n"
            f"## Status\nin_progress\n\n"
            f"## Cost\n"
            f"- Session UUID: {sid}\n"
            f"- Phase: thorough-plan\n"
            f"- Recorded in cost ledger: no\n"
            f"- end_of_day_due: yes\n"
            f"- fallback_fires: 0\n"
            f"- verification_ran: no\n"
            f"- verification_mismatches: 0\n"
        )
        _atomic_write(ss_path, template)


def _write_checkpoint(
    checkpoints_dir: Path,
    sid: str,
    stage: str,
    task: str,
    branch: str,
    phase: str,
    plan_path: str,
    critic_path: str,
    ss_path_str: str,
) -> Path:
    """Write (overwrite) the phase-boundary checkpoint file.

    Fixed filename: `thorough-plan-progress-{sid}.md`
    (D-03: one file per session, overwritten at each boundary).
    """
    fname = f"thorough-plan-progress-{sid}.md"
    ckpt_path = checkpoints_dir / fname

    next_ph = _next_phase(phase)
    saved_ts = datetime.now(tz=timezone.utc).isoformat()

    # C-01: ## Last user intent MUST be present and non-empty
    last_user_intent = (
        f"thorough_plan {task}: last completed boundary {stage}; "
        f"next phase to run: {next_ph}. "
        f"Re-invoke /thorough_plan {task} and resume at {next_ph}."
    )

    restore_hint = (
        f"Re-invoke /thorough_plan {task} in a fresh session; "
        f"last completed boundary: {stage}; next phase: {next_ph}."
    )

    content = (
        f"## Status\nphase-boundary checkpoint\n\n"
        f"## Current stage\n{stage}\n\n"
        f"## Active task\n{task}\n\n"
        f"## Branch\n{branch}\n\n"
        f"## Session ID\n{sid}\n\n"
        f"## Session link\n"
        + (
            f"Resume: (see ${{CLAUDE_PLUGIN_ROOT}}/scripts/get_session_uuid.py)\n"
            if sid and sid != "unknown"
            else "Resume: (session UUID unavailable)\n"
        )
        + f"\n## Last user intent\n{last_user_intent}\n\n"
        f"## Saved\n{saved_ts}\n\n"
        f"## In-flight artifacts\n"
        f"- current-plan.md: {plan_path or '(none found)'}\n"
        f"- critic-response: {critic_path or '(none found)'}\n"
        f"- session-state: {ss_path_str or '(none found)'}\n\n"
        f"## Open questions\n(none)\n\n"
        f"## Decisions made\n(none — see task plan)\n\n"
        f"## Unfinished work\n(none — in progress; resume at {next_ph})\n\n"
        f"## Restore hint\n{restore_hint}\n"
    )

    _atomic_write(ckpt_path, content)
    return ckpt_path


def _write_sentinel(mem_dir: Path, sid: str, ckpt_path: Path) -> None:
    """Write pending-restore-{sid}.txt sentinel (last, newer mtime).

    Skipped when sid is empty or "unknown" (empty-SID orphan guard, C-02).
    """
    if not sid or sid == "unknown":
        print(
            "[thorough_plan_checkpoint] WARNING: session UUID empty/unknown; "
            "refusing to write pending-restore sentinel (would create orphan). "
            "Checkpoint file kept; use T-04 direct scan to resume.",
            file=sys.stderr,
        )
        return
    sentinel_path = mem_dir / f"pending-restore-{sid}.txt"
    _atomic_write(sentinel_path, str(ckpt_path) + "\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Always exits 0 (fail-OPEN)."""
    parser = argparse.ArgumentParser(
        description=(
            "Write a phase-boundary checkpoint for /thorough_plan (IVG-98). "
            "Always exits 0."
        ),
        add_help=True,
    )
    parser.add_argument("--project-root", required=True, metavar="PATH",
                        help="Absolute path to the project root.")
    parser.add_argument("--task", required=True, metavar="NAME",
                        help="Task name (kebab-case).")
    parser.add_argument("--round", required=True, type=int, metavar="N",
                        dest="round_n",
                        help="Current round number (1-based).")
    parser.add_argument("--phase", required=True,
                        choices=list(VALID_PHASES),
                        help="Last completed phase: plan, critic, or revise.")
    parser.add_argument("--sid", default="unknown", metavar="UUID",
                        help="Session UUID (may be 'unknown' or empty).")
    parser.add_argument("--branch", default="unknown", metavar="NAME",
                        help="Current git branch name.")
    parser.add_argument("--plan-path", default="", metavar="PATH",
                        help="Path to current-plan.md (optional).")
    parser.add_argument("--critic-path", default="", metavar="PATH",
                        help="Path to latest critic-response-N.md (optional).")
    parser.add_argument("--session-state", default="", metavar="PATH",
                        dest="session_state",
                        help=(
                            "Path to the ORCHESTRATOR-DEDICATED session-state file "
                            "({date}-{task}-orchestrator.md). Written FIRST (older mtime) "
                            "before the checkpoint file."
                        ))

    try:
        args = parser.parse_args(argv)
    except SystemExit:
        # Even on arg-parse errors, exit 0 (fail-OPEN)
        return 0

    try:
        # Normalise SID: treat empty string as "unknown"
        sid = args.sid.strip() if args.sid else "unknown"

        # 1. Resolve memory dir + checkpoints subdir
        mem_dir = Path(args.project_root) / ".workflow_artifacts" / "memory"
        checkpoints_dir = mem_dir / "checkpoints"
        checkpoints_dir.mkdir(parents=True, exist_ok=True)

        # 2. Compose stage token
        stage = f"thorough-plan:round-{args.round_n}-{args.phase}"

        # 3. Session-state update FIRST (M-01 write-order: older mtime)
        ss_path_str = ""
        if args.session_state:
            try:
                ss_path = Path(args.session_state)
                ss_path.parent.mkdir(parents=True, exist_ok=True)
                _update_session_state(
                    ss_path,
                    stage=stage,
                    task=args.task,
                    branch=args.branch,
                    sid=sid,
                )
                ss_path_str = str(ss_path)
            except Exception as exc:  # noqa: BLE001
                print(
                    f"[thorough_plan_checkpoint] WARNING: session-state update failed: {exc}",
                    file=sys.stderr,
                )

        # Small sleep to guarantee checkpoint mtime > session-state mtime (M-01)
        # Only needed when we actually wrote a session-state file.
        if ss_path_str:
            time.sleep(0.01)

        # 4. Write checkpoint file SECOND (newer mtime — M-01)
        ckpt_path = _write_checkpoint(
            checkpoints_dir=checkpoints_dir,
            sid=sid,
            stage=stage,
            task=args.task,
            branch=args.branch,
            phase=args.phase,
            plan_path=args.plan_path,
            critic_path=args.critic_path,
            ss_path_str=ss_path_str,
        )

        # 5. Write pending-restore sentinel LAST (even newer mtime)
        _write_sentinel(mem_dir, sid, ckpt_path)

        # 6. Print checkpoint path to stdout
        print(str(ckpt_path))

    except Exception as exc:  # noqa: BLE001
        print(
            f"[thorough_plan_checkpoint] WARNING: unexpected error: {exc}; "
            "checkpoint skipped (fail-OPEN).",
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
