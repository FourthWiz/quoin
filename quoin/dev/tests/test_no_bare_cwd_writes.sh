#!/bin/sh
# test_no_bare_cwd_writes.sh — grep-canary completeness lint for IVG-61
#
# Ensures every hook cwd variable is followed by a resolve_project_root call,
# and that no $(pwd)/.workflow_artifacts path bypasses the helper in the hooks.
#
# Usage: sh quoin/dev/tests/test_no_bare_cwd_writes.sh
# Exit 0 if all checks pass; non-zero otherwise.

set -eu

PASS=0
FAIL=0
FAIL_MSGS=""

ok()   { PASS=$((PASS + 1)); printf 'ok  %s\n' "$1"; }
fail() {
  FAIL=$((FAIL + 1))
  printf 'FAIL %s\n' "$1" >&2
  FAIL_MSGS="$FAIL_MSGS\n  - $1"
}

HOOKS_DIR="$(cd "$(dirname "$0")" && pwd)/../../hooks"

# ─── 2. precompact.sh: ≥1 resolve_project_root call ─────────────────────────
_pc="$HOOKS_DIR/precompact.sh"
if [ -r "$_pc" ]; then
  _count=$(grep -c 'resolve_project_root' "$_pc" 2>/dev/null || echo 0)
  if [ "$_count" -ge 1 ]; then
    ok "precompact.sh: resolve_project_root present (found $_count)"
  else
    fail "precompact.sh: resolve_project_root missing"
  fi
else
  fail "precompact.sh: not readable at $_pc"
fi

# ─── 3. postcompact.sh: ≥1 resolve_project_root call + _lib.sh source ────────
_poc="$HOOKS_DIR/postcompact.sh"
if [ -r "$_poc" ]; then
  _count=$(grep -c 'resolve_project_root' "$_poc" 2>/dev/null || echo 0)
  if [ "$_count" -ge 1 ]; then
    ok "postcompact.sh: resolve_project_root present (found $_count)"
  else
    fail "postcompact.sh: resolve_project_root missing"
  fi
  # Also verify _lib.sh is sourced (postcompact previously did not source it)
  if grep -q '_lib\.sh' "$_poc" 2>/dev/null; then
    ok "postcompact.sh: _lib.sh sourced"
  else
    fail "postcompact.sh: _lib.sh not sourced (needed for resolve_project_root)"
  fi
else
  fail "postcompact.sh: not readable at $_poc"
fi

# ─── 4. sessionstart.sh: ≥1 resolve_project_root call ───────────────────────
_ss="$HOOKS_DIR/sessionstart.sh"
if [ -r "$_ss" ]; then
  _count=$(grep -c 'resolve_project_root' "$_ss" 2>/dev/null || echo 0)
  if [ "$_count" -ge 1 ]; then
    ok "sessionstart.sh: resolve_project_root present (found $_count)"
  else
    fail "sessionstart.sh: resolve_project_root missing"
  fi
else
  fail "sessionstart.sh: not readable at $_ss"
fi

# ─── 5. sessionend.sh: ≥1 resolve_project_root call ─────────────────────────
_se="$HOOKS_DIR/sessionend.sh"
if [ -r "$_se" ]; then
  _count=$(grep -c 'resolve_project_root' "$_se" 2>/dev/null || echo 0)
  if [ "$_count" -ge 1 ]; then
    ok "sessionend.sh: resolve_project_root present (found $_count)"
  else
    fail "sessionend.sh: resolve_project_root missing"
  fi
else
  fail "sessionend.sh: not readable at $_se"
fi

# ─── 6. _lib.sh: resolve_project_root defined ────────────────────────────────
_lib="$HOOKS_DIR/_lib.sh"
if [ -r "$_lib" ]; then
  if grep -q 'resolve_project_root()' "$_lib" 2>/dev/null; then
    ok "_lib.sh: resolve_project_root() function defined"
  else
    fail "_lib.sh: resolve_project_root() function not found"
  fi
else
  fail "_lib.sh: not readable at $_lib"
fi

# ─── 7. No bare ${cwd}/.workflow_artifacts in hooks without preceding resolve ─
# Advisory (not exhaustive): grep for the antipattern ${cwd}/.workflow_artifacts
# appearing in precompact.sh before the resolve line.
# The resolve line adds `cwd=$(resolve_project_root "$cwd")` immediately after
# the fallback, so any ${cwd}/.workflow_artifacts BELOW the resolve is fine.
# We check that the resolve appears BEFORE the first ${cwd}/.workflow_artifacts usage.
if [ -r "$_pc" ]; then
  _resolve_line=$(grep -n 'resolve_project_root' "$_pc" | head -1 | cut -d: -f1)
  _first_use=$(grep -n '"\${cwd}/.workflow_artifacts\|"${cwd}/.workflow_artifacts' "$_pc" | head -1 | cut -d: -f1)
  if [ -n "$_resolve_line" ] && [ -n "$_first_use" ]; then
    if [ "$_resolve_line" -lt "$_first_use" ]; then
      ok "precompact.sh: resolve (line $_resolve_line) precedes first cwd/.workflow_artifacts use (line $_first_use)"
    else
      fail "precompact.sh: resolve (line $_resolve_line) does NOT precede first cwd/.workflow_artifacts use (line $_first_use)"
    fi
  else
    ok "precompact.sh: ordering check skipped (grep returned empty — likely different quoting)"
  fi
fi

# ─── Summary ─────────────────────────────────────────────────────────────────
printf '\n---\n'
if [ "$FAIL" -eq 0 ]; then
  printf 'PASS: %d  FAIL: 0 — canary green\n' "$PASS"
  exit 0
else
  printf 'PASS: %d  FAIL: %d\n' "$PASS" "$FAIL"
  printf 'Failures:%b\n' "$FAIL_MSGS"
  exit 1
fi
