"""authored_content_lint.py must be wired into the ACTIVE adapter bodies for
/end_of_task and /review.

Position-sensitive assertions (not mere presence) guard against the wiring
regressing into the AskUserQuestion-gated trigger region: the new advisory
step must land structurally AFTER the existing "If nothing found" item, and
the pre-existing trigger wording / question text must stay untouched.
"""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
ADAPTER_SKILLS = REPO_ROOT / "quoin" / "adapters" / "claude" / "skills"

_EOT_PATH = ADAPTER_SKILLS / "end_of_task" / "SKILL.md"
_REVIEW_PATH = ADAPTER_SKILLS / "review" / "SKILL.md"

_TOKEN = "authored_content_lint.py"

_TRIGGER_SENTENCE = "if any garbage file or debug leftover was found, show a categorized summary:"
_ASK_QUESTION_TEXT = "Garbage files or debug leftovers found. How would you like to proceed?"
_IF_NOTHING_FOUND = "**If nothing found** — print one line and continue:"


def test_wired_files_exist():
    assert _EOT_PATH.is_file()
    assert _REVIEW_PATH.is_file()


def test_token_present_in_both_active_adapter_bodies():
    for path in (_EOT_PATH, _REVIEW_PATH):
        body = path.read_text(encoding="utf-8")
        assert _TOKEN in body, (
            f"{_TOKEN!r} not found in active adapter body {path} — the wiring "
            f"must land here."
        )


def test_end_of_task_token_position_after_if_nothing_found_item():
    body = _EOT_PATH.read_text(encoding="utf-8")
    idx_if_nothing_found = body.index(_IF_NOTHING_FOUND)
    idx_token = body.index(_TOKEN)
    assert idx_token > idx_if_nothing_found, (
        "authored_content_lint.py invocation must appear structurally AFTER "
        "the 'If nothing found' item, never inside items 1-4 of Step 1b."
    )


def test_end_of_task_trigger_wording_names_its_population_explicitly():
    body = _EOT_PATH.read_text(encoding="utf-8")
    assert _TRIGGER_SENTENCE in body, (
        "Step 1b's 'Present findings' trigger must name only 'garbage file' and "
        "'debug leftover' as its trigger population, not a bare 'anything'."
    )


def test_end_of_task_ask_user_question_text_is_byte_unchanged():
    body = _EOT_PATH.read_text(encoding="utf-8")
    assert _ASK_QUESTION_TEXT in body, (
        "The Step 1b AskUserQuestion question text must remain byte-unchanged."
    )


def test_end_of_task_basis_is_union():
    body = _EOT_PATH.read_text(encoding="utf-8")
    idx = body.index(_TOKEN)
    line_start = body.rfind("\n", 0, idx) + 1
    line_end = body.index("\n", idx)
    invocation_line = body[line_start:line_end]
    assert "--basis union" in invocation_line


def test_review_basis_is_committed():
    body = _REVIEW_PATH.read_text(encoding="utf-8")
    idx = body.index(_TOKEN)
    line_start = body.rfind("\n", 0, idx) + 1
    line_end = body.index("\n", idx)
    invocation_line = body[line_start:line_end]
    assert "--basis committed" in invocation_line


def test_mutation_folding_invocation_into_trigger_region_fails_position_check(tmp_path):
    """Scratch-mutation guard: folding the new step's invocation line back
    into the Step 1b 'Present findings' summary block (before 'If nothing
    found') must fail the position assertion above."""
    body = _EOT_PATH.read_text(encoding="utf-8")
    invocation_line = next(
        line for line in body.splitlines() if _TOKEN in line and "--basis union" in line
    )
    mutated = body.replace("\n" + invocation_line + "\n", "\n", 1)
    insertion_point = mutated.index(_TRIGGER_SENTENCE) + len(_TRIGGER_SENTENCE)
    mutated = (
        mutated[:insertion_point] + "\n\n   " + invocation_line + mutated[insertion_point:]
    )

    idx_if_nothing_found = mutated.index(_IF_NOTHING_FOUND)
    idx_token = mutated.index(_TOKEN)
    assert idx_token < idx_if_nothing_found, (
        "mutation setup sanity check failed — the mutated invocation should now "
        "sit before the 'If nothing found' item"
    )
