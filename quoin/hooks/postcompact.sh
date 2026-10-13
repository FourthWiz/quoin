#!/bin/sh
# postcompact.sh — PostCompact hook for quoin workflow
# Deployed to ~/.claude/hooks/ by bash install.sh
# Fires after auto compaction completes. No decision control — side effects only.
# Writes a postcompact-reset-${session_id}.txt sentinel so userpromptsubmit.sh
# STEP 0.5 can detect compaction and trash-move the sentinel on the next prompt submit.
# Fail-OPEN: any error → exit 0. No stdout output.
#
# Shebang assertion: head -1 ... | grep -qE '^#!/bin/sh( |$)'

# STEP -1: Capture stdin before any parsing (stdin can only be read once)
STDIN=$(cat)
. "$(dirname "$0")/_lib.sh" 2>/dev/null || true
read_constants 2>/dev/null || true

# Parse fields from JSON (same pattern as precompact.sh lines 17-38)
session_id=$(printf '%s' "$STDIN" | jq -r '.session_id // empty' 2>/dev/null) || exit 0
cwd=$(printf '%s' "$STDIN" | jq -r '.cwd // empty' 2>/dev/null) || exit 0
transcript_path=$(printf '%s' "$STDIN" | jq -r '.transcript_path // empty' 2>/dev/null) || exit 0

# Guard: session_id required for a usable sentinel filename
[ -z "$session_id" ] && exit 0
[ -z "$cwd" ] && cwd="$PWD"
_ctx_proj="$cwd"
cwd=$(resolve_project_root "$cwd")

MEMORY_DIR="${cwd}/.workflow_artifacts/memory"
[ -d "$MEMORY_DIR" ] || exit 0

SENTINEL="${MEMORY_DIR}/postcompact-reset-${session_id}.txt"

compacted_at=$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null) || compacted_at="unknown"

transcript_bytes_after=0
if [ -n "$transcript_path" ] && [ -r "$transcript_path" ]; then
    transcript_bytes_after=$(wc -c < "$transcript_path" 2>/dev/null | awk '{print $1}') || transcript_bytes_after=0
fi
transcript_bytes_after=${transcript_bytes_after:-0}

printf 'compacted_at=%s\nsession_id=%s\ntranscript_path=%s\ntranscript_bytes_after=%s\n' \
    "$compacted_at" "$session_id" "$transcript_path" "$transcript_bytes_after" \
    > "$SENTINEL" 2>/dev/null || true

COMPACT_HAPPENED_SENTINEL="${MEMORY_DIR}/compact-happened-${session_id}.txt"
printf 'compacted_at=%s\nsession_id=%s\n' "$compacted_at" "$session_id" \
    > "$COMPACT_HAPPENED_SENTINEL" 2>/dev/null || true

# STEP 3: Telemetry — the "post" half of a compaction event, appended after
# both sentinels above. Best-effort: a telemetry failure never affects the
# sentinels or the exit code. The .allow-compact marker suppresses this
# append only (both sentinels above are still written) — unlike
# precompact.sh, where the same marker skips the whole hook.
(
  [ -f "${cwd}/.allow-compact" ] && exit 0
  _tel_dir="${MEMORY_DIR}/telemetry"
  _tel_sink="${_tel_dir}/compaction-events.jsonl"
  mkdir -p "$_tel_dir" 2>/dev/null || {
    printf '[quoin-postcompact] WARNING: cannot create telemetry dir; append skipped\n' >&9
    exit 0
  }
  if [ -L "$_tel_dir" ] || [ -L "$_tel_sink" ]; then
    printf '[quoin-postcompact] WARNING: telemetry sink refused (symlink); append skipped\n' >&9
    exit 0
  fi
  if [ ! -d "$_tel_dir" ]; then
    printf '[quoin-postcompact] WARNING: telemetry dir refused (not a directory); append skipped\n' >&9
    exit 0
  fi
  if [ -e "$_tel_sink" ]; then
    if [ ! -f "$_tel_sink" ]; then
      printf '[quoin-postcompact] WARNING: telemetry sink refused (non-regular file); append skipped\n' >&9
      exit 0
    fi
    if ! _tel_links=$(find "$_tel_sink" -maxdepth 0 -links +1 2>/dev/null); then
      printf '[quoin-postcompact] WARNING: telemetry link probe failed; append skipped\n' >&9
      exit 0
    fi
    if [ -n "$_tel_links" ]; then
      printf '[quoin-postcompact] WARNING: telemetry sink refused (hard link); append skipped\n' >&9
      exit 0
    fi
  fi
  # Parse trigger and summary length inside the subshell so a jq failure
  # cannot abort the hook or endanger the two sentinels above (those
  # top-level parses use `|| exit 0`; these must not). The summary text
  # itself is never bound to a shell variable and never written — only
  # its length leaves jq.
  _tel_trigger=$(printf '%s' "$STDIN" | jq -r '.trigger // empty' 2>/dev/null) || _tel_trigger=""
  _tel_cslen=$(printf '%s' "$STDIN" | jq -r 'if (.compact_summary|type)=="string" then (.compact_summary|length) else empty end' 2>/dev/null) || _tel_cslen=""
  case "$_tel_cslen" in ''|*[!0-9]*) _tel_cslen="" ;; esac
  # event_seq: take the highest event_seq on a "half":"pre" line for this
  # session in the live sink's last 1 MiB, but only if it is strictly
  # ahead of the highest "half":"post" line for the same session in the
  # same window — a pre no more recent than the last recorded post did
  # not belong to this compaction, and adopting it would synthesise a
  # false pair (D-06).
  _tel_esc=$(jq -nc --arg s "$session_id" '$s' 2>/dev/null) || exit 0
  _tel_seq=""
  if [ -f "$_tel_sink" ]; then
    _tel_window=$(tail -c 1048576 "$_tel_sink" 2>/dev/null | grep -F "\"session_id\":$_tel_esc" 2>/dev/null) || _tel_window=""
    _tel_max_pre=$(printf '%s\n' "$_tel_window" | grep -F '"half":"pre"' 2>/dev/null \
      | jq -Rr 'fromjson? | .event_seq | numbers' 2>/dev/null | sort -n | tail -1) || _tel_max_pre=""
    case "$_tel_max_pre" in ''|*[!0-9]*) _tel_max_pre="" ;; esac
    _tel_max_post=$(printf '%s\n' "$_tel_window" | grep -F '"half":"post"' 2>/dev/null \
      | jq -Rr 'fromjson? | .event_seq | numbers' 2>/dev/null | sort -n | tail -1) || _tel_max_post=""
    case "$_tel_max_post" in ''|*[!0-9]*) _tel_max_post="" ;; esac
    if [ -n "$_tel_max_pre" ]; then
      if [ -z "$_tel_max_post" ] || [ "$_tel_max_pre" -gt "$_tel_max_post" ]; then
        _tel_seq="$_tel_max_pre"
      fi
    fi
  fi
  # Computed exactly as the pre half's own _tel_ts/_tel_bytes/_tel_est —
  # a separate _tel_ba that stays empty when the transcript is unreadable
  # (the sentinel's own transcript_bytes_after is 0-defaulted above and
  # would misreport an unreadable transcript as a genuine zero-byte one).
  _tel_ts=$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null) || _tel_ts=$(date +%Y-%m-%dT%H:%M:%SZ)
  _tel_ba=""
  if [ -n "$transcript_path" ] && [ -r "$transcript_path" ]; then
    _tel_ba=$(wc -c < "$transcript_path" 2>/dev/null | awk '{print $1}') || _tel_ba=""
  fi
  _tel_est=""
  if [ -n "$_tel_ba" ]; then
    _tel_est=$(awk -v b="$_tel_ba" -v bpt="${BPT:-8.0}" 'BEGIN{ printf "%d", b / bpt }' 2>/dev/null) || _tel_est=""
  fi
  # Real token reading (best-effort): the value is recorded only when it came
  # from transcript usage and the newest reading is a compact boundary; any failure leaves the fields null.
  _tel_rt=""; _tel_rw=""; _tel_src=""
  if [ -n "$transcript_path" ] && [ -r "$transcript_path" ]; then
    _tel_rd=$(compute_context_reading "$transcript_path" "$_ctx_proj" --kind 2>/dev/null) || _tel_rd=""
    _tel_rbps=""; _tel_rsrc=""; _tel_rtok=""; _tel_rwin=""; _tel_rkind=""
    read -r _tel_rbps _tel_rsrc _tel_rtok _tel_rwin _tel_rkind <<TELEOF
$_tel_rd
TELEOF
    if [ "$_tel_rsrc" = "real" ] && [ "$_tel_rkind" = "compact" ]; then
      case "$_tel_rtok$_tel_rwin" in
        ''|*[!0-9]*) ;;
        *) _tel_rt="$_tel_rtok"; _tel_rw="$_tel_rwin" ;;
      esac
    fi
  fi
  if [ -n "$_tel_rt" ]; then _tel_src="real"; elif [ -n "$_tel_est" ]; then _tel_src="estimated"; fi
  jq -nc --arg half "post" --arg sid "$session_id" --arg seq "$_tel_seq" \
    --arg ts "$_tel_ts" --arg ba "$_tel_ba" --arg eta "$_tel_est" \
    --arg rta "$_tel_rt" --arg rw "$_tel_rw" --arg rsrc "$_tel_src" \
    --arg trig "$_tel_trigger" --arg cslen "$_tel_cslen" \
    '{v: 1, half: $half, session_id: $sid,
      event_seq: (if $seq == "" then null else ($seq|tonumber) end),
      ts: $ts,
      bytes_after: (if $ba == "" then null else ($ba|tonumber) end),
      est_tokens_after: (if $eta == "" then null else ($eta|tonumber) end),
      real_tokens_after: (if $rta == "" then null else ($rta|tonumber) end),
      real_window: (if $rw == "" then null else ($rw|tonumber) end),
      reading_source: (if $rsrc == "" then null else $rsrc end),
      trigger: $trig,
      compact_summary_len: (if $cslen == "" then null else ($cslen|tonumber) end)}' \
    >> "$_tel_sink" 2>/dev/null || exit 0
) 9>&2 2>/dev/null || true

exit 0
