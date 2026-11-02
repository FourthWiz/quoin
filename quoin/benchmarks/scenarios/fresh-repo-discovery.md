# Fresh Repo Discovery

## Purpose

Evaluate how well the workflow builds an accurate first-pass map of an
unfamiliar repository without changing files.

## Starting State

Use a clean checkout of the fixture repository. Do not provide prior project
notes beyond the scenario prompt and normal runtime instructions.

## Prompt

Inspect this repository and produce a concise discovery report: what the project
does, where the main code and tests live, how to run the relevant checks, and
the top risks or unknowns for a future contributor. Do not edit files.

## Mode Notes

- Simple Claude: use Claude normally and return a discovery report.
- Quoin + Claude: use the Claude adapter discovery workflow and record Quoin
  artifacts under `.workflow_artifacts/`.
- Simple Codex: use Codex normally and return a discovery report.
- Quoin + Codex: use repo-local Quoin guidance and preserve portable discovery
  evidence under `.workflow_artifacts/`.
- Simple OpenCode: use the pinned OpenCode binary to explore and summarise the
  repository. Same model and endpoint as the Quoin OpenCode run.
- Quoin + OpenCode: run the Quoin discover command from the installed scaffold
  and keep its memory files under `.workflow_artifacts/`.

## Expected Evidence

- Final discovery report.
- Files or directories inspected.
- Commands run, if any.
- For Quoin modes, relevant `.workflow_artifacts/` entries.

## Evaluation Notes

Score for correctness of the repository map, usefulness to a future
contributor, clarity about unknowns, and whether the run avoided unnecessary
setup or edits.
