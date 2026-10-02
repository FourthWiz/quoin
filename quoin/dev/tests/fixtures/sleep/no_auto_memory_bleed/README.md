Used by write-boundary test to assert /sleep ONLY writes to lessons-learned.md and forgotten/.

This fixture contains entries that score highly for promote (user_marked_yes). If the write-target
restriction were absent, a naive implementation might attempt to write to ~/.claude/projects/<hash>/memory/
(auto-memory) instead of the correct lessons-learned.md path.

The test_sleep_write_boundary.py Layer 1 test runs:
  python3 quoin/scripts/sleep_score.py --dry-run \
    --scan-dir quoin/dev/tests/fixtures/sleep/no_auto_memory_bleed/ \
    --scan-days 365 --output json

And asserts that NO output line contains any path matching ~/.claude/projects/ or /Users/*/.claude/projects/.

The test also verifies (Layer 2) that quoin/skills/sleep/SKILL.md contains the literal string "ONLY writes to".

Format note: the insights files here use the legacy "### Insight N:" entry-heading format, kept to
exercise that parsing path. The format writers produce today is "## <HH:MM> — <context>"; see
capture_insight_format/.
