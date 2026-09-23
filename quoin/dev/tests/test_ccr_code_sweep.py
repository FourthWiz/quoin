"""AC-39/AC-40 repo-wide `ccr code` sweep, and AC-42 CHANGELOG byte-identity.

Every tracked file mentioning `ccr code` must fall into exactly one of four
buckets, or the sweep is red:

  1. Sanctioned exception — this file's own assertions, plus a small,
     enumerated set of other test files, each with its occurrence count
     pinned so a new instance reddens.
  2. Part-B-governed — `src/quoin/ccr_config.py` and `src/quoin/router.py`,
     excluded from the proximity check below and proven instead by parsing
     every non-docstring string literal in `src/quoin`.
  3. Proximity-satisfying rendered surfaces — every mention must sit within
     a bounded character window of the corrected v3 command.
  4. `CHANGELOG.md`, scoped to `## [Unreleased]` only — text before that
     header is historical and exempt outright; text inside it must either
     carry version-aware guidance or match one of the two entries this task
     pins byte-identical (AC-42).

A file that matches none of the four buckets fails the sweep.
"""
from __future__ import annotations

import ast
import hashlib
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "src"))

from quoin.ccr_config import V2_LAUNCH_COMMAND, V3_LAUNCH_COMMAND  # noqa: E402


def _tracked_files_mentioning_ccr_code() -> list[str]:
    """Mechanical, repo-wide — not a hand-picked glob (that was the AC-39
    defect this sweep replaces: a 3-extension glob silently excluded any
    other tracked file type)."""
    out = subprocess.run(
        ["git", "grep", "-l", "ccr code", "--", "."],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    # `git grep` exits 1, not 0, when the pattern matches zero files — that
    # is a legitimate (if surprising) sweep result, not a command failure,
    # so it must not error the test before the assertions below get a
    # chance to report it.
    assert out.returncode in (0, 1), f"git grep exited {out.returncode}: {out.stderr}"
    return sorted(out.stdout.splitlines())


# ── Bucket definitions ──────────────────────────────────────────────────────

# Bucket 1 — sanctioned exception, enumerated (not a directory wildcard):
# this file's own assertions (count not pinned — it changes with the
# assertions), plus these, each with its occurrence count pinned so a new
# instance reddens and must be consciously re-pinned. Unit is str.count
# occurrences, not git grep -c's line count.
_BUCKET_1_SELF = "quoin/dev/tests/test_ccr_code_sweep.py"
_BUCKET_1_COUNTS = {
    "quoin/dev/tests/test_models.py": 11,
    "quoin/dev/tests/test_router_setup.py": 4,
    "quoin/dev/tests/test_launch_guidance.py": 3,
    "quoin/dev/tests/test_agentdesk_zsh_args.py": 4,
}

# Bucket 2 — Part-B-governed; excluded from the proximity check below.
_BUCKET_2 = {"src/quoin/ccr_config.py", "src/quoin/router.py"}

# Bucket 3 — proximity-satisfying rendered surfaces, per-file counts pinned.
_BUCKET_3_COUNTS = {
    "README.md": 5,
    "quoin/CLAUDE.md": 1,
    "quoin/memory/workflow-catalog.md": 1,
    "quoin/tools/agentdesk/agentdesk.zsh": 1,
}
_PROXIMITY_WINDOW = 250

# Bucket 4 — CHANGELOG.md, version-aware-guidance rule, scoped to
# "## [Unreleased]" only. Entries under a released-version header are
# historical and excluded outright, per AC-39's own exception.
_BUCKET_4_FILE = "CHANGELOG.md"
_CHANGELOG_V3_GUIDANCE_MARKER = "no `ccr code` subcommand"
# T-02's pinned historical-entry anchors:
# sha256((line + "\n").encode("utf-8")) of the exact CHANGELOG.md line.
_CHANGELOG_PINNED_ANCHORS = {
    "d4364f4d928027f252e38ff56eaec6ca1f9562970d39012df29c908425a02f57",  # IVG-243 / v0.18.6
    "3e409fdbd42a70a19c41702fda95f50380d0791429d30a6181a3ac5c8f9318d1",  # IVG-64
}


def test_every_tracked_ccr_code_mention_is_classified() -> None:
    files = _tracked_files_mentioning_ccr_code()
    unclassified = [
        f
        for f in files
        if f != _BUCKET_1_SELF
        and f not in _BUCKET_1_COUNTS
        and f not in _BUCKET_2
        and f not in _BUCKET_3_COUNTS
        and f != _BUCKET_4_FILE
    ]
    assert unclassified == [], f"unclassified `ccr code` mentions: {unclassified}"


def test_bucket_1_occurrence_counts_pinned() -> None:
    for rel, expected in _BUCKET_1_COUNTS.items():
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert text.count("ccr code") == expected, rel


@pytest.mark.parametrize("rel", list(_BUCKET_3_COUNTS))
def test_bucket_3_count_and_proximity(rel: str) -> None:
    text = (REPO_ROOT / rel).read_text(encoding="utf-8")
    assert text.count("ccr code") == _BUCKET_3_COUNTS[rel], rel
    for m in re.finditer(re.escape("ccr code"), text):
        window = text[max(0, m.start() - _PROXIMITY_WINDOW): m.end() + _PROXIMITY_WINDOW]
        assert V3_LAUNCH_COMMAND in window, (
            f"{rel}: 'ccr code' at offset {m.start()} has no {V3_LAUNCH_COMMAND!r} "
            f"within {_PROXIMITY_WINDOW} chars"
        )


def _changelog_unreleased_section() -> tuple[list[str], int]:
    """Lines of the '## [Unreleased]' section body (header excluded), and
    the 1-based line number the body starts at."""
    lines = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8").splitlines(keepends=True)
    start = next(i for i, ln in enumerate(lines) if ln.strip() == "## [Unreleased]")
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("## [")),
        len(lines),
    )
    return lines[start + 1 : end], start + 2


def test_bucket_4_changelog_unreleased_entries_are_governed() -> None:
    section_lines, first_lineno = _changelog_unreleased_section()
    hits = [
        (first_lineno + i, line) for i, line in enumerate(section_lines) if "ccr code" in line
    ]
    assert len(hits) == 2, (
        f"expected 2 `ccr code` occurrences under ## [Unreleased], found {len(hits)}"
    )
    for lineno, line in hits:
        digest = hashlib.sha256((line.rstrip("\n") + "\n").encode("utf-8")).hexdigest()
        marker_ok = _CHANGELOG_V3_GUIDANCE_MARKER in line
        anchor_ok = digest in _CHANGELOG_PINNED_ANCHORS
        assert marker_ok or anchor_ok, (
            f"CHANGELOG.md line {lineno}: neither the guidance marker nor a pinned "
            "content anchor — an uncorrected `ccr code` mention under ## [Unreleased]"
        )


def test_bucket_4_text_before_unreleased_is_exempt_outright() -> None:
    text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    start = text.index("## [Unreleased]")
    before = text[:start]
    assert "ccr code" not in before


# ── Part B: exactly one non-docstring string literal in src/quoin ──────────


def _docstring_constant_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body:
                first = body[0]
                if (
                    isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)
                ):
                    ids.add(id(first.value))
    return ids


def test_part_b_exactly_one_non_docstring_literal_contains_ccr_code() -> None:
    src_dir = REPO_ROOT / "src" / "quoin"
    hits: list[tuple[str, str]] = []
    for py_file in sorted(src_dir.glob("*.py")):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        docstring_ids = _docstring_constant_ids(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstring_ids
                and "ccr code" in node.value
            ):
                hits.append((py_file.name, node.value))
    assert hits == [("ccr_config.py", V2_LAUNCH_COMMAND)]


# ── T-02 (AC-42): CHANGELOG byte-identity ───────────────────────────────────


def _changelog_lines() -> list[str]:
    return (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8").splitlines(keepends=True)


def _sha256_line(line: str) -> str:
    return hashlib.sha256((line.rstrip("\n") + "\n").encode("utf-8")).hexdigest()


@pytest.mark.parametrize("anchor", sorted(_CHANGELOG_PINNED_ANCHORS))
def test_pinned_changelog_entry_occurs_exactly_once_byte_identical(anchor: str) -> None:
    """AC-42: the two `ccr code` CHANGELOG entries are byte-identical to
    their pre-task content, and no line-number assertion is made — a
    line-keyed test would already be red today (both entries moved once,
    while staying byte-identical, when the v3 entry above them was
    inserted)."""
    matches = [ln for ln in _changelog_lines() if _sha256_line(ln) == anchor]
    assert len(matches) == 1, f"anchor {anchor} matched {len(matches)} lines, expected 1"


def test_v3_entry_exists_under_unreleased_added_naming_config_sqlite() -> None:
    section_lines, _ = _changelog_unreleased_section()
    added_idx = next(i for i, ln in enumerate(section_lines) if ln.strip() == "### Added")
    # Bounded at the next "### " subsection header, not the end of the whole
    # Unreleased section — which holds several unrelated "### Added"/"###
    # Fixed" blocks below this one, any of which could contain the literal
    # string and make the assertion pass regardless of what this specific
    # entry says.
    added_end = next(
        (i for i in range(added_idx + 1, len(section_lines)) if section_lines[i].startswith("### ")),
        len(section_lines),
    )
    added_block = "".join(section_lines[added_idx:added_end])
    assert "config.sqlite" in added_block
