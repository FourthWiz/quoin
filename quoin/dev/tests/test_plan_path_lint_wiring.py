"""IVG-143 T-08: plan_path_lint.py must be wired into the ACTIVE adapter bodies.

Cheap silent-wrong-file guard (R-05): asserts the literal token `plan_path_lint.py`
appears in the active `adapters/claude/skills/{plan,revise,gate}/SKILL.md` bodies. Mirrors
the general "wiring lands in the correct file" discipline used by other IVG-143
sibling integration tests in this repo.
"""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
ADAPTER_SKILLS = REPO_ROOT / "quoin" / "adapters" / "claude" / "skills"

_WIRED_FILES = {
    "plan": ADAPTER_SKILLS / "plan" / "SKILL.md",
    "revise": ADAPTER_SKILLS / "revise" / "SKILL.md",
    "gate": ADAPTER_SKILLS / "gate" / "SKILL.md",
}

_TOKEN = "plan_path_lint.py"


def test_wired_files_exist():
    for name, path in _WIRED_FILES.items():
        assert path.is_file(), f"expected active adapter body for {name!r} at {path}"


def test_token_present_in_each_active_adapter_body():
    for name, path in _WIRED_FILES.items():
        body = path.read_text(encoding="utf-8")
        assert _TOKEN in body, (
            f"{_TOKEN!r} not found in active adapter body {path} — the {name!r} "
            f"wiring for IVG-143 (I-1/I-2/I-3) must land here."
        )
