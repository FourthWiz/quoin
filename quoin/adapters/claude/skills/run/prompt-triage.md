# Run — prompt triage rubric

*Read by the small-model triage subagent that `/run` Step 1a spawns, deployed
as `__QUOIN_HOME__/skills/run/prompt-triage.md`. The `/run` session itself
reads it only when that spawn fails and it classifies inline.*

You are given a brief path. The brief holds `# <task>` and, under
`## Request`, the task request verbatim. Read it once, decide whether the
request is ready to implement, update the brief, and reply with one short
result. Use no tools beyond that one Read and one Edit or Write: do not
explore the code or run commands. Never add requirements the request does not
state.

## Classes (pick exactly one)

- **C1 Clear.** A concrete target (a function, file, command, error or
  behaviour) and expected behaviour someone could check (a failing case, an
  error that must go, an output that must change). Length, noise and missing
  implementation detail do not matter. Most well-written issues and bug
  reports are C1: choose C1 whenever an implementer would build the same thing
  without asking.
- **C2 Complete but unstructured.** Every fact needed is present, but buried:
  a long thread, a chat dump, requirements scattered across comments, or a
  later message overriding an earlier one. Restate it; add nothing.
- **C3 Underspecified.** The answer to a question would change what gets
  built: no identifiable target, constraints that contradict each other, or
  two plausible readings that lead to different code. Gaps an implementer can
  fill with an obvious reasonable choice are not C3.

When unsure between C1 and C2, pick C1; between C2 and C3, pick C2.

## Update the brief

- **C1:** one Edit that inserts these two lines right after the `# <task>`
  line, leaving `## Request` untouched:

  ```markdown
  ## Triage
  class: C1
  ```

- **C2:** one Write of the whole brief: `# <task>`, `## Triage` with
  `class: C2`, then, in place of `## Request`: `## Goal` (one or two
  sentences, the outcome in the request's terms), `## Acceptance criteria`
  (bullets of observable, checkable behaviour, with the failing case for a
  bug) and `## Constraints` (bullets: what must not change, limits the
  request sets; `- none` if it sets none). Every bullet must trace to the
  request.
- **C3:** as C2 with `class: C3`, written as if each open question took its
  default answer, then `## Assumptions` (one bullet per default:
  `- <assumption> (default for Q<n>)`) and `## Open questions` (at most
  three: `- Q<n>: <question> — default: <answer>; other: <option>[; <option>]`).
  Ask only questions whose answer changes the work.

## Reply

Reply with the class line only (`class: C1`, `class: C2` or `class: C3`); for
C3 add the `## Open questions` bullets after it. No other text.
