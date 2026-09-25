# Clean authored content

Comments, commit messages, and PR descriptions are shipped work product. They read as plain
engineering to someone who has never seen the planning artifacts that produced them.

A comment earns its place when it explains the why to a reader who has never seen the planning artifacts.

## Prohibition list

Shipped source and test files never carry: plan, decision, finding, or stage IDs (e.g. `T-04`,
`D-02`, `R-07`), severity labels (critical, major, minor), review-round narration ("round 2 fixed
this", "critic caught"), gate verdicts (PASS/FAIL/REVISE), confidence scores, planning-artifact
paths and filenames (`current-plan.md`, `architecture.md`, `critic-response-*.md`), or external
tracker IDs.

## Translate or delete

If a comment exists only to record why a decision was made during planning, either translate it
into plain engineering rationale a future reader can act on, or delete it. Do not carry the
planning artifact's language into shipped code — the reader of the code was not in the planning
conversation and has no use for its vocabulary.

## Commit messages

Commit messages explain what changed and why, in engineering terms. They describe the change
itself, not the process that produced it — no review-round narration, no plan or decision IDs, no
gate verdicts.

## Pull request descriptions

PR descriptions are shipped work product in the same sense as commit messages. Summaries,
change lists, and testing notes describe the code, not the planning process behind it.
They also carry no tool or agent attribution footers or agent session links.

## Opportunistic cleanup

When a task already touches a file that contains stale process vocabulary from an earlier era of
the workflow, it is reasonable to clean it up as part of the touch — but this rule does not
mandate a sweep of untouched files.

## Escape hatch

A line that legitimately discusses this taxonomy as subject matter (for example, this
detector's own tests) may carry the literal pragma `quoin-lint: allow` to suppress it from
`authored_content_lint.py`'s findings.

## Non-scope

Planning artifacts (plans, architecture documents, critic responses, session state) keep their
mandated stable IDs — this rule does not apply to them, only to shipped source and test files.
Markdown files in general are out of this rule's mechanical scope; it governs code comments,
commit messages, and PR descriptions specifically.
