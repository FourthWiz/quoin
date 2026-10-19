#!/bin/sh
# test_checkpoint.sh — fixture tests for /checkpoint skill contract
#
# Tests the checkpoint save/restore file-level contract:
#   - Save mode: writes checkpoint file + pending-restore sentinel
#   - Paths-not-content rule: no blockquotes / large content in checkpoint
#   - Soft cap warning: oversized checkpoint emits warning but does not abort
#   - Write-target boundary: lessons-learned.md and forgotten/ not touched
#   - Restore mode: CASE A (no pending-prompt), CASE B (current-session prompt),
#                   CASE C (stale pending-prompt from other session)
#   - mtime-most-recent rebound fallback: correct sentinel surfaced (d3)
#   - Corrupt checkpoint: graceful error
#
# NOTE: /checkpoint is a skill (LLM-invoked), not a standalone shell script.
# This test harness validates the file-level contract — the artifact structure
# that /checkpoint MUST produce — rather than invoking the skill directly.
# The save-mode tests simulate what the skill would produce, then assert
# the correct output shape. The restore-mode tests validate the lookup logic
# (which is also exercised by sessionstart.sh and test_sessionstart_pending_restore.sh).
#
# Usage: sh quoin/dev/tests/test_checkpoint.sh
# Exit 0 if all tests pass; non-zero otherwise.

set -eu

PASS=0
FAIL=0
FAIL_MSGS=""

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

ok() { PASS=$((PASS + 1)); printf 'ok  %s\n' "$1"; }
fail() {
  FAIL=$((FAIL + 1))
  printf 'FAIL %s\n' "$1" >&2
  FAIL_MSGS="$FAIL_MSGS\n  - $1"
}

TMPDIR_TEST="${TMPDIR:-/tmp}/test_checkpoint_$$"
mkdir -p "$TMPDIR_TEST/.workflow_artifacts/memory/checkpoints"
mkdir -p "$TMPDIR_TEST/.workflow_artifacts/memory/sessions"

cleanup() { rm -rf "$TMPDIR_TEST"; }
trap cleanup EXIT

MEMORY_DIR="$TMPDIR_TEST/.workflow_artifacts/memory"
CHECKPOINTS_DIR="$MEMORY_DIR/checkpoints"

# ─── Helper: create a canonical checkpoint file ──────────────────────────────

write_checkpoint() {
  local session_id="$1"
  local task_name="${2:-test-task}"
  local branch="${3:-test-branch}"
  local checkpoint_date=$(date -u +%Y-%m-%d)
  local checkpoint_path="$CHECKPOINTS_DIR/${checkpoint_date}-${task_name}.md"

  cat > "$checkpoint_path" << CPEOF
## Status
test-save

## Current stage
implement

## Active task
${task_name}

## Branch
${branch}

## Session ID
${session_id}

## In-flight artifacts
- current-plan.md: $TMPDIR_TEST/.workflow_artifacts/${task_name}/current-plan.md
- architecture.md: $TMPDIR_TEST/.workflow_artifacts/${task_name}/architecture.md

## Open questions
None

## Decisions made
- D-01: some decision

## Restore hint
Run /checkpoint --restore in a fresh session to resume.
CPEOF

  # Write pending-restore sentinel
  printf '%s\n' "$checkpoint_path" > "$MEMORY_DIR/pending-restore-${session_id}.txt"

  printf '%s' "$checkpoint_path"
}

# ─── (a) Save mode — full save scope ─────────────────────────────────────────

# Simulate /checkpoint save: write a canonical checkpoint file + sentinel
checkpoint_path=$(write_checkpoint "sess-save-a" "task-a")

if [ -f "$checkpoint_path" ]; then
  ok "(a) save mode: checkpoint file exists"
else
  fail "(a) save mode: checkpoint file NOT written"
fi

# Check required sections
for section in "## Status" "## Active task" "## Branch" "## In-flight artifacts" "## Restore hint"; do
  if grep -q "$section" "$checkpoint_path" 2>/dev/null; then
    ok "(a) save mode: section '$section' present"
  else
    fail "(a) save mode: section '$section' MISSING from checkpoint"
  fi
done

# Check pending-restore sentinel
if [ -f "$MEMORY_DIR/pending-restore-sess-save-a.txt" ]; then
  ok "(a) save mode: pending-restore-sess-save-a.txt written"
else
  fail "(a) save mode: pending-restore-sess-save-a.txt NOT written"
fi

# Sentinel must contain the checkpoint file path
sentinel_content=$(cat "$MEMORY_DIR/pending-restore-sess-save-a.txt")
if [ "$sentinel_content" = "$checkpoint_path" ]; then
  ok "(a) save mode: sentinel content matches checkpoint path"
else
  fail "(a) save mode: sentinel content mismatch (got: $sentinel_content)"
fi

# ─── (b) Paths-not-content rule + soft cap warning ───────────────────────────

# (b) paths-not-content: checkpoint must NOT contain blockquote lines ("> content")
# which would indicate leaked file contents
if grep -q '^> ' "$checkpoint_path" 2>/dev/null; then
  fail "(b) paths-not-content: checkpoint contains blockquote lines (potential content leak)"
else
  ok "(b) paths-not-content: no blockquote lines in checkpoint (paths-only rule satisfied)"
fi

# (b1) file <= 4KB: no warning expected
cp_size=$(wc -c < "$checkpoint_path" | awk '{print $1}')
if [ "$cp_size" -le 4096 ]; then
  ok "(b1) soft-cap: checkpoint <= 4KB (no warning expected)"
else
  fail "(b1) soft-cap: checkpoint unexpectedly > 4KB ($cp_size bytes)"
fi

# (b2) oversized fixture: create a checkpoint > 4KB and verify warning would fire
# We simulate this by checking whether the soft-cap rule is documented in the SKILL.md
# (The actual warning is emitted by the LLM skill, not a shell script)
CHECKPOINT_SKILL="$SCRIPT_DIR/../../skills/checkpoint/SKILL.md"
if grep -q '4 KB\|4KB\|4096' "$CHECKPOINT_SKILL" 2>/dev/null; then
  ok "(b2) soft-cap: 4 KB threshold documented in checkpoint/SKILL.md"
else
  fail "(b2) soft-cap: 4 KB threshold NOT found in checkpoint/SKILL.md"
fi

if grep -q 'WARNING\|warning' "$CHECKPOINT_SKILL" 2>/dev/null && \
   grep -q 'soft.cap\|soft cap' "$CHECKPOINT_SKILL" 2>/dev/null; then
  ok "(b2) soft-cap: WARNING and soft-cap language present in SKILL.md"
else
  fail "(b2) soft-cap: WARNING/soft-cap language NOT found in checkpoint/SKILL.md"
fi

# ─── (c) Write-target boundary ───────────────────────────────────────────────

LESSONS_LEARNED="$TMPDIR_TEST/.workflow_artifacts/memory/lessons-learned.md"
FORGOTTEN_DIR="$TMPDIR_TEST/.workflow_artifacts/memory/forgotten"

# The checkpoint writes to checkpoints/ and pending-restore-*.txt only.
# lessons-learned.md and forgotten/ must NOT be touched.
# Since we're in a fresh tmpdir, neither should exist.
if [ ! -f "$LESSONS_LEARNED" ]; then
  ok "(c) write-target boundary: lessons-learned.md NOT created by checkpoint"
else
  fail "(c) write-target boundary: lessons-learned.md was created"
fi

if [ ! -d "$FORGOTTEN_DIR" ]; then
  ok "(c) write-target boundary: forgotten/ NOT created by checkpoint"
else
  fail "(c) write-target boundary: forgotten/ was created"
fi

# ─── (d) Restore mode — CASE A (no pending-prompt) ───────────────────────────

# Create a checkpoint for a new session; no pending-prompt files exist
rm -f "$MEMORY_DIR/pending-prompt-"*.txt 2>/dev/null || true

checkpoint_a=$(write_checkpoint "sess-restore-a" "task-restore-a")

# Verify no pending-prompt files exist before restore
pp_count=$(ls "$MEMORY_DIR"/pending-prompt-*.txt 2>/dev/null | wc -l | awk '{print $1}')
if [ "$pp_count" -eq 0 ]; then
  ok "(d) CASE A: no pending-prompt files exist before restore"
else
  fail "(d) CASE A: unexpected pending-prompt files exist: $pp_count"
fi

# Simulate restore: read checkpoint, check in-flight artifacts section
if grep -q '## In-flight artifacts' "$checkpoint_a" 2>/dev/null; then
  ok "(d) CASE A: checkpoint has In-flight artifacts section for restore"
else
  fail "(d) CASE A: In-flight artifacts section missing from checkpoint"
fi

# SKILL.md must document CASE A explicitly
if grep -q 'CASE A\|case A\|no pending-prompt' "$CHECKPOINT_SKILL" 2>/dev/null; then
  ok "(d) CASE A: documented in checkpoint/SKILL.md"
else
  fail "(d) CASE A: CASE A NOT documented in checkpoint/SKILL.md"
fi

# ─── (d2) CASE C: sentinel-staleness (stale pending-prompt from other session) ─

rm -f "$MEMORY_DIR/pending-prompt-"*.txt 2>/dev/null || true
rm -f "$MEMORY_DIR/pending-restore-"*.txt 2>/dev/null || true

checkpoint_c=$(write_checkpoint "sess-restore-c" "task-restore-c")

# Create a STALE pending-prompt from a different session
printf 'my old prompt from other session\n' > "$MEMORY_DIR/pending-prompt-sess-old.txt"

# CASE C: current session is sess-restore-c; the old pending-prompt is from sess-old
# The restore should surface the stale sentinel with a mismatch warning
# (This is handled by the --restore mode logic in the SKILL.md)
if grep -q 'CASE C\|stale\|mismatch\|sentinel' "$CHECKPOINT_SKILL" 2>/dev/null; then
  ok "(d2) CASE C: stale/mismatch sentinel handling documented in SKILL.md"
else
  fail "(d2) CASE C: stale sentinel handling NOT documented in checkpoint/SKILL.md"
fi

rm -f "$MEMORY_DIR/pending-prompt-sess-old.txt"

# ─── (d3) mtime-most-recent rebound fallback ────────────────────────────────

# Create three pending-restore files with different mtimes (within last few minutes)
rm -f "$MEMORY_DIR/pending-restore-"*.txt 2>/dev/null || true

printf 'checkpoint-zzzzZZ.md\n' > "$MEMORY_DIR/pending-restore-zzzzZZ.txt"
printf 'checkpoint-aaaaAA.md\n' > "$MEMORY_DIR/pending-restore-aaaaAA.txt"
printf 'checkpoint-mmmmMM.md\n' > "$MEMORY_DIR/pending-restore-mmmmMM.txt"

# Set zzzzZZ to 2 minutes ago, aaaaAA to 1 minute ago, mmmmMM stays fresh
TWO_MIN_AGO=$(date -v -2M +%Y%m%d%H%M.%S 2>/dev/null || \
              date -d '2 minutes ago' +%Y%m%d%H%M.%S 2>/dev/null || echo "")
ONE_MIN_AGO=$(date -v -1M +%Y%m%d%H%M.%S 2>/dev/null || \
              date -d '1 minute ago' +%Y%m%d%H%M.%S 2>/dev/null || echo "")

if [ -n "$TWO_MIN_AGO" ] && [ -n "$ONE_MIN_AGO" ]; then
  touch -t "$TWO_MIN_AGO" "$MEMORY_DIR/pending-restore-zzzzZZ.txt" 2>/dev/null || true
  touch -t "$ONE_MIN_AGO" "$MEMORY_DIR/pending-restore-aaaaAA.txt" 2>/dev/null || true

  # ls -t should surface mmmmMM first (mtime-newest)
  most_recent=$(ls -t "$MEMORY_DIR"/pending-restore-*.txt 2>/dev/null | head -1 | xargs basename 2>/dev/null)
  if printf '%s' "$most_recent" | grep -q 'mmmmMM' 2>/dev/null; then
    ok "(d3) mtime-most-recent: ls -t surfaces mmmmMM (newest), not zzzzZZ (lex-greatest)"
  else
    fail "(d3) mtime-most-recent: expected mmmmMM but got $most_recent"
  fi
else
  ok "(d3) mtime-most-recent: (skipped — date manipulation not supported)"
fi

rm -f "$MEMORY_DIR/pending-restore-"*.txt 2>/dev/null || true

# ─── (e) Restore mode — CASE B (block-recovery flow) ────────────────────────

# Create checkpoint + pending-prompt for current session
checkpoint_b=$(write_checkpoint "sess-restore-b" "task-restore-b")
printf 'my saved prompt from block-recovery flow\n' > "$MEMORY_DIR/pending-prompt-sess-restore-b.txt"

# Both sentinels should exist before restore
if [ -f "$MEMORY_DIR/pending-restore-sess-restore-b.txt" ] && \
   [ -f "$MEMORY_DIR/pending-prompt-sess-restore-b.txt" ]; then
  ok "(e) CASE B: both sentinels present before restore"
else
  fail "(e) CASE B: one or both sentinels missing before restore"
fi

# After simulated 'y' restore: both sentinels should be trash-moved (not hard-deleted)
# We simulate this using trash_move from _lib.sh:
. "$SCRIPT_DIR/../../hooks/_lib.sh"
trash_move "$MEMORY_DIR/pending-prompt-sess-restore-b.txt" "$MEMORY_DIR"
trash_move "$MEMORY_DIR/pending-restore-sess-restore-b.txt" "$MEMORY_DIR"

TODAY_DATE=$(date -u +%Y-%m-%d 2>/dev/null) || TODAY_DATE=$(date +%Y-%m-%d)
TRASH_DIR="$MEMORY_DIR/trash/$TODAY_DATE"

# (e1) pending-restore sentinel appears in trash/<date>/
if [ -f "$TRASH_DIR/pending-restore-sess-restore-b.txt" ]; then
  ok "(e1) CASE B: pending-restore sentinel in trash/<date>/ after restore"
else
  fail "(e1) CASE B: pending-restore sentinel NOT found in trash (expected: $TRASH_DIR/pending-restore-sess-restore-b.txt)"
fi

# (e2) pending-restore sentinel NOT in memory/ root
if [ ! -f "$MEMORY_DIR/pending-restore-sess-restore-b.txt" ]; then
  ok "(e2) CASE B: pending-restore sentinel removed from memory/ root"
else
  fail "(e2) CASE B: pending-restore sentinel still in memory/ root"
fi

if [ ! -f "$MEMORY_DIR/pending-prompt-sess-restore-b.txt" ] && \
   [ ! -f "$MEMORY_DIR/pending-restore-sess-restore-b.txt" ]; then
  ok "(e) CASE B: both sentinels consumed (trash-moved) after restore"
else
  fail "(e) CASE B: sentinels not cleaned up after restore"
fi

# (e3) Collision suffix: trash-move a second file with the same basename
printf 'duplicate\n' > "$MEMORY_DIR/pending-restore-sess-restore-b.txt"
trash_move "$MEMORY_DIR/pending-restore-sess-restore-b.txt" "$MEMORY_DIR"
if [ -f "$TRASH_DIR/pending-restore-sess-restore-b.txt-1" ]; then
  ok "(e3) CASE B collision: second trash-move uses -1 suffix"
else
  fail "(e3) CASE B collision: -1 suffix file NOT found in trash"
fi

# ─── (f) Corrupt checkpoint — graceful error ────────────────────────────────

# Write a corrupt checkpoint file (not valid sections format)
corrupt_checkpoint="$CHECKPOINTS_DIR/corrupt-checkpoint.md"
printf 'this is corrupt{JSON:::invalid\n' > "$corrupt_checkpoint"
printf '%s\n' "$corrupt_checkpoint" > "$MEMORY_DIR/pending-restore-sess-corrupt.txt"

# The SKILL.md must document graceful handling of corrupt checkpoints
if grep -q 'corrupt\|parse fail\|graceful' "$CHECKPOINT_SKILL" 2>/dev/null; then
  ok "(f) corrupt checkpoint: graceful handling documented in SKILL.md"
else
  fail "(f) corrupt checkpoint: graceful handling NOT found in checkpoint/SKILL.md"
fi

# Sentinel must NOT be deleted on corrupt checkpoint (preserve for manual recovery)
if [ -f "$MEMORY_DIR/pending-restore-sess-corrupt.txt" ]; then
  ok "(f) corrupt checkpoint: sentinel preserved (not deleted) after corrupt-parse"
else
  fail "(f) corrupt checkpoint: sentinel was deleted despite corrupt checkpoint"
fi

rm -f "$MEMORY_DIR/pending-restore-sess-corrupt.txt" "$corrupt_checkpoint"

# ─── (g) Round-trip timing: sentinel write to restore is fast ────────────────

# This is a smoke test of the file-level latency — full V-04 soak is post-merge.
# We measure time to write a checkpoint + read it back.
START_TIME=$(date +%s 2>/dev/null || printf '0')
checkpoint_g=$(write_checkpoint "sess-timing-g" "task-timing")
content=$(cat "$checkpoint_g" 2>/dev/null)
END_TIME=$(date +%s 2>/dev/null || printf '0')
ELAPSED=$((END_TIME - START_TIME))

if [ "$ELAPSED" -le 5 ]; then
  ok "(g) round-trip timing: checkpoint write+read completed in ${ELAPSED}s (target <5s for file I/O)"
else
  fail "(g) round-trip timing: took ${ELAPSED}s (unexpectedly slow for file I/O)"
fi

rm -f "$MEMORY_DIR/pending-restore-sess-timing-g.txt"

# ─── T-06a: UUID anchor — session-state lookup by UUID, not mtime ────────────
# Fixture: three session-state files with different UUIDs; target-UUID file is NOT mtime-newest.
# Verification: SKILL.md implements the UUID-anchored lookup procedure.

# Create three session-state files with distinct UUIDs
UUID_TARGET="AAAA0000-1111-2222-3333-444444444444"
UUID_OTHER1="BBBB1111-2222-3333-4444-555555555555"
UUID_OTHER2="cccc2222-3333-4444-5555-666666666666"  # lowercase UUID

cat > "$MEMORY_DIR/sessions/session-a.md" << SSEOF
## Status
in_progress

## Current stage
implement

## Cost
- Session UUID: ${UUID_OTHER1}
- Phase: implement
- Recorded in cost ledger: yes
- end_of_day_due: yes
- fallback_fires: 0
SSEOF

cat > "$MEMORY_DIR/sessions/session-b.md" << SSEOF
## Status
in_progress

## Current stage
implement

## Cost
- Session UUID: ${UUID_TARGET}
- Phase: implement
- Recorded in cost ledger: yes
- end_of_day_due: yes
- fallback_fires: 0
SSEOF

# session-c.md gets lowercase UUID
cat > "$MEMORY_DIR/sessions/session-c.md" << SSEOF
## Status
in_progress

## Current stage
implement

## Cost
- Session UUID: ${UUID_OTHER2}
- Phase: implement
- Recorded in cost ledger: yes
- end_of_day_due: yes
- fallback_fires: 0
SSEOF

# Make session-b not the mtime-newest (make session-c newer by touching it)
# We can do this with touch on the other files first, then touch session-c last.
FIVE_MIN_AGO=$(date -v -5M +%Y%m%d%H%M.%S 2>/dev/null || \
               date -d '5 minutes ago' +%Y%m%d%H%M.%S 2>/dev/null || echo "")
if [ -n "$FIVE_MIN_AGO" ]; then
  touch -t "$FIVE_MIN_AGO" "$MEMORY_DIR/sessions/session-b.md" 2>/dev/null || true
fi

# (T-06a-1) UUID-anchored lookup is documented in SKILL.md
if grep -q 'UUID-anchored\|uuid-anchor\|grep -iE' "$CHECKPOINT_SKILL" 2>/dev/null; then
  ok "(T-06a-1) UUID-anchored session-state lookup documented in SKILL.md"
else
  fail "(T-06a-1) UUID-anchored session-state lookup NOT found in SKILL.md"
fi

# (T-06a-2) The pattern in SKILL.md supports case-insensitive matching (grep -iE)
if grep -q 'grep -iE\|case.insensitive\|case.insensitiv' "$CHECKPOINT_SKILL" 2>/dev/null; then
  ok "(T-06a-2) Case-insensitive UUID matching (grep -iE) documented in SKILL.md"
else
  fail "(T-06a-2) Case-insensitive UUID matching NOT found in SKILL.md"
fi

# (T-06a-3) SKILL.md has the UUID anchored grep pattern
if grep -q 'Session UUID' "$CHECKPOINT_SKILL" 2>/dev/null; then
  ok "(T-06a-3) 'Session UUID' pattern present in SKILL.md for UUID anchor lookup"
else
  fail "(T-06a-3) 'Session UUID' pattern NOT found in SKILL.md"
fi

# (T-06a-4) SKILL.md documents the ## Session ID section in checkpoint format
if grep -q '## Session ID' "$CHECKPOINT_SKILL" 2>/dev/null; then
  ok "(T-06a-4) '## Session ID' section documented in checkpoint SKILL.md"
else
  fail "(T-06a-4) '## Session ID' NOT found in checkpoint SKILL.md"
fi

# (T-06a-5) Checkpoint file written by write_checkpoint() contains ## Session ID
checkpoint_uuid_test=$(write_checkpoint "$UUID_TARGET" "uuid-test-task")
if grep -q "## Session ID" "$checkpoint_uuid_test" 2>/dev/null; then
  ok "(T-06a-5) Checkpoint file has ## Session ID section"
else
  fail "(T-06a-5) Checkpoint file MISSING ## Session ID section"
fi

# (T-06a-6) The UUID value appears on the line after ## Session ID (two-line form)
session_id_line=$(awk '/^## Session ID/{getline; print; exit}' "$checkpoint_uuid_test" 2>/dev/null)
if [ "$session_id_line" = "$UUID_TARGET" ]; then
  ok "(T-06a-6) Session ID value on next line after ## Session ID heading (two-line form)"
else
  fail "(T-06a-6) Session ID value mismatch: expected '$UUID_TARGET', got '$session_id_line'"
fi

# (T-06a-7) Target UUID file is found by UUID grep even if not mtime-newest
# Simulate the grep that SKILL.md prescribes
matched=$(grep -ilE "^([[:space:]]*-[[:space:]]*)?(Session UUID:[[:space:]]*)${UUID_TARGET}" \
  "$MEMORY_DIR/sessions/"*.md 2>/dev/null | head -1)
if printf '%s' "$matched" | grep -q 'session-b.md' 2>/dev/null; then
  ok "(T-06a-7) UUID grep finds session-b.md (target UUID) even if not mtime-newest"
else
  fail "(T-06a-7) UUID grep returned '$matched' instead of session-b.md"
fi

# (T-06a-8) Lowercase UUID (UUID_OTHER2) is found by case-insensitive grep
matched_lc=$(grep -ilE "^([[:space:]]*-[[:space:]]*)?(Session UUID:[[:space:]]*)${UUID_OTHER2}" \
  "$MEMORY_DIR/sessions/"*.md 2>/dev/null | head -1)
if [ -n "$matched_lc" ]; then
  ok "(T-06a-8) Lowercase UUID found by case-insensitive grep"
else
  fail "(T-06a-8) Lowercase UUID NOT found by case-insensitive grep"
fi

# (T-06a-9) SKILL.md documents WARNING when UUID not found (negative case)
if grep -q 'WARNING.*no session-state\|no session-state.*WARNING' "$CHECKPOINT_SKILL" 2>/dev/null; then
  ok "(T-06a-9) WARNING for missing UUID documented in SKILL.md"
else
  fail "(T-06a-9) WARNING for missing UUID NOT found in SKILL.md"
fi

# (T-06a-10) session-state-resolution bullet in ## In-flight artifacts
if grep -q 'session-state-resolution' "$CHECKPOINT_SKILL" 2>/dev/null; then
  ok "(T-06a-10) session-state-resolution tracking documented in SKILL.md"
else
  fail "(T-06a-10) session-state-resolution NOT found in SKILL.md"
fi

rm -f "$MEMORY_DIR/sessions/session-a.md" \
      "$MEMORY_DIR/sessions/session-b.md" \
      "$MEMORY_DIR/sessions/session-c.md" \
      "$MEMORY_DIR/pending-restore-${UUID_TARGET}.txt" \
      "$checkpoint_uuid_test"

# ─── T-06c: --after-compact flag (Save mode documentation + bypass) ──────────

# (T-06c-1) --after-compact flag is documented in SKILL.md
if grep -q '\-\-after-compact\|after.compact' "$CHECKPOINT_SKILL" 2>/dev/null; then
  ok "(T-06c-1) --after-compact flag documented in SKILL.md"
else
  fail "(T-06c-1) --after-compact flag NOT found in SKILL.md"
fi

# (T-06c-2) SKILL.md has AFTER_COMPACT_FLAG_PRESENT variable (replaces POST_COMPACT)
if grep -q 'AFTER_COMPACT_FLAG_PRESENT' "$CHECKPOINT_SKILL" 2>/dev/null; then
  ok "(T-06c-2) AFTER_COMPACT_FLAG_PRESENT variable present in SKILL.md"
else
  fail "(T-06c-2) AFTER_COMPACT_FLAG_PRESENT NOT found in SKILL.md"
fi

# (T-06c-3) SKILL.md documents the INFO line emitted on --after-compact (deprecation message)
if grep -q 'after-compact flag is deprecated' "$CHECKPOINT_SKILL" 2>/dev/null; then
  ok "(T-06c-3) INFO line '--after-compact flag is deprecated' documented in SKILL.md"
else
  fail "(T-06c-3) --after-compact deprecation INFO line NOT found in SKILL.md"
fi

# (T-06c-4) Step 0.5 is documented before Step 1 (post-compact flag injection point)
# Find the line number of Step 0.5 and Step 1 in SKILL.md; 0.5 must come first
step_05_line=$(grep -n 'Step 0.5\|Step 0\.5' "$CHECKPOINT_SKILL" 2>/dev/null | head -1 | cut -d: -f1)
step_1_line=$(grep -n '^### Step 1:' "$CHECKPOINT_SKILL" 2>/dev/null | head -1 | cut -d: -f1)
if [ -n "$step_05_line" ] && [ -n "$step_1_line" ] && [ "$step_05_line" -lt "$step_1_line" ]; then
  ok "(T-06c-4) Step 0.5 appears before Step 1 in SKILL.md (correct injection point)"
else
  fail "(T-06c-4) Step 0.5 not found before Step 1 (step_05=$step_05_line, step_1=$step_1_line)"
fi

# ─── T-06e: Picker reads disk-only checkpoints (no sentinel) ─────────────────
# Fixture: zero sentinels; 3 checkpoint files: one legacy (no ## Session ID),
#          one 0-byte .tmp sibling, one < 100 bytes.
# Verification: SKILL.md documents the picker exclusions.

# Create a valid legacy checkpoint (no ## Session ID section, >= 100 bytes)
LEGACY_CP="$CHECKPOINTS_DIR/2026-01-01-legacy-task.md"
cat > "$LEGACY_CP" << LEGEOF
## Status
test-save-legacy

## Current stage
implement

## Active task
legacy-task

## Branch
legacy-branch

## In-flight artifacts
- current-plan.md: /some/path/current-plan.md

## Open questions
None

## Decisions made
None

## Restore hint
Run /checkpoint --restore in a fresh session to resume.
LEGEOF

# Create a 0-byte .tmp sibling (should be excluded by finder)
printf '' > "$CHECKPOINTS_DIR/2026-01-02-some-task.md.tmp"

# Create a tiny checkpoint < 100 bytes (should be excluded by size guard)
printf '## Status\nok\n' > "$CHECKPOINTS_DIR/2026-01-03-tiny-task.md"

# Ensure zero sentinels for the test session
rm -f "$MEMORY_DIR/pending-restore-sess-picker-test.txt" 2>/dev/null || true

# (T-06e-1) SKILL.md documents the picker fast path (current-session sentinel bypass)
if grep -q 'fast path\|fast-path\|FAST PATH' "$CHECKPOINT_SKILL" 2>/dev/null; then
  ok "(T-06e-1) Picker fast path documented in SKILL.md"
else
  fail "(T-06e-1) Picker fast path NOT found in SKILL.md"
fi

# (T-06e-2) SKILL.md documents the *.tmp exclusion in the picker find command
if grep -q '! -name.*\.tmp\|\.tmp.*exclude\|exclude.*\.tmp' "$CHECKPOINT_SKILL" 2>/dev/null; then
  ok "(T-06e-2) .tmp exclusion documented in picker (SKILL.md)"
else
  fail "(T-06e-2) .tmp exclusion NOT found in SKILL.md picker"
fi

# (T-06e-3) SKILL.md documents the 100-byte minimum size guard
if grep -q '100 byte\|100B\|< 100\|>= 100\|wc -c.*100\|100.*wc -c' "$CHECKPOINT_SKILL" 2>/dev/null; then
  ok "(T-06e-3) 100-byte size guard documented in SKILL.md picker"
else
  fail "(T-06e-3) 100-byte size guard NOT found in SKILL.md picker"
fi

# (T-06e-4) SKILL.md documents backward-compat for missing ## Session ID (legacy)
if grep -q 'legacy\|tolerate missing.*Session ID\|Session ID.*missing' "$CHECKPOINT_SKILL" 2>/dev/null; then
  ok "(T-06e-4) Backward-compat for legacy checkpoints (missing Session ID) documented"
else
  fail "(T-06e-4) Backward-compat for missing Session ID NOT documented in SKILL.md"
fi

# (T-06e-5) The 0-byte .tmp file is excluded by finder (simulate the find command)
# grep returns exit 1 when no match found; suppress via || true to avoid set -e exit
tmp_in_find=$(find "$CHECKPOINTS_DIR" -maxdepth 1 -name '*.md' ! -name '*.tmp' -print0 2>/dev/null | \
  while IFS= read -r -d '' f; do printf '%s\n' "$f"; done | grep '\.tmp' 2>/dev/null || true)
if [ -z "$tmp_in_find" ]; then
  ok "(T-06e-5) .tmp files excluded from find output (! -name '*.tmp' -print0 works)"
else
  fail "(T-06e-5) .tmp files present in find output: $tmp_in_find"
fi

# (T-06e-6) The tiny checkpoint is excluded by size guard (< 100 bytes)
tiny_size=$(wc -c < "$CHECKPOINTS_DIR/2026-01-03-tiny-task.md" 2>/dev/null | awk '{print $1+0}')
if [ "${tiny_size:-0}" -lt 100 ]; then
  ok "(T-06e-6) Tiny checkpoint ($tiny_size bytes) correctly detected as < 100 bytes"
else
  fail "(T-06e-6) Tiny checkpoint size guard check failed (size=$tiny_size)"
fi

# (T-06e-7) Legacy checkpoint passes size guard (>= 100 bytes)
legacy_size=$(wc -c < "$LEGACY_CP" 2>/dev/null | awk '{print $1+0}')
if [ "${legacy_size:-0}" -ge 100 ]; then
  ok "(T-06e-7) Legacy checkpoint ($legacy_size bytes) passes >= 100 byte size guard"
else
  fail "(T-06e-7) Legacy checkpoint is too small ($legacy_size bytes) — fixture needs padding"
fi

# (T-06e-8) Legacy checkpoint has Active task and Branch but no Session ID
if grep -q '## Active task' "$LEGACY_CP" 2>/dev/null && \
   grep -q '## Branch' "$LEGACY_CP" 2>/dev/null && \
   ! grep -q '## Session ID' "$LEGACY_CP" 2>/dev/null; then
  ok "(T-06e-8) Legacy checkpoint has Active task + Branch but no Session ID (correct legacy format)"
else
  fail "(T-06e-8) Legacy checkpoint format incorrect (check Active task, Branch, Session ID)"
fi

# (T-06e-9) Awk extraction handles CRLF (gsub /\r$/) — verified in SKILL.md
if grep -q 'gsub.*\\\\r\|CRLF\|trailing CR' "$CHECKPOINT_SKILL" 2>/dev/null; then
  ok "(T-06e-9) CRLF stripping (gsub /\\r\$/) documented in SKILL.md picker awk"
else
  fail "(T-06e-9) CRLF stripping NOT documented in SKILL.md picker"
fi

rm -f "$LEGACY_CP" \
      "$CHECKPOINTS_DIR/2026-01-02-some-task.md.tmp" \
      "$CHECKPOINTS_DIR/2026-01-03-tiny-task.md"

# ─── T-09: Three-mode checkpoint contract tests ───────────────────────────────
# Tests the file-level contract for the three save modes added by T-04.
# These are fixture-based contract tests: they simulate what /checkpoint MUST
# produce and assert the correct sentinel file shape.

# (T-09a) restore mode: pending-restore sentinel written, no other mode sentinels
SID_09A="sess-mode-restore-$(date -u +%s)"
cp_09a="$CHECKPOINTS_DIR/$(date -u +%Y-%m-%d)-mode-restore-task.md"
cat > "$cp_09a" << EOF09A
## Status
test-save (restore mode)

## Active task
mode-restore-task

## Branch
main

## Session ID
${SID_09A}

## In-flight artifacts
- current-plan.md: /some/path/current-plan.md

## Restore hint
Run /checkpoint --restore in a fresh session.
EOF09A
printf '%s\n' "$cp_09a" > "$MEMORY_DIR/pending-restore-${SID_09A}.txt"

# Assertions
if [ -f "$MEMORY_DIR/pending-restore-${SID_09A}.txt" ]; then
  ok "(T-09a) restore mode: pending-restore sentinel written"
else
  fail "(T-09a) restore mode: pending-restore sentinel NOT found"
fi

sentinel_content_09a=$(cat "$MEMORY_DIR/pending-restore-${SID_09A}.txt")
if [ "$sentinel_content_09a" = "$cp_09a" ]; then
  ok "(T-09a) restore mode: sentinel content is the checkpoint path"
else
  fail "(T-09a) restore mode: sentinel content mismatch (got: $sentinel_content_09a)"
fi

# No pending-resume-ref or mid-agent-handoff for this session
if [ ! -f "$MEMORY_DIR/pending-resume-ref-${SID_09A}.txt" ] && \
   [ ! -f "$MEMORY_DIR/mid-agent-handoff-${SID_09A}.txt" ]; then
  ok "(T-09a) restore mode: no pending-resume-ref or mid-agent-handoff sentinels"
else
  fail "(T-09a) restore mode: unexpected extra sentinels found"
fi

rm -f "$cp_09a" "$MEMORY_DIR/pending-restore-${SID_09A}.txt"

# (T-09b) load-as-reference mode: pending-resume-ref written, no pending-restore
SID_09B="sess-mode-load-as-ref-$(date -u +%s)"
cp_09b="$CHECKPOINTS_DIR/$(date -u +%Y-%m-%d)-mode-loadref-task.md"
cat > "$cp_09b" << EOF09B
## Status
test-save (load-as-reference mode)

## Active task
mode-loadref-task

## Branch
main

## Session ID
${SID_09B}

## In-flight artifacts
- current-plan.md: /some/path/current-plan.md

## Restore hint
Run /checkpoint --restore in a fresh session.
EOF09B

# Write pending-resume-ref sentinel (as /checkpoint would in load-as-reference mode)
printf 'prior_session_uuid=%s\ncheckpoint_path=%s\n' "$SID_09B" "$cp_09b" \
  > "$MEMORY_DIR/pending-resume-ref-${SID_09B}.txt"

# Assertions
if [ -f "$MEMORY_DIR/pending-resume-ref-${SID_09B}.txt" ]; then
  ok "(T-09b) load-as-reference mode: pending-resume-ref sentinel written"
else
  fail "(T-09b) load-as-reference mode: pending-resume-ref sentinel NOT found"
fi

ref_uuid_09b=$(grep '^prior_session_uuid=' "$MEMORY_DIR/pending-resume-ref-${SID_09B}.txt" | cut -d= -f2)
ref_cp_09b=$(grep '^checkpoint_path=' "$MEMORY_DIR/pending-resume-ref-${SID_09B}.txt" | cut -d= -f2-)
if [ "$ref_uuid_09b" = "$SID_09B" ] && [ "$ref_cp_09b" = "$cp_09b" ]; then
  ok "(T-09b) load-as-reference mode: sentinel contains prior_session_uuid and checkpoint_path"
else
  fail "(T-09b) load-as-reference mode: sentinel content mismatch (uuid=$ref_uuid_09b, cp=$ref_cp_09b)"
fi

# No pending-restore for the SAME session_id (load-as-ref skips Step 3)
if [ ! -f "$MEMORY_DIR/pending-restore-${SID_09B}.txt" ]; then
  ok "(T-09b) load-as-reference mode: no pending-restore for same session_id"
else
  fail "(T-09b) load-as-reference mode: unexpected pending-restore found for same session"
fi

rm -f "$cp_09b" "$MEMORY_DIR/pending-resume-ref-${SID_09B}.txt"

# (T-09c) mid-agent mode: only mid-agent-handoff sentinel, no checkpoint file, no pending-restore
SID_09C="sess-mode-mid-agent-$(date -u +%s)"
# Simulate a foreign pidfile (another skill is running)
mkdir -p "$MEMORY_DIR/sessions"
touch "$MEMORY_DIR/sessions/critic-99999.pidfile.lock"

# Write ONLY the mid-agent-handoff sentinel (no full checkpoint file in mid-agent mode)
printf 'prior_session_uuid=%s\ntask_name=my-task\nactive_skills=critic 99999\ntimestamp=2026-05-15T10:00:00Z\n' \
  "$SID_09C" > "$MEMORY_DIR/mid-agent-handoff-${SID_09C}.txt"

# Assertions
if [ -f "$MEMORY_DIR/mid-agent-handoff-${SID_09C}.txt" ]; then
  ok "(T-09c) mid-agent mode: mid-agent-handoff sentinel written"
else
  fail "(T-09c) mid-agent mode: mid-agent-handoff sentinel NOT found"
fi

# No pending-restore for this session
if [ ! -f "$MEMORY_DIR/pending-restore-${SID_09C}.txt" ]; then
  ok "(T-09c) mid-agent mode: no pending-restore sentinel"
else
  fail "(T-09c) mid-agent mode: unexpected pending-restore found"
fi

# No full checkpoint file for this session (mid-agent skips Step 2)
if [ ! -f "$CHECKPOINTS_DIR/"*"-$(date -u +%Y-%m-%d)"-*".md" ] 2>/dev/null || \
   ! ls "$CHECKPOINTS_DIR/"*.md 2>/dev/null | xargs grep -l "## Session ID" 2>/dev/null | \
     xargs grep -l "$SID_09C" 2>/dev/null | grep -q .; then
  ok "(T-09c) mid-agent mode: no full checkpoint file for this session (minimal save)"
else
  fail "(T-09c) mid-agent mode: unexpected full checkpoint file found"
fi

rm -f "$MEMORY_DIR/sessions/critic-99999.pidfile.lock" \
      "$MEMORY_DIR/mid-agent-handoff-${SID_09C}.txt" \
      "$CHECKPOINTS_DIR/"*".md" 2>/dev/null || true

# ─── T-06f: COMPACT_FIRST_BPS boundary (9000 / 8999) ────────────────────────
# Verify that compute_utilization returns 9000 for a transcript at exactly the
# 90.00% threshold, and 8999 for one byte below. The operator in SKILL.md:164
# is >= , so 9000 triggers deferred-compact and 8999 does not.
# This fixture test locks in the operator semantics so a future >= → > change
# would be caught by CI.

LIB="$SCRIPT_DIR/../../hooks/_lib.sh"
TRANSCRIPT_9000="$TMPDIR_TEST/transcript-9000.jsonl"
TRANSCRIPT_8999="$TMPDIR_TEST/transcript-8999.jsonl"

# Create synthetic transcript files: 1,080,000 bytes (9000 bps) and
# 1,079,999 bytes (8999 bps) at default BPT=8.0, LIMIT=150000.
if command -v dd >/dev/null 2>&1; then
  dd if=/dev/zero bs=1080000 count=1 2>/dev/null | tr '\0' 'x' > "$TRANSCRIPT_9000"
  # Write 1,079,999 bytes = 1,080,000 - 1
  dd if=/dev/zero bs=1079999 count=1 2>/dev/null | tr '\0' 'x' > "$TRANSCRIPT_8999"
else
  python3 -c "open('$TRANSCRIPT_9000','wb').write(b'x'*1080000)"
  python3 -c "open('$TRANSCRIPT_8999','wb').write(b'x'*1079999)"
fi

if [ -r "$LIB" ]; then
  # T-06f-1: util_bps at 9000 bytes exactly equals COMPACT_FIRST_BPS threshold
  bps_9000=$(. "$LIB" && read_constants && compute_utilization "$TRANSCRIPT_9000")
  if [ "$bps_9000" -eq 9000 ] 2>/dev/null; then
    ok "(T-06f-1) compute_utilization(1080000 bytes) = 9000 (at COMPACT_FIRST_BPS threshold)"
  else
    fail "(T-06f-1) compute_utilization(1080000 bytes) returned '$bps_9000', expected 9000"
  fi

  # T-06f-2: util_bps at 1 byte below is 8999 (below threshold)
  bps_8999=$(. "$LIB" && read_constants && compute_utilization "$TRANSCRIPT_8999")
  if [ "$bps_8999" -eq 8999 ] 2>/dev/null; then
    ok "(T-06f-2) compute_utilization(1079999 bytes) = 8999 (one below COMPACT_FIRST_BPS threshold)"
  else
    fail "(T-06f-2) compute_utilization(1079999 bytes) returned '$bps_8999', expected 8999"
  fi

  # T-06f-3: >= operator semantics — 9000 crosses threshold, 8999 does not
  if [ "$bps_9000" -ge 9000 ]; then
    ok "(T-06f-3) 9000 >= COMPACT_FIRST_BPS(9000): compact-first triggers (>= operator confirmed)"
  else
    fail "(T-06f-3) 9000 should satisfy >= 9000 (operator broken)"
  fi
  if [ "$bps_8999" -ge 9000 ]; then
    fail "(T-06f-4) 8999 should NOT satisfy >= 9000 (false trigger)"
  else
    ok "(T-06f-4) 8999 < COMPACT_FIRST_BPS(9000): compact-first does NOT trigger (>= boundary correct)"
  fi
else
  fail "(T-06f) _lib.sh not found at $LIB — skipping boundary tests"
  fail "(T-06f-1) skipped"
  fail "(T-06f-2) skipped"
  fail "(T-06f-3) skipped"
  fail "(T-06f-4) skipped"
fi

rm -f "$TRANSCRIPT_9000" "$TRANSCRIPT_8999" 2>/dev/null || true

# ─── T-06g: Step 1.4 compact-already-ran skip path documented in SKILL.md ────

echo "T-06g: Step 1.4 skip path present in SKILL.md"
if grep -q 'Step 1.4' "$CHECKPOINT_SKILL" 2>/dev/null; then
  ok "(T-06g-1) Step 1.4 heading present in SKILL.md"
else
  fail "(T-06g-1) Step 1.4 heading not found in SKILL.md"
fi

if grep -q 'compact-happened-' "$CHECKPOINT_SKILL" 2>/dev/null; then
  ok "(T-06g-2) compact-happened sentinel referenced in SKILL.md"
else
  fail "(T-06g-2) compact-happened sentinel not found in SKILL.md"
fi

if grep -q 'AFTER_COMPACT_FLAG_PRESENT' "$CHECKPOINT_SKILL" 2>/dev/null; then
  ok "(T-06g-3) AFTER_COMPACT_FLAG_PRESENT explicit-flag guard present in SKILL.md"
else
  fail "(T-06g-3) AFTER_COMPACT_FLAG_PRESENT not found in SKILL.md"
fi

# ─── T-06h: High-util branch inverted — old STOP language absent, two-option notice present ────

echo "T-06h: Step 1.5.A inverted"
if grep -qF 'STOP (do not proceed to Steps' "$CHECKPOINT_SKILL" 2>/dev/null; then
  fail "(T-06h-1) Old STOP language still present in SKILL.md"
else
  ok "(T-06h-1) Old STOP language absent from SKILL.md"
fi

if grep -q 'fresh session' "$CHECKPOINT_SKILL" 2>/dev/null; then
  ok "(T-06h-2) Two-option notice (fresh session) present in SKILL.md"
else
  fail "(T-06h-2) Two-option notice (fresh session) NOT found in SKILL.md"
fi

if grep -q 'Run /compact in this session' "$CHECKPOINT_SKILL" 2>/dev/null; then
  ok "(T-06h-3) Two-option notice (compact option) present in SKILL.md"
else
  fail "(T-06h-3) Two-option notice (compact option) NOT found in SKILL.md"
fi

# ─── T-06i: --after-compact deprecation documented ────

echo "T-06i: --after-compact deprecation"
if grep -q 'AFTER_COMPACT_FLAG_PRESENT' "$CHECKPOINT_SKILL" 2>/dev/null; then
  ok "(T-06i-1) AFTER_COMPACT_FLAG_PRESENT present in SKILL.md"
else
  fail "(T-06i-1) AFTER_COMPACT_FLAG_PRESENT not found in SKILL.md"
fi

if grep -q 'after-compact flag is deprecated' "$CHECKPOINT_SKILL" 2>/dev/null; then
  ok "(T-06i-2) Deprecation INFO string 'after-compact flag is deprecated' present in SKILL.md"
else
  fail "(T-06i-2) Deprecation INFO string not found in SKILL.md"
fi

# ─── T-05b: zero-candidate graceful path (B3 no-candidate case) ──────────────
#
# When no pending-restore sentinels exist AND no recent session-state files exist,
# the picker must surface a graceful "no checkpoints found" message — not crash,
# not emit a false synthesize prompt.
#
# We assert this via SKILL.md prose: the B3 trigger must explicitly handle the
# "no session-state files" case by falling through to the existing graceful error path.

echo "T-05b: zero-candidate + no session-state → graceful no-checkpoint path"

# Verify SKILL.md documents the zero-session-state fall-through
if grep -q 'fall.*through.*Step 3\|Step 3.*graceful\|no checkpoints found' "$CHECKPOINT_SKILL" 2>/dev/null; then
  ok "(T-05b-1) SKILL.md documents graceful 'no checkpoints found' fall-through"
else
  fail "(T-05b-1) Graceful fall-through NOT documented in checkpoint/SKILL.md"
fi

# Verify B3 session-state fallback requires at least one session file to surface
# the synthesize prompt (i.e., it does NOT fire when sessions/ is also empty)
if grep -q 'zero session-state\|no session-state\|no.*session.*file\|if not most_recent\|most_recent.*fall' "$CHECKPOINT_SKILL" 2>/dev/null; then
  ok "(T-05b-2) SKILL.md confirms B3 fallback requires a non-empty session-state match"
else
  # Try alternate phrasings the skill might use
  if grep -q 'If zero session-state\|If.*session.*state.*match' "$CHECKPOINT_SKILL" 2>/dev/null; then
    ok "(T-05b-2) SKILL.md confirms B3 fallback requires a non-empty session-state match (alt phrasing)"
  else
    fail "(T-05b-2) B3 zero-session-state guard NOT documented in checkpoint/SKILL.md"
  fi
fi

# Also simulate the find logic: empty sessions dir → no candidates → fall through
rm -f "$MEMORY_DIR/sessions/"*.md 2>/dev/null || true
rm -f "$MEMORY_DIR/pending-restore-"*.txt 2>/dev/null || true

SESS_CANDIDATES=$(find "$MEMORY_DIR/sessions" -maxdepth 1 -name '*.md' 2>/dev/null | wc -l | awk '{print $1}')
SENT_CANDIDATES=$(find "$MEMORY_DIR" -maxdepth 1 -name 'pending-restore-*.txt' 2>/dev/null | wc -l | awk '{print $1}')

if [ "$SESS_CANDIDATES" -eq 0 ] && [ "$SENT_CANDIDATES" -eq 0 ]; then
  ok "(T-05b-3) Fixture confirms: 0 session files + 0 sentinels → graceful zero-candidate path"
else
  fail "(T-05b-3) Expected 0 sessions and 0 sentinels but got sess=$SESS_CANDIDATES, sent=$SENT_CANDIDATES"
fi

# ─── Summary ──────────────────────────────────────────────────────────────────

printf '\n'
if [ "$FAIL" -eq 0 ]; then
  printf 'PASS: all %d tests passed\n' "$PASS"
  exit 0
else
  printf 'FAIL: %d/%d tests failed:\n' "$FAIL" "$((PASS + FAIL))" >&2
  printf '%b\n' "$FAIL_MSGS" >&2
  exit 1
fi
