#!/bin/sh
# test_auto_resume_sessionstart_start.sh — behavioral fixtures for the
# run-continuation hand-off block added to hooks/sessionstart.sh (IVG-280
# T-08). Consolidated, not the architecture's full exhaustive matrix — see
# current-plan.md T-08's recorded deviation.

PASS=0; FAIL=0
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HOOKS_DIR="$SCRIPT_DIR/../../hooks"
SESSIONSTART_SH="$HOOKS_DIR/sessionstart.sh"

# SAFETY: never let a test case resolve the machine's REAL installed `quoin`
# CLI (e.g. ~/.local/bin/quoin) — a successful hand-off spawns a detached,
# start_new_session=True subprocess, and the real binary would try to drive
# a real headless claude session. Every hook invocation below pins PATH to
# this dir (python3 only, plus core POSIX utilities) unless a test
# explicitly prepends its own stub.
_PY3_DIR="$(dirname "$(command -v python3)")"
SAFE_PATH="$_PY3_DIR:/usr/bin:/bin"

pass() { echo "PASS: $1"; PASS=$((PASS+1)); }
fail() { echo "FAIL: $1"; FAIL=$((FAIL+1)); }

_now_iso() { date -u +%Y-%m-%dT%H:%M:%S+00:00; }

_write_record() {
  # $1 = memory dir, $2 = task, $3 = owner session_id
  mem="$1"; task="$2"; owner_sid="$3"
  mkdir -p "$mem"
  printf 'task: %s\ntimestamp: %s\nautonomous: true\n' "$task" "$(_now_iso)" \
    > "$mem/autonomous-run-$task.marker"
  cat > "$mem/run-state-$task.json" <<EOF
{"schema": 1, "task": "$task", "session_id": "$owner_sid", "active": true,
 "phase": "implement", "phase_index": 3, "subphase": "", "step": "",
 "at_stage_boundary": false, "route": "", "profile": "", "artifacts": [],
 "next_action": "", "resume_command": "/run --resume $task",
 "notes_path": "$mem/run-notes-$task.md", "updated_at": "$(_now_iso)"}
EOF
}

echo ""
echo "Test 1: source=clear -> hand-off block contributes nothing"
# Note: sessionstart.sh's pre-existing S-4/S-5 banner logic is NOT gated on
# source and may still print its own (unrelated) advisory for a fresh cwd —
# this test only pins that the run-continuation block itself stays silent.
T1=$(mktemp -d)
MEM1="$T1/.workflow_artifacts/memory"
_write_record "$MEM1" "demo" "sid-owner"
STDIN1=$(printf '{"source":"clear","session_id":"sid-new","cwd":"%s"}' "$T1")
OUT1=$(cd "$T1" && PATH="$SAFE_PATH" sh -c "printf '%s' \"\$1\" | sh \"$SESSIONSTART_SH\"" _ "$STDIN1")
if ! printf '%s' "$OUT1" | grep -q "quoin-auto-resume\|hand-off\|NO_CLI"; then
  pass "Test 1 — source=clear: hand-off block contributes nothing"
else
  fail "Test 1 — expected no hand-off-related output on source=clear, got: $OUT1"
fi
rm -rf "$T1"

echo ""
echo "Test 2: no run-state file -> hand-off block contributes nothing"
T2=$(mktemp -d)
mkdir -p "$T2/.workflow_artifacts/memory"
STDIN2=$(printf '{"source":"startup","session_id":"sid-new","cwd":"%s"}' "$T2")
OUT2=$(cd "$T2" && PATH="$SAFE_PATH" sh -c "printf '%s' \"\$1\" | sh \"$SESSIONSTART_SH\"" _ "$STDIN2")
if [ -z "$OUT2" ]; then
  pass "Test 2 — no run-state file: silent"
else
  fail "Test 2 — expected silent with no run-state file, got: $OUT2"
fi
rm -rf "$T2"

echo ""
echo "Test 3: owner unknown (no ended marker, no transcript) -> no hand-off attempted, silent"
T3=$(mktemp -d)
MEM3="$T3/.workflow_artifacts/memory"
_write_record "$MEM3" "demo" "sid-owner"
FAKE_HOME3=$(mktemp -d)
mkdir -p "$FAKE_HOME3/.claude/projects"
STDIN3=$(printf '{"source":"startup","session_id":"sid-new","cwd":"%s"}' "$T3")
OUT3=$(cd "$T3" && HOME="$FAKE_HOME3" PATH="$SAFE_PATH" sh -c "printf '%s' \"\$1\" | sh \"$SESSIONSTART_SH\"" _ "$STDIN3")
if [ -z "$OUT3" ]; then
  pass "Test 3 — owner unknown: no hand-off attempted, silent"
else
  fail "Test 3 — expected silent, got: $OUT3"
fi
rm -rf "$T3" "$FAKE_HOME3"

# NOTE (deviation, recorded for /review): the architecture's list also names
# an "owner gone + quoin on PATH -> hand-off attempted, stub invoked" case
# and a "no quoin on PATH -> NO_CLI advisory" case. Both were attempted here
# and dropped: on this development machine a real `quoin` CLI is installed
# at ~/.local/bin/quoin (the deployed toolkit another live session may
# depend on), and a PATH-prepended stub did not reliably shadow it inside
# the nested python3-under-pyenv-shim subprocess this hook spawns — a real
# detached hand-off could fire against a real project root. Rather than
# risk that non-determinism, both cases are left to the already-safe
# (mocked Popen, no real subprocess) coverage in
# test_auto_resume_core.py::test_handoff_startup_refuses_live_owner and its
# neighbors. The two safe cases below (no CLI reachable + opt-out) still run
# for real, since neither one ever reaches Popen.

echo ""
echo "Test 4: opt-out (QUOIN_AUTO_RESUME=0) -> silent even with owner gone"
T4=$(mktemp -d)
MEM4="$T4/.workflow_artifacts/memory"
_write_record "$MEM4" "demo" "sid-owner"
: > "$MEM4/session-ended-sid-owner.txt"
FAKE_HOME4=$(mktemp -d)
mkdir -p "$FAKE_HOME4/.claude/projects"
STDIN4=$(printf '{"source":"startup","session_id":"sid-new","cwd":"%s"}' "$T4")
OUT4=$(cd "$T4" && HOME="$FAKE_HOME4" PATH="$SAFE_PATH" QUOIN_AUTO_RESUME=0 sh -c "printf '%s' \"\$1\" | sh \"$SESSIONSTART_SH\"" _ "$STDIN4")
if [ -z "$OUT4" ]; then
  pass "Test 4 — opt-out: silent"
else
  fail "Test 4 — expected silent under opt-out, got: $OUT4"
fi
rm -rf "$T4" "$FAKE_HOME4"

echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] || exit 1
