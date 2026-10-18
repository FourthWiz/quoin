#!/bin/sh
# test_postcompact_hook.sh — fixture tests for quoin/hooks/postcompact.sh
#
# Covers T-09 acceptance criteria.
# Requires: jq on PATH, sh (POSIX).
#
# Usage: sh quoin/dev/tests/test_postcompact_hook.sh
# Exit 0 if all tests pass; non-zero otherwise.

set -eu

PASS=0
FAIL=0
FAIL_MSGS=""

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HOOK="$SCRIPT_DIR/../../hooks/postcompact.sh"
DEPLOYED_HOOK="$HOME/.claude/hooks/postcompact.sh"

ok() { PASS=$((PASS + 1)); printf 'ok  %s\n' "$1"; }
fail() {
  FAIL=$((FAIL + 1))
  printf 'FAIL %s\n' "$1" >&2
  FAIL_MSGS="$FAIL_MSGS\n  - $1"
}

TMPDIR_TEST="${TMPDIR:-/tmp}/test_postcompact_$$"
MEMORY_DIR="$TMPDIR_TEST/.workflow_artifacts/memory"
mkdir -p "$MEMORY_DIR"

# Create a small dummy transcript file
TRANSCRIPT="$TMPDIR_TEST/dummy.jsonl"
printf '{"type":"message"}\n' > "$TRANSCRIPT"

cleanup() { rm -rf "$TMPDIR_TEST"; }
trap cleanup EXIT

make_stdin() {
  session_id="${1:-test-session-pc}"
  cwd="${2:-$TMPDIR_TEST}"
  transcript="${3:-$TRANSCRIPT}"
  printf '{"session_id":"%s","cwd":"%s","transcript_path":"%s","hook_event_name":"PostCompact"}' \
    "$session_id" "$cwd" "$transcript"
}

# ─── T-01 scaffolding: precompact driver + run-state writer ──────────────────
# The compaction-telemetry correlation lives across both hooks, so this file
# (previously postcompact.sh-only) also needs to drive precompact.sh to plant
# the "pre" half of a pair.

HOOK_PRE="$SCRIPT_DIR/../../hooks/precompact.sh"
RUN_STATE="$SCRIPT_DIR/../../core/scripts/run_state.py"
TEL_DIR="$MEMORY_DIR/telemetry"
TEL_SINK="$TEL_DIR/compaction-events.jsonl"

make_stdin_pre() {
  trigger="${1:-auto}"
  session_id="${2:-test-session-precompact}"
  cwd="${3:-$TMPDIR_TEST}"
  printf '{"trigger":"%s","session_id":"%s","cwd":"%s","transcript_path":"%s/dummy.jsonl"}' \
    "$trigger" "$session_id" "$cwd" "$cwd"
}

# run_hook_watchdog — run a hook under a bounded 5s watchdog so a fixture
# that would otherwise block (a FIFO the hook fails to refuse, or a stray
# read against a controlling tty) fails loudly instead of hanging the suite.
# Args: $1=hook path, $2=stdin content, $3=PATH override (empty for default).
# Sets globals: WD_RC, WD_OUT, WD_TIMEDOUT (1/0).
_WD_N=0
run_hook_watchdog() {
  _wd_hook="$1"; _wd_stdin="$2"; _wd_path="${3:-}"
  _WD_N=$((_WD_N + 1))
  _wd_out="$TMPDIR_TEST/wd-out-$_WD_N.txt"
  : > "$_wd_out"
  if [ -n "$_wd_path" ]; then
    ( printf '%s' "$_wd_stdin" | PATH="$_wd_path" sh "$_wd_hook" > "$_wd_out" 2>/dev/null ) &
  else
    ( printf '%s' "$_wd_stdin" | sh "$_wd_hook" > "$_wd_out" 2>/dev/null ) &
  fi
  _wd_pid=$!
  _w=0
  while [ "$_w" -lt 50 ] && kill -0 "$_wd_pid" 2>/dev/null; do
    sleep 0.1
    _w=$((_w + 1))
  done
  if kill -0 "$_wd_pid" 2>/dev/null; then
    kill "$_wd_pid" 2>/dev/null || true
    wait "$_wd_pid" 2>/dev/null || true
    WD_TIMEDOUT=1
    WD_RC=124
  else
    WD_RC=0
    wait "$_wd_pid" 2>/dev/null || WD_RC=$?
    WD_TIMEDOUT=0
  fi
  WD_OUT=$(cat "$_wd_out" 2>/dev/null)
  rm -f "$_wd_out"
}

# ─── (e) Shebang assertion ────────────────────────────────────────────────────

if head -1 "$HOOK" | grep -qE '^#!/bin/sh( |$)'; then
  ok "(e) shebang: source hook starts with #!/bin/sh"
else
  fail "(e) shebang: source hook does not start with #!/bin/sh"
fi

# ─── (a) Valid input → no postcompact-reset sentinel is written ──────────────

SID="test-session-pc-a"
stdin=$(make_stdin "$SID" "$TMPDIR_TEST" "$TRANSCRIPT")
stdout=$(printf '%s' "$stdin" | sh "$HOOK" 2>/dev/null)

SENTINEL="$MEMORY_DIR/postcompact-reset-${SID}.txt"

if [ -f "$SENTINEL" ]; then
  fail "(a) valid input: postcompact-reset sentinel written at $SENTINEL (nothing reads it any more)"
else
  ok "(a) valid input: no postcompact-reset sentinel written"
fi

# (b2) compact-happened sentinel present
COMPACT_HAPPENED="${MEMORY_DIR}/compact-happened-${SID}.txt"
if [ -f "${COMPACT_HAPPENED}" ]; then
  ok "(b2) compact-happened sentinel written at correct path"
else
  fail "(b2) compact-happened sentinel NOT written (expected: ${COMPACT_HAPPENED})"
fi

# (b3) compact-happened sentinel has required fields
if [ -f "${COMPACT_HAPPENED}" ]; then
  if grep -q '^compacted_at=' "${COMPACT_HAPPENED}"; then
    ok "(b3) compact-happened sentinel has compacted_at field"
  else
    fail "(b3) compact-happened sentinel missing compacted_at field"
  fi

  if grep -q '^session_id=' "${COMPACT_HAPPENED}"; then
    ok "(b3) compact-happened sentinel has session_id field"
  else
    fail "(b3) compact-happened sentinel missing session_id field"
  fi
fi

# ─── (c) Hook exits 0 and produces no stdout ─────────────────────────────────

SID_C="test-session-pc-c"
stdin_c=$(make_stdin "$SID_C" "$TMPDIR_TEST" "$TRANSCRIPT")
stdout_c=$(printf '%s' "$stdin_c" | sh "$HOOK" 2>/dev/null)
exit_code=0
printf '%s' "$stdin_c" | sh "$HOOK" > /dev/null 2>/dev/null || exit_code=$?

if [ "$exit_code" -eq 0 ]; then
  ok "(c) hook exits 0"
else
  fail "(c) hook exited $exit_code (expected 0)"
fi

if [ -z "$stdout_c" ]; then
  ok "(c) hook produces no stdout"
else
  fail "(c) hook produced stdout: $stdout_c"
fi

# ─── (d) Missing session_id → fail-OPEN, no sentinel, exit 0 ─────────────────

stdin_d='{"cwd":"'"$TMPDIR_TEST"'","transcript_path":"'"$TRANSCRIPT"'","hook_event_name":"PostCompact"}'
exit_d=0
printf '%s' "$stdin_d" | sh "$HOOK" > /dev/null 2>/dev/null || exit_d=$?

if [ "$exit_d" -eq 0 ]; then
  ok "(d) missing session_id: hook exits 0 (fail-OPEN)"
else
  fail "(d) missing session_id: hook exited $exit_d (expected 0)"
fi

# No sentinel should be written when session_id is missing (empty glob = no files)
sentinel_count=$(find "$MEMORY_DIR" -name 'postcompact-reset-*.txt' -maxdepth 1 2>/dev/null | wc -l | awk '{print $1}')
# Tests (c) wrote compact-happened markers; check none has an empty name
if find "$MEMORY_DIR" -name 'postcompact-reset-.txt' -maxdepth 1 2>/dev/null | grep -q .; then
  fail "(d) missing session_id: stray sentinel 'postcompact-reset-.txt' created"
else
  ok "(d) missing session_id: no sentinel with empty session_id created"
fi

# ─── T-01: compaction-telemetry "post" half + pre/post correlation ──────────
# File-local numbering — unrelated to precompact.sh's own T-01 section.

# `.` is a POSIX special built-in: sourcing a missing file can be fatal to a
# non-interactive shell even behind `|| true`. Every scratch-copy mutant
# below lives directly under $TMPDIR_TEST, so `_lib.sh` must be reachable at
# that same directory for the hook's own `. "$(dirname "$0")/_lib.sh"` line.
ln -sf "$SCRIPT_DIR/../../hooks/_lib.sh" "$TMPDIR_TEST/_lib.sh" 2>/dev/null || true

# (T-01-pair) plant an eligible run-state record, then drive precompact.sh
# then postcompact.sh once for that session: one pre + one post, matching
# session_id and event_seq, non-null bytes_before/bytes_after, and the pre's
# task/phase/subphase/step equal to the planted values.
python3 "$RUN_STATE" --write --project-root "$TMPDIR_TEST" --task tel-pair --session-id sess-tel-pair --phase implement --subphase code --step "pair step" >/dev/null 2>&1
stdin_pre_pair=$(make_stdin_pre "auto" "sess-tel-pair" "$TMPDIR_TEST")
printf '%s' "$stdin_pre_pair" | sh "$HOOK_PRE" >/dev/null 2>&1 || true
_t0_pair=$(python3 -c 'import time; print(time.time())')
stdin_post_pair=$(make_stdin "sess-tel-pair" "$TMPDIR_TEST" "$TRANSCRIPT")
printf '%s' "$stdin_post_pair" | sh "$HOOK" >/dev/null 2>&1 || true
_t1_pair=$(python3 -c 'import time; print(time.time())')
printf 'wall-clock (postcompact.sh STEP 3 included, T-01-pair invocation): %sms\n' \
  "$(python3 -c "print(int(($_t1_pair - $_t0_pair) * 1000))")"

pair_pre_line=$(grep -F '"session_id":"sess-tel-pair"' "$TEL_SINK" | grep -F '"half":"pre"' | tail -1)
pair_post_line=$(grep -F '"session_id":"sess-tel-pair"' "$TEL_SINK" | grep -F '"half":"post"' | tail -1)
pair_pre_seq=$(printf '%s' "$pair_pre_line" | jq -r .event_seq)
pair_post_seq=$(printf '%s' "$pair_post_line" | jq -r .event_seq)
pair_pre_sid=$(printf '%s' "$pair_pre_line" | jq -r .session_id)
pair_post_sid=$(printf '%s' "$pair_post_line" | jq -r .session_id)
pair_bb=$(printf '%s' "$pair_pre_line" | jq -r .bytes_before)
pair_ba=$(printf '%s' "$pair_post_line" | jq -r .bytes_after)
pair_task=$(printf '%s' "$pair_pre_line" | jq -r .task)
pair_phase=$(printf '%s' "$pair_pre_line" | jq -r .phase)
pair_subphase=$(printf '%s' "$pair_pre_line" | jq -r .subphase)
pair_step=$(printf '%s' "$pair_pre_line" | jq -r .step)
if [ "$pair_pre_seq" = "$pair_post_seq" ] && [ "$pair_pre_sid" = "$pair_post_sid" ] \
   && [ "$pair_bb" != "null" ] && [ "$pair_ba" != "null" ] \
   && [ "$pair_task" = "tel-pair" ] && [ "$pair_phase" = "implement" ] \
   && [ "$pair_subphase" = "code" ] && [ "$pair_step" = "pair step" ]; then
  ok "(T-01-pair) pre/post pair carries matching event identity, before/after sizes, run and stage"
else
  fail "(T-01-pair) pair fields mismatched (seq=$pair_pre_seq/$pair_post_seq sid=$pair_pre_sid/$pair_post_sid bb=$pair_bb ba=$pair_ba task=$pair_task phase=$pair_phase subphase=$pair_subphase step=$pair_step)"
fi
rm -f "$MEMORY_DIR/run-state-tel-pair.json" "$MEMORY_DIR/run-notes-tel-pair.md"

# (T-01-twice) two compactions in one session → two distinct pairs
SID_TWICE="sess-tel-twice"
stdin_pre_twice=$(make_stdin_pre "auto" "$SID_TWICE" "$TMPDIR_TEST")
stdin_post_twice=$(make_stdin "$SID_TWICE" "$TMPDIR_TEST" "$TRANSCRIPT")
printf '%s' "$stdin_pre_twice" | sh "$HOOK_PRE" >/dev/null 2>&1 || true
printf '%s' "$stdin_post_twice" | sh "$HOOK" >/dev/null 2>&1 || true
printf '%s' "$stdin_pre_twice" | sh "$HOOK_PRE" >/dev/null 2>&1 || true
printf '%s' "$stdin_post_twice" | sh "$HOOK" >/dev/null 2>&1 || true
twice_pre_seqs=$(grep -F "\"session_id\":\"$SID_TWICE\"" "$TEL_SINK" | grep -F '"half":"pre"' | jq -r .event_seq | sort -n | tr '\n' ',')
twice_post_seqs=$(grep -F "\"session_id\":\"$SID_TWICE\"" "$TEL_SINK" | grep -F '"half":"post"' | jq -r .event_seq | sort -n | tr '\n' ',')
if [ "$twice_pre_seqs" = "0,1," ] && [ "$twice_post_seqs" = "0,1," ]; then
  ok "(T-01-twice) two compactions → two pre/post pairs with event_seq 0 and 1"
else
  fail "(T-01-twice) sequence mismatch (pre=$twice_pre_seqs post=$twice_post_seqs)"
fi

# (T-01-unmatched-post) postcompact.sh alone, no pre for this session → the
# post is still appended, its event_seq is null, exit 0, no stdout
SID_UMP="sess-tel-unmatched-post"
stdin_ump=$(make_stdin "$SID_UMP" "$TMPDIR_TEST" "$TRANSCRIPT")
rc_ump=0
out_ump=$(printf '%s' "$stdin_ump" | sh "$HOOK" 2>/dev/null) || rc_ump=$?
ump_seq=$(grep -F "\"session_id\":\"$SID_UMP\"" "$TEL_SINK" | tail -1 | jq -r .event_seq)
if [ "$rc_ump" -eq 0 ] && [ -z "$out_ump" ] && [ "$ump_seq" = "null" ]; then
  ok "(T-01-unmatched-post) post with no matching pre → appended with event_seq null"
else
  fail "(T-01-unmatched-post) mishandled (rc=$rc_ump out=$out_ump seq=$ump_seq)"
fi

# (T-01-marker-symmetry) .allow-compact suppresses telemetry on BOTH halves;
# precompact.sh exits before its own allow printf (empty stdout); postcompact.sh
# exits inside STEP 3 after the compact-happened marker is already written.
SID_MARK="sess-tel-marker"
touch "$TMPDIR_TEST/.allow-compact"
tel_before_mark=$(wc -l < "$TEL_SINK" 2>/dev/null | awk '{print $1}') || tel_before_mark=0
stdin_pre_mark=$(make_stdin_pre "auto" "$SID_MARK" "$TMPDIR_TEST")
rc_pre_mark=0
out_pre_mark=$(printf '%s' "$stdin_pre_mark" | sh "$HOOK_PRE" 2>/dev/null) || rc_pre_mark=$?
stdin_post_mark=$(make_stdin "$SID_MARK" "$TMPDIR_TEST" "$TRANSCRIPT")
rc_post_mark=0
out_post_mark=$(printf '%s' "$stdin_post_mark" | sh "$HOOK" 2>/dev/null) || rc_post_mark=$?
tel_after_mark=$(wc -l < "$TEL_SINK" 2>/dev/null | awk '{print $1}') || tel_after_mark=0
SENTINEL_MARK="$MEMORY_DIR/postcompact-reset-${SID_MARK}.txt"
COMPACT_HAPPENED_MARK="$MEMORY_DIR/compact-happened-${SID_MARK}.txt"
if [ "$rc_pre_mark" -eq 0 ] && [ -z "$out_pre_mark" ] && [ "$rc_post_mark" -eq 0 ] && [ -z "$out_post_mark" ] \
   && [ "$tel_after_mark" = "$tel_before_mark" ] && [ ! -f "$SENTINEL_MARK" ] && [ -f "$COMPACT_HAPPENED_MARK" ]; then
  ok "(T-01-marker-symmetry) .allow-compact suppresses telemetry on both hooks; compact-happened marker still written"
else
  fail "(T-01-marker-symmetry) marker symmetry mishandled (rc_pre=$rc_pre_mark out_pre=$out_pre_mark rc_post=$rc_post_mark out_post=$out_post_mark tel=$tel_before_mark->$tel_after_mark)"
fi
rm -f "$TMPDIR_TEST/.allow-compact"

# (T-01-stale-pre) a stale in-window pre (no more recent than the session's
# last recorded post) must not be adopted — the second post gets event_seq
# null rather than a duplicate of the already-claimed pair.
SID_STALE="sess-tel-stale-pre"
stdin_pre_stale=$(make_stdin_pre "auto" "$SID_STALE" "$TMPDIR_TEST")
printf '%s' "$stdin_pre_stale" | sh "$HOOK_PRE" >/dev/null 2>&1 || true
stdin_post_stale=$(make_stdin "$SID_STALE" "$TMPDIR_TEST" "$TRANSCRIPT")
printf '%s' "$stdin_post_stale" | sh "$HOOK" >/dev/null 2>&1 || true
stale_post_first=$(grep -F "\"session_id\":\"$SID_STALE\"" "$TEL_SINK" | grep -F '"half":"post"' | tail -1 | jq -r .event_seq)

# Preserve the sink while precompact.sh's own pre append is refused (a naive
# ln -sf at the existing sink would unlink it first, defeating the fixture):
# mv the real sink aside, plant a symlink so the refusal ladder rejects it,
# run precompact.sh (no second pre lands), then restore the real sink.
mv "$TEL_SINK" "$TMPDIR_TEST/stale-pre-sink-real.jsonl"
ln -s "$TMPDIR_TEST/stale-pre-dummy.jsonl" "$TEL_SINK"
printf '%s' "$stdin_pre_stale" | sh "$HOOK_PRE" >/dev/null 2>&1 || true
rm -f "$TEL_SINK"
mv "$TMPDIR_TEST/stale-pre-sink-real.jsonl" "$TEL_SINK"

printf '%s' "$stdin_post_stale" | sh "$HOOK" >/dev/null 2>&1 || true
stale_post_second=$(grep -F "\"session_id\":\"$SID_STALE\"" "$TEL_SINK" | grep -F '"half":"post"' | jq -r .event_seq | tail -1)
if [ "$stale_post_first" = "0" ] && [ "$stale_post_second" = "null" ]; then
  ok "(T-01-stale-pre) stale in-window pre is not adopted; second post gets event_seq null"
else
  fail "(T-01-stale-pre) stale-pre guard mishandled (first=$stale_post_first second=$stale_post_second)"
fi

# Mutation probe: a scratch copy without the max-post comparison would
# wrongly adopt the stale pre, duplicating the already-claimed event_seq 0.
MUTANT_STALE="$TMPDIR_TEST/postcompact-mutant-stalepre.sh"
python3 - "$HOOK" "$MUTANT_STALE" <<'PYEOF'
import sys
src, dst = sys.argv[1], sys.argv[2]
text = open(src).read()
needle = '''    if [ -n "$_tel_max_pre" ]; then
      if [ -z "$_tel_max_post" ] || [ "$_tel_max_pre" -gt "$_tel_max_post" ]; then
        _tel_seq="$_tel_max_pre"
      fi
    fi'''
replacement = '''    if [ -n "$_tel_max_pre" ]; then
      _tel_seq="$_tel_max_pre"
    fi'''
if needle not in text:
    sys.exit("mutation anchor not found")
open(dst, "w").write(text.replace(needle, replacement, 1))
PYEOF
chmod +x "$MUTANT_STALE"
SID_STALE_M="sess-tel-stale-pre-mutant"
stdin_pre_stale_m=$(make_stdin_pre "auto" "$SID_STALE_M" "$TMPDIR_TEST")
stdin_post_stale_m=$(make_stdin "$SID_STALE_M" "$TMPDIR_TEST" "$TRANSCRIPT")
printf '%s' "$stdin_pre_stale_m" | sh "$HOOK_PRE" >/dev/null 2>&1 || true
printf '%s' "$stdin_post_stale_m" | sh "$MUTANT_STALE" >/dev/null 2>&1 || true
mv "$TEL_SINK" "$TMPDIR_TEST/stale-pre-m-sink-real.jsonl"
ln -s "$TMPDIR_TEST/stale-pre-m-dummy.jsonl" "$TEL_SINK"
printf '%s' "$stdin_pre_stale_m" | sh "$HOOK_PRE" >/dev/null 2>&1 || true
rm -f "$TEL_SINK"
mv "$TMPDIR_TEST/stale-pre-m-sink-real.jsonl" "$TEL_SINK"
printf '%s' "$stdin_post_stale_m" | sh "$MUTANT_STALE" >/dev/null 2>&1 || true
stale_post_m_second=$(grep -F "\"session_id\":\"$SID_STALE_M\"" "$TEL_SINK" | grep -F '"half":"post"' | jq -r .event_seq | tail -1)
if [ "$stale_post_m_second" = "0" ]; then
  ok "(T-01-stale-pre-mutation) mutant without the max-post comparison wrongly adopts the stale pre — guard is load-bearing"
else
  fail "(T-01-stale-pre-mutation) mutant did not reproduce the false pair (got event_seq=$stale_post_m_second)"
fi
rm -f "$MUTANT_STALE"

# (T-01-rotate) QUOIN_TELEMETRY_MAX_BYTES=200, four compactions: both files
# exist, no .2, and every live pre pairs with its post in the same file.
SID_ROT="sess-tel-rotate"
stdin_pre_rot=$(make_stdin_pre "auto" "$SID_ROT" "$TMPDIR_TEST")
stdin_post_rot=$(make_stdin "$SID_ROT" "$TMPDIR_TEST" "$TRANSCRIPT")
_ri=0
while [ "$_ri" -lt 4 ]; do
  printf '%s' "$stdin_pre_rot" | QUOIN_TELEMETRY_MAX_BYTES=200 sh "$HOOK_PRE" >/dev/null 2>&1 || true
  printf '%s' "$stdin_post_rot" | sh "$HOOK" >/dev/null 2>&1 || true
  _ri=$((_ri + 1))
done
rotate_files=$(find "$TEL_DIR" -name 'compaction-events.jsonl*' 2>/dev/null | wc -l | awk '{print $1}')
rotate_same_file_ok=1
_live_pre_seqs=$(grep -F "\"session_id\":\"$SID_ROT\"" "$TEL_SINK" 2>/dev/null | grep -F '"half":"pre"' | jq -r .event_seq)
for _seq in $_live_pre_seqs; do
  _has_post=$(grep -F "\"session_id\":\"$SID_ROT\"" "$TEL_SINK" 2>/dev/null | grep -F '"half":"post"' | jq -r "select(.event_seq == $_seq)" 2>/dev/null)
  [ -n "$_has_post" ] || rotate_same_file_ok=0
done
if [ "$rotate_files" -eq 2 ] && [ -f "$TEL_SINK" ] && [ -f "${TEL_SINK}.1" ] && [ "$rotate_same_file_ok" -eq 1 ]; then
  ok "(T-01-rotate) rotation at the configured size leaves exactly two files; every live pre pairs in the same file"
else
  fail "(T-01-rotate) rotation mishandled (files=$rotate_files same_file_ok=$rotate_same_file_ok)"
fi

# (T-01-failopen) eight fixtures: four fail-OPEN pins (pre-STEP-3 early
# exits) plus four that reach STEP 3 with a positive assertion on the
# emitted record or fd-9 warning. All eight run under the watchdog bound as
# the practical, automatable proxy for "no /dev/tty open on any path" — the
# hook does no interactive read anywhere in its source (STEP -1 captures
# stdin exactly once), so a hang here can only mean a stray blocking open.

# pin 1: malformed stdin
run_hook_watchdog "$HOOK" "not json" ""
if [ "$WD_TIMEDOUT" -eq 0 ] && [ "$WD_RC" -eq 0 ] && [ -z "$WD_OUT" ]; then
  ok "(T-01-failopen-badstdin) malformed stdin → exit 0, empty stdout, no hang"
else
  fail "(T-01-failopen-badstdin) malformed stdin mishandled (rc=$WD_RC out=$WD_OUT timedout=$WD_TIMEDOUT)"
fi

# pin 2: jq stubbed absent from PATH
STUB_DIR_PC="$TMPDIR_TEST/stubpath-pc"
mkdir -p "$STUB_DIR_PC"
for _u in cat dirname date find grep sed awk ls mkdir mv wc basename head tr xargs rm sh; do
  _up=$(command -v "$_u" 2>/dev/null) || _up=""
  if [ -n "$_up" ]; then
    ln -s "$_up" "$STUB_DIR_PC/$_u" 2>/dev/null || true
  fi
done
stdin_nojq_pc=$(make_stdin "sess-pc-nojq" "$TMPDIR_TEST" "$TRANSCRIPT")
run_hook_watchdog "$HOOK" "$stdin_nojq_pc" "$STUB_DIR_PC"
if [ "$WD_TIMEDOUT" -eq 0 ] && [ "$WD_RC" -eq 0 ] && [ -z "$WD_OUT" ]; then
  ok "(T-01-failopen-nojq) jq absent → exit 0, empty stdout, no hang"
else
  fail "(T-01-failopen-nojq) jq absent mishandled (rc=$WD_RC out=$WD_OUT timedout=$WD_TIMEDOUT)"
fi

# pin 3: missing session_id
stdin_nosid_pc='{"cwd":"'"$TMPDIR_TEST"'","transcript_path":"'"$TRANSCRIPT"'"}'
run_hook_watchdog "$HOOK" "$stdin_nosid_pc" ""
if [ "$WD_TIMEDOUT" -eq 0 ] && [ "$WD_RC" -eq 0 ] && [ -z "$WD_OUT" ]; then
  ok "(T-01-failopen-nosid) missing session_id → exit 0, empty stdout, no hang"
else
  fail "(T-01-failopen-nosid) missing session_id mishandled (rc=$WD_RC out=$WD_OUT timedout=$WD_TIMEDOUT)"
fi

# pin 4: jq succeeds for the three top-level parses, fails only inside STEP 3
JQSTUB3_DIR="$TMPDIR_TEST/jqstub3"
mkdir -p "$JQSTUB3_DIR"
real_jq_pc=$(command -v jq)
JQ3_COUNTFILE="$TMPDIR_TEST/jqstub3-count"
printf '0' > "$JQ3_COUNTFILE"
cat > "$JQSTUB3_DIR/jq" <<JQEOF
#!/bin/sh
_n=\$(cat "$JQ3_COUNTFILE" 2>/dev/null || echo 0)
_n=\$((_n + 1))
printf '%s' "\$_n" > "$JQ3_COUNTFILE"
if [ "\$_n" -gt 3 ]; then
  exit 1
fi
exec "$real_jq_pc" "\$@"
JQEOF
chmod +x "$JQSTUB3_DIR/jq"
SID_S3="sess-pc-step3fail"
stdin_step3fail=$(make_stdin "$SID_S3" "$TMPDIR_TEST" "$TRANSCRIPT")
tel_before_s3=$(grep -cF "\"session_id\":\"$SID_S3\"" "$TEL_SINK" 2>/dev/null) || tel_before_s3=0
run_hook_watchdog "$HOOK" "$stdin_step3fail" "$JQSTUB3_DIR:$PATH"
tel_after_s3=$(grep -cF "\"session_id\":\"$SID_S3\"" "$TEL_SINK" 2>/dev/null) || tel_after_s3=0
SENTINEL_S3="$MEMORY_DIR/postcompact-reset-${SID_S3}.txt"
COMPACT_HAPPENED_S3="$MEMORY_DIR/compact-happened-${SID_S3}.txt"
if [ "$WD_TIMEDOUT" -eq 0 ] && [ "$WD_RC" -eq 0 ] && [ -z "$WD_OUT" ] \
   && [ "$tel_after_s3" = "$tel_before_s3" ] && [ ! -f "$SENTINEL_S3" ] && [ -f "$COMPACT_HAPPENED_S3" ]; then
  ok "(T-01-failopen-step3jq) jq failing only inside STEP 3 → exit 0, empty stdout, marker unaffected, nothing appended"
else
  fail "(T-01-failopen-step3jq) step3-only jq failure mishandled (rc=$WD_RC out=$WD_OUT tel=$tel_before_s3->$tel_after_s3)"
fi

# reaches-STEP-3 fixture 1: unreadable transcript → null bytes_after/est_tokens_after
UNREAD_TRANSCRIPT="$TMPDIR_TEST/unreadable.jsonl"
printf '{"type":"message"}\n' > "$UNREAD_TRANSCRIPT"
chmod 000 "$UNREAD_TRANSCRIPT"
SID_UNREAD="sess-pc-unread"
stdin_unread=$(make_stdin "$SID_UNREAD" "$TMPDIR_TEST" "$UNREAD_TRANSCRIPT")
run_hook_watchdog "$HOOK" "$stdin_unread" ""
post_unread=$(grep -F "\"session_id\":\"$SID_UNREAD\"" "$TEL_SINK" | tail -1)
ba_unread=$(printf '%s' "$post_unread" | jq -r .bytes_after)
eta_unread=$(printf '%s' "$post_unread" | jq -r .est_tokens_after)
if [ "$WD_TIMEDOUT" -eq 0 ] && [ "$WD_RC" -eq 0 ] && [ -z "$WD_OUT" ] && [ "$ba_unread" = "null" ] && [ "$eta_unread" = "null" ]; then
  ok "(T-01-failopen-unread) unreadable transcript → bytes_after/est_tokens_after null"
else
  fail "(T-01-failopen-unread) unreadable transcript mishandled (rc=$WD_RC ba=$ba_unread eta=$eta_unread)"
fi
chmod 644 "$UNREAD_TRANSCRIPT" 2>/dev/null || true

# reaches-STEP-3 fixture 2: MEMORY_DIR unwritable (chmod 555 before telemetry/
# is created there) → fd-9 warning; a fresh project root keeps this isolated
# from every other T-01 fixture's already-created telemetry/ dir. No-op under
# root, since root ignores directory permission bits.
if [ "$(id -u)" = "0" ]; then
  ok "(T-01-failopen-unwritable) skipped under root (directory permission bits are a no-op)"
else
  # A SIBLING of TMPDIR_TEST, not a child: resolve_project_root picks the
  # outermost .workflow_artifacts ancestor, so a project root nested inside
  # TMPDIR_TEST would resolve back to TMPDIR_TEST's own already-populated
  # MEMORY_DIR instead of this fixture's clean one.
  UNWRITABLE_ROOT="${TMPDIR_TEST}-unwritable-root"
  mkdir -p "$UNWRITABLE_ROOT/.workflow_artifacts/memory"
  UNWRITABLE_MEMDIR="$UNWRITABLE_ROOT/.workflow_artifacts/memory"
  UNWRIT_TRANSCRIPT="$TMPDIR_TEST/unwrit-transcript.jsonl"
  printf '{"type":"message"}\n' > "$UNWRIT_TRANSCRIPT"
  chmod 555 "$UNWRITABLE_MEMDIR"
  SID_UNWRIT="sess-pc-unwritable"
  stdin_unwrit=$(make_stdin "$SID_UNWRIT" "$UNWRITABLE_ROOT" "$UNWRIT_TRANSCRIPT")
  rc_unwrit=0
  stdout_unwrit=$(printf '%s' "$stdin_unwrit" | sh "$HOOK" 2>"$TMPDIR_TEST/unwrit-stderr.txt") || rc_unwrit=$?
  stderr_unwrit=$(cat "$TMPDIR_TEST/unwrit-stderr.txt" 2>/dev/null)
  if [ "$rc_unwrit" -eq 0 ] && [ -z "$stdout_unwrit" ] && printf '%s' "$stderr_unwrit" | grep -qF '[quoin-postcompact] WARNING: cannot create telemetry dir'; then
    ok "(T-01-failopen-unwritable) MEMORY_DIR-unwritable → exit 0, empty stdout, fd-9 warning surfaced"
  else
    fail "(T-01-failopen-unwritable) MEMORY_DIR-unwritable mishandled (rc=$rc_unwrit out=$stdout_unwrit stderr=$stderr_unwrit)"
  fi
  chmod 755 "$UNWRITABLE_MEMDIR" 2>/dev/null || true
  rm -rf "$UNWRITABLE_ROOT"
fi

# reaches-STEP-3 fixture 3: non-string compact_summary → compact_summary_len null
SID_CSTYPE="sess-pc-cstype"
stdin_cstype=$(jq -nc --arg sid "$SID_CSTYPE" --arg cwd "$TMPDIR_TEST" --arg tp "$TRANSCRIPT" '{session_id:$sid, cwd:$cwd, transcript_path:$tp, compact_summary: 12345}')
run_hook_watchdog "$HOOK" "$stdin_cstype" ""
cslen_val=$(grep -F "\"session_id\":\"$SID_CSTYPE\"" "$TEL_SINK" | tail -1 | jq -r .compact_summary_len)
if [ "$WD_TIMEDOUT" -eq 0 ] && [ "$WD_RC" -eq 0 ] && [ -z "$WD_OUT" ] && [ "$cslen_val" = "null" ]; then
  ok "(T-01-failopen-cstype) non-string compact_summary → compact_summary_len null"
else
  fail "(T-01-failopen-cstype) non-string compact_summary mishandled (rc=$WD_RC cslen=$cslen_val)"
fi

# reaches-STEP-3 fixture 4: absent trigger → trigger empty string
SID_NOTRIG="sess-pc-notrig"
stdin_notrig=$(jq -nc --arg sid "$SID_NOTRIG" --arg cwd "$TMPDIR_TEST" --arg tp "$TRANSCRIPT" '{session_id:$sid, cwd:$cwd, transcript_path:$tp}')
run_hook_watchdog "$HOOK" "$stdin_notrig" ""
trig_val=$(grep -F "\"session_id\":\"$SID_NOTRIG\"" "$TEL_SINK" | tail -1 | jq -r .trigger)
if [ "$WD_TIMEDOUT" -eq 0 ] && [ "$WD_RC" -eq 0 ] && [ -z "$WD_OUT" ] && [ "$trig_val" = "" ]; then
  ok "(T-01-failopen-notrigger) absent trigger → trigger empty string"
else
  fail "(T-01-failopen-notrigger) absent trigger mishandled (rc=$WD_RC trig='$trig_val')"
fi

# Mutation probe: delete STEP 3 in a scratch copy — the four pins must stay
# green (their exit unaffected by STEP 3's presence), while the record-based
# fixtures above become unsatisfiable (STEP 3 is the only code path that
# appends anything for these session ids).
MUTANT_NOSTEP3="$TMPDIR_TEST/postcompact-mutant-nostep3.sh"
python3 - "$HOOK" "$MUTANT_NOSTEP3" <<'PYEOF'
import sys
src, dst = sys.argv[1], sys.argv[2]
text = open(src).read()
marker = "# STEP 3: Telemetry"
idx = text.find(marker)
if idx == -1:
    sys.exit("STEP 3 marker not found")
open(dst, "w").write(text[:idx] + "exit 0\n")
PYEOF
chmod +x "$MUTANT_NOSTEP3"

rc_mp1=0; out_mp1=$(printf 'not json' | sh "$MUTANT_NOSTEP3" 2>/dev/null) || rc_mp1=$?
stdin_mp3=$(make_stdin "sess-pc-mutant-nosid-check" "$TMPDIR_TEST" "$TRANSCRIPT")
rc_mp3=0; out_mp3=$(printf '%s' "$stdin_mp3" | sh "$MUTANT_NOSTEP3" 2>/dev/null) || rc_mp3=$?
if [ "$rc_mp1" -eq 0 ] && [ -z "$out_mp1" ] && [ "$rc_mp3" -eq 0 ] && [ -z "$out_mp3" ]; then
  ok "(T-01-failopen-mutation-pins) STEP-3 deletion leaves the fail-OPEN pins green"
else
  fail "(T-01-failopen-mutation-pins) pins regressed under the STEP-3-deleted mutant (rc1=$rc_mp1 rc3=$rc_mp3)"
fi

MUT_UNREAD_SID="sess-pc-mutant-unread"
stdin_mu1=$(make_stdin "$MUT_UNREAD_SID" "$TMPDIR_TEST" "$UNREAD_TRANSCRIPT")
printf '%s' "$stdin_mu1" | sh "$MUTANT_NOSTEP3" >/dev/null 2>&1 || true
MUT_CSTYPE_SID="sess-pc-mutant-cstype"
stdin_mu2=$(jq -nc --arg sid "$MUT_CSTYPE_SID" --arg cwd "$TMPDIR_TEST" --arg tp "$TRANSCRIPT" '{session_id:$sid, cwd:$cwd, transcript_path:$tp, compact_summary: 999}')
printf '%s' "$stdin_mu2" | sh "$MUTANT_NOSTEP3" >/dev/null 2>&1 || true
MUT_NOTRIG_SID="sess-pc-mutant-notrig"
stdin_mu3=$(jq -nc --arg sid "$MUT_NOTRIG_SID" --arg cwd "$TMPDIR_TEST" --arg tp "$TRANSCRIPT" '{session_id:$sid, cwd:$cwd, transcript_path:$tp}')
printf '%s' "$stdin_mu3" | sh "$MUTANT_NOSTEP3" >/dev/null 2>&1 || true
mut_hits=$(grep -cF -e "\"session_id\":\"$MUT_UNREAD_SID\"" -e "\"session_id\":\"$MUT_CSTYPE_SID\"" -e "\"session_id\":\"$MUT_NOTRIG_SID\"" "$TEL_SINK" 2>/dev/null) || mut_hits=0
if [ "$mut_hits" -eq 0 ]; then
  ok "(T-01-failopen-mutation) STEP-3 deletion makes the record-based fixtures unsatisfiable (no lines appended)"
else
  fail "(T-01-failopen-mutation) mutant unexpectedly appended $mut_hits record(s) with STEP 3 removed"
fi
rm -f "$MUTANT_NOSTEP3"

# (T-01-hostile) sink probes at postcompact.sh: symlinked telemetry dir,
# symlinked sink, FIFO sink, FIFO where the dir should be, hard-linked sink.
# Each asserts nothing written through the planted object, exit 0, empty stdout.

TEL_OUTSIDE_PC="$TMPDIR_TEST/pc-tel-outside"
mkdir -p "$TEL_OUTSIDE_PC"
mv "$TEL_DIR" "$TMPDIR_TEST/pc-telemetry-real"
ln -s "$TEL_OUTSIDE_PC" "$TEL_DIR"
SID_H1="sess-pc-hostile-symdir"
stdin_h1=$(make_stdin "$SID_H1" "$TMPDIR_TEST" "$TRANSCRIPT")
rc_h1=0; out_h1=$(printf '%s' "$stdin_h1" | sh "$HOOK" 2>/dev/null) || rc_h1=$?
if [ "$rc_h1" -eq 0 ] && [ -z "$out_h1" ] && [ ! -f "$TEL_OUTSIDE_PC/compaction-events.jsonl" ]; then
  ok "(T-01-hostile-symdir) symlinked telemetry dir → no write-through"
else
  fail "(T-01-hostile-symdir) symlinked telemetry dir mishandled (rc=$rc_h1 out=$out_h1)"
fi
rm -f "$TEL_DIR"
mv "$TMPDIR_TEST/pc-telemetry-real" "$TEL_DIR"
rm -rf "$TEL_OUTSIDE_PC"

printf 'victim\n' > "$TMPDIR_TEST/pc-tel-victim.jsonl"
mv "$TEL_SINK" "$TMPDIR_TEST/pc-tel-sink-real.jsonl"
ln -s "$TMPDIR_TEST/pc-tel-victim.jsonl" "$TEL_SINK"
SID_H2="sess-pc-hostile-symfile"
stdin_h2=$(make_stdin "$SID_H2" "$TMPDIR_TEST" "$TRANSCRIPT")
rc_h2=0; out_h2=$(printf '%s' "$stdin_h2" | sh "$HOOK" 2>/dev/null) || rc_h2=$?
if [ "$rc_h2" -eq 0 ] && [ -z "$out_h2" ] && [ "$(cat "$TMPDIR_TEST/pc-tel-victim.jsonl")" = "victim" ]; then
  ok "(T-01-hostile-symfile) symlinked telemetry sink → no write-through"
else
  fail "(T-01-hostile-symfile) symlinked telemetry sink mishandled (rc=$rc_h2 out=$out_h2)"
fi
rm -f "$TEL_SINK" "$TMPDIR_TEST/pc-tel-victim.jsonl"
mv "$TMPDIR_TEST/pc-tel-sink-real.jsonl" "$TEL_SINK"

mv "$TEL_SINK" "$TMPDIR_TEST/pc-tel-sink-real2.jsonl"
mkfifo "$TEL_SINK"
SID_H3="sess-pc-hostile-fifo"
stdin_h3=$(make_stdin "$SID_H3" "$TMPDIR_TEST" "$TRANSCRIPT")
run_hook_watchdog "$HOOK" "$stdin_h3" ""
if [ "$WD_TIMEDOUT" -eq 0 ] && [ "$WD_RC" -eq 0 ] && [ -z "$WD_OUT" ]; then
  ok "(T-01-hostile-fifo) FIFO telemetry sink → refused without hanging"
else
  fail "(T-01-hostile-fifo) FIFO telemetry sink mishandled (rc=$WD_RC out=$WD_OUT timedout=$WD_TIMEDOUT)"
fi
rm -f "$TEL_SINK"
mv "$TMPDIR_TEST/pc-tel-sink-real2.jsonl" "$TEL_SINK"

mv "$TEL_DIR" "$TMPDIR_TEST/pc-telemetry-real3"
mkfifo "$TEL_DIR"
SID_H4="sess-pc-hostile-fifodir"
stdin_h4=$(make_stdin "$SID_H4" "$TMPDIR_TEST" "$TRANSCRIPT")
run_hook_watchdog "$HOOK" "$stdin_h4" ""
if [ "$WD_TIMEDOUT" -eq 0 ] && [ "$WD_RC" -eq 0 ] && [ -z "$WD_OUT" ]; then
  ok "(T-01-hostile-fifodir) FIFO telemetry dir → refused without hanging"
else
  fail "(T-01-hostile-fifodir) FIFO telemetry dir mishandled (rc=$WD_RC out=$WD_OUT timedout=$WD_TIMEDOUT)"
fi
rm -f "$TEL_DIR"
mv "$TMPDIR_TEST/pc-telemetry-real3" "$TEL_DIR"

printf 'victim\n' > "$TMPDIR_TEST/pc-tel-hl-victim.jsonl"
mv "$TEL_SINK" "$TMPDIR_TEST/pc-tel-sink-real3.jsonl"
ln "$TMPDIR_TEST/pc-tel-hl-victim.jsonl" "$TEL_SINK"
SID_H5="sess-pc-hostile-hardlink"
stdin_h5=$(make_stdin "$SID_H5" "$TMPDIR_TEST" "$TRANSCRIPT")
rc_h5=0; out_h5=$(printf '%s' "$stdin_h5" | sh "$HOOK" 2>/dev/null) || rc_h5=$?
if [ "$rc_h5" -eq 0 ] && [ -z "$out_h5" ] && [ "$(cat "$TMPDIR_TEST/pc-tel-hl-victim.jsonl")" = "victim" ]; then
  ok "(T-01-hostile-hardlink) hard-linked telemetry sink → nothing appended through the link"
else
  fail "(T-01-hostile-hardlink) hard-linked telemetry sink mishandled (rc=$rc_h5 out=$out_h5)"
fi
rm -f "$TEL_SINK" "$TMPDIR_TEST/pc-tel-hl-victim.jsonl"
mv "$TMPDIR_TEST/pc-tel-sink-real3.jsonl" "$TEL_SINK"

# ─── deployed hook check (if present) ────────────────────────────────────────

if [ -f "$DEPLOYED_HOOK" ]; then
  if head -1 "$DEPLOYED_HOOK" | grep -qE '^#!/bin/sh( |$)'; then
    ok "shebang: deployed hook starts with #!/bin/sh"
  else
    fail "shebang: deployed hook does not start with #!/bin/sh"
  fi
fi

# ─── summary ─────────────────────────────────────────────────────────────────

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
if [ "$FAIL" -gt 0 ]; then
  printf 'Failures:%b\n' "$FAIL_MSGS" >&2
  exit 1
fi
