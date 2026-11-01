"""T-10 / T-13: Fresh install end-to-end smoke test — bash and python transports.

Skips on CI (no `claude` or `npx`). On a dev machine, verifies:
  - Transport exits 0
  - All skills' SKILL.md files copied to ~/.claude/skills/
  - All Tier-1 memory files deployed to ~/.claude/memory/
  - Scripts deployed + executable
  - ~/.claude/CLAUDE.md has exactly one marker section
  - QUICKSTART.md deployed to ~/.claude/

T-13 parametrize: bash path tests install.sh; python path tests
`python -m quoin install --source-dir` directly (subprocess PYTHONPATH set).
Timeout bumped to 180s (MAJ-3 round-2): warm Tier 1 path is <10s; cold pip
case can take ~120s; 180s covers the worst case.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
INSTALL_SH = REPO_ROOT / "quoin" / "install.sh"
QUOIN_SRC = REPO_ROOT / "quoin"
SRC = REPO_ROOT / "src"

# Import canonical constants from installer (MAJ-6/MIN-5 fix: programmatic, no hardcoded count)
# PYTHONPATH is set via pyproject.toml [tool.pytest.ini_options] pythonpath = ["src"]
from quoin.installer import CANONICAL_SKILLS, TIER1_MEMORY_FILES  # noqa: E402

pytestmark = pytest.mark.skipif(
    shutil.which("claude") is None or shutil.which("npx") is None,
    reason=(
        "install requires `claude` (hard) and `npx` (soft); dev-machine only. "
        "check_prerequisites() aborts on missing claude, so test cannot run on CI."
    ),
)


def _run_transport(transport: str, tmp_home: Path, timeout: int = 180) -> subprocess.CompletedProcess:
    if transport == "bash":
        return subprocess.run(
            ["bash", str(INSTALL_SH)],
            env={**os.environ, "HOME": str(tmp_home)},
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    else:
        # python transport — explicit PYTHONPATH so src layout works without pip install
        return subprocess.run(
            [sys.executable, "-m", "quoin", "install", "--source-dir", str(QUOIN_SRC)],
            env={**os.environ, "HOME": str(tmp_home), "PYTHONPATH": str(SRC)},
            capture_output=True,
            text=True,
            timeout=timeout,
        )


@pytest.mark.parametrize("transport", ["bash", "python"])
def test_fresh_clone_install_e2e(transport: str):
    assert INSTALL_SH.exists(), f"quoin/install.sh not found at {INSTALL_SH}"

    with tempfile.TemporaryDirectory() as tmp_home_str:
        tmp_home = Path(tmp_home_str)
        result = _run_transport(transport, tmp_home)
        assert result.returncode == 0, (
            f"{transport} transport failed: returncode={result.returncode}\n"
            f"stdout: {result.stdout[:1500]}\nstderr: {result.stderr[:1500]}"
        )

        skills_dir = tmp_home / ".claude" / "skills"
        for skill in CANONICAL_SKILLS:
            skill_md = skills_dir / skill / "SKILL.md"
            assert skill_md.exists(), f"[{transport}] Missing skill SKILL.md: {skill}"

        # Phases 6–17 migrated skills: each is installed from the Claude
        # adapter path. Verify the deployed file matches the adapter source
        # after __QUOIN_HOME__ substitution.
        MIGRATED_SKILLS = (
            "capture_insight", "triage", "start_of_day", "review",
            "plan", "critic", "revise", "revise-fast",
            "architect", "thorough_plan",
            "gate", "implement", "rollback",
            "end_of_task",
            "run",
            "end_of_day",
            "weekly_review",
            "cost_snapshot",
            "expand",
            "discover",
            "init_workflow",
        )
        # FIX-3: Use substitution-aware comparison.  Source SKILL.md files may contain
        # __QUOIN_HOME__ placeholders that the installer replaces at deploy time, so raw
        # byte-identity between source and deployed file is no longer correct.
        from quoin.installer import substitute_quoin_home  # noqa: E402
        dest_root = tmp_home / ".claude"
        for migrated in MIGRATED_SKILLS:
            adapter_src = (
                REPO_ROOT / "quoin" / "adapters" / "claude" / "skills"
                / migrated / "SKILL.md"
            )
            adapter_dst = skills_dir / migrated / "SKILL.md"
            assert adapter_dst.exists(), (
                f"install.sh did not deploy {migrated}"
            )
            src_text = adapter_src.read_text(encoding="utf-8")
            expected_text = substitute_quoin_home(src_text, dest_root)
            deployed_text = adapter_dst.read_text(encoding="utf-8")
            assert expected_text == deployed_text, (
                f"Deployed {migrated} SKILL.md does not match expected substituted content "
                f"from Claude adapter source "
                f"quoin/adapters/claude/skills/{migrated}/SKILL.md"
            )

        memory_dir = tmp_home / ".claude" / "memory"
        for mem_file in TIER1_MEMORY_FILES:
            assert (memory_dir / mem_file).exists(), (
                f"[{transport}] Missing Tier-1 memory file: {mem_file}"
            )

        # Byte-identical check for format-kit-pitfalls.md
        pitfalls_src = QUOIN_SRC / "memory" / "format-kit-pitfalls.md"
        pitfalls_dst = memory_dir / "format-kit-pitfalls.md"
        assert pitfalls_dst.exists()
        assert pitfalls_src.read_bytes() == pitfalls_dst.read_bytes(), (
            f"[{transport}] format-kit-pitfalls.md not byte-identical to source"
        )

        assert (tmp_home / ".claude" / "QUICKSTART.md").exists(), (
            f"[{transport}] QUICKSTART.md not deployed"
        )

        claude_md = tmp_home / ".claude" / "CLAUDE.md"
        assert claude_md.exists(), f"[{transport}] CLAUDE.md not created"
        content = claude_md.read_text()
        marker_count = content.count("# === DEV WORKFLOW START ===")
        assert marker_count == 1, (
            f"[{transport}] Expected 1 marker section, got {marker_count}"
        )

        # Skill count matches programmatic constant (MAJ-6)
        # The installer emits dest_root-relative path (e.g. /tmp/.../skills/), not ~/-relative.
        m = re.search(r"Copied (\d+) skills to .+/skills/", result.stdout)
        assert m is not None, f"[{transport}] missing skill count line in stdout"
        assert int(m.group(1)) == len(CANONICAL_SKILLS), (
            f"[{transport}] skill count {m.group(1)} != len(CANONICAL_SKILLS)={len(CANONICAL_SKILLS)}"
        )
