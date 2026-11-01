#!/bin/sh
# test_sessionend_verify.sh — Tests for sessionend.sh STEP 5b/7/7b §V backstop (T-12/T-09)
# Run from project root: bash quoin/dev/tests/test_sessionend_verify.sh
# All tests exit 0 on PASS, emit "FAIL: <reason>" and set FAIL counter.
# Style: POSIX sh, mktemp, trap-based cleanup.
#
# Covers T-12 acceptance (a)-(j):
#   (a) manifest-present MISMATCH folded into the single systemMessage
#   (b) manifest-absent at EOD-class -> absent banner
#   (c) no gh invocation (--finalized-only always)
#   (d) verify_claims.py absent on a non-EOD-class / eod_due==0 fixture -> silent exit 0
#   (e) nudge fires iff eod_due==1 and the session file is never modified
#   (f) exactly one systemMessage JSON emitted in every branch
#   (g) present-but-EMPTY manifest WITH in-window work -> empty-manifest banner
#   (h) hook audit line goes to the DISTINCT .hookaudit.md sibling; manifest path
#       stays absent so a second same-day run still bangs the absent banner
#   (i) quiet-day no-false-banner: present-but-EMPTY manifest with only
#       out-of-window finalized/ folders -> no banner, exit 0
#   (j) eod_due==1 + wrapper-absent -> STEP 5b falls through (no exit); nudge
#       still fires and no Close snapshot is appended

PASS=0; FAIL=0
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HOOKS_DIR="$SCRIPT_DIR/../../hooks"

pass() { echo "PASS: $1"; PASS=$((PASS+1)); }
fail() { echo "FAIL: $1"; FAIL=$((FAIL+1)); }
skip() { echo "SKIP: $1"; PASS=$((PASS+1)); }

TODAY=$(date +%Y-%m-%d)

# Build a fake hooks/ tree (sessionend.sh + _lib.sh only, no sibling scripts/
# dir) so WRAPPER="$(dirname "$0")/../scripts/verify_claims.py" resolves to a
# path that does not exist -- simulates a deploy where the wrapper failed to
# ship (r5/MINOR-1).
_make_wrapperless_hooks() {
  fake_root=$(mktemp -d)
  mkdir -p "$fake_root/hooks"
  cp "$HOOKS_DIR/sessionend.sh" "$fake_root/hooks/sessionend.sh"
  cp "$HOOKS_DIR/_lib.sh" "$fake_root/hooks/_lib.sh"
  printf '%s' "$fake_root/hooks/sessionend.sh"
}

# ── Test A (acceptance a): manifest-present MISMATCH -> folded into one systemMessage ──
echo ""
echo "Test A: manifest-present MISMATCH -> §V banner folded into single systemMessage"
TA_WORLD=$(mktemp -d)
TA_HOME=$(mktemp -d)
trap 'rm -rf "$TA_WORLD" "$TA_HOME"' EXIT

mkdir -p "$TA_WORLD/.workflow_artifacts/memory/sessions"
mkdir -p "$TA_WORLD/.workflow_artifacts/memory/daily"
mkdir -p "$TA_WORLD/.workflow_artifacts/memory/verification"
mkdir -p "$TA_WORLD/.workflow_artifacts/finalized/ivg-105-thing"

printf '## Cost\nend_of_day_due: yes\n' > "$TA_WORLD/.workflow_artifacts/memory/sessions/${TODAY}-ta-task.md"
printf 'daily cache\n' > "$TA_WORLD/.workflow_artifacts/memory/daily/${TODAY}.md"
printf '## Claims\n```yaml\n- task_ref: "IVG-105"\n  status: awaiting_end_of_task\n```\n' \
  > "$TA_WORLD/.workflow_artifacts/memory/verification/end_of_day-${TODAY}.md"

TA_STDIN=$(printf '{"cwd":"%s"}' "$TA_WORLD")
TA_OUT=$(printf '%s' "$TA_STDIN" | env HOME="$TA_HOME" sh "$HOOKS_DIR/sessionend.sh" 2>/dev/null)

if printf '%s' "$TA_OUT" | grep -q 'claims contradict ground truth' \
   && printf '%s' "$TA_OUT" | grep -q 'ivg-105-thing' \
   && printf '%s' "$TA_OUT" | grep -q 'quoin-S-4'; then
  pass "Test A — MISMATCH banner + S-4 nudge folded into one systemMessage"
else
  fail "Test A — expected MISMATCH banner + nudge in output, got: $TA_OUT"
fi
trap - EXIT
rm -rf "$TA_WORLD" "$TA_HOME"

# ── Test B (acceptance b + h): manifest-absent -> absent banner, no self-erase on re-fire ──
echo ""
echo "Test B: manifest-absent at EOD-class -> absent banner; second same-day fire repeats it"
TB_WORLD=$(mktemp -d)
TB_HOME=$(mktemp -d)
trap 'rm -rf "$TB_WORLD" "$TB_HOME"' EXIT

mkdir -p "$TB_WORLD/.workflow_artifacts/memory/sessions"
mkdir -p "$TB_WORLD/.workflow_artifacts/memory/daily"
printf '## Cost\nend_of_day_due: yes\n' > "$TB_WORLD/.workflow_artifacts/memory/sessions/${TODAY}-tb-task.md"
printf 'daily cache\n' > "$TB_WORLD/.workflow_artifacts/memory/daily/${TODAY}.md"

TB_STDIN=$(printf '{"cwd":"%s"}' "$TB_WORLD")
TB_OUT1=$(printf '%s' "$TB_STDIN" | env HOME="$TB_HOME" sh "$HOOKS_DIR/sessionend.sh" 2>/dev/null)
TB_MANIFEST="$TB_WORLD/.workflow_artifacts/memory/verification/end_of_day-${TODAY}.md"
TB_HOOKAUDIT="$TB_WORLD/.workflow_artifacts/memory/verification/end_of_day-${TODAY}.hookaudit.md"

TB1_OK=0
if printf '%s' "$TB_OUT1" | grep -q 'wrote no verification manifest' \
   && [ ! -f "$TB_MANIFEST" ] \
   && [ -f "$TB_HOOKAUDIT" ] && grep -q 'manifest-absent' "$TB_HOOKAUDIT"; then
  TB1_OK=1
fi

# Second same-day fire — manifest must still be absent, banner must repeat (no self-erase)
TB_OUT2=$(printf '%s' "$TB_STDIN" | env HOME="$TB_HOME" sh "$HOOKS_DIR/sessionend.sh" 2>/dev/null)
TB2_OK=0
if printf '%s' "$TB_OUT2" | grep -q 'wrote no verification manifest' && [ ! -f "$TB_MANIFEST" ]; then
  TB2_OK=1
fi

if [ "$TB1_OK" -eq 1 ] && [ "$TB2_OK" -eq 1 ]; then
  pass "Test B — absent banner on both fires; manifest path stays absent (no self-erase); hookaudit sibling written"
else
  fail "Test B — first_ok=$TB1_OK second_ok=$TB2_OK out1=$TB_OUT1 out2=$TB_OUT2"
fi
trap - EXIT
rm -rf "$TB_WORLD" "$TB_HOME"

# ── Test C (acceptance c): no gh invocation ──────────────────────────────────
echo ""
echo "Test C: hook never invokes gh (--finalized-only always)"
TC_WORLD=$(mktemp -d)
TC_HOME=$(mktemp -d)
TC_BIN=$(mktemp -d)
trap 'rm -rf "$TC_WORLD" "$TC_HOME" "$TC_BIN"' EXIT

mkdir -p "$TC_WORLD/.workflow_artifacts/memory/sessions"
mkdir -p "$TC_WORLD/.workflow_artifacts/memory/daily"
mkdir -p "$TC_WORLD/.workflow_artifacts/memory/verification"
mkdir -p "$TC_WORLD/.workflow_artifacts/finalized/ivg-999-other"
printf '## Cost\nend_of_day_due: yes\n' > "$TC_WORLD/.workflow_artifacts/memory/sessions/${TODAY}-tc-task.md"
printf 'daily cache\n' > "$TC_WORLD/.workflow_artifacts/memory/daily/${TODAY}.md"
printf '## Claims\n```yaml\n- task_ref: "IVG-999"\n  status: awaiting_pr\n```\n' \
  > "$TC_WORLD/.workflow_artifacts/memory/verification/end_of_day-${TODAY}.md"

TC_MARKER="$TC_WORLD/gh-was-called.marker"
cat > "$TC_BIN/gh" <<EOF
#!/bin/sh
touch "$TC_MARKER"
echo '[]'
EOF
chmod +x "$TC_BIN/gh"

TC_STDIN=$(printf '{"cwd":"%s"}' "$TC_WORLD")
printf '%s' "$TC_STDIN" | env HOME="$TC_HOME" PATH="$TC_BIN:$PATH" sh "$HOOKS_DIR/sessionend.sh" >/dev/null 2>&1

if [ ! -f "$TC_MARKER" ]; then
  pass "Test C — gh binary never invoked (finalized-only backbone only)"
else
  fail "Test C — gh binary was invoked despite the hook's --finalized-only contract"
fi
trap - EXIT
rm -rf "$TC_WORLD" "$TC_HOME" "$TC_BIN"

# ── Test D (acceptance d): wrapper absent + non-EOD-class/eod_due==0 -> silent exit 0 ──
echo ""
echo "Test D: verify_claims.py absent + eod_due==0 + non-EOD-class -> silent exit 0"
TD_WORLD=$(mktemp -d)
TD_HOME=$(mktemp -d)
trap 'rm -rf "$TD_WORLD" "$TD_HOME"' EXIT

mkdir -p "$TD_WORLD/.workflow_artifacts/memory/sessions"
printf '## Cost\nend_of_day_due: no\n' > "$TD_WORLD/.workflow_artifacts/memory/sessions/${TODAY}-td-task.md"
# No daily/<today>.md -> non-EOD-class

TD_HOOK=$(_make_wrapperless_hooks)
TD_STDIN=$(printf '{"cwd":"%s"}' "$TD_WORLD")
TD_OUT=$(printf '%s' "$TD_STDIN" | env HOME="$TD_HOME" sh "$TD_HOOK" 2>/dev/null)
TD_EXIT=$?
rm -rf "$(dirname "$(dirname "$TD_HOOK")")" 2>/dev/null || true

if [ "$TD_EXIT" -eq 0 ] && [ -z "$TD_OUT" ]; then
  pass "Test D — wrapper-absent + eod_due=0 + non-EOD-class: fully silent, exit 0"
else
  fail "Test D — exit=$TD_EXIT output=$TD_OUT (expected silent exit 0)"
fi
trap - EXIT
rm -rf "$TD_WORLD" "$TD_HOME"

# ── Test E (acceptance e): nudge fires iff eod_due==1; session file is never modified ──
echo ""
echo "Test E: nudge fires iff eod_due==1, and the session file is never modified"

_run_nudge_fixture() {
  # $1 = end_of_day_due value ("yes" or "no"); prints "SKIP ..." or "changed nudge world home"
  world=$(mktemp -d)
  home=$(mktemp -d)
  mkdir -p "$world/.workflow_artifacts/memory/sessions"
  mkdir -p "$world/.workflow_artifacts/memory/daily"
  session_file="$world/.workflow_artifacts/memory/sessions/${TODAY}-te-task.md"
  printf '## Cost\nend_of_day_due: %s\n' "$1" > "$session_file"
  printf 'daily cache\n' > "$world/.workflow_artifacts/memory/daily/${TODAY}.md"

  # Fake project transcript newer than the session file: with the old snapshot
  # step present this setup made the hook append to the session file.
  proj_hash=$(printf '%s' "$world" | sed 's|/|-|g')
  jsonl_dir="$home/.claude/projects/$proj_hash"
  mkdir -p "$jsonl_dir"
  printf '{}' > "$jsonl_dir/deadbeef-0000-1234-5678-abcdef000099.jsonl"

  thirty_min_ago=$(date -v-30M +%Y%m%d%H%M 2>/dev/null) || thirty_min_ago=""
  if [ -z "$thirty_min_ago" ] || ! touch -t "${thirty_min_ago}" "$session_file" 2>/dev/null; then
    echo "SKIP $session_file $world $home"
    return
  fi

  before=$(cksum < "$session_file")
  stdin=$(printf '{"cwd":"%s"}' "$world")
  out=$(printf '%s' "$stdin" | env HOME="$home" sh "$HOOKS_DIR/sessionend.sh" 2>/dev/null)
  after=$(cksum < "$session_file")
  changed=0; [ "$before" = "$after" ] || changed=1
  nudge=0; printf '%s' "$out" | grep -q 'quoin-S-4' && nudge=1
  echo "$changed $nudge $world $home"
}

TE1_RESULT=$(_run_nudge_fixture "yes")
TE1_CHANGED=$(printf '%s' "$TE1_RESULT" | awk '{print $1}')
TE1_NUDGE=$(printf '%s' "$TE1_RESULT" | awk '{print $2}')
TE1_WORLD=$(printf '%s' "$TE1_RESULT" | awk '{print $3}')
TE1_HOME=$(printf '%s' "$TE1_RESULT" | awk '{print $4}')

TE2_RESULT=$(_run_nudge_fixture "no")
TE2_CHANGED=$(printf '%s' "$TE2_RESULT" | awk '{print $1}')
TE2_NUDGE=$(printf '%s' "$TE2_RESULT" | awk '{print $2}')
TE2_WORLD=$(printf '%s' "$TE2_RESULT" | awk '{print $3}')
TE2_HOME=$(printf '%s' "$TE2_RESULT" | awk '{print $4}')

if [ "$TE1_CHANGED" = "SKIP" ] || [ "$TE2_CHANGED" = "SKIP" ]; then
  skip "Test E — touch -t not available on this platform; nudge test cannot run"
else
  if [ "$TE1_NUDGE" -eq 1 ] && [ "$TE1_CHANGED" -eq 0 ] && [ "$TE2_NUDGE" -eq 0 ] && [ "$TE2_CHANGED" -eq 0 ]; then
    pass "Test E — nudge fires when eod_due=yes, withheld when no; session file never modified"
  else
    fail "Test E — yes: nudge=$TE1_NUDGE changed=$TE1_CHANGED; no: nudge=$TE2_NUDGE changed=$TE2_CHANGED (expected 1/0 and 0/0)"
  fi
fi
rm -rf "$TE1_WORLD" "$TE1_HOME" "$TE2_WORLD" "$TE2_HOME" 2>/dev/null || true

# ── Test F (acceptance f): exactly one systemMessage JSON in every branch ────
echo ""
echo "Test F: exactly one systemMessage JSON object emitted (never zero-or-two lines of it)"
TF_WORLD=$(mktemp -d)
TF_HOME=$(mktemp -d)
trap 'rm -rf "$TF_WORLD" "$TF_HOME"' EXIT

mkdir -p "$TF_WORLD/.workflow_artifacts/memory/sessions"
mkdir -p "$TF_WORLD/.workflow_artifacts/memory/daily"
mkdir -p "$TF_WORLD/.workflow_artifacts/memory/verification"
mkdir -p "$TF_WORLD/.workflow_artifacts/finalized/ivg-105-thing"
printf '## Cost\nend_of_day_due: yes\n' > "$TF_WORLD/.workflow_artifacts/memory/sessions/${TODAY}-tf-task.md"
printf 'daily cache\n' > "$TF_WORLD/.workflow_artifacts/memory/daily/${TODAY}.md"
printf '## Claims\n```yaml\n- task_ref: "IVG-105"\n  status: awaiting_end_of_task\n```\n' \
  > "$TF_WORLD/.workflow_artifacts/memory/verification/end_of_day-${TODAY}.md"

TF_STDIN=$(printf '{"cwd":"%s"}' "$TF_WORLD")
TF_OUT=$(printf '%s' "$TF_STDIN" | env HOME="$TF_HOME" sh "$HOOKS_DIR/sessionend.sh" 2>/dev/null)
TF_MSG_LINES=$(printf '%s\n' "$TF_OUT" | grep -c 'systemMessage'); TF_MSG_LINES=${TF_MSG_LINES:-0}
TF_TOTAL_LINES=$(printf '%s\n' "$TF_OUT" | grep -c '.'); TF_TOTAL_LINES=${TF_TOTAL_LINES:-0}

if [ "$TF_MSG_LINES" -eq 1 ] && [ "$TF_TOTAL_LINES" -eq 1 ]; then
  pass "Test F — exactly one systemMessage JSON line emitted (nudge + banner folded together)"
else
  fail "Test F — systemMessage_lines=$TF_MSG_LINES total_lines=$TF_TOTAL_LINES output=$TF_OUT"
fi
trap - EXIT
rm -rf "$TF_WORLD" "$TF_HOME"

# ── Test G (acceptance g): present-but-EMPTY manifest WITH in-window work -> empty-manifest banner ──
echo ""
echo "Test G: present-but-EMPTY manifest with in-window finalized/ work -> empty-manifest banner"
TG_WORLD=$(mktemp -d)
TG_HOME=$(mktemp -d)
trap 'rm -rf "$TG_WORLD" "$TG_HOME"' EXIT

mkdir -p "$TG_WORLD/.workflow_artifacts/memory/sessions"
mkdir -p "$TG_WORLD/.workflow_artifacts/memory/daily"
mkdir -p "$TG_WORLD/.workflow_artifacts/memory/verification"
mkdir -p "$TG_WORLD/.workflow_artifacts/finalized/ivg-200-thing"
printf '## Cost\nend_of_day_due: yes\n' > "$TG_WORLD/.workflow_artifacts/memory/sessions/${TODAY}-tg-task.md"
printf 'daily cache\n' > "$TG_WORLD/.workflow_artifacts/memory/daily/${TODAY}.md"
printf '## Claims\n```yaml\n```\n' > "$TG_WORLD/.workflow_artifacts/memory/verification/end_of_day-${TODAY}.md"
# finalized/ivg-200-thing mtime defaults to "now" -> L=today (no prior daily/*.md) -> in-window

TG_STDIN=$(printf '{"cwd":"%s"}' "$TG_WORLD")
TG_OUT=$(printf '%s' "$TG_STDIN" | env HOME="$TG_HOME" sh "$HOOKS_DIR/sessionend.sh" 2>/dev/null)

if printf '%s' "$TG_OUT" | grep -q 'empty verification manifest' && printf '%s' "$TG_OUT" | grep -q 'ivg-200-thing'; then
  pass "Test G — empty-manifest banner names the unclaimed in-window task"
else
  fail "Test G — expected empty-manifest banner naming ivg-200-thing, got: $TG_OUT"
fi
trap - EXIT
rm -rf "$TG_WORLD" "$TG_HOME"

# ── Test I (acceptance i): quiet-day no-false-banner — out-of-window finalized/ only ──
echo ""
echo "Test I: present-but-EMPTY manifest, only OUT-OF-WINDOW finalized/ -> no banner, exit 0"
TI_WORLD=$(mktemp -d)
TI_HOME=$(mktemp -d)
trap 'rm -rf "$TI_WORLD" "$TI_HOME"' EXIT

mkdir -p "$TI_WORLD/.workflow_artifacts/memory/sessions"
mkdir -p "$TI_WORLD/.workflow_artifacts/memory/daily"
mkdir -p "$TI_WORLD/.workflow_artifacts/memory/verification"
mkdir -p "$TI_WORLD/.workflow_artifacts/finalized/ivg-300-old"
# eod_due=no so the S-4 nudge is absent too -> output must be fully silent
printf '## Cost\nend_of_day_due: no\n' > "$TI_WORLD/.workflow_artifacts/memory/sessions/${TODAY}-ti-task.md"
printf 'daily cache\n' > "$TI_WORLD/.workflow_artifacts/memory/daily/${TODAY}.md"
# A prior daily cache the day before today -> L = today (excludes today's own cache)
YESTERDAY=$(date -v-1d +%Y-%m-%d 2>/dev/null) || YESTERDAY=""
printf '## Claims\n```yaml\n```\n' > "$TI_WORLD/.workflow_artifacts/memory/verification/end_of_day-${TODAY}.md"

if [ -n "$YESTERDAY" ]; then
  printf 'prior daily cache\n' > "$TI_WORLD/.workflow_artifacts/memory/daily/${YESTERDAY}.md"
  TEN_DAYS_AGO=$(date -v-10d +%Y%m%d%H%M 2>/dev/null) || TEN_DAYS_AGO=""
  if [ -n "$TEN_DAYS_AGO" ] && touch -t "$TEN_DAYS_AGO" "$TI_WORLD/.workflow_artifacts/finalized/ivg-300-old" 2>/dev/null; then
    TI_STDIN=$(printf '{"cwd":"%s"}' "$TI_WORLD")
    TI_OUT=$(printf '%s' "$TI_STDIN" | env HOME="$TI_HOME" sh "$HOOKS_DIR/sessionend.sh" 2>/dev/null)
    TI_EXIT=$?
    if [ "$TI_EXIT" -eq 0 ] && [ -z "$TI_OUT" ]; then
      pass "Test I — quiet-day: no banner, exit 0 (window-scoped, not all-time-archive-keyed)"
    else
      fail "Test I — exit=$TI_EXIT output=$TI_OUT (expected silent exit 0)"
    fi
  else
    skip "Test I — touch -t not available on this platform; window-scope test cannot run"
  fi
else
  skip "Test I — date -v not available on this platform (BSD-only); window-scope test cannot run"
fi
trap - EXIT
rm -rf "$TI_WORLD" "$TI_HOME"

# ── Test J (acceptance j): eod_due==1 + wrapper-absent -> fall-through, nudge still fires, no snapshot ──
echo ""
echo "Test J: eod_due==1 + verify_claims.py absent -> STEP 5b falls through (no exit); nudge still fires, no Close snapshot"
TJ_WORLD=$(mktemp -d)
TJ_HOME=$(mktemp -d)
trap 'rm -rf "$TJ_WORLD" "$TJ_HOME"' EXIT

mkdir -p "$TJ_WORLD/.workflow_artifacts/memory/sessions"
mkdir -p "$TJ_WORLD/.workflow_artifacts/memory/daily"
TJ_SESSION_FILE="$TJ_WORLD/.workflow_artifacts/memory/sessions/${TODAY}-tj-task.md"
printf '## Cost\nend_of_day_due: yes\n' > "$TJ_SESSION_FILE"
printf 'daily cache\n' > "$TJ_WORLD/.workflow_artifacts/memory/daily/${TODAY}.md"

TJ_PROJ_HASH=$(printf '%s' "$TJ_WORLD" | sed 's|/|-|g')
TJ_JSONL_DIR="$TJ_HOME/.claude/projects/$TJ_PROJ_HASH"
mkdir -p "$TJ_JSONL_DIR"
TJ_UUID="deadbeef-0000-1234-5678-abcdef0000ff"
printf '{}' > "$TJ_JSONL_DIR/${TJ_UUID}.jsonl"

THIRTY_MIN_AGO_J=$(date -v-30M +%Y%m%d%H%M 2>/dev/null) || THIRTY_MIN_AGO_J=""
if [ -n "$THIRTY_MIN_AGO_J" ] && touch -t "${THIRTY_MIN_AGO_J}" "$TJ_SESSION_FILE" 2>/dev/null; then
  TJ_HOOK=$(_make_wrapperless_hooks)
  TJ_STDIN=$(printf '{"cwd":"%s"}' "$TJ_WORLD")
  TJ_OUT=$(printf '%s' "$TJ_STDIN" | env HOME="$TJ_HOME" sh "$TJ_HOOK" 2>/dev/null)
  TJ_EXIT=$?
  rm -rf "$(dirname "$(dirname "$TJ_HOOK")")" 2>/dev/null || true

  TJ_SNAP_COUNT=$(grep -c '^## Close snapshot$' "$TJ_SESSION_FILE" 2>/dev/null); TJ_SNAP_COUNT=${TJ_SNAP_COUNT:-0}
  if [ "$TJ_EXIT" -eq 0 ] && printf '%s' "$TJ_OUT" | grep -q 'quoin-S-4' && [ "$TJ_SNAP_COUNT" -eq 0 ]; then
    pass "Test J — wrapper-absent fall-through: nudge still fires and no Close snapshot is appended"
  else
    fail "Test J — exit=$TJ_EXIT snapshot_count=$TJ_SNAP_COUNT output=$TJ_OUT"
  fi
else
  skip "Test J — touch -t not available on this platform; wrapper-absent fall-through test cannot run"
fi
trap - EXIT
rm -rf "$TJ_WORLD" "$TJ_HOME"

# ── Final summary ──────────────────────────────────────────────────────────────
echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] || exit 1
