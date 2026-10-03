# Continuation record

## Purpose

A continuation record lets a new runtime session pick up a task where an earlier session stopped, including when the earlier session ran in a different runtime. It states which phases are finished, which are pending, what the operator decided, which artifacts existed and with what content hash, the repository revisions, the validation results, and the policy ceiling the earlier session ran under.

It is not the dispatch and return envelope described in `handoff-format.md`. That envelope stays unchanged and may point at a record through its existing `checkpoint:` field.

## Location

One record per task at `.workflow_artifacts/memory/continuation/TASK.json`. The previous copy is kept as `TASK.prev.json`. Every write is an atomic replace: the new content is written to a temporary file in the same directory, flushed, and renamed over the target. Directories are mode 700 and files mode 600. A symlinked directory component or target is refused.

## Schema

The schema identifier is `quoin-continuation/1`. Every object is closed: a key not listed here makes the record invalid. Fields marked R are required and fields marked O are optional. Names use dots for nesting and `[]` for list items.

| Field | R/O | Type and meaning |
|---|---|---|
| `schema` | R | string, exactly `quoin-continuation/1` |
| `task` | R | task name, `[A-Za-z0-9][A-Za-z0-9._-]{0,127}` |
| `created_at` | R | UTC timestamp, `YYYY-MM-DDTHH:MM:SSZ` |
| `origin_runtime` | R | free lowercase token naming the runtime that wrote the record |
| `phase` | R | object: the item the next session works on |
| `phase.current` | R | gated phase name or null when everything is done |
| `phase.stage` | R | integer at least 1, or null (always null for discover and architect) |
| `phase.status` | R | one of `pending`, `in_progress`, `interrupted`, `done` |
| `completed` | R | list of finished items that passed their gate |
| `completed[].phase` | R | gated phase name |
| `completed[].stage` | R | integer at least 1 or null |
| `completed[].run_id` | R | token of the run that produced it, or null |
| `completed[].gate` | R | always `PASS` |
| `pending` | R | list of items still to do |
| `pending[].phase` | R | gated phase name |
| `pending[].stage` | R | integer at least 1 or null |
| `decisions` | R | list of operator decisions worth carrying forward |
| `decisions[].text` | R | free text, at most 2000 characters |
| `decisions[].source` | R | token naming where the decision came from |
| `notes` | O | list of free text notes |
| `artifacts` | R | list of task artifacts with content hashes |
| `artifacts[].path` | R | project-relative POSIX path |
| `artifacts[].sha256` | R | lowercase hex digest of the file |
| `artifacts[].type` | R | token classifying the artifact |
| `repo_revisions` | R | list of repository states |
| `repo_revisions[].path` | R | repository path relative to the project root (`.`, a run of `..`, or a descending path) |
| `repo_revisions[].head` | R | commit id or null when unknown |
| `repo_revisions[].source_dirty` | R | boolean or null when unknown |
| `repo_revisions[].source_digest` | R | digest of the working tree sources or null |
| `repo_revisions[].source_error` | O | token naming why a value is unknown |
| `validation` | R | list of recorded gate results |
| `validation[].phase` | R | gated phase name |
| `validation[].stage` | R | integer at least 1 or null |
| `validation[].verdict` | R | `PASS` or `FAIL` |
| `validation[].reasons` | R | list of reason tokens |
| `provenance` | R | object describing how the record was built |
| `provenance.sources` | R | non-empty list of source tokens |
| `provenance.transcripts_imported` | R | always the literal `false` |
| `provenance.scope_source` | O | token saying where the recorded profile came from |
| `unavailable_telemetry` | R | list of unique tokens for facts the runtime could not supply |
| `scope` | R | object: the policy the earlier session ran under |
| `scope.profile` | R | evaluated profile name |
| `scope.classification` | R | classification token |
| `scope.policy_ceiling` | R | closed object, the ceiling a continuation may not widen |
| `scope.policy_ceiling.enabled_providers` | R | list of provider tokens |
| `scope.policy_ceiling.provider_allowlist` | R | list of `PROVIDER/MODEL` references |
| `scope.policy_ceiling.role_models` | R | map of role token to model reference |
| `scope.policy_ceiling.limits` | R | map of limit name to integer or null (null is unlimited) |
| `scope.policy_ceiling.network` | R | network policy token |
| `native` | O | object: optional aid for resuming a native session |
| `native.run_id` | O | token of the interrupted run |
| `native.session_id` | O | token of its native session, or null |

Example:

```json
{
  "schema": "quoin-continuation/1",
  "task": "example-task",
  "created_at": "2026-01-01T00:00:00Z",
  "origin_runtime": "example-runtime",
  "phase": {"current": "implement", "stage": 1, "status": "pending"},
  "completed": [{"phase": "plan", "stage": 1, "run_id": null, "gate": "PASS"}],
  "pending": [{"phase": "implement", "stage": 1}],
  "decisions": [{"text": "keep the parser strict", "source": "checkpoint"}],
  "artifacts": [{"path": "task/current-plan.md", "sha256": "<64 hex digits>", "type": "current-plan"}],
  "repo_revisions": [{"path": ".", "head": null, "source_dirty": null, "source_digest": null}],
  "validation": [{"phase": "plan", "stage": 1, "verdict": "PASS", "reasons": []}],
  "provenance": {"sources": ["workflow-state"], "transcripts_imported": false},
  "unavailable_telemetry": [],
  "scope": {
    "profile": "personal",
    "classification": "personal",
    "policy_ceiling": {
      "enabled_providers": ["alpha"],
      "provider_allowlist": ["alpha/model-one"],
      "role_models": {"planner": "alpha/model-one"},
      "limits": {"max_run_seconds": 600},
      "network": "profile-default"
    }
  }
}
```

## Validation

Validation returns reason strings; an empty list means the record is valid. Codes: `record-not-object`, `field-missing:NAME`, `field-invalid:NAME`, `field-unknown:NAME`, `schema-unsupported` (a well-formed `quoin-continuation/N` with N other than 1), `transcripts-imported`, `path-invalid:NAME`, `text-oversized:NAME`, `text-control-character:NAME`, `list-too-long:NAME`, `duplicate-entry:LIST`, `pending-overlaps-completed`, `phase-inconsistent`, `record-too-large`. Names use the form `name[index].key`.

Loading a file adds `unsafe-path`, `record-missing`, `record-unreadable`, `record-too-large`, `json-invalid` and `record-not-object`. Writing adds `record-invalid`, carrying the validation reasons. Continuing from a record adds `continuation-missing`, `continuation-legacy-format`, `continuation-invalid`, `task-finalized`, `profile-mismatch`, `classification-mismatch` and `policy-widened`. The codes `continuation-state-mismatch` and `continuation-artifact-changed` are reserved for the coordinator that decides whether to continue.

Paths in `artifacts` must be project-relative POSIX paths: no absolute form, no leading `~`, no backslash, no NUL, no `.`, `..` or empty segment, no trailing slash, no drive prefix. Free text is limited to 2000 characters and may not contain control characters other than newline and tab, line or paragraph separators, or bidirectional controls.

## Scope comparison

A continuation may narrow the recorded scope but never widen it. Profile and classification must be equal; any difference refuses. For the ceiling: enabled providers and the allowlist of the requested scope must be subsets of the recorded ones; every requested role must exist in the recorded role models with the same model (a changed model counts as widening); for each limit, a recorded null or absent value means unlimited, a requested null or absent value against a recorded number is a widening, and a requested number above the recorded number is a widening; the network policy must be equal. All differences are reported together, naming fields and never values.

## Continuation rules

A new runtime session is always created from the record. Transcripts are never read, and `provenance.transcripts_imported` is always false. Native run and session ids are an optional aid only: they are used when the named run is still interrupted and resumable, and the continuation starts a fresh native session otherwise. Older-format checkpoints and finalized tasks are refused, never migrated. Recording workflow entries after runs is left to a later coordinator, so until then a phase finished by a headless run has no workflow entry and is reported as a completed run to adopt and gate.

## Versioning

The first major version is `quoin-continuation/1`. Unknown keys refuse. Any new field is a new major version, because a reader that cannot validate a field must not act on a record that carries it.
