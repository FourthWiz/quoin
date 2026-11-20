"""Prose contract for the repo_cleanup SKILL.md hand-written body.

The generated guard blocks are stripped first; every assertion reads a slice
of the hand-written text and first proves the slice really starts at its own
heading, so a renamed heading cannot turn a check vacuous.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL = REPO_ROOT / "quoin" / "adapters" / "claude" / "skills" / "repo_cleanup" / "SKILL.md"
SCRIPTS = REPO_ROOT / "quoin" / "scripts"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    sys.path.insert(0, str(path.parent))
    spec.loader.exec_module(mod)
    return mod


def _hand_written() -> str:
    gen = _load(SCRIPTS / "inject_pollution_dispatch.py", "_t_repo_cleanup_gen")
    text = SKILL.read_text(encoding="utf-8")
    p, m = text.index(gen.POLLUTION_HEADING), text.index(gen.MINTIER_HEADING)
    b = text.index("## Session bootstrap")
    assert p < m < b
    return text[:p] + text[b:]


def _sections() -> dict[str, str]:
    body = _hand_written()
    parts = re.split(r"(?m)^(?=## )", body)
    out: dict[str, str] = {}
    for part in parts:
        head = part.split("\n", 1)[0]
        if head.startswith("#"):
            out[head.lstrip("# ").strip()] = part
    return out


def _section(name: str) -> str:
    secs = _sections()
    assert name in secs, f"missing section {name}"
    assert secs[name].startswith("## " + name)
    return secs[name]


def _has(text: str, *phrases: str) -> None:
    low = text.lower()
    for ph in phrases:
        assert ph.lower() in low, f"missing phrase: {ph}"


def _residue_hits(lines):
    lint = _load(REPO_ROOT / "quoin" / "core" / "scripts" / "authored_content_lint.py",
                 "_t_repo_cleanup_lint")
    hits = []
    for line in lines:
        hit = lint.match_taxonomy(line, ())
        if hit and hit[0] == "A" and hit[1] not in (".workflow_artifacts", "lessons-learned"):
            hits.append((line, hit))
    return hits


def test_residue_helper_still_bites():
    planted = ["see MAJ-2 for why", "done in T-04"]
    allowed = ["report at .workflow_artifacts/x/repo-cleanup-report.md", "fall back to lessons-learned.md"]
    assert len(_residue_hits(planted)) == 2
    assert _residue_hits(allowed) == []
