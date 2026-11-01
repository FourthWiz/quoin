# Codex Skill Adapter Docs

These files are Codex facing docs generated/scaffolded from Quoin's portable
skill contracts under `quoin/core/skills/` and metadata in
`quoin/core/workflow/skills.json`.

They are repo-local adapter docs only. They do not define Codex command files,
global install paths, approval behavior, sandbox behavior, or model-dispatch
mechanics.

| Skill | Phase | Effort | User-facing |
|-------|-------|--------|-------------|
| [`architect`](architect/README.md) | architecture | max | yes |
| [`capture_insight`](capture_insight/README.md) | memory | low | yes |
| [`checkpoint`](checkpoint/README.md) | checkpoint | medium | yes |
| [`cleanup`](cleanup/README.md) | cleanup | low | yes |
| [`continue_work`](continue_work/README.md) | continue-work | medium | yes |
| [`cost_snapshot`](cost_snapshot/README.md) | cost | low | yes |
| [`critic`](critic/README.md) | critic | high | yes |
| [`discover`](discover/README.md) | discovery | high | yes |
| [`end_of_day`](end_of_day/README.md) | session-lifecycle | medium | yes |
| [`end_of_task`](end_of_task/README.md) | task-finalization | medium | yes |
| [`expand`](expand/README.md) | utility | medium | yes |
| [`gate`](gate/README.md) | gate | medium | yes |
| [`implement`](implement/README.md) | implementation | medium | yes |
| [`init_workflow`](init_workflow/README.md) | project-bootstrap | high | yes |
| [`next_steps`](next_steps/README.md) | next-steps | low | yes |
| [`plan`](plan/README.md) | planning | high | yes |
| [`pr`](pr/README.md) | pr | medium | yes |
| [`review`](review/README.md) | review | high | yes |
| [`revise`](revise/README.md) | planning | high | yes |
| [`revise-fast`](revise-fast/README.md) | planning | medium | no |
| [`rollback`](rollback/README.md) | rollback | medium | yes |
| [`run`](run/README.md) | orchestration | high | yes |
| [`run_hard`](run_hard/README.md) | orchestration | max | yes |
| [`security_review`](security_review/README.md) | security_review | high | yes |
| [`sleep`](sleep/README.md) | sleep | low | yes |
| [`start_of_day`](start_of_day/README.md) | session-lifecycle | low | yes |
| [`status`](status/README.md) | status | low | yes |
| [`thorough_plan`](thorough_plan/README.md) | planning | max | yes |
| [`triage`](triage/README.md) | routing | low | yes |
| [`weekly_review`](weekly_review/README.md) | session-lifecycle | low | yes |
| [`specify`](specify/README.md) | specify | max | yes |
| [`enrich`](enrich/README.md) | enrich | max | yes |
| [`workspace`](workspace/README.md) | workspace | medium | yes |
