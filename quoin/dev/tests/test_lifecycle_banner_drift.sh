#!/bin/sh
# test_lifecycle_banner_drift.sh — Drift test for lifecycle banner wording
#
# Purpose: asserts no banner surface still says '/end_of_day before' —
#   replaced with same-day and cross-day wrap-up wording.
#
# Invocation (from project root):
#   bash quoin/dev/tests/test_lifecycle_banner_drift.sh
#
# Note: the search-target list is fixed at four paths and must stay aligned
#   with the architecture's Stage 3 R-01 mitigation. Do NOT add or remove
#   targets without updating the architecture.md acceptance spec.

PASS=0; FAIL=0
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$SCRIPT_DIR/../../.."

pass() { echo "PASS: $1"; PASS=$((PASS+1)); }
fail() { echo "FAIL: $1"; FAIL=$((FAIL+1)); }

TMPFILE=$(mktemp 2>/dev/null) || TMPFILE="${TMPDIR:-/tmp}/quoin-drift-tmp-$$"
trap 'rm -f "$TMPFILE"' EXIT

# ── Test 1: no banner surface says '/end_of_day before' ──────────────────────
echo ""
echo "Test 1: no banner surface contains '/end_of_day before'"

grep -rn '/end_of_day before' \
  "$REPO_ROOT/quoin/hooks/" \
  "$REPO_ROOT/quoin/skills/start_of_day/SKILL.md" \
  "$REPO_ROOT/quoin/CLAUDE.md" \
  > "$TMPFILE" 2>/dev/null
GREP_EXIT=$?
LINE_COUNT=$(wc -l < "$TMPFILE" | tr -d ' ')

if [ "$GREP_EXIT" -eq 1 ] && [ "$LINE_COUNT" -eq 0 ]; then
  pass "Test 1 — no banner surface contains '/end_of_day before'"
else
  fail "Test 1 — '/end_of_day before' still present in one or more banner surfaces (exit=$GREP_EXIT, lines=$LINE_COUNT):"
  cat "$TMPFILE"
fi

# ── Test 2: same-day flagged session -> wrap-up reminder with the run resume command ──
# ── Test 3 (T-04): cross-day flagged session -> /end_of_day recommendation ──
# Fixture-driven: invoke the real sessionstart.sh and sessionend.sh hooks with
# a session file dated today vs. dated yesterday, and assert the emitted
# banner names the correct command for each case.

echo ""
echo "Test 2/3: sessionstart.sh / sessionend.sh same-day vs cross-day banner wording"

SESSIONSTART_HOOK="$REPO_ROOT/quoin/hooks/sessionstart.sh"
SESSIONEND_HOOK="$REPO_ROOT/quoin/hooks/sessionend.sh"

if [ -f "$SESSIONSTART_HOOK" ] && [ -f "$SESSIONEND_HOOK" ] && command -v python3 >/dev/null 2>&1; then
  T04_TMPDIR="${TMPDIR:-/tmp}/quoin-t04-drift-$$"
  T04_SESSIONS="$T04_TMPDIR/.workflow_artifacts/memory/sessions"
  mkdir -p "$T04_SESSIONS"

  # LOCAL date (not -u): matches sessionstart.sh's TODAY_NUM / sessionend.sh's
  # pre-existing `today` (both use local `date`, per the T-04 fix — see sessionstart.sh).
  TODAY_T04=$(date +%Y-%m-%d)
  YESTERDAY_T04=$(python3 -c "from datetime import date, timedelta; print(date.today()-timedelta(days=1))" 2>/dev/null || echo "")

  make_t04_stdin() {
    printf '{"source":"startup","session_id":"%s","cwd":"%s"}' "$1" "$T04_TMPDIR"
  }

  # T-04's sessionstart.sh S-4 banner has a 300s dedup sentinel keyed on TODAY's date
  # ONLY (not cwd/session-scoped) — clear it before each invocation below so this
  # test's fixture runs aren't silently suppressed by an unrelated real session's
  # banner firing earlier in the same 5-minute window.
  T04_EOD_SENTINEL="${TMPDIR:-/tmp}/quoin-s4-eod-banner-$(date -u +%Y%m%d).tmp"

  # --- same-day case: session file dated today, end_of_day_due: yes ---
  rm -f "$T04_SESSIONS"/*.md 2>/dev/null || true
  printf -- '---\n## Status\nin_progress\n## Cost\n- end_of_day_due: yes\n' \
    > "$T04_SESSIONS/${TODAY_T04}-t04-same-day-task.md"

  rm -f "$T04_EOD_SENTINEL" 2>/dev/null || true
  out_t04_same=$(printf '%s' "$(make_t04_stdin "sess-t04-same")" | sh "$SESSIONSTART_HOOK" 2>/dev/null)
  if printf '%s' "$out_t04_same" | grep -q '/run --resume' 2>/dev/null \
     && ! printf '%s' "$out_t04_same" | grep -q '/checkpoint' 2>/dev/null; then
    pass "Test 2 — sessionstart.sh same-day-only flagged session points at /end_of_day and /run --resume"
  else
    fail "Test 2 — sessionstart.sh same-day-only flagged session wording wrong: $out_t04_same"
  fi

  out_t04_same_end=$(printf '%s' "$(make_t04_stdin "sess-t04-same-end")" | sh "$SESSIONEND_HOOK" 2>/dev/null)
  if printf '%s' "$out_t04_same_end" | grep -q '/run --resume' 2>/dev/null \
     && ! printf '%s' "$out_t04_same_end" | grep -q '/checkpoint' 2>/dev/null; then
    pass "Test 2 — sessionend.sh same-day-only flagged session points at /end_of_day and /run --resume"
  else
    fail "Test 2 — sessionend.sh same-day-only flagged session wording wrong: $out_t04_same_end"
  fi

  # --- cross-day case: session file dated yesterday, end_of_day_due: yes ---
  if [ -n "$YESTERDAY_T04" ]; then
    rm -f "$T04_SESSIONS"/*.md 2>/dev/null || true
    printf -- '---\n## Status\nin_progress\n## Cost\n- end_of_day_due: yes\n' \
      > "$T04_SESSIONS/${YESTERDAY_T04}-t04-cross-day-task.md"
    # mtime must be recent enough to survive each hook's own lookback window
    # (sessionstart.sh: -mtime -2 / 48h; sessionend.sh: 8h) — file was just written, so ok.

    rm -f "$T04_EOD_SENTINEL" 2>/dev/null || true
    out_t04_cross=$(printf '%s' "$(make_t04_stdin "sess-t04-cross")" | sh "$SESSIONSTART_HOOK" 2>/dev/null)
    if printf '%s' "$out_t04_cross" | grep -q '/end_of_day' 2>/dev/null; then
      pass "Test 3 — sessionstart.sh cross-day flagged session recommends /end_of_day"
    else
      fail "Test 3 — sessionstart.sh cross-day flagged session did not mention /end_of_day: $out_t04_cross"
    fi

    out_t04_cross_end=$(printf '%s' "$(make_t04_stdin "sess-t04-cross-end")" | sh "$SESSIONEND_HOOK" 2>/dev/null)
    if printf '%s' "$out_t04_cross_end" | grep -q '/end_of_day' 2>/dev/null; then
      pass "Test 3 — sessionend.sh cross-day flagged session recommends /end_of_day"
    else
      fail "Test 3 — sessionend.sh cross-day flagged session did not mention /end_of_day: $out_t04_cross_end"
    fi
  else
    pass "Test 3 — (skipped: could not compute yesterday's date via python3)"
  fi

  rm -rf "$T04_TMPDIR" 2>/dev/null || true
else
  pass "Test 2/3 — (skipped: sessionstart.sh/sessionend.sh/python3 not available)"
fi

# ── Final summary ──────────────────────────────────────────────────────────────
echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] || exit 1
