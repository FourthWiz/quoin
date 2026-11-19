# Run — routes above r0

*Reference for `/run`, deployed as `__QUOIN_HOME__/skills/run/routes.md`.
`/run` reads this file once, with one Read, when the task is r1, r2 or r3:
Step 1b picks that class, a plain run reclassifies upward, or a resumed run's
brief names it. An r0 run never reads it. Step numbers, phase markers and
artifacts of SKILL.md apply unchanged here.*

## Brief

In one Bash call, log the markers of the route below and write
`.workflow_artifacts/<task>/brief.md` with a heredoc (plus `## Assumptions` if
you made any). A `[from-hard]` task's `## Request` is `see <the switch note's evidence: file>`.
A `--resume` or `stage N of <task>` run keeps the brief it has (resume
appends `## Scope` if it is missing).

```markdown
# <task>
## Request
<the request verbatim>
## Scope
route: <r1 | r2 | r3>
signal: <the signal that fired; "forced by r1:" when a tag decided>
```

Each class adds exactly one thing to r0. **r1** → the plain route plus the
Step 7 review (markers: `discover`, `context` `--skipped`, `frame`). **r2** →
a short stage plan: read the light pipeline file
(`__QUOIN_HOME__/skills/run/light-pipeline.md`) once, with one Read, and
follow it; it also holds the switch procedure an interactive r3 needs.

**r3** → the heavy pipeline, on explicit opt-in only. Interactive, when
Escalation allows switching: the light pipeline file's switch procedure,
trigger `design`; "Continue as is" adds `runs-as:` as below. Otherwise (`AUTONOMOUS`,
`NO_SWITCH`, `[from-hard]`, an existing `pipeline-switch.md`) never switch:
add `runs-as: r2` (an r2 signal also holds) or `runs-as: r1` to `## Scope`,
go on as that class, and end the report recommending `/run hard: <task>`.

## r1 — plain route plus review

SKILL.md's plain route (find the code and test command, Steps 5 and 6), then
Step 7 below, then Step 8, whose record call appends
`## Verification` and `## Commit` to this brief.

**Reclassify upward** (never down; not when a tag forced the class) when the
work shows a signal Step 1b missed. r0 → r1: the diff touches an r1 signal;
write the brief above now with `route: r1` and a line `escalated: r0 → r1 —
<signal>`, and run Step 7 before committing. r0 or r1 → r2: the change must
span repos or land in parts: reference.md "Reclassify to r2".

## Step 7 — Review (subagent, one pass; r1 only)

Marker: `review`, chained onto `git add -N <new files>`. Spawn one
general-purpose subagent on **opus** with only the brief, `git diff <base>`
(base = where the task or stage started) and the verification result; not
your reasoning or the context pack, so its read is not anchored on yours. Ask
for at most about 10 findings, each with `path:line`, severity (blocker /
should-fix / nit) and a concrete fix, on correctness against the acceptance
criteria (the brief's request), missed call sites, security, and
proportionate tests (missing or excessive).

Then log `review-fixes`, apply blockers and should-fixes (through the
implementer when the guard dispatched: reference.md "Implementer tier guard
(dispatch)"), skip nits unless trivial, and re-run Step 6 once if code
changed. No second review. List each finding and what you did in
`.workflow_artifacts/<task>/review.md` (`stage-N/review.md` on r2).
Then Step 8 in SKILL.md.
