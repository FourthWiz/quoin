# Dispatch guide — §0 and §0' verbose reference

## §0 Model dispatch preamble (verbose reference)

The 20 cheap-tier skills (gate, end_of_day, start_of_day, triage, capture_insight, cleanup, cost_snapshot, weekly_review, end_of_task, implement, rollback, expand, revise-fast, sleep, next_steps, checkpoint, continue_work, pr, status, workspace) carry a `## §0 Model dispatch (FIRST STEP — execute before anything else)` block as the first body H2 after the H1. When invoked from a session running on a model strictly more expensive than the declared tier, the skill self-dispatches via the Agent tool to its declared model and prefixes the child prompt with the bare `[no-redispatch]` sentinel to prevent infinite recursion. The counter form `[no-redispatch:N]` is reserved for an abort signal: if a child sees N≥2, it aborts instead of proceeding (the bare form is the normal parent-emit; counter forms catch buggy parents or mistaken manual overrides). The 12 Opus-tier skills do NOT carry the preamble — they should run on Opus regardless of session model.

If the harness's subagent-spawn tool is unavailable or returns an error, dispatch falls back to a fail-OPEN path (proceed at current tier, emit a one-line `[quoin-stage-1: subagent dispatch unavailable; ...]` warning). This is intentional per architecture I-01: cost guardrail is best-effort, not load-bearing for correctness.

**WorktreeCreate hook and two-phase dispatch (source-mutating skills only, 2026-05-21; extended IVG-116):**
The four source-mutating cheap-tier skills (`/implement`, `/rollback`, `/end_of_task`, `/pr`) use a two-phase worktree isolation mechanism. Before calling the Agent tool, the skill runs `dispatch_sidecar.py` to write a sidecar JSON at `<project_root>/.workflow_artifacts/.dispatch-hint.json` containing the skill name, project root, plan path, and session ID. Phase 1 dispatches with `isolation: "worktree"`; the deployed WorktreeCreate hook at `__QUOIN_HOME__/hooks/worktreecreate.sh` reads the sidecar, calls `git_root_for_dispatch.py --sidecar`, and (when a single nested git repo resolves) runs `git worktree add` anchored at the nested repo and prints the created worktree path to stdout. The hook always exits 0 (fail-OPEN); if it skips (no stdout → harness worktree fails) or Phase 1 returns a worktree-class error, the skill retries as Phase 2 without `isolation: "worktree"` at the same cheap-tier model. No AskUserQuestion prompt is needed for these four skills; Phase 2 retry is fully automated. The `§0-sidecar` block carrying this two-phase logic is HAND-AUTHORED (marker-delimited `<!-- §0-sidecar-begin/end -->`) and kept BYTE-IDENTICAL across all four adapter SKILL.md; the drift test `test_quoin_stage1_worktree_fallback.py` enforces byte-equality via `SOURCE_MUTATING_WORKTREE_SKILLS` (now includes `pr`).

**Worktree isolation decider (STEP A0) + self-generation (IVG-116):**
The four source-mutating skills consult a decider BEFORE writing the sidecar. STEP A0 (byte-identical across the four sidecar blocks) runs `python3 __QUOIN_HOME__/scripts/worktree_isolation.py --decide`. On `skip` (the default), the skill dispatches plainly at its declared tier with NO sidecar write and NO `isolation: "worktree"` — eliminating the failed worktree round-trip that Google-Drive-synced projects paid on every dispatch (the IVG-116 root cause). Only on `attempt` do STEP A/B/C (sidecar write → isolation dispatch → Phase-2 retry) run. The decider mirrors `dispatch_config.py`'s precedence: env `QUOIN_WORKTREE_ISOLATION` (`on`→attempt, `off`→skip) > `$HOME/.config/quoin/dispatch.json` key `worktree_isolation` > project sentinel `.workflow_artifacts/memory/worktree-probe.txt` (`works`→attempt, `broken`→skip) > **DEFAULT skip** (isolation is opt-in, D-04). Blanket fail-OPEN → `skip` (never pay the failed attempt on any error). The probe sentinel `worktree-probe.txt` is declared OUT OF `/sleep --purge --sentinels` scope (MIN-3); if purged anyway, the decider reverts to safe default-skip and the next opt-in run re-probes. When the T-01 spike / opt-in path confirms the harness placed the child inside the created worktree, the parent calls `worktree_isolation.py --write-probe --result works` once to cache the capability.

Worktree knobs: `QUOIN_WORKTREE_ISOLATION` (`on`|`off`|unset — decider config layer); `QUOIN_WORKTREE_SELFGEN` (`0` restores the old hook skip-when-harness-omits-path behaviour; default on); the hook's `git worktree add` is bounded by `QUOIN_SUBPROCESS_TIMEOUT` (fail-OPEN if the `timeout` binary is absent). Self-generated worktrees are anchored OUTSIDE the Drive tree under `${TMPDIR:-/tmp}/quoin-worktrees` (project `.worktrees/` fallback); no GC — run `git worktree prune` manually if they accumulate.

**Subprocess-timeout + child-repo-scan knobs (IVG-116, Workstream B):**
`QUOIN_SUBPROCESS_TIMEOUT` (integer seconds, default 30 — ~2× the observed 14.3s Drive baseline) bounds every SHORT git subprocess across the core scripts (`branch_hygiene`, `affected_tests`, `build_preambles`, and the inline-literal sites in `status_graph`/`verify_claims`/`generate_discovery_map`). The long-running pytest subprocess in `affected_tests` gets a DERIVED generous bound `max(600, QUOIN_SUBPROCESS_TIMEOUT)` (no second env var); a pytest `TimeoutExpired` maps to **exit code 3** with `exit_reason="pytest-timeout"` — mirroring the existing `pytest-missing`→exit-3 arm. Per the gate contract, exit 3 = BLOCKING-SURFACE: a hung suite is NEVER a silent GREEN and NEVER a hard-RED false block (exit 1) — the human decides. `QUOIN_DISABLE_CHILD_REPO_SCAN=1` skips the depth-1 child `.git` scan in `branch_hygiene.discover_repos` / `affected_tests.discover_repos` (single-repo view). It is SEPARATE from `QUOIN_DISABLE_DISPATCH_CWD` (which scopes only the dispatch-detection site): the two are independent so a user fixing Drive dispatch latency never silently narrows `/gate` multi-repo coverage (MAJ-2 / D-08). Both unset = byte-identical prior behaviour.

**Worktree-class errors in artifact-only skills (unchanged, 2026-05-21):**
The 12 artifact-only cheap-tier skills still use the original worktree-class recovery path. Worktree-class errors (substring match: `Cannot create agent worktree` OR `worktree` + `not in a git repository`) are classified BEFORE the fail-OPEN warning is emitted. The skill uses the AskUserQuestion tool to present one recovery option: `(c) proceed-current-tier`. A second classification line is emitted: `[quoin-stage-1: error-class=worktree; user-choice=c; proceeding at current tier]`. The bare warning `[quoin-stage-1: subagent dispatch unavailable; proceeding at current tier]` is always emitted verbatim first.

Manual override: prefix any user-typed slash invocation with bare `[no-redispatch]` to skip dispatch entirely. Use this only when intentionally overriding the cost guardrail (e.g., for one-off debugging on a different tier).

**Why the bare `[no-redispatch]` sentinel is dual-source by design (IVG-165 Step B pointer target):** the same bare token skips dispatch whether it was emitted by the parent (an already-dispatched child re-invoking a skill, preventing infinite recursion) or typed by a human user (the manual override above). A child receiving the sentinel cannot tell — and does not need to tell — which source it came from, because both paths want the identical outcome: skip dispatch, proceed at the current tier. This is intentional, not an ambiguity bug; the counter form `[no-redispatch:N]` (N>=2) exists precisely to catch the one case that DOES need disambiguating (a buggy or looping parent), by making repeated automated re-dispatch visible as an incrementing count while the single-shot manual override never needs to count past one.

**1M-context credit mismatch recovery (IVG-89, all 19 §0 skills):** Pre-dispatch model-name detection is impossible — the model name never contains "1m" and 1M status is undetectable from inside the agent (IVG-89 F-02). Recovery is folded into the EXISTING `§0-worktree-fallback` error-classification leaf as a new 1M-credit-class branch. Classification order: 1M-credit-class is checked FIRST (before Worktree-class), keyed on substring `Usage credits required for 1M context` in the dispatch error text. On match: emit a specific advisory (`[quoin: 1M-context credit mismatch on <tier> subagent dispatch; proceeding in-session at parent tier — run /model to switch this session to standard context for a permanent fix]`) and proceed in-session at parent tier (no AskUserQuestion; fail-OPEN). The dead `§0-1m-context-precheck` marker blocks have been removed from all 19 §0 skills (IVG-89 D-03 Option A).

**Proactive 1M-context credit detection (IVG-90, all 19 §0 skills):** Before every §0 Agent dispatch, the skill now runs a single `dispatch_config.py --decide --tier <declared_tier>` call that folds three information sources into a binary verdict — `dispatch` (proceed as today) or `safe-path` (skip the Agent call entirely and run in-session at the parent tier, making no API call). The three layers are: **Layer 1 — explicit opt-in config** (zero cost, deterministic): `QUOIN_1M_DISPATCH` env var or `$HOME/.config/quoin/dispatch.json` `one_m_dispatch` field (env > file > unset). **Layer 2 — per-tier session cache** (at most one cache lookup, at most one failing call per tier per session): `.workflow_artifacts/memory/1m-tier-<tier>.txt`, one token `safe` | `unsafe`, keyed by TIER alone (no session_id because a skill body cannot reliably obtain a session identifier), whole-file overwrite using atomic-rename (no read-modify-write), written by the skill's §0 procedure via `dispatch_config.py --write-cache --tier T --result safe|unsafe`. Aged out by `/cleanup`'s existing sentinel sweep (`QUOIN_CLEANUP_SENTINEL_WINDOW`). Project root resolved via `path_resolve.py --project-root`. **Layer 3 — live probe**: the real Agent dispatch itself; when a 1M-credit-class error fires, the IVG-89 catch retains its verbatim advisory and feeds the cache with `--result unsafe` before falling through to the in-session path. The unconfigured user's first dispatch per tier still makes one real failing call (unavoidable per the IVG-90 investigation) and is caught by the IVG-89 path.

Knob table (from architecture `D-01`): `QUOIN_1M_DISPATCH` — `unset` (default, today's behavior, equivalent to free dispatch); `off` (never dispatch to any tier — safe-path for all cheaper-tier dispatches, opt-out for users without 1M credits); `on` (dispatch freely, skip all cache lookups); `<tier-csv>` (per-tier allow-list, e.g. `sonnet,haiku` — listed tiers dispatch freely, unlisted tiers fall back to safe-path). `QUOIN_1M_FALLBACK_MODEL` — `unset` (default) or a model-id string. **Parsed and stored by `dispatch_config.py` but INERT in v1 (IVG-90 `D-04`)**: a named non-1M model passed to the Agent tool would still carry the parent session's `context-1m-2025-08-07` beta header and produce an identical 400 response; this knob is reserved for a future iteration once a header-stripping mechanism is available.

Precedence (same for both knobs): env var > `$HOME/.config/quoin/dispatch.json` field > unset-default. `dispatch.json` is a SEPARATE file from `models.json` (the router config is untouched). To persist the safe-path opt-out across sessions, write:

```json
// $HOME/.config/quoin/dispatch.json
{ "one_m_dispatch": "off", "one_m_fallback_model": null }
```

Env vars override the file at runtime; default (file absent or `one_m_dispatch` unset) = today's behavior with no change to the dispatch path.

Two best-effort `--write-cache` lines are injected into every §0 body: one on the success path (`--result safe`, executes just before the `return`/`STOP` after a successful Agent subagent dispatch), and one inside the IVG-89 1M-credit-class catch leaf (`--result unsafe`, retained verbatim from IVG-89). Both are wrapped in the `<!-- §0-1m-cachewrite-begin -->` / `<!-- §0-1m-cachewrite-end -->` markers (the block appears twice per file — once on the success path and once in the catch leaf).

Fail-OPEN: any `--decide` error → returns `dispatch` (today's path, no suppression); any cache-read error → treated as `unknown` (the live probe runs); any cache-write error → silent skip. An unreadable config or cache MUST NEVER suppress a dispatch.

Observability: when `--decide` returns `safe-path`, the skill emits `[quoin: 1M-unsafe declared-tier per <reason>; running SAFE PATH without dispatch]` (the `--verbose` flag surfaces the reason token `config` or `cache`).

The §0 body of all 19 cheap-tier skills has two byte-identical marked regions: `<!-- §0-1m-decide-begin -->` / `<!-- §0-1m-decide-end -->` (the pre-dispatch DECISION block — the `--decide` call and its `safe-path` branch) and `<!-- §0-1m-cachewrite-begin -->` / `<!-- §0-1m-cachewrite-end -->` (the cache-write lines). The blocks are byte-identical across all 19 files using literal placeholders `§1/§0c` and `tier T` (no per-skill token). Byte-identity is verified by `quoin/dev/tests/test_1m_proactive_precheck.py` (`test_all_19_s0_files_decide_block_byte_identical` + `test_all_19_s0_files_cachewrite_block_byte_identical`, iterating all 19 via `SECTION0_TARGETS`/`MIGRATED_TO_ADAPTER`).

**C-01 caveat — stub coverage (refreshed 2026-07-21, IVG-117 Q-01):** `validate_adapter_drift.py` iterates `manifest["skills"]` — `skills.json` (schema_version 2) now has 32 entries and ALREADY INCLUDES the 5 legacy stub skills (`checkpoint`, `cleanup`, `continue_work`, `next_steps`, `sleep`) with populated `claude_model`/`section_0` fields. `validate_adapter_drift.py` exits 0 (green) on the committed tree WITH those 5 in the manifest, proving `AD-CO`/`AD-AD`/`AD-LS`/`AD-FB`/`AD-SS` all already hold for them (all three files exist per stub skill; stub frontmatter byte-equals the adapter; the stub body stays shorter than the adapter). Deriving `claude_model=="sonnet" && section_0==true` from `skills.json` yields exactly the 13 §0‴ targets; `haiku && section_0` yields the 7 structurally-exempt skills; `opus && !section_0 && not orchestrator` yields the 10 Opus §0″ leaf skills — these rosters are machine-derivable and are asserted by the set-equality guards in `test_sonnet_mintier_guard.py`. `skills.json` remains a READ-ONLY cross-check source; no code path writes back to it. New scripts introduced by IVG-90: `dispatch_config.py` (canonical implementation at `quoin/core/scripts/dispatch_config.py` + compatibility wrapper at `quoin/scripts/dispatch_config.py`), added to both the `DEPLOYED_SCRIPTS` and `CORE_SCRIPTS` installer lists in `quoin/src/quoin/installer.py`.

Mechanical drift detection lives in `quoin/dev/tests/test_quoin_stage1_preamble.py` and `quoin/dev/tests/test_quoin_stage1_recursion_abort.py`; manual production-dispatch verification is captured in `quoin/dev/verify_subagent_dispatch.md`.

## §0' Pollution dispatch (verbose reference)

The 10 Opus-tier skills that are NOT orchestrators (architect, plan, critic, revise, review, init_workflow, discover, specify, security_review, enrich) carry a `## §0' Pollution dispatch (execute after §0 / §0c if present — before skill body)` block. When `pollution_score` exceeds `POLLUTION_THRESHOLD`, the skill self-dispatches as a fresh Agent subagent carrying per-skill paths (not content).

**Detection:** reads `pollution_score: N` from session-state file or `pollution-score-latest.txt`. Fires if N >= threshold AND no `[no-redispatch]` AND no prior §0 dispatch. Score formula: `transcript_kb + (agent_returns × 5) + (read_calls × 1) + (bash_calls × 1)` — implemented in `quoin/hooks/_lib.sh`. Written by `userpromptsubmit.sh` STEP 0.5 on every prompt submit.

**Per-skill dispatch contract:**

| Skill | What the dispatch prompt carries |
|-------|----------------------------------|
| /architect | task description + paths to /discover output |
| /plan | task description + path to architecture.md + stage identifier |
| /critic | absolute path to target artifact |
| /revise | path to current-plan.md + path to critic-response-N.md |
| /review | path to current-plan.md + branch ref |
| /init_workflow | project root absolute path |
| /discover | project root absolute path |
| /specify | task description + spec output path (`.workflow_artifacts/<task>/spec.md`) |
| /security_review | current git branch + plan path (if resolvable) |
| /enrich | raw task description + enriched-prompt output path (`.workflow_artifacts/<task>/enriched-prompt.md`) |

**Ordering:** §0 fires FIRST; §0' fires only if no §0 dispatch. For §0c skills (architect, review): §0c → §0' → body. **Excluded:** /run and /thorough_plan. **Threshold:** `QUOIN_POLLUTION_THRESHOLD` (default 5000). Fail-OPEN on Agent unavailable. `[no-redispatch]` skips. Drift detection: `test_quoin_pollution_preamble.py`; verification: `quoin/dev/verify_pollution_dispatch.md`.

**1M-context credit mismatch recovery (IVG-89, all 7 §0' skills):** Same impossibility as §0 — model-name detection does not work. Recovery is folded into the `Fail-OPEN path` section of the §0' block. On a dispatch error matching `Usage credits required for 1M context`: issue AskUserQuestion (abort/proceed) with 1M-specific wording. For any other non-1M dispatch error: also issue AskUserQuestion (generic wording) so §0' never silently loses recovery (D-06). The dead `§0prime-1m-context-precheck` marker blocks have been removed from the generator template (`inject_pollution_dispatch.py`) and regenerated into all 7 §0' skills (IVG-89 D-03 Option A).

**Autonomous-class branch (all three §0'/§0″/§0‴ fail-OPEN paths — IVG-162 T-03 audit backfill):** every fail-OPEN classification checks Autonomous-class FIRST, before 1M-credit-class or the generic-error branch. If the incoming prompt carries the `[autonomous]` sentinel, then on ANY dispatch-failure (including a 1M-context-credit error) the skill proceeds at current tier fail-OPEN and does NOT call `AskUserQuestion` — it skips the 1M-credit-class and generic branches entirely. Each block prints a block-specific one-line advisory and then proceeds to skill body (treated as bare `[no-redispatch]`):
- §0': `[quoin-autonomous: §0' dispatch failed; proceeding fail-OPEN at current tier]`
- §0″: `[quoin-mintier-autonomous: §0″ dispatch failed; proceeding fail-OPEN at current tier]`
- §0‴: `[quoin-mintier-autonomous: §0‴ dispatch failed; proceeding fail-OPEN at current tier]`

Rationale: an `--autonomous` run (see `autonomous-mode.md`) has no human available to answer an `AskUserQuestion` prompt, so the guard must degrade to fail-OPEN silently rather than block indefinitely on an unanswerable question.

## §0″ Minimum-tier guard (verbose reference)

The 10 Opus-tier leaf skills (architect, plan, critic, revise, review, init_workflow, discover, specify, security_review, enrich) carry a `## §0″ Minimum-tier guard (execute after §0 / §0c / §0' if present — before skill body)` block. Fires when the executing session is running on a model cheaper than Opus (inverse of §0's over-tier trigger: `current_tier < declared_tier`). Mirrors §0 down-dispatch: zero user-visible prompts on the happy path. Orchestrators /run and /thorough_plan are excluded (D-04). Generated by `inject_pollution_dispatch.py`. Drift test: `test_mintier_guard.py`. Activated as Option A per IVG-91 (2026-06-23).

**Detection:** Read model name from system context. Tier order: haiku < sonnet < opus. Declared tier = opus. Fire conditions: `current_tier < declared_tier` AND no `[no-redispatch]` sentinel AND `QUOIN_DISABLE_MINTIER_GUARD` not set.

**Happy path (silent up-dispatch):** Spawn an Agent subagent `model: "opus"`, child prompt prefixed `[no-redispatch]\n<original user input>`. Return child output. STOP. No user-visible prompts on success.

**Fail-open path (fires only when Agent dispatch fails):** Classify the error text first:
- **Autonomous-class** (checked FIRST — see the shared "Autonomous-class branch" note above): `[autonomous]` sentinel present → fail-OPEN silently, no AskUserQuestion, advisory `[quoin-mintier-autonomous: §0″ dispatch failed; proceeding fail-OPEN at current tier]`.
- **1M-credit-class** (error contains `Usage credits required for 1M context`): Issue AskUserQuestion with 1M-specific wording — Option 1 "Abort — I'll switch with /model first", Option 2 "Proceed in-session at parent tier". Mirrors §0' post-dispatch 1M-credit handling (IVG-89/IVG-91). First failing dispatch is unavoidable (same trade-off as §0').
- **Generic error**: Issue AskUserQuestion — Option 1 "Abort — run from an Opus session" → print `[quoin-mintier: aborted; re-invoke /{skill} from an Opus session]` and STOP. Option 2 "Proceed at current tier (under-powered)" → print `[quoin-mintier: min-tier up-dispatch unavailable; proceeding at current tier per user choice]` and fall through to skill body.

**Env knob:** `QUOIN_DISABLE_MINTIER_GUARD=1` → silent skip (no advisory). Intentional silence: explicit opt-out is user-controlled, not an unexpected error state.

**Recursion guard:** Child has `current_tier == declared_tier` after up-dispatch; `[no-redispatch]` prefix is belt-and-suspenders. No counter form needed.

## §0‴ Minimum-tier guard — Sonnet tier (verbose reference)

Added IVG-117 (2026-07-21) to close Gap 1: §0″ only protected the 10 Opus-tier leaf skills, leaving 19 cheap-tier skills to run silently under-tier when invoked from a more expensive session. Of those 19, 10 are Sonnet-declared and carry the mirrored guard; the other 9 are Haiku-declared and structurally exempt (bottom tier — nothing cheaper to guard against). IVG-263 later moved `/sleep` and `/cleanup` to Sonnet: 13 Sonnet-declared carriers, 7 Haiku-declared.

The 13 Sonnet-tier targets (checkpoint, cleanup, continue_work, end_of_day, end_of_task, expand, gate, implement, pr, revise-fast, rollback, sleep, workspace) carry a `## §0‴ Minimum-tier guard (execute after §0 — before any §0-sidecar block and the skill body)` block, landing as the FIRST H2 immediately after §0 (before §0a/§0b/§0c sidecar blocks where present — MIN-1 neutral wording avoids misdescribing the 7 sidecar-less targets). Fires when `current_tier < declared_tier` (declared_tier = sonnet), mirroring §0″'s fire condition exactly.

**Anchor (D-06):** §0‴ anchors on `SECTION0_HEADING` (the hand-authored §0 heading, always present and count==1 in all 13 targets) — NOT on §0'/§0″, which only exist on the disjoint Opus-10 file set. FAIL LOUD if §0 is absent or appears more than once.

**Byte-safety (D-07):** separate `MINTIER_SONNET_*` constants and a separate `_MINTIER_SONNET_BLOCK_BODY` template — zero edits to the existing Opus `_MINTIER_BLOCK_BODY`/`MINTIER_HEADING`. The 10 deployed Opus files and `test_mintier_guard.py` are untouched by this change.

**Shared knob:** `QUOIN_DISABLE_MINTIER_GUARD` (same env var as §0″ — one concept, char-budget friendly; no tier-specific knob).

**Happy path / fail-OPEN triage:** identical shape to §0″ — silent Agent up-dispatch (`model: "sonnet"`) on the happy path; Autonomous-class (checked FIRST — see the shared "Autonomous-class branch" note above; advisory `[quoin-mintier-autonomous: §0‴ dispatch failed; proceeding fail-OPEN at current tier]`) / 1M-credit-class / generic-error classification on dispatch failure, with the same AskUserQuestion fallback wording (tier words swapped: "Abort — run from a Sonnet session", etc.).

**Recursion delegation (MIN-2):** §0‴ carries ONE net-new line vs the Opus §0″ body, inside the Detection section, documenting that the counter form `[no-redispatch:N]` (N≥2) never reaches this block — §0 (earlier in the same file) already aborts on N≥2 before any §0‴ tool call. This is the sole permitted semantic delta between the two templates; the template-parity guard in the drift test strips this one line before asserting equality.

Generated by `inject_pollution_dispatch.py` (Loop 3, independent of the §0'/§0″ loops). Drift test: `quoin/dev/tests/test_sonnet_mintier_guard.py` (mirrors `test_mintier_guard.py`'s structural checks, plus set-equality guards against `skills.json` and the template-parity guard). Orchestrators `/run`/`/thorough_plan` excluded (same D-04 rationale).

## Verbatim AskUserQuestion wording (IVG-162 T-03 audit backfill — Stage 5/Stage 2 pointer target)

The Stage 2 (§0'/§0″/§0‴) and Stage 5 (§0) slims pointer-ize the FULL `AskUserQuestion` Question/Header/Option-description text out of the generated blocks, retaining inline only the option LABELS and print-message strings the drift tests byte-pin (per `proc:AUDIT-TESTS`). This section is the backfilled target those pointers resolve to — audited BEFORE any inline prose was deleted (D-03/R-08 hard prerequisite).

**§0' 1M-credit-class** (fires when the §0' Opus dispatch itself errors with the 1M-context substring):
- Question: "§0' opus dispatch failed with a 1M-context credit mismatch for /{skill}. The parent session carries the 1M-context beta header which propagates to all subagent calls; Opus lacks 1M credits. How would you like to proceed?"
- Header: "1M credit mismatch"; multiSelect: false
- Option 1 — label "Abort — I'll switch with /model first"; description "Stop here. Run /model in your terminal to switch to a standard-context model (e.g., /model opus), then re-invoke /{skill}. The §0' dispatch will then land on standard Opus successfully."
- Option 2 — label "Proceed in-session at parent tier"; description "Skip the §0' dispatch this once. /{skill} runs in the current session (may be polluted, but works). Emits a one-line advisory."

**§0' generic (non-1M) error:**
- Question: "§0' pollution dispatch failed for /{skill}. Would you like to proceed in the current (polluted) session, or abort?"
- Header: "Dispatch error"; multiSelect: false
- Option 1 — label "Abort — I'll diagnose and retry"; description "Stop here. Investigate the dispatch error, then re-invoke /{skill}."
- Option 2 — label "Proceed in-session (polluted)"; description "Continue in the current session despite the dispatch failure. Performance may be degraded due to context pollution."
- Both options print the SAME message: `[quoin-S-1: pollution dispatch unavailable; proceeding in current session]` (Option 1 then STOPs; Option 2 then proceeds with skill body).

**§0″/§0‴ 1M-credit-class** (tier-swap opus/Opus ↔ sonnet/Sonnet for §0‴; §0″ shown):
- Question: "§0″ up-dispatch to opus failed with a 1M-context credit mismatch for /{skill}. The parent session carries the 1M-context beta header; Opus lacks 1M credits. How would you like to proceed?"
- Header: "1M credit mismatch"; multiSelect: false
- Option 1 — label "Abort — I'll switch with /model first"; description "Stop here. Run /model in your terminal to switch to a standard-context model (e.g., /model opus), then re-invoke /{skill}."
- Option 2 — label "Proceed in-session at parent tier"; description "Skip the up-dispatch this once. /{skill} runs in the current session (below Opus, but works). Emits a one-line advisory."
- On Option 1: print `[quoin-mintier: 1M-context credit mismatch; abort per user choice — switch with /model and re-invoke /{skill}]` and STOP.
- On Option 2: print `[quoin-mintier: 1M-context credit mismatch on opus up-dispatch; proceeding in-session at parent tier — run /model to switch to standard context]` and proceed to skill body.

**§0″/§0‴ generic (non-1M) error** (tier-swap for §0‴; §0″ shown):
- Question: "/{skill} requires Opus but this session is below Opus. Auto-dispatch to Opus failed. How would you like to proceed?"
- Header: "Min-tier"; multiSelect: false
- Option 1 — label "Abort — run from an Opus session"; description "Stop here. Switch the session to Opus (/model opus) and re-invoke /{skill}."
- Option 2 — label "Proceed at current tier (under-powered)"; description "Run /{skill} on the current cheaper model. Quality may be reduced; emits a one-line advisory."
- On Option 1: print `[quoin-mintier: aborted; re-invoke /{skill} from an Opus session]` and STOP.
- On Option 2: print `[quoin-mintier: min-tier up-dispatch unavailable; proceeding at current tier per user choice]` and proceed to skill body.
