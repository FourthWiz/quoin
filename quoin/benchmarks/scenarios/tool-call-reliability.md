# Tool-Call Reliability

## Purpose

Separate runtime and protocol reliability from task quality. The scenario
checks whether a model behind a given endpoint completes a harmless native tool
loop (read a file, list a directory, run a read-only command, report the
result) without malformed tool arguments, lost tool results or stalled turns.
A failure here is a protocol or gateway problem, not evidence about workflow
quality.

## Starting State

Use a small synthetic fixture repository containing a known file with a known
line count. No credentials, no network destinations beyond the model endpoint,
and no write permissions are needed. Freeze the fixture and the prompt before
any prompt tuning.

## Prompt

Read `README.md`, count its lines with a read-only shell command, list the top
level of the repository, and reply with the line count and the file names.
Do not modify any file.

## Mode Notes

- Simple Claude: not part of the pilot thresholds; may be run as a reference.
- Quoin + Claude: not part of the pilot thresholds; may be run as a reference,
  with any artifacts under `.workflow_artifacts/`.
- Simple Codex: not part of the pilot thresholds; may be run as a reference.
- Quoin + Codex: not part of the pilot thresholds; may be run as a reference.
- Simple OpenCode: run the prompt with the pinned binary, repeated 20 times on
  the same model and endpoint.
- Quoin + OpenCode: same prompt and repetitions with the Quoin scaffold
  installed; nothing should be written under `.workflow_artifacts/` beyond run
  evidence.

## Expected Evidence

- Raw JSON event stream per repetition.
- The reported line count and file list.
- Tool error count and any protocol diagnostics per repetition.
- An unchanged working tree (empty diff).

## Evaluation Notes

A repetition succeeds when the answer is correct, every tool call returned a
result, the tree is unchanged, and no protocol error occurred. Record every
repetition, including failures, with its failure mode. The pilot threshold is
at least 19 successes out of 20 repetitions; treat that as qualification
evidence for one model and endpoint, not a general reliability guarantee.
