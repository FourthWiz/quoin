#!/bin/sh
# Frozen copy of the run-continuation block from the previous
# userpromptsubmit.sh, kept only as a parity oracle for the residual hook.
# It sources the repo _lib.sh for resolve_project_root. Do not edit.
. "$(dirname "$0")/../../../hooks/_lib.sh"

STDIN=$(cat)

# run continuation: a typed prompt takes control back; a typed /run records
# consent. jq-free and runs before any jq-dependent step so it still fires
# without jq. Decision logic lives in scripts/auto_resume.py; this block only
# maintains the arm/consent sentinels the Stop hook reads.
(
  _ar_sid=$(printf '%s' "$STDIN" | sed -n 's/.*"session_id"[[:space:]]*:[[:space:]]*"\([A-Za-z0-9-]*\)".*/\1/p' | head -1)
  [ -n "$_ar_sid" ] || exit 0
  _ar_tp=$(printf '%s' "$STDIN" | sed -n 's/.*"transcript_path"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)
  case "$_ar_tp" in */subagents/*) exit 0 ;; esac
  printf '%s' "$STDIN" | grep -qE '"prompt"[[:space:]]*:[[:space:]]*"[[:space:]]*(<task-notification>)' && exit 0
  _ar_cwd=$(printf '%s' "$STDIN" | sed -n 's/.*"cwd"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)
  [ -n "$_ar_cwd" ] || _ar_cwd="$PWD"
  _ar_root=$(resolve_project_root "$_ar_cwd")
  _ar_mem="$_ar_root/.workflow_artifacts/memory"
  [ -n "$_ar_root" ] && [ -d "$_ar_mem" ] || exit 0
  rm -f "$_ar_mem/run-continue-arm-$_ar_sid.txt"
  if printf '%s' "$STDIN" | grep -qE '"prompt"[[:space:]]*:[[:space:]]*"[[:space:]]*/run([[:space:]]|\\|")' \
     && ls "$_ar_mem"/run-state-*.json >/dev/null 2>&1; then
    touch "$_ar_mem/run-continue-consent-$_ar_sid.txt"
  fi
) </dev/null >/dev/null 2>&1 || true
