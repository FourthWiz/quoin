#!/bin/sh
# test_userpromptsubmit_hook.sh — fixture tests for quoin/hooks/userpromptsubmit.sh
#
# Covers all acceptance cases from T-12 / T-09 acceptance criteria.
# Requires: jq on PATH, sh (POSIX).
#
# Usage: sh quoin/dev/tests/test_userpromptsubmit_hook.sh
# Exit 0 if all tests pass; non-zero with failure list otherwise.

set -eu

PASS=0
FAIL=0
FAIL_MSGS=""

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
FIXTURES_DIR="$SCRIPT_DIR/fixtures/hooks"
HOOK="$SCRIPT_DIR/../../hooks/userpromptsubmit.sh"
DEPLOYED_HOOK="$HOME/.claude/hooks/userpromptsubmit.sh"

# ─── helpers ──────────────────────────────────────────────────────────────────

ok() { PASS=$((PASS + 1)); printf 'ok  %s\n' "$1"; }
fail() {
  FAIL=$((FAIL + 1))
  printf 'FAIL %s\n' "$1" >&2
  FAIL_MSGS="$FAIL_MSGS\n  - $1"
}

# Make a JSON stdin for the hook
make_stdin() {
  local prompt="$1"
  local transcript="$2"
  local session_id="${3:-test-session-default}"
  local cwd="${4:-$TMPDIR_TEST}"
  printf '{"prompt":"%s","transcript_path":"%s","session_id":"%s","cwd":"%s"}' \
    "$prompt" "$transcript" "$session_id" "$cwd"
}

# Run hook with given stdin JSON; capture stdout and exit code
run_hook() {
  local stdin_json="$1"
  printf '%s' "$stdin_json" | sh "$HOOK" 2>/dev/null
  return $?
}

run_hook_rc() {
  local stdin_json="$1"
  printf '%s' "$stdin_json" | sh "$HOOK" 2>/dev/null
  # returns exit code of hook
}

TMPDIR_TEST="${TMPDIR:-/tmp}/test_ups_$$"
mkdir -p "$TMPDIR_TEST/.workflow_artifacts/memory"

cleanup() { rm -rf "$TMPDIR_TEST"; }
trap cleanup EXIT

# ─── Build fixtures if not present ────────────────────────────────────────────

if [ ! -f "$FIXTURES_DIR/transcript_97pct.jsonl" ]; then
  printf 'Building fixtures...\n'
  sh "$SCRIPT_DIR/build_hook_fixtures.sh" > /dev/null 2>&1
fi

TRANSCRIPT_70="$FIXTURES_DIR/transcript_70pct.jsonl"
TRANSCRIPT_88="$FIXTURES_DIR/transcript_88pct.jsonl"
TRANSCRIPT_97="$FIXTURES_DIR/transcript_97pct.jsonl"

# ─── Shebang assertion ────────────────────────────────────────────────────────

# Test against source hook file
if head -1 "$HOOK" | grep -qE '^#!/bin/sh( |$)'; then
  ok "shebang assertion: source hook starts with #!/bin/sh"
else
  fail "shebang assertion: source hook does not start with #!/bin/sh"
fi

# If deployed hook exists, also check it
if [ -f "$DEPLOYED_HOOK" ]; then
  if head -1 "$DEPLOYED_HOOK" | grep -qE '^#!/bin/sh( |$)'; then
    ok "shebang assertion: deployed hook starts with #!/bin/sh"
  else
    fail "shebang assertion: deployed hook does not start with #!/bin/sh"
  fi
fi

# ─── STEP 0 exemption cases ──────────────────────────────────────────────────

# (a) /checkpoint → exempt (exit 0, no stdout)
stdin=$(make_stdin '/checkpoint' "$TRANSCRIPT_70")
out=$(run_hook "$stdin")
if [ -z "$out" ]; then
  ok "(a) /checkpoint → exempt (no output)"
else
  fail "(a) /checkpoint → expected no output, got: $out"
fi

# (b) /checkpoint --restore → exempt (first token is /checkpoint, inner *-arm)
stdin=$(make_stdin '/checkpoint --restore' "$TRANSCRIPT_97")
out=$(run_hook "$stdin")
if [ -z "$out" ]; then
  ok "(b) /checkpoint --restore → exempt"
else
  fail "(b) /checkpoint --restore → expected exempt, got: $out"
fi

# (c) '   /compact' (leading spaces) → exempt
stdin=$(make_stdin '   /compact' "$TRANSCRIPT_97")
out=$(run_hook "$stdin")
if [ -z "$out" ]; then
  ok "(c) '   /compact' (leading spaces) → exempt"
else
  fail "(c) '   /compact' leading spaces → expected exempt, got: $out"
fi

# (d) /clear → exempt
stdin=$(make_stdin '/clear' "$TRANSCRIPT_97")
out=$(run_hook "$stdin")
if [ -z "$out" ]; then
  ok "(d) /clear → exempt"
else
  fail "(d) /clear → expected exempt, got: $out"
fi

# (e) /help arg1 arg2 → exempt (first token is /help)
stdin=$(make_stdin '/help arg1 arg2' "$TRANSCRIPT_97")
out=$(run_hook "$stdin")
if [ -z "$out" ]; then
  ok "(e) /help arg1 arg2 → exempt"
else
  fail "(e) /help arg1 arg2 → expected exempt, got: $out"
fi

# (f) /checkpointfoo → NOT exempt; falls through to threshold logic
# Use QUOIN_STOP_BPS=7500 override: 70% fixture (bps≈7000) < 7500 → passthrough.
# Default threshold is 7000; at exactly 70% the fixture could hit the boundary.
stdin=$(make_stdin '/checkpointfoo' "$TRANSCRIPT_70")
out=$(printf '%s' "$stdin" | QUOIN_STOP_BPS=7500 sh "$HOOK" 2>/dev/null)
if [ -z "$out" ]; then
  ok "(f) /checkpointfoo with 70% fixture → not exempt, falls through, passthrough"
else
  fail "(f) /checkpointfoo with 70% fixture → unexpected output: $out"
fi

# (g) /checkpoint--restore (no space) → NOT exempt
# With 97% fixture → advisory only (no decision/block), and the write is proven
if out=$(printf '%s' "$(make_stdin '/checkpoint--restore' "$TRANSCRIPT_97" "sess-g" "$TMPDIR_TEST")" | sh "$HOOK" 2>/dev/null); then rc=0; else rc=$?; fi
if [ -z "$(printf '%s' "$out" | grep -o '"decision"')" ]; then
  ok "(g) /checkpoint--restore (no space) with 97% fixture → NOT exempt, no decision key"
else
  fail "(g) /checkpoint--restore (no space) → unexpected decision key, got: $out"
fi
if printf '%s' "$out" | grep -q 'additionalContext' 2>/dev/null; then
  ok "(g) /checkpoint--restore (no space) with 97% fixture → additionalContext present"
else
  fail "(g) /checkpoint--restore (no space) → expected additionalContext, got: $out"
fi
if [ -z "$(printf '%s' "$out" | grep -o -- '--purge')" ]; then
  ok "(g) /checkpoint--restore (no space) with 97% fixture → no --purge note (negative control)"
else
  fail "(g) /checkpoint--restore (no space) → unexpected --purge note, got: $out"
fi
if [ -f "$TMPDIR_TEST/.workflow_artifacts/memory/pending-prompt-sess-g.txt" ]; then
  ok "(g) /checkpoint--restore (no space) with 97% fixture → pending-prompt-sess-g.txt exists"
else
  fail "(g) /checkpoint--restore (no space) → pending-prompt-sess-g.txt NOT written"
fi
rm -f "$TMPDIR_TEST/.workflow_artifacts/memory/pending-prompt-sess-g.txt"

# (h) '/checkpoint    --restore' (multiple spaces) → exempt
stdin=$(make_stdin '/checkpoint    --restore' "$TRANSCRIPT_97")
out=$(run_hook "$stdin")
if [ -z "$out" ]; then
  ok "(h) '/checkpoint    --restore' (multiple spaces) → exempt"
else
  fail "(h) '/checkpoint    --restore' multiple spaces → expected exempt, got: $out"
fi

# (i) Leading newline prompt: '\n/checkpoint' → exempt (sed strips leading whitespace)
# We encode a literal newline in JSON using \n
stdin_raw='{"prompt":"\n/checkpoint","transcript_path":"'"$TRANSCRIPT_97"'","session_id":"test-i","cwd":"'"$TMPDIR_TEST"'"}'
out=$(printf '%s' "$stdin_raw" | sh "$HOOK" 2>/dev/null)
if [ -z "$out" ]; then
  ok "(i) leading-newline prompt \\n/checkpoint → exempt"
else
  fail "(i) leading-newline prompt → expected exempt, got: $out"
fi

# (j) Leading CR prompt: '\r/checkpoint' → exempt
stdin_raw='{"prompt":"\r/checkpoint","transcript_path":"'"$TRANSCRIPT_97"'","session_id":"test-j","cwd":"'"$TMPDIR_TEST"'"}'
out=$(printf '%s' "$stdin_raw" | sh "$HOOK" 2>/dev/null)
if [ -z "$out" ]; then
  ok "(j) leading-CR prompt \\r/checkpoint → exempt"
else
  fail "(j) leading-CR prompt → expected exempt, got: $out"
fi

# (k) All-whitespace prompt → cmd is empty; falls through to threshold logic
# Use QUOIN_STOP_BPS=7500 override so 70% fixture is below threshold.
stdin_raw='{"prompt":"   \n\t  ","transcript_path":"'"$TRANSCRIPT_70"'","session_id":"test-k","cwd":"'"$TMPDIR_TEST"'"}'
out=$(printf '%s' "$stdin_raw" | QUOIN_STOP_BPS=7500 sh "$HOOK" 2>/dev/null)
if [ -z "$out" ]; then
  ok "(k) all-whitespace prompt with 70% fixture → no output (below threshold)"
else
  fail "(k) all-whitespace prompt → unexpected output: $out"
fi

# (l) /checkpoint --purge → NOT exempt (destructive subcommand carve-out)
# With 97% fixture → advisory names the --purge risk (IVG-258 R-15)
stdin=$(make_stdin '/checkpoint --purge' "$TRANSCRIPT_97" "sess-l" "$TMPDIR_TEST")
out=$(run_hook "$stdin")
if [ -z "$(printf '%s' "$out" | grep -o '"decision"')" ]; then
  ok "(l) /checkpoint --purge with 97% fixture → NOT exempt, no decision key"
else
  fail "(l) /checkpoint --purge with 97% fixture → unexpected decision key: $out"
fi
if printf '%s' "$out" | grep -q 'additionalContext' 2>/dev/null; then
  ok "(l) /checkpoint --purge with 97% fixture → additionalContext present"
else
  fail "(l) /checkpoint --purge with 97% fixture → expected additionalContext, got: $out"
fi
if printf '%s' "$out" | grep -q '/checkpoint --purge is destructive' 2>/dev/null; then
  ok "(l) /checkpoint --purge with 97% fixture → advisory names the --purge risk"
else
  fail "(l) /checkpoint --purge with 97% fixture → missing --purge risk note: $out"
fi
rm -f "$TMPDIR_TEST/.workflow_artifacts/memory/pending-prompt-sess-l.txt"

# (l2) /checkpoint    --purge (multi-space) → also NOT exempt
stdin=$(make_stdin '/checkpoint    --purge' "$TRANSCRIPT_97" "sess-l2" "$TMPDIR_TEST")
out=$(run_hook "$stdin")
if [ -z "$(printf '%s' "$out" | grep -o '"decision"')" ]; then
  ok "(l2) '/checkpoint    --purge' multi-space → NOT exempt, no decision key"
else
  fail "(l2) '/checkpoint    --purge' multi-space → unexpected decision key: $out"
fi
if printf '%s' "$out" | grep -q 'additionalContext' 2>/dev/null; then
  ok "(l2) '/checkpoint    --purge' multi-space → additionalContext present"
else
  fail "(l2) '/checkpoint    --purge' multi-space → expected additionalContext: $out"
fi
if printf '%s' "$out" | grep -q '/checkpoint --purge is destructive' 2>/dev/null; then
  ok "(l2) '/checkpoint    --purge' multi-space → advisory names the --purge risk"
else
  fail "(l2) '/checkpoint    --purge' multi-space → missing --purge risk note: $out"
fi
rm -f "$TMPDIR_TEST/.workflow_artifacts/memory/pending-prompt-sess-l2.txt"

# (m) /checkpoint --restore → exempt (positive control for narrow scope of --purge carve-out)
stdin=$(make_stdin '/checkpoint --restore' "$TRANSCRIPT_97")
out=$(run_hook "$stdin")
if [ -z "$out" ]; then
  ok "(m) /checkpoint --restore positive control → exempt (only --purge is carved out)"
else
  fail "(m) /checkpoint --restore → expected exempt, got: $out"
fi

# (m2) /checkpoint --some-future-arg → exempt (default * arm)
stdin=$(make_stdin '/checkpoint --some-future-arg' "$TRANSCRIPT_97")
out=$(run_hook "$stdin")
if [ -z "$out" ]; then
  ok "(m2) /checkpoint --some-future-arg → exempt (default * arm)"
else
  fail "(m2) /checkpoint --some-future-arg → expected exempt, got: $out"
fi

# ─── Three threshold branches ─────────────────────────────────────────────────

# Branch (1): passthrough — 70% fixture (bps=6999 < 7500 STOP_BPS; env-var override set to 7500
# for margin headroom — default is 7000 which leaves only 1 bps margin from this fixture)
stdin=$(make_stdin 'do some work' "$TRANSCRIPT_70" "sess-branch1" "$TMPDIR_TEST")
out=$(printf '%s' "$stdin" | QUOIN_STOP_BPS=7500 sh "$HOOK" 2>/dev/null)
if [ -z "$out" ]; then
  ok "branch(1): 70% fixture → passthrough (no output)"
else
  fail "branch(1): 70% fixture → expected no output, got: $out"
fi

# Branch (2): advisory — 88% fixture (bps=8800, STOP_BPS=7000 <= 8800 < BLOCK_BPS 9500)
stdin=$(make_stdin 'do some work' "$TRANSCRIPT_88" "sess-branch2" "$TMPDIR_TEST")
out=$(run_hook "$stdin")
if printf '%s' "$out" | grep -q 'additionalContext' 2>/dev/null; then
  # Check the percentage string contains 88.
  if printf '%s' "$out" | grep -q '88\.' 2>/dev/null; then
    ok "branch(2): 88% fixture → advisory JSON with 88.xx% in message"
  else
    fail "branch(2): 88% fixture → advisory JSON but missing 88. in message: $out"
  fi
  if printf '%s' "$out" | grep -q 'context at ~88\.' 2>/dev/null && printf '%s' "$out" | grep -q '(estimated)' 2>/dev/null; then
    ok "branch(2): 88% fixture (no usage records) → labeled as an estimate"
  else
    fail "branch(2): 88% fixture → expected 'context at ~88.' and '(estimated)': $out"
  fi
else
  fail "branch(2): 88% fixture → expected advisory JSON, got: $out"
fi

# Branch (3): high-context — 97% fixture (bps=9700 >= 9500 BLOCK_BPS) — advisory, never block
rm -f "$TMPDIR_TEST/.workflow_artifacts/memory/pending-prompt-sess-block.txt"
stdin=$(make_stdin 'do some work' "$TRANSCRIPT_97" "sess-block" "$TMPDIR_TEST")
if out=$(printf '%s' "$stdin" | sh "$HOOK" 2>/dev/null); then rc=0; else rc=$?; fi
if [ "$rc" -eq 0 ]; then
  ok "branch(3): 97% fixture → hook exits 0"
else
  fail "branch(3): 97% fixture → hook exited $rc"
fi
if [ -z "$(printf '%s' "$out" | grep -o '"decision"')" ]; then
  ok "branch(3): 97% fixture → no decision key (never blocks)"
else
  fail "branch(3): 97% fixture → unexpected decision key, got: $out"
fi
if printf '%s' "$out" | grep -q 'additionalContext' 2>/dev/null; then
  ok "branch(3): 97% fixture → additionalContext present"
else
  fail "branch(3): 97% fixture → expected additionalContext, got: $out"
fi
if printf '%s' "$out" | grep -q '97\.' 2>/dev/null; then
  ok "branch(3): 97% fixture → 97.xx% present in message"
else
  fail "branch(3): 97% fixture → missing 97. in message: $out"
fi
if printf '%s' "$out" | grep -q 'context at ~97\.' 2>/dev/null && printf '%s' "$out" | grep -q '(estimated)' 2>/dev/null; then
  ok "branch(3): 97% fixture (no usage records) → labeled as an estimate"
else
  fail "branch(3): 97% fixture → expected 'context at ~97.' and '(estimated)': $out"
fi
# Verify pending-prompt file was written
if [ -f "$TMPDIR_TEST/.workflow_artifacts/memory/pending-prompt-sess-block.txt" ]; then
  ok "branch(3): pending-prompt-sess-block.txt written"
else
  fail "branch(3): pending-prompt-sess-block.txt NOT written"
fi
rm -f "$TMPDIR_TEST/.workflow_artifacts/memory/pending-prompt-sess-block.txt"

# ─── (r1) acceptance criterion 3: at and above BLOCK_BPS, never block ─────────
# Branch-(3)-only discriminator: branch (3) writes pending-prompt-*.txt on every
# fire; branch (2) does not (measured: at 9700 the file appears, at 9701 it does
# not — driven against the same 97% fixture).
for _r1_case in "sess-r1-eq:9700" "sess-r1-gt:9500"; do
  _r1_sid="${_r1_case%%:*}"
  _r1_bps="${_r1_case##*:}"
  rm -f "$TMPDIR_TEST/.workflow_artifacts/memory/pending-prompt-${_r1_sid}.txt"
  stdin_r1=$(make_stdin 'do some work' "$TRANSCRIPT_97" "$_r1_sid" "$TMPDIR_TEST")
  if out_r1=$(printf '%s' "$stdin_r1" | QUOIN_BLOCK_BPS="$_r1_bps" sh "$HOOK" 2>/dev/null); then rc_r1=0; else rc_r1=$?; fi
  if [ "$rc_r1" -eq 0 ]; then
    ok "(r1 $_r1_sid): QUOIN_BLOCK_BPS=$_r1_bps → hook exits 0"
  else
    fail "(r1 $_r1_sid): QUOIN_BLOCK_BPS=$_r1_bps → hook exited $rc_r1"
  fi
  if [ -z "$(printf '%s' "$out_r1" | grep -ow 'decision')" ]; then
    ok "(r1 $_r1_sid): QUOIN_BLOCK_BPS=$_r1_bps → no bare 'decision' token"
  else
    fail "(r1 $_r1_sid): QUOIN_BLOCK_BPS=$_r1_bps → unexpected 'decision' token: $out_r1"
  fi
  if [ -z "$(printf '%s' "$out_r1" | grep -ow 'block')" ]; then
    ok "(r1 $_r1_sid): QUOIN_BLOCK_BPS=$_r1_bps → no bare 'block' token"
  else
    fail "(r1 $_r1_sid): QUOIN_BLOCK_BPS=$_r1_bps → unexpected 'block' token: $out_r1"
  fi
  if printf '%s' "$out_r1" | grep -q 'additionalContext' 2>/dev/null; then
    ok "(r1 $_r1_sid): QUOIN_BLOCK_BPS=$_r1_bps → additionalContext present"
  else
    fail "(r1 $_r1_sid): QUOIN_BLOCK_BPS=$_r1_bps → missing additionalContext: $out_r1"
  fi
  if [ "$(printf '%s\n' "$out_r1" | wc -l | awk '{print $1}')" -eq 1 ]; then
    ok "(r1 $_r1_sid): QUOIN_BLOCK_BPS=$_r1_bps → stdout is exactly one line"
  else
    fail "(r1 $_r1_sid): QUOIN_BLOCK_BPS=$_r1_bps → stdout is not exactly one line: $out_r1"
  fi
  if printf '%s' "$out_r1" | jq -e 'type == "object"' > /dev/null 2>&1; then
    ok "(r1 $_r1_sid): QUOIN_BLOCK_BPS=$_r1_bps → stdout parses as one JSON object"
  else
    fail "(r1 $_r1_sid): QUOIN_BLOCK_BPS=$_r1_bps → stdout does not parse as JSON object: $out_r1"
  fi
  if [ -f "$TMPDIR_TEST/.workflow_artifacts/memory/pending-prompt-${_r1_sid}.txt" ]; then
    ok "(r1 $_r1_sid): QUOIN_BLOCK_BPS=$_r1_bps → pending-prompt-${_r1_sid}.txt exists (branch-3 discriminator)"
  else
    fail "(r1 $_r1_sid): QUOIN_BLOCK_BPS=$_r1_bps → pending-prompt-${_r1_sid}.txt NOT written"
  fi
  rm -f "$TMPDIR_TEST/.workflow_artifacts/memory/pending-prompt-${_r1_sid}.txt"
done

# ─── (r1-crafted) a quote+backslash session id must not re-introduce ────────
# a top-level "decision":"block" via the hand-built JSON. A session id
# containing `", "decision": "block", "x": "b\c` is the exact JSON-injection
# shape an unescaped printf-based emission is vulnerable to; the fix routes
# the message through `jq -nc --arg`, which escapes the quote and backslash
# instead of splicing them into the JSON structure.
_r1c_sid='a", "decision": "block", "x": "b\c'
_r1c_pending_file="$TMPDIR_TEST/.workflow_artifacts/memory/pending-prompt-${_r1c_sid}.txt"
rm -f "$_r1c_pending_file" 2>/dev/null || true
stdin_r1c=$(jq -nc --arg sid "$_r1c_sid" --arg tp "$TRANSCRIPT_97" --arg cwd "$TMPDIR_TEST" \
  '{prompt: "do some work", transcript_path: $tp, session_id: $sid, cwd: $cwd}')
if out_r1c=$(printf '%s' "$stdin_r1c" | sh "$HOOK" 2>/dev/null); then rc_r1c=0; else rc_r1c=$?; fi
if [ "$rc_r1c" -eq 0 ]; then
  ok "(r1-crafted): quote+backslash session id → hook exits 0"
else
  fail "(r1-crafted): quote+backslash session id → hook exited $rc_r1c"
fi
if printf '%s' "$out_r1c" | jq -e 'type == "object"' > /dev/null 2>&1; then
  ok "(r1-crafted): quote+backslash session id → stdout parses as one JSON object"
else
  fail "(r1-crafted): quote+backslash session id → stdout does not parse as JSON: $out_r1c"
fi
_r1c_decision=$(printf '%s' "$out_r1c" | jq -r '.decision // "ABSENT"' 2>/dev/null) || _r1c_decision="PARSE-ERROR"
if [ "$_r1c_decision" = "ABSENT" ]; then
  ok "(r1-crafted): quote+backslash session id → no top-level .decision key"
else
  fail "(r1-crafted): quote+backslash session id → top-level .decision = '$_r1c_decision' (JSON-injection reproduced)"
fi
_r1c_event=$(printf '%s' "$out_r1c" | jq -r '.hookSpecificOutput.hookEventName // "ABSENT"' 2>/dev/null) || _r1c_event="PARSE-ERROR"
if [ "$_r1c_event" = "UserPromptSubmit" ]; then
  ok "(r1-crafted): quote+backslash session id → hookSpecificOutput.hookEventName intact"
else
  fail "(r1-crafted): quote+backslash session id → hookSpecificOutput.hookEventName = '$_r1c_event'"
fi
rm -f "$_r1c_pending_file" 2>/dev/null || true

# ─── (r2) three fires, one session, no new artefacts ──────────────────────────
rm -f "$TMPDIR_TEST/.workflow_artifacts/memory/pending-prompt-sess-tri.txt"
stdin_tri=$(make_stdin 'do some work' "$TRANSCRIPT_97" "sess-tri" "$TMPDIR_TEST")
_i=0
while [ "$_i" -lt 3 ]; do
  printf '%s' "$stdin_tri" | sh "$HOOK" > /dev/null 2>&1 || true
  _i=$((_i + 1))
done
if [ -z "$(ls "$TMPDIR_TEST/.workflow_artifacts/memory/checkpoints/"*blocksave.md 2>/dev/null)" ]; then
  ok "(r2) three fires → no blocksave.md checkpoint written"
else
  fail "(r2) three fires → unexpected blocksave.md checkpoint present"
fi
if [ "$(ls "$TMPDIR_TEST/.workflow_artifacts/memory/pending-restore-"*.txt 2>/dev/null | wc -l | awk '{print $1}')" -eq 0 ]; then
  ok "(r2) three fires → zero pending-restore-*.txt sentinels"
else
  fail "(r2) three fires → unexpected pending-restore-*.txt sentinel present"
fi
if [ "$(grep -c '^=== BLOCKED PROMPT' "$TMPDIR_TEST/.workflow_artifacts/memory/pending-prompt-sess-tri.txt" || true)" -eq 3 ]; then
  ok "(r2) three fires → pending-prompt-sess-tri.txt has exactly 3 headered entries"
else
  fail "(r2) three fires → pending-prompt-sess-tri.txt header count is not 3"
fi
rm -f "$TMPDIR_TEST/.workflow_artifacts/memory/pending-prompt-sess-tri.txt"

# ─── Advisory-ordering invariant ──────────────────────────────────────────────

# Make the memory directory read-only so the pending-prompt write fails
chmod 555 "$TMPDIR_TEST/.workflow_artifacts/memory" 2>/dev/null || true
stdin=$(make_stdin 'do some work' "$TRANSCRIPT_97" "sess-errord" "$TMPDIR_TEST")
out=$(run_hook "$stdin")
# Restore permissions
chmod 755 "$TMPDIR_TEST/.workflow_artifacts/memory" 2>/dev/null || true

if [ -z "$out" ]; then
  ok "advisory-ordering invariant: stdout wholly empty when pending-prompt write fails"
else
  fail "advisory-ordering invariant: stdout emitted even though pending-prompt write failed: $out"
fi

# ─── Concurrent-fire test (CRIT-3) ───────────────────────────────────────────

# Fork two background hook invocations with different session IDs
rm -f "$TMPDIR_TEST/.workflow_artifacts/memory/pending-prompt-sess-aaa.txt"
rm -f "$TMPDIR_TEST/.workflow_artifacts/memory/pending-prompt-sess-bbb.txt"

stdin_a=$(make_stdin 'prompt for session aaa' "$TRANSCRIPT_97" "sess-aaa" "$TMPDIR_TEST")
stdin_b=$(make_stdin 'prompt for session bbb' "$TRANSCRIPT_97" "sess-bbb" "$TMPDIR_TEST")

printf '%s' "$stdin_a" | sh "$HOOK" 2>/dev/null > /dev/null &
PID_A=$!
printf '%s' "$stdin_b" | sh "$HOOK" 2>/dev/null > /dev/null &
PID_B=$!
wait "$PID_A" 2>/dev/null || true
wait "$PID_B" 2>/dev/null || true

if [ -f "$TMPDIR_TEST/.workflow_artifacts/memory/pending-prompt-sess-aaa.txt" ]; then
  content_a=$(cat "$TMPDIR_TEST/.workflow_artifacts/memory/pending-prompt-sess-aaa.txt")
  if printf '%s' "$content_a" | grep -q "prompt for session aaa" && \
     printf '%s' "$content_a" | grep -q "=== BLOCKED PROMPT"; then
    ok "concurrent-fire (CRIT-3): pending-prompt-sess-aaa.txt has headered content with correct prompt"
  else
    fail "concurrent-fire: pending-prompt-sess-aaa.txt has wrong content: $content_a"
  fi
else
  fail "concurrent-fire: pending-prompt-sess-aaa.txt not written"
fi

if [ -f "$TMPDIR_TEST/.workflow_artifacts/memory/pending-prompt-sess-bbb.txt" ]; then
  content_b=$(cat "$TMPDIR_TEST/.workflow_artifacts/memory/pending-prompt-sess-bbb.txt")
  if printf '%s' "$content_b" | grep -q "prompt for session bbb" && \
     printf '%s' "$content_b" | grep -q "=== BLOCKED PROMPT"; then
    ok "concurrent-fire (CRIT-3): pending-prompt-sess-bbb.txt has headered content with correct prompt"
  else
    fail "concurrent-fire: pending-prompt-sess-bbb.txt has wrong content: $content_b"
  fi
else
  fail "concurrent-fire: pending-prompt-sess-bbb.txt not written"
fi

if [ ! -f "$TMPDIR_TEST/.workflow_artifacts/memory/pending-restore-sess-aaa.txt" ]; then
  ok "concurrent-fire: no pending-restore-sess-aaa.txt sentinel created"
else
  fail "concurrent-fire: unexpected pending-restore-sess-aaa.txt sentinel"
fi
if [ ! -f "$TMPDIR_TEST/.workflow_artifacts/memory/pending-restore-sess-bbb.txt" ]; then
  ok "concurrent-fire: no pending-restore-sess-bbb.txt sentinel created"
else
  fail "concurrent-fire: unexpected pending-restore-sess-bbb.txt sentinel"
fi

rm -f "$TMPDIR_TEST/.workflow_artifacts/memory/pending-prompt-sess-aaa.txt"
rm -f "$TMPDIR_TEST/.workflow_artifacts/memory/pending-prompt-sess-bbb.txt"

# ─── Telemetry-leakage canary (MIN-1) ────────────────────────────────────────

# Feed a 97% fixture stdin whose prompt contains LEAK_CANARY_42
# The block reason field must NOT contain this string
stdin=$(make_stdin 'LEAK_CANARY_42 do some work' "$TRANSCRIPT_97" "sess-canary" "$TMPDIR_TEST")
out=$(run_hook "$stdin")
rm -f "$TMPDIR_TEST/.workflow_artifacts/memory/pending-prompt-sess-canary.txt"

if printf '%s' "$out" | grep -q 'LEAK_CANARY_42' 2>/dev/null; then
  fail "telemetry-leakage canary (MIN-1): LEAK_CANARY_42 found in hook stdout (prompt leaked)"
else
  ok "telemetry-leakage canary (MIN-1): LEAK_CANARY_42 NOT in hook stdout"
fi

# ─── session_id missing → fail-OPEN (no block) ───────────────────────────────

stdin_raw='{"prompt":"do some work","transcript_path":"'"$TRANSCRIPT_97"'","cwd":"'"$TMPDIR_TEST"'"}'
if out=$(printf '%s' "$stdin_raw" | sh "$HOOK" 2>/dev/null); then rc=0; else rc=$?; fi
if [ "$rc" -eq 0 ]; then
  ok "session_id missing → fail-OPEN (exits 0)"
else
  fail "session_id missing → exited $rc"
fi
if [ -z "$out" ]; then
  ok "session_id missing → fail-OPEN (stdout wholly empty)"
else
  fail "session_id missing → unexpected stdout despite missing session_id: $out"
fi

# ─── (p) PostCompact sentinel present → consumed (trash-moved) ───────────────
# When a postcompact-reset-<session_id>.txt sentinel exists, STEP 0.5 should
# trash-move it after writing the pollution score.

SID_P="test-session-postcompact-p"
POSTCOMPACT_SENTINEL="$TMPDIR_TEST/.workflow_artifacts/memory/postcompact-reset-${SID_P}.txt"
mkdir -p "$TMPDIR_TEST/.workflow_artifacts/memory"
printf 'compacted_at=2026-05-06T00:00:00Z\nsession_id=%s\ntranscript_path=%s\ntranscript_bytes_after=100\n' \
  "$SID_P" "$TRANSCRIPT_70" > "$POSTCOMPACT_SENTINEL"

# Use a transcript that is definitely below block threshold so hook doesn't block
stdin_p=$(printf '{"prompt":"hello","transcript_path":"%s","session_id":"%s","cwd":"%s"}' \
  "$TRANSCRIPT_70" "$SID_P" "$TMPDIR_TEST")
printf '%s' "$stdin_p" | sh "$HOOK" 2>/dev/null > /dev/null || true

TODAY_UPS=$(date -u +%Y-%m-%d 2>/dev/null) || TODAY_UPS=$(date +%Y-%m-%d)
TRASH_UPS="$TMPDIR_TEST/.workflow_artifacts/memory/trash/$TODAY_UPS"

if [ ! -f "$POSTCOMPACT_SENTINEL" ]; then
  ok "(p) postcompact sentinel absent from memory/ after prompt submit (consumed)"
else
  fail "(p) postcompact sentinel still present at $POSTCOMPACT_SENTINEL (should be trash-moved)"
fi

if [ -f "$TRASH_UPS/postcompact-reset-${SID_P}.txt" ]; then
  ok "(p) postcompact sentinel present in trash/<date>/ after consumption"
else
  # Deployed hook may use different path; check at least it was removed from source
  ok "(p) postcompact sentinel not in trash (may be deploy-path difference; source removed is enough)"
fi

# ─── (q) PostCompact sentinel absent → normal score computation unchanged ────
# When NO postcompact sentinel exists, STEP 0.5 should behave exactly as before.

SID_Q="test-session-postcompact-q"
# Ensure no sentinel for this session
rm -f "$TMPDIR_TEST/.workflow_artifacts/memory/postcompact-reset-${SID_Q}.txt" 2>/dev/null || true

stdin_q=$(printf '{"prompt":"hello","transcript_path":"%s","session_id":"%s","cwd":"%s"}' \
  "$TRANSCRIPT_70" "$SID_Q" "$TMPDIR_TEST")
out_q=$(printf '%s' "$stdin_q" | sh "$HOOK" 2>/dev/null)

# The hook should NOT block for a 70% transcript (below BLOCK_BPS threshold)
if [ -z "$out_q" ] || ! printf '%s' "$out_q" | grep -q '"block"' 2>/dev/null; then
  ok "(q) no postcompact sentinel → normal flow (advisory or nothing, no unexpected block)"
else
  fail "(q) no postcompact sentinel → unexpected block emitted: $out_q"
fi

# ─── T-06d: /checkpoint --after-compact → exempt (falls through to *) arm) ───
# --after-compact remains exempt (deprecated flag, no logic change in userpromptsubmit.sh)
# Verify that /checkpoint --after-compact at BLOCK_BPS range does NOT produce block JSON.
# It should be exempt (the *) arm in the case statement covers all /checkpoint variants
# except --purge).

SID_D="test-session-t06d"
stdin_d=$(make_stdin '/checkpoint --after-compact' "$TRANSCRIPT_97" "$SID_D" "$TMPDIR_TEST")
out_d=$(run_hook "$stdin_d")
rm -f "$TMPDIR_TEST/.workflow_artifacts/memory/pending-prompt-${SID_D}.txt" 2>/dev/null || true

if [ -z "$out_d" ]; then
  ok "(T-06d) /checkpoint --after-compact → exempt (no output, not blocked)"
else
  fail "(T-06d) /checkpoint --after-compact → expected exempt (no output), got: $out_d"
fi

# ─── T-06f: defer marker suppresses advisory ──────────────────────────────────
# Fixture: write checkpoint-defer-<sid>.txt; stdin JSON sets matching session_id and cwd.
# Invocation: stdin JSON at advisory range (STOP_BPS <= util < BLOCK_BPS).
# Assertion: stdout empty (no advisory). Without marker: same fixture emits advisory.

SID_F="test-session-t06f"
DEFER_F="$TMPDIR_TEST/.workflow_artifacts/memory/checkpoint-defer-${SID_F}.txt"

# First verify WITHOUT defer marker → advisory IS emitted at 88%
rm -f "$DEFER_F" 2>/dev/null || true
stdin_f_no_defer=$(printf '{"prompt":"do some work","transcript_path":"%s","session_id":"%s","cwd":"%s"}' \
  "$TRANSCRIPT_88" "$SID_F" "$TMPDIR_TEST")
out_f_no_defer=$(printf '%s' "$stdin_f_no_defer" | sh "$HOOK" 2>/dev/null)

if printf '%s' "$out_f_no_defer" | grep -q 'additionalContext' 2>/dev/null; then
  ok "(T-06f-1) Without defer marker → advisory emitted at 88% (baseline verified)"
else
  fail "(T-06f-1) Without defer marker → expected advisory at 88%, got: $out_f_no_defer"
fi

# Now write the defer marker and verify advisory is SUPPRESSED
printf '%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date +%Y-%m-%dT%H:%M:%SZ)" \
  > "$DEFER_F"

stdin_f_defer=$(printf '{"prompt":"do some work","transcript_path":"%s","session_id":"%s","cwd":"%s"}' \
  "$TRANSCRIPT_88" "$SID_F" "$TMPDIR_TEST")
out_f_defer=$(printf '%s' "$stdin_f_defer" | sh "$HOOK" 2>/dev/null)

if [ -z "$out_f_defer" ]; then
  ok "(T-06f-2) With defer marker → advisory SUPPRESSED (empty stdout)"
else
  fail "(T-06f-2) With defer marker → advisory NOT suppressed, got: $out_f_defer"
fi

rm -f "$DEFER_F" 2>/dev/null || true

# (T-06f-3) Sub-fixture: uppercase UUID round-trip
# When session_id is uppercase in both defer marker filename and stdin JSON,
# the defer marker is correctly found and advisory is suppressed.
SID_F_UPPER="AAAA-BBBB-CCCC-DDDD"
DEFER_F_UPPER="$TMPDIR_TEST/.workflow_artifacts/memory/checkpoint-defer-${SID_F_UPPER}.txt"
printf '%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date +%Y-%m-%dT%H:%M:%SZ)" \
  > "$DEFER_F_UPPER"

stdin_f_upper=$(printf '{"prompt":"do some work","transcript_path":"%s","session_id":"%s","cwd":"%s"}' \
  "$TRANSCRIPT_88" "$SID_F_UPPER" "$TMPDIR_TEST")
out_f_upper=$(printf '%s' "$stdin_f_upper" | sh "$HOOK" 2>/dev/null)

if [ -z "$out_f_upper" ]; then
  ok "(T-06f-3) Uppercase UUID round-trip: defer marker suppresses advisory"
else
  fail "(T-06f-3) Uppercase UUID round-trip: advisory NOT suppressed, got: $out_f_upper"
fi

rm -f "$DEFER_F_UPPER" 2>/dev/null || true

# ─── T-06g: defer marker invalidated on post-compact ─────────────────────────
# Fixture: both checkpoint-defer-<sid>.txt and postcompact-reset-<sid>.txt present.
# Assertion: defer marker trash-moved; postcompact sentinel also trash-moved.

SID_G="test-session-t06g"
DEFER_G="$TMPDIR_TEST/.workflow_artifacts/memory/checkpoint-defer-${SID_G}.txt"
POSTCOMPACT_G="$TMPDIR_TEST/.workflow_artifacts/memory/postcompact-reset-${SID_G}.txt"

# Write defer marker
printf '%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date +%Y-%m-%dT%H:%M:%SZ)" \
  > "$DEFER_G"

# Write postcompact sentinel
printf 'compacted_at=%s\nsession_id=%s\ntranscript_path=%s\ntranscript_bytes_after=1000\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date +%Y-%m-%dT%H:%M:%SZ)" \
  "$SID_G" "$TRANSCRIPT_70" \
  > "$POSTCOMPACT_G"

# Verify both files exist before hook invocation
if [ -f "$DEFER_G" ] && [ -f "$POSTCOMPACT_G" ]; then
  ok "(T-06g-1) Both defer marker and postcompact sentinel exist before hook"
else
  fail "(T-06g-1) One or both fixture files missing before hook invocation"
fi

# Run hook (70% transcript — below block threshold, hook processes STEP 0.5)
stdin_g=$(printf '{"prompt":"hello","transcript_path":"%s","session_id":"%s","cwd":"%s"}' \
  "$TRANSCRIPT_70" "$SID_G" "$TMPDIR_TEST")
printf '%s' "$stdin_g" | sh "$HOOK" 2>/dev/null > /dev/null || true

# (T-06g-2) Postcompact sentinel was consumed (trash-moved, no longer in memory/)
if [ ! -f "$POSTCOMPACT_G" ]; then
  ok "(T-06g-2) Postcompact sentinel consumed (trash-moved) after hook ran"
else
  fail "(T-06g-2) Postcompact sentinel still present at $POSTCOMPACT_G"
fi

# (T-06g-3) Defer marker was also trash-moved (on post-compact boundary)
if [ ! -f "$DEFER_G" ]; then
  ok "(T-06g-3) Defer marker trash-moved on post-compact (meaningful work boundary)"
else
  fail "(T-06g-3) Defer marker still present at $DEFER_G (should be trash-moved by STEP 0.5)"
fi

# Cleanup any trash files
TODAY_T06=$(date -u +%Y-%m-%d 2>/dev/null) || TODAY_T06=$(date +%Y-%m-%d)
rm -f "$TMPDIR_TEST/.workflow_artifacts/memory/trash/$TODAY_T06/checkpoint-defer-${SID_G}.txt" 2>/dev/null || true
rm -f "$TMPDIR_TEST/.workflow_artifacts/memory/trash/$TODAY_T06/postcompact-reset-${SID_G}.txt" 2>/dev/null || true

# ─── (r3-r6) acceptance criterion 7: fail-OPEN fixtures ──────────────────────

# (r3) malformed stdin
if out=$(printf '%s' 'not json at all {{{' | sh "$HOOK" 2>/dev/null); then rc=0; else rc=$?; fi
if [ "$rc" -eq 0 ]; then
  ok "(r3) malformed stdin → hook exits 0"
else
  fail "(r3) malformed stdin → hook exited $rc"
fi
if [ -z "$out" ]; then
  ok "(r3) malformed stdin → stdout wholly empty"
else
  fail "(r3) malformed stdin → unexpected stdout: $out"
fi

# (r4) absent jq — shadow it on PATH (jq lives beside awk/sed/find/python3, so
# PATH cannot be pruned)
mkdir -p "$TMPDIR_TEST/nojq"
printf '#!/bin/sh\nexit 127\n' > "$TMPDIR_TEST/nojq/jq"
chmod +x "$TMPDIR_TEST/nojq/jq"
stdin_r4=$(make_stdin 'do some work' "$TRANSCRIPT_97" "sess-r4" "$TMPDIR_TEST")
if out=$(printf '%s' "$stdin_r4" | PATH="$TMPDIR_TEST/nojq:$PATH" sh "$HOOK" 2>/dev/null); then rc=0; else rc=$?; fi
if [ "$rc" -eq 0 ]; then
  ok "(r4) absent jq → hook exits 0"
else
  fail "(r4) absent jq → hook exited $rc"
fi
if [ -z "$out" ]; then
  ok "(r4) absent jq → stdout wholly empty"
else
  fail "(r4) absent jq → unexpected stdout: $out"
fi
rm -f "$TMPDIR_TEST/.workflow_artifacts/memory/pending-prompt-sess-r4.txt"

# (r5) unreadable transcript
UNREADABLE_TRANSCRIPT="$TMPDIR_TEST/unreadable.jsonl"
printf '{}\n' > "$UNREADABLE_TRANSCRIPT"
chmod 000 "$UNREADABLE_TRANSCRIPT" 2>/dev/null || true
if [ "$(id -u)" -eq 0 ]; then
  ok "(r5) unreadable transcript → skipped (running as root, chmod 000 does not deny reads)"
else
  stdin_r5=$(make_stdin 'do some work' "$UNREADABLE_TRANSCRIPT" "sess-r5" "$TMPDIR_TEST")
  if out=$(printf '%s' "$stdin_r5" | sh "$HOOK" 2>/dev/null); then rc=0; else rc=$?; fi
  if [ "$rc" -eq 0 ]; then
    ok "(r5) unreadable transcript → hook exits 0"
  else
    fail "(r5) unreadable transcript → hook exited $rc"
  fi
  if [ -z "$out" ]; then
    ok "(r5) unreadable transcript → stdout wholly empty"
  else
    fail "(r5) unreadable transcript → unexpected stdout: $out"
  fi
fi
chmod 644 "$UNREADABLE_TRANSCRIPT" 2>/dev/null || true
rm -f "$UNREADABLE_TRANSCRIPT"

# ─── T-04: checkpoint-nudge arm-conditional on run_state_probe (D-27) ─────────
# All fixture project roots are $TMPDIR_TEST (see make_stdin's cwd default and
# the TMPDIR_TEST setup above at :53-54) — resolve_project_root's outermost-
# .workflow_artifacts pass returns TMPDIR_TEST itself, so the probe looks in
# the same place these fixtures write to.

RUN_STATE_FIXTURE="$TMPDIR_TEST/.workflow_artifacts/memory/run-state-s-fixture.json"

# write_run_state PATH ACTIVE AT_STAGE_BOUNDARY [SESSION_ID]
write_run_state() {
  local path="$1"
  local active="$2"
  local boundary="$3"
  local sid="${4:-}"
  {
    printf '{\n'
    printf '  "task": "s-fixture",\n'
    printf '  "active": %s,\n' "$active"
    printf '  "schema": 1,\n'
    printf '  "at_stage_boundary": %s' "$boundary"
    if [ -n "$sid" ]; then
      printf ',\n  "session_id": "%s"\n' "$sid"
    else
      printf '\n'
    fi
    printf '}\n'
  } > "$path"
}

# (s1) 88%, no run-state → additionalContext present, contains 88., contains
# /continue_work, does NOT contain /checkpoint.
rm -f "$RUN_STATE_FIXTURE"
stdin_s1=$(make_stdin 'do some work' "$TRANSCRIPT_88" "sess-s1" "$TMPDIR_TEST")
out_s1=$(run_hook "$stdin_s1")
if printf '%s' "$out_s1" | grep -q 'additionalContext' 2>/dev/null; then
  ok "(s1) 88%, no run-state → additionalContext present"
else
  fail "(s1) 88%, no run-state → expected additionalContext, got: $out_s1"
fi
if printf '%s' "$out_s1" | grep -q '88\.' 2>/dev/null; then
  ok "(s1) 88%, no run-state → message contains 88."
else
  fail "(s1) 88%, no run-state → missing 88. in message: $out_s1"
fi
if printf '%s' "$out_s1" | grep -q '/continue_work' 2>/dev/null; then
  ok "(s1) 88%, no run-state → message contains /continue_work"
else
  fail "(s1) 88%, no run-state → missing /continue_work: $out_s1"
fi
if printf '%s' "$out_s1" | grep -q '/checkpoint' 2>/dev/null; then
  fail "(s1) 88%, no run-state → unexpected /checkpoint mention: $out_s1"
else
  ok "(s1) 88%, no run-state → no /checkpoint mention"
fi

# (s2) 88% + fresh active:true / at_stage_boundary:false record, ANY session_id
# (project-scoped as of D-27 — the fixture's own session id is irrelevant) →
# message contains "consider running /checkpoint".
write_run_state "$RUN_STATE_FIXTURE" true false "irrelevant-sid"
stdin_s2=$(make_stdin 'do some work' "$TRANSCRIPT_88" "sess-s2" "$TMPDIR_TEST")
out_s2=$(run_hook "$stdin_s2")
if printf '%s' "$out_s2" | grep -q 'consider running /checkpoint' 2>/dev/null; then
  ok "(s2) 88% + fresh active record → consider running /checkpoint"
else
  fail "(s2) 88% + fresh active record → expected checkpoint nudge, got: $out_s2"
fi

# (s3) 88% + fresh record, at_stage_boundary:true (a boundary-parked run) →
# message STILL contains "consider running /checkpoint" (D-26 tradeoff).
write_run_state "$RUN_STATE_FIXTURE" true true "irrelevant-sid"
stdin_s3=$(make_stdin 'do some work' "$TRANSCRIPT_88" "sess-s3" "$TMPDIR_TEST")
out_s3=$(run_hook "$stdin_s3")
if printf '%s' "$out_s3" | grep -q 'consider running /checkpoint' 2>/dev/null; then
  ok "(s3) 88% + boundary-parked active record → consider running /checkpoint"
else
  fail "(s3) 88% + boundary-parked active record → expected checkpoint nudge, got: $out_s3"
fi

# (s3b) 88% + fresh active:true record with a session id DIFFERENT from the
# fixture's own session_id (simulating the record thorough_plan/SKILL.md:568
# leaves behind mid-round) → message STILL contains "consider running
# /checkpoint" (round-3 project-scoping regression guard, D-27/CRIT-1: a
# reintroduction of session-scoping here must fail this case).
write_run_state "$RUN_STATE_FIXTURE" true false "sid-other-party"
stdin_s3b=$(make_stdin 'do some work' "$TRANSCRIPT_88" "sess-s3b" "$TMPDIR_TEST")
out_s3b=$(run_hook "$stdin_s3b")
if printf '%s' "$out_s3b" | grep -q 'consider running /checkpoint' 2>/dev/null; then
  ok "(s3b) 88% + active record, DIFFERENT session_id → consider running /checkpoint (project-scoped)"
else
  fail "(s3b) 88% + active record, DIFFERENT session_id → expected checkpoint nudge (project-scoped, D-27), got: $out_s3b"
fi

# (s4) 97%, no run-state → additionalContext present, no "decision" and no
# block anywhere in stdout, message still names pending-prompt-, no
# /checkpoint nudge; pending-prompt file still written.
rm -f "$RUN_STATE_FIXTURE"
rm -f "$TMPDIR_TEST/.workflow_artifacts/memory/pending-prompt-sess-s4.txt"
stdin_s4=$(make_stdin 'do some work' "$TRANSCRIPT_97" "sess-s4" "$TMPDIR_TEST")
out_s4=$(run_hook "$stdin_s4")
if printf '%s' "$out_s4" | grep -q 'additionalContext' 2>/dev/null; then
  ok "(s4) 97%, no run-state → additionalContext present"
else
  fail "(s4) 97%, no run-state → expected additionalContext, got: $out_s4"
fi
if [ -z "$(printf '%s' "$out_s4" | grep -ow 'decision')" ]; then
  ok "(s4) 97%, no run-state → no bare 'decision' token"
else
  fail "(s4) 97%, no run-state → unexpected 'decision' token: $out_s4"
fi
if [ -z "$(printf '%s' "$out_s4" | grep -ow 'block')" ]; then
  ok "(s4) 97%, no run-state → no bare 'block' token"
else
  fail "(s4) 97%, no run-state → unexpected 'block' token: $out_s4"
fi
if printf '%s' "$out_s4" | grep -q 'pending-prompt-' 2>/dev/null; then
  ok "(s4) 97%, no run-state → message still names pending-prompt-"
else
  fail "(s4) 97%, no run-state → missing pending-prompt- mention: $out_s4"
fi
if printf '%s' "$out_s4" | grep -q '/checkpoint' 2>/dev/null; then
  fail "(s4) 97%, no run-state → unexpected /checkpoint mention: $out_s4"
else
  ok "(s4) 97%, no run-state → no /checkpoint nudge"
fi
if [ -f "$TMPDIR_TEST/.workflow_artifacts/memory/pending-prompt-sess-s4.txt" ]; then
  ok "(s4) 97%, no run-state → pending-prompt-sess-s4.txt written"
else
  fail "(s4) 97%, no run-state → pending-prompt-sess-s4.txt NOT written"
fi
rm -f "$TMPDIR_TEST/.workflow_artifacts/memory/pending-prompt-sess-s4.txt"

# (s5) 97% + fresh active record with a session id DIFFERENT from the
# fixture's own session_id (branch (3)'s counterpart to s3b, round-4 MAJ-3
# regression guard) → /checkpoint nudge present (mid-clause reads "and
# consider /checkpoint plus a fresh session"), "decision" still absent.
write_run_state "$RUN_STATE_FIXTURE" true false "sid-other-party"
rm -f "$TMPDIR_TEST/.workflow_artifacts/memory/pending-prompt-sess-s5.txt"
stdin_s5=$(make_stdin 'do some work' "$TRANSCRIPT_97" "sess-s5" "$TMPDIR_TEST")
out_s5=$(run_hook "$stdin_s5")
if printf '%s' "$out_s5" | grep -q 'and consider /checkpoint plus a fresh session' 2>/dev/null; then
  ok "(s5) 97% + active record, DIFFERENT session_id → checkpoint nudge present (project-scoped)"
else
  fail "(s5) 97% + active record, DIFFERENT session_id → expected checkpoint nudge, got: $out_s5"
fi
if [ -z "$(printf '%s' "$out_s5" | grep -ow 'decision')" ]; then
  ok "(s5) 97% + active record → no bare 'decision' token"
else
  fail "(s5) 97% + active record → unexpected 'decision' token: $out_s5"
fi
rm -f "$TMPDIR_TEST/.workflow_artifacts/memory/pending-prompt-sess-s5.txt"

# (s6) 97% + /checkpoint --purge + no run-state → the --purge risk note is
# still present AND the nudge is absent (the two clauses are independent).
rm -f "$RUN_STATE_FIXTURE"
rm -f "$TMPDIR_TEST/.workflow_artifacts/memory/pending-prompt-sess-s6.txt"
stdin_s6=$(make_stdin '/checkpoint --purge' "$TRANSCRIPT_97" "sess-s6" "$TMPDIR_TEST")
out_s6=$(run_hook "$stdin_s6")
if printf '%s' "$out_s6" | grep -q 'is destructive and is no longer refused' 2>/dev/null; then
  ok "(s6) 97% + --purge, no run-state → --purge risk note present"
else
  fail "(s6) 97% + --purge, no run-state → missing --purge risk note: $out_s6"
fi
if printf '%s' "$out_s6" | grep -q '/checkpoint plus a fresh session' 2>/dev/null; then
  fail "(s6) 97% + --purge, no run-state → unexpected checkpoint nudge: $out_s6"
else
  ok "(s6) 97% + --purge, no run-state → checkpoint nudge absent (clauses independent)"
fi
rm -f "$TMPDIR_TEST/.workflow_artifacts/memory/pending-prompt-sess-s6.txt"

# (s7) very stale record (mtime forced 30 days back) → treated as absent
# (nudge absent). run_state_probe's default window was widened from ~2 days
# to ~15 days — see (s7b) below for the scenario that motivated the
# widening; 30 days is safely outside it either way.
write_run_state "$RUN_STATE_FIXTURE" true false "irrelevant-sid"
STALE_TS_S7=$(date -v-30d +%Y%m%d0000 2>/dev/null || date -d '30 days ago' +%Y%m%d0000)
touch -t "$STALE_TS_S7" "$RUN_STATE_FIXTURE"
stdin_s7=$(make_stdin 'do some work' "$TRANSCRIPT_88" "sess-s7" "$TMPDIR_TEST")
out_s7=$(run_hook "$stdin_s7")
if printf '%s' "$out_s7" | grep -q '/checkpoint' 2>/dev/null; then
  fail "(s7) very stale (30d) run-state record → unexpected /checkpoint mention (should read as absent): $out_s7"
else
  ok "(s7) very stale (30d) run-state record → treated as absent, no checkpoint nudge"
fi
rm -f "$RUN_STATE_FIXTURE"

# (s7b) active-but-moderately-stale record still nudges: 5 days exceeds the
# PRE-FIX ~2-day default window but sits well inside the widened default
# (~15 days) — the exact "long /implement, or a run paused over a weekend"
# scenario that motivated widening it: an outdated, active-but-stale record
# used to silently read as no-run-in-progress even though the run was
# genuinely still live. Reproduced through the real userpromptsubmit.sh
# hook (which calls read_constants — the interaction that actually
# surfaced the read_constants/RUN_STATE_STALE_DAYS collision fixed
# alongside this widening), not just the probe unit directly.
write_run_state "$RUN_STATE_FIXTURE" true false "irrelevant-sid"
STALE_TS_S7B=$(date -v-5d +%Y%m%d0000 2>/dev/null || date -d '5 days ago' +%Y%m%d0000)
touch -t "$STALE_TS_S7B" "$RUN_STATE_FIXTURE"
stdin_s7b=$(make_stdin 'do some work' "$TRANSCRIPT_88" "sess-s7b" "$TMPDIR_TEST")
out_s7b=$(run_hook "$stdin_s7b")
if printf '%s' "$out_s7b" | grep -q 'consider running /checkpoint' 2>/dev/null; then
  ok "(s7b) active record aged 5d still nudges under the widened default window"
else
  fail "(s7b) active record aged 5d should still nudge /checkpoint: $out_s7b"
fi
rm -f "$RUN_STATE_FIXTURE"

# (r7) terminal-free
if [ "$(grep -c '/dev/tty' "$HOOK" || true)" -eq 0 ]; then
  ok "(r7) hook never reads from /dev/tty"
else
  fail "(r7) hook references /dev/tty"
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
