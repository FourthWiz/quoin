#!/bin/sh
# precompact.sh — PreCompact hook for quoin workflow isolation
# Deployed to ~/.claude/hooks/ by bash install.sh
#
# Contract: fires on PreCompact event (auto compaction only — manual /compact
# is passed through). ALWAYS allows compaction (never blocks). On every auto
# compaction it appends the session to recent-sessions.md, appends a run-notes
# block while an active run sits between stage boundaries, and appends the
# "pre" half of a compaction telemetry event. It writes no checkpoint file and
# no pending-restore sentinel: a run resumes from its own run-state record and
# the SessionStart compact re-entry, and native auto-compaction carries the rest.
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

# The run-state lookup below is keyed on the session id; without one there is
# nothing to match, so the remaining steps are skipped.
[ -z "$session_id" ] && exit 0

MEMORY_DIR="${cwd}/.workflow_artifacts/memory"

# STEP 1c: Session-scoped run-state read.
# Placed after MEMORY_DIR is assigned.
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

# Always allow. Stderr names whether an active run was seen; nothing else is written.
if [ "$run_active" = "1" ]; then
  printf '[quoin-precompact] INFO: active run detected (task: %s); allowing auto-compaction; nothing else written\n' "${rs_task:-unknown}" >&2
else
  printf '[quoin-precompact] INFO: no active run-state record; allowing auto-compaction; nothing else written\n' >&2
fi
printf '{"decision": "allow"}\n'
