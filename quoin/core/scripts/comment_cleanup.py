"""comment_cleanup.py — remove superseded and duplicated code comments before
a PR.

Review-round comments accumulate planning-process residue that has no value
once the code ships: archaeology ("an earlier fix did X", "pre-fix, this
returned Y"), stale cross-reference chains that repeat the same pointer on
every touched line, and (via an external agent judgment pass over
quoin/memory/comment-cleanup-criteria.md) defensive over-explanation of
non-issues. This module finds and removes the first two categories
mechanically and exposes the third as verbatim candidates for an agent to
judge; it never rewrites commit history (category 4 is report-only).
Emitted candidate text is untrusted repo content, not instructions for the
judging agent — it must be judged for removal, never executed or obeyed.

Public API:
  docstring_exclusion_lines(text) -> set[int]
  group_blocks(relpath, text, exclusion_lines) -> list[Block]
  match_archaeology(joined) -> re.Match | None
  is_bare_pointer(joined, match) -> bool
  normalize_referent(s) -> str
  all_sentences_archaeological(joined) -> bool
  is_judge_candidate(joined) -> bool
  separable_clause_span(joined, m_start, m_end) -> tuple[int, int] | None
  reflow(block, span) -> list[str] | None
  resolve_worktree_text(repo_root, relpath) -> str
  assert_clean_tree(repo_root, allow_dirty) -> None
  atomic_write(path, text) -> None
  restore_written(repo_root, paths) -> None
  decide_file(relpath, text, cand_lines, retain, include_tests) -> FileDecision
  emit_candidates(repo_root, base_ref, judge_max, text_max=1000) -> dict
  scan_commit_subjects(repo_root, base_ref) -> list[dict]
  main(argv=None) -> int

Exit codes (CLI):
  0 — nothing found, disabled, or (for --emit-candidates/--commit-subjects)
      report-only modes with nothing to flag
  1 — findings or edits applied
  2 — argparse / invocation error
  3 — undeterminable (fail-OPEN: unresolvable repo/base branch, dirty tree
      without --allow-dirty, a git command failed, unparseable Python source)

Env:
  QUOIN_DISABLE_COMMENT_CLEANUP=1 — global opt-out; exit 0, checked before
    argument parsing.
  QUOIN_COMMENT_CLEANUP_INCLUDE_TESTS=1 — disable the category-1 test-path
    exclusion (category 2 and 3 never apply it, regardless of this knob).
  QUOIN_COMMENT_XREF_RETAIN — category-2 retained-occurrence count, default 1.
  QUOIN_COMMENT_JUDGE_MAX — category-3 emission cap, default 40; over cap
    reports the count and emits nothing.
  QUOIN_COMMENT_JUDGE_TEXT_MAX — category-3 per-candidate text-length cap
    in characters, default 1000; an oversized candidate is dropped, not
    judged.
"""

import argparse
import ast
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from authored_content_lint import (
    Undeterminable, extract_comment_regions, match_taxonomy,
    read_text_source, resolve_candidates, resolve_repo_root,
    resolve_tracker_prefixes, _resolve_base_branch,
)

_SUBPROCESS_TIMEOUT = 30
_ENV_DISABLE = "QUOIN_DISABLE_COMMENT_CLEANUP"
_ENV_INCLUDE_TESTS = "QUOIN_COMMENT_CLEANUP_INCLUDE_TESTS"
_ENV_XREF_RETAIN = "QUOIN_COMMENT_XREF_RETAIN"
_ENV_JUDGE_MAX = "QUOIN_COMMENT_JUDGE_MAX"
_ENV_JUDGE_TEXT_MAX = "QUOIN_COMMENT_JUDGE_TEXT_MAX"
_PRAGMA = "quoin-lint: allow"

# Self-exclusion mirrors authored_content_lint._EXCLUDE_PATHS — this module's
# own two paths are also added there (arch D-21) so the lint never flags its
# own vocabulary-adjacent identifiers.
_EXCLUDE_PATHS = frozenset(
    {
        "quoin/core/scripts/comment_cleanup.py",
        "quoin/scripts/comment_cleanup.py",
    }
)


# ---------------------------------------------------------------------------
# Category 1 / 2 discriminators (arch D-03, D-04, D-05)
# ---------------------------------------------------------------------------

# The hyphenated lookahead admits no whitespace: "pre-fix," qualifies,
# "pre-fix line-buffered" does not. The five multi-word phrases are
# unconditional matches regardless of surrounding punctuation.
_ARCH_HYPHENATED_RE = re.compile(r"\b(?:pre|post)-(?:fix|change)(?=[,:.!?])", re.IGNORECASE)
_ARCH_MULTIWORD_RE = re.compile(
    r"an earlier fix|before the fix|previously dropped|used to be|earlier round",
    re.IGNORECASE,
)

_POINTER_RE = re.compile(
    r"\b(?:see|per|cf\.|refer to)\s+"
    r"(?P<ref>[A-Za-z_]\w*(?:[./][A-Za-z_]\w*)+)",
    re.IGNORECASE,
)
_STOCK_FILLER_RE = re.compile(
    r"\A\s*(?:(?:for the full rationale|for details|for why|for the reasoning)\s*)?(?:[.,;:]\s*)?\Z",
    re.IGNORECASE,
)

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

_JUDGE_MARKERS = (
    "this is not",
    "one might think",
    "note that this does not",
    "no, this is not a bug",
    "contrary to",
)

_TEST_PATH_RE = re.compile(r"(^|/)tests/")
_TEST_BASENAME_RE = re.compile(r"^(test_.*\.py|.*_test\.py)$")


def match_archaeology(joined):
    """Earliest-starting archaeology hit, or None."""
    candidates = [m for m in (_ARCH_HYPHENATED_RE.search(joined), _ARCH_MULTIWORD_RE.search(joined)) if m]
    if not candidates:
        return None
    return min(candidates, key=lambda m: m.start())


def is_bare_pointer(joined, match):
    return bool(_STOCK_FILLER_RE.fullmatch(joined[match.end("ref"):]))


def normalize_referent(s):
    return s.lower().rstrip(".,;:")


def all_sentences_archaeological(joined):
    """True iff every non-empty sentence in `joined` carries an archaeology
    hit. Gates whole-block removal only."""
    sentences = [s for s in _SENTENCE_SPLIT_RE.split(joined) if s.strip()]
    if not sentences:
        return False
    return all(match_archaeology(s) is not None for s in sentences)


def is_judge_candidate(joined):
    lowered = joined.lower()
    return any(marker in lowered for marker in _JUDGE_MARKERS)


def _is_test_path(relpath, include_tests):
    if include_tests:
        return False
    if _TEST_PATH_RE.search(relpath):
        return True
    return bool(_TEST_BASENAME_RE.match(Path(relpath).name))


# ---------------------------------------------------------------------------
# T-02 — docstring exclusion set + block grouper
# ---------------------------------------------------------------------------

def docstring_exclusion_lines(text):
    """AST-derived set of every line number covered by a module/class/
    function docstring — never a text-shape heuristic, so a comment-shaped
    line that merely appears inside a docstring (a markdown heading, a `#
    ---` rule) is excluded on the same footing as any other docstring line."""
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        raise Undeterminable(f"unparseable Python source: {exc}") from exc
    exclusion = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if not (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            continue
        start = first.value.lineno
        end = getattr(first.value, "end_lineno", start) or start
        exclusion.update(range(start, end + 1))
    return exclusion


@dataclass
class Block:
    start: int
    end: int
    indent: str
    raw_lines: list
    joined: str
    is_trailing: bool
    code_prefix: str
    width: int


def _strip_marker(text):
    """Strip a leading '#' and at most one following space, then .strip()."""
    s = text.lstrip()
    if s.startswith("#"):
        s = s[1:]
        if s.startswith(" "):
            s = s[1:]
    return s.strip()


def group_blocks(relpath, text, exclusion_lines):
    regions = extract_comment_regions(relpath, text)
    dedup = {}
    for ln, txt in regions:
        dedup.setdefault(ln, txt)
    linenos = sorted(ln for ln in dedup if ln not in exclusion_lines)
    source_lines = text.splitlines()
    is_py = Path(relpath).suffix == ".py"

    blocks = []
    i = 0
    n = len(linenos)
    while i < n:
        ln = linenos[i]
        line = source_lines[ln - 1] if 1 <= ln <= len(source_lines) else ""
        if line.lstrip().startswith("#"):
            indent = line[: len(line) - len(line.lstrip())]
            run = [ln]
            j = i + 1
            while j < n and linenos[j] == run[-1] + 1:
                next_line = source_lines[linenos[j] - 1] if 1 <= linenos[j] <= len(source_lines) else ""
                if not next_line.lstrip().startswith("#"):
                    break
                next_indent = next_line[: len(next_line) - len(next_line.lstrip())]
                if next_indent != indent:
                    break
                run.append(linenos[j])
                j += 1
            raw = [source_lines[l - 1] for l in run]
            joined = " ".join(p for p in (_strip_marker(l) for l in raw) if p)
            blocks.append(
                Block(
                    start=run[0], end=run[-1], indent=indent, raw_lines=raw,
                    joined=joined, is_trailing=False, code_prefix="",
                    width=max(len(l) for l in raw),
                )
            )
            i = j
        else:
            region_text = dedup[ln]
            idx = line.rfind(region_text) if is_py else line.rfind(region_text)
            if idx == -1:
                # Should not happen for a genuine trailing region. Abandon
                # rather than risk truncating the code line at the wrong
                # offset — the line is left untouched, as if never found.
                i += 1
                continue
            code_prefix = line[:idx]
            joined = _strip_marker(region_text)
            blocks.append(
                Block(
                    start=ln, end=ln, indent="", raw_lines=[line],
                    joined=joined, is_trailing=True, code_prefix=code_prefix,
                    width=len(line),
                )
            )
            i += 1
    return blocks


# ---------------------------------------------------------------------------
# T-04 — clause span and reflow (excision engine, shared by categories 1-3)
# ---------------------------------------------------------------------------

# The negative lookbehind pins a match's start to a non-whitespace position
# (or the run boundary immediately before a sentence-ending `\s+` match) —
# without it, an unanchored leading `\s*` re-tries the whole whitespace run
# from every offset inside it whenever the run isn't followed by a separator,
# which is quadratic in run length. Python 3.10 has no possessive quantifier
# (`\s*+`), so the lookbehind is the fix, not a quantifier change.
_SEPARATOR_RE = re.compile(r"(?<!\s)\s*(?:—|–|;|:|,)|(?<=[.!?])\s+")
_LEADING_SEP_CONJ_RE = re.compile(
    r"^\s*(?:—|–|;|:|,)\s*(?:and|or|but|nor|yet|so)\b\s*", re.IGNORECASE
)
_BARE_COMMENT_MARKER_RE = re.compile(r"^#+\s*$")
_PAREN_TAIL_RE = re.compile(r"[.!?\s]*")


def separable_clause_span(joined, m_start, m_end, seps=None):
    # Step 1 — trailing parenthetical. Prefer the tightest enclosing pair
    # that (a) contains the match and (b) is followed only by sentence-
    # ending punctuation and whitespace.
    stack = []
    pairs = []
    for i, ch in enumerate(joined):
        if ch == "(":
            stack.append(i)
        elif ch == ")" and stack:
            pairs.append((stack.pop(), i))
    qualifying = [
        (open_idx, close_idx)
        for open_idx, close_idx in pairs
        if open_idx <= m_start
        and m_end <= close_idx + 1
        and _PAREN_TAIL_RE.fullmatch(joined[close_idx + 1 :])
    ]
    if qualifying:
        open_idx, close_idx = min(qualifying, key=lambda p: p[1] - p[0])
        # Absorb preceding whitespace into the span so it never survives
        # into the residue as an orphaned space before the terminal
        # punctuation.
        while open_idx > 0 and joined[open_idx - 1].isspace():
            open_idx -= 1
        return (open_idx, close_idx + 1)

    # Step 2 — else the last separator before the match. `seps` is the full
    # finditer result over `joined`, precomputed once per block by callers
    # that probe multiple matches against the same text (avoids re-scanning
    # `joined` once per archaeological hit).
    if seps is None:
        seps = _SEPARATOR_RE.finditer(joined)
    candidates = [sm for sm in seps if sm.start() < m_start]
    if not candidates:
        # Step 3: no separator precedes the match — nothing to excise.
        return None
    span_start = candidates[-1].start()

    # Right edge clamped at the first sentence boundary at or after the
    # match end, or the end of the block if none exists — a trailing
    # forward-acting sentence stays in the residue instead of being
    # excised alongside the archaeological clause.
    boundaries = [bm for bm in _SENTENCE_SPLIT_RE.finditer(joined) if bm.start() >= m_end]
    span_end = boundaries[0].start() if boundaries else len(joined)
    return (span_start, span_end)


def _apply_excision_step(joined, span):
    """reflow steps 1-2 for a single span: seam-repair the join, then strip
    a leading separator+conjunction remnant at the seam.

    Seam repair fires only when the excised span left a real, substantive
    tail — the clamp branch always does; the trailing-parenthetical branch
    never does, because step 1 above already guaranteed its tail is only
    sentence-ending punctuation and whitespace (so the lstripped tail is
    empty, or starts with one of .!? and the clause is a no-op there).
    """
    start, end = span
    tail = joined[end:]
    tail_lstripped = tail.lstrip()
    seam_needed = bool(tail_lstripped) and tail_lstripped[0] not in ".!?"
    head = joined[:start]
    if seam_needed and not head.rstrip().endswith((".", "!", "?")):
        residue = head.rstrip() + "." + tail
        join_at = len(head.rstrip()) + 1
    else:
        residue = head + tail
        join_at = len(head)

    m = _LEADING_SEP_CONJ_RE.match(residue[join_at:])
    if m:
        residue = residue[:join_at] + residue[join_at + m.end() :]
    return residue


def reflow(block, span):
    """Excise `span` (a single (start, end) tuple, or a list of such tuples
    for the rare case where both categories cut disjoint spans from the same
    block — arch D-24) from `block.joined` and return the block's
    replacement lines, or None if the excision is abandoned."""
    spans = [span] if isinstance(span, tuple) else list(span)
    residue = block.joined
    for s in sorted(spans, key=lambda sp: -sp[0]):
        residue = _apply_excision_step(residue, s)

    residue = residue.strip()

    if not block.is_trailing:
        if not residue or _BARE_COMMENT_MARKER_RE.match(residue):
            return None

    fully_excised_trailing = block.is_trailing and not residue
    if fully_excised_trailing:
        return [block.code_prefix.rstrip()]

    # Steps 4-5 apply to whole-line blocks and partially-excised trailing
    # blocks; only a fully-excised trailing block skips straight here. A
    # directive-shaped residue (e.g. a leftover "pyright:") is left alone —
    # appending a period would turn a tool directive into prose punctuation
    # no tool recognizes.
    if residue[-1] not in ".!?" and not _DIRECTIVE_RE.search(residue):
        residue = residue + "."

    if block.is_trailing:
        width = block.width - len(block.code_prefix) - 2
    else:
        width = block.width - len(block.indent) - 2
    wrapped = textwrap.wrap(residue, width=max(width, 1)) or [residue]

    if block.is_trailing:
        if len(wrapped) != 1:
            # A trailing comment has no continuation line to wrap onto.
            return None
        return [block.code_prefix + "# " + wrapped[0]]

    return [block.indent + "# " + line for line in wrapped]


def _overlaps(a, b):
    return a[0] < b[1] and b[0] < a[1]


# ---------------------------------------------------------------------------
# T-05 — git and filesystem layer
# ---------------------------------------------------------------------------

def _run(args):
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT)
        return proc.stdout.strip(), proc.stderr.strip(), proc.returncode
    except subprocess.TimeoutExpired:
        return "", "timeout", 1
    except FileNotFoundError:
        return "", "git not found", 1
    except OSError as exc:
        return "", str(exc), 1


def resolve_worktree_text(repo_root, relpath):
    """Always the worktree file — never read_text_source(..., 'committed').

    Reads strictly as UTF-8. A file with non-UTF-8 bytes anywhere — even on
    a line this pass never touches — must not be silently transcoded:
    reading with `errors="replace"` and then rewriting the whole file as
    UTF-8 turns every undecodable byte into U+FFFD, changing live string
    literals outside the edited region. Raises `UnicodeDecodeError` (not
    `Undeterminable`) on a decode failure so callers can distinguish "this
    one file can't be read" from "the whole run is undeterminable" — the
    CLI's per-file loop marks the file undeterminable and continues with
    the rest of the run instead of aborting it.
    """
    full = Path(repo_root) / relpath
    try:
        return full.read_text(encoding="utf-8")
    except OSError as exc:
        raise Undeterminable(f"cannot read {relpath}: {exc}") from exc


def assert_clean_tree(repo_root, allow_dirty):
    if allow_dirty:
        return
    out, err, rc = _run(["git", "-C", str(repo_root), "status", "--porcelain"])
    if rc != 0:
        raise Undeterminable(f"git status failed: {err}")
    if out:
        raise Undeterminable("worktree not clean at entry")


def atomic_write(path, text):
    """Mirrors quoin/core/scripts/run_state.py's mkstemp atomic-write
    variant (def _atomic_write_record at line 161, closing pass at line
    180) — finally: + a guarded unlink, not except BaseException.

    Unlike run_state.py's own record file, the destination here is a
    pre-existing user source file, so its mode must survive the rewrite:
    mkstemp always creates the temp file 0600, and os.replace carries that
    mode onto the destination unless it is corrected first — left alone,
    every cleaned file narrows (0644 -> 0600, or an executable entrypoint
    loses its exec bit).

    Refuses a non-regular destination. `mkstemp` writes into the parent
    directory and `os.replace` swaps whatever name currently occupies
    `path` — for a symlink that replaces the link itself with a regular
    file, silently destroying it while its (possibly out-of-repo) target
    is left untouched. Callers should already skip symlinked candidates
    before reaching here; this is the backstop for anything that doesn't.
    Also refuses to write when the destination's current on-disk bytes are
    not valid UTF-8 — a second guard against the same corruption
    `resolve_worktree_text`'s strict read exists to prevent, in case some
    future caller feeds this function a path it didn't read that way.
    """
    path = Path(path)
    if path.is_symlink():
        raise Undeterminable(f"refusing to write through a symlink: {path}")
    try:
        original_stat = path.stat()
    except OSError:
        original_stat = None
    if original_stat is not None and not stat.S_ISREG(original_stat.st_mode):
        raise Undeterminable(f"refusing to write to a non-regular destination: {path}")
    original_mode = original_stat.st_mode & 0o777 if original_stat is not None else None
    try:
        original_bytes = path.read_bytes()
    except OSError:
        original_bytes = None
    if original_bytes is not None:
        try:
            original_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise Undeterminable(
                f"refusing to write {path}: destination is not valid UTF-8 ({exc})"
            ) from exc
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        if original_mode is not None:
            os.chmod(tmp_name, original_mode)
        os.replace(tmp_name, str(path))
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def restore_written(repo_root, paths, cause=None):
    """Mid-run failure recovery (arch D-15): restore already-written paths
    to their committed state, then re-raise so /pr can report exactly which
    files were touched and rolled back.

    `git checkout HEAD -- <paths>` is all-or-nothing: if any one pathspec is
    absent from HEAD, the whole checkout aborts and every other listed file
    is left modified. Check the return code and, on failure, fall back to
    restoring paths one at a time so the report names exactly which files
    were and were not recovered — never a blanket claimed success. `cause`
    (the exception that triggered the write failure, if known) is chained
    via `from` so the operator still learns why the write failed, not only
    that a restore was attempted. `cause=None` skips the `from` clause
    entirely rather than passing it as `from None`, which would explicitly
    suppress the currently-active exception's implicit context.
    """
    _out, err, rc = _run(["git", "-C", str(repo_root), "checkout", "HEAD", "--", *paths])
    if rc == 0:
        msg = f"restored after write failure: {', '.join(paths)}"
        if cause is not None:
            raise Undeterminable(msg) from cause
        raise Undeterminable(msg)

    restored, failed = [], []
    for p in paths:
        _o, _e, prc = _run(["git", "-C", str(repo_root), "checkout", "HEAD", "--", p])
        (restored if prc == 0 else failed).append(p)
    msg = (
        f"restore after write failure was incomplete ({err}) — "
        f"restored: {', '.join(restored) or 'none'}; "
        f"NOT restored (still modified): {', '.join(failed) or 'none'}"
    )
    if cause is not None:
        raise Undeterminable(msg) from cause
    raise Undeterminable(msg)


# ---------------------------------------------------------------------------
# T-06 — categories 1 and 2: decide and apply
# ---------------------------------------------------------------------------

@dataclass
class FileDecision:
    relpath: str
    new_text: object
    changed: bool


def _all_archaeology_matches(joined):
    """Every non-overlapping archaeology hit in `joined`, left to right —
    unlike match_archaeology (earliest hit only), this drives category 1's
    excision so a block carrying two or more archaeological sentences
    converges in a single decide_file pass instead of needing one pass per
    hit (each pass only ever excised the earliest one)."""
    raw = sorted(
        list(_ARCH_HYPHENATED_RE.finditer(joined)) + list(_ARCH_MULTIWORD_RE.finditer(joined)),
        key=lambda m: m.start(),
    )
    matches = []
    last_end = -1
    for m in raw:
        if m.start() >= last_end:
            matches.append(m)
            last_end = m.end()
    return matches


def decide_category_1(block, cand_lines, relpath, include_tests):
    matches = _all_archaeology_matches(block.joined)
    if not matches:
        return None
    if cand_lines is not None and not any(ln in cand_lines for ln in range(block.start, block.end + 1)):
        return None
    if _is_test_path(relpath, include_tests):
        return None

    # Try clause excision on the earliest hit first. Whole-block removal is
    # the fallback, taken only when that earliest hit has no separable
    # clause of its own (the phrase leads the comment with nothing before it
    # to cut against) — and only for a genuinely whole-line block. A
    # trailing block's "whole block" is the code line it rides on
    # (decide_file's apply step deletes the physical line for a remove_block
    # edit), so a trailing block never qualifies here; it falls through to
    # the excise attempt below instead, where reflow correctly strips only
    # the comment and leaves the code.
    seps = list(_SEPARATOR_RE.finditer(block.joined))
    earliest = matches[0]
    earliest_span = separable_clause_span(block.joined, earliest.start(), earliest.end(), seps)
    if (
        earliest_span is None
        and not block.is_trailing
        and all_sentences_archaeological(block.joined)
    ):
        return ("remove_block", (0, len(block.joined)))

    # Accumulate a clause span per archaeological hit (not just the earliest
    # one) so a block carrying two or more archaeological sentences excises
    # all of them in this single pass instead of needing one pass per hit.
    spans = []
    for m in matches:
        span = earliest_span if m is earliest else separable_clause_span(block.joined, m.start(), m.end(), seps)
        if span is not None and not any(_overlaps(span, existing) for existing in spans):
            spans.append(span)
    if spans:
        return ("excise", spans)
    return ("report", None)


def _category2_verdicts(blocks, cand_lines, retain):
    """Category 2 for a whole file at once: retention is ordered over every
    pointer block in the post-image file, so it cannot be decided per block
    in isolation (arch D-07 — an occurrence on an unchanged line still
    anchors the count)."""
    pointer_blocks = []
    for block in blocks:
        m = _POINTER_RE.search(block.joined)
        if m:
            pointer_blocks.append((block, m))
    pointer_blocks.sort(key=lambda pair: pair[0].start)

    seen = {}
    verdicts = {}
    for block, m in pointer_blocks:
        key = normalize_referent(m.group("ref"))
        count = seen.get(key, 0)
        seen[key] = count + 1
        pointer_span = (m.start(), m.end())
        if count < retain:
            verdicts[id(block)] = ("report", pointer_span)
            continue
        eligible = is_bare_pointer(block.joined, m) and (
            cand_lines is None or any(ln in cand_lines for ln in range(block.start, block.end + 1))
        )
        span = separable_clause_span(block.joined, m.start(), m.end()) if eligible else None
        if eligible and span is not None:
            verdicts[id(block)] = ("excise", span)
        else:
            verdicts[id(block)] = ("report", pointer_span)
    return verdicts


def _has_pragma(block):
    return any(_PRAGMA in line for line in block.raw_lines)


# A tool-directive comment (a type-checker/linter/formatter instruction) must
# never be reflowed into free prose alongside an adjacent archaeological
# comment — grouping and reflow both operate on raw text with no notion of
# directive syntax, so any block containing one is vetoed outright, the same
# way _has_pragma vetoes a block carrying the opt-out marker.
_DIRECTIVE_RE = re.compile(
    r"\b(?:type|pragma|fmt|pylint|flake8|mypy|ruff|isort|pyright|pytype|yapf|skipcq)\s*:"
    r"|\bnoqa\b|\bcoding[:=]|\bnosec\b|\bnoinspection\b|\bsourcery\s+skip\s*:",
    re.IGNORECASE,
)


def _has_directive(block):
    return any(_DIRECTIVE_RE.search(line) for line in block.raw_lines)


def decide_file(relpath, text, cand_lines, retain, include_tests):
    """One pass, decisions computed from the pre-edit state."""
    is_py = Path(relpath).suffix == ".py"
    exclusion = docstring_exclusion_lines(text) if is_py else set()
    blocks = group_blocks(relpath, text, exclusion)
    blocks = [b for b in blocks if not _has_pragma(b) and not _has_directive(b)]

    cat2 = _category2_verdicts(blocks, cand_lines, retain)

    edits = {}
    for block in blocks:
        cat1 = decide_category_1(block, cand_lines, relpath, include_tests)
        v2 = cat2.get(id(block))

        proposals = [p for p in (cat1, v2) if p is not None]
        cut_spans = []
        for v, s in proposals:
            if v == "remove_block":
                cut_spans.append((0, len(block.joined)))
            elif v == "excise":
                # Category 1 may hand back a list of non-overlapping spans
                # (one per archaeological sentence in the block); category 2
                # always hands back a single span.
                cut_spans.extend(s if isinstance(s, list) else [s])
        keep_spans = [s for v, s in proposals if v == "report" and s is not None]

        # A keep vetoes only an overlapping span, never a disjoint one.
        for keep_span in keep_spans:
            cut_spans = [s for s in cut_spans if not _overlaps(s, keep_span)]

        if not cut_spans:
            continue

        is_remove_block = (
            cat1 is not None
            and cat1[0] == "remove_block"
            and cat1[1] in cut_spans
            and not block.is_trailing
        )
        if is_remove_block:
            edits[block.start] = ("remove", None)
        else:
            replacement = reflow(block, cut_spans)
            if replacement is None:
                continue
            edits[block.start] = ("excise", replacement)

    if not edits:
        return FileDecision(relpath=relpath, new_text=None, changed=False)

    lines = text.splitlines(keepends=True)
    # Apply block edits in descending block.start order so a later block's
    # edit never shifts the still-to-be-applied offsets of an earlier one.
    for block in sorted(blocks, key=lambda b: -b.start):
        if block.start not in edits:
            continue
        kind, payload = edits[block.start]
        if kind == "remove":
            del lines[block.start - 1 : block.end]
        else:
            trailing_nl = "\n" if lines[block.end - 1].endswith("\n") else ""
            replacement_lines = [l + trailing_nl for l in payload]
            lines[block.start - 1 : block.end] = replacement_lines

    new_text = "".join(lines)
    if is_py:
        # Cheap defense-in-depth for a tool that rewrites and auto-commits
        # source: never hand back an edit that would leave the file
        # unparseable, whatever the reason. Fails loud (Undeterminable)
        # rather than writing corrupted text.
        try:
            ast.parse(new_text)
        except SyntaxError as exc:
            raise Undeterminable(
                f"cleanup of {relpath} would produce unparseable Python ({exc}); aborting this file's edit"
            ) from exc
    return FileDecision(relpath=relpath, new_text=new_text, changed=(new_text != text))


# ---------------------------------------------------------------------------
# T-07 — category 3 emission and category 4 scan
# ---------------------------------------------------------------------------

def emit_candidates(repo_root, base_ref, judge_max, text_max=1000):
    """File-scoped, not line-scoped (arch D-11): runs over the current
    worktree, so every surviving block in a touched file is reachable, not
    only diff-scoped ones. Bounded by is_judge_candidate and judge_max.

    `text_max` caps each candidate's `text` length in characters — a block
    longer than that is dropped rather than truncated, so the judging agent
    never sees a partial fragment that could read as something else. This
    is independent of judge_max, which caps the candidate *count*: a single
    oversized comment must not be able to smuggle a long, adversarial
    payload past the judge just because the block count stays low.
    """
    candidates, _merge_base = resolve_candidates(repo_root, "committed", base_ref)
    found = []
    for relpath in sorted(candidates.keys()):
        if Path(relpath).suffix != ".py":
            continue
        full_path = Path(repo_root) / relpath
        if full_path.is_symlink():
            continue
        try:
            text = resolve_worktree_text(repo_root, relpath)
        except UnicodeDecodeError:
            continue
        exclusion = docstring_exclusion_lines(text)
        blocks = group_blocks(relpath, text, exclusion)
        for block in blocks:
            if _has_pragma(block) or _has_directive(block):
                continue
            if _is_test_path(relpath, include_tests=False):
                continue
            if len(block.joined) > text_max:
                continue
            if is_judge_candidate(block.joined):
                found.append(
                    {"file": relpath, "start": block.start, "end": block.end, "text": block.joined}
                )
    if len(found) > judge_max:
        return {"count": len(found), "candidates": []}
    return {"count": len(found), "candidates": found}


def _current_branch_name(repo_root):
    out, _err, rc = _run(["git", "-C", str(repo_root), "rev-parse", "--abbrev-ref", "HEAD"])
    return out if rc == 0 else ""


def scan_commit_subjects(repo_root, base_ref):
    """Report-only: never rewrites, amends or rebases."""
    out, err, rc = _run(["git", "-C", str(repo_root), "log", "--format=%s", f"{base_ref}..HEAD"])
    if rc != 0:
        raise Undeterminable(f"git log failed: {err}")
    prefixes = resolve_tracker_prefixes(_current_branch_name(repo_root))
    findings = []
    for subject in out.splitlines():
        match = match_taxonomy(subject, prefixes)
        if match is not None:
            tier, token = match
            findings.append({"subject": subject, "token": token, "tier": tier})
    return findings


# ---------------------------------------------------------------------------
# T-08 — CLI surface
# ---------------------------------------------------------------------------

def _build_parser():
    parser = argparse.ArgumentParser(
        prog="comment_cleanup.py",
        description="Remove superseded and duplicated code comments before a PR.",
    )
    parser.add_argument("--base", default=None, metavar="BASE_REF")
    parser.add_argument("--basis", choices=("committed", "union"), default="committed")
    parser.add_argument("--apply", action="store_true", default=False)
    parser.add_argument("--emit-candidates", action="store_true", default=False)
    parser.add_argument("--allow-dirty", action="store_true", default=False)
    parser.add_argument("--commit-subjects", action="store_true", default=False)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--project-root", default=None, metavar="PATH")
    parser.add_argument("--repo", default=None, metavar="PATH")
    parser.add_argument(
        "--paths",
        nargs="+",
        default=None,
        metavar="PATH",
        help="Dogfood escape hatch: explicit relpaths, cand_lines=None, bypassing resolve_candidates.",
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


def _print_result(result, fmt):
    """Emit `result` on stdout in the requested format. Shared by the
    success path and by both undeterminable-exit branches in `main` so an
    exit 3 still reports which files were written or skipped, instead of
    leaving the caller with an empty stdout and no way to identify the
    files a mid-run failure touched."""
    if fmt == "json":
        print(json.dumps(result))
    else:
        print(_format_text(result))


def _format_text(result):
    lines = [f"base_ref: {result.get('base_ref')}"]
    if "error" in result:
        lines.append(f"error: {result['error']}")
    if "written" in result:
        lines.append(f"written before failure ({len(result['written'])}):")
        for f in result["written"]:
            lines.append(f"  {f}")
    if "undeterminable_files" in result:
        lines.append(f"undeterminable files ({len(result['undeterminable_files'])}):")
        for f in result["undeterminable_files"]:
            lines.append(f"  {f['file']}: {f['reason']}")
    if "decisions" in result:
        changed = [d for d in result["decisions"] if d["changed"]]
        if not changed:
            lines.append("comment_cleanup: OK — nothing to remove")
        else:
            lines.append(f"files changed ({len(changed)}):")
            for d in changed:
                lines.append(f"  {d['file']}")
    if "emit_candidates" in result:
        out = result["emit_candidates"]
        lines.append(f"category-3 candidates: {out['count']}")
        for c in out["candidates"]:
            lines.append(f"  {c['file']}:{c['start']}-{c['end']}: {c['text']}")
    if "commit_subjects" in result:
        findings = result["commit_subjects"]
        lines.append(f"commit-subject findings ({len(findings)}):")
        for f in findings:
            lines.append(f"  [{f['tier']}] {f['token']}: {f['subject']}")
    return "\n".join(lines)


def main(argv=None):
    if os.environ.get(_ENV_DISABLE) == "1":
        print(json.dumps({"disabled": True}))
        return 0

    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.allow_dirty and not args.emit_candidates:
        parser.error("--allow-dirty is only valid with --emit-candidates")

    result = {}
    try:
        repo_root = _resolve_repo_from_args(args)
        if repo_root is None:
            print("comment_cleanup: no resolvable git repo", file=sys.stderr)
            return 3

        base_ref = args.base or _resolve_base_branch(str(repo_root))
        if base_ref is None:
            print("comment_cleanup: no resolvable base branch", file=sys.stderr)
            return 3

        retain = int(os.environ.get(_ENV_XREF_RETAIN, "1"))
        include_tests = os.environ.get(_ENV_INCLUDE_TESTS) == "1"
        judge_max = int(os.environ.get(_ENV_JUDGE_MAX, "40"))
        text_max = int(os.environ.get(_ENV_JUDGE_TEXT_MAX, "1000"))

        result["base_ref"] = base_ref
        exit_code = 0

        if args.emit_candidates:
            assert_clean_tree(repo_root, args.allow_dirty)
            out = emit_candidates(repo_root, base_ref, judge_max, text_max)
            result["emit_candidates"] = out
            if out["count"] > judge_max or out["candidates"]:
                exit_code = max(exit_code, 1)

        if args.commit_subjects:
            findings = scan_commit_subjects(repo_root, base_ref)
            result["commit_subjects"] = findings
            if findings:
                exit_code = max(exit_code, 1)

        if not args.emit_candidates and not args.commit_subjects:
            assert_clean_tree(repo_root, allow_dirty=False)
            if args.paths:
                candidates = {p: None for p in args.paths}
            else:
                candidates, _merge_base = resolve_candidates(repo_root, args.basis, base_ref)
                candidates = {
                    f: lines for f, lines in candidates.items() if f not in _EXCLUDE_PATHS
                }
            decisions = []
            written = []
            undeterminable_files = []
            try:
                for relpath in sorted(candidates.keys()):
                    cand_lines = candidates[relpath]
                    full_path = repo_root / relpath
                    if full_path.is_symlink():
                        undeterminable_files.append({"file": relpath, "reason": "symlink"})
                        continue
                    try:
                        text = resolve_worktree_text(repo_root, relpath)
                    except UnicodeDecodeError as exc:
                        # One file that isn't valid UTF-8 marks itself
                        # undeterminable and is skipped — it must not zero
                        # out decisions already made for the rest of the run
                        # by propagating to the except Exception below.
                        undeterminable_files.append({"file": relpath, "reason": str(exc)})
                        continue
                    decision = decide_file(relpath, text, cand_lines, retain, include_tests)
                    if decision.changed:
                        decisions.append(decision)
                        if args.apply and Path(relpath).suffix == ".py":
                            atomic_write(repo_root / relpath, decision.new_text)
                            written.append(relpath)
            except Exception as exc:
                result["written"] = written
                if undeterminable_files:
                    result["undeterminable_files"] = undeterminable_files
                if written:
                    restore_written(repo_root, written, exc)
                raise
            result["decisions"] = [{"file": d.relpath, "changed": d.changed} for d in decisions]
            if undeterminable_files:
                result["undeterminable_files"] = undeterminable_files
            if decisions:
                exit_code = max(exit_code, 1)

    except Undeterminable as exc:
        print(f"comment_cleanup: undeterminable — {exc}", file=sys.stderr)
        result["error"] = str(exc)
        _print_result(result, args.format)
        return 3
    except Exception as exc:  # noqa: BLE001 — fail-OPEN: never crash the caller
        print(f"comment_cleanup: undeterminable — {exc}", file=sys.stderr)
        result["error"] = str(exc)
        _print_result(result, args.format)
        return 3

    _print_result(result, args.format)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
