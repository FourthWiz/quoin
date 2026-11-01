#!/bin/sh
# build_hook_fixtures.sh — Generate fixture transcript files for hook tests
#
# Creates the JSONL fixture transcript used as a high-fill input (97% of the
# effective context window) by the prompt-hook and precompact tests.
#
# Reads constants from env (same defaults as hooks/_lib.sh):
#   QUOIN_BYTES_PER_TOKEN  (default 8.0)
#   QUOIN_EFFECTIVE_CONTEXT_LIMIT  (default 150000)
#
# Output: quoin/dev/tests/fixtures/hooks/transcript_97pct.jsonl
#
# Idempotent: same constants → byte-identical output.
# No timestamps or run-dependent content — deterministic filler text only.
#
# Usage: sh quoin/dev/tests/build_hook_fixtures.sh

BPT=${QUOIN_BYTES_PER_TOKEN:-8.0}
LIMIT=${QUOIN_EFFECTIVE_CONTEXT_LIMIT:-150000}

FIXTURES_DIR="$(cd "$(dirname "$0")/fixtures/hooks" 2>/dev/null && pwd)"
if [ -z "$FIXTURES_DIR" ]; then
  # Fallback: compute relative to script location
  FIXTURES_DIR="$(dirname "$0")/fixtures/hooks"
  mkdir -p "$FIXTURES_DIR"
fi

# Compute target byte size for a given percent (integer, e.g., 70 for 70%)
# target_bytes = (pct / 100) * BPT * LIMIT
# We use awk for arithmetic (BPT may be a decimal like "8.0")
compute_target_bytes() {
  local pct="$1"
  awk -v p="$pct" -v bpt="$BPT" -v lim="$LIMIT" \
    'BEGIN{ printf "%d\n", (p / 100) * bpt * lim }'
}

# Generate a JSONL fixture at a given byte target.
# The file contains a single synthetic assistant message whose content
# is padded to the exact target byte size.
# Format: one JSON line per the harness JSONL format (type=assistant, usage block, content).
# Content is deterministic ASCII filler (no timestamps, no random data).
generate_fixture() {
  local target_pct="$1"
  local outfile="$2"

  local target_bytes
  target_bytes=$(compute_target_bytes "$target_pct")

  # The JSONL wrapper overhead (the static parts of the JSON line) is approximately:
  # {"type":"assistant","message":{"content":[{"type":"text","text":""}],"usage":{"input_tokens":0,"output_tokens":0}}}
  # We measure this and pad the text field to reach target_bytes.
  local prefix='{"type":"assistant","message":{"content":[{"type":"text","text":"'
  local suffix='"}],"usage":{"input_tokens":0,"output_tokens":0}}}'
  local overhead=$((${#prefix} + ${#suffix} + 1))  # +1 for trailing newline

  local filler_bytes=$((target_bytes - overhead))
  if [ "$filler_bytes" -lt 0 ]; then
    filler_bytes=0
  fi

  # Generate deterministic filler: repeat the alphabet block
  # "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 " (63 chars)
  local block="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 "
  local block_len=63

  # Use awk to generate exactly filler_bytes of the repeating block
  local filler
  filler=$(awk -v n="$filler_bytes" -v blk="$block" -v bl="$block_len" \
    'BEGIN {
      total = 0
      while (total < n) {
        remaining = n - total
        chunk = (remaining < bl) ? substr(blk, 1, remaining) : blk
        printf "%s", chunk
        total += length(chunk)
      }
    }')

  # Write the fixture file
  printf '%s%s%s\n' "$prefix" "$filler" "$suffix" > "$outfile"

  # Verify size is within 1% of target
  local actual_bytes
  actual_bytes=$(wc -c < "$outfile" | awk '{print $1}')
  local tolerance
  tolerance=$(awk -v t="$target_bytes" 'BEGIN{ printf "%d\n", t * 0.01 + 1 }')

  local diff=$((actual_bytes - target_bytes))
  if [ "$diff" -lt 0 ]; then
    diff=$((-diff))
  fi

  if [ "$diff" -le "$tolerance" ]; then
    printf '  %s: %d bytes (target %d, within 1%%)\n' "$(basename "$outfile")" "$actual_bytes" "$target_bytes"
  else
    printf '  WARNING: %s: %d bytes (target %d, diff %d > tolerance %d)\n' \
      "$(basename "$outfile")" "$actual_bytes" "$target_bytes" "$diff" "$tolerance" >&2
  fi
}

printf 'Building hook fixture transcripts (BPT=%s, LIMIT=%s)...\n' "$BPT" "$LIMIT"

generate_fixture 97 "$FIXTURES_DIR/transcript_97pct.jsonl"

printf 'Done. Fixtures written to %s\n' "$FIXTURES_DIR"
