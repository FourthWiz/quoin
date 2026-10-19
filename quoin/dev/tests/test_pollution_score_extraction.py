"""
Quoin Stage 1 — pollution-guard detection tests.

The score itself is computed by quoin/scripts/pollution_guard.py (covered by
test_pollution_guard.py). This file covers two layers:
  1. Skill-side read: the §0' blocks tell the model to run the guard, key on
     the OVER| prefix and check the sentinel before running anything.
  2. Threshold behaviour: boundary conditions driven through the guard script.

Per lesson 2026-04-23: no live LLM calls — deterministic subprocess + string matching only.
Per lesson 2026-04-29: stdlib-only for deployed scripts.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).parent
GUARD = TESTS_DIR.parent.parent / "scripts" / "pollution_guard.py"

POLLUTION_HEADING = "## §0' Pollution dispatch (execute after §0 / §0c if present — before skill body)"
TARGET_SKILLS = [
    "architect", "plan", "critic", "revise", "review", "init_workflow", "discover",
    "specify", "security_review", "enrich",
]
# Phase-10 adapter migration moved active SKILL.md content from quoin/skills/ (stubs)
# to quoin/adapters/claude/skills/ (active adapter files). §0' was restored into the
# adapter dir by inject_pollution_dispatch.py (IVG-69 Stage A). Tests must read adapters.
SKILLS_DIR = TESTS_DIR.parent.parent / "adapters" / "claude" / "skills"


def _guard(transcript: Path, tmp_path: Path, extra_env: dict | None = None) -> str:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path / "home")}
    (tmp_path / "home").mkdir(exist_ok=True)
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run([sys.executable, str(GUARD), "--transcript", str(transcript)],
                          capture_output=True, text=True, env=env, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _block(skill: str) -> str:
    text = (SKILLS_DIR / skill / "SKILL.md").read_text(encoding="utf-8")
    start = text.index(POLLUTION_HEADING)
    end = text.index("\n## ", start + 1)
    return text[start:end]


# ─── 1. Skill-side read — structural assertions ───────────────────────────────

class TestSkillSideRead:
    """Verify §0' blocks run the guard and key on its OVER| output."""

    @pytest.mark.parametrize("skill", TARGET_SKILLS)
    def test_block_names_the_guard_script(self, skill):
        assert "pollution_guard.py" in _block(skill), (
            f"{skill}/SKILL.md §0' block must run pollution_guard.py to decide whether to dispatch"
        )

    @pytest.mark.parametrize("skill", TARGET_SKILLS)
    def test_block_keys_on_over_prefix(self, skill):
        assert "OVER|" in _block(skill)

    @pytest.mark.parametrize("skill", TARGET_SKILLS)
    def test_block_does_not_read_a_score_file(self, skill):
        block = _block(skill)
        assert "pollution-score-latest.txt" not in block
        assert "pollution_score:" not in block

    @pytest.mark.parametrize("skill", TARGET_SKILLS)
    def test_sentinel_check_precedes_the_guard_run(self, skill):
        block = _block(skill)
        sentinel = block.index("Sentinel check")
        prior = block.index("prior §0 dispatch already fired")
        run = block.index("pollution_guard.py")
        assert sentinel < run and prior < run


# ─── 2. Threshold behavior assertions ─────────────────────────────────────────

class TestThresholdBehavior:
    """Verify the threshold constant and tunable env var are documented in skills."""

    @pytest.mark.parametrize("skill", TARGET_SKILLS)
    def test_threshold_constant_referenced(self, skill):
        assert "POLLUTION_THRESHOLD" in (SKILLS_DIR / skill / "SKILL.md").read_text(encoding="utf-8")

    @pytest.mark.parametrize("skill", TARGET_SKILLS)
    def test_threshold_default_value_documented(self, skill):
        assert "5000" in (SKILLS_DIR / skill / "SKILL.md").read_text(encoding="utf-8")

    def test_score_boundary_below_threshold(self, tmp_path):
        f = tmp_path / "just_below.jsonl"
        f.write_bytes(b"x" * 4_999_000)
        assert _guard(f, tmp_path).startswith("OK|4999|")

    def test_score_at_threshold(self, tmp_path):
        f = tmp_path / "at_threshold.jsonl"
        f.write_bytes(b"x" * 5_000_000)
        assert _guard(f, tmp_path).startswith("OVER|5000|")

    def test_custom_threshold_via_env(self, tmp_path):
        f = tmp_path / "custom_threshold.jsonl"
        f.write_bytes(b"x" * 2_000_000)
        assert _guard(f, tmp_path).startswith("OK|2000|")
        assert _guard(f, tmp_path, {"QUOIN_POLLUTION_THRESHOLD": "2000"}).startswith("OVER|2000|")
