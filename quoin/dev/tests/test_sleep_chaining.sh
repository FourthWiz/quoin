#!/usr/bin/env bash
# test_sleep_chaining.sh — static text checks verifying /end_of_day → /sleep chaining.
#
# All tests are grep checks against quoin/adapters/claude/skills/end_of_day/SKILL.md
# (the active Claude adapter file; quoin/skills/end_of_day/SKILL.md is a deprecated,
# behavior-free stub since the Phase 16 adapter migration).
# Runtime verification (actual sleep subagent firing) is T-16 Sub-task B manual smoke.
#
# Usage:
#   bash quoin/dev/tests/test_sleep_chaining.sh
# Exit:
#   0 — all 4 sub-tests pass
#   1 — one or more sub-tests failed

set -e

SKILL_FILE="quoin/adapters/claude/skills/end_of_day/SKILL.md"
PASS=0
FAIL=0

# ---------------------------------------------------------------------------
# test_skip_sleep_flag
# ---------------------------------------------------------------------------
test_skip_sleep_flag() {
  local name="test_skip_sleep_flag"
  local ok=true

  grep -q 'skip-sleep' "$SKILL_FILE" || {
    echo "FAIL: ${name}: --skip-sleep not found in end_of_day SKILL.md"
    ok=false
  }

  grep -q 'Skipping /sleep' "$SKILL_FILE" || {
    echo "FAIL: ${name}: 'Skipping /sleep' skip message not found in end_of_day SKILL.md"
    ok=false
  }

  grep -q 'Step 6' "$SKILL_FILE" || {
    echo "FAIL: ${name}: 'Step 6' not found in end_of_day SKILL.md"
    ok=false
  }

  if $ok; then
    echo "PASS: ${name}"
    PASS=$((PASS + 1))
  else
    FAIL=$((FAIL + 1))
  fi
}

# ---------------------------------------------------------------------------
# test_sleep_failure_no_rollback
# ---------------------------------------------------------------------------
test_sleep_failure_no_rollback() {
  local name="test_sleep_failure_no_rollback"
  local ok=true

  grep -q 'quoin-S-3: /sleep invocation failed' "$SKILL_FILE" || {
    echo "FAIL: ${name}: '[quoin-S-3: /sleep invocation failed' not found in end_of_day SKILL.md"
    ok=false
  }

  grep -q 'DO NOT roll back' "$SKILL_FILE" || {
    echo "FAIL: ${name}: 'DO NOT roll back' instruction not found in end_of_day SKILL.md"
    ok=false
  }

  if $ok; then
    echo "PASS: ${name}"
    PASS=$((PASS + 1))
  else
    FAIL=$((FAIL + 1))
  fi
}

# ---------------------------------------------------------------------------
# test_default_chain_fires
# ---------------------------------------------------------------------------
test_default_chain_fires() {
  local name="test_default_chain_fires"
  local ok=true

  # The [no-redispatch] sentinel must appear inside Step 6 (the /sleep subagent dispatch prompt)
  grep -q '\[no-redispatch\]' "$SKILL_FILE" || {
    echo "FAIL: ${name}: '[no-redispatch]' sentinel not found in end_of_day SKILL.md"
    ok=false
  }

  # NOTE: runtime verification that the sleep subagent actually fires is manual
  # and is covered in T-16 Sub-task B smoke.

  if $ok; then
    echo "PASS: ${name} (static text check; runtime: T-16 Sub-task B)"
    PASS=$((PASS + 1))
  else
    FAIL=$((FAIL + 1))
  fi
}

# ---------------------------------------------------------------------------
# test_step_6_dispatch_tier
# ---------------------------------------------------------------------------
test_step_6_dispatch_tier() {
  local name="test_step_6_dispatch_tier"
  local ok=true

  # The Step 6 /sleep subagent dispatch must declare model: "sonnet" (/sleep
  # moved to Sonnet tier in IVG-263; a stale haiku dispatch would run it
  # under-powered, and [no-redispatch] suppresses /sleep's own min-tier guard).
  grep -q 'model: "sonnet"' "$SKILL_FILE" || {
    echo "FAIL: ${name}: 'model: \"sonnet\"' not found in end_of_day SKILL.md"
    ok=false
  }

  if $ok; then
    echo "PASS: ${name}"
    PASS=$((PASS + 1))
  else
    FAIL=$((FAIL + 1))
  fi
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
echo "Running 4 sub-tests from test_sleep_chaining.sh"
echo "SKILL_FILE: ${SKILL_FILE}"
echo ""

test_skip_sleep_flag
test_sleep_failure_no_rollback
test_default_chain_fires
test_step_6_dispatch_tier

echo ""
echo "Results: ${PASS} passed, ${FAIL} failed"

if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
exit 0
