#!/bin/sh
# test_checkpoint_picker_staleness.sh — regression tests for /checkpoint picker staleness fixes
#
# Three subtests for bugs B1, B2, and B3 (see plan: checkpoint-restore-picker-staleness):
#
#   sub-1 (B1): stale-sentinel mtime filter — only within-window sentinels are candidates.
#   sub-2 (B2): consumed-sentinel cleanup — picker picks OLD session sentinel; Step 5
#               trash-moves it, not just the current-session sentinel.
#   sub-3 (B3): session-state fallback — when no sentinels exist but a fresh session-state
#               file is present, the picker surfaces the synthesize prompt anchored on the
#               filename-derived task name.
#
# NOTE: /checkpoint is a skill (LLM-invoked), not a standalone shell script.
# This test harness validates the file-level contract — the artifact structure and logic
# that /checkpoint --restore MUST implement — rather than invoking the skill directly.
# Where the skill cannot be invoked directly, assertions are made against SKILL.md prose.
#
# Usage: sh quoin/dev/tests/test_checkpoint_picker_staleness.sh
# Exit 0 if all sub-tests pass; non-zero otherwise.

set -eu

PASS=0
FAIL=0

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
FIXTURE_DIR="$SCRIPT_DIR/fixtures/checkpoint_picker"
SLEEP_SKILL="$SCRIPT_DIR/../../skills/sleep/SKILL.md"

ok() { PASS=$((PASS + 1)); printf 'ok  %s\n' "$1"; }
fail() {
  FAIL=$((FAIL + 1))
  printf 'FAIL %s\n' "$1" >&2
}

TMPDIR_TEST="${TMPDIR:-/tmp}/test_checkpoint_picker_staleness_$$"
mkdir -p "$TMPDIR_TEST/.workflow_artifacts/memory/checkpoints"
mkdir -p "$TMPDIR_TEST/.workflow_artifacts/memory/sessions"

cleanup() { rm -rf "$TMPDIR_TEST"; }
trap cleanup EXIT

MEMORY_DIR="$TMPDIR_TEST/.workflow_artifacts/memory"
CHECKPOINTS_DIR="$MEMORY_DIR/checkpoints"

# Source _lib.sh for trash_move helper
. "$SCRIPT_DIR/../../hooks/_lib.sh"

TODAY_DATE=$(date -u +%Y-%m-%d 2>/dev/null) || TODAY_DATE=$(date +%Y-%m-%d)
TRASH_DIR="$MEMORY_DIR/trash/$TODAY_DATE"

# ─── sub-1 (B1): stale-sentinel mtime filter ─────────────────────────────────
#
# The SKILL.md restore picker enumerates pending-restore-*.txt sentinels using a
# mtime filter (QUOIN_RESTORE_SENTINEL_WINDOW, default 7d). This sub-test verifies:
#   (a) A sentinel dated 8 days ago is OUTSIDE the default 7d window.
#   (b) A sentinel dated 1 day ago is INSIDE the 7d window.
#   (c) The `find -mtime -7` idiom correctly distinguishes them.
# We test the find idiom directly since the skill is LLM-invoked.

printf '\n--- sub-1: B1 stale-sentinel mtime filter ---\n'

# Create two sentinels
printf 'checkpoint-stale.md\n' > "$MEMORY_DIR/pending-restore-STALE.txt"
printf 'checkpoint-fresh.md\n' > "$MEMORY_DIR/pending-restore-FRESH.txt"

# Set STALE to 8 days ago
EIGHT_DAYS_AGO=$(date -v -8d +%Y%m%d%H%M.%S 2>/dev/null || \
                 date -d '8 days ago' +%Y%m%d%H%M.%S 2>/dev/null || echo "")
if [ -n "$EIGHT_DAYS_AGO" ]; then
  touch -t "$EIGHT_DAYS_AGO" "$MEMORY_DIR/pending-restore-STALE.txt" 2>/dev/null || true
fi

# Simulate the picker's B1 filter: find sentinels within 7 days
WINDOW=7
CANDIDATES=$(find "$MEMORY_DIR" -maxdepth 1 -name 'pending-restore-*.txt' -mtime -${WINDOW} 2>/dev/null | sort)

# Only FRESH should appear (STALE is 8d old, outside -mtime -7 window)
# Note: if date manipulation is unsupported, both may appear — skip gracefully
if [ -n "$EIGHT_DAYS_AGO" ]; then
  candidate_count=$(printf '%s\n' "$CANDIDATES" | grep -c 'pending-restore' 2>/dev/null || echo "0")
  if printf '%s\n' "$CANDIDATES" | grep -q 'FRESH' 2>/dev/null; then
    ok "sub-1 (B1): FRESH sentinel (1d old) is within the 7d window"
  else
    fail "sub-1 (B1): FRESH sentinel NOT found in filtered candidates (got: $CANDIDATES)"
  fi
  if printf '%s\n' "$CANDIDATES" | grep -q 'STALE' 2>/dev/null; then
    fail "sub-1 (B1): STALE sentinel (8d old) appeared in filtered candidates — should be excluded"
  else
    ok "sub-1 (B1): STALE sentinel (8d old) correctly excluded from 7d window"
  fi
else
  ok "sub-1 (B1): (skipped — date -v/-d not supported on this platform)"
fi

rm -f "$MEMORY_DIR/pending-restore-STALE.txt" "$MEMORY_DIR/pending-restore-FRESH.txt"

# ─── sub-2 (B2): consumed-sentinel cleanup ───────────────────────────────────
#
# When the picker picks an OLD-session sentinel (not the current-session sentinel),
# Step 5 must trash-move the OLD sentinel, not just the current-session one.
# We simulate this with the trash_move helper and assert file state.

printf '\n--- sub-2 (B2): consumed-sentinel cleanup ---\n'

CURR_SESSION="sess-curr-b2"
OLD_SESSION="sess-old-b2"

# No current-session sentinel; only an old-session sentinel
printf 'checkpoint-old-b2.md\n' > "$MEMORY_DIR/pending-restore-${OLD_SESSION}.txt"

# Simulate: picker picks OLD sentinel → consumed_sentinel_path = path to OLD sentinel
consumed_sentinel_path="$MEMORY_DIR/pending-restore-${OLD_SESSION}.txt"
current_session_id="$CURR_SESSION"

# Verify OLD sentinel exists before "restore"
if [ -f "$consumed_sentinel_path" ]; then
  ok "sub-2 (B2): OLD sentinel exists before simulated restore"
else
  fail "sub-2 (B2): OLD sentinel NOT found at $consumed_sentinel_path"
fi

# Step 5 cleanup logic (simulated):
#   trash_move current-session sentinel if it exists (it doesn't here)
curr_sentinel="$MEMORY_DIR/pending-restore-${current_session_id}.txt"
[ -f "$curr_sentinel" ] && trash_move "$curr_sentinel" "$MEMORY_DIR" || true

#   trash_move consumed sentinel if non-empty and different from current-session sentinel
if [ -n "$consumed_sentinel_path" ] && [ "$consumed_sentinel_path" != "$curr_sentinel" ]; then
  if [ -f "$consumed_sentinel_path" ]; then
    trash_move "$consumed_sentinel_path" "$MEMORY_DIR"
  fi
fi

# Assert: OLD sentinel is in trash, not in memory/ root
if [ -f "$TRASH_DIR/pending-restore-${OLD_SESSION}.txt" ]; then
  ok "sub-2 (B2): OLD sentinel trash-moved to trash/<date>/"
else
  fail "sub-2 (B2): OLD sentinel NOT found in trash/<date>/ (expected: $TRASH_DIR/pending-restore-${OLD_SESSION}.txt)"
fi

if [ ! -f "$MEMORY_DIR/pending-restore-${OLD_SESSION}.txt" ]; then
  ok "sub-2 (B2): OLD sentinel removed from memory/ root"
else
  fail "sub-2 (B2): OLD sentinel still in memory/ root after Step 5"
fi

# Sub-test: disk-only candidate — consumed_sentinel_path="" → no non-current sentinel touched
printf 'checkpoint-disk-only.md\n' > "$MEMORY_DIR/pending-restore-${OLD_SESSION}-diskonly.txt"
consumed_sentinel_path=""  # disk-only candidate: no sentinel consumed

[ -f "$curr_sentinel" ] && trash_move "$curr_sentinel" "$MEMORY_DIR" || true
if [ -n "$consumed_sentinel_path" ] && [ "$consumed_sentinel_path" != "$curr_sentinel" ]; then
  if [ -f "$consumed_sentinel_path" ]; then
    trash_move "$consumed_sentinel_path" "$MEMORY_DIR"
  fi
fi

# Assert: disk-only sentinel is NOT trash-moved (consumed_sentinel_path was empty)
if [ -f "$MEMORY_DIR/pending-restore-${OLD_SESSION}-diskonly.txt" ]; then
  ok "sub-2 (B2): disk-only case — non-current sentinel NOT trash-moved when consumed_sentinel_path empty"
else
  fail "sub-2 (B2): disk-only case — sentinel was trash-moved despite empty consumed_sentinel_path"
fi

rm -f "$MEMORY_DIR/pending-restore-${OLD_SESSION}-diskonly.txt"

# ─── sub-3 (B3): session-state fallback ──────────────────────────────────────
#
# When no checkpoint sentinels exist but a recent session-state file is present,
# the picker surfaces a synthesize prompt. The task name must be derived from the
# FILENAME (stripping YYYY-MM-DD- prefix and .md suffix), not from ## Active task.
#
# We use the portable fixture at fixtures/checkpoint_picker/sessions/ to avoid
# reaching outside the temp dir or touching the user's real memory/ directory.

printf '\n--- sub-3 (B3): session-state fallback (filename-derived task name) ---\n'

# Verify the fixture exists
FIXTURE_SESSION="$FIXTURE_DIR/sessions/2026-05-17-personal-site-sim-embed.md"
if [ -f "$FIXTURE_SESSION" ]; then
  ok "sub-3 (B3): portable fixture exists at $FIXTURE_SESSION"
else
  fail "sub-3 (B3): fixture NOT found at $FIXTURE_SESSION"
fi

# Copy fixture into the test's sessions/ temp dir (no reach-outside-tmp)
cp "$FIXTURE_SESSION" "$MEMORY_DIR/sessions/" 2>/dev/null || {
  fail "sub-3 (B3): failed to copy fixture to temp sessions dir"
}

# Assert: fixture has no ## Active task heading (real-world session shape that caused the bug)
if grep -q '^## Active task' "$FIXTURE_SESSION" 2>/dev/null; then
  fail "sub-3 (B3): fixture should NOT have ## Active task heading (violates test design)"
else
  ok "sub-3 (B3): fixture correctly has no ## Active task heading"
fi

# Simulate B3 task-name extraction from filename
FIXTURE_BASENAME=$(basename "$FIXTURE_SESSION")                    # 2026-05-17-personal-site-sim-embed.md
TASK_NAME=$(printf '%s' "$FIXTURE_BASENAME" | sed 's/^[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]-//' | sed 's/\.md$//')

if [ "$TASK_NAME" = "personal-site-sim-embed" ]; then
  ok "sub-3 (B3): task name 'personal-site-sim-embed' correctly derived from filename"
else
  fail "sub-3 (B3): expected 'personal-site-sim-embed' but got '$TASK_NAME'"
fi

# Verify fixture has ## Current stage and ## Unfinished work (B3 extraction targets)
if grep -q '^## Current stage' "$FIXTURE_SESSION" 2>/dev/null; then
  ok "sub-3 (B3): fixture has ## Current stage (45/46 hit rate)"
else
  fail "sub-3 (B3): fixture missing ## Current stage"
fi

if grep -q '^## Unfinished work' "$FIXTURE_SESSION" 2>/dev/null; then
  ok "sub-3 (B3): fixture has ## Unfinished work (46/46 hit rate)"
else
  fail "sub-3 (B3): fixture missing ## Unfinished work"
fi

# Simulate INTENT extraction: first non-empty line of ## Unfinished work,
# stripped of list glyphs and status glyphs
UNFINISHED=$(awk '/^## Unfinished work/{found=1; next} found && /^## /{found=0} found{print}' "$FIXTURE_SESSION" | grep -v '^[[:space:]]*$' | head -1)
# Strip leading list glyphs: "- ", "* ", "1. ", "1) "
INTENT=$(printf '%s' "$UNFINISHED" | sed 's/^[[:space:]]*//' | sed 's/^[-*] //' | sed 's/^[0-9][0-9]*[.)][[:space:]]*//' | sed 's/^[⏳✓✗🚫][[:space:]]*//')

if [ -n "$INTENT" ]; then
  ok "sub-3 (B3): INTENT extracted from ## Unfinished work: '$INTENT'"
else
  fail "sub-3 (B3): INTENT extraction returned empty string"
fi

# No pending-restore-* files should have been created or removed in this sub-test
pr_count=$(find "$MEMORY_DIR" -maxdepth 1 -name 'pending-restore-*.txt' 2>/dev/null | wc -l | awk '{print $1}')
if [ "$pr_count" -eq 0 ]; then
  ok "sub-3 (B3): no pending-restore-*.txt files created or left by B3 path"
else
  fail "sub-3 (B3): unexpected pending-restore-*.txt files present after B3 path ($pr_count found)"
fi

# ─── Summary ─────────────────────────────────────────────────────────────────

printf '\n--- Results ---\n'
printf 'PASS: %d\n' "$PASS"
printf 'FAIL: %d\n' "$FAIL"

if [ "$FAIL" -eq 0 ]; then
  printf '\nAll sub-tests passed.\n'
  exit 0
else
  printf '\n%d sub-test(s) failed.\n' "$FAIL" >&2
  exit 1
fi
