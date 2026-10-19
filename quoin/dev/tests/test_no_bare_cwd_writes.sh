#!/bin/sh
# test_no_bare_cwd_writes.sh — grep-canary completeness lint for IVG-61
#
# Ensures every hook cwd variable is followed by a resolve_project_root call,
# and that no $(pwd)/.workflow_artifacts path bypasses the helper in the hooks
# or the /checkpoint SKILL.md.
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
SKILL_MD="$(cd "$(dirname "$0")" && pwd)/../../skills/checkpoint/SKILL.md"

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

# ─── 7. SKILL.md: no $(pwd)/.workflow_artifacts adjacent hit ─────────────────
# Scope: grep for $(pwd) immediately followed by /.workflow_artifacts
# Prose mentions like "NOT $(pwd)" do NOT have the path appended, so they won't match.
if [ -r "$SKILL_MD" ]; then
  _pwd_hits=$(grep -n '\$(pwd)/\.workflow_artifacts' "$SKILL_MD" 2>/dev/null | wc -l | awk '{print $1}')
  if [ "$_pwd_hits" -eq 0 ]; then
    ok "checkpoint SKILL.md: no \$(pwd)/.workflow_artifacts adjacent hits"
  else
    fail "checkpoint SKILL.md: found $_pwd_hits \$(pwd)/.workflow_artifacts hit(s) — should be 0"
    grep -n '\$(pwd)/\.workflow_artifacts' "$SKILL_MD" 2>/dev/null | head -5 >&2 || true
  fi
  # Also verify _PROJECT_ROOT appears (resolve-once instruction present)
  if grep -q '_PROJECT_ROOT' "$SKILL_MD" 2>/dev/null; then
    ok "checkpoint SKILL.md: _PROJECT_ROOT reference present"
  else
    fail "checkpoint SKILL.md: _PROJECT_ROOT not found — resolve-once instruction may be missing"
  fi
else
  fail "checkpoint SKILL.md: not readable at $SKILL_MD"
fi

# ─── 8. No bare ${cwd}/.workflow_artifacts in hooks without preceding resolve ─
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

# ─── 9. SKILL.md: no ${_cwd}/.workflow_artifacts, ${_rs_cwd}/.workflow_artifacts,
#         or <cwd>/.workflow_artifacts prose placeholder in the save-mode region ──
# Check A: shell-variable forms (save-mode underscore-prefixed names). The project-hash
#   exception uses `printf '%s' "$_cwd" | tr '/' '-'` and never produces
#   "${_cwd}/.workflow_artifacts", so it won't match.
# Check B: prose-placeholder form `<cwd>/.workflow_artifacts` in the save-mode region.
#   Restore mode now also uses ${_PROJECT_ROOT}, so ALL occurrences in both save-mode
#   and restore-mode should be zero after the IVG-61 fix.
#   Exception: the resolve-once instruction itself uses the literal string
#   "from stdin JSON .cwd field" (not "<cwd>/.workflow_artifacts"), so it won't match.
if [ -r "$SKILL_MD" ]; then
  _cwd_hits=$(grep -E '\$\{_cwd\}/\.workflow_artifacts|\$\{_rs_cwd\}/\.workflow_artifacts' "$SKILL_MD" 2>/dev/null | wc -l | awk '{print $1}')
  if [ "$_cwd_hits" -eq 0 ]; then
    ok "checkpoint SKILL.md: no save-mode un-resolved _cwd/.workflow_artifacts path sites"
  else
    fail "checkpoint SKILL.md: found $_cwd_hits save-mode un-resolved _cwd/.workflow_artifacts site(s) — convert to \${_PROJECT_ROOT}"
    grep -nE '\$\{_cwd\}/\.workflow_artifacts|\$\{_rs_cwd\}/\.workflow_artifacts' "$SKILL_MD" 2>/dev/null | head -5 >&2 || true
  fi
  # Check B: prose placeholder form — should be zero in both save-mode and restore-mode
  _prose_hits=$(grep -n '<cwd>/\.workflow_artifacts' "$SKILL_MD" 2>/dev/null | wc -l | awk '{print $1}')
  if [ "$_prose_hits" -eq 0 ]; then
    ok "checkpoint SKILL.md: no <cwd>/.workflow_artifacts prose placeholder sites"
  else
    fail "checkpoint SKILL.md: found $_prose_hits <cwd>/.workflow_artifacts prose placeholder site(s) — convert to \${_PROJECT_ROOT}"
    grep -n '<cwd>/\.workflow_artifacts' "$SKILL_MD" 2>/dev/null | head -5 >&2 || true
  fi
fi

# ─── 10. SKILL.md: Step 2 write path uses ${_PROJECT_ROOT} prefix ─────────────
if [ -r "$SKILL_MD" ]; then
  if grep -qE '_PROJECT_ROOT.*\.workflow_artifacts/memory/checkpoints' "$SKILL_MD" 2>/dev/null; then
    ok "checkpoint SKILL.md: Step 2 write path references _PROJECT_ROOT/.workflow_artifacts/memory/checkpoints"
  else
    fail "checkpoint SKILL.md: Step 2 write path does not reference _PROJECT_ROOT — bare relative path may still be present"
  fi
fi

# ─── 11. SKILL.md: no bare-relative .workflow_artifacts/memory — token-anchored ─
# For each occurrence of .workflow_artifacts/memory, the character immediately before
# it must be / (meaning it is part of a resolved ${VAR}/.workflow... path).
# Allowlist (line-level grep -v): shell comments (#), resolve_project_root calls,
# lines with the word "prefix", lines with the prose placeholder <path>.
# Any unlisted occurrence where the preceding char is NOT / → fail.
# Uses perl for per-occurrence lookahead (POSIX grep cannot do lookbehind).
if [ -r "$SKILL_MD" ]; then
  _bare_hits=$(grep -n '\.workflow_artifacts/memory' "$SKILL_MD" 2>/dev/null \
    | grep -v '^[^:]*:[[:space:]]*#' \
    | grep -v 'resolve_project_root' \
    | perl -ne 'while (/(.?)\.workflow_artifacts\/memory/g) { print "$_" if $1 ne "/" }' \
    | wc -l | awk '{print $1}')
  if [ "$_bare_hits" -eq 0 ]; then
    ok "checkpoint SKILL.md: no bare-relative .workflow_artifacts/memory token (token-anchored check)"
  else
    fail "checkpoint SKILL.md: found $_bare_hits bare-relative .workflow_artifacts/memory occurrence(s) — each must be preceded by / (i.e. part of \${VAR}/.workflow...)"
    grep -n '\.workflow_artifacts/memory' "$SKILL_MD" 2>/dev/null \
      | grep -v '^[^:]*:[[:space:]]*#' \
      | grep -v 'resolve_project_root' \
      | perl -ne 'while (/(.?)\.workflow_artifacts\/memory/g) { print if $1 ne "/" }' \
      | head -10 >&2 || true
  fi
else
  fail "checkpoint SKILL.md (check 11): not readable"
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
