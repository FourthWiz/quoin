"""Citation rows the workflow evidence and gate work relies on.

Reads the compatibility document only; no network access. The existing docs
test owns the general row grammar, so this file adds the rows specific to
snapshot placement, delegation output, cost and variant visibility, and extends
the rule that an unverified key is never quoted in code.
"""
from __future__ import annotations

import re

import _opencode_helpers as helpers

COMPAT_PATH = helpers.OPENCODE_DIR / "compatibility.md"
ADAPTER_DIR = helpers.SOURCE_DIR.parent / "src" / "quoin" / "opencode_adapter"
SECTION = "Headless run events and process lifecycle"
REQUIRED_KEYS = (
    "discovery-stops-at-git-root",
    "project-identity",
    "task-sync-output-to-parent",
    "parent-cost-scope",
    "cost-unpriced-zero",
    "effective-variant-visibility",
    "readonly-write-error",
)


def _section_text():
    text = COMPAT_PATH.read_text(encoding="utf-8")
    match = re.search(r"^## " + re.escape(SECTION) + r"\s*$", text, re.MULTILINE)
    assert match, SECTION
    rest = text[match.end():]
    following = re.search(r"^## ", rest, re.MULTILINE)
    return rest[: following.start()] if following else rest


def _rows():
    rows = []
    for line in _section_text().splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if cells[0] == "Claim" or set(cells[0]) <= {"-"}:
            continue
        rows.append(cells)
    return rows


def _by_key():
    out = {}
    for cells in _rows():
        assert len(cells) == 4, cells
        match = re.match(r"^`key: ([a-z0-9-]+)`", cells[3])
        assert match, cells[3]
        out.setdefault(match.group(1), []).append(cells)
    return out


def test_all_workflow_keys_present_once():
    keys = _by_key()
    for key in REQUIRED_KEYS:
        assert key in keys, key
        assert len(keys[key]) == 1, key
    for key, cells in keys.items():
        assert len(cells) == 1, "duplicate key %s" % key


def test_verified_rows_cite_pinned_lines():
    keys = _by_key()
    for key in REQUIRED_KEYS:
        _claim, status, evidence, _note = keys[key][0]
        if status == "verified":
            assert "blob/v1.18.32/" in evidence, key
            assert re.search(r"L\d+", evidence), key
        else:
            assert re.match(r"^unverified\s*(?:—|\s-\s)\s*\S", status), key


def test_unverified_keys_are_not_quoted_in_evidence_or_gate_code():
    unverified = [
        key for key, cells in _by_key().items() if cells[0][1].startswith("unverified")
    ]
    for name in ("evidence.py", "gate.py"):
        path = ADAPTER_DIR / name
        if not path.exists():
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            for key in unverified:
                assert ('"%s"' % key) not in line and ("'%s'" % key) not in line, (name, number, key)
