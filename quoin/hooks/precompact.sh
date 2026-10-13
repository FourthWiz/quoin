#!/bin/sh
# precompact.sh — PreCompact hook for quoin workflow isolation
# Deployed to ~/.claude/hooks/ by bash install.sh
#
# Contract: fires on PreCompact event (auto compaction only — manual /compact
# is passed through). ALWAYS allows compaction (never blocks). Three-row
# behavior: (1) a fresh active run-state record for this session → checkpoint
# saved, no pending-restore sentinel (the run resumes via its own record);
# (2) skill pidfiles active → checkpoint saved, no sentinel (workflow
# continues uninterrupted); (3) neither → no checkpoint and no sentinel
# (the recent-sessions and telemetry appends still happen on every row), unless
# QUOIN_PRECOMPACT_NORUN_CHECKPOINT=1 restores the checkpoint-plus-sentinel
# for a plain conversation (sessionstart.sh then surfaces the restore banner).
# Fail-OPEN: any error → exit 0 (compaction proceeds unblocked).
#
# Shebang assertion: head -1 ... | grep -qE '^#!/bin/sh( |$)'
# No-args form RECOMMENDED for fail-OPEN hooks (set -e would break fail-OPEN).

# Source shared helper library
. "$(dirname "$0")/_lib.sh" && read_constants

# STEP -1: Capture stdin before any parsing (stdin can only be read once)
STDIN=$(cat)

# STEP 1: Parse trigger field
# If jq is absent, safe_jq_or_passthrough returns non-zero → fail-OPEN
trigger=$(printf '%s' "$STDIN" | jq -r '.trigger // empty' 2>/dev/null) || exit 0

# Manual /compact: pass through immediately — do not block user-initiated compaction
if [ "$trigger" = "manual" ]; then
  exit 0
fi

# Parse remaining fields
session_id=$(printf '%s' "$STDIN" | jq -r '.session_id // empty' 2>/dev/null) || exit 0
cwd=$(printf '%s' "$STDIN" | jq -r '.cwd // empty' 2>/dev/null) || exit 0
[ -z "$cwd" ] && cwd="$PWD"
_ctx_proj="$cwd"
cwd=$(resolve_project_root "$cwd")

# STEP 1b: Recent-session record (append before compaction wipes context)
(
    [ -z "$session_id" ] && exit 0
    _pc_mem="${cwd}/.workflow_artifacts/memory"
    mkdir -p "$_pc_mem" 2>/dev/null || exit 0
    _pc_now=$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null) || _pc_now=$(date +%Y-%m-%dT%H:%M:%SZ)
    printf '%s | %s\n' "$_pc_now" "$session_id" >> "${_pc_mem}/recent-sessions.md" 2>/dev/null || true
) 2>/dev/null || true

transcript_path=$(printf '%s' "$STDIN" | jq -r '.transcript_path // empty' 2>/dev/null) || exit 0

# Override: .allow-compact marker file in cwd
if [ -f "${cwd}/.allow-compact" ]; then
  exit 0
fi

# Require session_id for pending-restore discriminant (CRIT-3 fix)
# Without session_id the sentinel cannot be matched in sessionstart / /checkpoint --restore
if [ -z "$session_id" ]; then
  printf '[quoin-precompact] WARNING: session_id absent from stdin; cannot write session-scoped sentinel; proceeding fail-OPEN\n' >&2
  exit 0
fi

# Pre-compute sentinel path so we can check it before writing a new checkpoint (A3 fix)
MEMORY_DIR="${cwd}/.workflow_artifacts/memory"
pending_restore_file="${MEMORY_DIR}/pending-restore-${session_id}.txt"

# STEP 1c: Session-scoped run-state read (three-row truth-table input).
# Placed after MEMORY_DIR is assigned and before the sentinel pre-check so
# the run-aware steps also run on the early-skip path — a session with a
# voluntary checkpoint sentinel is still live and about to compact.
run_state_file=$(run_state_select "$MEMORY_DIR" "$session_id" 2>/dev/null) || run_state_file=""
run_active=0
rs_task=""; rs_phase=""; rs_subphase=""; rs_step=""
rs_at_stage_boundary=""; rs_next_action=""; rs_notes_path=""
if [ -n "$run_state_file" ]; then
  run_active=1
  # Bind record fields without eval: the record's values are sanitized for
  # quotes, backslashes and control bytes but NOT for shell metacharacters,
  # so they must never pass through eval or an unquoted expansion. The
  # here-doc keeps the read loop in the current shell (a pipe would lose
  # every assignment in a subshell); the case allow-list drops any
  # unexpected key instead of binding it.
  while IFS='=' read -r _rsk _rsv; do
    case "$_rsk" in
      task) rs_task=$_rsv ;;
      phase) rs_phase=$_rsv ;;
      subphase) rs_subphase=$_rsv ;;
      step) rs_step=$_rsv ;;
      at_stage_boundary) rs_at_stage_boundary=$_rsv ;;
      next_action) rs_next_action=$_rsv ;;
      notes_path) rs_notes_path=$_rsv ;;
    esac
  done <<RSEOF
$(run_state_fields "$run_state_file" task phase subphase step at_stage_boundary next_action notes_path)
RSEOF
fi

# STEP 1d: Run-notes append (mid-stage row only) — one block per
# auto-compaction while an active run for this session sits between stage
# boundaries, mirroring the record writer's own notes-block shape plus a
# provenance line. Best-effort: the whole step is a subshell and a notes
# failure never changes the decision or the exit code. No rotation here —
# rotation stays with the record's Python writer.
(
  [ "$run_active" = "1" ] || exit 0
  [ "$rs_at_stage_boundary" = "false" ] || exit 0
  # Containment before any append: notes_path comes from the record and is
  # derived from the WRITER's project root, so nothing constrains it to this
  # hook's own memory dir. Reject traversal first — a POSIX case pattern
  # lets * match /, so the prefix allow-list alone would pass a path that
  # escapes through .. segments — then require the writer's own
  # run-notes-<task>.md naming convention directly under MEMORY_DIR.
  _rn_offered="$rs_notes_path"
  case "$rs_notes_path" in *..*) rs_notes_path="" ;; esac
  case "$rs_notes_path" in "$MEMORY_DIR"/run-notes-*.md) ;; *) rs_notes_path="" ;; esac
  # Flatness check: the prefix pattern above still lets * match /, so a
  # path routed through a subdirectory (e.g. a symlinked dir planted
  # inside MEMORY_DIR) would pass it and escape through the link; after
  # stripping the prefix, no separator may remain.
  case "${rs_notes_path#"$MEMORY_DIR"/}" in */*) rs_notes_path="" ;; esac
  # Each refusal below warns on fd 9 (stderr, saved before this subshell's
  # own 2>/dev/null): a silent refusal would let a planted object disable
  # this sink permanently with no operator-visible signal.
  if [ -z "$rs_notes_path" ]; then
    [ -z "$_rn_offered" ] || printf '[quoin-precompact] WARNING: run-notes path refused (containment); notes append skipped\n' >&9
    exit 0
  fi
  # Best-effort symlink refusal: the record's Python writer refuses a
  # symlink AND opens with O_NOFOLLOW, closing the check-to-write race;
  # this shell check followed by >> reopens that window. Accepted for a
  # bounded hook append — this is NOT parity with the Python writer.
  if [ -L "$rs_notes_path" ]; then
    printf '[quoin-precompact] WARNING: run-notes path refused (symlink); notes append skipped\n' >&9
    exit 0
  fi
  # Non-regular files and hard links: a FIFO (or device/socket) at the
  # notes path would block the >> open itself and stall the hook until the
  # harness kills it — the hook must reach its allow line no matter what is
  # planted here. A hard link passes every check above (no dotdot, prefix
  # match, flat, not a symlink), so refuse a link count above 1 too. The
  # link probe asserts positively: it must SUCCEED and report a link count
  # of 1 — a failed probe reads as "refuse", never as "no extra links".
  if [ -e "$rs_notes_path" ]; then
    if [ ! -f "$rs_notes_path" ]; then
      printf '[quoin-precompact] WARNING: run-notes path refused (non-regular file); notes append skipped\n' >&9
      exit 0
    fi
    if ! _rn_links=$(find "$rs_notes_path" -maxdepth 0 -links +1 2>/dev/null); then
      printf '[quoin-precompact] WARNING: run-notes link probe failed; notes append skipped\n' >&9
      exit 0
    fi
    if [ -n "$_rn_links" ]; then
      printf '[quoin-precompact] WARNING: run-notes path refused (hard link); notes append skipped\n' >&9
      exit 0
    fi
  fi
  _rn_ts=$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null) || _rn_ts=$(date +%Y-%m-%dT%H:%M:%SZ)
  {
    printf '## %s — %s/%s\n' "$_rn_ts" "$rs_phase" "$rs_subphase"
    printf -- '- step: %s\n' "$rs_step"
    printf -- '- next_action: %s\n' "$rs_next_action"
    printf -- '- source: precompact hook (compaction imminent)\n'
    printf '\n'
  } >> "$rs_notes_path" 2>/dev/null
) 9>&2 2>/dev/null || true

# STEP 1e: Telemetry — the "pre" half of a compaction event, appended on
# every row (a plain-conversation compaction is still a data point for
# compaction frequency; run fields are empty strings when no run is
# active). The sink lives under telemetry/ so the depth-1 sentinel sweeps
# never see it. Best-effort: a telemetry failure never changes the
# decision or the exit code. Rotation fires here, at the pair boundary,
# immediately before this pre is appended — see D-01 in the stage plan:
# every earlier event's pre/post are already complete pairs on disk at
# that instant, so a rotation here always moves whole pairs, never a
# split one.
(
  _tel_dir="${MEMORY_DIR}/telemetry"
  _tel_sink="${_tel_dir}/compaction-events.jsonl"
  mkdir -p "$_tel_dir" 2>/dev/null || {
    printf '[quoin-precompact] WARNING: cannot create telemetry dir; append skipped\n' >&9
    exit 0
  }
  # Refuse a symlinked dir or sink before appending: mkdir -p succeeds
  # through a symlinked directory, and >> would follow a planted link —
  # mirrors the notes-path symlink refusal above (same bounded
  # check-then-write caveat). Refusals warn on fd 9 (stderr, saved before
  # this subshell's own 2>/dev/null) so a planted object cannot disable
  # the sink silently.
  if [ -L "$_tel_dir" ] || [ -L "$_tel_sink" ]; then
    printf '[quoin-precompact] WARNING: telemetry sink refused (symlink); append skipped\n' >&9
    exit 0
  fi
  # Same non-regular-file discipline as the notes path: require a real
  # directory and a regular-or-absent sink, and refuse a hard-linked sink —
  # a FIFO here would hang the append and with it the whole hook. The link
  # probe asserts positively: it must SUCCEED and report a link count of 1.
  if [ ! -d "$_tel_dir" ]; then
    printf '[quoin-precompact] WARNING: telemetry dir refused (not a directory); append skipped\n' >&9
    exit 0
  fi
  if [ -e "$_tel_sink" ]; then
    if [ ! -f "$_tel_sink" ]; then
      printf '[quoin-precompact] WARNING: telemetry sink refused (non-regular file); append skipped\n' >&9
      exit 0
    fi
    if ! _tel_links=$(find "$_tel_sink" -maxdepth 0 -links +1 2>/dev/null); then
      printf '[quoin-precompact] WARNING: telemetry link probe failed; append skipped\n' >&9
      exit 0
    fi
    if [ -n "$_tel_links" ]; then
      printf '[quoin-precompact] WARNING: telemetry sink refused (hard link); append skipped\n' >&9
      exit 0
    fi
  fi
  # Rotate at the pair boundary: every earlier event's pre and post are
  # already on disk as complete pairs by the time this pre is about to be
  # written, so the rename moves whole pairs (mirrors
  # run_state.py::_append_notes's rotate-before-append shape — unlink the
  # rotation target before the rename, never mv over a directory symlink).
  _tel_max=${TELEMETRY_MAX_BYTES:-1048576}
  case "$_tel_max" in ''|*[!0-9]*) _tel_max=1048576 ;; esac
  if [ -f "$_tel_sink" ]; then
    _tel_size=$(wc -c < "$_tel_sink" 2>/dev/null | awk '{print $1}') || _tel_size=0
    case "$_tel_size" in ''|*[!0-9]*) _tel_size=0 ;; esac
    if [ "$_tel_size" -gt "$_tel_max" ]; then
      if [ -e "${_tel_sink}.1" ] || [ -L "${_tel_sink}.1" ]; then
        if [ -L "${_tel_sink}.1" ]; then
          rm -f "${_tel_sink}.1" 2>/dev/null
        fi
        if [ -e "${_tel_sink}.1" ] && [ ! -f "${_tel_sink}.1" ]; then
          printf '[quoin-precompact] WARNING: telemetry rotation target refused (non-regular); append skipped\n' >&9
          exit 0
        fi
      fi
      if ! mv -f "$_tel_sink" "${_tel_sink}.1" 2>/dev/null; then
        printf '[quoin-precompact] WARNING: telemetry rotation failed; append skipped\n' >&9
        exit 0
      fi
    fi
  fi
  # Probe the session id through the same jq encoder that builds the line,
  # so the sequence count matches the escaped form as it actually appears
  # in the sink — a raw id containing a quote or backslash never matches
  # its own escaped form, and a fixed-string grep avoids regex semantics.
  _tel_esc=$(jq -nc --arg s "$session_id" '$s' 2>/dev/null) || exit 0
  _tel_seq=0
  if [ -f "$_tel_sink" ]; then
    # Bounded count: the sink is append-only and unrotated, so count within
    # the last 1 MiB only — an oversized sink cannot burn the hook's time
    # budget on a full rescan. event_seq is therefore monotonic per session
    # only WITHIN that tail window and WITHIN the current rotation
    # generation: once a session's earlier rows scroll past the tail
    # window, OR the sink rotates (above), the count restarts from 0. A
    # consumer needing a unique key uses (session_id, event_seq) paired
    # within a single source file, never event_seq alone.
    _tel_seq=$(tail -c 1048576 "$_tel_sink" 2>/dev/null | grep -F "\"session_id\":$_tel_esc" 2>/dev/null | grep -cF '"half":"pre"' 2>/dev/null) || _tel_seq=0
    case "$_tel_seq" in ''|*[!0-9]*) _tel_seq=0 ;; esac
  fi
  _tel_bytes=""
  _tel_est=""
  if [ -n "$transcript_path" ] && [ -r "$transcript_path" ]; then
    _tel_bytes=$(wc -c < "$transcript_path" 2>/dev/null | awk '{print $1}') || _tel_bytes=""
    if [ -n "$_tel_bytes" ]; then
      _tel_est=$(awk -v b="$_tel_bytes" -v bpt="${BPT:-8.0}" 'BEGIN{ printf "%d", b / bpt }' 2>/dev/null) || _tel_est=""
    fi
  fi
  # Real token reading (best-effort): the value is recorded only when it came
  # from transcript usage; any failure leaves the fields null.
  _tel_rt=""; _tel_rw=""; _tel_src=""
  if [ -n "$transcript_path" ] && [ -r "$transcript_path" ]; then
    _tel_rd=$(compute_context_reading "$transcript_path" "$_ctx_proj" --kind 2>/dev/null) || _tel_rd=""
    _tel_rbps=""; _tel_rsrc=""; _tel_rtok=""; _tel_rwin=""; _tel_rkind=""
    read -r _tel_rbps _tel_rsrc _tel_rtok _tel_rwin _tel_rkind <<TELEOF
$_tel_rd
TELEOF
    if [ "$_tel_rsrc" = "real" ]; then
      case "$_tel_rtok$_tel_rwin" in
        ''|*[!0-9]*) ;;
        *) _tel_rt="$_tel_rtok"; _tel_rw="$_tel_rwin" ;;
      esac
    fi
  fi
  if [ -n "$_tel_rt" ]; then _tel_src="real"; elif [ -n "$_tel_est" ]; then _tel_src="estimated"; fi
  _tel_ts=$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null) || _tel_ts=$(date +%Y-%m-%dT%H:%M:%SZ)
  jq -nc --arg half "pre" --arg sid "$session_id" --arg seq "$_tel_seq" \
    --arg ts "$_tel_ts" --arg bb "$_tel_bytes" --arg etb "$_tel_est" \
    --arg rtb "$_tel_rt" --arg rw "$_tel_rw" --arg rsrc "$_tel_src" \
    --arg task "$rs_task" --arg phase "$rs_phase" --arg subphase "$rs_subphase" \
    --arg step "$rs_step" \
    '{v: 1, half: $half, session_id: $sid, event_seq: ($seq|tonumber), ts: $ts,
      bytes_before: (if $bb == "" then null else ($bb|tonumber) end),
      est_tokens_before: (if $etb == "" then null else ($etb|tonumber) end),
      real_tokens_before: (if $rtb == "" then null else ($rtb|tonumber) end),
      real_window: (if $rw == "" then null else ($rw|tonumber) end),
      reading_source: (if $rsrc == "" then null else $rsrc end),
      task: $task, phase: $phase, subphase: $subphase, step: $step}' \
    >> "$_tel_sink" 2>/dev/null || exit 0
) 9>&2 2>/dev/null || true

# Pre-initialize pidfile_info to "none" so STEP 4 branching is safe in the early-skip path.
# The early-skip path (sentinel already exists) skips the else block entirely, so pidfile
# detection inside the else block never runs — pidfile_info stays "none". This is the
# intentional conservative behavior: if the user ran /checkpoint manually, they are
# managing the session themselves. The else block overwrites this "none" if pidfiles
# are found.
pidfile_info="none"
early_skip=0

# STEP 2: Save checkpoint state (paths-not-content rule)
# Skip entirely if voluntary /checkpoint already ran this session — preserve the better sentinel
# and avoid creating an orphaned -precompact.md file (A3 fix)
if [ -f "$pending_restore_file" ]; then
  printf '[quoin-precompact] INFO: pending-restore sentinel already exists (voluntary /checkpoint was run earlier in this session); skipping checkpoint write to avoid orphaned -precompact.md file\n' >&2
  early_skip=1
else

# Determine active task name from session-state filenames (best-effort).
# Hoisted above the checkpoint-write gate: pidfile presence is one of the
# gate's inputs, so it must be known before deciding whether to write.
session_state_dir="${cwd}/.workflow_artifacts/memory/sessions"
active_task="unknown-task"
latest_session=""
if [ -d "$session_state_dir" ]; then
  # Most recently modified session state file (ls -t) — mtime-most-recent
  latest_session=$(ls -t "$session_state_dir"/*.md 2>/dev/null | head -1)
  if [ -n "$latest_session" ]; then
    # Extract task name from filename pattern: YYYY-MM-DD-<task-name>.md
    session_base=$(basename "$latest_session" .md)
    # Strip leading date prefix (YYYY-MM-DD-)
    active_task=$(printf '%s' "$session_base" | sed 's/^[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]-//')
  fi
fi

# Collect active pidfiles (overwrite pre-initialized "none" if any are found)
if ls "$session_state_dir"/*.pidfile.lock > /dev/null 2>&1; then
  pidfile_info=$(ls "$session_state_dir"/*.pidfile.lock 2>/dev/null | xargs -I{} basename {} 2>/dev/null | tr '\n' ' ' | sed 's/ $//')
  [ -z "$pidfile_info" ] && pidfile_info="none"
fi

# Checkpoint-write gate (three-row truth table): rows 1 and 2 (active run
# for this session, or skill pidfiles present) write the checkpoint; row 3
# (plain conversation) writes nothing unless the opt-in knob is set.
if [ "$run_active" = "1" ] || [ "$pidfile_info" != "none" ] || [ "${PRECOMPACT_NORUN_CHECKPOINT:-0}" = "1" ]; then

CHECKPOINT_DIR="${cwd}/.workflow_artifacts/memory/checkpoints"
mkdir -p "$CHECKPOINT_DIR" 2>/dev/null || {
  printf '[quoin-precompact] WARNING: cannot create checkpoint dir; falling back fail-OPEN\n' >&2
  exit 0
}

checkpoint_date=$(date -u +%Y-%m-%d 2>/dev/null) || checkpoint_date="unknown-date"

# Determine current git branch (best-effort)
current_branch="unknown"
if command -v git > /dev/null 2>&1; then
  branch_out=$(git -C "$cwd" rev-parse --abbrev-ref HEAD 2>/dev/null) && current_branch="$branch_out"
fi

# Find most recent plan, architecture, critic-response, and review files
# Uses find -exec ls -t {} + for mtime sort — whitespace-safe (no word-splitting on paths)
# and no BSD-xargs empty-stdin hazard (A1 fix)
current_plan_path="(none found)"
architecture_path="(none found)"
latest_critic="(none found)"
latest_review="(none found)"
if [ -d "${cwd}/.workflow_artifacts" ]; then
  found_plan=$(find "${cwd}/.workflow_artifacts" -name "current-plan.md" \
    -exec ls -t {} + 2>/dev/null | head -1)
  [ -n "$found_plan" ] && current_plan_path="$found_plan"
  found_arch=$(find "${cwd}/.workflow_artifacts" -name "architecture.md" \
    -exec ls -t {} + 2>/dev/null | head -1)
  [ -n "$found_arch" ] && architecture_path="$found_arch"
  c=$(find "${cwd}/.workflow_artifacts" -name "critic-response-*.md" \
    -exec ls -t {} + 2>/dev/null | head -1)
  [ -n "$c" ] && latest_critic="$c"
  r=$(find "${cwd}/.workflow_artifacts" -name "review-*.md" \
    -exec ls -t {} + 2>/dev/null | head -1)
  [ -n "$r" ] && latest_review="$r"
fi

# Extract current phase from session-state ## Cost block (A2 fix)
# Handles bulleted format: - Phase: implement
current_phase="unknown"
if [ -n "$latest_session" ] && [ -f "$latest_session" ]; then
  current_phase=$(awk '/^## Cost/{f=1;next} f && /^[[:space:]]*-?[[:space:]]*Phase:/{sub(/^[[:space:]]*-?[[:space:]]*Phase:[[:space:]]*/,""); print; exit} f && /^## /{exit}' \
    "$latest_session" 2>/dev/null)
  [ -z "$current_phase" ] && current_phase="unknown"
fi

# Extract open questions and unfinished work from session-state (A2 fix)
# Guards: /^## / exits section; /^pollution_score:/ prevents trailing score line from leaking
open_questions=""
if [ -n "$latest_session" ] && [ -f "$latest_session" ]; then
  block=$(awk '/^## Open questions/{f=1;next} /^## /{if(f)exit} /^pollution_score:/{if(f)exit} f{print}' \
    "$latest_session" 2>/dev/null | sed '/^[[:space:]]*$/d')
  [ -n "$block" ] && open_questions="$block"
fi

unfinished_work=""
if [ -n "$latest_session" ] && [ -f "$latest_session" ]; then
  block=$(awk '/^## Unfinished work/{f=1;next} /^## /{if(f)exit} /^pollution_score:/{if(f)exit} f{print}' \
    "$latest_session" 2>/dev/null | sed '/^[[:space:]]*$/d')
  [ -n "$block" ] && unfinished_work="$block"
fi

checkpoint_file="${CHECKPOINT_DIR}/${checkpoint_date}-${active_task}-precompact.md"

# Write checkpoint (paths-not-content — never carry file contents)
# User-content fields (open_questions, unfinished_work) use placeholders to prevent
# shell expansion of $varname, backticks, or ${...} from session-state free-form text (A2 fix)
cat > "$checkpoint_file" 2>/dev/null << CPEOF
## Status
precompact-hook save (auto-compaction intercepted)

## Current stage
${current_phase}

## Active task
${active_task}

## Branch
${current_branch}

## Session ID
${session_id}

## Trigger
auto (active pidfiles at save time: ${pidfile_info})

## Active skills (pidfiles)
${pidfile_info}

## In-flight artifacts
- current-plan.md: ${current_plan_path}
- architecture.md: ${architecture_path}
- latest critic-response: ${latest_critic}
- latest review: ${latest_review}
- session-state: ${latest_session:-"(none found)"}
- transcript: ${transcript_path}

## Open questions
__OPEN_QUESTIONS_PLACEHOLDER__

## Unfinished work
__UNFINISHED_WORK_PLACEHOLDER__

## Restore hint
Run /checkpoint --restore in a fresh session to resume task '${active_task}' from branch '${current_branch}'.
CPEOF

# Substitute user-content placeholders via awk -v (no shell expansion of user content)
# Primary fix: atomic write with visible-failure discipline — no '|| true' swallowing.
# On any fill failure the heredoc-written file (with placeholders) is RETAINED (D-06):
# better to have a visible placeholder than to lose the checkpoint entirely.
_oq="${open_questions:-(none)}"
_uw="${unfinished_work:-(none)}"

awk -v oq="$_oq" -v uw="$_uw" '
  /^__OPEN_QUESTIONS_PLACEHOLDER__$/{print (oq=="" ? "(none)" : oq); next}
  /^__UNFINISHED_WORK_PLACEHOLDER__$/{print (uw=="" ? "(none)" : uw); next}
  {print}
' "$checkpoint_file" > "${checkpoint_file}.tmp" 2>/dev/null
_awk_exit=$?

if [ $_awk_exit -eq 0 ] && [ -s "${checkpoint_file}.tmp" ]; then
  # Temp file is non-empty and awk succeeded — rename with explicit error capture
  if mv -f "${checkpoint_file}.tmp" "$checkpoint_file" 2>/dev/null; then
    # SUCCESS: verify no placeholders remain (anchored grep)
    if grep -qE '^(__OPEN_QUESTIONS_PLACEHOLDER__|__UNFINISHED_WORK_PLACEHOLDER__)$' \
        "$checkpoint_file" 2>/dev/null; then
      # Rare: placeholders still in file after mv (content-check failure)
      printf '[quoin-precompact] WARNING: placeholder substitution incomplete in %s; checkpoint retained with placeholders (better than losing it)\n' "$checkpoint_file" >&2
      # Do NOT trash-move — user keeps the file, placeholders are visible
    fi
    # FALL THROUGH: success path, checkpoint_file is valid
  else
    # mv failed — clean up temp, warn, but KEEP the heredoc-written file with placeholders
    _mv_exit=$?
    printf '[quoin-precompact] WARNING: mv failed (exit %d) renaming %s; checkpoint retained with placeholder tokens\n' \
      "$_mv_exit" "${checkpoint_file}.tmp" >&2
    rm -f "${checkpoint_file}.tmp" 2>/dev/null || true
    # checkpoint_file remains as-is (with placeholders) — sentinel write still runs below
  fi
else
  # awk produced empty output or failed — compute tmp size safely
  _tmp_size=$(wc -c < "${checkpoint_file}.tmp" 2>/dev/null || echo 0)
  _tmp_size=${_tmp_size:-0}
  printf '[quoin-precompact] WARNING: placeholder fill failed (awk exit %d, tmp size %s); checkpoint retained with placeholder tokens\n' \
    "$_awk_exit" "$_tmp_size" >&2
  rm -f "${checkpoint_file}.tmp" 2>/dev/null || true
  # checkpoint_file remains as-is — sentinel write still runs below
fi

# Check if write succeeded
if [ ! -f "$checkpoint_file" ]; then
  printf '[quoin-precompact] WARNING: checkpoint write failed; proceeding fail-OPEN\n' >&2
  exit 0
fi

fi  # end: checkpoint-write gate

fi  # end: if [ ! -f "$pending_restore_file" ]

# STEP 4: Always allow. Stderr names the row that fired; the pending-restore
# sentinel is written ONLY on the no-run/no-pidfile row when
# QUOIN_PRECOMPACT_NORUN_CHECKPOINT=1 opted back in, and only when this
# invocation actually wrote a checkpoint — on the early-skip path
# checkpoint_file stays unset, so the existing sentinel is never overwritten.
#
# KNOWN LIMITATION: stale pidfiles
# The pidfile liveness check is NOT performed. If a skill crashed without releasing its
# .pidfile.lock file, pidfile_info will be non-"none" and the hook keeps writing a
# checkpoint with no sentinel. Rationale: liveness checking (kill -0 <pid>) requires
# parsing the PID from the filename, a POSIX loop, and fragile format coupling. The
# crash-without-cleanup failure mode is rare and bounded. To clean up after a crash:
# rm .workflow_artifacts/memory/sessions/*.pidfile.lock
# The early-skip path never assigns checkpoint_file; phrase that case
# explicitly instead of logging "checkpoint saved at none".
if [ -n "${checkpoint_file:-}" ]; then
  cp_note="checkpoint saved at ${checkpoint_file}"
else
  cp_note="checkpoint write skipped (voluntary /checkpoint sentinel already present)"
fi
if [ "$run_active" = "1" ]; then
  printf '[quoin-precompact] INFO: active run detected (task: %s); allowing auto-compaction; %s\n' "${rs_task:-unknown}" "$cp_note" >&2
elif [ "$pidfile_info" != "none" ]; then
  printf '[quoin-precompact] INFO: skills running (%s); allowing auto-compaction; %s\n' "$pidfile_info" "$cp_note" >&2
elif [ "${PRECOMPACT_NORUN_CHECKPOINT:-0}" = "1" ] && [ -n "${checkpoint_file:-}" ]; then
  mkdir -p "$MEMORY_DIR" 2>/dev/null || true
  printf '%s\n' "$checkpoint_file" > "$pending_restore_file" 2>/dev/null || {
    printf '[quoin-precompact] WARNING: cannot write pending-restore sentinel; sessionstart hook cannot surface restore banner\n' >&2
  }
  printf '[quoin-precompact] INFO: no active run-state record and no pidfiles; QUOIN_PRECOMPACT_NORUN_CHECKPOINT=1 set — checkpoint saved at %s; pending-restore sentinel written\n' "$checkpoint_file" >&2
elif [ "$early_skip" = "1" ]; then
  # Voluntary checkpoint sentinel found earlier: nothing new was written,
  # and pidfile collection never ran on this path, so claim neither — and
  # do not suggest a knob that changes nothing here.
  printf '[quoin-precompact] INFO: voluntary checkpoint sentinel already present; allowing auto-compaction; nothing new written (pidfile state not collected on this path)\n' >&2
else
  printf '[quoin-precompact] INFO: no active run-state record and no pidfiles; allowing auto-compaction; nothing written (set QUOIN_PRECOMPACT_NORUN_CHECKPOINT=1 to restore the no-run checkpoint and sentinel)\n' >&2
fi
printf '{"decision": "allow"}\n'
