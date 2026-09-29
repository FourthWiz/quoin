#!/bin/sh
# userpromptsubmit.sh — UserPromptSubmit hook for quoin workflow isolation
# Deployed to ~/.claude/hooks/ by bash install.sh
#
# Contract: reads transcript_path, prompt, session_id, cwd from stdin JSON.
# Checks context utilization; emits an advisory at STOP_BPS and at BLOCK_BPS.
# Fail-OPEN: any error → exit 0, no output.
#
# Shebang assertion: head -1 ... | grep -qE '^#!/bin/sh( |$)'
# No-args form RECOMMENDED for fail-OPEN hooks (set -e would break fail-OPEN).

# Source shared helper library
. "$(dirname "$0")/_lib.sh" && read_constants

# STEP -1: Capture stdin before any parsing (stdin can only be read once)
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

# STEP 0.5: Pollution-score writer (Plan B — runs on every prompt submit, fail-OPEN)
# T-00 spike confirmed: SessionStart does not provide transcript_path, so the writer
# lives here. Score written before the exemption check so it fires on all prompts.
(
    _ups_tp=$(printf '%s' "$STDIN" | jq -r '.transcript_path // empty' 2>/dev/null) || true
    _ups_sid=$(printf '%s' "$STDIN" | jq -r '.session_id // empty' 2>/dev/null) || true
    if [ -n "$_ups_tp" ] && [ -r "$_ups_tp" ]; then
        _ups_score=$(compute_pollution_score "$_ups_tp") || true
        if [ -n "$_ups_score" ]; then
            _ups_cwd=$(printf '%s' "$STDIN" | jq -r '.cwd // empty' 2>/dev/null) || true
            [ -z "$_ups_cwd" ] && _ups_cwd="$PWD"
            _ups_cwd=$(resolve_project_root "$_ups_cwd")
            _ups_mem="${_ups_cwd}/.workflow_artifacts/memory"
            _ups_session=$(find "${_ups_mem}/sessions/" -name "$(date +%Y-%m-%d)-*.md" -type f -print0 2>/dev/null | xargs -0 ls -t 2>/dev/null | head -1)
            if [ -n "$_ups_session" ] && [ -f "$_ups_session" ]; then
                sed -i.bak '/^pollution_score:/d' "$_ups_session" 2>/dev/null && \
                  rm -f "${_ups_session}.bak" 2>/dev/null || true
                printf 'pollution_score: %s\n' "$_ups_score" >> "$_ups_session" 2>/dev/null || true
            else
                mkdir -p "$_ups_mem" 2>/dev/null || true
                printf '%s\n' "$_ups_score" > "${_ups_mem}/pollution-score-latest.txt" 2>/dev/null || true
            fi
            # PostCompact sentinel: if auto-compaction occurred, consume the sentinel.
            # The compacted transcript is naturally small so _ups_score above is already
            # the post-compact value — no recomputation needed. Guard: skip if no session_id.
            if [ -n "$_ups_sid" ]; then
                _ups_postcompact="${_ups_mem}/postcompact-reset-${_ups_sid}.txt"
                if [ -f "$_ups_postcompact" ]; then
                    # Trash-moves: postcompact-reset-${_ups_sid}.txt and checkpoint-defer-${_ups_sid}.txt.
                    # Preserved (intentionally NOT moved here): compact-happened-${_ups_sid}.txt (read by /checkpoint Step 1.4).
                    trash_move "$_ups_postcompact" "$_ups_mem" 2>/dev/null || true
                    # Also expire defer marker on post-compact (meaningful work boundary)
                    _ups_defer="${_ups_mem}/checkpoint-defer-${_ups_sid}.txt"
                    [ -f "$_ups_defer" ] && trash_move "$_ups_defer" "$_ups_mem" 2>/dev/null || true
                fi
            fi
        fi
    fi
) 2>/dev/null || true

# STEP 0.7: Recent-session tracking + idle flag
(
    _rs_sid=$(printf '%s' "$STDIN" | jq -r '.session_id // empty' 2>/dev/null) || true
    _rs_cwd=$(printf '%s' "$STDIN" | jq -r '.cwd // empty' 2>/dev/null) || true
    [ -z "$_rs_cwd" ] && _rs_cwd="$PWD"
    _rs_cwd=$(resolve_project_root "$_rs_cwd")
    [ -z "$_rs_sid" ] && exit 0

    _rs_mem="${_rs_cwd}/.workflow_artifacts/memory"
    mkdir -p "$_rs_mem" 2>/dev/null || exit 0
    _rs_file="${_rs_mem}/recent-sessions.md"
    _rs_now=$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null) || _rs_now=$(date +%Y-%m-%dT%H:%M:%SZ)

    # Idle check: look for previous entry for this session_id
    _rs_idle=0
    if [ -f "$_rs_file" ]; then
        _rs_prev=$(grep " | ${_rs_sid}$" "$_rs_file" 2>/dev/null | tail -1)
        if [ -n "$_rs_prev" ]; then
            _rs_prev_ts=$(printf '%s' "$_rs_prev" | awk '{print $1}')
            _rs_prev_epoch=$(python3 -c "
import sys, datetime
ts = sys.argv[1].replace('Z', '+00:00')
try:
    d = datetime.datetime.fromisoformat(ts)
    print(int(d.timestamp()))
except Exception:
    sys.exit(1)
" "$_rs_prev_ts" 2>/dev/null) || _rs_prev_epoch=0
            _rs_now_epoch=$(python3 -c "import time; print(int(time.time()))" 2>/dev/null) || _rs_now_epoch=0
            if [ -n "$_rs_prev_epoch" ] && [ "$_rs_prev_epoch" -gt 0 ] && [ "$_rs_now_epoch" -gt 0 ]; then
                _rs_gap=$(( _rs_now_epoch - _rs_prev_epoch ))
                [ "$_rs_gap" -gt 3600 ] && _rs_idle=1
            fi
        fi
    fi

    # Append current entry (unconditional)
    printf '%s | %s\n' "$_rs_now" "$_rs_sid" >> "$_rs_file" 2>/dev/null || true

    # Write idle flag file if idle gap detected
    if [ "$_rs_idle" -eq 1 ]; then
        printf '%s\n' "$_rs_now" > "${_rs_mem}/idle-advisory-pending-${_rs_sid}.txt" 2>/dev/null || true
    fi
) 2>/dev/null || true

# STEP 0: Recovery-command exemption
# Extract prompt field (POSIX-portable; avoid echo's non-portable behavior)
prompt=$(printf '%s' "$STDIN" | jq -r '.prompt // empty' 2>/dev/null) || exit 0

# Strip ALL leading whitespace (including newlines, carriage returns, tabs)
# then extract first whitespace-delimited token (the command token).
# NOTE: sed '^' matches line-by-line; a leading newline creates an empty first
# line that `^[[:space:]]+` cannot strip. tr converts newlines/CRs to spaces
# first so the whole prompt is a single-line string before sed+awk processing.
cmd=$(printf '%s' "$prompt" | tr '\n\r' '  ' | sed -E 's/^[[:space:]]+//' | awk '{print $1}')

# Extract second token for /checkpoint --purge discrimination
arg2=$(printf '%s' "$prompt" | tr '\n\r' '  ' | sed -E 's/^[[:space:]]+//' | awk '{print $2}')

# Exact-token match — exempt-list per quoin/CLAUDE.md ### Hooks deployed by quoin
case "$cmd" in
  /compact|/clear|/help)
    exit 0
    ;;
  /checkpoint)
    case "$arg2" in
      --purge)
        # NOT exempt — destructive subcommand falls through to threshold logic
        # (IVG-258: --purge reaches the high-context advisory, which names the risk)
        ;;
      *)
        # All other /checkpoint subcommands exempt (no-arg, --restore, future args)
        exit 0
        ;;
    esac
    ;;
esac

# STEP 0.9: Idle-session advisory (parent shell, after STEP 0 exemption check)
_idle_cwd=$(printf '%s' "$STDIN" | jq -r '.cwd // empty' 2>/dev/null)
[ -z "$_idle_cwd" ] && _idle_cwd="$PWD"
_idle_cwd=$(resolve_project_root "$_idle_cwd")
_idle_sid=$(printf '%s' "$STDIN" | jq -r '.session_id // empty' 2>/dev/null)
if [ -n "$_idle_sid" ]; then
    _idle_flag="${_idle_cwd}/.workflow_artifacts/memory/idle-advisory-pending-${_idle_sid}.txt"
    if [ -f "$_idle_flag" ]; then
        rm -f "$_idle_flag" 2>/dev/null || true
        printf '{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "[quoin-idle] This session has been idle for over 1 hour. Consider starting a fresh session to keep token usage low. Run /continue_work to resume context in a new session."}}\n'
        exit 0
    fi
fi

# STEP 1: Read transcript path
transcript_path=$(printf '%s' "$STDIN" | jq -r '.transcript_path // empty' 2>/dev/null) || exit 0
[ -z "$transcript_path" ] && exit 0

# STEP 2: Compute utilization (returns basis-point integer 0..10000)
util=$(compute_utilization "$transcript_path") || exit 0
[ -z "$util" ] && exit 0

# STEP 3: Branch on utilization
if [ "$util" -lt "$STOP_BPS" ]; then
  # Branch (1): below advisory threshold — transparent passthrough
  exit 0
elif [ "$util" -ge "$STOP_BPS" ] && [ "$util" -lt "$BLOCK_BPS" ]; then
  # Branch (2): advisory range (STOP_BPS..BLOCK_BPS-1) — non-blocking advisory
  # Defer-marker guard: re-parse cwd+session_id from stdin (cheap; $STDIN already in memory)
  # These variables are NOT yet acquired — they are only parsed inside the BLOCK branch (lines below).
  _adv_cwd=$(printf '%s' "$STDIN" | jq -r '.cwd // empty' 2>/dev/null)
  [ -z "$_adv_cwd" ] && _adv_cwd="$PWD"
  _adv_cwd=$(resolve_project_root "$_adv_cwd")
  _adv_sid=$(printf '%s' "$STDIN" | jq -r '.session_id // empty' 2>/dev/null)
  if [ -n "$_adv_sid" ]; then
    _adv_defer="${_adv_cwd}/.workflow_artifacts/memory/checkpoint-defer-${_adv_sid}.txt"
    [ -f "$_adv_defer" ] && exit 0
  fi
  # Defer marker not set — emit advisory. The checkpoint guidance is
  # conditional on a fresh active run-state record existing project-wide
  # (run_state_probe, project-scoped) — the advisory itself always fires;
  # only the "run /checkpoint" wording is arm-conditional.
  pct_int=$((util / 100))
  pct_dec=$(printf '%02d' $((util % 100)))
  if run_state_probe "${_adv_cwd}/.workflow_artifacts/memory"; then
    printf '{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "context at %d.%s%% — consider running /checkpoint and starting a fresh session"}}\n' \
      "$pct_int" "$pct_dec"
  else
    printf '{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "context at %d.%s%% — start a fresh session when convenient; run /continue_work there to restore context. No checkpoint is needed: no active run in this project."}}\n' \
      "$pct_int" "$pct_dec"
  fi
  exit 0
else
  # Branch (3): high-context range (>= BLOCK_BPS)
  # Advisory-ordering invariant: the advisory is emitted ONLY AFTER the
  # pending-prompt file is written; if any step fails, exit 0 (fail-OPEN).

  # STEP A: Re-parse session_id (may already have prompt from STEP 0)
  session_id=$(printf '%s' "$STDIN" | jq -r '.session_id // empty' 2>/dev/null) || exit 0

  # STEP B-validate: session_id must be non-empty (discriminant collapses without it)
  [ -z "$session_id" ] && exit 0

  # STEP B: Compute pending-prompt path
  cwd=$(printf '%s' "$STDIN" | jq -r '.cwd // empty' 2>/dev/null) || exit 0
  [ -z "$cwd" ] && cwd="$PWD"
  cwd=$(resolve_project_root "$cwd")
  pending_prompt_file="${cwd}/.workflow_artifacts/memory/pending-prompt-${session_id}.txt"

  # Ensure directory exists
  mkdir -p "${cwd}/.workflow_artifacts/memory" 2>/dev/null || exit 0

  # STEP C: Append blocked prompt to pending-prompt file
  # Uses append (>>) not overwrite (>) so multiple parallel agent completions accumulate.
  # Each entry is prefixed with a === BLOCKED PROMPT [<timestamp>] === header.
  # Migration: if the file exists in legacy raw-text format (no header), convert it
  # to headered format on first append so no prior content is dropped.
  # Task-notification detection (T-03): background subagent completions (wrapped in
  # <task-notification>...</task-notification> tags) are marked [non-replayable:task-notification]
  # in the header so restore CASE B excludes them from 'all' replay while preserving
  # the audit trail. Users can still replay them by explicit single-number selection.
  _timestamp=$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null) || _timestamp="unknown"
  _is_task_notification=0
  case "$prompt" in *'<task-notification>'*) _is_task_notification=1 ;; esac
  if [ "$_is_task_notification" -eq 1 ]; then
    _prompt_header="=== BLOCKED PROMPT [${_timestamp}] [non-replayable:task-notification] ==="
  else
    _prompt_header="=== BLOCKED PROMPT [${_timestamp}] ==="
  fi
  if [ -f "$pending_prompt_file" ] && ! head -1 "$pending_prompt_file" 2>/dev/null | grep -q '^=== BLOCKED PROMPT'; then
    _legacy=$(cat "$pending_prompt_file" 2>/dev/null)
    {
      printf '=== BLOCKED PROMPT [legacy-pre-migration] ===\n'
      printf '%s\n' "$_legacy"
      printf '%s\n' "$_prompt_header"
      printf '%s\n' "$prompt"
    } > "$pending_prompt_file" 2>/dev/null || exit 0
  else
    {
      printf '%s\n' "$_prompt_header"
      printf '%s\n' "$prompt"
    } >> "$pending_prompt_file" 2>/dev/null || exit 0
  fi

  # STEP D: Emit the advisory (only reaches here if STEP C succeeded).
  # Advisory-ordering invariant (was: error-ordering invariant): the advisory
  # names pending-prompt-{sid}.txt, so it is emitted ONLY AFTER that append
  # succeeded. If the append failed, STEP C already exited 0 with no stdout —
  # the prompt still reaches the model in the same turn.
  # This branch NEVER emits a block-shaped JSON response on any path (IVG-258).
  pct_int=$((util / 100))
  pct_dec=$(printf '%02d' $((util % 100)))
  _purge_note=""
  if [ "$cmd" = "/checkpoint" ] && [ "$arg2" = "--purge" ]; then
    _purge_note=" NOTE: /checkpoint --purge is destructive and is no longer refused at high context — re-read what it will delete before continuing."
  fi
  # Checkpoint mid-clause is arm-conditional on a fresh active run-state
  # record existing project-wide (run_state_probe, project-scoped,
  # matching branch (2) above).
  if run_state_probe "$cwd/.workflow_artifacts/memory"; then
    _cp_clause=" and consider /checkpoint plus a fresh session"
  else
    _cp_clause=" and start a fresh session (/continue_work restores context; no checkpoint is needed — no active run in this project)"
  fi
  _msg=$(printf '[quoin-context] context at %d.%s%% — finish the current stage, prefer subagents for heavy reads,%s. Your prompt was also appended to pending-prompt-%s.txt (prompt text only — no checkpoint was written).%s' \
    "$pct_int" "$pct_dec" "$_cp_clause" "$session_id" "$_purge_note")
  jq -nc --arg m "$_msg" '{systemMessage: $m, hookSpecificOutput: {hookEventName: "UserPromptSubmit", additionalContext: $m}}'
  exit 0
fi
