---
name: review
description: "Reviews the current change against its plan and tests before it is committed or shared. Use for: /review, 'review my change', 'check before committing'."
---

# Review

You are running on an open model through `quoin open`. Review the change as an independent pass: assume it has a mistake until the evidence says otherwise.

## Step 1: Collect the evidence

Run, one command each:

- `git status --short` and `git diff` (add `--cached` for staged changes, or `<base>...HEAD` for committed ones);
- the plan, `.workflow_artifacts/<task>/plan.md`, if one exists;
- the tests that cover the changed files; print their output.

## Step 2: Check

Go through every changed file and ask:

1. Does it do what the plan or request says, and nothing else?
2. Is every caller of a changed function, field or route updated? Grep for each changed name.
3. Are errors, empty inputs and edge cases handled?
4. Do the tests cover the new behaviour, and do they pass?
5. Any secret, debug print or leftover code?

## Step 3: Report

List findings, each as `path:line`, severity (blocker, should-fix, nit) and the concrete fix. Then one verdict line: `APPROVED` when there are no blockers or should-fixes, else `CHANGES REQUESTED`. Do not edit code during the review; fix afterwards if asked.
