# repo_cleanup

Runtime-neutral intent for the repo_cleanup skill. Any runtime adapter
(Claude, Codex, ...) that implements this skill should match the contract
described here.

## Purpose

A whole-repo cleanup pass over a target repository in two fixed phases. The
comment phase removes redundant comments from Python files under a
removal-only proof. The test phase trims the Python test suite using
coverage and mutation evidence and speeds up slow tests. Both phases work on
local commits only. The skill never pushes and never opens a pull request.

It differs from `/cleanup`, which rewrites the comments of one branch diff.
This skill sweeps the entire tracked repository and also looks at the tests.

## When to use

- A repository has accumulated narration comments, commented-out code or
  slow, redundant tests, and a full sweep is wanted rather than a diff pass.
- Invoked explicitly as `/repo_cleanup`; it is never triggered implicitly.

## Inputs

- Target repository: defaults to the project root or current directory.
- `--include` and `--exclude`: path filters for the comment phase.
- `--phase comments|tests|both`: default `both`; order is always comments
  first, then tests.
- `--dry-run`: analyse and report, change nothing, commit nothing.
- `--mutation-tool mutmut|cosmic-ray|none`: override tool detection.
- `--mutation-budget MINUTES`: wall-clock budget for all mutation runs,
  default 30.

## Output

- Local commits: one for the comment phase, then one per test-change
  category that has changes (execution and config, efficiency, removals).
- A cleanup report at `.workflow_artifacts/<task>/repo-cleanup-report.md`
  when run inside a workflow task, plus a chat summary. It lists comments
  removed (count, by file), comments kept as protected, non-Python files
  skipped, tests removed with reasons, efficiency and execution changes,
  surviving-mutant findings, skipped analyses, and before and after test
  count, total runtime, slowest tests, coverage and mutation score.
- A scratch directory outside the target repository holding every
  measurement output; it is kept after the run and its path is named in the
  report.

## Behavior contract

Pre-flight:

- Refuse to run on `main` or `master` and on a dirty working tree, print the
  reason and stop. Record a baseline (test count, runtime, coverage) before
  any change.

Comment phase (Python only):

- Non-Python files are skipped and counted by extension. Docstrings are never
  touched.
- The helper `repo_comment_cleanup.py` lists comment blocks, applies
  deletions and verifies equivalence. Whole files are chunked (about 150
  blocks or 40 KB), up to 8 concurrent subagents judge each chunk, comment
  text is fenced as untrusted data, and uncertain blocks are kept.
  Protected blocks (lint directives, license lines, TODOs citing a ticket,
  `#123` or a URL, URLs) are dropped before `apply`, and `apply` refuses them
  again. Chunk decisions are written to the scratch directory so a rerun can
  resume.
- `verify` is the gate: the AST and the code tokens must be unchanged, only
  comments may disappear, protected comments must remain, encoding, line
  endings, whitespace and file mode must be untouched. Any failure stops the
  phase and nothing is committed.

Test phase:

- Tool detection order: the tool the repo configures, then mutmut, then
  cosmic-ray, then none. Read the tool help and version before first use.
- `suite_audit.py` supplies `durations`, `static`, `redundancy`, `compare`
  and `deselect-plugin`. Removal sets reach pytest through a plugin loaded
  with `PYTEST_ADDOPTS`, never through edits to the target repo's config.
- Evidence rule: a test is removed only when coverage shows it is redundant
  and a scoped mutation run confirms no mutant is killed only by it.
  Zero-coverage tests are never removable. Candidates without both kinds of
  evidence stay report-only. Mutation score is reported as scoped to code
  covered by removal candidates.
- Non-deleting test changes are gated by category: execution and config
  changes need a green suite and an unchanged test count and coverage;
  efficiency edits also need coverage not to drop and, when a mutation tool
  exists, no new surviving mutants; fixture scope widening additionally needs
  a green run in reverse order and green runs under both `-n 0` and `-n auto`
  when xdist exists. A failed gate reverts that category and reports it.
- Removals are bisected on failure: a failing compare reverts the removal and
  the offending tests are found by halving; if it still fails all removals
  are abandoned.
- Mutation runs need a committed tree and a clean `git status --porcelain`
  afterward. A `mutants/` directory the skill created is moved out with the
  trash helper at the end; a pre-existing one is left alone.
- JavaScript and TypeScript suites are reported only. CI file edits are
  limited to flags proven locally and labelled unverified in CI.

## Out of scope

- Non-Python comment cleanup, docstring edits, pushing, pull requests.
- Removing tests with zero measured coverage.
- Running or editing non-Python test suites.

## Notes

- Helper scripts: `repo_comment_cleanup.py` (list, apply, verify) and
  `suite_audit.py` (durations, static, redundancy, compare, deselect-plugin),
  both stdlib-only with thin wrappers under `quoin/scripts/`.
- The existing `comment_cleanup.py` backs `/cleanup` and is untouched.
