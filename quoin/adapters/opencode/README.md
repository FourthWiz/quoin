# OpenCode adapter

OpenCode: generated project assets (`quoin install --runtime opencode`,
`quoin doctor --runtime opencode`, `quoin opencode uninstall`), statically
checked and offline smoke-tested; live runtime support is not yet verified.

This directory holds the generator, installer and doctor that produce and
check a project's OpenCode assets, plus the gateway-qualification tooling
used to decide whether a gateway and model are usable as an OpenCode agent
backend.

- `probe_gateway.py` — a standalone script that runs a short handshake against
  a chat-completions-shaped endpoint and reports whether it supports the
  protocol features an agent needs (text generation, native tool calling,
  streamed tool arguments).
- `fake_openai_server.py` — an offline fake provider used by the test suite
  (and available for local experimentation) so the probe can be exercised
  without a real gateway or API credentials.
- `fixtures/scenarios.json` — the data-only catalogue of request/response
  shapes the fake server plays back.
- `quoin opencode config explain|compile|import-preview` and
  `quoin opencode probe` — the runtime-configuration commands: resolve the
  layered profile, compile a native config outside the project, propose a
  personal profile, and qualify a model (see "Runtime configuration").
- `feature-manifest.json` — the classified catalogue of every Quoin skill,
  with a support status, a target milestone, and the OpenCode asset names
  generated for it. See "Support classification" below.

## Install, check and uninstall

Three commands manage a project's repo-local `.opencode/` scaffold:

- `quoin install --runtime opencode --project-root <path> [--profile <label>] [--check]` —
  renders the scaffold from the source tree and writes it under `<path>`. `--check`
  prints the plan and writes nothing.
- `quoin opencode uninstall --project-root <path> [--dry-run]` — removes everything
  Quoin owns under that project. `--dry-run` prints the plan and writes nothing.
- `quoin opencode script <name> [args...]` — runs one allowlisted Quoin script
  against the current project, without a full install. This command is a
  deliberate addition to the spec's CLI surface: the allowlist and its
  refusal behavior are described below.

A plan line names one of these actions for each file: `create` (new), `update`
(owned, content differs), `unchanged` (owned, content matches), `adopt` (an
existing file byte-identical to what Quoin would generate becomes owned —
a deliberate narrowing of "any existing unowned path is a conflict", chosen
so a clean re-render of an unmodified project never conflicts with itself),
`delete` (owned file removed because it's no longer generated), `forget`
(an owned file already missing is dropped from the metadata without an
error), and `conflict` (an existing path the installer cannot safely take
over — nothing is written when any conflict is present).

Ownership metadata lives at `.quoin/opencode-install.json`. It's plain JSON,
safe to commit, and should be committed together with the generated files
or not at all — committing one without the other leaves a teammate's `.quoin`
directory out of sync with what's actually on disk. An existing directory
that ends up holding only files and directories Quoin generated is recorded
as Quoin's own, the same as a directory Quoin created outright, and is
removed on uninstall once it's empty.

A hard kill mid-apply can leave a `.quoin-tmp-*` temporary file behind; the
next install or uninstall sweeps it automatically, so no manual cleanup is
needed.

Exit codes:

| Command | Code | Meaning |
|---|---|---|
| install | 0 | done, or already up to date |
| install | 1 | `--check` found changes pending |
| install | 2 | usage, metadata or generation error, or an apply was interrupted |
| install | 3 | one or more conflicts; nothing was written |
| uninstall | 0 | done, or nothing was installed |
| uninstall | 2 | usage or metadata error |
| uninstall | 4 | done, but one or more owned files had been modified and were left in place; the metadata still lists them, so a later install will conflict on those paths |
| script | (script's own) | the allowlisted script's own exit code |
| script | 1 | the script raised `SystemExit` with a non-integer value |
| script | 2 | an unknown script name, or the runner refused to run it |

`--dry-run` and `--check` return the same code the real run would return.

`quoin opencode script` only runs six allowlisted Quoin scripts:
`checkpoint_picker`, `classify_critic_issues`, `generate_discovery_map`,
`handoff_validate`, `path_resolve`, `validate_artifact`. One of them,
`generate_discovery_map`, writes a file; its output is confined to the
artifact root of the installed project, the same root the generated
permission rules edit inside, by walking every path component between the
project root and the destination with `lstat` — never resolving a symlink.
The runner refuses to run it when: a symlinked path component sits anywhere
in that walk (including a symlinked artifact root or a symlinked `.quoin`
directory); the destination's basename is anything other than
`discovery-map.json`; or a `discovery-map.json.tmp` left over from an
earlier crash already exists at that location (remove it by hand before
retrying). The script's own positional project-root argument is read with
whatever read posture a Quoin role already has — only the write path above
is confined. The runner also refuses to run at all when its own standard
output or standard error is a regular file, so the script's output never
lands in that file — but the shell still creates or empties the target
before the runner starts, and a redirection of any other file descriptor
(such as `3>`) is never seen by the refusal check at all; a hard kill
between the shell opening the target and the refusal firing can leave it
truncated. Passing `--` on the command line is not a transparent
pass-through to the script either: the outer command-line parser consumes
that `--` itself, and the arguments after it still reach the script and are
still checked by the runner's own parser.

## Doctor

`quoin doctor --runtime opencode --project-root <path> [--smoke] [--json]`
checks a project's generated OpenCode assets.

- Host checks (default, no flag): reads the host filesystem and
  environment. Checks the installed state against a fresh render, the
  manifest against the pinned source, `.quoin` install metadata (including
  leftover `.quoin-tmp-*` files), the Claude Code rules fallback
  (`AGENTS.md`/`CLAUDE.md`), OpenCode compatibility flags and config
  sources, a skill census (duplicate skill names, a skill outside the
  project's own `.opencode/skills`, legacy Claude Code skills OpenCode may
  also load), a user `permission` config that narrows a tool a generated
  role's own permission map still allows while it runs, and the `opencode`
  binary's presence and version (not its position on `PATH`). Because it reads the real
  host, a machine that also has Quoin's Claude adapter installed normally
  reports warnings — its own Claude rules files and skills are visible to
  the census, and that is expected, not a failure. Use `--smoke` for a
  clean pass/fail signal, for example in CI.
- `--smoke`: renders the catalog offline into a temporary directory and
  checks the rendered bytes and metadata against themselves. No host
  filesystem or environment is read beyond the source tree, no network
  call is made, and no `opencode` binary is required.
- `--json`: emits the JSON schema below instead of text. Refused with
  exit 2 for any `--runtime` other than `opencode`.

Exit codes:

| Code | Meaning |
|---|---|
| 0 | healthy — no `error` or `warn` findings |
| 1 | one or more `error` findings, or a `--smoke` check failed |
| 2 | usage error, such as `--json` combined with a non-`opencode` runtime (`--smoke` is valid for `codex` too) |
| 4 | warnings only — no `error` findings, at least one `warn` finding |

JSON schema (`--json`):

```json
{
  "schema_version": 1,
  "runtime": "opencode",
  "status": "healthy | warnings | errors",
  "findings": [
    {
      "id": "<finding id>",
      "severity": "error | warn | info | ok",
      "message": "<fixed-vocabulary message>",
      "path": "<display path, present when the finding names one>",
      "remediation": "<suggested next step, present when one applies>"
    }
  ]
}
```

Finding ids, grouped by severity (`legacy-claude-skills` reports `warn`
normally and `info` when the matching disable flag is already set):

| Severity | Finding ids |
|---|---|
| error | `smoke-render`, `smoke-roundtrip`, `smoke-names`, `smoke-frontmatter`, `smoke-digests`, `smoke-bundle`, `smoke-read-only-roles`, `smoke-task-graph`, `smoke-forbidden-output`, `smoke-script-refs`, `smoke-config`, `render-failed`, `manifest-drift`, `manifest-unreadable`, `install-metadata-invalid`, `project-config-disabled` |
| warn | `install-absent`, `owned-missing`, `owned-modified`, `owned-stale`, `owned-not-installed`, `owned-unreadable`, `rules-global-claude-md`, `rules-project-claude-md`, `config-unreadable`, `subagent-depth-raised`, `quoin-not-on-path`, `skill-duplicate`, `skill-duplicate-unnamed`, `quoin-skill-outside-project`, `legacy-claude-skills` |
| info | `temp-leftover`, `rules-agents-md-present`, `flags-set`, `config-env-set`, `opencode-binary-absent`, `opencode-version`, `opencode-version-unknown`, `install-version-differs`, `census-unverified`, `skills-url-not-scanned`, `skills-path-missing`, `census-truncated`, `permission-loosened` |
| ok | `smoke-ok`, `install-current` |

Redaction: every finding message is built only from a fixed message
template plus the names, paths, counts and version numbers the doctor is
explicitly allowed to substitute into it. A path is always rendered under
its matching root's display name (see the install and script-runner
sections above), never as a raw absolute path; the doctor never echoes
file content, exception text or an environment variable's value.

## Runtime configuration

Runtime configuration decides which provider and model each Quoin role uses
when OpenCode runs, and compiles that decision into a native OpenCode config
file that is kept outside the project. It is layered: a personal profile,
an optional project file that may only narrow it, and an optional managed
policy that only constrains. Everything described here is checked offline;
no live OpenCode run or corporate gateway has been qualified.

### Commands

- `quoin opencode config explain [--profile NAME] [--project-root PATH] [--redact] [--json]`
  shows how the layers resolve, which roles are blocked and why, and where the
  compiled files would go. `--redact` masks endpoint hosts, keychain accounts
  and the output location. Exit 0 when compilable, 1 when a blocking finding
  remains, 2 for configuration or usage errors.
- `quoin opencode config compile --profile NAME [--project-root PATH] [--output DIR] [--check] [--allow-unqualified]`
  writes the compiled pair (see file locations). `--check` writes nothing and
  reports whether the files on disk match a fresh build. `--allow-unqualified`
  accepts models without a valid qualification record and marks the result
  not launchable; it is refused for a work project under a managed policy.
  Exit 0 written or up to date, 1 blocked or stale, 2 configuration, usage or
  file-system errors.
- `quoin opencode config import-preview [--profile-name NAME] [--apply] [--confirm-model-id ID ...] [--force]`
  proposes a personal profile from the tier-to-model mapping `quoin models`
  keeps. The preview reads that mapping and writes nothing. `--apply` writes
  the profile only when every provider model id in the proposal is confirmed
  with `--confirm-model-id` (the `model_id` values, not the `or-*` names) and
  nothing else is; an existing profile is kept unless `--force` is given.
  Exit 0 for a preview or a write, 2 for anything refused.
- `quoin opencode probe --profile NAME --synthetic-only [--model NAME] [--project-root PATH]`
  qualifies one profile model against its gateway and writes its qualification
  record. Exit 0 qualified, 1 not qualified, 2 could not run or refused.
- `quoin opencode status (--task NAME | --run-id ID) [--project-root PATH] [--json]`
  reports the latest phase run of a task, or one run id, without changing
  anything (see "Runtime driver"). Exit 0, or 2 for an unreadable store or an
  invalid name.
- `quoin opencode start --profile NAME [--project-root PATH] [--dry-run]`
  opens the OpenCode terminal interface with the compiled profile.
  `--dry-run` validates and prints the command, directory and environment
  variable names without starting it. Exit 3 when a check refuses.
- `quoin opencode gate --task NAME --phase PHASE [--stage N] [--project-root PATH] [--write] [--explanation-file PATH] [--source-dir PATH]`
  evaluates one gated phase (`discover`, `architect`, `plan`, `implement`,
  `review`) with a fixed list of deterministic checks and prints one JSON
  line. No option approves, adopts or chooses evidence, and `--explanation-file`
  text (up to 64 KiB) is only carried in the audit file, never evaluated.
  Without `--write` it is read-only and takes no lock; with `--write` it takes
  the task lock, writes `gate-PHASE-DATE.md` into the stage folder (the task
  root for `discover` and `architect`), and records the verdict. A same-day
  file this command wrote is replaced; a file of that name written by another
  tool is never touched. Exit 0 passed, 7 refused by a check, 2 refused
  request or unreadable store, 3 task lock held, 8 audit file not written or
  not valid, or written but the verdict could not be recorded (the payload
  keeps `outcome` and the verdict and adds `artifact_error` or `record_error`,
  each with `code` and `message`; `outcome` is not a promise that the state
  was updated). Artifacts must pass `validate_artifact.py` strictly, so a plan or
  review written through a skill's plain-English fallback (no `## For human`
  section) or with extra headings is refused as `artifact-invalid` naming the
  invariant. A verdict is read by a strict rule. The document has one Verdict heading
  (`## Verdict` or `## Verdict: X`) and no other heading that names the
  verdict or spells a value in capitals. The section holds only the value
  (bare, bold, in a code span or in a `<verdict>` tag) and ends at a standard
  section heading or the end of the file. Any "Verdict:" line elsewhere starts
  with the same value. In an approving document, outside the Verdict section,
  the frontmatter and `## Dimension Verdicts`, no line starts with a
  non-approving value (after markup, container markers and an optional short
  label, including inside code spans), no table cell is one, and no heading
  contains "blocked", "revise" or "changes requested" in any case, even as
  plain English. Earlier review rounds are described in a sentence, not as a
  label line or a table of rounds. Other refusals: a line break or bidi
  control character, a frontmatter verdict that differs from the section, a
  value line outside the Verdict section that states another value, an HTML
  block before the Verdict heading, a run of non-ASCII letters or symbols that
  spells a "verdict"-length word (refused as a look-alike), and, in critic
  responses only, a heading that contains "revise". The approving-only scan
  does not read these shapes: a value after an emoji prefix, a checkbox or
  brackets, a non-approving value in a table cell followed by notes, a value
  after an intervening word in prose, a label of four or more words, and a
  non-verdict frontmatter key. A non-approving dimension row under an
  approving overall result passes by design. An unreadable or oversized file
  is reported as such, with its own recovery. An HTML element that a raw-text rule names
  (title, pre, script and the like) is named without angle brackets before the
  Verdict heading. Anything else is `verdict-unparseable`, and prose belongs in
  another section. The rule catches an honest writer's formatting mistakes; it
  does not try to defeat a writer who sets out to mislead. Recovery: edit the
  artifact, then run `quoin opencode adopt`, or re-run the phase.
  A single `--phase plan` run is refused as `critic-missing` until a critic
  has run: use `--phase thorough-plan` (the run command also accepts
  `thorough_plan`), or `adopt` after the critic. Any untracked, non-ignored
  file created after evidence was recorded (for example `__pycache__` or
  `.pytest_cache` from a test run) changes the source digest and makes the
  gate refuse. Repositories are found as immediate subdirectories only; edits
  inside a deeper untracked repository or a submodule are not reflected in the
  digest. Edits hidden from git itself (`--assume-unchanged`, `--skip-worktree`,
  `.git/info/exclude`) are not seen either; a repository's `core.fsmonitor` and
  untracked-cache settings are overridden for the digest. The audit file is
  named with the UTC date.
- `quoin opencode adopt --task NAME --phase PHASE [--stage N] [--project-root PATH]`
  is a human step that records evidence for a phase finished outside a
  recorded run (for example in the terminal interface), from the tree as it is
  now, and prints the `gate` command to run next. The gate never treats
  adopted evidence as run-verified: it reports `run-evidence-absent` and
  `boundary-unverified` as warnings, and still refuses any later change.
  Takes the task lock. Exit 0, 2 refused request, 3 task lock held.
- `quoin opencode handoff write --task NAME [--project-root PATH] [--source-dir PATH] [--profile NAME] [--decision TEXT] [--note TEXT]`
  builds the portable continuation record from the workflow state, the run
  records and the tree, and writes it to `continuation/NAME.json` in the
  project's workflow memory directory; agent text enters the record only
  through `--decision` and `--note` (repeatable, at most 20 per call, 2000
  characters each). Takes the task lock. Exit 0, 2 refused, 3 task lock held,
  8 record not written.
- `quoin opencode handoff show --task NAME [--project-root PATH] [--source-dir PATH] [--profile NAME]`
  validates the record and refuses a missing, invalid, older-format or
  finalized continuation (and, with `--profile`, a scope that is not covered by
  the recorded one), then prints the next steps, or candidate commands with
  what to check when the choice needs the operator. Reads only; takes no lock.
  Whether a native session can be resumed relies on the compatibility keys
  `continuation-flags` and `continuation-no-replay`.
- `quoin opencode handoff validate --task NAME [--project-root PATH] [--source-dir PATH]`
  checks the record file alone against its schema.

  Until a later release records workflow entries after runs, a phase finished
  by a headless `quoin run` is reported as run completed, even after later
  runs of other phases, and the advice is to adopt and gate it rather than run
  it again. When some run records cannot be read, a fresh run is offered only
  as a candidate to check first.
- `quoin doctor --runtime opencode --profile NAME` adds the profile checks
  described under "Runtime driver".

### File locations

- Profiles: `$XDG_CONFIG_HOME/quoin/opencode/profiles/NAME.json`.
- Project file: `.quoin/runtime.json` in the project root.
- Managed policy: the file named by `QUOIN_OPENCODE_MANAGED_POLICY`; there is
  no platform default location.
- Qualification records: `$XDG_CONFIG_HOME/quoin/opencode/qualifications/NAME.json`.
- Compiled pair: `$XDG_STATE_HOME/quoin/opencode/PROFILE/PROJECT_KEY/opencode.json`
  and `quoin-compile.json` next to it. The compiled files are never written
  inside the project or its git checkout.

`XDG_CONFIG_HOME` and `XDG_STATE_HOME` default to `.config` and
`.local/state` under the home directory. Files are written mode 0600 into
directories of mode 0700; an existing directory that other users can write
to is refused.

### Work profile status

The work profile is not yet supported: no corporate gateway has been
qualified, and every gateway value in `decisions.md` is still `not set`. The
personal OpenRouter profile is the only one `import-preview` produces.

### Qualification and the probe

`quoin opencode probe` sends a short handshake to the model's gateway. It
sends synthetic prompts only, but the requests are live and may be billed,
which is why it refuses to run without `--synthetic-only`. It supports the
chat-completions endpoint family only. The credential is resolved when the
probe runs and handed to the probe script in memory; it is never placed on a
command line, in a file or in output.

The previous qualification record is removed before any request is sent, and
every verdict, including could-not-run, writes a new record. A failed or
interrupted re-probe therefore leaves the model unqualified (`failed` or
missing), never still qualified. Records expire after 30 days or when the
endpoint, the model id or the pinned OpenCode version changes.

### Native schema subset

The vendored native-config schema covers exactly the keys the compiler emits.
It is stricter than OpenCode itself, keeps the upstream enums, and cites its
sources and blob hashes in its `$comment` fields. `compatibility.md` has the
per-fact rows the compiler relies on.

### Launcher contract

Compiling writes `quoin-compile.json` beside the native file. A launcher reads
these keys:

- `sidecar_format` — version of this file's layout.
- `digest` — digest of every input that shaped the native file.
- `native_sha256` — SHA-256 of the native file's exact bytes.
- `launchable` — false when unqualified models were accepted; refuse to launch.
- `unqualified_models` — the models accepted without a valid record.
- `pinned_version` — the OpenCode version the compilation targets.
- `profile` — the profile name.
- `project_key` — the stable key of the project directory.
- `classification` — the effective work or personal classification.
- `role_resolutions` — per role: model, provider, effort and origin.
- `auxiliary_resolutions` — the same for the title and compaction agents.
- `effort_diagnostics` — roles whose effort was left out, with the reason.
- `credential_env` — environment variable name to profile provider id.
- `native_provider` — profile provider id to native provider id.
- `launch_requirements` — what a launcher must do:
  - `config_path_env` — the variable that carries the compiled file's path.
  - `protected_keys` — the keys later config layers must not change: `$schema`, `model`, `small_model`, `agent`, `share`, `autoupdate`, `enabled_providers`, `provider`, `experimental.policies`.
  - `credential_env_required` — every `credential_env` name must be exported with a value.

A launcher must:

1. Refuse a compiled pair with `launchable: false`.
2. Rebuild in memory, or run `config compile --check` immediately before launch, and verify `native_sha256` rather than trusting the stored files.
3. Pass the compiled file through `OPENCODE_CONFIG`.
4. For every `credential_env` name, resolve that provider's `credential_ref` at launch and export a non-empty value under that name only; refuse on an empty value.
5. Remove `OPENROUTER_API_KEY` and other ambient provider credential variables from the child environment. The OpenRouter kind keeps OpenCode's built-in provider id and would otherwise fall back to an ambient or stored key.
6. Use an isolated OpenCode data directory so keys stored by `opencode auth` are not merged in.
7. Verify that the `protected_keys` were not overridden by later config layers: the project's `opencode.json(c)`, `.opencode` files, `OPENCODE_CONFIG_CONTENT`, and organization or managed config.
8. Never put a secret on a command line.

### Retry policy

The retry policy is a library only; the launcher consumes it. Connect
errors, timeouts, 429 and 5xx responses are transient; authentication,
policy, configuration and other responses are not. `Retry-After` is honoured
(seconds or an HTTP date) without jitter and capped at 30 seconds, otherwise
delays use exponential backoff with full jitter. The attempt cap comes from
`max_transient_retries` and the time budget from `max_run_seconds`. A request
is never repeated while a state-changing tool is running. Unknown token or
cost usage stays unknown (`None`) when totalled, and a retry never moves work
to another profile.

## Runtime driver

`quoin run --runtime opencode --profile NAME --phase PHASE TASK` runs one
workflow phase headlessly on OpenCode and prints a JSON summary. A phase is a
single supported command such as `plan`. The bare whole-task form (no `--phase`
and no `--workflow`) is still refused; a whole task runs through the
coordinator described under "Workflow coordinator". The driver is verified
offline against a fake executable only: no live OpenCode run has been verified,
and the work profile is not made supported by anything here.

### Phase runs

Flags: `--profile` (required), `--phase` (required), `--stage N` (a stage of a
multi-stage task), `--new-run` (start the phase over instead of resuming the
recorded run), `--max-relaunch N`, `--halt-on-abort` and `--budget`. A run
holds the task lock while it works; the lock records `runtime: opencode`, so
Claude auto-resume never continues such a task and treats the lock as owned by
another runtime. A phase run never writes the whole-task halt sentinel.

Run states: `prepared`, `running`, `completed`, `failed`, `awaiting_approval`,
`cancelled` and `interrupted`. Each attempt is classified by one precedence
order, first match wins: a cancel, then an approval stop, then a native error
or a failed delegation or agent fallback (failed), then a timeout, driver
error, lost driver, early end of stream, lost session, signal, missing or
non-zero exit code or no proof the last step finished (interrupted), and only
then completion. The summary reports a completed run whose evidence is not
`full` as `COMPLETED_UNVERIFIED`.

Evidence is `full` when every tool of the final step settled and nothing was
delegated away, and `partial` otherwise. Delegated work (background tasks and
subagent tool calls whose outcome the event stream cannot show) keeps the run
`partial`, which is why it ends as `COMPLETED_UNVERIFIED` rather than
`COMPLETED`. Approval requests cannot be answered in a headless run: asks are
rejected automatically and the run ends awaiting approval. A policy `deny`
rule is different: the model sees the denial and may continue, so it is
recoverable.

Summary keys: `runtime`, `task`, `stage`, `phase`, `profile`, `outcome`,
`exit_code`, `run_state`, `evidence`, `reason`, `resume_blocked`, `run_id`,
`sidecar`, `attempts`, `artifact_coverage` (`full` unless an input snapshot was
cut short, then `partial`), `refusal`, `resume_hint`, `superseded_run` (the
older run a `--new-run` replaced, when it still read running) and
`workflow_validated` (always false: the driver does not check workflow
artifacts). Exit codes: 0 completed, 2 failed, aborted or internal error, 3
refused, 4 awaiting approval, 5 interrupted, 6 completed but unverified, and
130 or 143 when stopped by SIGINT or SIGTERM.

A phase run that can no longer continue (it completed, failed, awaits
approval, was cancelled, is blocked from resuming, or was replaced by a later
run) writes one cost row to the task's cost ledger and stores its telemetry in
its run record. A run of a gated phase (discover, architect, plan, thorough
plan, critic, implement or review) also records a workflow entry for the gate,
listing the critic responses or the review the run itself produced; a phase
finished before runs recorded entries has none and is reported as work to adopt
and gate. A run that rewrote earlier lines of the cost ledger ends `FAILED`
with reason `boundary-violation` (exit code 2, and the hint offers `--new-run`).
The ledger check fails closed: when the ledger existed before the run and
cannot be read afterwards (unreadable, replaced by a link or special file, or
over the size limit), its earlier lines cannot be shown to be unchanged, so the
run is treated as having rewritten them and ends `FAILED` with the same
`boundary-violation` reason. A ledger that cannot be read before the run is
recorded as unavailable and never counts as a rewrite.
The summary keys and the single line on standard output do not change; cost
rows and telemetry never appear there.

Run store: under the project's workflow memory directory, in `runtime/opencode/`:
`RUN_ID.jsonl` (the event sidecar), `RUN_ID.run.json` (the run record),
`RUN_ID.checkpoint.json` (the resume checkpoint) and `task-TASK.json` (a
pointer from a task to its latest run). A run id looks like
`oc-YYYYMMDDTHHMMSSZ-xxxxxxxx`. The directory is private to the user.

Cancellation stops the whole process tree: the child and its descendants are
terminated, then killed after a grace period. A second Ctrl-C during that
grace does not force an exit; `kill -KILL` on the `quoin run` process is the
escape, and the dead run's lock is reclaimed by the next run. Phase runs
write `relaunches: 0` to the supervisor result file so the Claude auto-resume
counter is never charged.

### Resume, blocks and limits

A new invocation for the same task, phase, stage and profile resumes an
interrupted run. Resume is blocked, and the run must be started over with
`--new-run`, when the driver cannot prove it is safe:

- `effect-uncertain`: a step may have changed files that the driver cannot account for (the run stopped inside a step, the driver was lost, or no session was recorded).
- `session-lost`: OpenCode reported that the recorded session no longer exists.
- `session-invalid`: the recorded session id is not a usable OpenCode session id.
- `sidecar-behind-checkpoint`: the event sidecar is shorter than the checkpoint says.
- `checkpoint-invalid`: the checkpoint cannot be read or fails its checks.

A run that was already closed (its cost row and telemetry are written) is never
resumed. Re-running the same request against such an interrupted run ends
`INTERRUPTED` with reason `run-closed` when the record names no block of its
own; the hint then ends with `--new-run`, so restarting is always an explicit
choice.

The `resume_hint` field is a command line; it ends with `--new-run` exactly
when the next invocation would not resume. When another `quoin run` still holds
the task lock, a new invocation is refused with `lock-held`. A record that
still reads running while its driver is gone is refused with `run-in-progress`
and names the child pid and process group: check them, stop the child if it is
alive, then re-run with `--new-run` (holding the lock proves the old driver has
exited, so `--new-run` always proceeds and reports the replaced run as
`superseded_run`).

Limits: `max_run_seconds` is counted per `quoin run` invocation, not across
resumes. The no-progress guard can stop a run whose transient failures emit no
native events before `max_transient_retries` is reached.

### Cost rows and run telemetry

**Single writer.** The driver and the coordinator are the only writers of the
cost ledger for a headless run; an agent that appends to or rewrites it ends the
run `FAILED` with `boundary-violation`. A ledger that exists but cannot be read
is treated as changed (fail closed), never as empty.

**Row shape.** The row is the shared eight-column ledger row, built with the
portable cost-event formatter and parsed back before it is written. Column
one is the run id, so one run id is one row. Column two is the UTC date the
last attempt ended. Column three is the ledger phase: discover, architect,
plan, `thorough-plan`, critic, implement, review, gate, `end-of-task`,
checkpoint, `ad-hoc` (for `continue_work`, which has no ledger phase of its
own) and `run-orchestrator` (the coordinator phase, not runnable yet). Column
four is the effective model, column five is `task`, and column six is the
note: `runtime=opencode command=quoin-PHASE outcome=ENDED attempts=N
scope=parent-session-only`, where PHASE spells the command name with hyphens,
ENDED is the run's end in lower case with underscores, and N counts every
non-staged attempt of the run across invocations. Column seven is the
fallback count (always 0). Column eight is the attribution: `usd=X;tok=N;src=opencode_stream`
when the dollar cost is known, `tok=N;src=unresolved` when only tokens are
known, and `src=unresolved` when nothing is. The `opencode_stream` tag is
used only when a dollar amount is known and is defined here, not in the
shared core. Every field written to the ledger has pipes, control characters
and line breaks replaced and its length capped, so a row is always one line
with eight columns.

**When a row is written.** Once, when the run can no longer continue: a
completed, failed, aborted, cancelled or approval-stopped run, a run blocked
from resuming (including a checkpoint that cannot be read), or a run that a
later run took the place of. A resumable interruption writes nothing yet; a
run resumed across invocations ends with one row holding the usage of every
attempt. A run that moves the task's pointer (a different phase, stage or
profile, or `--new-run`) closes an earlier run that was still open: that run
is costed with `outcome=superseded`, its record names the run that replaced
it and reads as closed, `quoin opencode status` shows it as superseded with
nothing to resume, and a run that never spawned a child gets telemetry but no
row. An `end_of_task` run writes its row just before the child starts, with
`outcome=launched` and unknown usage, because the run itself may move the task
folder away; no row can be added after that. When the task folder does not
exist no ledger is created and no workflow entry is recorded, but the
telemetry is still stored.

**Unknown is never zero.** A dollar amount is written only when the compiled
configuration prices the effective model and the stream reported a cost that
is not 0 while tokens were used (a model without prices reports 0 for every
step, so a reported 0 cannot tell a free model from an unpriced one). The
generated configuration does not price models yet, so rows carry
`tok=N;src=unresolved` and the telemetry says `cost-unpriced-model`. A value
that cannot be known is omitted from the row (and null with a reason in the
telemetry); it is never written as 0 and never estimated.

**Scope.** Usage is the latest revision of each step-finish part of the run's
own session, counted once per part id. Usage of subagent sessions is not in
the parent's event stream and the parent's cost does not include it, so the
row covers the parent session only (`scope=parent-session-only`) and child
usage is reported as unavailable. The effective reasoning effort is not
visible in the stream and is reported as unknown beside the configured one.

**Telemetry.** The run record gains a `telemetry` block: the schema number,
`final`, how the run ended, the provider (shared and native ids), the
effective model, the effort (requested, configured, variant, and the
effective value as unknown), elapsed seconds per attempt with a total and a
wall-clock figure, retry counts, the native session and step-finish part ids,
usage and cost with a reason for every unknown field, the provenance of the
numbers, the list of things that are unavailable, the ledger result (mark,
whether the row was written, found or skipped and why, the prefix check, the
lines other writers appended), and the workflow-entry result. Nothing copies
event text, standard error or the command line. A hook failure is stored as
`hook_error` and never changes the outcome.

**Earlier bytes and appended lines.** Before a phase runs the command records
the size and digest of the ledger. Lines that other writers appended during
the run are kept and listed in the run's telemetry and workflow entry (the gate
warns about them). If the earlier bytes changed, or the ledger shrank or
disappeared, the run ends `FAILED` with `boundary-violation`; the row is still
appended once. The violation is sticky: a workflow entry that lists a run which
rewrote earlier lines stays marked, so a later clean critic run cannot clear it
and the gate refuses until the plan is run again as a fresh plan run.

**Workflow entries from runs.** A plan or thorough-plan run starts a fresh
entry whose critic responses are the files that run produced; a critic run
extends the live entry written by a run, adding itself and its response. The
gate trusts only the responses and the review recorded this way, never older
files left on disk, for an entry a run wrote; adopted entries keep the earlier
behaviour. A critic run after an adopted plan starts its own entry, which has
no plan run listed, so the gate refuses it: adopt the plan again after the
critique, or run the plan as a phase run. The critic round cap counts only the
responses recorded since the last plan run, so it does not bound a plan and
critic sequence driven by single-phase runs. A run's produced files come from
its own before and after hashes of the task folder; when those hashes are
incomplete no file is recorded and the entry says why.

**Residual.** A run left open with no later run for the task is costed only
when it ends or when a later run for the task replaces it; until then it has no
row, and `quoin opencode status` shows it as open. A crash between appending the
row and writing the run record loses only the closed marking and the telemetry
of that run: the row is correct and is never written twice.

### Status and the terminal interface

`quoin opencode status` is strictly read-only: it never repairs a torn
sidecar, reconciles a record, creates the store or sends a signal. It reports
the state (`running (driver lost)` when the recording process has exited), the
child, the last event, a torn sidecar tail, older runs still reading running,
and the task lock. Liveness comes from one process-table snapshot; when the
table cannot be read, liveness is reported as unknown (`null`) rather than
guessed. Text output names the remedy for a dead lock, a lost driver or a
blocked resume. A run that a later run replaced is shown as superseded, with
nothing to resume.

`quoin opencode start` runs the same checks as a phase run (binary, pinned
version, configuration, gateway qualification, installed files unchanged,
configuration layers) and then replaces the `quoin` process with the OpenCode
interface, started in the project directory with the compiled configuration
and an isolated data directory. Nothing is captured: there is no event
sidecar, no run record and no task lock, and the TUI keeps the terminal and
receives Ctrl-C directly. The data directory is per profile and shared by
every run and TUI session of that profile, so sessions created interactively
are stored beside headless ones and are visible to the same profile.

### Doctor categories and profile checks

Every doctor finding carries a `category` in the JSON report, using the same
slugs as run refusals so a finding and a refusal about one problem agree:

| Category | Label | Example finding ids |
|---|---|---|
| `missing-binary` | missing binary | `opencode-binary-absent` |
| `unsupported-version` | unsupported version | `opencode-version`, `opencode-version-unknown` |
| `invalid-configuration` | invalid configuration | `config-unreadable`, `runtime-config-invalid`, `runtime-compile-blocked` |
| `unqualified-gateway` | unqualified gateway | `runtime-gateway-unqualified` |
| `policy-denial` | policy denial | `runtime-config-policy-denied`, `runtime-plugin-directory-present` |
| `missing-optional-integration` | missing optional integration | `quoin-not-on-path`, `census-unverified` |
| `workflow-validation` | workflow validation failure | `owned-modified`, `runtime-orphan-run`, `runtime-stale-lock` |

Host checks now also report process-group support, an unusable run store,
orphaned runs, a stale OpenCode task lock, a phase command whose agent is not
primary, and plugin directories. With `--profile NAME` (not with `--smoke`)
the doctor also evaluates that profile's configuration without resolving any
credential and reports whether it can launch. A role blocked by a provider
exclusion is reported as compile-blocked, exactly as the run refusal reports
it.

A plugin script in any OpenCode plugin directory (including the global one)
refuses launches, because plugin code can hook permission handling. Move the
plugin files out of those directories for the duration of a run, or run with a
separate `XDG_CONFIG_HOME`. The doctor does not scan Quoin-named skills placed
outside the installed project folders.

## Workflow coordinator

`quoin run --runtime opencode --profile NAME --workflow TASK` walks a whole
task headlessly: every phase is a fresh driver run with its own command
(`quoin-discover`, `quoin-architect`, `quoin-plan`, `quoin-critic`,
`quoin-implement`, `quoin-review`), followed by the deterministic gate for that
phase. The `/quoin-run` command in the terminal interface only names the next
step; the coordinator is the headless route. It never runs `end_of_task`:
finalizing a task stays an explicit human action.

Flags (`--workflow` itself is refused without `--runtime opencode`, and every
other flag here is refused without `--workflow`): `--workflow` starts a task,
`--continue` continues one from its continuation record, `--no-pause` skips the pauses described below,
`--through PHASE` stops after that phase passes its gate, `--from-discover`
runs `discover` even when discovery files exist, `--max-critic-rounds N`
(default 2, at most 5), `--test-command CMD`, `--test-include PATH` and
`--test-timeout SECONDS` (the tests the coordinator runs after each implement),
`--rerun-from {plan,implement,review}` and `--adopt PHASE` (both need
`--continue`). `--max-relaunch`, `--halt-on-abort` and `--budget` apply to
every run.

Sequence: `discover` (only with `--from-discover` or when the discovery files
are missing), `architect`, then for each stage listed in the architecture
`plan`, `implement` and `review`; a task without a stage list has one
task-root stage. The stage list is read again after `architect` passes. Each
phase runs only after the previous gate passed.

Critic loop: a plan item is the plan run followed by an isolated critic run;
while the critic answers `REVISE` the plan is run again, up to the round cap,
which the gate reads from the same stored setting. The second and later plan
rounds, and an `implement` rerun after a refused review, receive the finding
they must address through a context suffix on the command argument, for
example `stage 1 of demo (context: PATH-TO-THE-CRITIC-RESPONSE) (non-interactive run)`, where the path is project-relative.
The suffix always comes before the non-interactive marker. Critic and review
sessions are separate contexts from planning and implementing; they use the
same model unless the profile says otherwise, so this is a clean-context
check, not a second opinion from a different model.

Pause rule: without `--no-pause` the coordinator stops with outcome
`PAUSED_AT_GATE` (exit 0) after each plan gate and each review gate except
the last item, and `--continue` goes on. A re-gated plan that a record seeded
does not pause.

Tests: the operator configures the test command with `--test-command`; the
coordinator runs it itself after every implement and again for each review,
under the driver's own state directory, so what an agent runs inside a run and
what the coordinator runs read and write the same result. The coordinator
runs implementer-written code with the user's privileges; `--test-command` is
the operator's opt-in.

Outcomes and exit codes: `COMPLETED` 0, `PAUSED_AT_GATE` 0, `FAILED` 2,
`REFUSED` 3, `AWAITING_APPROVAL` 4, `INTERRUPTED` 5, `COMPLETED_UNVERIFIED` 6,
`GATE_REFUSED` 7 (a gate check refused; the reasons are in the summary),
`GATE_ARTIFACT_FAILED` 8 (the gate audit file could not be written or
recorded) and `CANCELLED` 143 (130 for an interrupt). A refused or failed
phase prints the command that continues it. A closed run that stored a
boundary violation is reported again as `FAILED` with `boundary-violation`
and is not run again until `--rerun-from` restarts it.

Recovery: `--continue` re-reads the continuation record, refuses a record that
disagrees with the workflow state, and resumes an interrupted run of a plain
phase under the same run id; a critic or review run that was interrupted
starts fresh and the abandoned run is closed as superseded. A cancelled run is
final: `--continue --rerun-from PHASE` starts that phase of the current stage
over (and every later phase of the stage); with every item passed it reruns
the last stage. `--continue --adopt PHASE` records a phase finished outside a
recorded run, as `quoin opencode adopt` does, and the gate still treats it as
unverified. A headless run that stops for an approval ends `AWAITING_APPROVAL`
and prints the `adopt` command to use after finishing the phase in the
terminal interface.

## Deterministic gate

After every phase the coordinator runs the same checks as
`quoin opencode gate --write`, so the verdict, the audit file and the recorded
entry are identical to those of a gate run by hand. The checks include the
artifact format, the critic loop (`critic-not-converged` while the last
response asks for a revision), the review verdict, the boundary result of each
run, the tests (`tests-failed`, `tests-not-run`, `tests-settings-changed` and
the other `tests-` reasons) and the cost ledger. A critic response or review
file that is newer than the one recorded on the entry is refused as
`finding-superseded`; this also applies to entries recorded by a single-phase
run, so run the phase again or record the newer file before gating. A changed
test pin refuses implement before anything is spawned and shows up at the gate
as `tests-settings-changed`.

## Continuation record

`quoin opencode handoff write` and the coordinator build the portable
continuation record from the workflow state, the run records and the tree. The
coordinator keeps it level with state: while a run of the current item is open
the artifact hashes in the record are the ones state holds, so an agent's edit
in mid-run never puts a hash in the record that state has not seen, and a
record that lags state by a recorded step is accepted and rewritten. A record
written by another runtime seeds continuation entries and is re-gated, never
trusted: nothing but the record, the workflow state and directory listings is
read, so other runtimes' transcripts and session files are not opened, and a
new native session is started. Refusals: `continuation-missing`,
`continuation-invalid`, `continuation-legacy-format` (only older-format files
exist), `task-finalized`, `continuation-state-mismatch`,
`continuation-artifact-changed`, `profile-mismatch`, `classification-mismatch`
and `policy-widened`.

## Role boundaries

Every phase run is checked against what its role may change. The coordinator
lists the paths a role can legitimately touch before and after the run and
compares the two against the rules below. The roles, in plain words:

| Role (phases) | May change | Source repositories |
|---|---|---|
| Investigator (`discover`) | any file in the task folder except gate audit files; the discovery map and the three discovery files | unchanged |
| Architect, planner (`architect`, `plan`, `thorough_plan`) | any file in the task folder except gate audit files | unchanged |
| Implementer (`implement`) | any file in the task folder except gate audit files | may change |
| Critic and reviewer in the real tree (`critic`, `review` outside a snapshot) | only a new numbered critic response or review file in the task or stage folder | unchanged |
| Critic and reviewer in a snapshot | nothing in the project; the finding is harvested (see Snapshot runs) | unchanged |
| Gate | gate audit files in the task or stage folder and the task's own workflow-state record | unchanged |
| Checkpoint | the task's continuation record and its previous copy | unchanged |
| Continue work | nothing | unchanged |
| End of task | moves the task folder into `finalized/`, plus the lessons-learned file | may change |

Session scratch files (`memory/sessions/`, `memory/daily/insights-*.md`,
`cache/`) are writable by any role and are never judged. The cost ledger, gate
audit files, continuation records and the run store belong to the coordinator;
no role may write the ledger, and the run's own store files are excluded from
the comparison by path.

A check ends in one of three results:

- `ok`: nothing outside the role's allowance changed and the source state could
  be compared.
- `violation`: a path outside the allowance changed, a symlink appeared in the
  listed scope, or a repository changed under a role that must leave source
  alone. The run ends `FAILED` with reason `boundary-violation`, and the gate
  fails the entry.
- `unverified`: the check could not be completed. Reasons include a truncated
  listing, an unreadable install record, source state that cannot be compared,
  a run resumed after an earlier invocation already ran (the window began
  before this listing), and a change that may belong to another writer. The
  entry stores no boundary result. The gate warns for entries recorded by a
  single-phase run (`boundary-unverified`) and refuses entries recorded by the
  coordinator, so an unverifiable check never approves a coordinator phase.

**What is listed.** The listing is deliberately narrow so that files other
tools legitimately write are never judged: the artifact root except `memory/`,
`cache/` and the contents of `finalized/` (only its immediate children are
listed); inside `memory/`, only `continuation/`, `runtime/opencode/` and
`lessons-learned.md`; inside `.opencode/` and `.quoin/`, only the paths the
install record owns plus the two install records. Source state (head, dirty
flag and a digest of the changes) is compared for every repository, with
the artifact root, `.opencode`, `.quoin` and `.workspaces` left out and
nested repositories attributed to their own entry. Symlinks are never followed.
Files are hashed up to fixed caps; past a cap the result is `unverified`, not a
failure.

OpenCode itself writes into every configuration directory it scans at launch:
a `.gitignore`, a `package.json`, a lockfile and `node_modules/`, from the
background install of its plugin package (compatibility row for
`ensureGitignore`, in "Configuration sources and precedence"). Those names are
never listed, so the runtime's own launch-time writes are not mistaken for a
role writing outside its allowance.

**Concurrent tasks.** Another task's run holds its own task lock. A change
inside another task's folder, run store or pointer is downgraded from
`violation` to `unverified` with reason `concurrent-task-run` when that task's
lock named a live process at either listing. Without a live lock, a change
confined to another task's folder is `unverified` with reason
`concurrent-writer-unlocked` for single-phase runs, because other sessions in
the same project routinely edit other task folders without taking the OpenCode
lock; the coordinator's snapshot runs keep it a violation. Changes to the
install-owned paths, to source state, to the memory scope and to the run's own
task are never downgraded. In the gate these show up as the `boundary-unverified`
warning on the entry, with the reason stored beside the result.

**Permission inheritance.** At generation time the adapter also checks that a
role which delegates to a child role never lets the child do more than the
parent. Rules are compared as effective actions under last-match-wins: the
child's action for each of its patterns must not exceed the parent's action for
that pattern, lowered by any later parent pattern that may overlap it, and
built-in defaults count as `allow`. The one exemption is a read-only or
artifact-confined helper script. At run time OpenCode also re-applies the
parent's deny rules to a subagent (compatibility rows under "Commands, agents,
delegation and permissions"), so the generation check is a second line.

## Snapshot runs

A headless critic or review run starts in a read-only copy of the project, not
in the project. The copy lives outside the project, under the adapter state
directory (`snapshots/PROJECT_KEY/TAG`, mode 0700), and is its own git
repository, so OpenCode discovery and project identity stop at its root
(compatibility keys `discovery-stops-at-git-root` and `project-identity`);
parent directories are never read, and a relative path inside it cannot name a
project file. It holds the tracked and untracked-not-ignored source of every
repository, the installed OpenCode files, the task folder and the discovery
files. It never holds a `.git` entry, a symlink or a `.env*` file. Reviewers
also get a `review-context/` directory with the diff of the stage against its
base tree (HEAD when no base tree was recorded; the context file says which).
The copy and its `.opencode/` directory are writable only where OpenCode needs
to write at launch; owned files inside stay read-only. The copy is removed
after the run.

Only one file leaves the copy: the finding the run wrote to its outbox.
Harvest requires exactly one match, validates it, renumbers it to the next free
critic-response or review number in the real task folder, and creates it
exclusively. The model's own number is ignored. A run with a boundary
violation harvests nothing.

Limits:

- Snapshot runs apply to headless critic and review only. In the TUI the same
  roles keep their permission-map guard rails and are not isolated.
- A snapshot run always starts fresh. An interrupted snapshot run is superseded
  and costed when the next run starts, never resumed, so it cannot continue in
  a different snapshot or on the real root.
- A copy over the size or file-count cap is refused (`snapshot-too-large`).
- A dirty tree whose source digest goes over its budget (64 MiB of changed
  content, 5000 untracked files) cannot be digested, so every snapshot boundary
  is `unverified` and nothing is harvested until the tree is committed or
  cleaned.

## Test runs

`quoin opencode test-run --task TASK [--stage N]` runs the task's configured
test command against a throwaway copy of the working tree and prints one JSON
line (exit 0 `PASSED`, 1 `FAILED`, 2 refused). The command and the
include directories (git-ignored subdirectories of the project) are fixed in the task's workflow state by a human-side
configuration call; the subcommand has no option that changes them, so a model
cannot choose what runs.

For each repository the command runs in a detached git worktree at the current
HEAD with the working-tree changes overlaid (tracked edits and untracked files,
never `.env*` files). Include directories are linked in, the command runs
there, the worktrees are removed, and the real repositories are then checked:
if HEAD, the source state or the refs changed, the run fails and its result is
not used. 
The result is written outside the project, under the adapter state directory,
stamped with the id and attempt number of the phase run that started it, so a
file written by an agent inside the project cannot stand in for it. When the
implement run finishes, the evidence hook copies the result into the entry only
if it passed and belongs to that run's last attempt; a relaunched implement run
must therefore run its tests again. Test-run does not take the task lock, since
it runs inside a phase run that holds it; a busy file stops two test runs of the
same task from overlapping.

The command runs with a scrubbed environment. Under the launcher it keeps only
the basic variables the launcher lets through (path, home, locale and similar),
which drops the launcher's own variables, provider credentials and proxy
settings. Outside the launcher it removes only the launcher's own names. A
command that needs anything else, such as `PYTHONPATH`, sets it in its own
argv, for example `env PYTHONPATH=src python -m pytest`, and a command that
needs a proxy must set it the same way. Review entries carry no test result
yet.

What a `PASSED` result means: the configured command exited 0 against a copy of
the working tree. That tree includes code the implementer wrote, such as test
files, a `conftest.py` or source modules, and `test-run` runs it with the user's
privileges without a prompt, even though headless shell commands are otherwise
asked about and auto-rejected. Code run this way can write outside the project,
force a zero exit or start a process that outlives the run, so a result attests
only to what agent-controlled code reported. A deadline bounds the run: output
is read against it, so a child that left the process group and holds the
output open cannot extend it. The digest of the configured command, include
list and timeout is stored under the adapter state directory when the settings
are written, and a run refuses settings that no longer match it, so editing the
workflow state inside the project does not change what runs.

## Non-interactive runs

A headless run cannot answer questions (asks are rejected automatically,
compatibility key `auto-reject-asks`). The coordinator therefore appends the
literal marker ` (non-interactive run)` to the command argument. A phase that
sees the marker takes the task name from the word before it, asks nothing, and
stops with a statement of what is missing if it cannot continue. A single-phase
`quoin run --runtime opencode ... --non-interactive` sets the same marker for
that run; without the flag the argument is passed as typed. Headless implement
runs additionally run no shell command other than the allowed Quoin helpers,
never commit and skip the branch checks; the launch checks the branch.

`quoin opencode gate --write` and `quoin opencode handoff write` take the task
lock, and `quoin run` holds that lock for the whole run. A gate or checkpoint
phase run headless therefore gets a `lock-held` outcome (exit 3) and writes
nothing; the verdict or handoff has to be written from the TUI, where no lock is
held, or after the run. The overlay text for those phases says so.

## Running the probe

```
python3 quoin/adapters/opencode/probe_gateway.py \
  --base-url https://gateway.example.invalid/v1 \
  --model example-model \
  --credential-env EXAMPLE_API_KEY \
  --provider example \
  --output probe-record.json
```

The credential is read from the environment variable **named** by
`--credential-env` — never pass a credential value on the command line. The
variable's value is used once, held in memory only for the duration of the
run, and never printed; only the variable's name ever appears in output.

Other flags:

- `--ca-file` — a custom CA bundle, for gateways behind a corporate proxy
  with a private certificate authority. Certificate verification is never
  disabled by this tool.
- `--use-env-proxy` — honour `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY` from the
  environment. Off by default, so a stray proxy variable in your shell can't
  silently redirect a probe run.
- `--runtime-version` — the OpenCode version this run is meant to qualify;
  recorded in the output but does not change probe behaviour.
- `--declared-context-limit` / `--declared-output-limit` — record a limit you
  already know from the gateway's documentation. These are recorded as
  declared, not observed — the probe does not verify them.
- `--timeout` — per-request timeout in seconds (default 30). The streaming
  phase has a total budget of `--timeout` seconds; header and non-stream body
  reads carry a per-read socket timeout instead, so a connection that goes
  silent mid-response is never waited on indefinitely.
- `--active-error-checks` — optional, comma-separated (`invalid-token`,
  `context-overflow`). See the caution below before enabling these.

The probe never follows redirects, never retries a failed request, and never
falls back to a different endpoint or model.

**Caution:** `--base-url` must not carry a credential or tenant token in its
path (userinfo, query strings and URL fragments are already dropped or
rejected). The endpoint path is recorded in the capability record exactly as
given, so anything sensitive placed there ends up on disk.

## Exit codes

| Exit | Meaning |
|---|---|
| 0 | qualified — the endpoint supports the protocol this tool checks |
| 1 | not qualified — the endpoint answered, but showed a protocol incompatibility |
| 2 | could not run — the check was inconclusive |

Exit 2 covers every cause that doesn't tell you anything about protocol
support: bad configuration, connection failures, TLS problems, timeouts,
redirects, invalid or forbidden credentials, rate limiting, server errors,
and an endpoint or model that could not be found. None of those disprove
tool-calling support, so none of them are reported as "not qualified" — a
maintainer deciding whether to invest in an integration needs to be able to
tell "this gateway doesn't support tools" apart from "I couldn't even reach
it".

On exit 2, the diagnostic goes to stderr; stdout carries at most the
one-word verdict `could_not_run` (nothing at all on a configuration error or
an unwritable `--output`). A script driving this tool should key off the
exit code or the written record's verdict status, not stdout content.

## Live error provocations (`--active-error-checks`)

By default the probe only classifies failures it happens to see while
running its normal steps. `--active-error-checks` opts into two live
provocations instead:

- `invalid-token` sends one request with a deliberately wrong credential.
  On a gateway with lockout or anomaly-detection policies, this could
  trigger a temporary block on the real credential.
- `context-overflow` sends an oversized prompt to force a context-length
  error. This costs real tokens against your quota.

Both are off by default for these reasons. Neither check can change the
overall verdict — that depends only on the first three steps.

## Capability record

`--output` writes a JSON record (schema `quoin.opencode.capability-record`)
describing the endpoint, the model, which steps passed or failed, and a
status for nine capability fields (text generation, tool calls, streamed
tool arguments, usage reporting, structured output, context/output limits,
reasoning parameters, parallel tool calls). Several fields stay `unknown` in
this version because the probe deliberately avoids sending optional
parameters that some gateways reject outright — sending them would turn an
untested feature into a false failure. The record is written whenever the
output path is writable, on every exit code, and is redacted before being
written to disk.

## Status documents

- `compatibility.md` — which upstream OpenCode release this adapter is
  qualified against, and the per-claim verification status of that release's
  documented behavior.
- `decisions.md` — configuration choices that only a maintainer can make,
  plus the empty template a real probe run's record gets pasted into.

## Support classification

`feature-manifest.json` classifies every Quoin skill into one of three
statuses:

- `supported` — an OpenCode command and skill are generated for it; the
  generated pair is statically valid for the pinned release and
  offline-smoked, but not yet qualified against a live OpenCode run.
- `documentation-only` — the skill has a portable contract but no OpenCode
  asset is generated for it yet; it is listed as not yet available.
- `unsupported` — the skill depends on Claude-only mechanics that have no
  OpenCode equivalent.

`live_runtime_evidence` is `false` for every row today. The rows for
`architect`, `checkpoint`, `continue_work`, `critic`, `discover`, `end_of_task`,
`gate`, `implement`, `plan`, `review`, `thorough_plan` and `run` list `evidence`:
repo-relative test files that the manifest check opens and that must name the
row's command (`--workflow` for `run`). They say the row is fixture-verified;
a later change flips `live_runtime_evidence` to `true` once a row has been
exercised against a real OpenCode run.

Each row also names a target milestone, in plain terms:

- `adapter-foundation` — the generator, install, doctor, and runtime
  configuration this adapter ships.
- `workflow-execution` — the runtime driver and live end-to-end workflow
  runs.
- `work-context` — profile isolation and read-only work integrations.
- `controlled-writes` — approved external writes.
- `release-hardening` — remaining catalog coverage, benchmarks, and release
  qualification.
- `none` — no OpenCode support is planned for this skill.

Naming rule: an OpenCode-facing name is `quoin-` plus the skill's id, with
every underscore turned into a hyphen. Names are checked for uniqueness
per namespace (commands, skills, agents), since OpenCode keeps those as
separate maps. The canonical id — the one used everywhere else in Quoin —
always stays in the manifest row, never in the generated name itself.

Run the drift check with (from a repo checkout, `quoin` is not installed, so
`src` must be on `PYTHONPATH`):

```
PYTHONPATH=src python3 -m quoin.opencode_adapter check-manifest --source-dir quoin
```

## Using the fake server

```python
from fake_openai_server import FakeProviderServer

with FakeProviderServer() as server:
    print(server.base_url)   # http://127.0.0.1:<port>/v1
    ...
```

It can also run standalone for manual experimentation:

```
python3 quoin/adapters/opencode/fake_openai_server.py --scenario default_ok
```

Scenario selection, in priority order: the `X-Fake-Scenario` request header,
then the request's `model` field (when it names a known scenario), then the
server's configured default. `GET /v1/models` lists every scenario name, so
a model picker can drive the server through its model ID alone.

## Adding a scenario

Scenarios live entirely in `fixtures/scenarios.json` — no server code needs
to change. Each scenario is a list of turns; the server picks a turn by
matching the request's depth (how many assistant turns have already
happened) and shape (whether tools were sent, whether streaming was
requested) against each turn's optional `depth`/`match` fields, falling back
to the deepest turn that declares no shape restriction. A turn is either a
plain JSON body, a non-stream chat-completion `message`, or a list of SSE
`chunks`. Two template placeholders, `{{last_tool_content}}` and
`{{last_tool_call_id}}`, can appear in a turn at depth 1 or deeper to echo
back the previous tool result.
