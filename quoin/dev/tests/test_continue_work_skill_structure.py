"""Tests for continue_work skill structure and installer registration."""
import pathlib
import re

import pytest

# Repo root is 3 levels up from this file (quoin/dev/tests/ → quoin/ → quoin/ → root)
REPO_ROOT = pathlib.Path(__file__).parent.parent.parent.parent
ADAPTER_SKILLS_DIR = REPO_ROOT / "quoin" / "adapters" / "claude" / "skills"
SKILL_FILE = ADAPTER_SKILLS_DIR / "continue_work" / "SKILL.md"


def _read_skill() -> str:
    return SKILL_FILE.read_text(encoding="utf-8")


def test_skill_file_exists():
    """SKILL.md must exist at the expected path."""
    assert SKILL_FILE.exists(), f"Expected {SKILL_FILE} to exist"


def test_frontmatter_model_is_sonnet():
    """Frontmatter must declare model: sonnet (not haiku)."""
    content = _read_skill()
    # Frontmatter is between the first two --- delimiters
    match = re.search(r"^---\n(.*?)\n---", content, re.DOTALL)
    assert match, "No frontmatter found in SKILL.md"
    frontmatter = match.group(1)
    assert "model: sonnet" in frontmatter, f"Expected 'model: sonnet' in frontmatter, got:\n{frontmatter}"


def test_frontmatter_name_is_continue_work():
    """Frontmatter must declare name: continue_work."""
    content = _read_skill()
    match = re.search(r"^---\n(.*?)\n---", content, re.DOTALL)
    assert match, "No frontmatter found in SKILL.md"
    frontmatter = match.group(1)
    assert "name: continue_work" in frontmatter, f"Expected 'name: continue_work' in frontmatter"


def test_frontmatter_description_nonempty():
    """Frontmatter description must be non-empty."""
    content = _read_skill()
    match = re.search(r'^description:\s*"(.+)"', content, re.MULTILINE)
    assert match, "No non-empty description found in frontmatter"
    assert len(match.group(1).strip()) > 10, "Description is too short"


def test_s0_model_dispatch_section_present():
    """§0 Model dispatch section must be present."""
    content = _read_skill()
    assert "## §0 Model dispatch" in content, "§0 Model dispatch section missing"


def test_s0_block_uses_sonnet_not_haiku():
    """§0 block must reference sonnet tier, not haiku."""
    content = _read_skill()
    s0_start = content.find("## §0 Model dispatch")
    assert s0_start >= 0, "§0 block not found"
    s0_end = content.find("\n## ", s0_start + 1)
    s0_block = content[s0_start:s0_end] if s0_end >= 0 else content[s0_start:]

    assert "model: sonnet" in s0_block, "§0 block should reference model: sonnet"
    assert "dispatched-tier: sonnet" in s0_block, "§0 block should reference dispatched-tier: sonnet"
    assert 'model: "sonnet"' in s0_block, '§0 block should use model: "sonnet" in Agent spawn'
    # The dispatch description must say "sonnet tier", not "haiku tier"
    assert "dispatched at sonnet tier" in s0_block, "§0 description should say 'dispatched at sonnet tier'"
    assert "dispatched at haiku tier" not in s0_block, "§0 must not say 'dispatched at haiku tier'"
    # Must NOT use model: "haiku" in Agent spawn
    assert 'model: "haiku"' not in s0_block, '§0 block must not spawn with model: "haiku"'


def test_continue_work_in_canonical_skills():
    """continue_work must be in CANONICAL_SKILLS in installer.py."""
    installer = REPO_ROOT / "src" / "quoin" / "installer.py"
    assert installer.exists(), f"installer.py not found at {installer}"
    content = installer.read_text(encoding="utf-8")
    assert '"continue_work"' in content, "continue_work not found in installer.py"
    # Must appear inside the CANONICAL_SKILLS tuple
    canonical_match = re.search(r"CANONICAL_SKILLS\s*=\s*\(([^)]+)\)", content, re.DOTALL)
    assert canonical_match, "CANONICAL_SKILLS tuple not found in installer.py"
    canonical_block = canonical_match.group(1)
    assert '"continue_work"' in canonical_block, "continue_work not in CANONICAL_SKILLS tuple"


def test_skill_overrides_is_name_only():
    """SKILL_OVERRIDES must have continue_work: 'name-only' (NOT 'on')."""
    installer = REPO_ROOT / "src" / "quoin" / "installer.py"
    content = installer.read_text(encoding="utf-8")
    overrides_match = re.search(r"SKILL_OVERRIDES\s*:\s*dict\[.*?\]\s*=\s*\{([^}]+)\}", content, re.DOTALL)
    assert overrides_match, "SKILL_OVERRIDES dict not found"
    overrides_block = overrides_match.group(1)
    assert '"continue_work": "name-only"' in overrides_block, (
        f"Expected '\"continue_work\": \"name-only\"' in SKILL_OVERRIDES, got block:\n{overrides_block}"
    )
    assert '"continue_work": "on"' not in overrides_block, (
        "continue_work must not be 'on' in SKILL_OVERRIDES"
    )
