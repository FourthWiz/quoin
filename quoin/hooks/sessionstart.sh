#!/bin/sh
# sessionstart.sh — SessionStart hook for quoin workflow isolation
# Deployed to ~/.claude/hooks/ by bash install.sh
# Registered for THREE matchers: startup, resume, and compact. Matcher-aware —
# the compact branch is a dedicated early-exit; startup/resume share the
# banner body below it, which the compact branch never reaches.
#
# S-2 responsibility: pending-restore banner emission.
#   - Sweeps stale pending-prompt-*.txt and pending-restore-*.txt files
#   - Surfaces pending-restore banner if a sentinel is found for this session
#     (or mtime-most-recent if no current-session match)
#
# Fail-OPEN: any error → exit 0, no output.
# Shebang assertion: head -1 ... | grep -qE '^#!/bin/sh( |$)'
# No-args form RECOMMENDED for fail-OPEN hooks.

# Source shared helper library
. "$(dirname "$0")/_lib.sh" && read_constants

# STEP -1: Capture stdin before any parsing (stdin can only be read once)
STDIN=$(cat)

# STEP 1: Parse source and session_id
src=$(printf '%s' "$STDIN" | jq -r '.source // empty' 2>/dev/null) || exit 0
session_id=$(printf '%s' "$STDIN" | jq -r '.session_id // empty' 2>/dev/null) || exit 0
cwd=$(printf '%s' "$STDIN" | jq -r '.cwd // empty' 2>/dev/null) || exit 0
[ -z "$cwd" ] && cwd="$PWD"
raw_cwd="$cwd"   # S-04: capture RAW launch dir BEFORE resolve_project_root rewrites it to the project root
cwd=$(resolve_project_root "$cwd")

MEMORY_DIR="${cwd}/.workflow_artifacts/memory"

# === IVG-258 post-compaction re-entry ===
# Early-exit branch: on a compaction-triggered session start (source=compact),
# read this session's active run-state record (if any) and emit a one-shot
# advisory plus a verbatim resume-command echo, then exit before the banner
# body below runs. Ordering matters: MEMORY_DIR must already be set above
# and this branch must run before the missing-EOD/discovery-staleness/sweep
# steps, none of which apply to a compaction re-entry.
if [ "$src" = "compact" ]; then
  # No session_id: nothing to key the record lookup on. Mirrors precompact.sh's
  # own guard against an empty id matching an equally-empty stored field.
  [ -z "$session_id" ] && exit 0

  run_state_file=$(run_state_select "$MEMORY_DIR" "$session_id" 2>/dev/null) || run_state_file=""
  # No matching active record for THIS session: a plain conversation compacting
  # stays quiet — no output at all, not even an empty object.
  [ -z "$run_state_file" ] && exit 0

  rs_task=""; rs_phase=""; rs_subphase=""; rs_step=""
  rs_at_stage_boundary=""; rs_next_action=""; rs_notes_path=""; rs_resume_command=""
  while IFS='=' read -r _rk _rv; do
    case "$_rk" in
      task) rs_task=$_rv ;;
      phase) rs_phase=$_rv ;;
      subphase) rs_subphase=$_rv ;;
      step) rs_step=$_rv ;;
      at_stage_boundary) rs_at_stage_boundary=$_rv ;;
      next_action) rs_next_action=$_rv ;;
      notes_path) rs_notes_path=$_rv ;;
      resume_command) rs_resume_command=$_rv ;;
    esac
  done <<RSEOF
$(run_state_fields "$run_state_file" task phase subphase step at_stage_boundary next_action notes_path resume_command)
RSEOF

  # notes_path containment (mirrors precompact.sh's traversal + prefix + flatness
  # + symlink checks): reject on any of the four checks by clearing the value —
  # the run-notes line is then dropped from the advisory entirely.
  case "$rs_notes_path" in *..*) rs_notes_path="" ;; esac
  case "$rs_notes_path" in "$MEMORY_DIR"/run-notes-*.md) ;; *) rs_notes_path="" ;; esac
  case "${rs_notes_path#"$MEMORY_DIR"/}" in */*) rs_notes_path="" ;; esac
  if [ -n "$rs_notes_path" ] && [ -L "$rs_notes_path" ]; then
    rs_notes_path=""
  fi

  # D-11: at a stage boundary, `step` names the phase that JUST finished, not
  # what comes next — the raw value would misdirect a reader relying on this
  # advisory alone. Qualify phase/sub-phase as completed and replace the step
  # line with a boundary framing; next_action stays the anchor for what's next.
  if [ "$rs_at_stage_boundary" = "true" ]; then
    _reentry_phase_line="phase (completed): ${rs_phase}"
    _reentry_subphase_line="sub-phase (completed): ${rs_subphase}"
    _reentry_step_line="step: stage complete (${rs_phase} / ${rs_subphase}); see next action below"
  else
    _reentry_phase_line="phase: ${rs_phase}"
    _reentry_subphase_line="sub-phase: ${rs_subphase}"
    _reentry_step_line="step: ${rs_step}"
  fi

  _reentry_ctx="[quoin-reentry] run/task: ${rs_task}
${_reentry_phase_line}
${_reentry_subphase_line}
${_reentry_step_line}
next action: ${rs_next_action}"
  if [ -n "$rs_notes_path" ]; then
    _reentry_ctx="${_reentry_ctx}
run-notes: ${rs_notes_path}"
  fi

  # Envelope: jq -nc --arg only, never printf-interpolated (D-05) — session_id
  # and every record field arrive from outside this hook's control. Wrapper
  # matches the file's three existing SessionStart emit sites; initialUserMessage
  # nests inside hookSpecificOutput (T-01 probe e, Candidate B) and is skipped
  # when the record carries no resume command to echo — the field is passed
  # through as-is, never built here.
  if [ -n "$rs_resume_command" ]; then
    jq -nc --arg ctx "$_reentry_ctx" --arg cmd "$rs_resume_command" \
      '{hookSpecificOutput: {hookEventName: "SessionStart", additionalContext: $ctx, initialUserMessage: $cmd}}'
  else
    jq -nc --arg ctx "$_reentry_ctx" \
      '{hookSpecificOutput: {hookEventName: "SessionStart", additionalContext: $ctx}}'
  fi
  exit 0
fi
# === end IVG-258 post-compaction re-entry ===

# === run continuation hand-off ===
# On a fresh or resumed session start, ask the run-continuation helper
# whether the previous owner of an active run-state record is gone; if so
# it spawns a detached supervisor and returns an advisory naming the task.
# Decision logic lives in scripts/auto_resume.py. Fail-safe: without a
# session_id, a memory dir, a run-state record or python3, this does nothing.
_ar_task=""
case "$src" in
  startup|resume)
    if [ -n "$session_id" ] && [ -d "$MEMORY_DIR" ] \
       && ls "$MEMORY_DIR"/run-state-*.json >/dev/null 2>&1 \
       && command -v python3 >/dev/null 2>&1; then
      _ar_out=$(python3 "$(dirname "$0")/../scripts/auto_resume.py" start --project-root "$cwd" --source "$src" --session-id "$session_id" 2>/dev/null)
      [ -n "$_ar_out" ] && printf '%s\n' "$_ar_out"
      _ar_task=$(printf '%s' "$_ar_out" | sed -n 's/.*task=\([A-Za-z0-9._-]*\) .*via=supervisor.*/\1/p')
    fi
    ;;
esac
# === end run continuation hand-off ===

# === S-4 missing-EOD banner ===
# Check for session files with end_of_day_due: yes within last 36 hours.
# Sentinel dedup: skip if banner fired within the last 5 minutes.

SESSIONS_DIR="${MEMORY_DIR}/sessions"
EOD_SENTINEL="${TMPDIR:-/tmp}/quoin-s4-eod-banner-$(date -u +%Y%m%d).tmp"
EOD_BANNER_FIRED=0

# Dedup check: if sentinel exists and is < 300 seconds old, skip banner
if [ -f "$EOD_SENTINEL" ]; then
  SENTINEL_MTIME=$(python3 -c "import os,sys; print(int(os.path.getmtime(sys.argv[1])))" "$EOD_SENTINEL" 2>/dev/null \
    || stat -f %m "$EOD_SENTINEL" 2>/dev/null \
    || echo 0)
  SENTINEL_AGE=$(( $(date +%s) - SENTINEL_MTIME ))
  if [ "$SENTINEL_AGE" -lt 300 ]; then
    EOD_BANNER_FIRED=1
  fi
fi

if [ "$EOD_BANNER_FIRED" -eq 0 ] && [ -d "$SESSIONS_DIR" ]; then
  # Collect task names from session files modified within last 36 hours
  # with end_of_day_due: yes
  # 36 hours = 1.5 days; -mtime -2 catches files modified within last 48h on most systems
  # Use a temp file to collect results (POSIX-safe: avoids < <(...) bash-ism)
  _EOD_TMPFILE=$(mktemp 2>/dev/null) || _EOD_TMPFILE="${TMPDIR:-/tmp}/quoin-s4-eod-tmp-$$"
  find "$SESSIONS_DIR" -maxdepth 1 -name '*.md' -mtime -2 2>/dev/null > "$_EOD_TMPFILE"

  # T-04: same-day/cross-day branch — ISO-date filename prefix vs today, compared as
  # integers (portable on bash 3.2 / dash; avoids the non-POSIX `[ a \< b ]` string op).
  # LOCAL date (not -u): session filenames are stamped with the writer's local
  # calendar date, and this must agree with sessionend.sh's pre-existing local
  # `today` (used for its daily-cache path check) so both surfaces classify the
  # same file the same way.
  TODAY_NUM=$(date +%Y%m%d)
  ANY_PAST_DAY=0

  UNFINISHED_TASKS=""
  while IFS= read -r session_file; do
    [ -f "$session_file" ] || continue
    if grep -q 'end_of_day_due: yes' "$session_file" 2>/dev/null; then
      # Extract task name from filename: <date>-<task-name>.md → task-name
      _fname=$(basename "$session_file" .md)
      task_name=$(printf '%s' "$_fname" | sed 's/^[0-9]*-[0-9]*-[0-9]*-//')
      UNFINISHED_TASKS="${UNFINISHED_TASKS}${task_name} "
      # First 10 chars of the filename are the YYYY-MM-DD date prefix
      _session_date_num=$(printf '%s' "$_fname" | cut -c1-10 | tr -d '-')
      case "$_session_date_num" in
        ''|*[!0-9]*) ;;  # malformed prefix — skip date comparison, don't misclassify
        *) [ "$_session_date_num" -lt "$TODAY_NUM" ] 2>/dev/null && ANY_PAST_DAY=1 ;;
      esac
    fi
  done < "$_EOD_TMPFILE"
  rm -f "$_EOD_TMPFILE" 2>/dev/null || true

  UNFINISHED_TASKS="${UNFINISHED_TASKS% }"
  # banner shape mirrors quoin/adapters/claude/skills/start_of_day/SKILL.md Step 1 — keep in sync
  # Same-day (all flagged sessions dated today) -> /checkpoint; cross-day (any predates
  # today) -> /end_of_day, per T-04 (IVG-137).
  if [ -n "$UNFINISHED_TASKS" ]; then
    if [ "$ANY_PAST_DAY" -eq 1 ]; then
      _eod_action="a prior day was never wrapped up — run /end_of_day to wrap up the workday."
    else
      _eod_action="run /checkpoint to save your place (or /end_of_day to wrap up the workday)."
    fi
    printf '{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "[quoin-S-4] Unfinished /end_of_day detected for task(s): %s — %s"}}\n' \
      "$UNFINISHED_TASKS" "$_eod_action"
    # Write dedup sentinel
    touch "$EOD_SENTINEL" 2>/dev/null || true
  fi
fi
# === end S-4 missing-EOD banner ===

# === S-5 discovery-staleness banner ===
# Runs after S-4. Calls discovery_staleness.py (deployed wrapper) via hook-relative path.
# Fail-OPEN: any error (python missing, script absent, non-zero-other) → no banner, continue.
# Exit codes: 0=fresh/disabled (no banner), 10=stale, 11=absent, 12=serena-present-but-stale.
# Dedup sentinel: mirrors S-4 pattern exactly, including stat -f %m fallback.

_disc_banner=""
_DISC_SENTINEL="${TMPDIR:-/tmp}/quoin-s5-staleness-banner-$(date -u +%Y%m%d).tmp"

# Dedup check: skip if sentinel fired within the last 300 seconds
if [ -f "$_DISC_SENTINEL" ]; then
  _disc_sentinel_mtime=$(python3 -c "import os,sys; print(int(os.path.getmtime(sys.argv[1])))" "$_DISC_SENTINEL" 2>/dev/null \
    || stat -f %m "$_DISC_SENTINEL" 2>/dev/null \
    || echo 0)
  _disc_sentinel_age=$(( $(date +%s) - _disc_sentinel_mtime ))
  if [ "$_disc_sentinel_age" -lt 300 ]; then
    _DISC_SKIP_S5=1
  fi
fi

if [ "${_DISC_SKIP_S5:-0}" = "0" ]; then
  if command -v python3 >/dev/null 2>&1; then
    # env (QUOIN_DISCOVERY_STALE_DAYS, QUOIN_DISCOVERY_REFRESH_DISABLE) propagates to child
    python3 "$(dirname "$0")/../scripts/discovery_staleness.py" "$cwd" --quiet >/dev/null 2>&1
    _disc_rc=$?
  else
    _disc_rc=0   # no python → no signal → no banner (fail-OPEN)
  fi
  case "$_disc_rc" in
    10) _disc_banner="[quoin-S-5] Discovery memory may be stale — run /start_of_day or /discover to refresh. Set QUOIN_DISCOVERY_REFRESH_DISABLE=1 to silence." ;;
    11) _disc_banner="[quoin-S-5] No discovery memory found — run /discover to index the project." ;;
    12) _disc_banner="[quoin-S-5] Serena project memory is stale — run /start_of_day and choose 'Set up / Refresh Serena memory'. Set QUOIN_DISCOVERY_REFRESH_DISABLE=1 to silence." ;;
    *)  _disc_banner="" ;;   # 0 fresh/disabled, 2 error → silent (fail-OPEN)
  esac
  if [ -n "$_disc_banner" ]; then
    printf '{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "%s"}}\n' "$_disc_banner"
    touch "$_DISC_SENTINEL" 2>/dev/null || true
  fi
fi
# === end S-5 discovery-staleness banner ===

# === S-04 workspace heartbeat (opt-in) ===
# Owner-only last_seen refresh for a session launched INSIDE a feature workspace.
# Default OFF (opt-in per Q-06). Output fully suppressed and `|| true` so it can
# NEVER change hook stdout/exit status — the hook stays fail-OPEN and its banner
# JSON is untouched. Uses raw_cwd (captured before resolve_project_root) because
# the marker lives BELOW the project root under .workspaces/<feat>/. INERT until
# S-06 deploys workspace.py to ~/.claude/scripts/ (until then the shell-out errors
# harmlessly into /dev/null).
if [ "${QUOIN_WORKSPACE_HEARTBEAT:-0}" = "1" ] && command -v python3 >/dev/null 2>&1; then
  python3 "$(dirname "$0")/../scripts/workspace.py" heartbeat \
    --cwd "$raw_cwd" --session-uuid "$session_id" >/dev/null 2>&1 || true
fi
# === end S-04 workspace heartbeat (opt-in) ===

# STEP 2: UUID-aware sweep of all 9 sentinel families.
# Tight window (SESSIONSTART_SWEEP_DAYS, default 1d) when current session is known;
# falls back to conservative STALE_DAYS (7d) age-only when session_id is empty (defensive — see D-02).
# NOTE (D-08): the tight window also applies to pending-restore/pending-resume-ref/mid-agent-handoff,
# so STEP 4's cross-session fallback now sees only <=1d non-current pending-restore anchors. This is
# an INTENTIONAL narrowing of the STEP 4 input set (NOT a no-op); the banner-rendering code below is
# byte-unchanged. Do NOT re-add any "STEP 3+ unchanged" claim.
# Safe for paths with spaces: IFS= read -r preserves the full path (verified on the "My Drive" root).
# Sentinel basenames are FAMILY-<uuid>.txt (no newlines), so the find|while-read pipe is newline-safe
# for this controlled filename set — diverges from the old -exec form but is correct here.
# (_lib.sh sourced once at sessionstart.sh:16, so sentinel_globs/trash_move are in scope; no per-file re-source.)
if [ -n "$session_id" ]; then
  _sweep_days="$SESSIONSTART_SWEEP_DAYS"
else
  _sweep_days="$STALE_DAYS"   # no anchor → conservative age-only (defensive, see D-02)
fi
sentinel_globs | while IFS= read -r _glob; do
  [ -n "$_glob" ] || continue
  find "$MEMORY_DIR" -maxdepth 1 -name "$_glob" -mtime +"$_sweep_days" 2>/dev/null | \
  while IFS= read -r _f; do
    [ -f "$_f" ] || continue
    _base=$(basename "$_f")
    # current-session protection: skip files ending in -SESSION_ID.txt
    if [ -n "$session_id" ] && [ "$_base" != "${_base%-${session_id}.txt}" ]; then
      continue
    fi
    trash_move "$_f" "$MEMORY_DIR" 2>/dev/null || true
  done
done 2>/dev/null || true

# STEP 3: Look for sentinel files (priority order: pending-restore > pending-resume-ref > mid-agent-handoff)
# Emit AT MOST ONE banner per session start.
pending_restore=""
pending_resume_ref=""
mid_agent_handoff=""
session_id_match="current-session"
banner_text=""
banner_type=""

# STEP 3a: Check for pending-restore (highest priority — direct resume)
if [ -n "$session_id" ] && [ -f "${MEMORY_DIR}/pending-restore-${session_id}.txt" ]; then
  pending_restore="${MEMORY_DIR}/pending-restore-${session_id}.txt"
  session_id_match="current-session"
fi

# STEP 3b: Check for pending-resume-ref (informational — load as background reference)
if [ -n "$session_id" ] && [ -f "${MEMORY_DIR}/pending-resume-ref-${session_id}.txt" ]; then
  pending_resume_ref="${MEMORY_DIR}/pending-resume-ref-${session_id}.txt"
fi

# STEP 3c: Check for mid-agent-handoff (advisory — skill was running at checkpoint)
if [ -n "$session_id" ] && [ -f "${MEMORY_DIR}/mid-agent-handoff-${session_id}.txt" ]; then
  mid_agent_handoff="${MEMORY_DIR}/mid-agent-handoff-${session_id}.txt"
fi

# STEP 4: Fallback to mtime-most-recent if no current-session match for pending-restore
# (UUID-shaped session_ids have no time-ordering; lex order is meaningless)
if [ -z "$pending_restore" ]; then
  most_recent=$(ls -t "${MEMORY_DIR}"/pending-restore-*.txt 2>/dev/null | head -1)
  if [ -n "$most_recent" ] && [ -f "$most_recent" ]; then
    if [ -n "$_ar_task" ] && grep -qF -- "$_ar_task" "$most_recent"; then
      : # the run-continuation hand-off above already covers this task
    else
      pending_restore="$most_recent"
      session_id_match="mismatch-warning: surfaced from different session (mtime-most-recent fallback)"
    fi
  fi
fi

# STEP 5: Build banner text (AT MOST ONE banner; use highest-priority sentinel)
# Priority: pending-restore > pending-resume-ref > mid-agent-handoff
if [ -n "$pending_restore" ]; then
  sentinel_content=$(cat "$pending_restore" 2>/dev/null)
  if [ -n "$sentinel_content" ]; then
    banner_text="Pending restore detected. A /checkpoint --restore is recommended (checkpoint: ${sentinel_content} — session-id match: ${session_id_match})"
    banner_type="pending-restore"

    # === restore ground-truth backstop (IVG-139) ===
    # NOTE: intentionally NOT labeled "S-4"/"S-5" — this file already has an internal
    # "=== S-4 missing-EOD banner ===" block (:118) and "=== S-5 discovery-staleness banner ==="
    # block (:190) whose numbering is unrelated to architecture stage IDs; reusing either token
    # here would make it mean three different things in one file (critic round-1 m-1).
    # Advisory-only: append a task-context-mismatch WARN to the pending-restore banner by
    # reusing verify_claims.py check_side_effects(skill=checkpoint) UNCHANGED. WARN-not-block.
    # Fail-OPEN: any error/other-rc -> no suffix (SessionStart cannot block regardless).
    # Shared-namespace guard (critic round-1 M-1): the pending-restore sentinel family is
    # SHARED with /thorough_plan (thorough_plan_checkpoint.py:190-191 writes this same
    # sentinel, pointing at a fixed thorough-plan-progress-{sid}.md filename, :131). The
    # reused predicate is kind-blind and filename_task() on that filename shape NEVER equals
    # a real sessions/*.md task, so calling the predicate on it would ALWAYS emit
    # task_backstop: and ALWAYS warn — a false positive on every normal /thorough_plan
    # resume. Skip the predicate call entirely for that filename shape instead.
    _gt_cp=$(printf '%s' "$sentinel_content" | head -1)
    if [ -n "$_gt_cp" ] && [ -f "$_gt_cp" ] && command -v python3 >/dev/null 2>&1; then
      case "$(basename "$_gt_cp")" in
        thorough-plan-progress-*)
          : ;;  # /thorough_plan owns its own resume; not a /checkpoint task-mismatch signal
        *)
          _gt_json=$(python3 "$(dirname "$0")/../scripts/verify_claims.py" \
            --check-side-effects --skill checkpoint \
            --checkpoint-file "$_gt_cp" --project-root "$cwd" --json 2>/dev/null)
          _gt_rc=$?
          # WARN ONLY on a genuine task-context mismatch (task_backstop predicate) — never on
          # inflight_missing / checkpoint_file_missing / usage errors (R-04 false-positive guard).
          if [ "$_gt_rc" -eq 8 ] && printf '%s' "$_gt_json" | grep -q 'task_backstop:'; then
            banner_text="${banner_text} [quoin-IVG-139 WARN: task-context mismatch — this checkpoint's task differs from your freshest session-state; verify before /checkpoint --restore (advisory only; the /checkpoint picker makes the actual decision)]"
          fi
          ;;
      esac
    fi
    # === end restore ground-truth backstop (IVG-139) ===
  fi
elif [ -n "$pending_resume_ref" ]; then
  # Read prior_session_uuid and checkpoint_path from the sentinel
  ref_prior_uuid=$(grep '^prior_session_uuid=' "$pending_resume_ref" 2>/dev/null | cut -d= -f2)
  ref_checkpoint=$(grep '^checkpoint_path=' "$pending_resume_ref" 2>/dev/null | cut -d= -f2-)
  banner_text="Prior session loaded as reference. Checkpoint: ${ref_checkpoint:-unknown}. Run /checkpoint --restore only if you want to resume; otherwise this prior context is read-only background (prior session UUID: ${ref_prior_uuid:-unknown})."
  banner_type="pending-resume-ref"
elif [ -n "$mid_agent_handoff" ]; then
  # Read prior_session_uuid and active_skills from the sentinel
  handoff_uuid=$(grep '^prior_session_uuid=' "$mid_agent_handoff" 2>/dev/null | cut -d= -f2)
  handoff_skills=$(grep '^active_skills=' "$mid_agent_handoff" 2>/dev/null | cut -d= -f2-)
  banner_text="Mid-agent handoff detected: session ${handoff_uuid:-unknown} had active skill(s) [${handoff_skills:-unknown}] when paused. Inspect ${mid_agent_handoff} and decide whether to /checkpoint --restore, /clear and re-run, or abandon."
  banner_type="mid-agent-handoff"
fi

# STEP 6: Emit banner if any sentinel was found
if [ -n "$banner_text" ]; then
  printf '{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "%s"}}\n' \
    "$banner_text"
fi

# === S-1 pollution-score writer (stub — populated by S-1) ===
# (intentionally empty; S-1 implementation extends this block)
