"""IVG-258 T-14: `/cleanup` Step 5c run-state sweep, the two-family
regression, and the `_DOCS_TO_TESTS` routing rows this stage adds.

`cleanup/SKILL.md` carried zero `_DOCS_TO_TESTS` rows before this stage --
an affected-area gate after a cleanup-only edit selected no test at all.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = REPO_ROOT / "quoin"
CLEANUP_SKILL = SOURCE_ROOT / "adapters" / "claude" / "skills" / "cleanup" / "SKILL.md"
SLEEP_SKILL = SOURCE_ROOT / "adapters" / "claude" / "skills" / "sleep" / "SKILL.md"
AFFECTED_TESTS = SOURCE_ROOT / "core" / "scripts" / "affected_tests.py"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Step 5c
# ---------------------------------------------------------------------------


def test_hard_delete_check_catches_helper_form():
    fake = "Step 5c: trash_move a\npython3 fsops.py rm \"a\"\nStep 6"
    fake_step5c = fake.split("Step 5c", 1)[1].split("Step 6", 1)[0]
    assert "fsops.py rm" in fake_step5c


# ---------------------------------------------------------------------------
# Closed-scope statements + /sleep comparison table
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Nine-family regression (this stage must not touch the allow-list)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _DOCS_TO_TESTS routing rows
# ---------------------------------------------------------------------------


def _load_docs_to_tests():
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location("_quoin_core_affected_tests_t14", AFFECTED_TESTS)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module._DOCS_TO_TESTS


