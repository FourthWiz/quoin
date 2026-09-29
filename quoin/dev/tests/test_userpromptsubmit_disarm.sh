#!/bin/sh
# test_userpromptsubmit_disarm.sh — behavioral fixtures for the disarm +
# consent-stamp block added to hooks/userpromptsubmit.sh (IVG-280 T-08).
# Consolidated, not the architecture's full exhaustive matrix — see
# current-plan.md T-08's recorded deviation.

PASS=0; FAIL=0
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HOOKS_DIR="$SCRIPT_DIR/../../hooks"
UPS_SH="$HOOKS_DIR/userpromptsubmit.sh"

pass() { echo "PASS: $1"; PASS=$((PASS+1)); }
fail() { echo "FAIL: $1"; FAIL=$((FAIL+1)); }

_arm_path() { printf '%s/run-continue-arm-%s.txt' "$1" "$2"; }
_consent_path() { printf '%s/run-continue-consent-%s.txt' "$1" "$2"; }

echo ""
echo "Test 1: a plain human prompt disarms"
T1=$(mktemp -d)
MEM1="$T1/.workflow_artifacts/memory"
mkdir -p "$MEM1"
: > "$(_arm_path "$MEM1" sid-1)"
STDIN1=$(printf '{"session_id":"sid-1","cwd":"%s","transcript_path":"%s/sid-1.jsonl","prompt":"hold on a sec"}' "$T1" "$T1")
printf '%s' "$STDIN1" | sh "$UPS_SH" >/dev/null 2>&1
if [ ! -f "$(_arm_path "$MEM1" sid-1)" ]; then
  pass "Test 1 — plain prompt: arm removed"
else
  fail "Test 1 — arm should have been removed"
fi
rm -rf "$T1"

echo ""
echo "Test 2: a <task-notification> prompt does NOT disarm (D-26 seed)"
T2=$(mktemp -d)
MEM2="$T2/.workflow_artifacts/memory"
mkdir -p "$MEM2"
: > "$(_arm_path "$MEM2" sid-2)"
STDIN2=$(printf '{"session_id":"sid-2","cwd":"%s","transcript_path":"%s/sid-2.jsonl","prompt":"<task-notification> something happened"}' "$T2" "$T2")
printf '%s' "$STDIN2" | sh "$UPS_SH" >/dev/null 2>&1
if [ -f "$(_arm_path "$MEM2" sid-2)" ]; then
  pass "Test 2 — task-notification: arm kept"
else
  fail "Test 2 — arm should NOT have been removed"
fi
rm -rf "$T2"

echo ""
echo "Test 3: a prompt whose transcript_path is under /subagents/ does NOT disarm"
T3=$(mktemp -d)
MEM3="$T3/.workflow_artifacts/memory"
mkdir -p "$T3/xsid/subagents" "$MEM3"
: > "$(_arm_path "$MEM3" sid-3)"
STDIN3=$(printf '{"session_id":"sid-3","cwd":"%s","transcript_path":"%s/xsid/subagents/agent-1.jsonl","prompt":"hold on"}' "$T3" "$T3")
printf '%s' "$STDIN3" | sh "$UPS_SH" >/dev/null 2>&1
if [ -f "$(_arm_path "$MEM3" sid-3)" ]; then
  pass "Test 3 — subagent transcript: arm kept"
else
  fail "Test 3 — arm should NOT have been removed for a subagent transcript"
fi
rm -rf "$T3"

echo ""
echo "Test 4: /run --resume <task> with a matching run-state record -> consent stamp written"
T4=$(mktemp -d)
MEM4="$T4/.workflow_artifacts/memory"
mkdir -p "$MEM4"
: > "$(_arm_path "$MEM4" sid-4)"
: > "$MEM4/run-state-demo.json"
STDIN4=$(printf '{"session_id":"sid-4","cwd":"%s","transcript_path":"%s/sid-4.jsonl","prompt":"/run --resume demo"}' "$T4" "$T4")
printf '%s' "$STDIN4" | sh "$UPS_SH" >/dev/null 2>&1
if [ -f "$(_consent_path "$MEM4" sid-4)" ] && [ ! -f "$(_arm_path "$MEM4" sid-4)" ]; then
  pass "Test 4 — /run --resume with a record: consent stamped, arm still removed"
else
  fail "Test 4 — expected a consent stamp and a removed arm"
fi
rm -rf "$T4"

echo ""
echo "Test 5: a /run prompt with NO run-state record -> no consent stamp"
T5=$(mktemp -d)
MEM5="$T5/.workflow_artifacts/memory"
mkdir -p "$MEM5"
: > "$(_arm_path "$MEM5" sid-5)"
STDIN5=$(printf '{"session_id":"sid-5","cwd":"%s","transcript_path":"%s/sid-5.jsonl","prompt":"/run demo"}' "$T5" "$T5")
printf '%s' "$STDIN5" | sh "$UPS_SH" >/dev/null 2>&1
if [ ! -f "$(_consent_path "$MEM5" sid-5)" ]; then
  pass "Test 5 — /run with no record: no consent stamp"
else
  fail "Test 5 — no consent stamp should have been written"
fi
rm -rf "$T5"

echo ""
echo "Test 6: a non-/run human prompt with a record present -> no consent stamp"
T6=$(mktemp -d)
MEM6="$T6/.workflow_artifacts/memory"
mkdir -p "$MEM6"
: > "$(_arm_path "$MEM6" sid-6)"
: > "$MEM6/run-state-demo.json"
STDIN6=$(printf '{"session_id":"sid-6","cwd":"%s","transcript_path":"%s/sid-6.jsonl","prompt":"please help with something else"}' "$T6" "$T6")
printf '%s' "$STDIN6" | sh "$UPS_SH" >/dev/null 2>&1
if [ ! -f "$(_consent_path "$MEM6" sid-6)" ] && [ ! -f "$(_arm_path "$MEM6" sid-6)" ]; then
  pass "Test 6 — plain prompt with a record present: no stamp, arm still removed"
else
  fail "Test 6 — expected no stamp and a removed arm"
fi
rm -rf "$T6"

echo ""
echo "Test 7: no memory dir -> hook still exits 0"
T7=$(mktemp -d)
STDIN7=$(printf '{"session_id":"sid-7","cwd":"%s","prompt":"hi"}' "$T7")
printf '%s' "$STDIN7" | sh "$UPS_SH" >/dev/null 2>&1
RC7=$?
if [ "$RC7" -eq 0 ]; then
  pass "Test 7 — no memory dir: exit 0"
else
  fail "Test 7 — expected exit 0, got rc=$RC7"
fi
rm -rf "$T7"

echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] || exit 1
