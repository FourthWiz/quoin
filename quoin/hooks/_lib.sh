#!/bin/sh
# _lib.sh — shared helper library for quoin hook scripts
# Sourced by userpromptsubmit.sh, precompact.sh, sessionstart.sh via:
#   . "$(dirname "$0")/_lib.sh"
#
# NOTE: This file is sourced, not executed — the shebang is a cosmetic uniformity
# hint for editor highlighting and CI uniformity, not execution semantics.
# Per MIN-4: sourced-file invariant is cosmetic; assertion is:
#   head -1 quoin/hooks/_lib.sh | grep -qE '^#!/bin/sh( |$)'
#
# FAIL-OPEN contract: every helper returns non-zero on failure so callers can
# exit 0 (no JSON output, no block). Do NOT use `set -e` in this file —
# set -e would propagate errors past fail-OPEN catch points in callers.
# If defensive scripting is desired, prefer per-statement `|| true` over set -e.

# SINGLE SOURCE OF TRUTH for the 9 sentinel families.
# Consumed by sessionstart.sh STEP 2. cleanup/SKILL.md and sleep/SKILL.md document
# the same list and MUST stay byte-identical (drift test: test_sentinel_family_parity.py).
#
# sentinel_globs — echo the 9 canonical family globs, one per line (no quoting).
# Order matches the cleanup allow-list order.
sentinel_globs() {
    printf '%s\n' \
        'pending-restore-*.txt' \
        'pending-prompt-*.txt' \
        'compact-happened-*.txt' \
        'mid-agent-handoff-*.txt' \
        'pending-resume-ref-*.txt' \
        'checkpoint-defer-*.txt' \
        'postcompact-reset-*.txt' \
        'checkpoint-pending-compact-*.txt' \
        'idle-advisory-pending-*.txt'  # no hook writes this family any more; leftovers from older installs stay reclaimable
}

# read_constants — sources env-var defaults for tunable constants.
# After calling, the following are exported:
#   BPT            — bytes per token for the byte-estimate fallback (e.g., "8.0")
#   LIMIT          — explicit window override on the real path; estimate denominator (150000 when unset)
#   STOP_BPS       — stop/advisory threshold in basis-points (e.g., 7000)
#   BLOCK_BPS      — block threshold in basis-points (e.g., 9500)
#   STALE_DAYS     — sentinel staleness threshold in days (e.g., 7)
#   SESSIONSTART_SWEEP_DAYS — UUID-aware tight sweep window for sessionstart (default 1)
#   COMPACT_FIRST_BPS — /checkpoint high-util notice tier (e.g., 9000 = 90.00%)
#   PANIC_BPS      — /checkpoint panic/degraded-save tier (default 10000 = 100.00%)
#                    compute_utilization is unclamped so values >10000 are normal;
#                    PANIC_BPS=10000 correctly fires for all true overflow (>=100%).
#   DISCOVERY_STALE_DAYS — discovery-file staleness threshold in days (default 7)
#   SERENA_STALE_DAYS — Serena re-onboarding staleness threshold in days (default 30)
#   RUN_STATE_STALE_DAYS — run-state record freshness window in days (default 1)
#   PRECOMPACT_NORUN_CHECKPOINT — opt-in no-run checkpoint+sentinel row (default 0)
#   TELEMETRY_MAX_BYTES — compaction-telemetry sink rotation size in bytes (default 1048576)
read_constants() {
    BPT=${QUOIN_BYTES_PER_TOKEN:-8.0}
    LIMIT=${QUOIN_EFFECTIVE_CONTEXT_LIMIT:-150000}
    STOP_BPS=${QUOIN_STOP_BPS:-7000}
    BLOCK_BPS=${QUOIN_BLOCK_BPS:-9500}
    STALE_DAYS=${QUOIN_STALE_SENTINEL_DAYS:-7}
    SESSIONSTART_SWEEP_DAYS=${QUOIN_SESSIONSTART_SWEEP_DAYS:-1}
    COMPACT_FIRST_BPS=${QUOIN_COMPACT_FIRST_BPS:-9000}
    PANIC_BPS=${QUOIN_PANIC_BPS:-10000}
    DISCOVERY_STALE_DAYS=${QUOIN_DISCOVERY_STALE_DAYS:-7}
    SERENA_STALE_DAYS=${QUOIN_SERENA_STALE_DAYS:-30}
    RUN_STATE_STALE_DAYS=${QUOIN_RUN_STATE_STALE_DAYS:-1}
    PRECOMPACT_NORUN_CHECKPOINT=${QUOIN_PRECOMPACT_NORUN_CHECKPOINT:-0}
    TELEMETRY_MAX_BYTES=${QUOIN_TELEMETRY_MAX_BYTES:-1048576}
    export BPT LIMIT STOP_BPS BLOCK_BPS STALE_DAYS SESSIONSTART_SWEEP_DAYS COMPACT_FIRST_BPS PANIC_BPS DISCOVERY_STALE_DAYS SERENA_STALE_DAYS RUN_STATE_STALE_DAYS PRECOMPACT_NORUN_CHECKPOINT TELEMETRY_MAX_BYTES
}

# compute_utilization <transcript_path> [project_root] — returns a basis-point
# INTEGER representing the utilization of the model's context window.
# Example: returns "8540" for 85.40% utilization. Unclamped, so values above
# 10000 are normal on overflow.
#
# Thin wrapper over compute_context_reading (below): the number is read from the
# newest assistant usage record in the transcript tail and divided by the model's
# real context window. When no usage can be read (no jq, empty or non-JSONL file)
# it falls back to the byte estimate in _ctx_estimate.
#
# Returns non-zero if transcript_path is empty or file is unreadable.
compute_utilization() {
    _cu_out=$(compute_context_reading "$1" "${2:-}") || return 1
    printf '%s\n' "${_cu_out%% *}"
}

# _ctx_estimate <transcript_path> — byte-size estimate of utilization, used when
# no usage record can be read. Prints a basis-point integer.
#
# Uses POSIX awk for arithmetic (NOT bc — bc is not in POSIX core).
# 64-bit awk integers required: POSIX awk on darwin AND GNU defaults to 64-bit
# since ~2010, so no special configuration needed. Verified via boundary fixture:
# a 2.1 GB transcript size synthetic input produces a valid basis-point integer
# without overflow.
#
# Portability: wc -c < FILE works on darwin BSD AND GNU; do NOT use stat -c %s
# (GNU-only — explicitly rejected per architecture rev-6.1 MIN-1).
_ctx_estimate() {
    _ccr_est_path="$1"
    _ccr_est_bytes=$(wc -c < "$_ccr_est_path" 2>/dev/null) || return 1
    # Remove leading whitespace from wc output (BSD wc includes leading spaces)
    _ccr_est_bytes=$(printf '%s' "$_ccr_est_bytes" | awk '{print $1}')
    # awk arithmetic: (bytes / bpt / limit) * 10000 → basis-point integer
    # BPT may be a decimal like "8.0"; awk handles floating-point naturally
    awk -v b="$_ccr_est_bytes" -v bpt="$BPT" -v lim="$LIMIT" \
        'BEGIN{ printf "%d\n", (b / bpt / lim) * 10000 }'
}

# _ctx_positive_int <raw> — print raw as a positive integer of at most 9 digits
# (leading zeros stripped); print nothing and return 1 otherwise. Digits are
# validated before any arithmetic so a hostile value cannot reach $(( )).
_ctx_positive_int() {
    _cpi_raw="$1"
    case "$_cpi_raw" in
        ''|*[!0-9]*) return 1 ;;
    esac
    while :; do
        case "$_cpi_raw" in
            0?*) _cpi_raw=${_cpi_raw#0} ;;
            *) break ;;
        esac
    done
    [ "$_cpi_raw" = "0" ] && return 1
    [ "${#_cpi_raw}" -le 9 ] || return 1
    printf '%s\n' "$_cpi_raw"
}

# _ctx_tail_bytes — print how many trailing transcript bytes to scan
# (QUOIN_CONTEXT_TAIL_BYTES; default 2097152, capped at 67108864).
_ctx_tail_bytes() {
    _ctb_raw="${QUOIN_CONTEXT_TAIL_BYTES:-}"
    case "$_ctb_raw" in
        ''|*[!0-9]*) printf '%s\n' 2097152; return 0 ;;
    esac
    while :; do
        case "$_ctb_raw" in
            0?*) _ctb_raw=${_ctb_raw#0} ;;
            *) break ;;
        esac
    done
    if [ "$_ctb_raw" = "0" ]; then
        printf '%s\n' 2097152
    elif [ "${#_ctb_raw}" -ge 9 ] || [ "$_ctb_raw" -gt 67108864 ]; then
        printf '%s\n' 67108864
    else
        printf '%s\n' "$_ctb_raw"
    fi
}

# _ctx_truthy <raw> — success when raw is 1/true/yes/on (any case).
_ctx_truthy() {
    case "$(printf '%s' "$1" | tr 'A-Z' 'a-z')" in
        1|true|yes|on) return 0 ;;
    esac
    return 1
}

# _ctx_has_1m <model> — success when the model string carries a [1m] suffix.
_ctx_has_1m() {
    case "$(printf '%s' "$1" | tr 'A-Z' 'a-z')" in
        *'[1m]'*) return 0 ;;
    esac
    return 1
}

# _ctx_settings_model <project_root> — print the first non-empty model string
# from ANTHROPIC_MODEL, project settings.local.json, project settings.json, then
# the user settings.json. Prints nothing when none is set.
_ctx_settings_model() {
    _csm_proj="$1"
    if [ -n "${ANTHROPIC_MODEL:-}" ]; then
        printf '%s\n' "$ANTHROPIC_MODEL"
        return 0
    fi
    for _csm_file in "$_csm_proj/.claude/settings.local.json" \
                     "$_csm_proj/.claude/settings.json" \
                     "${HOME:-/nonexistent}/.claude/settings.json"; do
        [ -r "$_csm_file" ] || continue
        _csm_val=$(jq -r 'if (.model | type) == "string" then .model else "" end' "$_csm_file" 2>/dev/null) || continue
        if [ -n "$_csm_val" ]; then
            printf '%s\n' "$_csm_val"
            return 0
        fi
    done
    return 0
}

# jq program that scans transcript lines (one JSON record per line) and prints
# "tokens window_hint has_1m peak clamp kind" for the newest reading (kind is 0
# for a usage record, 1 for a compact boundary), or nothing when no usage record
# qualifies. Sidechain and synthetic error records never count as
# usage. The model rules between the markers mirror the Python reader in
# context_budget_guard.py (a drift test compares the two blocks).
_CTX_JQ_PROGRAM='
def num: if type == "number" then . else 0 end;
def inrange: type == "number" and . >= 1 and . <= 999999999999;
def ctxlower: ascii_downcase;
# ctx-rules-begin
def _ctx_strip: [
  "^(us|eu|apac|global)[.]",
  "^anthropic[.]",
  "-v[0-9]+(:[0-9]+)?$",
  ":[0-9]+$",
  "@[0-9]{8}$",
  "-[0-9]{8}$"
];
def _ctx_rules: [
  ["^claude-(fable|mythos)(-|$)", 1000000],
  ["^claude-(opus|sonnet|haiku)-([5-9]|[1-9][0-9]+)(-[0-9]+)?$", 1000000],
  ["^claude-opus-4-(7|8)$", 1000000],
  ["^claude-", 200000]
];
# ctx-rules-end
def normalize:
  (if type == "string" then ctxlower else "" end)
  | split("[1m]") | join("")
  | reduce _ctx_strip[] as $p (.; sub($p; ""));
def window_hint:
  normalize as $m
  | ([_ctx_rules[] as $rule | select($m | test($rule[0])) | $rule[1]] | first) // 0;
def has1m: type == "string" and (ctxlower | contains("[1m]"));
def clamptext:
  if type == "string" then contains("Usage credits required for 1M context")
  elif type == "array" then
    any(.[]; type == "object" and .type == "text" and (.text | type) == "string"
             and (.text | contains("Usage credits required for 1M context")))
  else false end;
reduce (inputs | try fromjson catch null) as $r (
  {t: null, m: "", pk: 0, cl: false, k: 0};
  if ($r | type) != "object" then .
  else
    . as $s
    | try (
        if $r.isSidechain == true then .
        elif $r.type == "assistant" and ($r.message | type) == "object" then
          $r.message as $msg
          | if $msg.model == "<synthetic>" then
              if $r.isApiErrorMessage == true
                 and ($r.apiError == "long_context_credits_required"
                      or ($msg.content | clamptext))
              then .cl = true else . end
            elif ($msg.usage | type) == "object" then
              $msg.usage as $u
              | ((($u.input_tokens | num) + ($u.cache_read_input_tokens | num)
                  + ($u.cache_creation_input_tokens | num)) | floor) as $sum
              | if $sum >= 1 and $sum <= 999999999999 then
                  .t = $sum
                  | .k = 0
                  | .m = (if ($msg.model | type) == "string" then $msg.model else "" end)
                  | .pk = ([.pk, $sum] | max)
                else . end
            else . end
        elif $r.type == "system" and $r.subtype == "compact_boundary"
             and ($r.compactMetadata | type) == "object" then
          $r.compactMetadata as $md
          | (if ($md.preTokens | inrange) then .pk = ([.pk, ($md.preTokens | floor)] | max) else . end)
          | (if ($md.postTokens | inrange) then .t = ($md.postTokens | floor) | .k = 1 else . end)
        else . end
      ) catch $s
  end
)
| if .t == null then empty
  else [.t, (.m | window_hint), (if (.m | has1m) then 1 else 0 end), .pk, (if .cl then 1 else 0 end), .k]
       | map(tostring) | join(" ")
  end
'

# compute_context_reading <transcript_path> [project_root] [--kind] — one-line
# reading of how full the context window is. Prints "<bps> real <tokens>
# <window>" when a usage record was read, or "<bps> estimated - -" when it fell
# back to the byte estimate. project_root (default $PWD) locates project
# settings for the [1m] probe. With a third argument of exactly --kind, a fifth
# field is appended: "usage" or "compact" for a real reading (compact means the
# newest reading is a compact boundary), "-" for an estimate. Returns non-zero
# when the path is empty or unreadable.
compute_context_reading() {
    _ccr_path="$1"
    _ccr_proj="${2:-$PWD}"
    _ccr_wantkind=0
    [ "${3:-}" = "--kind" ] && _ccr_wantkind=1
    if [ -z "$_ccr_path" ] || ! [ -r "$_ccr_path" ]; then
        return 1
    fi
    command -v jq >/dev/null 2>&1 || { _ccr_est=$(_ctx_estimate "$_ccr_path") || return 1; _ccr_emit_est "$_ccr_est"; return 0; }
    _ccr_size=$(wc -c < "$_ccr_path" 2>/dev/null) || return 1
    _ccr_size=$(printf '%s' "$_ccr_size" | awk '{print $1}')
    _ccr_tail=$(_ctx_tail_bytes)
    if [ "$_ccr_size" -gt "$_ccr_tail" ]; then
        _ccr_out=$(tail -c "$_ccr_tail" "$_ccr_path" 2>/dev/null | tail -n +2 | jq -R -n -r "$_CTX_JQ_PROGRAM" 2>/dev/null)
    else
        _ccr_out=$(jq -R -n -r "$_CTX_JQ_PROGRAM" < "$_ccr_path" 2>/dev/null)
    fi
    _ccr_tok=""; _ccr_hint=""; _ccr_h1m=""; _ccr_peak=""; _ccr_clamp=""; _ccr_kind=""
    read -r _ccr_tok _ccr_hint _ccr_h1m _ccr_peak _ccr_clamp _ccr_kind <<EOF
$_ccr_out
EOF
    # Every field must be plain digits within bounds before arithmetic.
    _ccr_ok=1
    for _ccr_f in "$_ccr_tok" "$_ccr_hint" "$_ccr_h1m" "$_ccr_peak" "$_ccr_clamp" "$_ccr_kind"; do
        case "$_ccr_f" in
            ''|*[!0-9]*) _ccr_ok=0 ;;
        esac
        [ "${#_ccr_f}" -le 12 ] || _ccr_ok=0
    done
    if [ "$_ccr_ok" = "1" ] && [ "$_ccr_tok" -ge 1 ] && [ "$_ccr_h1m" -le 1 ] && [ "$_ccr_clamp" -le 1 ] && [ "$_ccr_kind" -le 1 ]; then
        _ccr_win=$(_ctx_positive_int "${QUOIN_EFFECTIVE_CONTEXT_LIMIT:-}") || _ccr_win=""
        if [ -z "$_ccr_win" ]; then
            if _ctx_truthy "${CLAUDE_CODE_DISABLE_1M_CONTEXT:-}"; then
                _ccr_win=200000
            elif [ "$_ccr_clamp" = "1" ]; then
                _ccr_win=200000
            elif [ "$_ccr_h1m" = "1" ] || [ "$_ccr_hint" = "1000000" ]; then
                _ccr_win=1000000
            elif _ctx_has_1m "$(_ctx_settings_model "$_ccr_proj")"; then
                _ccr_win=1000000
            elif [ "$_ccr_hint" -gt 0 ]; then
                _ccr_win="$_ccr_hint"
            elif [ "$_ccr_peak" -gt 200000 ]; then
                _ccr_win=1000000
            else
                _ccr_win=200000
            fi
        fi
        if [ "$_ccr_wantkind" = "1" ]; then
            if [ "$_ccr_kind" = "1" ]; then _ccr_kname=compact; else _ccr_kname=usage; fi
            printf '%s real %s %s %s\n' "$(( _ccr_tok * 10000 / _ccr_win ))" "$_ccr_tok" "$_ccr_win" "$_ccr_kname"
        else
            printf '%s real %s %s\n' "$(( _ccr_tok * 10000 / _ccr_win ))" "$_ccr_tok" "$_ccr_win"
        fi
        return 0
    fi
    _ccr_est=$(_ctx_estimate "$_ccr_path") || return 1
    _ccr_emit_est "$_ccr_est"
}

# _ccr_emit_est <bps> — print the estimated-reading line, with the kind field
# when compute_context_reading was asked for it.
_ccr_emit_est() {
    if [ "${_ccr_wantkind:-0}" = "1" ]; then
        printf '%s estimated - - -\n' "$1"
    else
        printf '%s estimated - -\n' "$1"
    fi
}

# _run_state_validate_stale_days <raw> <default> — echo a validated,
# leading-zero-stripped, length-clamped day count for <raw>, falling back to
# <default> when <raw> is empty or non-numeric. Shared by run_state_probe
# and run_state_select so neither reinvents the same validation — but each
# caller resolves its OWN raw candidate from its own knob precedence FIRST,
# deliberately NOT shared here: read_constants() pre-resolves and EXPORTS
# RUN_STATE_STALE_DAYS (default 1, tuned for run_state_select's tight,
# over-inclusive pre-filter — see that function's own docstring) before
# either function runs, in any hook that calls read_constants
# (userpromptsubmit.sh does). If run_state_probe consulted that same
# exported variable it would silently re-inherit select's tight 1-day
# default and defeat the wider window this probe needs — a real interaction
# hit while building this function, not a hypothetical: run_state_probe's
# own default must stay reachable only via the raw
# QUOIN_RUN_STATE_STALE_DAYS knob, never via RUN_STATE_STALE_DAYS.
#
# Numeric-validates before the result ever reaches $(( )) or a find
# predicate (an unvalidated value is an arithmetic-eval injection sink under
# a bash /bin/sh), strips leading zeros so a value like "010" cannot be
# misread as octal, then clamps any 5-or-more-digit result to 36500 (100y)
# by length alone, without ever numerically comparing the raw value — a huge
# all-digit knob must not reach an arithmetic comparison, let alone $(( )),
# unclamped; combined with run-state-*.json having no sweep family, an
# unclamped knob would turn the probe into a full historical scan on every
# prompt.
_run_state_validate_stale_days() {
    _rvsd_raw="$1"
    _rvsd_default="$2"
    case "$_rvsd_raw" in
        ''|*[!0-9]*) _rvsd_days="$_rvsd_default" ;;
        *) _rvsd_days="$_rvsd_raw" ;;
    esac
    # Strip ALL leading zeros, so e.g. "010" is never later misread as
    # octal by $(( )) and no zero-led residue can ever reach an arithmetic
    # comparison — a per-character strip loop or any fixed iteration budget
    # would leave exactly the pad lengths just past the budget with zeros
    # intact. The strip runs only for zero-led multi-character values (the
    # 0?* guard), so ordinary knobs pay no fork at all; when it does run,
    # one sed pass does the whole strip in linear time — the pure-shell
    # alternative, ${var#"${var%%[!0]*}"}, retries the shortest-prefix
    # match at every length and goes quadratic, stalling for tens of
    # seconds on a 100k-zero knob. An all-zero value strips to empty and
    # reads as 0. The stderr redirect keeps run_state_probe's never-emit
    # contract when the sed itself fails (unresolvable on PATH, or an env
    # too large to exec); the empty-result fallback below already absorbs
    # the failed substitution.
    case "$_rvsd_days" in
        0?*) _rvsd_days=$(printf '%s\n' "$_rvsd_days" | sed 's/^0*//' 2>/dev/null) ;;
    esac
    [ -n "$_rvsd_days" ] || _rvsd_days=0
    case "$_rvsd_days" in
        ?????*) _rvsd_days=36500 ;;
    esac
    printf '%s\n' "$_rvsd_days"
}

# run_state_probe MEMORY_DIR [SESSION_ID] — return 0 iff MEMORY_DIR holds at
# least one fresh, active run-state-*.json record (optionally scoped to
# SESSION_ID). Single-mode: there is no separate at_stage_boundary distinction
# (D-26) — the probe answers exactly one question, "is there a fresh active
# run-state record, optionally belonging to SESSION_ID?"
#
# Unlike run_state_select's mtime prefilter — deliberately over-inclusive
# because a downstream run_state.py --max-age-days gate makes the real
# staleness call — this probe has no downstream gate, so its mtime window IS
# the final word on "active or not". Default 14 (vs. select's 1) so a
# boundary record from a long-running phase, or one paused over a weekend,
# does not read as inactive just because `/run` only refreshes the record
# at phase boundaries and nothing touches its mtime mid-phase. Still bounded
# (run-state-*.json is in no sweep family) and overridable via
# QUOIN_RUN_STATE_STALE_DAYS only — NOT RUN_STATE_STALE_DAYS; see below. A
# phase or pause that outlives even the widened window is a residual,
# documented risk — see run/SKILL.md's self-checkpoint bullet.
#
# Returns 1 on absence, an unreadable/missing MEMORY_DIR, or any error. Emits
# nothing on stdout or stderr, ever — callers rely on the exit code alone.
run_state_probe() {
    _rsp_dir="$1"
    _rsp_sid="${2:-}"
    [ -n "$_rsp_dir" ] || return 1
    [ -d "$_rsp_dir" ] || return 1

    # QUOIN_RUN_STATE_STALE_DAYS only — deliberately NOT RUN_STATE_STALE_DAYS
    # (read_constants' exported, select-tuned var); see
    # _run_state_validate_stale_days's docstring for why.
    _rsp_days=$(_run_state_validate_stale_days "${QUOIN_RUN_STATE_STALE_DAYS:-}" 14)

    # Split find's output on newlines only (not the default IFS) so a
    # MEMORY_DIR whose path contains spaces (e.g. under "My Drive") is not
    # word-split; captured as positional params (not a piped subshell) so
    # `return` below actually exits this function on the first eligible
    # candidate, rather than only the subshell a pipe would create. Pathname
    # expansion is separately suppressed around the capture: IFS-splitting
    # alone does not stop a glob metacharacter in a candidate's filename
    # from re-expanding against cwd; `set -f` is restored to its prior
    # state afterward, not unconditionally unset.
    _rsp_old_ifs="$IFS"
    IFS="
"
    case "$-" in
        *f*) _rsp_noglob_was_set=1 ;;
        *) _rsp_noglob_was_set=0 ;;
    esac
    set -f
    set -- $(find "$_rsp_dir" -maxdepth 1 -type f -name 'run-state-*.json' -mtime -$((_rsp_days + 1)) 2>/dev/null)
    [ "$_rsp_noglob_was_set" -eq 1 ] || set +f
    IFS="$_rsp_old_ifs"

    for _rsp_candidate in "$@"; do
        [ -f "$_rsp_candidate" ] || continue
        # One run_state_fields pass per candidate — the same 64 KiB-capped,
        # line-anchored extractor run_state_select uses, aligning the two
        # probes on both the read cap and the match shape: the old
        # byte-coupled '"active": true' grep was a false NEGATIVE against a
        # compact single-line record, and the old sed|tr -dc schema scan
        # concatenated every digit on a matched line — a single-line record
        # would have extracted garbage. run_state_fields's line-anchored key
        # match has neither failure mode, and collapses three forks
        # (grep/sed/tr) into one.
        _rsp_kv=$(run_state_fields "$_rsp_candidate" active schema session_id)
        [ -n "$_rsp_kv" ] || continue
        _rsp_active_line="" _rsp_schema_line="" _rsp_sid_line=""
        {
            IFS= read -r _rsp_active_line
            IFS= read -r _rsp_schema_line
            IFS= read -r _rsp_sid_line
        } <<RSPEOF
$_rsp_kv
RSPEOF
        [ "$_rsp_active_line" = "active=true" ] || continue
        _rsp_schema="${_rsp_schema_line#schema=}"
        case "$_rsp_schema" in
            ''|*[!0-9]*) continue ;;
            # See run_state_select's twin guard: reject 4+ digit schemas
            # before the arithmetic test below, not after — an overlong
            # all-digit value must never reach the shell's integer
            # comparison, which errors on stderr past the platform's
            # integer width.
            ????*) continue ;;
        esac
        [ "$_rsp_schema" -le 1 ] 2>/dev/null || continue
        # SESSION_ID is intentionally uncalled by every consumer as of this
        # stage (D-27) — T-03, T-05, and T-07 all probe project-scoped. Kept
        # for forward compatibility (covered by T-02 cases (j)/(k)); do not
        # delete this branch as dead code. Plain string equality against the
        # extracted field, matching run_state_select's own idiom: grep -F
        # treats a newline-bearing pattern as a pattern LIST — any one line
        # of a multi-line session id could fragment-match an unrelated line
        # in the record, and a session id arriving from hook stdin JSON can
        # legally contain '\n'.
        if [ -n "$_rsp_sid" ]; then
            _rsp_sid_val="${_rsp_sid_line#session_id=}"
            [ "$_rsp_sid_val" = "$_rsp_sid" ] || continue
        fi
        return 0
    done
    return 1
}

# trash_move <file> <trash_base> — move a file to the trash archive instead of deleting it.
# Creates <trash_base>/trash/<YYYY-MM-DD>/ and moves the file there.
# If a file with the same basename already exists in trash today, appends -1, -2, etc.
# Returns 0 on success. On failure: emits a one-line stderr warning and returns 1.
# POSIX sh compatible (no bash-isms).
# <trash_base> is typically ${cwd}/.workflow_artifacts/memory
trash_move() {
    _tm_file="$1"
    _tm_base="$2"
    [ -f "$_tm_file" ] || {
        printf '[quoin-trash] WARNING: trash-move failed for %s; source not found\n' "$_tm_file" >&2
        return 1
    }
    _tm_date=$(date -u +%Y-%m-%d 2>/dev/null) || _tm_date=$(date +%Y-%m-%d)
    _tm_dir="${_tm_base}/trash/${_tm_date}"
    mkdir -p "$_tm_dir" 2>/dev/null || true
    _tm_name=$(basename "$_tm_file")
    _tm_dest="${_tm_dir}/${_tm_name}"
    if [ -e "$_tm_dest" ]; then
        _tm_n=1
        while [ -e "${_tm_dir}/${_tm_name}-${_tm_n}" ]; do
            _tm_n=$((_tm_n + 1))
        done
        _tm_dest="${_tm_dir}/${_tm_name}-${_tm_n}"
    fi
    if mv "$_tm_file" "$_tm_dest" 2>/dev/null; then
        return 0
    else
        printf '[quoin-trash] WARNING: trash-move failed for %s; leaving in place\n' "$_tm_file" >&2
        return 1
    fi
}

# safe_jq_or_passthrough [jq-args]... — jq invocation with fail-OPEN.
# If jq is not on PATH, returns 1; caller should exit 0 (fail-OPEN).
# Usage: output=$(printf '%s' "$STDIN" | safe_jq_or_passthrough -r '.field // empty')
safe_jq_or_passthrough() {
    if ! command -v jq > /dev/null 2>&1; then
        return 1
    fi
    jq "$@"
}

# resolve_project_root <start_dir> — echo effective project root; always return 0.
# Precedence: OUTERMOST .workflow_artifacts/ (strictly < $HOME) > NEAREST .git (strictly < $HOME) > start_dir.
# EXCLUSIVE ceiling: $HOME and / bound the walk but are NEVER inspected as owners, NEVER returned.
resolve_project_root() {
    _rpr_start="$1"
    [ -n "$_rpr_start" ] || { printf '%s\n' "$_rpr_start"; return 0; }
    _rpr_ceiling="${HOME:-/}"
    # Pass 1: OUTERMOST artifacts owner STRICTLY BELOW ceiling.
    # Walk the FULL eligible chain, remembering the HIGHEST eligible hit.
    _rpr_best=""
    _rpr_cur="$_rpr_start"
    while [ -n "$_rpr_cur" ]; do
        [ "$_rpr_cur" = "$_rpr_ceiling" ] && break       # EXCLUSIVE: stop BEFORE inspecting $HOME
        [ "$_rpr_cur" = "/" ] && break                   # EXCLUSIVE: never inspect /
        [ -d "$_rpr_cur/.workflow_artifacts" ] && _rpr_best="$_rpr_cur"
        _rpr_parent=$(dirname "$_rpr_cur")
        [ "$_rpr_parent" = "$_rpr_cur" ] && break        # fixedpoint loop guard
        _rpr_cur="$_rpr_parent"
    done
    [ -n "$_rpr_best" ] && { printf '%s\n' "$_rpr_best"; return 0; }
    # Pass 2: NEAREST .git (dir OR file) STRICTLY BELOW ceiling.
    _rpr_cur="$_rpr_start"
    while [ -n "$_rpr_cur" ]; do
        [ "$_rpr_cur" = "$_rpr_ceiling" ] && break       # EXCLUSIVE: $HOME's .git never returned (CRIT-4)
        [ "$_rpr_cur" = "/" ] && break
        [ -e "$_rpr_cur/.git" ] && { printf '%s\n' "$_rpr_cur"; return 0; }
        _rpr_parent=$(dirname "$_rpr_cur")
        [ "$_rpr_parent" = "$_rpr_cur" ] && break
        _rpr_cur="$_rpr_parent"
    done
    # Pass 3: fall back to start (today's behavior; NEVER $HOME, NEVER /).
    printf '%s\n' "$_rpr_start"
    return 0
}

# run_state_fields <file> <key>... — extract requested keys from a run-state
# JSON record as "key=value" lines, one per requested key, in request order
# (an absent key emits "key=" with an empty value). Single awk pass over one
# bounded read of the file (first 64 KiB — a record is a few hundred bytes,
# so this is generous headroom while keeping an oversized, possibly hostile,
# file from slurping into the hook's time budget). The record writer's
# sanitization guarantees the file
# contains no backslash byte at all ('"' -> "'", '\' -> '/', C0/DEL -> space),
# so this extractor never meets an escape sequence and does no unescaping.
# The structural trailing comma is stripped BEFORE the quotes: the closing
# quote is what distinguishes a structural comma from a comma that is the
# last character of the value itself. Degrades to empty output on any
# failure (fail-OPEN).
run_state_fields() {
    _rsf_file="$1"
    shift
    [ -n "$_rsf_file" ] || return 0
    [ -f "$_rsf_file" ] || return 0
    [ -s "$_rsf_file" ] || return 0
    # (head -c is BSD/GNU-common but not POSIX-mandated; a head lacking -c
    # emits nothing here, so the pipeline degrades toward empty output —
    # the fail-OPEN direction for every consumer of this extractor.)
    # LC_ALL=C: a truncated-mid-multibyte-character or binary record can
    # make a locale-aware awk's character reader fail on the byte cut
    # above, leaking stderr and flipping the verdict by locale (a plain
    # byte-oriented C-locale awk never trips on this). Trailing
    # 2>/dev/null is a second, independent backstop for any other awk
    # error on a hostile record — this extractor never emits on either
    # stream.
    head -c 65536 "$_rsf_file" 2>/dev/null | LC_ALL=C awk -v keys="$*" '
    BEGIN {
        n = split(keys, order, " ")
    }
    {
        line = $0
        if (!match(line, /^[ \t]*"[^"]*"/)) next
        key = substr(line, RSTART, RLENGTH)
        gsub(/^[ \t]*"/, "", key)
        gsub(/"$/, "", key)
        rest = substr(line, RSTART + RLENGTH)
        sub(/^: */, "", rest)
        sub(/,$/, "", rest)
        sub(/^"/, "", rest)
        sub(/"$/, "", rest)
        val[key] = rest
    }
    END {
        for (i = 1; i <= n; i++) {
            k = order[i]
            printf "%s=%s\n", k, (k in val ? val[k] : "")
        }
    }
    ' 2>/dev/null
}

# run_state_select <memory_dir> <session_id> — echo the path of the freshest
# eligible run-state-*.json record whose own session_id field equals the
# second argument; echo nothing when no record matches. Eligibility: regular
# file, fresher than RUN_STATE_STALE_DAYS (day-granular, deliberately
# over-inclusive — the exact gate lives with the record writer), the
# writer's serialized "active": true line, schema <= 1, and exact
# session_id string equality. A record written by a different session is
# skipped outright — there is no freshest-active fallback, because falling
# back would attribute this session's compaction to another session's run.
run_state_select() {
    _rss_dir="$1"
    _rss_sid="$2"
    [ -n "$_rss_dir" ] || return 0
    [ -d "$_rss_dir" ] || return 0
    # Validation shared with run_state_probe via
    # _run_state_validate_stale_days; the raw-value precedence itself is
    # NOT shared — see that function's docstring for why. This function
    # keeps read_constants' RUN_STATE_STALE_DAYS at top
    # precedence, unchanged from its original behavior: this function's own
    # default (1) stays deliberately over-inclusive-but-tight, since a
    # downstream run_state.py --max-age-days gate makes the real staleness
    # call for select's callers, so a short pre-filter here is a
    # performance bound, not the final word.
    _rss_raw="${RUN_STATE_STALE_DAYS:-${QUOIN_RUN_STATE_STALE_DAYS:-1}}"
    _rss_days=$(_run_state_validate_stale_days "$_rss_raw" 1)
    # NOTE: -exec ls -t {} + can split into several ls invocations on a very
    # large directory, yielding per-batch rather than global mtime order —
    # harmless at realistic record counts (same latent trait as the
    # checkpoint sweeps elsewhere in the hooks).
    find "$_rss_dir" -maxdepth 1 -type f -name 'run-state-*.json' \
        -mtime -$((_rss_days + 1)) -exec ls -t {} + 2>/dev/null \
    | while IFS= read -r _rss_cand; do
        [ -f "$_rss_cand" ] || continue
        # Byte-exact match for the writer's serialized bool line; a
        # hand-rolled record without the space would not match, by design.
        # Bounded liveness probe — same 64 KiB ceiling as run_state_fields,
        # so an oversized candidate cannot slurp into the time budget before
        # the bounded extractor even runs.
        head -c 65536 "$_rss_cand" 2>/dev/null | grep -q '"active": true' || continue
        # One extractor pass per candidate covers both gate fields.
        _rss_kv=$(run_state_fields "$_rss_cand" schema session_id)
        [ -n "$_rss_kv" ] || continue
        _rss_schema_line=""
        _rss_sid_line=""
        {
            IFS= read -r _rss_schema_line
            IFS= read -r _rss_sid_line
        } <<RSSEOF
$_rss_kv
RSSEOF
        _rss_schema="${_rss_schema_line#schema=}"
        case "$_rss_schema" in
            ''|*[!0-9]*) continue ;;
            # See run_state_probe's twin guard: reject 4+ digit schemas
            # before the arithmetic test below, not after.
            ????*) continue ;;
        esac
        [ "$_rss_schema" -le 1 ] 2>/dev/null || continue
        # Plain string equality against the record's stored (sanitized)
        # session_id — no grep, no pattern semantics, no metacharacter
        # hazard. A raw id containing a quote or backslash cannot equal
        # its own sanitized stored form, so it falls to the no-match path.
        [ "$_rss_sid_line" = "session_id=$_rss_sid" ] || continue
        printf '%s\n' "$_rss_cand"
        break
    done
}
