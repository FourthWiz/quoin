#!/bin/sh
# test_recent_sessions_hook.sh - Shell tests for the recent-sessions.md appends
# done by sessionstart.sh (startup, resume), sessionend.sh and precompact.sh.
#
# Usage: sh quoin/dev/tests/test_recent_sessions_hook.sh
# Returns 0 on success, non-zero on failure.
#
# Runs the repo hooks (not the deployed copies) with HOME pinned to a temp dir.

PASS=0
FAIL=0
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HOOKS="$SCRIPT_DIR/../../hooks"
HOOK_START="$HOOKS/sessionstart.sh"
HOOK_END="$HOOKS/sessionend.sh"
HOOK_PRE="$HOOKS/precompact.sh"

pass() { printf 'PASS: %s\n' "$1"; PASS=$((PASS + 1)); }
fail() { printf 'FAIL: %s\n' "$1"; FAIL=$((FAIL + 1)); }

ROOT="$(mktemp -d)"
trap 'rm -rf "$ROOT"' EXIT
export HOME="$ROOT/home"
mkdir -p "$HOME"

new_project() {
  d="$ROOT/proj-$1"
  mkdir -p "$d/.workflow_artifacts/memory"
  printf '%s' "$d"
}

LINE_RE='^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z \| '

start() { # $1 project  $2 source  $3 session id
  printf '{"source":"%s","session_id":"%s","cwd":"%s"}' "$2" "$3" "$1" | sh "$HOOK_START" 2>/dev/null
}

# Test 1: startup appends one line in the expected format
P=$(new_project 1)
start "$P" startup sid-t1 >/dev/null
RS="$P/.workflow_artifacts/memory/recent-sessions.md"
if [ -f "$RS" ] && [ "$(wc -l < "$RS" | tr -d ' ')" = "1" ] && grep -qE "${LINE_RE}sid-t1\$" "$RS"; then
  pass "Test 1: startup appends one correctly formatted line"
else
  fail "Test 1: startup did not append one formatted line ($(cat "$RS" 2>/dev/null))"
fi

# Test 2: resume appends
P=$(new_project 2)
start "$P" resume sid-t2 >/dev/null
if grep -qE "${LINE_RE}sid-t2\$" "$P/.workflow_artifacts/memory/recent-sessions.md" 2>/dev/null; then
  pass "Test 2: resume appends a line"
else
  fail "Test 2: resume did not append"
fi

# Test 3: compact appends nothing
P=$(new_project 3)
start "$P" compact sid-t3 >/dev/null
if [ -e "$P/.workflow_artifacts/memory/recent-sessions.md" ]; then
  fail "Test 3: compact start wrote recent-sessions.md"
else
  pass "Test 3: compact start appends nothing"
fi

# Test 4: sessionend appends
P=$(new_project 4)
printf '{"session_id":"sid-t4","cwd":"%s"}' "$P" | sh "$HOOK_END" >/dev/null 2>&1
if grep -qE "${LINE_RE}sid-t4\$" "$P/.workflow_artifacts/memory/recent-sessions.md" 2>/dev/null; then
  pass "Test 4: sessionend appends a line"
else
  fail "Test 4: sessionend did not append"
fi

# Test 5: no memory dir -> no file and no directory created
P="$ROOT/proj-5"
mkdir -p "$P"
start "$P" startup sid-t5 >/dev/null
printf '{"session_id":"sid-t5","cwd":"%s"}' "$P" | sh "$HOOK_END" >/dev/null 2>&1
if [ -e "$P/.workflow_artifacts" ]; then
  fail "Test 5: a hook created .workflow_artifacts in a bare project"
else
  pass "Test 5: no memory dir -> nothing created"
fi

# Test 6: empty session id -> no append
P=$(new_project 6)
printf '{"source":"startup","cwd":"%s"}' "$P" | sh "$HOOK_START" >/dev/null 2>&1
printf '{"cwd":"%s"}' "$P" | sh "$HOOK_END" >/dev/null 2>&1
if [ -e "$P/.workflow_artifacts/memory/recent-sessions.md" ]; then
  fail "Test 6: a line was appended without a session id"
else
  pass "Test 6: missing session id appends nothing"
fi

# Test 7: the append does not change sessionstart stdout
P=$(new_project 7)
OUT_A=$(start "$P" startup sid-t7)
RS="$P/.workflow_artifacts/memory/recent-sessions.md"
rm -f "$RS"
OUT_B=$(start "$P" startup sid-t7)
printf '2026-01-01T00:00:00Z | older\n' > "$RS"
OUT_C=$(start "$P" startup sid-t7)
if [ "$OUT_A" = "$OUT_B" ] && [ "$OUT_B" = "$OUT_C" ]; then
  pass "Test 7: sessionstart stdout is the same with and without a history file"
else
  fail "Test 7: sessionstart stdout changed with the history file"
fi

# Test 8: existing entries are kept
P=$(new_project 8)
printf '2026-01-01T00:00:00Z | other-uuid-existing\n' > "$P/.workflow_artifacts/memory/recent-sessions.md"
start "$P" startup sid-t8 >/dev/null
RS="$P/.workflow_artifacts/memory/recent-sessions.md"
if grep -q other-uuid-existing "$RS" && grep -q sid-t8 "$RS"; then
  pass "Test 8: existing and new entries both present"
else
  fail "Test 8: history file lost an entry"
fi

# Test 9: precompact still appends (and creates the memory dir)
P="$ROOT/proj-9"
mkdir -p "$P"
printf '{"trigger":"auto","session_id":"pre-uuid-t9","cwd":"%s","transcript_path":"/dev/null"}' "$P" | sh "$HOOK_PRE" >/dev/null 2>&1
if grep -q pre-uuid-t9 "$P/.workflow_artifacts/memory/recent-sessions.md" 2>/dev/null; then
  pass "Test 9: precompact appended the session id"
else
  fail "Test 9: precompact did not append"
fi

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
