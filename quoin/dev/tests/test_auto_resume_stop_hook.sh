#!/bin/sh
# test_auto_resume_stop_hook.sh — behavioral fixtures for hooks/stop.sh
# (IVG-280 T-08). Consolidated, not the architecture's full exhaustive
# matrix — see current-plan.md T-08's recorded deviation. All tests exit 0
# on PASS, emit "FAIL: <reason>" and increment the FAIL counter.

PASS=0; FAIL=0
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HOOKS_DIR="$SCRIPT_DIR/../../hooks"
STOP_SH="$HOOKS_DIR/stop.sh"

pass() { echo "PASS: $1"; PASS=$((PASS+1)); }
fail() { echo "FAIL: $1"; FAIL=$((FAIL+1)); }

_now_iso() { date -u +%Y-%m-%dT%H:%M:%S+00:00; }

_write_fixture() {
  # $1 = memory dir, $2 = task, $3 = session_id
  mem="$1"; task="$2"; sid="$3"
  mkdir -p "$mem"
  printf 'task: %s\ntimestamp: %s\nautonomous: true\n' "$task" "$(_now_iso)" \
    > "$mem/autonomous-run-$task.marker"
  cat > "$mem/run-state-$task.json" <<EOF
{"schema": 1, "task": "$task", "session_id": "$sid", "active": true,
 "phase": "implement", "phase_index": 3, "subphase": "", "step": "",
 "at_stage_boundary": false, "route": "", "profile": "", "artifacts": [],
 "next_action": "", "resume_command": "/run --resume $task",
 "notes_path": "$mem/run-notes-$task.md", "updated_at": "$(_now_iso)"}
EOF
  : > "$mem/run-continue-arm-$sid.txt"
}

echo ""
echo "Test 1: no memory dir -> empty stdout, exit 0"
T1=$(mktemp -d)
OUT=$(cd "$T1" && sh "$STOP_SH" < /dev/null); RC=$?
if [ "$RC" -eq 0 ] && [ -z "$OUT" ]; then
  pass "Test 1 — no memory dir: silent, exit 0"
else
  fail "Test 1 — expected silent exit 0, got rc=$RC out=[$OUT]"
fi
rm -rf "$T1"

echo ""
echo "Test 2: memory dir present, no arm file -> empty stdout, exit 0"
T2=$(mktemp -d)
mkdir -p "$T2/.workflow_artifacts/memory"
STDIN2=$(printf '{"session_id":"sid-2","cwd":"%s"}' "$T2")
OUT2=$(cd "$T2" && printf '%s' "$STDIN2" | sh "$STOP_SH"); RC2=$?
if [ "$RC2" -eq 0 ] && [ -z "$OUT2" ]; then
  pass "Test 2 — no arm file: silent, exit 0"
else
  fail "Test 2 — expected silent exit 0, got rc=$RC2 out=[$OUT2]"
fi
rm -rf "$T2"

echo ""
echo "Test 3: armed + fresh record + matching sid -> one block object"
T3=$(mktemp -d)
MEM3="$T3/.workflow_artifacts/memory"
_write_fixture "$MEM3" "demo" "sid-3"
STDIN3=$(printf '{"session_id":"sid-3","cwd":"%s","transcript_path":"%s/sid-3.jsonl","background_tasks":[]}' "$T3" "$T3")
OUT3=$(cd "$T3" && printf '%s' "$STDIN3" | sh "$STOP_SH")
if printf '%s' "$OUT3" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["decision"]=="block"; assert "/run --resume demo" in d["reason"]' 2>/dev/null; then
  pass "Test 3 — armed session: one block object with the resume command in reason"
else
  fail "Test 3 — expected a block object, got: $OUT3"
fi
if [ ! -f "$MEM3/autonomous-halt-demo.md" ]; then
  pass "Test 3b — no halt file written on a plain in-session block"
else
  fail "Test 3b — a halt file should not exist yet"
fi
rm -rf "$T3"

echo ""
echo "Test 4: counter already at cap -> halt file written, empty stdout (I-13)"
T4=$(mktemp -d)
MEM4="$T4/.workflow_artifacts/memory"
_write_fixture "$MEM4" "demo" "sid-4"
cat > "$MEM4/auto-resume-demo.json" <<EOF
{"schema": 1, "task": "demo", "marker_timestamp": "$(_now_iso)", "attempts": 10,
 "consecutive_no_progress": 0, "last_done_count": 0, "last_phase": null,
 "in_flight": false, "chain_blocks": 0, "last_reason": "", "last_session_id": ""}
EOF
STDIN4=$(printf '{"session_id":"sid-4","cwd":"%s","transcript_path":"%s/sid-4.jsonl","background_tasks":[]}' "$T4" "$T4")
OUT4=$(cd "$T4" && printf '%s' "$STDIN4" | sh "$STOP_SH")
if [ -z "$OUT4" ] && [ -f "$MEM4/autonomous-halt-demo.md" ] && grep -q "reason: auto-resume cap" "$MEM4/autonomous-halt-demo.md"; then
  pass "Test 4 — at cap: empty stdout, halt file with the cap reason"
else
  fail "Test 4 — expected empty stdout + a cap halt file, got out=[$OUT4]"
fi
rm -rf "$T4"

echo ""
echo "Test 5: background_tasks non-empty -> silent, no counter mutation (D-23)"
T5=$(mktemp -d)
MEM5="$T5/.workflow_artifacts/memory"
_write_fixture "$MEM5" "demo" "sid-5"
BEFORE=$(ls "$MEM5" | sort)
STDIN5=$(printf '{"session_id":"sid-5","cwd":"%s","transcript_path":"%s/sid-5.jsonl","background_tasks":["x"]}' "$T5" "$T5")
OUT5=$(cd "$T5" && printf '%s' "$STDIN5" | sh "$STOP_SH")
AFTER=$(ls "$MEM5" | sort)
if [ -z "$OUT5" ] && [ "$BEFORE" = "$AFTER" ]; then
  pass "Test 5 — background_tasks present: silent, no new files"
else
  fail "Test 5 — expected silent no-op, got out=[$OUT5] before=[$BEFORE] after=[$AFTER]"
fi
rm -rf "$T5"

echo ""
echo "Test 6: opt-out (QUOIN_AUTO_RESUME=0) -> silent even when armed"
T6=$(mktemp -d)
MEM6="$T6/.workflow_artifacts/memory"
_write_fixture "$MEM6" "demo" "sid-6"
STDIN6=$(printf '{"session_id":"sid-6","cwd":"%s","transcript_path":"%s/sid-6.jsonl","background_tasks":[]}' "$T6" "$T6")
OUT6=$(cd "$T6" && QUOIN_AUTO_RESUME=0 sh -c "printf '%s' \"\$1\" | sh \"$STOP_SH\"" _ "$STDIN6")
if [ -z "$OUT6" ]; then
  pass "Test 6 — opt-out: silent"
else
  fail "Test 6 — expected silent under opt-out, got: $OUT6"
fi
rm -rf "$T6"

echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] || exit 1
