# Decision-gate guard reference (fail-closed) — IVG-150

Verbose Tier-1 reference for the shared fail-closed decision-gate guard. This is the
single source of truth for the "cannot ask ≠ approved" invariant, the `[no-interactive]`
sentinel, the ordered decision-gate contract, the `needs-decision-{task}.md` sentinel
schema, the structured NEEDS-DECISION return, and the classification-marker census.
`run_hard/SKILL.md` and the per-skill `SKILL.md` bodies carry the operative branches; this
file documents the whole surface in one pass.

This primitive is a **sibling of `[autonomous]` (IVG-153) with the opposite default**:
`[autonomous]` carries pre-authorized per-site answers so a decision gate auto-resolves;
a background context with NO opt-in has no pre-authorized answer, so its only safe move is
to fail closed. See `autonomous-mode.md`.

## The invariant: "cannot ask" ≠ "user approved"

A skill that gates on a REQUIRED user decision must **never** treat "I cannot surface the
prompt to a human" as "the user approved." When a required decision cannot be surfaced and
no pre-authorized `[autonomous]` resolution applies, the only safe degrade is to **fail
closed** — hard-stop, record the pending decision durably, and hand a structured signal
back to the caller. Never a silent proceed-on-default; never a silent stall.

## Layered detection (per mechanism shape)

No ambient interactivity probe exists in the runtime (a whole-tree grep for `isatty`,
`TERM`, `headless`, `non_interactive`, etc. returns zero hits). Detection is therefore an
explicit signal plus a per-mechanism fail-safe:

1. **Pre-flight (primary, load-bearing): the `[no-interactive]` leading-prompt sentinel.**
   A greppable, stackable, strip-before-processing sentinel (same convention as
   `[autonomous]` / `[no-session-age-guard]` / `[no-redispatch]`), parsed at bootstrap into
   a local `_INTERACTIVE=false` state. `run_hard` (`/run hard:`) injects it onto every NON-autonomous
   phase-subagent spawn (and subagent-mode gate spawns). This is the ONLY layer that works
   at the two non-`AskUserQuestion` sites (`/gate` STOP, session-age guard), so it is
   load-bearing, not an optimization.

2. **Post-flight (backstop, `AskUserQuestion`-only): a suppressed / empty / errored /
   unavailable `AskUserQuestion` is treated as "cannot ask."** When an AUQ decision gate
   cannot obtain a usable answer, the skill fails closed rather than falling through to a
   default.

**POC finding (T-01, `stage-1/poc-decision.md`), which pins the backstop wording:**
`AskUserQuestion` is **not provisioned to Agent-tool subagents at all** — it is absent from
the subagent tool list. A subagent therefore cannot even issue the call; there is no
prompt to suppress, no empty answer, no hang. Consequence: the backstop cannot fire on an
issued-but-suppressed call in a subagent (there is no call), so the **pre-flight
`[no-interactive]` sentinel is the sole load-bearing detector** for the subagent and
headless paths. Backstop prose must read: "AskUserQuestion is not provisioned to
subagents, so a gate reached there has no interactive prompt to surface — fail closed via
the pre-flight sentinel; if a future harness DOES surface AUQ and it returns
empty/error/no-usable-answer, fail closed the same way."

**Secondary reinforcing signal (documented, NOT load-bearing):** the environment variable
`CLAUDE_CODE_CHILD_SESSION=1` is set in every Agent-tool subagent (absent in the
foreground). This resolves Q-03 (a real subagent signal is knowable) and could reinforce
detection for a generic (non-`/run`) Agent spawn, but the design keeps the deterministic,
greppable, censusable `[no-interactive]` sentinel as the primary mechanism.

### Per-site detection matrix

| Site | Mechanism | Pre-flight `[no-interactive]` | Post-flight backstop |
|------|-----------|------------------------------|----------------------|
| end_of_task garbage / commit / archive | AskUserQuestion | yes | yes (AUQ unavailable/suppressed → fail closed) |
| implement branch-hygiene | AskUserQuestion | yes | yes |
| specify repo-spec write-gate | option-list (prose) | yes | safe-degrade: auto-reject |
| gate human-approval | STOP-and-wait | yes (sole detector) | n/a — no AUQ |
| session-age guard (IVG-146) | script exit code | yes (sole detector) | n/a — no AUQ |
| rollback destructive-undo | AskUserQuestion | yes | yes |

## The `[no-interactive]` sentinel

- Leading-prompt sentinel: `[no-interactive]`. Greppable, stackable, strip-before-
  processing. A spawn prompt may stack sentinels, e.g. `[no-redispatch] [no-interactive]
  <task>`.
- Parsed at each in-scope skill's bootstrap into `_INTERACTIVE=false` (default: interactive
  == absent).
- **Only `run_hard` injects it, only onto NON-autonomous phase-subagent spawns and
  subagent-mode gate spawns.** `/thorough_plan` never injects it (it spawns no fail-closed
  skill). The inline post-implement / post-review gates run in the FOREGROUND `run_hard`
  session where a human IS reachable — `run_hard` MUST NOT inject `[no-interactive]` onto them.
- **Mutually exclusive with `[autonomous]` per spawn:** under AUTONOMOUS, `run_hard` injects
  `[autonomous]` (pre-authorized answers) instead of `[no-interactive]`.
- Absence == interactive (default), so a real foreground user is never fail-closed (R-06).

## The decision-gate contract (the shared rule)

Each in-scope gate evaluates one ordered rule. The `[autonomous]` branch and any scripted
bypass are PER-SITE — present only where that site actually has one (`rollback` has no
`_AUTONOMOUS` arm; the session-age guard has `[no-session-age-guard]`):

```
on reaching a REQUIRED decision:
  if site has an [autonomous] arm AND _AUTONOMOUS:
      apply the existing pre-authorized [autonomous] resolution   # UNCHANGED, per-site
  elif site has a scripted bypass present (e.g. [no-session-age-guard]):
      proceed per that bypass                                     # UNCHANGED
  elif interactive (NOT _INTERACTIVE==false)
       AND (non-AUQ site OR AskUserQuestion returns a usable answer):
      honor the decision                                          # normal path
  else:                                                           # non-interactive, non-autonomous
      FAIL CLOSED -> decision_gate_guard.py fail-closed: write sentinel + emit needs-decision; STOP
```

`rollback` (no autonomous arm, source-mutating) reduces to: interactive → confirm; else →
fail closed. `/specify` repo-spec write-gate is a `safe-degrade` boundary: its degrade
action is auto-Reject (leave the human-owned repo spec untouched), NOT a helper STOP.

## The helper — `decision_gate_guard.py`

Canonical at `quoin/quoin/core/scripts/decision_gate_guard.py` (runtime-neutral: the
sentinel write + structured return live in the portable `.workflow_artifacts/memory/`
layout → core, D-04) with a thin wrapper at `quoin/quoin/scripts/decision_gate_guard.py`.
Core is stdlib-only and must not import from the adapter `scripts/` layer.

One subcommand does BOTH fail-closed actions atomically:

```
decision_gate_guard.py fail-closed \
  --task T --skill S --site SITE --reason R --resume-hint H \
  [--project-root ROOT] [--memory-dir DIR]
# -> writes <memory-dir>/needs-decision-<T>.md   (atomic write<tmp> then os.replace)
# -> prints the needs-decision block to stdout for the skill to echo
# exit 3 = fail-closed recorded (distinct from 0/1 so callers branch)
```

## Sentinel schema — a sibling of the halt family

The fail-closed sentinel is `needs-decision-{task}.md` under `.workflow_artifacts/memory/`
— a **DISTINCT filename** that `src/quoin/supervisor.py:read_halt()` never reads, so a
background fail-close can never poison a future `quoin run --autonomous {task}` (the
supervisor HALTs whenever `autonomous-halt-{task}.md` merely EXISTS). It reuses the
halt-sentinel CONTRACT (memory/ location, atomic write, survives the `/end_of_task` archive
move, schema shape) but is a role-specific sibling. `autonomous-halt-{task}.md` is
UNCHANGED (no schema rename, no migration).

The 7 schema fields (exact order; kept in lockstep with the helper's `SENTINEL_FIELDS`):

```
task: <task-name>
trigger: non-interactive-decision-gate
skill: <skill>
site: <decision-site-id>          # e.g. commit-decision
reason: <one line — which decision, why it could not be surfaced>
timestamp: <UTC ISO8601>
resume_hint: <one line — e.g. re-run /end_of_task interactively, or pass --autonomous>
```

**Cleanup lifecycle:** `needs-decision-{task}.md` is cleared when the decision is resolved
(the next successful interactive pass of that gate deletes it) and is subject to
`/cleanup`'s trash-move. It is a durable human-facing audit record; no automated relaunch
consumes it.

## Structured NEEDS-DECISION return

The helper prints, and the skill echoes as its final message, a machine-extractable block
(mirroring `review`'s verdict shape). `run_hard` routes `NEEDS-DECISION` exactly like it routes
review-BLOCKED / gate-FAIL — surface the block, STOP, never silent-proceed:

```
gate-result: NEEDS-DECISION
needs-decision:
  skill: end_of_task
  site: commit-decision
  task: askuserquestion-background-guard
  sentinel: .workflow_artifacts/memory/needs-decision-askuserquestion-background-guard.md
  resume_hint: re-run /end_of_task in an interactive session, or pass --autonomous
```

## Classification-marker convention + census

Every genuine decision site carries a greppable marker so the coverage guard
(`test_decision_gate_census.py`) can enumerate it structurally:

```
<!-- decision-gate: <class> site=<id> [reason=...] [tokens=N] -->
```

**Four classes:**
- `fail-closed` — genuinely-blocking proceed/ship gate → MUST reference
  `decision_gate_guard.py fail-closed`. The 6: end_of_task garbage-files / commit-decision
  / archive-type, gate gate-approval, implement branch-hygiene, rollback destructive-undo;
  plus the session-age gate (IVG-146).
- `best-effort` — degrades in a non-interactive context; MUST NOT reference the helper
  (default class for all remaining `AskUserQuestion` sites, e.g. enrich gap-questions,
  implement task-confirm, end_of_task lessons prompt, discover repo-spec offers, etc.).
- `out-of-scope` — user-invoked destructive site; MUST NOT reference the helper
  (`/sleep --purge`, `reason=user-invoked-destructive-requires-older-than`).
- `safe-degrade` — auto-reject a human-owned-artifact write; never proceeds on a default,
  MUST NOT reference the helper. Sole member: `/specify` repo-spec write-gate.

**Marker granularity = MARKER-TOKEN ADJACENCY / STRICT 1:1 BIJECTION (D-02).** Every
genuine token has its OWN classification marker as the nearest-preceding annotation. A
marker's SCOPE (marker line → next decision-gate marker OR next H2/H3/H4 heading, whichever
comes first) must contain EXACTLY the number of genuine tokens its optional `tokens=N`
field declares (default 1). More than declared → an absorbed extra gate inheriting a
neighbour's class → census FAILS. Fewer → a decorative/orphan marker → FAILS. Documented
exceptions: `tokens=0` = a self-declared prose/option-list site with NO call-syntax token
(the `/specify` `safe-degrade` marker); `tokens=N>1` = a single logical gate legitimately
spanning N call-syntax tokens (none today after call-syntax dedup).

**Census population derivation (the linchpin; D-03).** Enumerate GENUINE body decision
sites only — the generated dispatch-recovery `AskUserQuestion` prompts are infrastructure
fail-OPEN recovery, not user gates:
1. Strip generated dispatch-preamble sections STRUCTURALLY by the generator's OWN heading
   constants. Import FIVE constants from `inject_pollution_dispatch.py` (via
   `importlib.util.spec_from_file_location` — the dotted `quoin.quoin.scripts.…` path does
   NOT resolve): `SECTION0_HEADING`, `POLLUTION_HEADING`, `MINTIER_HEADING`,
   `MINTIER_SONNET_HEADING`, `ZC_HEADING`. Drop every H2 section whose heading equals one of
   the five. (Belt-and-suspenders: regex-strip residual `<!-- §0*-begin -->…<!-- §0*-end -->`
   fenced sub-regions.) Hand-authored `§0a` / `§0b` (Scope-cap, Branch-hygiene §0b,
   Session-age §0b) SURVIVE — they are genuine.
2. Enumerate surviving sites by CALL/INVOCATION syntax: `AskUserQuestion(` call-sites, the
   `session_age_guard.py` invocation, the gate-approval STOP (gate/SKILL.md `### Step 4`
   heading anchor), and the `/sleep --purge` `[y/n]` prompt. This drops prose/negation
   mentions that a bare-token grep would wrongly count.

A generator heading rename propagates automatically via the import, so the census stays in
lockstep with the generator and never re-types the headings. Verified live: ~25 genuine
sites.

## In-scope roster (audit result)

6 fail-closed sites across 5 skills — `/end_of_task` ×3 (garbage-files, commit-decision,
archive-type; the 4th, lessons, is best-effort), `/gate` approval (STOP, pre-flight-only),
`/implement` branch-hygiene, `/rollback` destructive-undo (no autonomous arm). The
session-age guard is a 7th in-scope gate handled via IVG-146. Plus 1 `safe-degrade`
boundary (`/specify` repo-spec write-gate → auto-reject). All remaining `AskUserQuestion`
sites are `best-effort`; `/sleep --purge` is `out-of-scope`. Every one carries a census
marker so none is invisible to the coverage guard.
