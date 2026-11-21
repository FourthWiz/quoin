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


def test_install_sh_has_security_review_override():
    _adapter_pilot_helpers.assert_installer_selects_adapter("security_review")


# ── OWASP checklist wiring guard (cheap early guard, T-12) ─────────────────

OWASP_TOKENS = ["OWASP", "injection", "secrets", "authz", "dependency"]


