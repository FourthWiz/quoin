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

# All four sub-tests assert on Step 6 specifically (the /sleep dispatch), so
# each grep is scoped to just that region rather than the whole file — a
# file-wide grep for a string like 'model: "sonnet"' or '[no-redispatch]'
# would also match unrelated §0/§0‴ dispatch-preamble blocks and pass even
# if Step 6 itself regressed.
STEP6="$(sed -n '/^### Step 6/,/^## Important behaviors/p' "$SKILL_FILE")"

# ---------------------------------------------------------------------------
# test_skip_sleep_flag
# ---------------------------------------------------------------------------
test_skip_sleep_flag() {
  local name="test_skip_sleep_flag"
  local ok=true

  grep -q 'skip-sleep' <<<"$STEP6" || {
    echo "FAIL: ${name}: --skip-sleep not found in Step 6 of end_of_day SKILL.md"
    ok=false
  }

  grep -q 'Skipping /sleep' <<<"$STEP6" || {
    echo "FAIL: ${name}: 'Skipping /sleep' skip message not found in Step 6 of end_of_day SKILL.md"
    ok=false
  }

  grep -q 'Step 6' <<<"$STEP6" || {
    echo "FAIL: ${name}: 'Step 6' not found in Step 6 of end_of_day SKILL.md"
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

  grep -q 'quoin-S-3: /sleep invocation failed' <<<"$STEP6" || {
    echo "FAIL: ${name}: '[quoin-S-3: /sleep invocation failed' not found in Step 6 of end_of_day SKILL.md"
    ok=false
  }

  grep -q 'DO NOT roll back' <<<"$STEP6" || {
    echo "FAIL: ${name}: 'DO NOT roll back' instruction not found in Step 6 of end_of_day SKILL.md"
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
  grep -q '\[no-redispatch\]' <<<"$STEP6" || {
    echo "FAIL: ${name}: '[no-redispatch]' sentinel not found in Step 6 of end_of_day SKILL.md"
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
  # was moved to Sonnet tier, and [no-redispatch] suppresses /sleep's own
  # min-tier guard, so the parent's dispatch choice here is load-bearing —
  # a stale haiku dispatch would run it under-powered).
  grep -q 'model: "sonnet"' <<<"$STEP6" || {
    echo "FAIL: ${name}: 'model: \"sonnet\"' not found in Step 6 of end_of_day SKILL.md"
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
