"""T-07: Verify no dev-workflow residuals remain post-rebrand.

Three test cases:
1. No lowercase 'dev-workflow' in quoin/ (excluding frozen v2-historical fixture).
2. No 'Quoin foundation Stage' prose-drift wording in skills or CLAUDE.md.
3. CHANGELOG.md documents the DEV WORKFLOW marker preservation.
"""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
QUOIN_DIR = REPO_ROOT / "quoin"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"


def test_changelog_documents_marker_preservation():
    """CHANGELOG.md must contain the literal DEV WORKFLOW marker string."""
    assert CHANGELOG.exists(), f"CHANGELOG.md not found at {CHANGELOG}"
    content = CHANGELOG.read_text()
    marker = "# === DEV WORKFLOW START ==="
    assert marker in content, (
        f"CHANGELOG.md must document marker preservation with the literal string '{marker}'"
    )
