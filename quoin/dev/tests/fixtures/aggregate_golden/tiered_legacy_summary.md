# Benchmark Run: run-tiered


## Primary Result: Within-Agent-Pair Comparison

_This section shows the primary finding: quoin's effect on each agent. Cross-agent comparison (Claude vs Codex) is secondary and model-confounded._

### Claude Pair

| Cell          | N tasks | pass@1 | 95% CI         | Mean cost USD | Total cost USD | Mean wall-clock s | Gate interventions | Headline pass/USD |
| ---           | ---     | ---    | ---            | ---           | ---            | ---               | ---                | ---               |
| simple-claude | 5       | 0.400  | [0.100, 0.900] | $3.1000       | $15.50         | 30.0              | 0                  | 0.1290            |
| quoin-claude  | 4       | 0.750  | [0.100, 0.900] | $2.5000       | $7.50          | 30.0              | 0                  | 0.3000            |


**McNemar paired test (quoin-claude vs simple-claude):** p = 0.5000 (NOT significant at α=0.05)

_Note: The observed delta is within sampling variance at this suite size. See methodology.md MDE table for detectable effect sizes._

### Codex Pair

_Note: Codex cost metric is undefined in v1 (cost telemetry not available per quoin/adapters/codex/cost.md). Headline pass/USD is reported for Claude pair only._

| Cell | N tasks | pass@1 | 95% CI | Mean cost USD | Total cost USD | Mean wall-clock s | Gate interventions | Headline pass/USD |
| ---  | ---     | ---    | ---    | ---           | ---            | ---               | ---                | ---               |

## Secondary / Model-Confounded View

**Do not headline these numbers.** Cross-agent comparison is model-confounded: differences between Claude and Codex cells reflect model differences, not quoin's effect. This table is provided for completeness only.

| Cell          | N tasks | pass@1 | 95% CI         | Mean cost USD | Total cost USD | Mean wall-clock s | Gate interventions |
| ---           | ---     | ---    | ---            | ---           | ---            | ---               | ---                |
| quoin-claude  | 4       | 0.750  | [0.100, 0.900] | $2.5000       | $7.50          | 30.0              | 0                  |
| simple-claude | 5       | 0.400  | [0.100, 0.900] | $3.1000       | $15.50         | 30.0              | 0                  |

## Rank-Order Table: Per-Task Pass/Fail

_Rows = cells; Columns = task IDs. Shows which tasks each cell solved, not just aggregate rates._

| Task ID | quoin-claude | simple-claude |
| ---     | ---          | ---           |
| e1      | P            | P             |
| e2      | P            | F             |
| l1      | -            | -             |
| m1      | P            | T             |
| m2      | E            | P             |

_Legend: P=pass, F=fail, E=error, T=timeout, -=not run_
