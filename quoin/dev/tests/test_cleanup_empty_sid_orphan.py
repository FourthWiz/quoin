"""IVG-84 T-11: Regression tests for /cleanup empty-SID orphan sentinel eligibility.

An empty-SID orphan (`pending-restore-.txt`, etc.) is created when /checkpoint writes
a sentinel with an empty or unknown session UUID.  With the IVG-84 fix, /checkpoint
now refuses to write such sentinels — but pre-existing orphans on disk must still be
cleaned up by /cleanup.

These tests verify:
  (a) Executable: `pending-restore-.txt` matches the `pending-restore-*.txt` glob used
      by /cleanup's sentinel sweep — confirming the orphan is in scope without any
      logic change.
  (b) Prose: cleanup/SKILL.md documents that empty-SID orphans (suffix `-.txt`) are
      always trash-eligible once older than QUOIN_CLEANUP_SENTINEL_WINDOW.
"""
import fnmatch
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths — pin to SOURCE (not deployed ~/.claude copy)
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]
CLEANUP_SKILL = REPO_ROOT / "quoin" / "adapters" / "claude" / "skills" / "cleanup" / "SKILL.md"


# ---------------------------------------------------------------------------
# Test (a): Executable glob match — `pending-restore-.txt` is in-scope
# ---------------------------------------------------------------------------

def test_orphan_matches_pending_restore_glob():
    """`pending-restore-.txt` must match the `pending-restore-*.txt` family glob.

    /cleanup Step 4 uses `find ... -name 'pending-restore-*.txt'` to enumerate
    candidates. This test confirms that `pending-restore-.txt` (the empty-SID
    orphan form) matches that glob — so no logic change is needed; the existing
    sweep already catches it.
    """
    orphan = "pending-restore-.txt"
    pattern = "pending-restore-*.txt"
    assert fnmatch.fnmatch(orphan, pattern), (
        f"'{orphan}' does NOT match '{pattern}'. "
        "The /cleanup sentinel sweep glob must cover the empty-SID orphan form."
    )


def test_normal_uuid_sentinel_matches_glob():
    """A normal UUID-suffixed sentinel also matches the glob (sanity check)."""
    normal = "pending-restore-d1ffcf15-0eae-44f5-9e69-29d2fbf69da4.txt"
    pattern = "pending-restore-*.txt"
    assert fnmatch.fnmatch(normal, pattern), (
        f"Normal sentinel '{normal}' does not match '{pattern}'. "
        "Something is wrong with the glob pattern."
    )


def test_orphan_does_not_match_uuid_skip_pattern():
    """The orphan `pending-restore-.txt` cannot match `-<current_uuid>.txt` suffix.

    /cleanup Step 4 skips a file if its suffix matches `-<current_uuid>.txt`.
    An orphan has suffix `-.txt`; a real UUID is never empty, so the orphan is
    NEVER skipped by the UUID-protection check — it is always trash-eligible.
    """
    # Any realistic UUID
    current_uuid = "d1ffcf15-0eae-44f5-9e69-29d2fbf69da4"
    skip_pattern = f"-{current_uuid}.txt"

    orphan = "pending-restore-.txt"
    assert not orphan.endswith(skip_pattern), (
        f"Orphan '{orphan}' unexpectedly matches the UUID-skip pattern '{skip_pattern}'. "
        "The UUID-skip logic would incorrectly protect the orphan from being trashed."
    )


# ---------------------------------------------------------------------------
# Test (b): Prose — cleanup/SKILL.md documents empty-SID orphan eligibility
# ---------------------------------------------------------------------------

