"""The context-reading documentation describes the real-usage reading, its labels,
its knobs and the 200k-window tier reachability, and no longer calls the window
denominator a toolkit-chosen constant."""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCS = REPO_ROOT / "quoin" / "docs" / "hooks-guide.md"
HOOKS_TABLE = REPO_ROOT / "quoin" / "memory" / "hooks-table.md"
CHECKPOINT_SPEC = REPO_ROOT / "quoin" / "memory" / "checkpoint-spec.md"
LIFECYCLE = REPO_ROOT / "quoin" / "memory" / "lifecycle-guide.md"
CLAUDE_MD = REPO_ROOT / "quoin" / "CLAUDE.md"
CHECKPOINT_SKILL = REPO_ROOT / "quoin" / "adapters" / "claude" / "skills" / "checkpoint" / "SKILL.md"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_hooks_guide_covers_labels_knobs_and_telemetry():
    text = _text(DOCS)
    for needle in ("(real)", "(estimated)", "QUOIN_CONTEXT_TAIL_BYTES",
                   "real_tokens_before", "real_tokens_after", "reading_source",
                   "### 200k-window tier reachability"):
        assert needle in text, needle


def test_reachability_table_numbers_follow_the_auto_compact_formula():
    text = _text(DOCS)
    assert 200000 - 20000 - 13000 == 167000
    assert 1000000 - 20000 - 13000 == 967000
    for needle in ("167,000", "967,000", "`QUOIN_COMPACT_FIRST_BPS=7800`",
                   "`QUOIN_BLOCK_BPS=8000`", "`QUOIN_PANIC_BPS=8300`"):
        assert needle in text, needle


def test_telemetry_schema_table_lists_the_new_fields():
    text = _text(HOOKS_TABLE)
    for field in ("real_tokens_before", "real_tokens_after", "real_window", "reading_source"):
        assert "`%s`" % field in text, field
    assert "QUOIN_CONTEXT_TAIL_BYTES" in text


def test_old_denominator_wording_is_gone():
    for path in (DOCS, HOOKS_TABLE, CHECKPOINT_SPEC, LIFECYCLE, CLAUDE_MD):
        text = _text(path)
        assert "Effective token limit used as 100% denominator" not in text, path
        assert "toolkit-chosen denominator" not in text, path


def test_checkpoint_notice_names_the_source():
    text = _text(CHECKPOINT_SKILL)
    assert "compute_context_reading TRANSCRIPT_PATH" in text
    assert "Context is at ${PCT_VALUE} (>=" in text
    assert "(estimated)" in text
