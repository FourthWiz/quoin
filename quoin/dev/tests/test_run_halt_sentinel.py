"""SKILL.md-lint tests for the autonomous halt-sentinel contract
(IVG-153, T-14/T-15).

Text-level guard asserting: (1) all six hard-stop sites in `run/SKILL.md`
document a halt-sentinel write with a reason before exit, plus the
never-auto-PR restatement; (2) the halt-sentinel schema (five fields) and
the stable outside-task-folder location statement appear in both
`core/skills/run_hard.md` and `memory/autonomous-mode.md`. Mirrors the repo's
existing SKILL.md-lint style (see test_run_autonomous_depth.py,
test_run_core_autonomous.py).
"""
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
RUN_SKILL = REPO_ROOT / "quoin" / "adapters" / "claude" / "skills" / "run_hard" / "SKILL.md"
CORE_DOC = REPO_ROOT / "quoin" / "core" / "skills" / "run_hard.md"
AUTONOMOUS_MODE_DOC = REPO_ROOT / "quoin" / "memory" / "autonomous-mode.md"

SCHEMA_FIELDS = ("task", "phase", "reason", "timestamp", "resume_hint")

HARD_STOP_SITES = (
    "Review BLOCKED",
    "Gate FAIL after the retry cap",
    "Review CHANGES_REQUESTED after 3 rounds",
    "Git conflict",
    "Branch-hygiene violation",
    "Below-bar formulation",
)


@pytest.fixture(scope="module")
def run_skill_text() -> str:
    assert RUN_SKILL.exists(), f"run/SKILL.md not found at {RUN_SKILL}"
    return RUN_SKILL.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def core_doc_text() -> str:
    assert CORE_DOC.exists(), f"core/skills/run_hard.md not found at {CORE_DOC}"
    return CORE_DOC.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def autonomous_mode_text() -> str:
    assert AUTONOMOUS_MODE_DOC.exists(), f"autonomous-mode.md not found at {AUTONOMOUS_MODE_DOC}"
    return AUTONOMOUS_MODE_DOC.read_text(encoding="utf-8")


def test_halt_sentinel_section_present(run_skill_text: str) -> None:
    assert "## Autonomous hard stops (halt-sentinel)" in run_skill_text


def test_all_six_hard_stop_sites_documented(run_skill_text: str) -> None:
    missing = [site for site in HARD_STOP_SITES if site not in run_skill_text]
    assert not missing, f"run/SKILL.md is missing hard-stop sites: {missing}"


def test_halt_sentinel_write_before_exit(run_skill_text: str) -> None:
    text = run_skill_text
    assert "write a halt-sentinel" in text or "write the halt-sentinel" in text
    assert "before exit" in text
    assert ".workflow_artifacts/memory/autonomous-halt-{task}.md" in text


def test_reason_field_present_per_site(run_skill_text: str) -> None:
    # The six-site table in "## Autonomous hard stops" documents a `reason:`
    # field for each site.
    assert run_skill_text.count("`reason:") >= 6


def test_never_auto_pr_restatement(run_skill_text: str) -> None:
    text = run_skill_text
    assert "NEVER auto-creates a pull request" in text
    assert "PR creation stays `/pr`" in text


def test_each_hard_stop_site_references_the_sentinel_section(run_skill_text: str) -> None:
    # Each of the four sites edited inline (gate-fail, CHANGES_REQUESTED,
    # BLOCKED, git conflict, branch-hygiene) must point back at the
    # consolidated "## Autonomous hard stops" section, not duplicate the
    # contract inline.
    text = run_skill_text
    assert text.count('per "## Autonomous hard stops"') >= 5


def test_core_doc_has_halt_sentinel_schema(core_doc_text: str) -> None:
    missing = [f for f in SCHEMA_FIELDS if f"`{f}`" not in core_doc_text]
    assert not missing, f"core/skills/run_hard.md missing schema fields: {missing}"


def test_core_doc_has_stable_outside_task_folder_location(core_doc_text: str) -> None:
    text = core_doc_text.lower()
    assert "outside the task-scoped artifact" in text or "outside the task folder" in text
    assert "stable" in text


def test_halt_sentinel_schema() -> None:
    """T-15: all 5 schema fields + the stable-location-outside-task-folder
    statement must appear in BOTH core/skills/run_hard.md and autonomous-mode.md.
    """
    core_text = CORE_DOC.read_text(encoding="utf-8")
    memory_text = AUTONOMOUS_MODE_DOC.read_text(encoding="utf-8")

    for doc_name, text in (("core/skills/run_hard.md", core_text), ("autonomous-mode.md", memory_text)):
        missing = [f for f in SCHEMA_FIELDS if f"`{f}`" not in text]
        assert not missing, f"{doc_name} missing schema fields: {missing}"

    assert "outside" in core_text.lower() and (
        "task-scoped artifact" in core_text.lower() or "task folder" in core_text.lower()
    )
    assert "outside" in memory_text.lower() and "task folder" in memory_text.lower()


def test_core_doc_stays_token_clean_with_new_content(core_doc_text: str) -> None:
    # Mirror test_run_core_autonomous.py's forbidden-token guard so the new
    # schema bullet doesn't reintroduce a runtime-specific token.
    forbidden = ("~/.claude", "Haiku", "Sonnet", "Opus", "Agent", "gh CLI")
    hits = [t for t in forbidden if t in core_doc_text]
    assert not hits, f"core/skills/run_hard.md contains forbidden tokens: {hits}"
