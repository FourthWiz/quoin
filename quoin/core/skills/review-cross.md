# review-cross

Runtime-neutral intent for the review-cross skill. Any runtime adapter that
implements this skill should match the contract described here.

## Purpose

Review one task's change across every repository it touched, focused on the
interfaces between them: when a provider changes, do its consumers still work,
and when a consumer changes, does it still match its provider. It runs once,
after the last stage of a staged cross-repository change, and complements the
per-stage review, which sees one repository at a time.

## When to use

- After the last stage of a staged (r2) run, before `/pr`.
- By hand, for any branch that exists in several repositories of the project.

## Inputs

- The task branch (the same name in every repository).
- The review pack from `review_cross_pack.py`: the impact report, each touched
  interface with its consumers' current code, and every repository's diff.
- The discovery map's `interfaces` section (the graph the pack is built from).

## Outputs

- `.workflow_artifacts/<task>/review-cross.md`: findings, each with
  `repo/path:line`, severity (blocker, major, minor) and a concrete fix, then a
  verdict (`APPROVED` or `CHANGES REQUESTED`).
- The same findings in the reply.

## Rules

- Read-only: it reports and never edits code.
- Every finding names a file and line in a specific repository and says what to
  change; findings without one are left out.
- Consumers of a changed interface come first, including consumers the change
  did not touch.
