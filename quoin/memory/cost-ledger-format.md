# Cost ledger format — full reference

## Row format

**Ledger path:** `.workflow_artifacts/<task-name>/cost-ledger.md`. Create with header `# Cost Ledger — <task-name>` if new.

**Row format — executable one-liner (7-column form):**

```bash
uuid=$(python3 __QUOIN_HOME__/scripts/get_session_uuid.py --project-path "$(pwd)" --phase "PHASE" 2>/dev/null || echo "unknown-PHASE-$(date -u +%Y%m%dT%H%M%SZ)"); uuid=${uuid:-unknown-PHASE-$(date -u +%Y%m%dT%H%M%SZ)} && printf '%s | %s | %s | %s | task | %s | %s\n' \
  "$uuid" "$(date -u +%Y-%m-%d)" "PHASE" "MODEL" "NOTE" "FALLBACK_FIRES" \
  >> "$LEDGER"
```

Substitute the bareword placeholders `PHASE`, `MODEL`, `NOTE`, `FALLBACK_FIRES` with session-specific values before running. `LEDGER` must be set to the ledger path (e.g., `.workflow_artifacts/<task-name>/cost-ledger.md`) before invocation. `NOTE` MUST be quoted — unquoted values containing spaces or pipes will produce malformed rows. Columns: `UUID | DATE | PHASE | MODEL | task | NOTE | FALLBACK_FIRES | ATTRIBUTION`.

**Empty-UUID guard:** all three self-write one-liners (7-, 8-, and 6-column forms above and below) assign `uuid` first, then apply `uuid=${uuid:-unknown-PHASE-$(date -u +%Y%m%dT%H%M%SZ)}` as its own statement — never spliced into the original `uuid=$(...) && printf` chain, since a guard clause there would short-circuit and suppress `printf` on the normal (non-empty) path. This is cheap defense-in-depth against an empty-but-successful `get_session_uuid.py` stdout read. The first column is always `UUID`: a row whose first field parses as a date (rather than a UUID) indicates a column transposition — a transcription slip, not necessarily an empty-`$uuid` write — and stays in place under the append-only invariant once observed (e.g. this task's own ledger).

**8-column form** (adds the optional `attribution` column — written by the on-behalf orchestrator path (stage 3: `/architect`, `/thorough_plan`, `/run`, flag-gated by `QUOIN_INLINE_COST_CAPTURE`, default ON since IVG-249 S-02); session-start one-liner writers keep emitting 6/7-col):

```bash
uuid=$(python3 __QUOIN_HOME__/scripts/get_session_uuid.py --project-path "$(pwd)" --phase "PHASE" 2>/dev/null || echo "unknown-PHASE-$(date -u +%Y%m%dT%H%M%SZ)"); uuid=${uuid:-unknown-PHASE-$(date -u +%Y%m%dT%H%M%SZ)} && printf '%s | %s | %s | %s | task | %s | %s | %s\n' \
  "$uuid" "$(date -u +%Y-%m-%d)" "PHASE" "MODEL" "NOTE" "FALLBACK_FIRES" "ATTRIBUTION" \
  >> "$LEDGER"
```

**On-behalf write (stage 3)** — a cost-aware orchestrator (`/architect`, `/thorough_plan`, `/run`) writes this row itself, on behalf of a managed child it spawned, instead of the child self-writing. Gated by `QUOIN_INLINE_COST_CAPTURE` (default ON since IVG-249 S-02 — opt out with `=0`; see the rollout note below). `AID` is the child's `agentId` (model-transcribed from the Agent tool result, or a unique fallback UUID on capture failure); `ATTR` is the T-01 CLI's col-8 micro-map output:

```bash
if [ "${QUOIN_INLINE_COST_CAPTURE:-1}" != "0" ]; then
  SID="$CLAUDE_CODE_SESSION_ID"
  _ERR=$(mktemp) || { printf 'cost-attr WARN: %s\n' "mktemp failed"; _ERR=/dev/null; }
  ATTR="$(python3 __QUOIN_HOME__/scripts/agent_transcript_cost.py \
            --sid "$SID" --agent-id "$AID" --tool-use-id "$TUID" 2>"$_ERR")"
  [ -z "$ATTR" ] && ATTR="src=unresolved"
  [ -s "$_ERR" ] && printf 'cost-attr WARN: %s\n' "$(head -c 500 "$_ERR" | tr '\011\012\015' '   ' | tr -d '\000-\037\177')"
  [ "$_ERR" != "/dev/null" ] && python3 __QUOIN_HOME__/scripts/fsops.py rm "$_ERR"
  printf '%s | %s | %s | %s | task | %s | %s | %s\n' \
    "$AID" "$(date -u +%Y-%m-%d)" "PHASE" "MODEL" "on-behalf: PHASE via /ORCH" "0" "$ATTR" \
    >> "$LEDGER"
  # Post-check (identifier-keyed, same invocation): verify THIS write's own AID
  # landed; if the append above silently failed, append a labeled fallback row
  # now. Every orchestrator call site embeds this — no managed spawn is left
  # with a silent zero-row path on write failure.
  { [ -n "$AID" ] && grep -qF -e "$AID | " -- "$LEDGER" 2>/dev/null; } || \
    printf '%s | %s | %s | %s | task | %s | %s\n' \
      "unknown-PHASE-$(date -u +%s)" "$(date -u +%Y-%m-%d)" "PHASE" "MODEL" \
      "/ORCH subagent (on-behalf write failed)" "0" >> "$LEDGER"
fi
```

**Rollout note (stage 3 → stage 4):** col-8-aware readers HAVE shipped (`cost_event.parse_row` tolerates 6/7/8 and reads col 8; `analyze_cost_ledger`, `dashboard_cost`, core `spend_monitor`, and `end_of_task` Sub-phase B all apply col-8 precedence). As of IVG-249 S-02 the flag is default ON — unset means capture is enabled. Opt out with `QUOIN_INLINE_COST_CAPTURE=0`; any other value (incl. unset) enables capture. Under opt-out, managed-phase rows revert to child self-writes sharing the parent session UUID, cohort-bucketed by `cohort_attribution` — per-phase attribution is unavailable. `/run` warns at Setup when the opt-out is explicit.

**6-column form** (for Conditional skills `/discover`, `/triage` that omit `fallback_fires`):

```bash
uuid=$(python3 __QUOIN_HOME__/scripts/get_session_uuid.py --project-path "$(pwd)" --phase "PHASE" 2>/dev/null || echo "unknown-PHASE-$(date -u +%Y%m%dT%H%M%SZ)"); uuid=${uuid:-unknown-PHASE-$(date -u +%Y%m%dT%H%M%SZ)} && printf '%s | %s | %s | %s | task | %s\n' \
  "$uuid" "$(date -u +%Y-%m-%d)" "PHASE" "MODEL" "NOTE" \
  >> "$LEDGER"
```

The 7th column (`fallback_fires`) is OPTIONAL. Existing 6-column rows are valid forever; readers MUST tolerate both shapes. When present, the value is a non-negative integer (`0` if no fires occurred during the session). When absent, parsers treat it as `0`. Writers SHOULD emit the 7th column on new rows; readers MUST NOT fail on a missing 7th column. Append-only ledger semantics are unchanged. The append-only invariant and the 6/7/8-column reader-tolerance rule (below) both hold unconditionally — no writer may delete or rewrite an existing row, and readers must not fail on any of the three shapes.

The 8th column (`attribution`) is likewise OPTIONAL and default-safe: existing 6- and 7-column rows are valid forever; readers MUST tolerate 6-, 7-, and 8-column shapes. Absent means "no inline attribution was captured" — it is NEVER interpreted as `$0`-with-confidence. See `## Attribution column (col 8)` below for the value grammar.

**Writer guidance:** Skills emitting a new ledger row SHOULD include the 7th column when they have a session-state `fallback_fires` value available (typically at session-end emits, not session-open). Skills MAY emit a 6-column row when no session-state exists (e.g., `/discover`, `/triage`) or when `fallback_fires` is 0; readers tolerate both shapes per the row-format spec. Column 8 (`attribution`) is written by the on-behalf orchestrator path (stage 3, flag-gated `QUOIN_INLINE_COST_CAPTURE`, default ON, opt-out `=0` — see the rollout note above); other writers omit it and readers treat the omission as "no attribution", not as a zero-cost claim.

## Attribution column (col 8)

`ATTRIBUTION := k=v(;k=v)*` — a semicolon-delimited micro-map, e.g. `usd=0.0123;tok=45210;src=<tag>`. No pipe characters are permitted inside the value (the bare-`|` row split must stay unaffected by col 8 content).

Keys:
- `usd` — optional float, a cost snapshot (≤6 decimal places).
- `tok` — optional int, durable — store whenever known so the row can be re-priced later if the price table changes.
- `src` — required provenance tag on any row that carries an `attribution` value at all; concrete values: `nested_jsonl` (resolved from the runtime's nested session JSONL), `backfill_session` (resolved after the fact from a backfill pass), `unresolved` (cost could not be attributed — labeled-unresolvable, never a silent `$0`).

Column 8 is OPTIONAL and default-safe. Absent means "no inline attribution" — never `$0`-with-confidence. A row whose `src` tag marks the cost as unresolvable is a labeled-unresolvable case (no `usd` value), not a silent zero.

### cost-summary.json keys (written by `cost_ledger_summary.py` at finalize)

`per_phase`, `per_model` (resolved USD only), `estimated_per_phase`, `task_total`, `off_topic_total` (always 0.0: non-task rows are dropped by the row parser), `resolved_total`, `estimated_total`, `estimated_count`, `repriced_count`, `priced_rows` (rows that contributed a resolved cost: inline `usd`, re-priced, or priced from their session), `rows_total`, `unresolvable_count`, `shared_sessions_total`, `price_table_updated`, `grand_total` (`resolved_total + estimated_total`), `phases_covered`, `phases_excluded` (`["end-of-task"]` when the ledger has no `end-of-task` row), `phases_note`, and `fallback_used`/`fallback_note` only when `estimated_count > 0` or `unresolvable_count > 0`. A `src=unresolved` row keeps its `tok=` value in the ledger and is re-priced at aggregation time from the transcript, or estimated from `tok` and the model alias; the ledger row itself is never rewritten.

Append-only and reader-tolerance semantics are unchanged by this column: 6-, 7-, and 8-column rows are all valid forever, and writers never delete or rewrite existing rows.

**UUID acquisition:** Use `python3 __QUOIN_HOME__/scripts/get_session_uuid.py --project-path "$(pwd)" --phase "PHASE"` to obtain the session UUID. The script finds the most-recently-modified `<uuid>.jsonl` under `~/.claude/projects/<project-hash>/` (project-hash = project path with non-alphanumeric chars replaced by `-`) and returns its stem. Falls back to `unknown-<phase_slug>-<YYYYMMDD>T<HHMMSS>Z` if no JSONL is found or on any error (fail-open). The `unknown-*` prefix is recognized by the cost_snapshot skip filter — synthetic UUIDs are excluded from cost totals. Phase dashes are slugified to underscores in the fallback (e.g., `end-of-task` → `end_of_task`). The outer shell fallback in the one-liner (`|| echo "unknown-PHASE-$(date -u ...)"`) ensures a UUID is always set even if Python is unavailable. The script exits 0 always (fail-open design).

## Phase values

`discover`, `architect`, `plan`, `critic`, `revise`, `implement`, `review`, `gate`, `end-of-task`, `run-orchestrator`, `thorough-plan`, `rollback`, `init-workflow`, `start-of-day`, `end-of-day`, `weekly-review`, `capture-insight`, `triage`, `expand`, `checkpoint`, `sleep`, `session-close-hook`, `next-steps`, `ad-hoc`

## Category

Always write `task`. The ledger is append-only — never delete or rewrite rows.

## Portable shape

See also `quoin/core/workflow/cost-ledger.md` for the runtime-neutral specification.

## Checkpoint cost attribution (IVG-157)

Inline `/checkpoint` runs in the parent session and writes the PARENT session
UUID; its cost is attributed via session-cohort attribution (labeled
`shared-session (multi-phase)` bucket), NOT the parent-session total.
Checkpoint behavior is unchanged. Concretely: `analyze_cost_ledger.py`,
`dashboard_cost.py`, and `cost_snapshot`'s per-task totals resolve each
distinct session UUID's cost exactly once — a UUID shared by two or more
phase rows (a "cohort") is never charged in full to each participating
phase, and never silently shown as $0. See `cohort_attribution` in
`quoin/core/scripts/cost_event.py` for the implementation. Note: tokens-mode
shared cohorts (unpriceable models) remain over-counted in tokens — the USD
per-phase overstatement is what is fixed here; this is a documented,
accepted residual gap for a rare fallback path.
