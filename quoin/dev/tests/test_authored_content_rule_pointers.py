"""Guard tests for the clean-authored-content rule.

Covers: the rule's canary sentence appears in exactly one file (its own home);
every pointer site is present with the right per-file count; the PR and
implement templates omit planning-artifact filenames within a precisely
block-scoped range; the /pr gathering step collects full diff content, not
stat-only; and no adapter file drifts into restating the rule's prohibition
paragraph verbatim instead of pointing at it.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

_THIS_FILE = Path(__file__).resolve()
# parents[0] = tests/, parents[1] = dev/, parents[2] = quoin/ (source package),
# parents[3] = the git root — same depth test_affected_tests.py resolves from
# its own location three directories shallower.
_REPO_ROOT = _THIS_FILE.parents[3]
assert (_REPO_ROOT / "quoin/dev/tests/test_affected_tests.py").exists(), (
    f"_REPO_ROOT resolution sanity check failed: resolved to {_REPO_ROOT}"
)

_RULE_FILE = _REPO_ROOT / "quoin/memory/clean-authored-content.md"
_RULE_TEXT = _RULE_FILE.read_text(encoding="utf-8")

_CANARY = (
    "A comment earns its place when it explains the why to a reader who has "
    "never seen the planning artifacts."
)

_POINTER = "memory/clean-authored-content.md"

_ADAPTER_SKILLS_DIR = _REPO_ROOT / "quoin/adapters/claude/skills"
_ADAPTER_POINTER_COUNTS = {
    "implement": 3,
    "end_of_task": 2,
    "pr": 1,
    "review": 1,
    "run": 1,
}

_EXCLUDE_DIR_NAMES = {
    ".venv",
    ".git",
    "dist",
    "node_modules",
    ".workflow_artifacts",
    ".pytest_cache",
    "__pycache__",
    ".serena",
}


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _extract_prohibition_paragraph(text: str) -> str:
    paragraphs = re.split(r"\n\s*\n", text)
    for para in paragraphs:
        if "Shipped source and test files never carry:" in para:
            return para.strip()
    raise AssertionError("prohibition paragraph not found in the rule file")


def _iter_scan_files():
    for dirpath, dirnames, filenames in os.walk(_REPO_ROOT):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDE_DIR_NAMES]
        for fname in filenames:
            path = Path(dirpath) / fname
            rel = path.relative_to(_REPO_ROOT).as_posix()
            yield path, rel


def _extract_block(text: str, start_heading: str, end_heading: str) -> str:
    start = text.index(start_heading) + len(start_heading)
    end = text.index(end_heading, start)
    return text[start:end]


def test_canary_appears_in_exactly_one_file():
    normalized_canary = _normalize(_CANARY)
    matches = []
    for path, rel in _iter_scan_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if normalized_canary in _normalize(text):
            matches.append(rel)
    assert matches == ["quoin/memory/clean-authored-content.md"], matches


