#!/bin/sh
# test_sessionend_arm_cleanup.sh — behavioral fixtures for the ended-marker
# + arm-cleanup block added to hooks/sessionend.sh (IVG-280 T-08).
# Consolidated, not the architecture's full exhaustive matrix — see
# current-plan.md T-08's recorded deviation.

PASS=0; FAIL=0
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HOOKS_DIR="$SCRIPT_DIR/../../hooks"
SESSIONEND_SH="$HOOKS_DIR/sessionend.sh"

pass() { echo "PASS: $1"; PASS=$((PASS+1)); }
fail() { echo "FAIL: $1"; FAIL=$((FAIL+1)); }

echo ""
echo "Test 1: a run-state record present -> ended marker written, arm removed"
T1=$(mktemp -d)
MEM1="$T1/.workflow_artifacts/memory"
mkdir -p "$MEM1"
: > "$MEM1/run-state-demo.json"
: > "$MEM1/run-continue-arm-sid-1.txt"
STDIN1=$(printf '{"session_id":"sid-1","cwd":"%s"}' "$T1")
printf '%s' "$STDIN1" | sh "$SESSIONEND_SH" >/dev/null 2>&1
if [ -f "$MEM1/session-ended-sid-1.txt" ] && [ ! -f "$MEM1/run-continue-arm-sid-1.txt" ]; then
  pass "Test 1 — record present: ended marker written, arm removed"
else
  fail "Test 1 — expected an ended marker and a removed arm"
fi
rm -rf "$T1"

echo ""
echo "Test 2: no run-state record -> nothing created"
T2=$(mktemp -d)
MEM2="$T2/.workflow_artifacts/memory"
mkdir -p "$MEM2"
: > "$MEM2/run-continue-arm-sid-2.txt"
BEFORE=$(ls "$MEM2" | sort)
STDIN2=$(printf '{"session_id":"sid-2","cwd":"%s"}' "$T2")
printf '%s' "$STDIN2" | sh "$SESSIONEND_SH" >/dev/null 2>&1
AFTER=$(ls "$MEM2" | sort)
if [ "$BEFORE" = "$AFTER" ]; then
  pass "Test 2 — no record: memory dir untouched"
else
  fail "Test 2 — expected no change, before=[$BEFORE] after=[$AFTER]"
fi
rm -rf "$T2"

echo ""
echo "Test 3: no memory dir -> hook exits 0, nothing created"
T3=$(mktemp -d)
STDIN3=$(printf '{"session_id":"sid-3","cwd":"%s"}' "$T3")
printf '%s' "$STDIN3" | sh "$SESSIONEND_SH" >/dev/null 2>&1
RC3=$?
if [ "$RC3" -eq 0 ] && [ ! -d "$T3/.workflow_artifacts" ]; then
  pass "Test 3 — no memory dir: exit 0, nothing created"
else
  fail "Test 3 — expected exit 0 and no created directory, rc=$RC3"
fi
rm -rf "$T3"

echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] || exit 1
