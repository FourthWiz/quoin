---
name: plan
description: "Plans a change before editing, then works the plan step by step with tests. Use for: /plan, 'plan this first', any non-trivial change on an open model."
---

# Plan first

You are running on an open model through `quoin open`. Plan before you touch code, then follow the plan one step at a time. A written plan and a test after each step catch the mistakes a single long pass makes.

## Step 1: Understand

Read the request. Find the files involved with Grep and Glob, then Read the relevant parts. File tools need absolute paths: always pass the full path (start from the current working directory shown in your environment), never a bare file name.

## Step 2: Write the plan

Write `.workflow_artifacts/<task>/plan.md` (pick a short kebab-case task name) with:

- **Goal:** one sentence.
- **Steps:** a numbered list, each one change small enough to test on its own, naming the absolute path of each file it touches.
- **Tests:** the command that runs the tests covering the change.
- **Done when:** the observable checks that prove the request is met.

Show the plan in one short message. Ask before continuing only if the request is ambiguous; otherwise go on.

## Step 3: Work the plan

For each step: make the change, run the tests, and fix failures before the next step. Mark the step done in `plan.md` (`- [x]`). If a step turns out wrong, update the plan rather than forcing it.

## Step 4: Finish

Run the full test command once more and print its output. Commit on the task branch (never main or master) with a conventional commit message. Reply with what changed, the test result, and anything left undone. Do not push.
