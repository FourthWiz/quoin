#!/usr/bin/env python3
"""context_bundle.py — context bundle helper for orchestrator spawn-prompt construction (IVG-164 stage 2 T-02).

Emits bundle lines for configured artifact members to stdout so the orchestrator
review-spawn construction block can wrap them in a [quoin-bundle] block.

Per-member extraction rules (architecture D-06, corrected per review round 1):
  - architecture.md   → path + FULL ## For human block (newlines collapsed to
                        single spaces so the member stays on one line)
  - current-plan.md   → path + FULL ## For human block when present, else an
                        explicit path-only entry
  - spec.md           → ALWAYS path-only (Class A — no ## For human block by contract)

Members whose FILE does not exist are OMITTED entirely (a path-only line is only
emitted for an existing file whose block is absent). If no member file exists,
output is empty and the caller's emptiness guard suppresses the block.

Path resolution delegates to the sibling path_resolve.py (grandfathering and
stage-name lookup); a mechanical stage-N fallback applies only if that call fails.
The resolved task directory must be contained in <project-root>/.workflow_artifacts/
or the bundle is suppressed (path-traversal guard).

Sanitization (review round 1, security; hardened per round 2 minor 1): the
summary is whitespace-collapsed and control-char-stripped FIRST, then a
bracket-normalized probe (whitespace removed inside [...] groups) is tested
against the sentinel list — so whitespace-inserted variants ("[autonomous ]",
"[ no-redispatch]") cannot evade the check and later re-form a near-canonical
token. The list covers [quoin-bundle], [/quoin-bundle], [autonomous],
[no-redispatch] (bare AND [no-redispatch:N] counter forms), [no-interactive],
[quoin-onbehalf], [no-phase-budget], and [no-session-age-guard]. A match
rejects the summary — the member falls back to the path-only entry. Embedded
" | " in summary text is replaced with " ¦ " so consumers can split each member
line on the FIRST " | " only. Summaries are clamped to _SUMMARY_MAX_BYTES
(UTF-8 bytes) with an explicit truncation marker (round 2 minor 4 — an
unbounded block would silently invert the census cost model).

Extraction is bounded and fence-aware: the ## For human heading is recognized
only within the first 50 lines after the closing frontmatter '---' (or the first
50 lines of the file when no frontmatter exists), and headings inside fenced
code blocks are ignored.

CLI:
  context_bundle.py --task <name> [--stage <N-or-name>] [--project-root <path>] [--wrap]

Output format: one member per line: <absolute-path> | <summary>
  where summary is the full ## For human block (newlines collapsed), or the
  literal string "summary: absent (path-only)". Consumers split each member
  line on the FIRST " | " only.

With --wrap: output wrapped in [quoin-bundle] / [/quoin-bundle] markers.
Emission sites use --wrap — markers are never hand-rolled in shell.

Degradation-safe: exit 0 on all errors; empty output → caller's guard catches it.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

SENTINEL_TOKENS = (
    "[quoin-bundle]",
    "[/quoin-bundle]",
    "[autonomous]",
    "[no-redispatch]",
    "[no-redispatch:",  # counter form [no-redispatch:N] (round 2 minor 1)
    "[no-interactive]",
    "[quoin-onbehalf]",
    "[no-phase-budget]",
    "[no-session-age-guard]",
    "[quoin-handoff/",
    "[/quoin-handoff]",
)

_HEADING_SCAN_LIMIT = 50

# Summary byte clamp (review round 2 minor 4): the pre-fix first-line emission
# had an implicit bound that full-block emission removed; without a clamp an
# oversized ## For human block silently inverts the census's net-positive row
# and bloats the spawn prompt. 4096 B comfortably fits every measured real
# block (largest observed: 2,716 B) while capping the worst case.
_SUMMARY_MAX_BYTES = 4096
_TRUNCATION_MARKER = " …[truncated]"

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

_BRACKET_GROUP = re.compile(r"\[([^\[\]]*)\]")


def resolve_project_root() -> Path:
    """Walk up from cwd to find nearest ancestor containing .workflow_artifacts/."""
    cwd = Path.cwd().resolve()
    for ancestor in [cwd, *cwd.parents]:
        if (ancestor / ".workflow_artifacts").is_dir():
            return ancestor
    return cwd


def resolve_task_dir(task_name: str, stage: str | None, project_root: Path) -> Path:
    """Resolve the task's artifact directory via the sibling path_resolve.py.

    Delegation preserves grandfathering (root-level current-plan.md, stage-name
    lookup). Mechanical fallback fires only when the resolver call fails.
    """
    resolver = Path(__file__).resolve().parent / "path_resolve.py"
    if resolver.exists():
        cmd = [
            sys.executable,
            str(resolver),
            "--task",
            task_name,
            "--project-root",
            str(project_root),
        ]
        if stage is not None:
            cmd.extend(["--stage", str(stage)])
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            candidate = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
            if proc.returncode == 0 and candidate:
                return Path(candidate)
        except (OSError, subprocess.SubprocessError):
            pass
    wf = project_root / ".workflow_artifacts" / task_name
    return wf / f"stage-{stage}" if stage else wf


def extract_for_human_block(filepath: Path) -> str | None:
    """Extract the ## For human block from a v3-format artifact.

    The heading is recognized only within the first _HEADING_SCAN_LIMIT lines
    after the closing frontmatter '---' (or of the whole file when no
    frontmatter exists). Headings inside fenced code blocks are ignored. The
    block body runs to the next fence-external '## ' heading.

    Returns the block body, or None if absent.
    """
    if not filepath.exists():
        return None
    content = filepath.read_text(encoding="utf-8", errors="replace")
    lines = content.splitlines()

    body_start = 0
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                body_start = i + 1
                break

    in_fence = False
    heading_idx = None
    for offset, line in enumerate(lines[body_start:]):
        if offset >= _HEADING_SCAN_LIMIT:
            break
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence and re.match(r"^## For human\s*$", line):
            heading_idx = body_start + offset
            break
    if heading_idx is None:
        return None

    in_fence = False
    lines_out: list[str] = []
    for line in lines[heading_idx + 1 :]:
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            lines_out.append(line)
            continue
        if not in_fence and line.startswith("## "):
            break
        lines_out.append(line)

    body = "\n".join(lines_out).strip()
    return body if body else None


def sanitize_summary(text: str) -> str | None:
    """Collapse to one line and neutralize prompt-affecting content.

    Returns None (→ path-only fallback) when the text carries any sentinel or
    marker token — data must never become prompt directives.

    Order matters (review round 2 minor 1): collapse whitespace and strip
    control characters FIRST, then test tokens on a bracket-normalized probe
    (whitespace removed inside [...] groups). Testing the RAW text let
    whitespace-inserted variants like "[autonomous ]" pass while the collapse
    re-formed a near-canonical token in the emitted line.
    """
    flat = _CONTROL_CHARS.sub("", " ".join(text.split()))
    probe = _BRACKET_GROUP.sub(
        lambda m: "[" + re.sub(r"\s+", "", m.group(1)) + "]", flat.lower()
    )
    for token in SENTINEL_TOKENS:
        if token in probe:
            return None
    flat = flat.replace(" | ", " ¦ ")
    encoded = flat.encode("utf-8")
    if len(encoded) > _SUMMARY_MAX_BYTES:
        clipped = encoded[:_SUMMARY_MAX_BYTES].decode("utf-8", errors="ignore").rstrip()
        flat = clipped + _TRUNCATION_MARKER
    return flat if flat else None


def is_class_a_artifact(filepath: Path) -> bool:
    """Class A artifacts carry no ## For human block by contract (spec.md only)."""
    return filepath.name == "spec.md"


def build_bundle_lines(task_name: str, stage: str | None, project_root: Path) -> list[str]:
    """Build bundle member lines for the given task and stage.

    Returns a list of strings, each "<path> | <summary>". Missing files are
    omitted; a path outside .workflow_artifacts/ suppresses the whole bundle.
    """
    wf_root = (project_root / ".workflow_artifacts").resolve()
    task_dir = resolve_task_dir(task_name, stage, project_root).resolve()
    task_root = (project_root / ".workflow_artifacts" / task_name).resolve()
    for candidate in (task_dir, task_root):
        try:
            candidate.relative_to(wf_root)
        except ValueError:
            return []

    members: list[Path] = [
        task_root / "architecture.md",
        task_dir / "current-plan.md",
        task_root / "spec.md",
    ]

    lines: list[str] = []
    for filepath in members:
        if not filepath.exists():
            continue
        if is_class_a_artifact(filepath):
            lines.append(f"{filepath} | summary: absent (path-only)")
            continue
        summary = extract_for_human_block(filepath)
        sanitized = sanitize_summary(summary) if summary is not None else None
        if sanitized is None:
            lines.append(f"{filepath} | summary: absent (path-only)")
        else:
            lines.append(f"{filepath} | {sanitized}")

    return lines


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Emit context bundle lines for orchestrator spawn-prompt construction"
    )
    parser.add_argument("--task", required=True, help="Task name (kebab-case)")
    parser.add_argument(
        "--stage",
        default=None,
        help="Stage number or stage name (optional; names resolve via "
        "path_resolve.py's ## Stage decomposition lookup)",
    )
    parser.add_argument(
        "--project-root",
        default=None,
        help="Project root path (default: auto-resolve from cwd)",
    )
    parser.add_argument(
        "--wrap",
        action="store_true",
        help="Wrap output in [quoin-bundle] / [/quoin-bundle] markers",
    )
    args = parser.parse_args()

    try:
        project_root = Path(args.project_root) if args.project_root else resolve_project_root()
        lines = build_bundle_lines(args.task, args.stage, project_root)
    except (OSError, ValueError) as exc:
        print(f"context_bundle: suppressed ({exc})", file=sys.stderr)
        return
    if not lines:
        return
    out: list[str] = []
    if args.wrap:
        out.append("[quoin-bundle]")
    out.extend(lines)
    if args.wrap:
        out.append("[/quoin-bundle]")
    print("\n".join(out))


if __name__ == "__main__":
    main()
