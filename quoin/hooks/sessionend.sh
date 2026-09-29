#!/bin/sh
# sessionend.sh — SessionEnd hook for quoin workflow isolation
# Deployed to ~/.claude/hooks/ by bash install.sh
# Registered for: SessionEnd (confirmed by T-00 spike — event name verified against Anthropic docs).
#
# S-4 responsibility: EOD nudge at session end.
#   - Reads most-recently-modified sessions/*.md file (mtime within 8 hours)
#   - If end_of_day_due: yes → folds an S-4 nudge into the systemMessage
#   - Does NOT invoke /end_of_day — nudge only
#
# §V responsibility (T-12/D-09/D-12): deterministic ground-truth verification
# backstop for /end_of_day, independent of end_of_day_due. Gated on the EOD-class
# signal daily/<today>.md (not end_of_day_due, which a complete-but-lying run
# flips to "no"). Reads the claims manifest T-05's early always-run step wrote and
# reconciles it (--finalized-only, no gh/network) — an absent OR present-but-empty
# manifest is itself a positive-failure signal, folded into the same systemMessage
# as the S-4 nudge. See quoin/memory/verification-guide.md.
#
# Output channel: systemMessage (SessionEnd does NOT support additionalContext — T-00 confirmed)
# SessionEnd runs asynchronously; exit code is ignored. Fail-OPEN by design.
#
# Fail-OPEN: any error → exit 0, no output. STEP 5b (§V) never `exit`s on failure —
# only STEP 4 (no recent session file) and STEP 7b (Close-snapshot gate) may exit.

. "$(dirname "$0")/_lib.sh" && read_constants

# STEP -1: Capture stdin (even if unused — consistency with other hooks)
STDIN=$(cat)

# run continuation: mark this session ended and drop its arm so a later
# Stop in the same process tree never fires against a session that's gone.
# jq-free and runs before STEP 1's jq-dependent early exit.
(
  _ar_sid=$(printf '%s' "$STDIN" | sed -n 's/.*"session_id"[[:space:]]*:[[:space:]]*"\([A-Za-z0-9-]*\)".*/\1/p' | head -1)
  [ -n "$_ar_sid" ] || exit 0
  _ar_cwd=$(printf '%s' "$STDIN" | sed -n 's/.*"cwd"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)
  [ -n "$_ar_cwd" ] || _ar_cwd="$PWD"
  _ar_root=$(resolve_project_root "$_ar_cwd")
  _ar_mem="$_ar_root/.workflow_artifacts/memory"
  [ -n "$_ar_root" ] && [ -d "$_ar_mem" ] || exit 0
  ls "$_ar_mem"/run-state-*.json >/dev/null 2>&1 || exit 0
  touch "$_ar_mem/session-ended-$_ar_sid.txt"
  rm -f "$_ar_mem/run-continue-arm-$_ar_sid.txt"
) </dev/null >/dev/null 2>&1 || true

# STEP 1: Parse cwd from stdin
cwd=$(printf '%s' "$STDIN" | jq -r '.cwd // empty' 2>/dev/null) || exit 0
[ -z "$cwd" ] && cwd="$PWD"
cwd=$(resolve_project_root "$cwd")

SESSIONS_DIR="${cwd}/.workflow_artifacts/memory/sessions"

# STEP 2: If sessions/ does not exist → exit 0 silently
[ -d "$SESSIONS_DIR" ] || exit 0

# STEP 3: Find most recently modified session file within the last 8 hours
# 8 hours = 28800 seconds; use -mtime -1 (24h window) + mtime sort for portability
# Then apply a POSIX-compatible age check using file mtime vs current time
NOW=$(date +%s)
EIGHT_HOURS=28800

RECENT_FILE=""
RECENT_MTIME=0

# Use a temp file to collect find results (POSIX-safe: avoids < <(...) bash-ism)
_SE_TMPFILE=$(mktemp 2>/dev/null) || _SE_TMPFILE="${TMPDIR:-/tmp}/quoin-s4-se-tmp-$$"
find "$SESSIONS_DIR" -maxdepth 1 -name '*.md' -mtime -1 2>/dev/null > "$_SE_TMPFILE"

while IFS= read -r f; do
  [ -f "$f" ] || continue
  # Get file mtime in epoch seconds using a POSIX-portable approach
  # Try: python3 (available on macOS + Linux); fallback: stat (BSD form works on darwin)
  fmtime=$(python3 -c "import os,sys; print(int(os.path.getmtime(sys.argv[1])))" "$f" 2>/dev/null) \
    || fmtime=$(stat -f %m "$f" 2>/dev/null) \
    || fmtime=0
  age=$(( NOW - fmtime ))
  if [ "$age" -le "$EIGHT_HOURS" ] && [ "$fmtime" -gt "$RECENT_MTIME" ]; then
    RECENT_MTIME="$fmtime"
    RECENT_FILE="$f"
  fi
done < "$_SE_TMPFILE"
rm -f "$_SE_TMPFILE" 2>/dev/null || true

# STEP 4: If no recent file found → exit 0
[ -z "$RECENT_FILE" ] && exit 0

# STEP 5: Compute eod_due boolean (T-12 restructure — NO exit here; was hard `|| exit 0`)
if grep -q 'end_of_day_due: yes' "$RECENT_FILE" 2>/dev/null; then
  eod_due=1
else
  eod_due=0
fi

# STEP 5b: §V ground-truth verification backstop (T-12/D-09/D-12/MAJ-1/MAJ-2)
# Deterministic, model-cannot-skip reconcile against the claims manifest end_of_day
# writes in an always-run early step (T-05). EOD-class gate = daily/<today>.md
# presence (D-12), NOT end_of_day_due — so a complete-but-lying end_of_day is still
# reconciled. Fail-open: every line below falls through on any error (never `exit` —
# only STEP 4 above and STEP 7b below may exit).
verify_msg=""
today=$(date +%Y-%m-%d)
daily="${cwd}/.workflow_artifacts/memory/daily/${today}.md"
if [ -f "$daily" ]; then
  WRAPPER="$(dirname "$0")/../scripts/verify_claims.py"
  hookaudit="${cwd}/.workflow_artifacts/memory/verification/end_of_day-${today}.hookaudit.md"
  audit_reason=""
  if [ -f "$WRAPPER" ]; then
    manifest="${cwd}/.workflow_artifacts/memory/verification/end_of_day-${today}.md"
    if [ ! -f "$manifest" ]; then
      # STATE 1: manifest ABSENT (CRIT-1) — the model skipped the always-run manifest step
      verify_msg="[quoin-§V] end_of_day ran (daily/<today>.md present) but wrote no verification manifest — verification was skipped; re-run /end_of_day verification."
      audit_reason="manifest-absent"
    else
      _v_json=$(python3 "$WRAPPER" --reconcile-tasks --claims-file "$manifest" --finalized-only --project-root "$cwd" --json 2>/dev/null)
      _v_exit=$?
      if [ "$_v_exit" -eq 8 ] 2>/dev/null; then
        _v_parsed=$(printf '%s' "$_v_json" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get("reason", ""))
    print(",".join(d.get("mismatched_tasks", [])))
except Exception:
    print("")
    print("")
' 2>/dev/null)
        _reason=$(printf '%s\n' "$_v_parsed" | sed -n '1p')
        _tasks=$(printf '%s\n' "$_v_parsed" | sed -n '2p')
        if [ "$_reason" = "empty-manifest" ]; then
          # STATE 2: PRESENT-BUT-EMPTY (MAJ-2), window-scoped — never the all-time archive
          verify_msg="[quoin-§V] end_of_day wrote an empty verification manifest (no claims) while ${_tasks} task(s) remain unclaimed — verification was skipped; re-run /end_of_day verification."
          audit_reason="empty-manifest"
        else
          # STATE 3a: MISMATCH — a listed claim contradicts re-derived truth
          verify_msg="[quoin-§V] end_of_day claims contradict ground truth: ${_tasks} — re-run /end_of_day verification."
          audit_reason="mismatch:${_tasks}"
        fi
      fi
      # _v_exit == 0 -> STATE 3b, clean reconcile; verify_msg stays ""
    fi
  fi
  # MAJ-2 part 2: the hook's own audit line goes to a DISTINCT sibling, NEVER to the
  # manifest path — an absent-branch append to the manifest path would fabricate an
  # empty stub, converting "absent" into "present-but-empty" on the next same-day fire.
  if [ -n "$audit_reason" ]; then
    mkdir -p "$(dirname "$hookaudit")" 2>/dev/null
    printf '> hook-reconcile: %s @%s\n' "$audit_reason" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$hookaudit" 2>/dev/null || true
  fi
fi

# STEP 6: Extract task name for the nudge message
_recent_fname=$(basename "$RECENT_FILE" .md)
task_name=$(printf '%s' "$_recent_fname" | sed 's/^[0-9]*-[0-9]*-[0-9]*-//')

# T-04: same-day/cross-day branch — ISO-date filename prefix vs today (reuses STEP 5b's
# `today`, already computed above), compared as integers (portable on bash 3.2 / dash).
_recent_date_num=$(printf '%s' "$_recent_fname" | cut -c1-10 | tr -d '-')
_today_num=$(printf '%s' "$today" | tr -d '-')
_past_day=0
case "$_recent_date_num" in
  ''|*[!0-9]*) ;;  # malformed prefix — skip date comparison, don't misclassify
  *) [ "$_recent_date_num" -lt "$_today_num" ] 2>/dev/null && _past_day=1 ;;
esac

# Banner shape mirrors quoin/hooks/sessionstart.sh and quoin/adapters/claude/skills/start_of_day/SKILL.md Step 1 — keep in sync; drift test: quoin/dev/tests/test_lifecycle_banner_drift.sh
# STEP 7: Compose and emit ONE systemMessage from the two independent signals
# (S-4 nudge + §V banner) — preserves the single-object hook-output contract (MAJ-B).
# SessionEnd output channel is systemMessage (not additionalContext — confirmed by T-00 spike).
nudge=""
if [ "$eod_due" = 1 ]; then
  if [ "$_past_day" -eq 1 ]; then
    nudge="[quoin-S-4] Session ending with unfinished task: ${task_name} — a prior day was never wrapped up; run /end_of_day to wrap up the workday."
  else
    nudge="[quoin-S-4] Session ending with unfinished task: ${task_name} — run /checkpoint to save your place (resume next session) or /end_of_day to wrap up the workday."
  fi
fi
msg="$nudge"
if [ -n "$verify_msg" ]; then
  if [ -n "$msg" ]; then
    msg="${msg} ${verify_msg}"
  else
    msg="$verify_msg"
  fi
fi
if [ -n "$msg" ]; then
  printf '{"systemMessage": "%s"}\n' "$(printf '%s' "$msg" | sed 's/"/\\"/g')"
fi

# STEP 7b: MAJ-1 — explicit guard LOGICALLY EQUIVALENT to STEP 8's original
# `end_of_day_due: yes` entry gate (not byte-identical: eod_due=1 iff that same
# grep succeeds). Close snapshot stays behind its original gate even though STEP 5b
# (reconcile) and STEP 7 (banner) now run for every EOD-class session.
[ "$eod_due" = 1 ] || exit 0

# STEP 8: Capture Close snapshot
# Writes a ## Close snapshot block to the active session-state file so /end_of_day
# Step 3e can reconcile the session UUID into the cost ledger.
# Fail-OPEN: every failure path falls through to exit 0 with no output.
# No new dependencies (python3, stat, find, sed, grep, basename, date, mktemp, cat).
# No stdout output — the existing STEP 7 systemMessage is the only stdout line.
_S2_TMP=""
_S2_BLOCK=""
_S2_CLEANUP() { rm -f "$_S2_TMP" "$_S2_BLOCK" 2>/dev/null || true; }

proj_hash=$(printf '%s' "$cwd" | sed 's|/|-|g') || { _S2_CLEANUP; exit 0; }
jsonl_dir="$HOME/.claude/projects/$proj_hash"
[ -d "$jsonl_dir" ] || exit 0

_S2_TMP=$(mktemp 2>/dev/null) || _S2_TMP="${TMPDIR:-/tmp}/quoin-s2-tmp-$$"
find "$jsonl_dir" -maxdepth 1 -name '*.jsonl' -mmin -60 2>/dev/null > "$_S2_TMP" || { _S2_CLEANUP; exit 0; }

# Select JSONL with greatest mtime using python3; fallback to stat -f %m (BSD)
selected_jsonl=$(python3 - "$_S2_TMP" <<'PYEOF' 2>/dev/null
import sys, os
with open(sys.argv[1]) as f:
    files = [l.strip() for l in f if l.strip()]
if not files:
    sys.exit(1)
best = max(files, key=lambda p: os.path.getmtime(p))
print(best)
PYEOF
) || selected_jsonl=""

if [ -z "$selected_jsonl" ]; then
  _S2_CLEANUP; exit 0
fi

# Get mtime of selected JSONL (seconds since epoch)
jsonl_mtime=$(python3 -c "import os,sys; print(int(os.path.getmtime(sys.argv[1])))" "$selected_jsonl" 2>/dev/null) \
  || jsonl_mtime=$(stat -f %m "$selected_jsonl" 2>/dev/null) \
  || jsonl_mtime=0

# Get mtime of session-state file
session_mtime=$(python3 -c "import os,sys; print(int(os.path.getmtime(sys.argv[1])))" "$RECENT_FILE" 2>/dev/null) \
  || session_mtime=$(stat -f %m "$RECENT_FILE" 2>/dev/null) \
  || session_mtime=0

# Stale cross-check: skip if JSONL was modified before the session-state file
if [ "$jsonl_mtime" -lt "$session_mtime" ] 2>/dev/null; then
  _S2_CLEANUP; exit 0
fi

jsonl_uuid=$(basename "$selected_jsonl" .jsonl) || { _S2_CLEANUP; exit 0; }

# Idempotency check: skip if UUID already recorded in the session file
grep -q "Session UUID:[[:space:]]*$jsonl_uuid" "$RECENT_FILE" 2>/dev/null && { _S2_CLEANUP; exit 0; }

# Build the snapshot block in a tmpfile then atomically append it
_S2_BLOCK=$(mktemp 2>/dev/null) || _S2_BLOCK="${TMPDIR:-/tmp}/quoin-s2-block-$$"
printf '\n## Close snapshot\n- Closed at: %s\n- JSONL UUID: %s\n- Project: %s\n- Note: session closed; UUID captured by sessionend hook for EOD reconciliation\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$jsonl_uuid" "$proj_hash" > "$_S2_BLOCK" 2>/dev/null || { _S2_CLEANUP; exit 0; }

cat "$_S2_BLOCK" >> "$RECENT_FILE" 2>/dev/null || true
_S2_CLEANUP

exit 0
