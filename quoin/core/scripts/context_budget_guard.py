#!/usr/bin/env python3
"""context_budget_guard.py — On-demand context-budget guard (IVG-141).

Portable-core, on-demand utilization reader modeled on the Claude adapter's
``session_age_guard.py``. At a heavy phase/task boundary a skill runs this guard
to decide whether the CURRENT session transcript is over a pre-phase budget
(~70%), so it can save a durable boundary checkpoint and hand off to a fresh
session BEFORE a phase adds a full context load and rides into auto-compact.

Portability
-----------
This module is portable-core: the reading logic and constants are runtime-neutral
and mirror ``hooks/_lib.sh::compute_context_reading`` (the shell and Python readers
are kept in lock-step by a parity test). The only Claude-specific surface is
transcript RESOLUTION (``~/.claude/projects``), and that is fully bypassable by
passing ``--transcript`` explicitly — a future Fable adapter passes its
own transcript path. The inline project-hash regex mirrors the
``get_session_uuid.py`` core precedent (do NOT import a Claude-adapter module from
core).

Reading the context size
------------------------
The guard reads the transcript tail (``QUOIN_CONTEXT_TAIL_BYTES``, default 2 MiB)
and takes the newest main-chain assistant ``usage`` record
(``input + cache_read + cache_creation`` tokens). Records from sidechains and
synthetic error messages are ignored. A ``compact_boundary`` record that is newer
than the last usage record supplies ``postTokens`` instead. The reading is divided
by the context window of the model:

  1. ``QUOIN_EFFECTIVE_CONTEXT_LIMIT`` when it is a positive integer;
  2. 200000 when ``CLAUDE_CODE_DISABLE_1M_CONTEXT`` is truthy;
  3. 200000 when the tail shows the API refused the 1M window for lack of credits;
  4. 1000000 when the model id carries ``[1m]`` or the model table lists it as 1M;
  5. 1000000 when ``ANTHROPIC_MODEL`` or a settings file selects a ``[1m]`` model;
  6. the table value for other known model ids (200000);
  7. 1000000 when the session has already exceeded 200000 tokens, else 200000.

The credit refusal is latched for the whole tail: it applies to the process-wide
session, so one refusal anywhere in the scanned tail clamps the window to 200000
until the record scrolls out of the tail. A resumed session reads only its own
transcript, so a refusal recorded before the resume is seen only while it is still
inside the tail.

When no usage can be read (empty or non-JSONL file, no qualifying record) the
reading falls back to the byte estimate: ``int((bytes / bpt / lim) * 10000)``
(unclamped; Python int() truncates toward zero exactly as awk ``printf "%d"`` for
positive values), where ``bpt``/``lim`` come from ``QUOIN_BYTES_PER_TOKEN`` and
``QUOIN_EFFECTIVE_CONTEXT_LIMIT`` with the ``_lib.sh`` defaults (8.0 / 150000).

Usage
-----
  python3 context_budget_guard.py [--threshold-bps N] [--project-root PATH] \
      [--current-uuid UUID] [--transcript PATH]

Threshold precedence: ``--threshold-bps`` if given, else
``QUOIN_PHASE_BOUNDARY_BPS`` (default 7000 bp = 70%).

Opt-out (checked FIRST, before any transcript read):
  ``QUOIN_DISABLE_PHASE_BUDGET=1`` → stdout ``OK|disabled|``, exit 0.

Caller-side behavior knob (NOT read here): ``QUOIN_PHASE_BUDGET_BLOCK=1`` — the
four SKILL wirings switch their reaction to ``OVER`` from the default
checkpoint+advisory+PROCEED to checkpoint + printed fresh-session resume
instruction + STOP. This helper's OK/OVER contract is IDENTICAL regardless of
that knob; no code here branches on it. Documented so the knob surface is
complete.

Stdout: single line ``<status>|<util_bps>|<transcript_path>``
  e.g. OVER|7250|/…/<uuid>.jsonl      (genuinely over budget)
       OK|5100|/…/<uuid>.jsonl        (under budget → proceed)
       OK|disabled|                   (QUOIN_DISABLE_PHASE_BUDGET=1)
       OK|0|                          (fail-OPEN: no transcript / stat error / bad args)

Stderr: human-readable diagnostics (never stdout).

Exit codes (mirror session_age_guard.py):
  1 — util_bps >= threshold (OVER)
  0 — OK (under), disabled, and EVERY fail-OPEN path (no project dir, no jsonl,
      missing uuid file, stat error, bad args)

Design decisions:
  - Stdlib-only: argparse, json, math, os, re, sys, pathlib. Python 3.8-safe.
  - Fail-OPEN: any error → ``OK|0|``, exit 0 (never block a phase on our failure).
  - Any failure while reading real usage falls back to the byte estimate, which is
    BYTE-IDENTICAL to the awk in the shell estimate helper.
  - Stderr carries one extra line ``[context-budget-guard] reading source=… tokens=…
    window=…`` so callers and tests can tell a real reading from an estimate.
  - Project hash mirrors Claude harness behavior: replace every char outside
    [A-Za-z0-9-] with '-' (matches get_session_uuid.project_hash /
    session_age_guard._project_hash).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Optional

# Claude Code exports the running session's id to Bash tool calls. Strict shape
# check: the value is joined into a path and a glob pattern.
_UUID_RE = re.compile(
    r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
)


def _env_session_uuid(environ=None) -> Optional[str]:
    """Return the session UUID from CLAUDE_CODE_SESSION_ID, or None.

    Unset/empty -> None silently; a malformed value -> None with a stderr note.
    """
    env = os.environ if environ is None else environ
    raw = (env.get("CLAUDE_CODE_SESSION_ID") or "").strip()
    if not raw:
        return None
    if not _UUID_RE.match(raw):
        print(
            "[context-budget-guard] ignoring malformed CLAUDE_CODE_SESSION_ID",
            file=sys.stderr,
        )
        return None
    return raw


def _find_uuid_jsonl(projects_base: Path, project_dir: Path, uuid: str) -> Optional[Path]:
    """Locate <uuid>.jsonl: the project dir first, then any single project dir.

    The cross-project lookup covers a session whose cwd hash differs from the
    project root's hash (worktrees, subdirectories). Never recursive.
    """
    name = f"{uuid}.jsonl"
    if project_dir.exists():
        candidate = project_dir / name
        if candidate.exists():
            return candidate
    try:
        for candidate in projects_base.glob(f"*/{name}"):
            return candidate
    except OSError:
        pass
    return None


# Drive conflict-copy filter (reused verbatim from get_session_uuid.py, ivg-75)
# — a conflict copy "UUID 2.jsonl" newer than the real file must not be picked.
_CONFLICT_RE = re.compile(r" \d{1,3}(\.[^ ]*)?$")


def _project_hash(project_root: Path) -> str:
    """Convert an absolute path to a Claude project hash.

    Mirrors the Claude harness: every character outside [A-Za-z0-9-] becomes
    '-'. Handles paths with spaces / dots / @ / underscores. INLINE by design —
    do NOT import a Claude-adapter module from core (mirrors get_session_uuid.py).
    """
    return re.sub(r'[^A-Za-z0-9-]', '-', str(project_root).rstrip("/"))


def _resolve_transcript(args) -> Path | None:
    """Resolve the transcript file to measure, or None (→ fail-OPEN).

    Precedence: --transcript, else --current-uuid → <hash>/<uuid>.jsonl, else
    CLAUDE_CODE_SESSION_ID (project dir, then any project dir), else newest
    non-conflict *.jsonl by mtime under ~/.claude/projects/<hash>/.
    """
    # 1. Explicit transcript path wins — the runtime-neutral entry point.
    if args.transcript:
        target = Path(args.transcript)
        if not target.exists():
            print(
                f"[context-budget-guard] --transcript not found: {target} — fail-OPEN",
                file=sys.stderr,
            )
            return None
        return target

    # 2/3. Resolve via project hash under ~/.claude/projects/.
    if args.project_root is None:
        project_root = Path.cwd()
        print(
            f"[context-budget-guard] --project-root not provided; using cwd: {project_root}",
            file=sys.stderr,
        )
    else:
        project_root = Path(args.project_root).resolve()

    projects_base = Path.home() / ".claude" / "projects"
    project_dir = projects_base / _project_hash(project_root)

    # The session id the harness exports names the current transcript exactly;
    # prefer it over the mtime guess, which picks a concurrent session's file.
    env_uuid = None if args.current_uuid else _env_session_uuid()
    if env_uuid:
        target = _find_uuid_jsonl(projects_base, project_dir, env_uuid)
        if target is None:
            print(
                f"[context-budget-guard] env session jsonl not found for {env_uuid} "
                f"under {projects_base} — fail-OPEN",
                file=sys.stderr,
            )
            return None
        return target

    if not project_dir.exists():
        print(
            f"[context-budget-guard] project dir not found: {project_dir} — fail-OPEN",
            file=sys.stderr,
        )
        return None

    if args.current_uuid:
        target = project_dir / f"{args.current_uuid}.jsonl"
        if not target.exists():
            print(
                f"[context-budget-guard] uuid jsonl not found: {target} — fail-OPEN",
                file=sys.stderr,
            )
            return None
        return target

    jsonl_files = sorted(
        [p for p in project_dir.glob("*.jsonl") if not _CONFLICT_RE.search(p.name)],
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    if not jsonl_files:
        print(
            f"[context-budget-guard] no jsonl files in {project_dir} — fail-OPEN",
            file=sys.stderr,
        )
        return None
    return jsonl_files[0]


def _resolve_threshold(args) -> int:
    """Threshold precedence: --threshold-bps if given, else env (default 7000).

    Defensive: a malformed env value falls back to 7000 (fail-OPEN spirit —
    never crash the guard on a bad knob).
    """
    if args.threshold_bps is not None:
        return args.threshold_bps
    try:
        return int(os.environ.get("QUOIN_PHASE_BOUNDARY_BPS", 7000))
    except (TypeError, ValueError):
        return 7000


def _resolve_constants() -> tuple[float, float]:
    """Return (bpt, lim) from env with the EXACT _lib.sh defaults (8.0 / 150000).

    Defensive parse: a malformed env value falls back to the default.
    """
    try:
        bpt = float(os.environ.get("QUOIN_BYTES_PER_TOKEN") or 8.0)
    except (TypeError, ValueError):
        bpt = 8.0
    try:
        lim = float(os.environ.get("QUOIN_EFFECTIVE_CONTEXT_LIMIT") or 150000)
    except (TypeError, ValueError):
        lim = 150000.0
    return bpt, lim


_DEFAULT_TAIL_BYTES = 2097152
_MAX_TAIL_BYTES = 67108864
_MAX_TOKENS = 999999999999
_CLAMP_PHRASE = "Usage credits required for 1M context"
_CLAMP_API_ERROR = "long_context_credits_required"
_LOWER = {c: c + 32 for c in range(ord("A"), ord("Z") + 1)}

# Model-id normalization and window rules. The same literals live in
# hooks/_lib.sh (jq); a drift test compares the two blocks. Keep one entry per
# line and avoid backslashes so the raw text can be diffed.
# ctx-rules-begin
_CTX_STRIP = (
    "^(us|eu|apac|global)[.]",
    "^anthropic[.]",
    "-v[0-9]+(:[0-9]+)?$",
    ":[0-9]+$",
    "@[0-9]{8}$",
    "-[0-9]{8}$",
)
_CTX_RULES = (
    ("^claude-(fable|mythos)(-|$)", 1000000),
    ("^claude-(opus|sonnet|haiku)-([5-9]|[1-9][0-9]+)(-[0-9]+)?$", 1000000),
    ("^claude-opus-4-(7|8)$", 1000000),
    ("^claude-", 200000),
)
# ctx-rules-end


def _ascii_lower(text: str) -> str:
    return text.translate(_LOWER)


def _positive_int(raw):
    """Return raw as a positive int of at most 9 digits, else None."""
    if not isinstance(raw, str) or not re.fullmatch(r"[0-9]+", raw):
        return None
    digits = raw.lstrip("0")
    if not digits or len(digits) > 9:
        return None
    return int(digits)


def _truthy(raw) -> bool:
    return isinstance(raw, str) and _ascii_lower(raw) in ("1", "true", "yes", "on")


def _tail_bytes(environ) -> int:
    raw = environ.get("QUOIN_CONTEXT_TAIL_BYTES") or ""
    if not re.fullmatch(r"[0-9]+", raw):
        return _DEFAULT_TAIL_BYTES
    digits = raw.lstrip("0")
    if not digits:
        return _DEFAULT_TAIL_BYTES
    if len(digits) >= 9 or int(digits) > _MAX_TAIL_BYTES:
        return _MAX_TAIL_BYTES
    return int(digits)


def _tail_lines(path, tail):
    """Return the transcript's trailing lines as bytes.

    When the file is larger than ``tail`` only its last ``tail`` bytes are read
    and everything through the first newline is dropped (the cut usually lands
    mid-line). Splits on b"\\n" only — never ``splitlines()`` — so U+2028 inside
    JSON text cannot fabricate a line break.
    """
    size = os.stat(path).st_size
    with open(path, "rb") as handle:
        if size > tail:
            handle.seek(size - tail)
            data = handle.read()
            cut = data.find(b"\n")
            data = b"" if cut < 0 else data[cut + 1:]
        else:
            data = handle.read()
    return data.split(b"\n")


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _num(value):
    return value if _is_number(value) else 0


def _in_token_range(value) -> bool:
    return _is_number(value) and 1 <= value <= _MAX_TOKENS


def _has_clamp_text(content) -> bool:
    if isinstance(content, str):
        return _CLAMP_PHRASE in content
    if isinstance(content, list):
        for item in content:
            if (
                isinstance(item, dict)
                and item.get("type") == "text"
                and isinstance(item.get("text"), str)
                and _CLAMP_PHRASE in item["text"]
            ):
                return True
    return False


def _scan(lines):
    """One pass over the tail. Returns (tokens, model, peak, clamp).

    ``tokens`` is None when no qualifying record exists.
    """
    tokens = None
    model = ""
    peak = 0
    clamp = False
    for raw in lines:
        try:
            text = raw.decode("utf-8", errors="replace")
            # jq accepts exactly one leading byte-order mark on any line.
            if text.startswith("\ufeff"):
                text = text[1:]
            rec = json.loads(text)
        except (ValueError, RecursionError):
            continue
        if not isinstance(rec, dict):
            continue
        try:
            if rec.get("isSidechain") is True:
                continue
            rtype = rec.get("type")
            if rtype == "assistant" and isinstance(rec.get("message"), dict):
                msg = rec["message"]
                mid = msg.get("model")
                if mid == "<synthetic>":
                    if rec.get("isApiErrorMessage") is True and (
                        rec.get("apiError") == _CLAMP_API_ERROR
                        or _has_clamp_text(msg.get("content"))
                    ):
                        clamp = True
                    continue
                usage = msg.get("usage")
                if not isinstance(usage, dict):
                    continue
                total = math.floor(
                    _num(usage.get("input_tokens"))
                    + _num(usage.get("cache_read_input_tokens"))
                    + _num(usage.get("cache_creation_input_tokens"))
                )
                if 1 <= total <= _MAX_TOKENS:
                    tokens = total
                    model = mid if isinstance(mid, str) else ""
                    peak = max(peak, total)
            elif rtype == "system" and rec.get("subtype") == "compact_boundary":
                meta = rec.get("compactMetadata")
                if not isinstance(meta, dict):
                    continue
                pre = meta.get("preTokens")
                post = meta.get("postTokens")
                if _in_token_range(pre):
                    peak = max(peak, math.floor(pre))
                if _in_token_range(post):
                    tokens = math.floor(post)
        except (TypeError, ValueError, OverflowError):
            continue
    return tokens, model, peak, clamp


def normalize_model(model) -> str:
    text = _ascii_lower(model if isinstance(model, str) else "")
    text = text.replace("[1m]", "")
    for pattern in _CTX_STRIP:
        text = re.sub(pattern, "", text, count=1)
    return text


def window_for_model(model) -> int:
    """Context window the model table gives for ``model``; 0 means no answer."""
    text = normalize_model(model)
    for pattern, window in _CTX_RULES:
        if re.search(pattern, text):
            return window
    return 0


def _has_1m(model) -> bool:
    return isinstance(model, str) and "[1m]" in _ascii_lower(model)


def _settings_model(project_root, environ) -> str:
    """First non-empty model string: env, project local, project, user settings."""
    env_model = environ.get("ANTHROPIC_MODEL") or ""
    if env_model:
        return env_model
    candidates = []
    if project_root is not None:
        candidates.append(Path(project_root) / ".claude" / "settings.local.json")
        candidates.append(Path(project_root) / ".claude" / "settings.json")
    home = environ.get("HOME") or ""
    if home:
        candidates.append(Path(home) / ".claude" / "settings.json")
    for candidate in candidates:
        try:
            data = json.loads(candidate.read_text(encoding="utf-8-sig", errors="replace"))
        except (OSError, ValueError, RecursionError):
            continue
        if isinstance(data, dict) and isinstance(data.get("model"), str) and data["model"]:
            return data["model"]
    return ""


def resolve_window(model, peak, clamp, project_root, environ) -> int:
    override = _positive_int(environ.get("QUOIN_EFFECTIVE_CONTEXT_LIMIT"))
    if override:
        return override
    if _truthy(environ.get("CLAUDE_CODE_DISABLE_1M_CONTEXT")):
        return 200000
    if clamp:
        return 200000
    if _has_1m(model):
        return 1000000
    hint = window_for_model(model)
    if hint == 1000000:
        return 1000000
    if _has_1m(_settings_model(project_root, environ)):
        return 1000000
    if hint:
        return hint
    if peak > 200000:
        return 1000000
    return 200000


def _estimate_bps(path) -> int:
    bpt, lim = _resolve_constants()
    num_bytes = Path(path).stat().st_size
    # BYTE-IDENTICAL to the awk in the shell estimate helper:
    #   awk 'BEGIN{ printf "%d\n", (b / bpt / lim) * 10000 }'
    return int((num_bytes / bpt / lim) * 10000)


def read_context(path, project_root=None, environ=None):
    """Return (bps, source, tokens, window).

    ``source`` is "real" when a usage record was read (tokens/window set) and
    "estimated" when the byte estimate was used (tokens/window None).
    """
    env = os.environ if environ is None else environ
    try:
        tokens, model, peak, clamp = _scan(_tail_lines(path, _tail_bytes(env)))
        if tokens is not None:
            window = resolve_window(model, peak, clamp, project_root, env)
            return tokens * 10000 // window, "real", tokens, window
    except Exception:  # noqa: BLE001 — any reading failure degrades to the estimate
        pass
    return _estimate_bps(path), "estimated", None, None


def main() -> int:
    # Opt-out FIRST — before any arg parse / transcript read.
    if os.environ.get("QUOIN_DISABLE_PHASE_BUDGET") == "1":
        print("OK|disabled|")
        return 0

    parser = argparse.ArgumentParser(
        description="On-demand context-budget guard for phase boundaries (IVG-141)."
    )
    parser.add_argument(
        "--threshold-bps",
        type=int,
        default=None,
        help="Budget threshold in basis-points (0..10000). "
             "Defaults to QUOIN_PHASE_BOUNDARY_BPS (default 7000).",
    )
    parser.add_argument(
        "--project-root",
        type=str,
        default=None,
        help="Absolute path to the project root. Defaults to cwd (with a warning).",
    )
    parser.add_argument(
        "--current-uuid",
        type=str,
        default=None,
        help="UUID of the current session's jsonl (without .jsonl). "
             "If omitted, CLAUDE_CODE_SESSION_ID is used; the most recent "
             "non-conflict *.jsonl by mtime only when that is unset or malformed.",
    )
    parser.add_argument(
        "--transcript",
        type=str,
        default=None,
        help="Explicit transcript path (runtime-neutral entry point). "
             "Overrides project-root/uuid resolution.",
    )

    try:
        args = parser.parse_args()
    except SystemExit:
        # Even on arg-parse errors, fail-OPEN.
        print("OK|0|")
        return 0

    try:
        threshold = _resolve_threshold(args)
        bpt, lim = _resolve_constants()

        target = _resolve_transcript(args)
        if target is None:
            print("OK|0|")
            return 0

        project_root = (
            Path(args.project_root).resolve() if args.project_root else Path.cwd()
        )
        try:
            util_bps, source, tokens, window = read_context(
                target, project_root, os.environ
            )
        except OSError as exc:
            print(
                f"[context-budget-guard] cannot stat {target}: {exc} — fail-OPEN",
                file=sys.stderr,
            )
            print("OK|0|")
            return 0

        print(
            f"[context-budget-guard] reading source={source} "
            f"tokens={'-' if tokens is None else tokens} "
            f"window={'-' if window is None else window}",
            file=sys.stderr,
        )

        status = "OVER" if util_bps >= threshold else "OK"
        print(f"{status}|{util_bps}|{target}")

        if status == "OVER":
            print(
                f"[context-budget-guard] util {util_bps}bp >= threshold "
                f"{threshold}bp — recommend boundary checkpoint + handoff",
                file=sys.stderr,
            )
            return 1

        print(
            f"[context-budget-guard] util {util_bps}bp < threshold {threshold}bp — OK",
            file=sys.stderr,
        )
        return 0
    except Exception as exc:  # noqa: BLE001 — fail-OPEN on ANY unexpected error
        print(
            f"[context-budget-guard] unexpected error: {exc} — fail-OPEN",
            file=sys.stderr,
        )
        print("OK|0|")
        return 0


if __name__ == "__main__":
    sys.exit(main())
