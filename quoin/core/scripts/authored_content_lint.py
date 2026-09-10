"""authored_content_lint.py — detect planning-process vocabulary leaking into
shipped comments and docstrings (task/decision/finding IDs, review-round
narration, gate verdicts, external tracker IDs).

Comments and docstrings in shipped code are read by people who never saw the
planning artifacts that produced them. A comment citing "T-04" or "critic
MAJ-2" or "round 3" is meaningless outside the session that wrote it. This
scanner extracts comment and docstring text from changed or whole-tree source
and flags lines carrying that vocabulary, using a precision-first taxonomy
(quoin/memory/clean-authored-content.md) split into an unconditional Tier A
set and a cue-word-gated Tier B set.

Public API:
  resolve_repo_root(start) -> Path | None
  extract_comment_regions(path_str, text) -> list[(lineno, text)]
  resolve_tracker_prefixes(branch) -> set[str]
  scan(repo_root, basis, *, triage=False) -> dict
  main(argv=None) -> int
  resolve_candidates(repo_root, basis, base_ref) -> (dict, str | None)
  read_text_source(repo_root, relpath, basis) -> str
  match_taxonomy(text, tracker_prefixes) -> (str, str) | None
  Undeterminable — exception raised when the scan cannot reach a definite
    result (maps to exit 3)

Exit codes (CLI):
  0 — clean (no findings), triage mode (always non-blocking), or globally
      disabled
  1 — one or more findings
  2 — argparse / invocation error
  3 — undeterminable (fail-OPEN: no resolvable git repo, unresolvable base
      branch, a git command failed, unparseable Python source)

Env:
  QUOIN_DISABLE_AUTHORED_CONTENT_LINT=1 — global opt-out; exit 0 + disabled
    envelope, checked before argument parsing (mirrors affected_tests.py).
  QUOIN_TRACKER_PREFIXES — csv of tracker prefixes; authoritative whenever the
    variable is SET, including an empty string (an explicit "no prefixes").
    Unset falls back to deriving prefixes from the current branch name.
  QUOIN_BASE_BRANCH — passed through to affected_tests._resolve_base_branch.

Suppression: a commentish line containing the literal `quoin-lint: allow` is
dropped from consideration — the escape hatch for a line that legitimately
discusses this taxonomy as subject matter (documented alongside the rule in
quoin/memory/clean-authored-content.md).
"""

import argparse
import ast
import io
import json
import os
import re
import subprocess
import sys
import tokenize
from pathlib import Path

from affected_tests import _resolve_base_branch


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ENV_DISABLE = "QUOIN_DISABLE_AUTHORED_CONTENT_LINT"
_ENV_TRACKER_PREFIXES = "QUOIN_TRACKER_PREFIXES"
_SUBPROCESS_TIMEOUT = 30

# Suffixes this detector scans. `.md` is excluded by construction (prose is
# the false-positive risk this whole detector exists to avoid); `.json`/
# `.jsonl` are excluded because JSON has no comment syntax.
_SCANNED_SUFFIXES = frozenset(
    {".py", ".sh", ".zsh", ".ts", ".js", ".toml", ".yml", ".yaml"}
)
_HASH_SUFFIXES = frozenset({".sh", ".zsh", ".toml", ".yml", ".yaml"})
_SLASH_SUFFIXES = frozenset({".ts", ".js"})

# Structural path exclusions (never inherited from .gitignore).
_EXCLUDE_SEGMENTS = (".workflow_artifacts/", "testdata/")
_EXCLUDE_PATHS = frozenset(
    {
        "quoin/core/scripts/authored_content_lint.py",
        "quoin/scripts/authored_content_lint.py",
        "quoin/core/scripts/comment_cleanup.py",
        "quoin/scripts/comment_cleanup.py",
    }
)

_PRAGMA = "quoin-lint: allow"

# --triage broad-recall superset probe (T-01). Fixed and non-circular: not
# imported from the taxonomy below, so the taxonomy can diverge from this
# fixed predicate without changing the census tool.
_TRIAGE_ID_SHAPE = re.compile(r"\b[A-Za-z]{2,6}-\d")
_TRIAGE_CUE_WORDS = frozenset(
    {"plan", "critic", "review", "gate", "finding", "verdict", "orchestrator", "deferred"}
)

# ---------------------------------------------------------------------------
# Taxonomy (Tier A unconditional, Tier B cue-word-gated)
# ---------------------------------------------------------------------------

_TIER_A_REGEXES = (
    re.compile(r"\b[TDRFQS]-\d{1,3}\b"),
    re.compile(r"\b(CRIT|MAJ|MIN)-\d+\b"),
    re.compile(r"\b(AC|FR)-\d+\b"),
    re.compile(r"\b(review|critic)[- ]round[- ]?\d", re.IGNORECASE),
    re.compile(r"\blessons?-\d{4}-\d{2}-\d{2}\b", re.IGNORECASE),
    re.compile(r"\bcurrent-plan\.md\b"),
    re.compile(r"\barchitecture\.md\b"),
    re.compile(r"\bcritic-response-\S*\.md\b"),
    re.compile(r"\breview-\d+\.md\b"),
    re.compile(r"\bgate-\S*\.md\b"),
    re.compile(r"\benriched-prompt\.md\b"),
)
_TIER_A_LITERALS = ("lessons-learned", ".workflow_artifacts")

# Cue words gating Tier B — a Tier B pattern only fires when one of these
# words also appears on the same line.
_TIER_B_CUE_WORDS = frozenset(
    {"plan", "critic", "review", "gate", "finding", "verdict", "orchestrator", "deferred"}
)
_TIER_B_REGEXES = (
    re.compile(r"\bMAJOR\b"),
    re.compile(r"\bMINOR\b"),
    re.compile(r"\bverdict\b", re.IGNORECASE),
    re.compile(r"\bconfidence\s+\d+", re.IGNORECASE),
    re.compile(r"\b(PASS|FAIL|REVISE)\b"),
    re.compile(r"\bround\s+\d+\b", re.IGNORECASE),
    re.compile(r"\bstage[- ]\d+\b", re.IGNORECASE),
    re.compile(r"\bphase[- ]\d+\b", re.IGNORECASE),
)

# Prefixes a branch-derived tracker set never contributes, even if the branch
# name happens to contain a letters-digits run that looks tracker-shaped.
_TRACKER_STOPLIST = frozenset(
    {"UTF", "ISO", "RFC", "PEP", "SHA", "AES", "UTC", "HTTP", "HTML", "SQL"}
)
_BRANCH_TOKEN_RE = re.compile(r"\b([A-Za-z]{2,6})-\d+\b")


class Undeterminable(Exception):
    """Raised when the scan cannot reach a definite result (maps to exit 3)."""


_Undeterminable = Undeterminable


# ---------------------------------------------------------------------------
# Subprocess helper (local copy — never cross-import, per house convention)
# ---------------------------------------------------------------------------

def _run(args):
    """Run a subprocess and return (stdout, stderr, returncode).

    stdout is stripped — safe for ref-shaped output (a SHA, a branch name, a
    file listing) but never for text that will be read back line-by-line,
    since stripping silently drops leading/trailing blank lines and shifts
    every line number after them. Line-numbered content reads must use
    `_run_content` instead.
    """
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT
        )
        return proc.stdout.strip(), proc.stderr.strip(), proc.returncode
    except subprocess.TimeoutExpired:
        return "", "timeout", 1
    except FileNotFoundError:
        return "", "git not found", 1
    except OSError as exc:
        return "", str(exc), 1


def _run_content(args):
    """Run a subprocess and return (stdout, stderr, returncode) with stdout
    left unstripped, for reads whose line numbers must match the source
    exactly (e.g. `git show HEAD:<path>`)."""
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT
        )
        return proc.stdout, proc.stderr.strip(), proc.returncode
    except subprocess.TimeoutExpired:
        return "", "timeout", 1
    except FileNotFoundError:
        return "", "git not found", 1
    except OSError as exc:
        return "", str(exc), 1


# ---------------------------------------------------------------------------
# Repo resolution
# ---------------------------------------------------------------------------

def resolve_repo_root(start):
    """Resolve a git repo root.

    Tries `git rev-parse --show-toplevel` from `start` first; falls back to a
    depth-1 `.git` child scan under `start` (mirrors branch_hygiene.py /
    affected_tests.discover_repos) for the case where `start` is the outer
    multi-repo project root rather than a git repo itself.
    """
    start = Path(start).resolve()
    out, _err, rc = _run(["git", "-C", str(start), "rev-parse", "--show-toplevel"])
    if rc == 0 and out:
        return Path(out)
    try:
        children = sorted(
            p for p in start.iterdir() if p.is_dir() and (p / ".git").exists()
        )
    except OSError:
        return None
    return children[0] if len(children) == 1 else None


def _current_branch(repo_root):
    out, _err, rc = _run(["git", "-C", str(repo_root), "rev-parse", "--abbrev-ref", "HEAD"])
    return out if rc == 0 else ""


# ---------------------------------------------------------------------------
# Tracker prefix resolution (D-09 — pure derivation function)
# ---------------------------------------------------------------------------

def _derive_tracker_prefixes(branch):
    """Pure function: derive tracker prefixes from a branch name string.

    No git, no env — testable against a plain branch-name string.
    """
    found = {m.group(1).upper() for m in _BRANCH_TOKEN_RE.finditer(branch or "")}
    return found - _TRACKER_STOPLIST


def resolve_tracker_prefixes(branch):
    """Resolve the effective tracker-prefix set.

    QUOIN_TRACKER_PREFIXES is authoritative whenever SET (including empty —
    an explicit "no prefixes"). Otherwise derive from the branch name; the
    stoplist applies only to the derived path.
    """
    if _ENV_TRACKER_PREFIXES in os.environ:
        raw = os.environ[_ENV_TRACKER_PREFIXES]
        return {p.strip().upper() for p in raw.split(",") if p.strip()}
    return _derive_tracker_prefixes(branch)


# ---------------------------------------------------------------------------
# Diff parsing (union / committed bases)
# ---------------------------------------------------------------------------

_HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _parse_diff_added_lines(diff_text):
    """Map a `git diff -U0` unified-diff body to per-file added post-image
    line numbers.

    Post-image path comes from the `+++ b/<path>` header line, never the
    `@@` line. A `+++ /dev/null` hunk (deletion-only) is skipped. A rename
    with no content change produces no `@@` hunks and is silently skipped.
    """
    result = {}
    current_file = None
    current_lineno = None
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            rest = line[len("+++ "):]
            if rest == "/dev/null":
                current_file = None
            elif rest.startswith("b/"):
                current_file = rest[2:]
            else:
                current_file = None
            current_lineno = None
            continue
        if line.startswith("@@ "):
            m = _HUNK_HEADER_RE.match(line)
            current_lineno = int(m.group(1)) if (m and current_file is not None) else None
            continue
        if current_file is None or current_lineno is None:
            continue
        if line.startswith("+"):
            result.setdefault(current_file, []).append(current_lineno)
            current_lineno += 1
        # '-' lines (including the '---' pre-image header, which never has a
        # live current_lineno at that point) consume no post-image number.
    return result


# ---------------------------------------------------------------------------
# Basis resolution — one command per mode, one post-image frame each
# ---------------------------------------------------------------------------

def _git_ls_files(repo_root, extra_args):
    out, err, rc = _run(["git", "-C", str(repo_root), "ls-files", *extra_args])
    if rc != 0:
        raise Undeterminable(f"git ls-files failed: {err}")
    return [f for f in out.splitlines() if f.strip()]


def _filter_suffix(paths):
    return [p for p in paths if Path(p).suffix in _SCANNED_SUFFIXES]


def _is_excluded(path):
    if path in _EXCLUDE_PATHS:
        return True
    return any(seg in path for seg in _EXCLUDE_SEGMENTS)


def resolve_candidates(repo_root, basis, base_ref):
    """Return (candidates, merge_base) where candidates is
    dict[relpath] -> list[int] | None (None means "scan every line")."""
    if basis == "whole-tree":
        tracked = _git_ls_files(repo_root, [])
        untracked = _git_ls_files(repo_root, ["--others", "--exclude-standard"])
        files = _filter_suffix(sorted(set(tracked) | set(untracked)))
        return {f: None for f in files if not _is_excluded(f)}, None

    merge_base_out, merge_err, merge_rc = _run(
        ["git", "-C", str(repo_root), "merge-base", base_ref, "HEAD"]
    )
    if merge_rc != 0 or not merge_base_out:
        raise Undeterminable(f"merge-base resolution failed: {merge_err}")
    merge_base = merge_base_out

    if basis == "union":
        diff_out, diff_err, diff_rc = _run(
            ["git", "-C", str(repo_root), "diff", "-U0", merge_base]
        )
        if diff_rc != 0:
            raise Undeterminable(f"git diff failed: {diff_err}")
        candidates = _parse_diff_added_lines(diff_out)
        untracked = _git_ls_files(repo_root, ["--others", "--exclude-standard"])
        for f in untracked:
            candidates.setdefault(f, None)
    else:  # committed
        diff_out, diff_err, diff_rc = _run(
            ["git", "-C", str(repo_root), "diff", "-U0", merge_base, "HEAD"]
        )
        if diff_rc != 0:
            raise Undeterminable(f"git diff failed: {diff_err}")
        candidates = _parse_diff_added_lines(diff_out)

    candidates = {
        f: lines
        for f, lines in candidates.items()
        if Path(f).suffix in _SCANNED_SUFFIXES and not _is_excluded(f)
    }
    return candidates, merge_base


_resolve_candidates = resolve_candidates


def read_text_source(repo_root, relpath, basis):
    """Read the post-image text for `relpath`, matching the basis frame:
    worktree file for union/whole-tree; the HEAD blob for committed."""
    if basis == "committed":
        out, err, rc = _run_content(["git", "-C", str(repo_root), "show", f"HEAD:{relpath}"])
        if rc != 0:
            raise Undeterminable(f"git show HEAD:{relpath} failed: {err}")
        return out
    full = repo_root / relpath
    try:
        return full.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise Undeterminable(f"cannot read {relpath}: {exc}") from exc


_read_text_source = read_text_source


# ---------------------------------------------------------------------------
# Comment / docstring extraction
# ---------------------------------------------------------------------------

def _docstring_node(node):
    body = getattr(node, "body", None)
    if not body:
        return None
    first = body[0]
    if (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        return first.value
    return None


def _extract_python_regions(text):
    regions = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                regions.append((tok.start[0], tok.string))
    except (tokenize.TokenError, IndentationError, SyntaxError) as exc:
        raise Undeterminable(f"unparseable Python source: {exc}") from exc
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        raise Undeterminable(f"unparseable Python source: {exc}") from exc
    lines = text.splitlines()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            doc_node = _docstring_node(node)
            if doc_node is None:
                continue
            start = doc_node.lineno
            end = getattr(doc_node, "end_lineno", start) or start
            for ln in range(start, end + 1):
                if 1 <= ln <= len(lines):
                    regions.append((ln, lines[ln - 1]))
    return regions


def _extract_hash_regions(text):
    regions = []
    for i, line in enumerate(text.splitlines(), start=1):
        idx = line.find("#")
        if idx != -1:
            regions.append((i, line[idx:]))
    return regions


def _extract_slash_regions(text):
    regions = []
    for i, line in enumerate(text.splitlines(), start=1):
        idx = line.find("//")
        if idx != -1:
            regions.append((i, line[idx:]))
    for m in re.finditer(r"/\*.*?\*/", text, re.DOTALL):
        start_line = text.count("\n", 0, m.start()) + 1
        for offset, block_line in enumerate(m.group(0).splitlines()):
            regions.append((start_line + offset, block_line))
    return regions


def extract_comment_regions(path_str, text):
    """Return [(lineno, commentish_text), ...] for every commentish region
    in `text`, dispatched by `path_str`'s suffix."""
    suffix = Path(path_str).suffix
    if suffix == ".py":
        return _extract_python_regions(text)
    if suffix in _HASH_SUFFIXES:
        return _extract_hash_regions(text)
    if suffix in _SLASH_SUFFIXES:
        return _extract_slash_regions(text)
    return []


def _select_regions(regions, candidate_lines):
    """Dedupe by line number (first occurrence wins), then filter to
    `candidate_lines` (None means every line is a candidate)."""
    dedup = {}
    for ln, txt in regions:
        dedup.setdefault(ln, txt)
    if candidate_lines is None:
        return sorted(dedup.items())
    cand_set = set(candidate_lines)
    return sorted((ln, txt) for ln, txt in dedup.items() if ln in cand_set)


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def match_taxonomy(text, tracker_prefixes):
    for pat in _TIER_A_REGEXES:
        m = pat.search(text)
        if m:
            return "A", m.group(0)
    for lit in _TIER_A_LITERALS:
        if lit in text:
            return "A", lit
    if tracker_prefixes:
        pat = re.compile(
            r"\b(" + "|".join(re.escape(p) for p in sorted(tracker_prefixes)) + r")-\d+\b",
            re.IGNORECASE,
        )
        m = pat.search(text)
        if m:
            return "A", m.group(0)
    lowered = text.lower()
    has_cue = any(
        re.search(r"\b" + re.escape(w) + r"\b", lowered) for w in _TIER_B_CUE_WORDS
    )
    if has_cue:
        for pat in _TIER_B_REGEXES:
            m = pat.search(text)
            if m:
                return "B", m.group(0)
    return None


_match_taxonomy = match_taxonomy


def _is_triage_candidate(text):
    if _TRIAGE_ID_SHAPE.search(text):
        return True
    lowered = text.lower()
    return any(re.search(r"\b" + w + r"\b", lowered) for w in _TRIAGE_CUE_WORDS)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def scan(repo_root, basis, *, triage=False, base_ref=None):
    """Run the full scan. Returns a result dict (see main() for shape)."""
    tracker_prefixes = resolve_tracker_prefixes(_current_branch(repo_root))
    candidates, merge_base = resolve_candidates(repo_root, basis, base_ref)

    findings = []
    triage_candidates = []
    for relpath, cand_lines in sorted(candidates.items()):
        text = read_text_source(repo_root, relpath, basis)
        regions = extract_comment_regions(relpath, text)
        selected = _select_regions(regions, cand_lines)
        for lineno, region_text in selected:
            if _PRAGMA in region_text:
                continue
            if triage:
                if _is_triage_candidate(region_text):
                    triage_candidates.append(
                        {"file": relpath, "line": lineno, "text": region_text.strip()}
                    )
                continue
            match = match_taxonomy(region_text, tracker_prefixes)
            if match is not None:
                tier, token = match
                findings.append(
                    {"file": relpath, "line": lineno, "token": token, "tier": tier}
                )

    return {
        "basis": basis,
        "base_ref": base_ref,
        "merge_base": merge_base,
        "tracker_prefixes": sorted(tracker_prefixes),
        "triage": triage,
        "files_scanned": len(candidates),
        "findings": findings,
        "candidates": triage_candidates,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser():
    parser = argparse.ArgumentParser(
        prog="authored_content_lint.py",
        description=(
            "Detect planning-process vocabulary (task/decision IDs, review-round "
            "narration, gate verdicts, tracker IDs) leaking into shipped comments "
            "and docstrings."
        ),
    )
    parser.add_argument(
        "--basis", choices=("union", "committed", "whole-tree"), default="union"
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--project-root", default=None, metavar="PATH")
    parser.add_argument("--repo", default=None, metavar="PATH")
    parser.add_argument(
        "--triage",
        action="store_true",
        default=False,
        help="Broad-recall census probe (whole-tree only). Never affects exit code.",
    )
    return parser


def _resolve_repo_from_args(args):
    if args.repo:
        candidate = Path(args.repo).resolve()
        out, _err, rc = _run(["git", "-C", str(candidate), "rev-parse", "--show-toplevel"])
        return Path(out) if (rc == 0 and out) else None
    if args.project_root:
        return resolve_repo_root(args.project_root)
    return resolve_repo_root(Path.cwd())


def _format_text(result):
    lines = [f"basis: {result['basis']}"]
    if result["base_ref"] is not None:
        lines.append(f"base_ref: {result['base_ref']}")
    lines.append(f"tracker_prefixes: {', '.join(result['tracker_prefixes']) or '(none)'}")
    lines.append(f"files_scanned: {result['files_scanned']}")
    if result["triage"]:
        cands = result["candidates"]
        lines.append(f"candidates for triage ({len(cands)}):")
        for c in cands:
            lines.append(f"  {c['file']}:{c['line']}: {c['text']}")
    else:
        findings = result["findings"]
        if not findings:
            lines.append("authored_content_lint: OK — no findings")
        else:
            lines.append(f"findings ({len(findings)}):")
            for f in findings:
                lines.append(f"  {f['file']}:{f['line']}: [{f['tier']}] {f['token']}")
    return "\n".join(lines)


def main(argv=None):
    # Checked before argument parsing so a disabled run never depends on
    # flags being well-formed (mirrors affected_tests.py's disable check).
    if os.environ.get(_ENV_DISABLE) == "1":
        print(json.dumps({"disabled": True, "findings": []}))
        return 0

    parser = _build_parser()
    args = parser.parse_args(argv)  # argparse errors -> SystemExit(2)

    if args.triage and args.basis != "whole-tree":
        parser.error("--triage is only valid with --basis whole-tree")

    try:
        repo_root = _resolve_repo_from_args(args)
        if repo_root is None:
            print("authored_content_lint: no resolvable git repo", file=sys.stderr)
            return 3

        base_ref = None
        if args.basis in ("union", "committed"):
            base_ref = _resolve_base_branch(str(repo_root))
            if base_ref is None:
                print("authored_content_lint: no resolvable base branch", file=sys.stderr)
                return 3

        result = scan(repo_root, args.basis, triage=args.triage, base_ref=base_ref)
    except Undeterminable as exc:
        print(f"authored_content_lint: undeterminable — {exc}", file=sys.stderr)
        return 3
    except Exception as exc:  # noqa: BLE001 — fail-OPEN: never crash the caller
        print(f"authored_content_lint: undeterminable — {exc}", file=sys.stderr)
        return 3

    if args.format == "json":
        print(json.dumps(result))
    else:
        print(_format_text(result))

    if result["triage"]:
        return 0
    return 1 if result["findings"] else 0


if __name__ == "__main__":
    sys.exit(main())
