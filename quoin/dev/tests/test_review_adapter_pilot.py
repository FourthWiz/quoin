from pathlib import Path
import _adapter_pilot_helpers

THIS_FILE = Path(__file__).resolve()
TESTS_DIR = THIS_FILE.parent
PKG_DIR = TESTS_DIR.parent.parent              # quoin/quoin/
CORE_SKILL_DOC = PKG_DIR / "core" / "skills" / "review.md"
ADAPTER_SKILL_MD = PKG_DIR / "adapters" / "claude" / "skills" / "review" / "SKILL.md"

# review is Opus-tier — forbidden tokens mirror the existing pilot tests plus gh CLI.
FORBIDDEN_TOKENS = ("~/.claude", "/review", "Haiku", "Sonnet", "Opus", "Agent", "gh CLI")
# Load-bearing input/output anchor for the review skill.
REQUIRED_MENTION = ".workflow_artifacts/<task-name>/"


def test_core_skill_doc_exists():
    assert CORE_SKILL_DOC.is_file(), f"Missing {CORE_SKILL_DOC}"


def test_core_skill_doc_has_no_forbidden_tokens():
    text = CORE_SKILL_DOC.read_text(encoding="utf-8")
    hits = [t for t in FORBIDDEN_TOKENS if t in text]
    assert not hits, f"Core doc contains forbidden tokens: {hits}"


def test_core_skill_doc_mentions_task_artifact_anchor():
    text = CORE_SKILL_DOC.read_text(encoding="utf-8")
    assert REQUIRED_MENTION in text, (
        f"Core doc must mention literal: {REQUIRED_MENTION}"
    )


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
    assert fm.get("name") == "review"


def test_adapter_skill_md_references_core_doc():
    text = ADAPTER_SKILL_MD.read_text(encoding="utf-8")
    assert "quoin/core/skills/review.md" in text, (
        "Adapter SKILL.md must reference the portable intent doc path"
    )


def test_install_sh_has_review_override():
    # IVG-69 Stage B retarget: install.sh no longer carries ADAPTER_*_SRC vars.
    # Assert the equivalent contract against installer.py logic instead.
    _adapter_pilot_helpers.assert_installer_selects_adapter("review")


def test_adapter_skill_md_does_not_have_section_0_dispatch():
    """review is Opus-tier — MUST NOT carry the §0 Model dispatch block.

    §0' Pollution + §0c Pidfile are valid on Opus-tier; only §0 Model dispatch
    is forbidden (IVG-69 Stage B retarget).
    """
    text = ADAPTER_SKILL_MD.read_text(encoding="utf-8")
    assert "§0 Model dispatch" not in text, (
        "review adapter SKILL.md must not carry §0 dispatch block (Opus-tier skill)"
    )
    # Narrow to §0 Model dispatch heading only — §0' Pollution + §0c Pidfile are
    # legitimately present on Opus-tier skills (IVG-69 Stage B retarget).
    assert "## §0 Model dispatch" not in text, (
        "review adapter SKILL.md must not have a ## §0 Model dispatch heading (Opus-tier skill)"
    )


# ── IVG-70: Branch-hygiene review backstop assertions ─────────────────────


def test_review_adapter_has_step_6a_branch_placement():
    """IVG-70 T-08 AC-1: review adapter SKILL.md must contain ### Step 6a branch placement backstop."""
    text = ADAPTER_SKILL_MD.read_text(encoding="utf-8")
    assert "### Step 6a" in text, (
        "review adapter SKILL.md must contain '### Step 6a' (branch placement backstop). "
        "IVG-70 T-06 edit is missing or was reverted."
    )


def test_review_adapter_step_6a_references_branch_hygiene():
    """IVG-70 T-08 AC-1: Step 6a must reference branch_hygiene.py."""
    text = ADAPTER_SKILL_MD.read_text(encoding="utf-8")
    assert "branch_hygiene.py" in text, (
        "review adapter SKILL.md must reference 'branch_hygiene.py' in the "
        "Step 6a backstop. IVG-70 T-06 edit is missing or was reverted."
    )


def test_review_adapter_step_6a_is_diff_independent():
    """IVG-70 T-08 AC-3: Step 6a must state it runs unconditionally/independently of the diff."""
    text = ADAPTER_SKILL_MD.read_text(encoding="utf-8")
    # The section must mention unconditional or independent execution
    assert "unconditionally" in text or "independently" in text, (
        "review adapter SKILL.md Step 6a must state it runs unconditionally / "
        "independently of the git diff basis. IVG-70 T-06 AC-3 guard failed."
    )


def test_review_adapter_step_6a_uses_pwd_root():
    """IVG-70 T-08 AC-1: Step 6a must use $(pwd) for root resolution (review runs inline)."""
    text = ADAPTER_SKILL_MD.read_text(encoding="utf-8")
    assert "$(pwd)" in text, (
        "review adapter SKILL.md Step 6a must use '$(pwd)' for project root "
        "(review runs inline at project-root cwd — no walk-up needed). "
        "IVG-70 T-06 AC-1 guard failed."
    )


def test_review_core_doc_has_placement_backstop_rule():
    """IVG-70 T-08 AC-2: core review.md Behavior contract must contain the backstop line."""
    text = CORE_SKILL_DOC.read_text(encoding="utf-8")
    assert "protected branch" in text, (
        "core/skills/review.md must mention 'protected branch' in the Behavior contract. "
        "IVG-70 T-06 core doc edit is missing or was reverted."
    )


def test_review_core_doc_no_adapter_specific_refs():
    """IVG-70 T-08: core review.md must NOT contain branch_hygiene.py or __QUOIN_HOME__."""
    text = CORE_SKILL_DOC.read_text(encoding="utf-8")
    assert "branch_hygiene.py" not in text, (
        "core/skills/review.md must NOT contain 'branch_hygiene.py' "
        "(adapter-specific). IVG-70 adapter/core boundary violated."
    )
    assert "__QUOIN_HOME__" not in text, (
        "core/skills/review.md must NOT contain '__QUOIN_HOME__' "
        "(adapter-specific). IVG-70 adapter/core boundary violated."
    )


# ── IVG-128 T-12: review fan-out wiring guard (cheap early guard) ──────────


def test_review_adapter_has_profile_detection_language():
    """T-08: adapter must document reading the task profile to decide fan-out."""
    text = ADAPTER_SKILL_MD.read_text(encoding="utf-8")
    assert "Profile detection" in text
    assert "Task profile: Small/Medium/Large" in text


def test_review_adapter_has_worst_of_merge_language():
    """T-08: adapter must document the worst-of verdict merge rule."""
    text = ADAPTER_SKILL_MD.read_text(encoding="utf-8")
    assert "worst-of" in text
    assert "BLOCKED` > `CHANGES_REQUESTED` > `APPROVED`" in text


def test_review_adapter_names_all_three_dimensions():
    """T-08: adapter must name all three fan-out dimensions."""
    text = ADAPTER_SKILL_MD.read_text(encoding="utf-8")
    for dimension in ("security", "performance", "architecture/integration"):
        assert dimension in text, f"missing dimension name: {dimension!r}"


def test_review_adapter_preserves_small_single_pass():
    """T-08 AC: Small task path must be documented as unchanged/single-pass.

    Retitled by the round-2 review-fix (MINOR 16 / MAJOR 6): the branch now
    also names the fast-path override case on Small/Medium/undetermined, not
    just Small — assert the retitled heading, not the retired one."""
    text = ADAPTER_SKILL_MD.read_text(encoding="utf-8")
    assert "Single-pass review — Small profile, or a fast-path override on Small/Medium/undetermined" in text
    assert "byte-path-identical" in text
