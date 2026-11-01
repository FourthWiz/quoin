"""Adapter-pilot wiring tests for the new security_review skill (IVG-128 T-12).

Mirrors the shape of test_review_adapter_pilot.py: core doc existence/anchors,
adapter SKILL.md existence/frontmatter/installer-selection, and a cheap
grep-based wiring guard confirming the OWASP checklist body and §0'/§0''
headings actually landed in the adapter file.
"""
from pathlib import Path
import _adapter_pilot_helpers

THIS_FILE = Path(__file__).resolve()
TESTS_DIR = THIS_FILE.parent
PKG_DIR = TESTS_DIR.parent.parent              # quoin/quoin/
CORE_SKILL_DOC = PKG_DIR / "core" / "skills" / "security_review.md"
ADAPTER_SKILL_MD = PKG_DIR / "adapters" / "claude" / "skills" / "security_review" / "SKILL.md"
CODEX_README = PKG_DIR / "adapters" / "codex" / "skills" / "security_review" / "README.md"


def test_core_skill_doc_exists():
    assert CORE_SKILL_DOC.is_file(), f"Missing {CORE_SKILL_DOC}"


def test_codex_readme_exists():
    assert CODEX_README.is_file(), f"Missing {CODEX_README}"


def test_adapter_skill_md_exists():
    assert ADAPTER_SKILL_MD.is_file(), f"Missing {ADAPTER_SKILL_MD}"


def test_adapter_skill_md_declares_opus():
    import yaml
    text = ADAPTER_SKILL_MD.read_text(encoding="utf-8")
    parts = text.split("---", 2)
    assert len(parts) >= 3, "Adapter SKILL.md missing YAML frontmatter"
    fm = yaml.safe_load(parts[1])
    assert fm.get("model") == "opus", (
        f"Adapter SKILL.md must declare model: opus (got {fm.get('model')!r})"
    )
    assert fm.get("name") == "security_review"


def test_adapter_skill_md_references_core_doc():
    text = ADAPTER_SKILL_MD.read_text(encoding="utf-8")
    assert "quoin/core/skills/security_review.md" in text, (
        "Adapter SKILL.md must reference the portable intent doc path"
    )


def test_install_sh_has_security_review_override():
    _adapter_pilot_helpers.assert_installer_selects_adapter("security_review")


# ── OWASP checklist wiring guard (cheap early guard, T-12) ─────────────────

OWASP_TOKENS = ["OWASP", "injection", "secrets", "authz", "dependency"]


def test_adapter_body_mentions_owasp_checklist_tokens():
    text = ADAPTER_SKILL_MD.read_text(encoding="utf-8")
    missing = [t for t in OWASP_TOKENS if t not in text]
    assert not missing, (
        f"security_review adapter SKILL.md is missing OWASP checklist tokens: {missing}"
    )


def test_adapter_has_pollution_and_mintier_headings():
    """security_review is an Opus-tier leaf skill (D-03) — must carry both
    generator-injected §0' and §0'' headings exactly once each."""
    text = ADAPTER_SKILL_MD.read_text(encoding="utf-8")
    pollution_heading = "## §0' Pollution dispatch"
    mintier_heading = "## §0″ Minimum-tier guard"
    assert text.count(pollution_heading) == 1, (
        f"expected exactly 1 {pollution_heading!r} heading, found {text.count(pollution_heading)}"
    )
    assert text.count(mintier_heading) == 1, (
        f"expected exactly 1 {mintier_heading!r} heading, found {text.count(mintier_heading)}"
    )


def test_adapter_does_not_have_section_0_dispatch():
    """security_review is Opus-tier — MUST NOT carry the §0 Model dispatch block
    (that's the cheap-tier mechanism; Opus-tier leaves use §0'' instead)."""
    text = ADAPTER_SKILL_MD.read_text(encoding="utf-8")
    assert "## §0 Model dispatch" not in text, (
        "security_review adapter SKILL.md must not have a ## §0 Model dispatch "
        "heading (Opus-tier skill)"
    )


def test_adapter_has_fan_out_contract():
    """T-02: adapter must document the Fan-out contract used when /review
    dispatches this skill as the Large-profile security dimension."""
    text = ADAPTER_SKILL_MD.read_text(encoding="utf-8")
    assert "## Fan-out contract" in text
    assert "<verdict>APPROVED|CHANGES_REQUESTED|BLOCKED</verdict>" in text
