#!/bin/sh
# test_userpromptsubmit_hook.sh - contract tests for quoin/hooks/userpromptsubmit.sh
#
# The hook only maintains run-continuation sentinels: it prints nothing, never
# opens the transcript, and a typed prompt removes the session's arm file.
# test_userpromptsubmit_residual.py covers the same contract in more depth;
# this fixture repeats the key checks so shell-fixture test selection covers it.
#
# Usage: sh quoin/dev/tests/test_userpromptsubmit_hook.sh

PASS=0
FAIL=0

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
FIXTURES_DIR="$SCRIPT_DIR/fixtures/hooks"
HOOK="$SCRIPT_DIR/../../hooks/userpromptsubmit.sh"

ok() { PASS=$((PASS + 1)); printf 'ok  %s\n' "$1"; }
fail() { FAIL=$((FAIL + 1)); printf 'FAIL %s\n' "$1" >&2; }

TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT
export HOME="$TMP_ROOT/home"
mkdir -p "$HOME"

PROJ="$TMP_ROOT/home/proj"
MEM="$PROJ/.workflow_artifacts/memory"
mkdir -p "$MEM"
SID="sid-1"
ARM="$MEM/run-continue-arm-$SID.txt"

payload() {
  # $1 prompt, $2 transcript path
  printf '{"prompt":"%s","transcript_path":"%s","session_id":"%s","cwd":"%s"}' \
    "$1" "$2" "$SID" "$PROJ"
}

run_hook() {
  printf '%s' "$1" | sh "$HOOK" 2>/dev/null
}

expect_silent_ok() {
  # $1 label, $2 payload
  out="$(run_hook "$2")"
  rc=$?
  if [ "$rc" -eq 0 ] && [ -z "$out" ]; then ok "$1"; else fail "$1 (rc=$rc out=[$out])"; fi
}

# shebang
if head -1 "$HOOK" | grep -qE '^#!/bin/sh( |$)'; then ok "shebang is #!/bin/sh"; else fail "shebang"; fi

# empty and garbage stdin
out="$(printf '' | sh "$HOOK" 2>/dev/null)"; rc=$?
if [ "$rc" -eq 0 ] && [ -z "$out" ]; then ok "empty stdin: exit 0, no stdout"; else fail "empty stdin"; fi
out="$(printf 'not json at all' | sh "$HOOK" 2>/dev/null)"; rc=$?
if [ "$rc" -eq 0 ] && [ -z "$out" ]; then ok "garbage stdin: exit 0, no stdout"; else fail "garbage stdin"; fi

# no stdout for plain and slash-command prompts
for p in "hello" "/compact" "/clear" "/help" "/checkpoint" "/checkpoint --purge"; do
  expect_silent_ok "no stdout for prompt: $p" "$(payload "$p" /nonexistent/t.jsonl)"
done

# high-fill transcript stays silent
expect_silent_ok "no stdout on a 97% transcript" "$(payload "hello" "$FIXTURES_DIR/transcript_97pct.jsonl")"
if ls "$MEM"/pending-prompt-* >/dev/null 2>&1; then fail "no pending-prompt file written"; else ok "no pending-prompt file written"; fi

# missing transcript
expect_silent_ok "missing transcript path" "$(payload "hello" /does/not/exist.jsonl)"

# unreadable transcript still disarms
LOCKED="$TMP_ROOT/locked.jsonl"
: > "$LOCKED"
chmod 000 "$LOCKED"
: > "$ARM"
run_hook "$(payload "hello" "$LOCKED")" >/dev/null
if [ -e "$ARM" ]; then
  if [ -r "$LOCKED" ]; then ok "chmod 000 transcript (skipped: file is readable by this user)"; else fail "chmod 000 transcript did not disarm"; fi
else
  ok "chmod 000 transcript still disarms"
fi
chmod 600 "$LOCKED"

# a plain prompt removes the arm file
: > "$ARM"
run_hook "$(payload "hello" /nonexistent/t.jsonl)" >/dev/null
if [ -e "$ARM" ]; then fail "plain prompt removes the arm file"; else ok "plain prompt removes the arm file"; fi

# a task notification keeps it
: > "$ARM"
run_hook "$(payload "<task-notification> done" /nonexistent/t.jsonl)" >/dev/null
if [ -e "$ARM" ]; then ok "task-notification prompt keeps the arm file"; else fail "task-notification prompt keeps the arm file"; fi

# never touches the transcript: a directory in its place is harmless
mkdir -p "$TMP_ROOT/dirtranscript"
expect_silent_ok "directory as transcript path" "$(payload "hello" "$TMP_ROOT/dirtranscript")"

printf '\nPASS: %s  FAIL: %s\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] && { printf 'PASS: all %s tests passed\n' "$PASS"; exit 0; }
exit 1
