#!/bin/sh
# test_precompact_hook.sh — fixture tests for quoin/hooks/precompact.sh
#
# Covers sub-cases from T-12b / T-10 acceptance criteria.
# Requires: jq on PATH, sh (POSIX).
#
# Usage: sh quoin/dev/tests/test_precompact_hook.sh
# Exit 0 if all tests pass; non-zero otherwise.

set -eu

PASS=0
FAIL=0
FAIL_MSGS=""

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
FIXTURES_DIR="$SCRIPT_DIR/fixtures/hooks"
HOOK="$SCRIPT_DIR/../../hooks/precompact.sh"
DEPLOYED_HOOK="$HOME/.claude/hooks/precompact.sh"
RUN_STATE="$SCRIPT_DIR/../../core/scripts/run_state.py"

ok() { PASS=$((PASS + 1)); printf 'ok  %s\n' "$1"; }
fail() {
  FAIL=$((FAIL + 1))
  printf 'FAIL %s\n' "$1" >&2
  FAIL_MSGS="$FAIL_MSGS\n  - $1"
}

# Strip macOS's trailing TMPDIR slash: paths planted through run_state.py are
# normalized by pathlib, so a double slash here would defeat the hook's
# notes-path containment comparison exercised by the fixtures below.
_TMP_BASE="${TMPDIR:-/tmp}"; _TMP_BASE="${_TMP_BASE%/}"
TMPDIR_TEST="$_TMP_BASE/test_precompact_$$"
mkdir -p "$TMPDIR_TEST/.workflow_artifacts/memory/checkpoints"
mkdir -p "$TMPDIR_TEST/.workflow_artifacts/memory/sessions"
MEM_DIR_TEST="$TMPDIR_TEST/.workflow_artifacts/memory"

cleanup() { rm -rf "$TMPDIR_TEST"; }
trap cleanup EXIT

# ─── Shebang assertion ────────────────────────────────────────────────────────

if head -1 "$HOOK" | grep -qE '^#!/bin/sh( |$)'; then
  ok "shebang assertion: source hook starts with #!/bin/sh"
else
  fail "shebang assertion: source hook does not start with #!/bin/sh"
fi

if [ -f "$DEPLOYED_HOOK" ]; then
  if head -1 "$DEPLOYED_HOOK" | grep -qE '^#!/bin/sh( |$)'; then
    ok "shebang assertion: deployed hook starts with #!/bin/sh"
  else
    fail "shebang assertion: deployed hook does not start with #!/bin/sh"
  fi
fi

# ─── helper ──────────────────────────────────────────────────────────────────

make_stdin() {
  local trigger="$1"
  local session_id="${2:-test-session-precompact}"
  local cwd="${3:-$TMPDIR_TEST}"
  printf '{"trigger":"%s","session_id":"%s","cwd":"%s","transcript_path":"%s/dummy.jsonl"}' \
    "$trigger" "$session_id" "$cwd" "$cwd"
}

# ─── (a) auto trigger + pidfile present → allow, nothing else written ────────

# Create a fake pidfile
touch "$TMPDIR_TEST/.workflow_artifacts/memory/sessions/implement-12345.pidfile.lock"

stdin=$(make_stdin "auto" "sess-auto-pidfile")
out=$(printf '%s' "$stdin" | sh "$HOOK" 2>/dev/null)

if printf '%s' "$out" | grep -q '"decision"' 2>/dev/null && \
   printf '%s' "$out" | grep -q '"allow"' 2>/dev/null; then
  ok "(a) auto trigger + active pidfile → allow JSON emitted"
else
  fail "(a) auto trigger + active pidfile → expected allow JSON, got: $out"
fi

# Check sentinel NOT written (allow path must not write pending-restore)
# Count all pending-restore-*.txt files to avoid trivial pass on fresh TMPDIR
sentinel_count=$(ls "$TMPDIR_TEST/.workflow_artifacts/memory/pending-restore-"*.txt 2>/dev/null | wc -l | awk '{print $1}')
if [ "$sentinel_count" -eq 0 ]; then
  ok "(a) auto trigger + active pidfile → pending-restore sentinel NOT written (allow path)"
else
  fail "(a) auto trigger + active pidfile → pending-restore sentinel was written (should not be in allow path)"
fi

# No checkpoint file is written either: compaction never saves one any more
checkpoint_count=$(ls "$TMPDIR_TEST/.workflow_artifacts/memory/checkpoints/"*.md 2>/dev/null | wc -l | awk '{print $1}')
if [ "$checkpoint_count" -eq 0 ]; then
  ok "(a) auto trigger + pidfile present → no checkpoint file written"
else
  fail "(a) auto trigger + pidfile present → a checkpoint file was written"
fi

rm -f "$TMPDIR_TEST/.workflow_artifacts/memory/sessions/implement-12345.pidfile.lock"
rm -f "$TMPDIR_TEST/.workflow_artifacts/memory/pending-restore-sess-auto-pidfile.txt"
rm -f "$TMPDIR_TEST/.workflow_artifacts/memory/checkpoints/"*.md 2>/dev/null || true

# ─── (b) auto trigger, no pidfile, no run-state → allow, NOTHING written ─────
# No row writes a checkpoint or a sentinel; the allow line is the only output.

stdin=$(make_stdin "auto" "sess-auto-nopidfile")
stderr_out=$(printf '%s' "$stdin" | sh "$HOOK" 2>&1 >/dev/null) || true
out=$(printf '%s' "$stdin" | sh "$HOOK" 2>/dev/null)

if printf '%s' "$out" | grep -q '"allow"' 2>/dev/null; then
  ok "(b) auto trigger no pidfile no run → allow JSON emitted (non-blocking)"
else
  fail "(b) auto trigger no pidfile no run → expected allow, got: $out"
fi

if printf '%s' "$stderr_out" | grep -q 'no active run-state record' 2>/dev/null; then
  ok "(b) no-run row → stderr INFO names the no-run row"
else
  fail "(b) no-run row → stderr missing the no-run INFO wording; got: $stderr_out"
fi

pending_restore_file="$TMPDIR_TEST/.workflow_artifacts/memory/pending-restore-sess-auto-nopidfile.txt"
if [ ! -f "$pending_restore_file" ]; then
  ok "(b) no-run row → pending-restore sentinel NOT written"
else
  fail "(b) no-run row → pending-restore sentinel written"
fi

checkpoint_count_b=$(ls "$TMPDIR_TEST/.workflow_artifacts/memory/checkpoints/"*.md 2>/dev/null | wc -l | awk '{print $1}')
if [ "$checkpoint_count_b" -eq 0 ]; then
  ok "(b) no-run row → no checkpoint written"
else
  fail "(b) no-run row → checkpoint written"
fi

rm -f "$pending_restore_file"
rm -f "$TMPDIR_TEST/.workflow_artifacts/memory/checkpoints/"*.md 2>/dev/null || true

# ─── (c) manual trigger → exit 0 immediately, no state save ──────────────────

before_checkpoint_count=$(ls "$TMPDIR_TEST/.workflow_artifacts/memory/checkpoints/"*.md 2>/dev/null | wc -l | awk '{print $1}')
stdin=$(make_stdin "manual" "sess-manual")
out=$(printf '%s' "$stdin" | sh "$HOOK" 2>/dev/null)
after_checkpoint_count=$(ls "$TMPDIR_TEST/.workflow_artifacts/memory/checkpoints/"*.md 2>/dev/null | wc -l | awk '{print $1}')

if [ -z "$out" ]; then
  ok "(c) manual trigger → exit 0, no output"
else
  fail "(c) manual trigger → expected no output, got: $out"
fi

if [ "$before_checkpoint_count" -eq "$after_checkpoint_count" ]; then
  ok "(c) manual trigger → no checkpoint written"
else
  fail "(c) manual trigger → checkpoint was written (should not be)"
fi

# ─── (d) CLAUDE_ALLOW_COMPACT env var removed (T-03) — hook always allows ─────
# The CLAUDE_ALLOW_COMPACT override was a workaround for the block path.
# Since the hook is now always non-blocking, this workaround is removed.
# Verify the env var no longer appears in precompact.sh.

if ! grep -q 'CLAUDE_ALLOW_COMPACT' "$HOOK" 2>/dev/null; then
  ok "(d) CLAUDE_ALLOW_COMPACT env var removed from precompact.sh (no longer needed)"
else
  fail "(d) CLAUDE_ALLOW_COMPACT still present in precompact.sh (should have been removed)"
fi

# ─── (e) save failure → exit 0 (fail-OPEN) ───────────────────────────────────

# A read-only checkpoints directory must not matter: the hook writes nothing
# there. The assertion is the exit status.
chmod 555 "$TMPDIR_TEST/.workflow_artifacts/memory/checkpoints" 2>/dev/null || true
stdin=$(make_stdin "auto" "sess-save-fail")
rc_e=0
printf '%s' "$stdin" | sh "$HOOK" 2>/dev/null > /dev/null || rc_e=$?
chmod 755 "$TMPDIR_TEST/.workflow_artifacts/memory/checkpoints" 2>/dev/null || true

if [ "$rc_e" -eq 0 ]; then
  ok "(e) read-only checkpoints dir → hook exits 0 (fail-OPEN)"
else
  fail "(e) read-only checkpoints dir → hook exited $rc_e"
fi
rm -f "$TMPDIR_TEST/.workflow_artifacts/memory/pending-restore-sess-save-fail.txt" 2>/dev/null || true

# ─── (f) session_id absent → fail-OPEN ───────────────────────────────────────

stdin_raw='{"trigger":"auto","cwd":"'"$TMPDIR_TEST"'","transcript_path":"'"$TMPDIR_TEST"'/dummy.jsonl"}'
out=$(printf '%s' "$stdin_raw" | sh "$HOOK" 2>/dev/null)
# Should fail-OPEN (exit 0, no block)
if [ -z "$out" ] || ! printf '%s' "$out" | grep -q '"block"' 2>/dev/null; then
  ok "(f) session_id absent → fail-OPEN (no block JSON)"
else
  fail "(f) session_id absent → block JSON emitted despite missing session_id: $out"
fi

# ─── (g) active run-state record + pidfile present → no checkpoint, no sentinel ──

touch "$TMPDIR_TEST/.workflow_artifacts/memory/sessions/plan-67890.pidfile.lock"
python3 "$RUN_STATE" --write --project-root "$TMPDIR_TEST" --task gcase --session-id sess-allow-content --at-stage-boundary true >/dev/null 2>&1
stdin=$(make_stdin "auto" "sess-allow-content")
out_g=$(printf '%s' "$stdin" | sh "$HOOK" 2>/dev/null)
cp_g=$(find "$TMPDIR_TEST/.workflow_artifacts/memory/checkpoints" -type f 2>/dev/null | wc -l | awk '{print $1}')
pr_g=$(find "$TMPDIR_TEST/.workflow_artifacts/memory" -maxdepth 1 -name 'pending-restore-*' 2>/dev/null | wc -l | awk '{print $1}')
if [ "$out_g" = '{"decision": "allow"}' ] && [ "$cp_g" -eq 0 ] && [ "$pr_g" -eq 0 ]; then
  ok "(g) active run + pidfile → allow only; no file under checkpoints/ and no pending-restore"
else
  fail "(g) active run + pidfile wrote state (out=$out_g checkpoints=$cp_g sentinels=$pr_g)"
fi
rm -f "$TMPDIR_TEST/.workflow_artifacts/memory/sessions/plan-67890.pidfile.lock" "$MEM_DIR_TEST/run-state-gcase.json" "$MEM_DIR_TEST/run-notes-gcase.md"

# ─── (h) a pre-existing sentinel is left untouched ────────────────────────────

pending_restore_h="$TMPDIR_TEST/.workflow_artifacts/memory/pending-restore-sess-prior-checkpoint.txt"
printf '/some/prior/checkpoint.md\n' > "$pending_restore_h"
mtime_before=$(stat -f %m "$pending_restore_h" 2>/dev/null || stat -c %Y "$pending_restore_h" 2>/dev/null || echo "0")
stdin=$(make_stdin "auto" "sess-prior-checkpoint")
out=$(printf '%s' "$stdin" | sh "$HOOK" 2>/dev/null)
mtime_after=$(stat -f %m "$pending_restore_h" 2>/dev/null || stat -c %Y "$pending_restore_h" 2>/dev/null || echo "0")
if printf '%s' "$out" | grep -q '"allow"' 2>/dev/null && [ -f "$pending_restore_h" ] && [ "$mtime_before" = "$mtime_after" ]; then
  ok "(h) sentinel pre-exists → allow, existing sentinel untouched"
else
  fail "(h) sentinel pre-exists → allow expected and sentinel unchanged (out=$out)"
fi
rm -f "$pending_restore_h"

# ─── (j) pidfile present (skill may or may not be alive) → allow ─────────────
# The hook never reads or checks PID liveness — it only lists pidfile names.
# PID 99999 is a descriptive dummy; whether that process is alive is irrelevant
# to the test outcome. This test documents the known limitation explicitly.

touch "$TMPDIR_TEST/.workflow_artifacts/memory/sessions/implement-99999.pidfile.lock"

stdin=$(make_stdin "auto" "sess-stale-pidfile")
out=$(printf '%s' "$stdin" | sh "$HOOK" 2>/dev/null)

if printf '%s' "$out" | grep -q '"allow"' 2>/dev/null; then
  ok "(j) pidfile present (skill may or may not be alive) → allow (liveness not checked)"
else
  fail "(j) pidfile present → expected allow regardless of PID liveness, got: $out"
fi

rm -f "$TMPDIR_TEST/.workflow_artifacts/memory/sessions/implement-99999.pidfile.lock"
rm -f "$TMPDIR_TEST/.workflow_artifacts/memory/checkpoints/"*.md 2>/dev/null || true

# ─── Paths with spaces ────────────────────────────────────────────────────────
# Verify the hook behaves correctly when the cwd contains spaces.
# We create a temp dir with spaces in the name and run the hook.

SPACE_DIR="${TMPDIR:-/tmp}/test precompact space $$"
mkdir -p "$SPACE_DIR/.workflow_artifacts/memory/checkpoints"
mkdir -p "$SPACE_DIR/.workflow_artifacts/memory/sessions"

stdin_e=$(printf '{"trigger":"auto","session_id":"sess-space","cwd":"%s","transcript_path":"%s/dummy.jsonl"}' \
  "$SPACE_DIR" "$SPACE_DIR")
out_e=$(printf '%s' "$stdin_e" | sh "$HOOK" 2>/dev/null) || true

# The hook should produce either block JSON or (if no cwd write is needed for other reasons) exit 0
# Key: it must not crash
if [ -z "$out_e" ] || printf '%s' "$out_e" | grep -q 'decision\|block\|allow' 2>/dev/null; then
  ok "(spaces) hook handles paths with spaces (no crash)"
else
  fail "(spaces) hook may have crashed on path-with-spaces: $out_e"
fi

rm -rf "$SPACE_DIR" 2>/dev/null || true
rm -f "$TMPDIR_TEST/.workflow_artifacts/memory/pending-restore-sess-space.txt"
rm -f "$TMPDIR_TEST/.workflow_artifacts/memory/checkpoints/"*.md 2>/dev/null || true

# ─── IVG-61 Nested-cwd regression (resolve_project_root wiring) ──────────────
# Feed a genuinely nested non-owning cwd.
# Without the fix: hook writes its notes to <root>/sub/.workflow_artifacts/.
# With the fix:    hook writes them to <root>/.workflow_artifacts/.
#
# Also hardens against a fake HOME that itself owns .workflow_artifacts/: the
# session record must still land at <root>, NOT fake HOME.
#
# LOAD-BEARING: "no subdir leaked" assertion — the recent-sessions append does
# mkdir -p on a wrong cwd, so absence of the nested dir proves resolve fired.

NESTED_HOME="${TMPDIR:-/tmp}/test_precompact_nested_home_$$"
NESTED_ROOT="$NESTED_HOME/project"
NESTED_SUB="$NESTED_ROOT/sub"
# Give fake HOME its own .workflow_artifacts/ (CRIT-3 hardening)
mkdir -p "$NESTED_HOME/.workflow_artifacts/memory"
mkdir -p "$NESTED_ROOT/.workflow_artifacts/memory/checkpoints"
mkdir -p "$NESTED_ROOT/.workflow_artifacts/memory/sessions"
mkdir -p "$NESTED_SUB"
touch "$NESTED_SUB/dummy.jsonl"

stdin_nested=$(printf '{"trigger":"auto","session_id":"sess-nested-ivg61","cwd":"%s","transcript_path":"%s/dummy.jsonl"}' \
  "$NESTED_SUB" "$NESTED_SUB")

# Run hook with fake HOME so resolve_project_root ceiling is deterministic
out_nested=$(printf '%s' "$stdin_nested" | HOME="$NESTED_HOME" sh "$HOOK" 2>/dev/null) || true

# Hook should emit {"decision":"allow"} (no pidfiles → direct-conversation path)
if printf '%s' "$out_nested" | grep -q '"allow"'; then
  ok "(IVG-61-nested) hook emits allow decision"
else
  fail "(IVG-61-nested) hook did not emit allow decision: $out_nested"
fi

# LOAD-BEARING: session record written at project root, NOT at nested sub
if grep -q 'sess-nested-ivg61' "$NESTED_ROOT/.workflow_artifacts/memory/recent-sessions.md" 2>/dev/null; then
  ok "(IVG-61-nested) session record created at project root"
else
  fail "(IVG-61-nested) no session record at project root — resolve_project_root may not be wired"
fi

# LOAD-BEARING: nested sub must NOT have a .workflow_artifacts/ leaked into it
if [ -d "$NESTED_SUB/.workflow_artifacts" ]; then
  fail "(IVG-61-nested) .workflow_artifacts/ leaked into nested sub — resolve did not fire"
else
  ok "(IVG-61-nested) no .workflow_artifacts/ leaked into nested sub"
fi

# CRIT-3: nothing leaked into fake HOME's .workflow_artifacts/
if [ -f "$NESTED_HOME/.workflow_artifacts/memory/recent-sessions.md" ]; then
  fail "(IVG-61-CRIT3) session record leaked into fake HOME — exclusive ceiling broken"
else
  ok "(IVG-61-CRIT3) no session record leaked into fake HOME"
fi

rm -rf "$NESTED_HOME" 2>/dev/null || true


# ─── T-01: run_state_select / run_state_fields (direct, via _lib.sh) ─────────
# Per-fixture record cleanup below is deliberate isolation between fixtures,
# not defensive boilerplate: today a leftover record is harmless only because
# of the exact-session_id match rule, and that is an accidental property of
# an unrelated rule, not designed isolation.

. "$SCRIPT_DIR/../../hooks/_lib.sh" && read_constants

# active:false — the writer has no --active flag; --clear flips active and
# re-serializes through the same writer, preserving the byte shape the
# eligibility grep expects. Boundary writes → no notes files at setup.
python3 "$RUN_STATE" --write --project-root "$TMPDIR_TEST" --task sel-af --session-id sid-sel-af --at-stage-boundary true >/dev/null 2>&1
python3 "$RUN_STATE" --clear --project-root "$TMPDIR_TEST" --task sel-af >/dev/null 2>&1
if grep -q '"active": false' "$MEM_DIR_TEST/run-state-sel-af.json" 2>/dev/null; then
  ok "(T-01-af-plant) active:false precondition planted"
else
  fail "(T-01-af-plant) planting recipe failed to produce active:false"
fi
if [ -z "$(run_state_select "$MEM_DIR_TEST" sid-sel-af)" ]; then
  ok "(T-01-af) active:false record → empty selector output"
else
  fail "(T-01-af) active:false record was selected"
fi

# schema:2 — the writer hard-assigns schema 1 on every write and can never
# emit 2, so a targeted sed on this one test-owned line is the documented
# carve-out from the no-hand-rolled-JSON rule. sed -i.bak is the portable
# (BSD + GNU) in-place form; the transient .bak cannot match run-state-*.json.
python3 "$RUN_STATE" --write --project-root "$TMPDIR_TEST" --task sel-s2 --session-id sid-sel-s2 --at-stage-boundary true >/dev/null 2>&1
sed -i.bak 's/"schema": 1/"schema": 2/' "$MEM_DIR_TEST/run-state-sel-s2.json" && rm -f "$MEM_DIR_TEST/run-state-sel-s2.json.bak"
if grep -q '"schema": 2' "$MEM_DIR_TEST/run-state-sel-s2.json" 2>/dev/null; then
  ok "(T-01-s2-plant) schema:2 precondition planted"
else
  fail "(T-01-s2-plant) planting recipe failed to produce schema:2"
fi
if [ -z "$(run_state_select "$MEM_DIR_TEST" sid-sel-s2)" ]; then
  ok "(T-01-s2) schema:2 record → empty selector output"
else
  fail "(T-01-s2) schema:2 record was selected"
fi

# stale mtime — real write, then age the file past the freshness window
python3 "$RUN_STATE" --write --project-root "$TMPDIR_TEST" --task sel-st --session-id sid-sel-st --at-stage-boundary true >/dev/null 2>&1
touch -t 202601010000 "$MEM_DIR_TEST/run-state-sel-st.json"
if [ -n "$(find "$MEM_DIR_TEST/run-state-sel-st.json" -mtime +2 2>/dev/null)" ]; then
  ok "(T-01-st-plant) stale-mtime precondition planted"
else
  fail "(T-01-st-plant) planting recipe failed to age the record"
fi
if [ -z "$(run_state_select "$MEM_DIR_TEST" sid-sel-st)" ]; then
  ok "(T-01-st) stale record → empty selector output"
else
  fail "(T-01-st) stale record was selected"
fi

# fresh, active, schema 1 — but a different session's id
python3 "$RUN_STATE" --write --project-root "$TMPDIR_TEST" --task sel-nm --session-id sid-sel-other --at-stage-boundary true >/dev/null 2>&1
if [ -z "$(run_state_select "$MEM_DIR_TEST" sid-sel-mine)" ]; then
  ok "(T-01-nm) non-matching session_id → empty selector output (no fallback)"
else
  fail "(T-01-nm) non-matching session_id record was selected"
fi

# nonexistent memory dir → empty output, no error
if [ -z "$(run_state_select "$MEM_DIR_TEST/nonexistent-dir" sid-x 2>&1)" ]; then
  ok "(T-01-dir) nonexistent memory dir → empty output, no error"
else
  fail "(T-01-dir) nonexistent memory dir produced output"
fi

# hook-level: with only ineligible records present, the hook takes the
# no-run row — no checkpoint, no sentinel
rm -f "$TMPDIR_TEST/.workflow_artifacts/memory/checkpoints/"*.md 2>/dev/null || true
stdin=$(make_stdin "auto" "sess-inelig-hook")
out_inelig=$(printf '%s' "$stdin" | sh "$HOOK" 2>/dev/null)
cp_inelig=$(ls "$TMPDIR_TEST/.workflow_artifacts/memory/checkpoints/"*.md 2>/dev/null | wc -l | awk '{print $1}')
if [ "$out_inelig" = '{"decision": "allow"}' ] && [ "$cp_inelig" -eq 0 ] && [ ! -f "$MEM_DIR_TEST/pending-restore-sess-inelig-hook.txt" ]; then
  ok "(T-01-hook) ineligible records fall through to the no-run row"
else
  fail "(T-01-hook) ineligible records did not fall through cleanly (out=$out_inelig cp=$cp_inelig)"
fi
rm -f "$MEM_DIR_TEST/run-state-sel-af.json" "$MEM_DIR_TEST/run-state-sel-s2.json" "$MEM_DIR_TEST/run-state-sel-st.json" "$MEM_DIR_TEST/run-state-sel-nm.json"

# matching older record wins over a fresher non-matching one; the fresher
# record is planted at a stage boundary so its setup writes NO notes file —
# that keeps the "no run-notes for the other task" assertion true by
# construction rather than falsified during setup.
python3 "$RUN_STATE" --write --project-root "$TMPDIR_TEST" --task sel-match --session-id sid-sel-match --phase implement --subphase code --step "match step" --next-action "match next" >/dev/null 2>&1
sleep 1
python3 "$RUN_STATE" --write --project-root "$TMPDIR_TEST" --task sel-fresher --session-id sid-sel-fresh2 --at-stage-boundary true >/dev/null 2>&1
sel_match=$(run_state_select "$MEM_DIR_TEST" sid-sel-match)
if [ "$sel_match" = "$MEM_DIR_TEST/run-state-sel-match.json" ]; then
  ok "(T-01-match) matching older record wins over fresher non-matching record"
else
  fail "(T-01-match) selector returned '$sel_match' (expected the matching record)"
fi

# run_state_fields: a 2-key call returns exactly 2 key=value lines, no
# spurious <file>= line from the shifted-off first positional
kv=$(run_state_fields "$MEM_DIR_TEST/run-state-sel-fresher.json" schema session_id)
kv_lines=$(printf '%s\n' "$kv" | wc -l | awk '{print $1}')
if [ "$kv_lines" = "2" ] && printf '%s\n' "$kv" | grep -q '^schema=1$' && printf '%s\n' "$kv" | grep -q '^session_id=sid-sel-fresh2$'; then
  ok "(T-01-fields) 2-key extractor call returns exactly 2 key=value lines"
else
  fail "(T-01-fields) unexpected extractor output: $kv"
fi

# companion: only the fresher non-matching record present → empty output,
# hook takes row 3, and no run-notes file for the other task appears
rm -f "$MEM_DIR_TEST/run-state-sel-match.json" "$MEM_DIR_TEST/run-notes-sel-match.md"
if [ -z "$(run_state_select "$MEM_DIR_TEST" sid-sel-match)" ]; then
  ok "(T-01-nofall) no matching record → empty output (no freshest-active fallback)"
else
  fail "(T-01-nofall) selector fell back to a non-matching record"
fi
rm -f "$TMPDIR_TEST/.workflow_artifacts/memory/checkpoints/"*.md 2>/dev/null || true
stdin=$(make_stdin "auto" "sid-sel-match")
out_nofall=$(printf '%s' "$stdin" | sh "$HOOK" 2>/dev/null)
cp_nofall=$(ls "$TMPDIR_TEST/.workflow_artifacts/memory/checkpoints/"*.md 2>/dev/null | wc -l | awk '{print $1}')
if [ "$out_nofall" = '{"decision": "allow"}' ] && [ "$cp_nofall" -eq 0 ] && [ ! -f "$MEM_DIR_TEST/run-notes-sel-fresher.md" ]; then
  ok "(T-01-nofall2) unmatched session takes the no-run row; other task's notes untouched"
else
  fail "(T-01-nofall2) unmatched session misbehaved (out=$out_nofall cp=$cp_nofall)"
fi
rm -f "$MEM_DIR_TEST/run-state-sel-fresher.json"

# scale: a full no-match scan over a 50-record directory stays far inside
# the hook's 10 s budget — a generous-ceiling regression guard, not a
# benchmark (records planted at a stage boundary → no notes files)
_i=1
while [ "$_i" -le 50 ]; do
  python3 "$RUN_STATE" --write --project-root "$TMPDIR_TEST" --task "sel-scale-$_i" --session-id "sid-scale-$_i" --at-stage-boundary true >/dev/null 2>&1
  _i=$((_i + 1))
done
SCALE_START=$(python3 -c 'import time; print(int(time.time()*1000))' 2>/dev/null || printf '0')
sel_scale=$(run_state_select "$MEM_DIR_TEST" sid-scale-nomatch)
SCALE_END=$(python3 -c 'import time; print(int(time.time()*1000))' 2>/dev/null || printf '0')
SCALE_ELAPSED_MS=$((SCALE_END - SCALE_START))
if [ -z "$sel_scale" ] && [ "$SCALE_ELAPSED_MS" -le 2000 ]; then
  ok "(T-01-scale) 50-record full scan returned empty in ${SCALE_ELAPSED_MS}ms (ceiling 2000ms)"
else
  fail "(T-01-scale) 50-record scan misbehaved (sel='$sel_scale' elapsed=${SCALE_ELAPSED_MS}ms)"
fi
rm -f "$MEM_DIR_TEST"/run-state-sel-scale-*.json

# read bound: a key planted past the 64 KiB mark must never come back —
# guards the bounded read in run_state_fields (reverting it to an
# unbounded read fails this fixture)
python3 -c '
import sys
pad = "x" * 1024
lines = ["{", "  \"schema\": 1,", "  \"session_id\": \"sid-bound-test\","]
for i in range(70):
    lines.append("  \"pad%d\": \"%s\"," % (i, pad))
lines.append("  \"next_action\": \"HOSTILE-PAST-BOUND\",")
lines.append("  \"active\": true")
lines.append("}")
open(sys.argv[1], "w").write("\n".join(lines) + "\n")
' "$MEM_DIR_TEST/run-state-bound.json"
kv_bound=$(run_state_fields "$MEM_DIR_TEST/run-state-bound.json" next_action)
if [ "$kv_bound" = "next_action=" ]; then
  ok "(T-01-bound) key past the 64 KiB mark is not returned (read bound holds)"
else
  fail "(T-01-bound) extractor returned data past the read bound: $kv_bound"
fi
rm -f "$MEM_DIR_TEST/run-state-bound.json"

# probe bound: a candidate whose "active": true sits past the 64 KiB mark
# must never be selected — the liveness probe reads a bounded head only.
# Reverting the probe to an unbounded scan makes this candidate selectable
# (schema/session_id sit at the top and would then match).
python3 -c '
import sys
pad = "x" * 1024
lines = ["{", "  \"schema\": 1,", "  \"session_id\": \"sid-probe-bound\","]
for i in range(1024):
    lines.append("  \"pad%d\": \"%s\"," % (i, pad))
lines.append("  \"active\": true")
lines.append("}")
open(sys.argv[1], "w").write("\n".join(lines) + "\n")
' "$MEM_DIR_TEST/run-state-probe-bound.json"
PBOUND_START=$(python3 -c 'import time; print(int(time.time()*1000))' 2>/dev/null || printf '0')
sel_pbound=$(run_state_select "$MEM_DIR_TEST" sid-probe-bound)
PBOUND_END=$(python3 -c 'import time; print(int(time.time()*1000))' 2>/dev/null || printf '0')
PBOUND_ELAPSED_MS=$((PBOUND_END - PBOUND_START))
if [ -z "$sel_pbound" ] && [ "$PBOUND_ELAPSED_MS" -le 2000 ]; then
  ok "(T-01-probe-bound) oversized candidate not selected in ${PBOUND_ELAPSED_MS}ms (probe bound holds)"
else
  fail "(T-01-probe-bound) probe bound broken (sel='$sel_pbound' elapsed=${PBOUND_ELAPSED_MS}ms)"
fi
rm -f "$MEM_DIR_TEST/run-state-probe-bound.json"

# 64 KiB by-construction invariant: a maximal writer-produced record —
# every string field at its cap, artifacts at their byte bound — must keep
# the late fields (next_action, notes_path) inside the extractor's read
# bound; a field-order or cap change eroding that headroom fails here.
_inv_pad=$(python3 -c 'print("v" * 5000)')
_inv_art=$(python3 -c 'print("a" * 4000)')
python3 "$RUN_STATE" --write --project-root "$TMPDIR_TEST" --task inv-max --session-id sid-inv-max \
  --phase "$_inv_pad" --subphase "$_inv_pad" --step "$_inv_pad" \
  --next-action "END-$_inv_pad" --route "$_inv_pad" --profile "$_inv_pad" \
  --resume-command "$_inv_pad" \
  --artifact "$_inv_art" --artifact "$_inv_art" --artifact "$_inv_art" \
  --artifact "$_inv_art" --artifact "$_inv_art" \
  --at-stage-boundary true >/dev/null 2>&1
kv_inv=$(run_state_fields "$MEM_DIR_TEST/run-state-inv-max.json" next_action notes_path)
_inv_na=$(printf '%s\n' "$kv_inv" | sed -n 's/^next_action=//p')
_inv_np=$(printf '%s\n' "$kv_inv" | sed -n 's/^notes_path=//p')
case "$_inv_na" in
  END-*) _inv_na_ok=1 ;;
  *) _inv_na_ok=0 ;;
esac
if [ "$_inv_na_ok" -eq 1 ] && [ "$_inv_np" = "$MEM_DIR_TEST/run-notes-inv-max.md" ]; then
  ok "(T-01-invariant) maximal record round-trips next_action/notes_path inside the read bound"
else
  fail "(T-01-invariant) maximal record broke the 64 KiB invariant (np='$_inv_np')"
fi
rm -f "$MEM_DIR_TEST/run-state-inv-max.json"

# ─── T-05: three-row fixtures + recent-sessions per-row append ───────────────

rm -f "$TMPDIR_TEST/.workflow_artifacts/memory/checkpoints/"*.md 2>/dev/null || true

# (r1) fresh active record matching the session → no checkpoint, no sentinel
python3 "$RUN_STATE" --write --project-root "$TMPDIR_TEST" --task row1 --session-id sess-row1-run --phase implement --subphase code --step "row1 step" --next-action "row1 next" >/dev/null 2>&1
stdin=$(make_stdin "auto" "sess-row1-run")
out_r1=$(printf '%s' "$stdin" | sh "$HOOK" 2>/dev/null)
cp_r1=$(ls "$TMPDIR_TEST/.workflow_artifacts/memory/checkpoints/"*.md 2>/dev/null | wc -l | awk '{print $1}')
rs_r1=$(grep -c 'sess-row1-run' "$MEM_DIR_TEST/recent-sessions.md" 2>/dev/null) || rs_r1=0
if [ "$out_r1" = '{"decision": "allow"}' ] && [ "$cp_r1" -eq 0 ] && [ ! -f "$MEM_DIR_TEST/pending-restore-sess-row1-run.txt" ]; then
  ok "(r1) run row → allow JSON only, no checkpoint, no sentinel"
else
  fail "(r1) run row misbehaved (out=$out_r1 cp=$cp_r1)"
fi
if [ "$rs_r1" -eq 1 ]; then
  ok "(r1) run row → exactly one recent-sessions line for this session"
else
  fail "(r1) run row → recent-sessions lines for this session: $rs_r1 (expected 1)"
fi
rm -f "$MEM_DIR_TEST/run-state-row1.json" "$MEM_DIR_TEST/run-notes-row1.md"
rm -f "$TMPDIR_TEST/.workflow_artifacts/memory/checkpoints/"*.md 2>/dev/null || true

# (r2) no record, pidfile present → no checkpoint, no sentinel
touch "$TMPDIR_TEST/.workflow_artifacts/memory/sessions/review-13579.pidfile.lock"
stdin=$(make_stdin "auto" "sess-row2-pidfile")
out_r2=$(printf '%s' "$stdin" | sh "$HOOK" 2>/dev/null)
cp_r2=$(ls "$TMPDIR_TEST/.workflow_artifacts/memory/checkpoints/"*.md 2>/dev/null | wc -l | awk '{print $1}')
rs_r2=$(grep -c 'sess-row2-pidfile' "$MEM_DIR_TEST/recent-sessions.md" 2>/dev/null) || rs_r2=0
if [ "$out_r2" = '{"decision": "allow"}' ] && [ "$cp_r2" -eq 0 ] && [ ! -f "$MEM_DIR_TEST/pending-restore-sess-row2-pidfile.txt" ]; then
  ok "(r2) pidfile row → allow JSON only, no checkpoint, no sentinel"
else
  fail "(r2) pidfile row misbehaved (out=$out_r2 cp=$cp_r2)"
fi
if [ "$rs_r2" -eq 1 ]; then
  ok "(r2) pidfile row → exactly one recent-sessions line for this session"
else
  fail "(r2) pidfile row → recent-sessions lines for this session: $rs_r2 (expected 1)"
fi
rm -f "$TMPDIR_TEST/.workflow_artifacts/memory/sessions/review-13579.pidfile.lock"
rm -f "$TMPDIR_TEST/.workflow_artifacts/memory/checkpoints/"*.md 2>/dev/null || true

# (r3) no record, no pidfile — single invocation under a fresh session id so
# the one-line recent-sessions assertion cannot be diluted by a reused id
stdin=$(make_stdin "auto" "sess-row3-norun")
out_r3=$(printf '%s' "$stdin" | sh "$HOOK" 2>/dev/null)
cp_r3=$(ls "$TMPDIR_TEST/.workflow_artifacts/memory/checkpoints/"*.md 2>/dev/null | wc -l | awk '{print $1}')
rs_r3=$(grep -c 'sess-row3-norun' "$MEM_DIR_TEST/recent-sessions.md" 2>/dev/null) || rs_r3=0
if [ "$out_r3" = '{"decision": "allow"}' ] && [ "$cp_r3" -eq 0 ] && [ ! -f "$MEM_DIR_TEST/pending-restore-sess-row3-norun.txt" ]; then
  ok "(r3) no-run row → allow JSON only, nothing written"
else
  fail "(r3) no-run row misbehaved (out=$out_r3 cp=$cp_r3)"
fi
if [ "$rs_r3" -eq 1 ]; then
  ok "(r3) no-run row → exactly one recent-sessions line for this session"
else
  fail "(r3) no-run row → recent-sessions lines for this session: $rs_r3 (expected 1)"
fi

# hostile record values bind literally — never eval, nothing executes
python3 "$RUN_STATE" --write --project-root "$TMPDIR_TEST" --task hostile-step --session-id sess-hostile-step --phase impl --subphase x --step 'editing foo; $(touch '"$TMPDIR_TEST"'/pwned) `touch '"$TMPDIR_TEST"'/pwned2`' --at-stage-boundary true >/dev/null 2>&1
stdin=$(make_stdin "auto" "sess-hostile-step")
out_hs=$(printf '%s' "$stdin" | sh "$HOOK" 2>/dev/null)
step_hs=$(grep -F '"session_id":"sess-hostile-step"' "$MEM_DIR_TEST/telemetry/compaction-events.jsonl" 2>/dev/null | tail -1 | jq -r .step) || step_hs="(extract failed)"
expected_hs='editing foo; $(touch '"$TMPDIR_TEST"'/pwned) `touch '"$TMPDIR_TEST"'/pwned2`'
if [ ! -f "$TMPDIR_TEST/pwned" ] && [ ! -f "$TMPDIR_TEST/pwned2" ] && [ "$out_hs" = '{"decision": "allow"}' ]; then
  ok "(T-02-bind) hostile step value executed nothing"
else
  fail "(T-02-bind) hostile step value had side effects (out=$out_hs)"
fi
if [ "$step_hs" = "$expected_hs" ]; then
  ok "(T-02-bind2) hostile step value bound as a literal string"
else
  fail "(T-02-bind2) bound step diverged: $step_hs"
fi
rm -f "$MEM_DIR_TEST/run-state-hostile-step.json"
rm -f "$TMPDIR_TEST/.workflow_artifacts/memory/checkpoints/"*.md 2>/dev/null || true

# ─── T-05: A3 early-skip path still fires run-notes + telemetry ──────────────
# Record mechanism (standard --write, whose own setup appends one notes
# block — the assertions are deltas over that setup state, never absolutes).
python3 "$RUN_STATE" --write --project-root "$TMPDIR_TEST" --task a3run --session-id sess-a3-run --phase implement --subphase code --step "a3 step" --next-action "a3 next" >/dev/null 2>&1
printf '/some/prior/checkpoint.md\n' > "$MEM_DIR_TEST/pending-restore-sess-a3-run.txt"
blocks_before_a3=$(grep -c '^## ' "$MEM_DIR_TEST/run-notes-a3run.md" 2>/dev/null) || blocks_before_a3=0
tel_before_a3=$(grep -F '"session_id":"sess-a3-run"' "$MEM_DIR_TEST/telemetry/compaction-events.jsonl" 2>/dev/null | grep -cF '"half":"pre"') || tel_before_a3=0
stdin=$(make_stdin "auto" "sess-a3-run")
out_a3=$(printf '%s' "$stdin" | sh "$HOOK" 2>/dev/null)
blocks_after_a3=$(grep -c '^## ' "$MEM_DIR_TEST/run-notes-a3run.md" 2>/dev/null) || blocks_after_a3=0
tel_after_a3=$(grep -F '"session_id":"sess-a3-run"' "$MEM_DIR_TEST/telemetry/compaction-events.jsonl" 2>/dev/null | grep -cF '"half":"pre"') || tel_after_a3=0
cp_a3=$(ls "$TMPDIR_TEST/.workflow_artifacts/memory/checkpoints/"*.md 2>/dev/null | wc -l | awk '{print $1}')
if [ "$blocks_after_a3" = "$((blocks_before_a3 + 1))" ]; then
  ok "(A3-path) early-skip + active run → hook contributed exactly one notes block"
else
  fail "(A3-path) notes delta wrong: $blocks_before_a3 -> $blocks_after_a3"
fi
if [ "$tel_after_a3" = "$((tel_before_a3 + 1))" ]; then
  ok "(A3-path) early-skip + active run → one telemetry pre line appended"
else
  fail "(A3-path) telemetry delta wrong: $tel_before_a3 -> $tel_after_a3"
fi
if [ "$out_a3" = '{"decision": "allow"}' ] && [ "$cp_a3" -eq 0 ] && [ -f "$MEM_DIR_TEST/pending-restore-sess-a3-run.txt" ]; then
  ok "(A3-path) allow emitted, no new checkpoint, existing sentinel untouched"
else
  fail "(A3-path) misbehaved (out=$out_a3 cp=$cp_a3)"
fi
rm -f "$MEM_DIR_TEST/pending-restore-sess-a3-run.txt" "$MEM_DIR_TEST/run-state-a3run.json" "$MEM_DIR_TEST/run-notes-a3run.md"

# ─── T-03: run-notes append fixtures ─────────────────────────────────────────

# mid-stage: exactly one block from the hook; record byte-identical after
python3 "$RUN_STATE" --write --project-root "$TMPDIR_TEST" --task notes-mid --session-id sess-notes-mid --phase implement --subphase code --step "mid step" --next-action "mid next" >/dev/null 2>&1
blocks_before_nm=$(grep -c '^## ' "$MEM_DIR_TEST/run-notes-notes-mid.md" 2>/dev/null) || blocks_before_nm=0
cp "$MEM_DIR_TEST/run-state-notes-mid.json" "$TMPDIR_TEST/record.snap"
stdin=$(make_stdin "auto" "sess-notes-mid")
out_nm=$(printf '%s' "$stdin" | sh "$HOOK" 2>/dev/null)
blocks_after_nm=$(grep -c '^## ' "$MEM_DIR_TEST/run-notes-notes-mid.md" 2>/dev/null) || blocks_after_nm=0
if [ "$blocks_after_nm" = "$((blocks_before_nm + 1))" ] && [ "$out_nm" = '{"decision": "allow"}' ]; then
  ok "(T-03-mid) mid-stage row → hook appended exactly one notes block"
else
  fail "(T-03-mid) notes delta wrong: $blocks_before_nm -> $blocks_after_nm (out=$out_nm)"
fi
if cmp -s "$MEM_DIR_TEST/run-state-notes-mid.json" "$TMPDIR_TEST/record.snap"; then
  ok "(T-03-mid2) JSON record byte-identical after the hook ran"
else
  fail "(T-03-mid2) hook mutated the run-state record"
fi
if grep -q '^- source: precompact hook (compaction imminent)$' "$MEM_DIR_TEST/run-notes-notes-mid.md" && tail -6 "$MEM_DIR_TEST/run-notes-notes-mid.md" | grep -qE '^## .+ — implement/code$'; then
  ok "(T-03-mid3) appended block re-reads as a valid notes block with provenance"
else
  fail "(T-03-mid3) appended block malformed"
fi
rm -f "$MEM_DIR_TEST/run-state-notes-mid.json" "$MEM_DIR_TEST/run-notes-notes-mid.md" "$TMPDIR_TEST/record.snap"
rm -f "$TMPDIR_TEST/.workflow_artifacts/memory/checkpoints/"*.md 2>/dev/null || true

# stage boundary: planted with --at-stage-boundary true (setup writes no
# notes file), and the hook must not create one either
python3 "$RUN_STATE" --write --project-root "$TMPDIR_TEST" --task notes-bnd --session-id sess-notes-bnd --phase implement --at-stage-boundary true >/dev/null 2>&1
if [ ! -f "$MEM_DIR_TEST/run-notes-notes-bnd.md" ]; then
  ok "(T-03-bnd-plant) boundary write created no notes file at setup"
else
  fail "(T-03-bnd-plant) setup unexpectedly created a notes file"
fi
stdin=$(make_stdin "auto" "sess-notes-bnd")
printf '%s' "$stdin" | sh "$HOOK" >/dev/null 2>&1 || true
if [ ! -f "$MEM_DIR_TEST/run-notes-notes-bnd.md" ]; then
  ok "(T-03-bnd) at_stage_boundary: true → hook appended nothing"
else
  fail "(T-03-bnd) hook appended on a stage boundary"
fi
rm -f "$MEM_DIR_TEST/run-state-notes-bnd.json"
rm -f "$TMPDIR_TEST/.workflow_artifacts/memory/checkpoints/"*.md 2>/dev/null || true

# symlinked notes path: refused; the link target stays untouched
python3 "$RUN_STATE" --write --project-root "$TMPDIR_TEST" --task notes-sym --session-id sess-notes-sym --phase implement >/dev/null 2>&1
printf 'victim content\n' > "$TMPDIR_TEST/victim.md"
rm -f "$MEM_DIR_TEST/run-notes-notes-sym.md"
ln -s "$TMPDIR_TEST/victim.md" "$MEM_DIR_TEST/run-notes-notes-sym.md"
stdin=$(make_stdin "auto" "sess-notes-sym")
out_sym=$(printf '%s' "$stdin" | sh "$HOOK" 2>/dev/null)
if [ "$(cat "$TMPDIR_TEST/victim.md")" = "victim content" ] && [ "$out_sym" = '{"decision": "allow"}' ]; then
  ok "(T-03-sym) symlinked notes path → no write-through, allow still emitted"
else
  fail "(T-03-sym) symlink guard failed"
fi
rm -f "$MEM_DIR_TEST/run-state-notes-sym.json" "$MEM_DIR_TEST/run-notes-notes-sym.md" "$TMPDIR_TEST/victim.md"
rm -f "$TMPDIR_TEST/.workflow_artifacts/memory/checkpoints/"*.md 2>/dev/null || true

# traversal inside the prefix: a notes_path that matches the prefix pattern
# but escapes through .. segments. The writer derives notes_path itself, so
# planting this variant edits one line of a test-owned record — the same
# documented sed carve-out as the schema:2 variant.
python3 "$RUN_STATE" --write --project-root "$TMPDIR_TEST" --task notes-trv --session-id sess-notes-trv --phase implement >/dev/null 2>&1
rm -f "$MEM_DIR_TEST/run-notes-notes-trv.md"
sed -i.bak 's|^  "notes_path": .*$|  "notes_path": "'"$MEM_DIR_TEST"'/run-notes-x/../../evil-target.md",|' "$MEM_DIR_TEST/run-state-notes-trv.json" && rm -f "$MEM_DIR_TEST/run-state-notes-trv.json.bak"
if grep -q 'run-notes-x/\.\./\.\./evil-target\.md' "$MEM_DIR_TEST/run-state-notes-trv.json"; then
  ok "(T-03-trv-plant) traversal notes_path precondition planted"
else
  fail "(T-03-trv-plant) planting recipe failed to embed the traversal path"
fi
stdin=$(make_stdin "auto" "sess-notes-trv")
out_trv=$(printf '%s' "$stdin" | sh "$HOOK" 2>/dev/null)
if [ ! -f "$TMPDIR_TEST/.workflow_artifacts/evil-target.md" ] && [ "$out_trv" = '{"decision": "allow"}' ]; then
  ok "(T-03-trv) traversal-inside-prefix notes_path → nothing appended, allow emitted"
else
  fail "(T-03-trv) traversal path was written through"
fi
rm -f "$MEM_DIR_TEST/run-state-notes-trv.json"
rm -f "$TMPDIR_TEST/.workflow_artifacts/memory/checkpoints/"*.md 2>/dev/null || true

# symlinked intermediate directory: a notes_path that matches the prefix
# pattern (case lets * match /) but routes through a symlinked dir planted
# inside MEMORY_DIR, escaping through the link. Planting edits one line of
# a test-owned record — the same documented sed carve-out as the traversal
# variant above.
python3 "$RUN_STATE" --write --project-root "$TMPDIR_TEST" --task notes-esc --session-id sess-notes-esc --phase implement >/dev/null 2>&1
rm -f "$MEM_DIR_TEST/run-notes-notes-esc.md"
ESC_OUTSIDE="$TMPDIR_TEST/esc-outside"
mkdir -p "$ESC_OUTSIDE"
ln -s "$ESC_OUTSIDE" "$MEM_DIR_TEST/run-notes-esc"
sed -i.bak 's|^  "notes_path": .*$|  "notes_path": "'"$MEM_DIR_TEST"'/run-notes-esc/target.md",|' "$MEM_DIR_TEST/run-state-notes-esc.json" && rm -f "$MEM_DIR_TEST/run-state-notes-esc.json.bak"
if grep -q 'run-notes-esc/target\.md' "$MEM_DIR_TEST/run-state-notes-esc.json"; then
  ok "(T-03-escdir-plant) symlinked-intermediate-dir notes_path precondition planted"
else
  fail "(T-03-escdir-plant) planting recipe failed to embed the symlinked-dir path"
fi
stdin=$(make_stdin "auto" "sess-notes-esc")
out_esc=$(printf '%s' "$stdin" | sh "$HOOK" 2>/dev/null)
if [ ! -f "$ESC_OUTSIDE/target.md" ] && [ "$out_esc" = '{"decision": "allow"}' ]; then
  ok "(T-03-escdir) symlinked-intermediate-dir notes_path → nothing appended, allow emitted"
else
  fail "(T-03-escdir) symlinked-dir path was written through"
fi
rm -f "$MEM_DIR_TEST/run-state-notes-esc.json" "$MEM_DIR_TEST/run-notes-esc"
rm -f "$TMPDIR_TEST/.workflow_artifacts/memory/checkpoints/"*.md 2>/dev/null || true

# FIFO planted at the notes path: a non-regular file would block the >>
# open itself and stall the hook to its harness timeout — the guard must
# refuse it and the hook must still emit its allow decision promptly.
python3 "$RUN_STATE" --write --project-root "$TMPDIR_TEST" --task notes-fifo --session-id sess-notes-fifo --phase implement --subphase code --step "fifo step" --next-action "fifo next" >/dev/null 2>&1
rm -f "$MEM_DIR_TEST/run-notes-notes-fifo.md"
mkfifo "$MEM_DIR_TEST/run-notes-notes-fifo.md"
stdin=$(make_stdin "auto" "sess-notes-fifo")
: > "$TMPDIR_TEST/fifo-notes-out.txt"
( printf '%s' "$stdin" | sh "$HOOK" > "$TMPDIR_TEST/fifo-notes-out.txt" 2>/dev/null ) &
_fifo_npid=$!
_w=0
while [ "$_w" -lt 50 ] && kill -0 "$_fifo_npid" 2>/dev/null; do
  sleep 0.1
  _w=$((_w + 1))
done
if kill -0 "$_fifo_npid" 2>/dev/null; then
  kill "$_fifo_npid" 2>/dev/null || true
  wait "$_fifo_npid" 2>/dev/null || true
  fail "(T-03-fifo) hook blocked on a FIFO notes path (killed at 5s watchdog)"
else
  wait "$_fifo_npid" 2>/dev/null || true
  out_nf=$(cat "$TMPDIR_TEST/fifo-notes-out.txt")
  if [ "$out_nf" = '{"decision": "allow"}' ]; then
    ok "(T-03-fifo) FIFO notes path → refused, allow emitted without hanging"
  else
    fail "(T-03-fifo) unexpected output with FIFO notes path (out=$out_nf)"
  fi
fi
rm -f "$MEM_DIR_TEST/run-state-notes-fifo.json" "$MEM_DIR_TEST/run-notes-notes-fifo.md" "$TMPDIR_TEST/fifo-notes-out.txt"
rm -f "$TMPDIR_TEST/.workflow_artifacts/memory/checkpoints/"*.md 2>/dev/null || true

# hard link planted at the notes path: passes every earlier predicate
# (no dotdot, prefix match, flat, not a symlink, regular file) — the
# link-count refusal is what keeps the append from landing in the
# link's other name outside memory/.
python3 "$RUN_STATE" --write --project-root "$TMPDIR_TEST" --task notes-hl --session-id sess-notes-hl --phase implement --subphase code --step "hl step" --next-action "hl next" >/dev/null 2>&1
rm -f "$MEM_DIR_TEST/run-notes-notes-hl.md"
printf 'victim\n' > "$TMPDIR_TEST/hl-victim.md"
ln "$TMPDIR_TEST/hl-victim.md" "$MEM_DIR_TEST/run-notes-notes-hl.md"
stdin=$(make_stdin "auto" "sess-notes-hl")
out_hl=$(printf '%s' "$stdin" | sh "$HOOK" 2>/dev/null)
if [ "$(cat "$TMPDIR_TEST/hl-victim.md")" = "victim" ] && [ "$out_hl" = '{"decision": "allow"}' ]; then
  ok "(T-03-hardlink) hard-linked notes path → nothing appended, allow emitted"
else
  fail "(T-03-hardlink) hard-linked notes path was appended through (out=$out_hl)"
fi
rm -f "$MEM_DIR_TEST/run-state-notes-hl.json" "$MEM_DIR_TEST/run-notes-notes-hl.md" "$TMPDIR_TEST/hl-victim.md"
rm -rf "$ESC_OUTSIDE"
rm -f "$TMPDIR_TEST/.workflow_artifacts/memory/checkpoints/"*.md 2>/dev/null || true

# well-formed notes_path outside this hook's memory dir: planted by writing
# the record under a different project root, then moving the record file
# (both real invocations / test-owned file operations)
OTHER_ROOT="$TMPDIR_TEST/otherroot"
mkdir -p "$OTHER_ROOT"
python3 "$RUN_STATE" --write --project-root "$OTHER_ROOT" --task notes-out --session-id sess-notes-out --phase implement >/dev/null 2>&1
mv "$OTHER_ROOT/.workflow_artifacts/memory/run-state-notes-out.json" "$MEM_DIR_TEST/run-state-notes-out.json"
rm -f "$OTHER_ROOT/.workflow_artifacts/memory/run-notes-notes-out.md"
stdin=$(make_stdin "auto" "sess-notes-out")
out_out=$(printf '%s' "$stdin" | sh "$HOOK" 2>/dev/null)
if [ ! -f "$OTHER_ROOT/.workflow_artifacts/memory/run-notes-notes-out.md" ] && [ "$out_out" = '{"decision": "allow"}' ]; then
  ok "(T-03-out) notes_path outside MEMORY_DIR → nothing appended, allow emitted"
else
  fail "(T-03-out) out-of-tree notes_path was written through"
fi
rm -f "$MEM_DIR_TEST/run-state-notes-out.json"
rm -rf "$OTHER_ROOT"
rm -f "$TMPDIR_TEST/.workflow_artifacts/memory/checkpoints/"*.md 2>/dev/null || true

# ─── T-04: telemetry pre-half fixtures ───────────────────────────────────────

TEL_SINK="$MEM_DIR_TEST/telemetry/compaction-events.jsonl"

# one compaction → exactly one pre line; second → event_seq one higher
stdin=$(make_stdin "auto" "sess-tel-basic")
printf '%s' "$stdin" | sh "$HOOK" >/dev/null 2>&1 || true
tel_n1=$(grep -F '"session_id":"sess-tel-basic"' "$TEL_SINK" 2>/dev/null | grep -cF '"half":"pre"') || tel_n1=0
if [ "$tel_n1" -eq 1 ]; then
  ok "(T-04-one) one compaction → exactly one pre line for this session"
else
  fail "(T-04-one) expected 1 pre line, found $tel_n1"
fi
seq_first=$(grep -F '"session_id":"sess-tel-basic"' "$TEL_SINK" | tail -1 | jq -r .event_seq)
printf '%s' "$stdin" | sh "$HOOK" >/dev/null 2>&1 || true
seq_second=$(grep -F '"session_id":"sess-tel-basic"' "$TEL_SINK" | tail -1 | jq -r .event_seq)
if [ "$seq_second" = "$((seq_first + 1))" ]; then
  ok "(T-04-seq) second compaction → event_seq exactly one higher"
else
  fail "(T-04-seq) event_seq did not increment: $seq_first -> $seq_second"
fi
if grep -F '"session_id":"sess-tel-basic"' "$TEL_SINK" | tail -1 | jq -e '.v == 1' >/dev/null 2>&1; then
  ok "(T-04-parse) telemetry line parses under jq -e and carries v: 1"
else
  fail "(T-04-parse) telemetry line does not parse or lacks v: 1"
fi
# no-run row → run fields are empty strings
task_field=$(grep -F '"session_id":"sess-tel-basic"' "$TEL_SINK" | tail -1 | jq -r .task)
if [ "$task_field" = "" ]; then
  ok "(T-04-empty) run fields are empty strings on the no-run row"
else
  fail "(T-04-empty) task field not empty on no-run row: $task_field"
fi

# hostile session id: line parses, and the sequence still increments under
# the same hostile id (fixed-string match against the jq-escaped form)
hostile_sid='sess-tel-"host\ile'
stdin_h=$(jq -nc --arg s "$hostile_sid" --arg c "$TMPDIR_TEST" '{trigger:"auto", session_id:$s, cwd:$c, transcript_path:($c+"/dummy.jsonl")}')
printf '%s' "$stdin_h" | sh "$HOOK" >/dev/null 2>&1 || true
printf '%s' "$stdin_h" | sh "$HOOK" >/dev/null 2>&1 || true
esc_sid=$(jq -nc --arg s "$hostile_sid" '$s')
tel_h=$(grep -F "\"session_id\":$esc_sid" "$TEL_SINK" 2>/dev/null | grep -cF '"half":"pre"') || tel_h=0
seq_h=$(grep -F "\"session_id\":$esc_sid" "$TEL_SINK" | tail -1 | jq -r .event_seq) || seq_h="?"
if [ "$tel_h" -eq 2 ] && [ "$seq_h" = "1" ]; then
  ok "(T-04-hostile) hostile session id → both lines land, event_seq increments"
else
  fail "(T-04-hostile) hostile id mishandled (lines=$tel_h seq=$seq_h)"
fi
if jq -e . "$TEL_SINK" >/dev/null 2>&1; then
  ok "(T-04-hostile2) whole sink still parses line-by-line under jq -e"
else
  fail "(T-04-hostile2) sink contains an unparseable line"
fi

# unreadable transcript → bytes fields empty, line still appended, and this
# is the one fail-OPEN fixture whose stdout is the allow JSON
stdin_u=$(jq -nc --arg c "$TMPDIR_TEST" '{trigger:"auto", session_id:"sess-tel-unread", cwd:$c, transcript_path:"/nonexistent-transcript-path"}')
out_u=$(printf '%s' "$stdin_u" | sh "$HOOK" 2>/dev/null)
bb_u=$(grep -F '"session_id":"sess-tel-unread"' "$TEL_SINK" | tail -1 | jq -r .bytes_before)
if [ "$out_u" = '{"decision": "allow"}' ] && [ "$bb_u" = "null" ]; then
  ok "(T-04-unread) unreadable transcript → null bytes fields, line appended, allow emitted"
else
  fail "(T-04-unread) unreadable transcript mishandled (out=$out_u bytes=$bb_u)"
fi

# sweep survival: the sink lives under telemetry/, one level below the
# depth-1 sentinel sweeps — run the sweep-shaped find and prove it misses
sweep_hits=$(sentinel_globs | while IFS= read -r _g; do find "$MEM_DIR_TEST" -maxdepth 1 -type f -name "$_g" 2>/dev/null; done)
depth1_hit=$(find "$MEM_DIR_TEST" -maxdepth 1 -name 'compaction-events.jsonl' 2>/dev/null)
if [ -f "$TEL_SINK" ] && [ -z "$depth1_hit" ] && ! printf '%s' "$sweep_hits" | grep -qF 'compaction-events'; then
  ok "(T-04-sweep) depth-1 sweeps cannot reach the telemetry sink"
else
  fail "(T-04-sweep) telemetry sink is visible to a depth-1 sweep"
fi
rm -f "$TMPDIR_TEST/.workflow_artifacts/memory/checkpoints/"*.md 2>/dev/null || true

# symlinked telemetry dir: refused; nothing lands in the link target
TEL_OUTSIDE="$TMPDIR_TEST/tel-outside"
mkdir -p "$TEL_OUTSIDE"
mv "$MEM_DIR_TEST/telemetry" "$TMPDIR_TEST/telemetry-real"
ln -s "$TEL_OUTSIDE" "$MEM_DIR_TEST/telemetry"
stdin=$(make_stdin "auto" "sess-tel-symdir")
out_tsd=$(printf '%s' "$stdin" | sh "$HOOK" 2>/dev/null)
if [ ! -f "$TEL_OUTSIDE/compaction-events.jsonl" ] && [ "$out_tsd" = '{"decision": "allow"}' ]; then
  ok "(T-04-symdir) symlinked telemetry dir → no write-through, allow emitted"
else
  fail "(T-04-symdir) telemetry append followed a symlinked dir"
fi
rm -f "$MEM_DIR_TEST/telemetry"
mv "$TMPDIR_TEST/telemetry-real" "$MEM_DIR_TEST/telemetry"
rm -rf "$TEL_OUTSIDE"

# symlinked sink file: refused; the link target stays untouched
printf 'victim\n' > "$TMPDIR_TEST/tel-victim.jsonl"
mv "$TEL_SINK" "$TMPDIR_TEST/tel-sink-real.jsonl"
ln -s "$TMPDIR_TEST/tel-victim.jsonl" "$TEL_SINK"
stdin=$(make_stdin "auto" "sess-tel-symfile")
out_tsf=$(printf '%s' "$stdin" | sh "$HOOK" 2>/dev/null)
if [ "$(cat "$TMPDIR_TEST/tel-victim.jsonl")" = "victim" ] && [ "$out_tsf" = '{"decision": "allow"}' ]; then
  ok "(T-04-symfile) symlinked telemetry sink → no write-through, allow emitted"
else
  fail "(T-04-symfile) telemetry append followed a symlinked sink"
fi
rm -f "$TEL_SINK" "$TMPDIR_TEST/tel-victim.jsonl"
mv "$TMPDIR_TEST/tel-sink-real.jsonl" "$TEL_SINK"

# FIFO planted at the telemetry sink: a non-regular file would block the
# append's open and hang the hook to its harness timeout, suppressing the
# checkpoint on every row — the guard must refuse it outright.
mv "$TEL_SINK" "$TMPDIR_TEST/tel-sink-real2.jsonl"
mkfifo "$TEL_SINK"
stdin=$(make_stdin "auto" "sess-tel-fifo")
: > "$TMPDIR_TEST/fifo-tel-out.txt"
( printf '%s' "$stdin" | sh "$HOOK" > "$TMPDIR_TEST/fifo-tel-out.txt" 2>/dev/null ) &
_fifo_tpid=$!
_w=0
while [ "$_w" -lt 50 ] && kill -0 "$_fifo_tpid" 2>/dev/null; do
  sleep 0.1
  _w=$((_w + 1))
done
if kill -0 "$_fifo_tpid" 2>/dev/null; then
  kill "$_fifo_tpid" 2>/dev/null || true
  wait "$_fifo_tpid" 2>/dev/null || true
  fail "(T-04-fifo) hook blocked on a FIFO telemetry sink (killed at 5s watchdog)"
else
  wait "$_fifo_tpid" 2>/dev/null || true
  out_tf=$(cat "$TMPDIR_TEST/fifo-tel-out.txt")
  if [ "$out_tf" = '{"decision": "allow"}' ]; then
    ok "(T-04-fifo) FIFO telemetry sink → refused, allow emitted without hanging"
  else
    fail "(T-04-fifo) unexpected output with FIFO telemetry sink (out=$out_tf)"
  fi
fi
rm -f "$TEL_SINK" "$TMPDIR_TEST/fifo-tel-out.txt"
mv "$TMPDIR_TEST/tel-sink-real2.jsonl" "$TEL_SINK"

# hard link planted at the telemetry sink: passes the symlink and
# regular-file predicates — the link-count refusal keeps the append from
# landing in the link's other name.
printf 'victim\n' > "$TMPDIR_TEST/tel-hl-victim.jsonl"
mv "$TEL_SINK" "$TMPDIR_TEST/tel-sink-real3.jsonl"
ln "$TMPDIR_TEST/tel-hl-victim.jsonl" "$TEL_SINK"
stdin=$(make_stdin "auto" "sess-tel-hl")
out_thl=$(printf '%s' "$stdin" | sh "$HOOK" 2>/dev/null)
if [ "$(cat "$TMPDIR_TEST/tel-hl-victim.jsonl")" = "victim" ] && [ "$out_thl" = '{"decision": "allow"}' ]; then
  ok "(T-04-hardlink) hard-linked telemetry sink → nothing appended, allow emitted"
else
  fail "(T-04-hardlink) hard-linked telemetry sink was appended through (out=$out_thl)"
fi
rm -f "$TEL_SINK" "$TMPDIR_TEST/tel-hl-victim.jsonl"
mv "$TMPDIR_TEST/tel-sink-real3.jsonl" "$TEL_SINK"

# FIFO planted where the telemetry DIRECTORY should be: mkdir -p fails on
# a non-directory path and the real-directory predicate backstops it — the
# hook must refuse promptly and still emit its allow decision.
mv "$MEM_DIR_TEST/telemetry" "$TMPDIR_TEST/telemetry-real3"
mkfifo "$MEM_DIR_TEST/telemetry"
stdin=$(make_stdin "auto" "sess-tel-fifodir")
: > "$TMPDIR_TEST/fifodir-tel-out.txt"
( printf '%s' "$stdin" | sh "$HOOK" > "$TMPDIR_TEST/fifodir-tel-out.txt" 2>/dev/null ) &
_fifo_dpid=$!
_w=0
while [ "$_w" -lt 50 ] && kill -0 "$_fifo_dpid" 2>/dev/null; do
  sleep 0.1
  _w=$((_w + 1))
done
if kill -0 "$_fifo_dpid" 2>/dev/null; then
  kill "$_fifo_dpid" 2>/dev/null || true
  wait "$_fifo_dpid" 2>/dev/null || true
  fail "(T-04-fifodir) hook blocked on a FIFO telemetry dir (killed at 5s watchdog)"
else
  wait "$_fifo_dpid" 2>/dev/null || true
  out_tfd=$(cat "$TMPDIR_TEST/fifodir-tel-out.txt")
  if [ "$out_tfd" = '{"decision": "allow"}' ]; then
    ok "(T-04-fifodir) FIFO telemetry dir → refused, allow emitted without hanging"
  else
    fail "(T-04-fifodir) unexpected output with FIFO telemetry dir (out=$out_tfd)"
  fi
fi
rm -f "$MEM_DIR_TEST/telemetry" "$TMPDIR_TEST/fifodir-tel-out.txt"
mv "$TMPDIR_TEST/telemetry-real3" "$MEM_DIR_TEST/telemetry"
rm -f "$TMPDIR_TEST/.workflow_artifacts/memory/checkpoints/"*.md 2>/dev/null || true

# ─── T-06: fail-OPEN fixtures ────────────────────────────────────────────────

# malformed stdin → exit 0, no stdout, no veto
rc_mf=0
out_mf=$(printf 'not json' | sh "$HOOK" 2>/dev/null) || rc_mf=$?
if [ "$rc_mf" -eq 0 ] && [ -z "$out_mf" ]; then
  ok "(T-06-badstdin) malformed stdin → exit 0, empty stdout"
else
  fail "(T-06-badstdin) malformed stdin → rc=$rc_mf out=$out_mf"
fi

# missing session_id → exit 0, empty stdout (aborts before the allow print)
stdin_nosid='{"trigger":"auto","cwd":"'"$TMPDIR_TEST"'","transcript_path":"'"$TMPDIR_TEST"'/dummy.jsonl"}'
rc_ns=0
out_ns=$(printf '%s' "$stdin_nosid" | sh "$HOOK" 2>/dev/null) || rc_ns=$?
if [ "$rc_ns" -eq 0 ] && [ -z "$out_ns" ]; then
  ok "(T-06-nosid) missing session_id → exit 0, empty stdout"
else
  fail "(T-06-nosid) missing session_id → rc=$rc_ns out=$out_ns"
fi

# jq absent: a PATH holding every coreutil the hook needs before its first
# jq call — and deliberately NOT jq — so the fixture proves the hook
# sources _lib.sh, reads stdin, and only then fails open at the jq parse.
STUB_DIR="$TMPDIR_TEST/stubpath"
mkdir -p "$STUB_DIR"
stub_missing=""
for _u in cat dirname date find grep sed awk ls mkdir mv wc basename head tr xargs rm; do
  _up=$(command -v "$_u" 2>/dev/null) || _up=""
  if [ -n "$_up" ]; then
    ln -s "$_up" "$STUB_DIR/$_u" 2>/dev/null || true
  fi
  [ -x "$STUB_DIR/$_u" ] || stub_missing="$stub_missing $_u"
done
if [ -z "$stub_missing" ]; then
  ok "(T-06-nojq-plant) stub PATH resolves every coreutil the hook needs pre-jq"
else
  fail "(T-06-nojq-plant) stub PATH missing:$stub_missing"
fi
if [ ! -e "$STUB_DIR/jq" ]; then
  ok "(T-06-nojq-plant2) jq deliberately absent from the stub PATH"
else
  fail "(T-06-nojq-plant2) jq unexpectedly present in the stub PATH"
fi
stdin=$(make_stdin "auto" "sess-nojq")
rc_nojq=0
out_nojq=$(printf '%s' "$stdin" | PATH="$STUB_DIR" /bin/sh "$HOOK" 2>/dev/null) || rc_nojq=$?
if [ "$rc_nojq" -eq 0 ] && [ -z "$out_nojq" ]; then
  ok "(T-06-nojq) jq absent → exit 0, empty stdout (failed open at the first jq parse)"
else
  fail "(T-06-nojq) jq absent → rc=$rc_nojq out=$out_nojq"
fi

# jq stubbed to fail only on -n builds (the telemetry line): the decision
# must be unaffected and the sink must gain nothing
JQSTUB_DIR="$TMPDIR_TEST/jqstub"
mkdir -p "$JQSTUB_DIR"
real_jq=$(command -v jq)
cat > "$JQSTUB_DIR/jq" <<JQEOF
#!/bin/sh
case "\$1" in
  -n|-nc) exit 1 ;;
esac
exec "$real_jq" "\$@"
JQEOF
chmod +x "$JQSTUB_DIR/jq"
tel_js_before=$(grep -cF '"session_id":"sess-jqstub"' "$TEL_SINK" 2>/dev/null) || tel_js_before=0
stdin=$(make_stdin "auto" "sess-jqstub")
rc_js=0
out_js=$(printf '%s' "$stdin" | PATH="$JQSTUB_DIR:$PATH" sh "$HOOK" 2>/dev/null) || rc_js=$?
tel_js_after=$(grep -cF '"session_id":"sess-jqstub"' "$TEL_SINK" 2>/dev/null) || tel_js_after=0
if [ "$rc_js" -eq 0 ] && [ "$out_js" = '{"decision": "allow"}' ] && [ "$tel_js_after" = "$tel_js_before" ]; then
  ok "(T-06-jqstub) telemetry-build failure → decision unchanged, nothing appended"
else
  fail "(T-06-jqstub) telemetry failure leaked (rc=$rc_js out=$out_js tel=$tel_js_before->$tel_js_after)"
fi

# no veto anywhere: none of the fail-OPEN fixtures above may emit "block"
for _o in "$out_mf" "$out_ns" "$out_nojq" "$out_js"; do
  if printf '%s' "$_o" | grep -q '"block"' 2>/dev/null; then
    fail "(T-06-noveto) a fail-OPEN fixture emitted a veto: $_o"
  fi
done
ok "(T-06-noveto) no fail-OPEN fixture emitted a veto"

# hooks/ contains no autonomous-mode text (shell-layer mirror of the pytest guard)
if grep -li 'autonomous' "$SCRIPT_DIR/../../hooks/"*.sh >/dev/null 2>&1; then
  fail "(T-06-scan) a file in quoin/hooks/ mentions autonomous"
else
  ok "(T-06-scan) no file in quoin/hooks/ mentions autonomous"
fi

# ─── T-07: rotation-hostile targets at ${_tel_sink}.1 ────────────────────────
# The live sink already carries plenty of bytes from the fixtures above, so a
# small QUOIN_TELEMETRY_MAX_BYTES forces rotation on the very next pre append.

# symlink-to-directory at the rotation target: unlink(2) on a symlink path
# never follows it, so the stale link is removed and rotation then proceeds
# onto a fresh regular file — nothing is ever written through the link.
ROT_SYMDIR_TARGET="$TMPDIR_TEST/rot-symdir-target"
mkdir -p "$ROT_SYMDIR_TARGET"
ln -s "$ROT_SYMDIR_TARGET" "${TEL_SINK}.1"
stdin=$(make_stdin "auto" "sess-rot-symdir")
out_rsd=$(printf '%s' "$stdin" | QUOIN_TELEMETRY_MAX_BYTES=1 sh "$HOOK" 2>/dev/null)
if [ "$out_rsd" = '{"decision": "allow"}' ] && [ -z "$(ls -A "$ROT_SYMDIR_TARGET" 2>/dev/null)" ] && [ -f "${TEL_SINK}.1" ] && [ ! -L "${TEL_SINK}.1" ]; then
  ok "(T-07-rot-symdir) symlink-to-directory at rotation target → link removed, rotation proceeds, nothing written through it"
else
  fail "(T-07-rot-symdir) rotation target symlink mishandled (out=$out_rsd)"
fi
rm -f "${TEL_SINK}.1"
rm -rf "$ROT_SYMDIR_TARGET"

# FIFO at the rotation target: refused outright (not a regular file); the
# append that would have followed rotation is skipped entirely for this row,
# and the hook must never block on it — run under the existing 5s watchdog.
mkfifo "${TEL_SINK}.1"
stdin=$(make_stdin "auto" "sess-rot-fifo")
: > "$TMPDIR_TEST/rot-fifo-out.txt"
( printf '%s' "$stdin" | QUOIN_TELEMETRY_MAX_BYTES=1 sh "$HOOK" > "$TMPDIR_TEST/rot-fifo-out.txt" 2>/dev/null ) &
_rot_fifo_pid=$!
_w=0
while [ "$_w" -lt 50 ] && kill -0 "$_rot_fifo_pid" 2>/dev/null; do
  sleep 0.1
  _w=$((_w + 1))
done
if kill -0 "$_rot_fifo_pid" 2>/dev/null; then
  kill "$_rot_fifo_pid" 2>/dev/null || true
  wait "$_rot_fifo_pid" 2>/dev/null || true
  fail "(T-07-rot-fifo) hook blocked on a FIFO rotation target (killed at 5s watchdog)"
else
  wait "$_rot_fifo_pid" 2>/dev/null || true
  out_rfifo=$(cat "$TMPDIR_TEST/rot-fifo-out.txt")
  if [ "$out_rfifo" = '{"decision": "allow"}' ] && [ -p "${TEL_SINK}.1" ]; then
    ok "(T-07-rot-fifo) FIFO rotation target → refused, allow emitted without hanging, nothing written into it"
  else
    fail "(T-07-rot-fifo) unexpected outcome with FIFO rotation target (out=$out_rfifo)"
  fi
fi
rm -f "${TEL_SINK}.1" "$TMPDIR_TEST/rot-fifo-out.txt"

# plain directory at the rotation target: refused outright, same as the FIFO
mkdir -p "${TEL_SINK}.1"
stdin=$(make_stdin "auto" "sess-rot-dir")
out_rdir=$(printf '%s' "$stdin" | QUOIN_TELEMETRY_MAX_BYTES=1 sh "$HOOK" 2>/dev/null)
if [ "$out_rdir" = '{"decision": "allow"}' ] && [ -d "${TEL_SINK}.1" ] && [ -z "$(ls -A "${TEL_SINK}.1" 2>/dev/null)" ]; then
  ok "(T-07-rot-dir) plain directory at rotation target → refused, allow emitted, directory left empty"
else
  fail "(T-07-rot-dir) rotation target directory mishandled (out=$out_rdir)"
fi
rm -rf "${TEL_SINK}.1"

# unmatched pre: precompact.sh alone, no post half — hook exits 0 and the
# sink still parses line-by-line under jq -e (T-04, D-06)
stdin=$(make_stdin "auto" "sess-rot-unmatched-pre")
rc_ump=0
printf '%s' "$stdin" | sh "$HOOK" >/dev/null 2>&1 || rc_ump=$?
if [ "$rc_ump" -eq 0 ] && jq -e . "$TEL_SINK" >/dev/null 2>&1; then
  ok "(T-07-unmatched-pre) precompact-only pre → exit 0, sink still parses line-by-line"
else
  fail "(T-07-unmatched-pre) precompact-only pre mishandled (rc=$rc_ump)"
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
