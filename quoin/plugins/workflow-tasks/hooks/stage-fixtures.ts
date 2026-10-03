/** One fixture table read by the TypeScript tests and by the Python parity test. */
export type StageFixture = {
  id: string
  /** Path relative to .workflow_artifacts/ -> body, or { text, mtime } (seconds offset from a fixed base); a key ending in / is an empty dir. */
  tree: Record<string, string | { text: string; mtime: number }>
  /** The task argument deriveStage receives. */
  task: string
  /** Directory handed to detect_phase, relative to .workflow_artifacts/; defaults to task. */
  phaseDir?: string
  expect: {
    detectPhase: string | null
    stage: string
    command: string | null
    gateVerdict?: 'pass' | 'fail' | 'undecided'
    newestGate?: string
  }
}

/** Base for the mtime offsets: 2023-11-14T22:13:20Z. */
export const FIXTURE_BASE_SECONDS = 1700000000

export const STAGE_FIXTURES: StageFixture[] =
// BEGIN STAGE FIXTURES
[
  {
    "id": "empty-task-folder",
    "tree": {
      "foo/": ""
    },
    "task": "foo",
    "expect": {
      "detectPhase": "discover",
      "stage": "started",
      "command": "/specify foo"
    }
  },
  {
    "id": "task-brief-only",
    "tree": {
      "foo/task-brief.md": "# Brief\n"
    },
    "task": "foo",
    "expect": {
      "detectPhase": "discover",
      "stage": "pre-spec",
      "command": "/specify foo"
    }
  },
  {
    "id": "spec-only",
    "tree": {
      "foo/spec.md": "# Spec\n"
    },
    "task": "foo",
    "expect": {
      "detectPhase": "discover",
      "stage": "spec",
      "command": "/gate foo"
    }
  },
  {
    "id": "spec-gate-pass",
    "tree": {
      "foo/spec.md": {
        "text": "# Spec\n",
        "mtime": 100
      },
      "foo/gate-specify-2026-10-03.md": {
        "text": "# Gate\n\n## Verdict\n\nPASS\n",
        "mtime": 200
      }
    },
    "task": "foo",
    "expect": {
      "detectPhase": "discover",
      "stage": "spec-approved",
      "command": "/architect foo",
      "gateVerdict": "pass"
    }
  },
  {
    "id": "spec-gate-fail",
    "tree": {
      "foo/spec.md": {
        "text": "# Spec\n",
        "mtime": 100
      },
      "foo/gate-specify-2026-10-03.md": {
        "text": "# Gate\n\n## Verdict\n\nFAIL\n",
        "mtime": 200
      }
    },
    "task": "foo",
    "expect": {
      "detectPhase": "discover",
      "stage": "spec-gate-failed",
      "command": "/specify foo",
      "gateVerdict": "fail"
    }
  },
  {
    "id": "spec-gate-fail-cleared-by-newer-spec",
    "tree": {
      "foo/spec.md": {
        "text": "# Spec\n",
        "mtime": 300
      },
      "foo/gate-specify-2026-10-03.md": {
        "text": "# Gate\n\n## Verdict\n\nFAIL\n",
        "mtime": 200
      }
    },
    "task": "foo",
    "expect": {
      "detectPhase": "discover",
      "stage": "spec",
      "command": "/gate foo",
      "gateVerdict": "fail"
    }
  },
  {
    "id": "program-folder",
    "tree": {
      "foo/program.md": "# Program\n",
      "foo/spec.md": "# Spec\n"
    },
    "task": "foo",
    "expect": {
      "detectPhase": "discover",
      "stage": "program",
      "command": null
    }
  },
  {
    "id": "architecture-only",
    "tree": {
      "foo/architecture.md": "# Architecture\n"
    },
    "task": "foo",
    "expect": {
      "detectPhase": "architecture",
      "stage": "architecture",
      "command": "/gate foo"
    }
  },
  {
    "id": "architecture-gate-pass",
    "tree": {
      "foo/architecture.md": {
        "text": "# Architecture\n",
        "mtime": 100
      },
      "foo/gate-architect-2026-10-03.md": {
        "text": "# Gate\n\n## Verdict\n\nPASS\n",
        "mtime": 200
      }
    },
    "task": "foo",
    "expect": {
      "detectPhase": "architecture",
      "stage": "architecture-approved",
      "command": "/thorough_plan foo",
      "gateVerdict": "pass"
    }
  },
  {
    "id": "architecture-gate-fail",
    "tree": {
      "foo/architecture.md": {
        "text": "# Architecture\n",
        "mtime": 100
      },
      "foo/gate-architect-2026-10-03.md": {
        "text": "# Gate\n\n## Verdict\n\nFAIL\n",
        "mtime": 200
      }
    },
    "task": "foo",
    "expect": {
      "detectPhase": "architecture",
      "stage": "architecture-gate-failed",
      "command": "/architect foo",
      "gateVerdict": "fail"
    }
  },
  {
    "id": "architecture-gate-fail-cleared-by-newer-architecture",
    "tree": {
      "foo/architecture.md": {
        "text": "# Architecture\n",
        "mtime": 300
      },
      "foo/gate-architect-2026-10-03.md": {
        "text": "# Gate\n\n## Verdict\n\nFAIL\n",
        "mtime": 200
      }
    },
    "task": "foo",
    "expect": {
      "detectPhase": "architecture",
      "stage": "architecture",
      "command": "/gate foo",
      "gateVerdict": "fail"
    }
  },
  {
    "id": "plan-alone",
    "tree": {
      "foo/current-plan.md": "# Plan\n"
    },
    "task": "foo",
    "expect": {
      "detectPhase": "planning",
      "stage": "planning",
      "command": "/gate foo"
    }
  },
  {
    "id": "plan-with-critic-rounds",
    "tree": {
      "foo/current-plan.md": "# Plan\n",
      "foo/critic-response-1.md": "x\n",
      "foo/critic-response-2.md": "x\n"
    },
    "task": "foo",
    "expect": {
      "detectPhase": "planning",
      "stage": "planning",
      "command": "/gate foo"
    }
  },
  {
    "id": "plan-gate-pass",
    "tree": {
      "foo/current-plan.md": {
        "text": "# Plan\n",
        "mtime": 100
      },
      "foo/gate-plan-2026-10-03.md": {
        "text": "# Gate\n\n## Verdict\n\nPASS\n",
        "mtime": 200
      }
    },
    "task": "foo",
    "expect": {
      "detectPhase": "plan-gated",
      "stage": "plan-approved",
      "command": "/implement foo",
      "gateVerdict": "pass"
    }
  },
  {
    "id": "plan-gate-post-plan-pass",
    "tree": {
      "foo/current-plan.md": {
        "text": "# Plan\n",
        "mtime": 100
      },
      "foo/gate-post-plan-2026-10-03.md": {
        "text": "# Gate\n\n## Verdict\n\nPASS\n",
        "mtime": 200
      }
    },
    "task": "foo",
    "expect": {
      "detectPhase": "plan-gated",
      "stage": "plan-approved",
      "command": "/implement foo",
      "gateVerdict": "pass"
    }
  },
  {
    "id": "plan-gate-tmp-leftover",
    "tree": {
      "foo/current-plan.md": {
        "text": "# Plan\n",
        "mtime": 100
      },
      "foo/gate-plan-2026-10-03.md.tmp": {
        "text": "partial\n",
        "mtime": 200
      }
    },
    "task": "foo",
    "expect": {
      "detectPhase": "plan-gated",
      "stage": "gate-undecided",
      "command": "/gate foo"
    }
  },
  {
    "id": "plan-gate-fail",
    "tree": {
      "foo/current-plan.md": {
        "text": "# Plan\n",
        "mtime": 100
      },
      "foo/gate-plan-2026-10-03.md": {
        "text": "# Gate\n\n## Verdict\n\nFAIL\n",
        "mtime": 200
      }
    },
    "task": "foo",
    "expect": {
      "detectPhase": "plan-gated",
      "stage": "plan-gate-failed",
      "command": "/thorough_plan foo",
      "gateVerdict": "fail"
    }
  },
  {
    "id": "plan-gate-fail-cleared-by-newer-plan",
    "tree": {
      "foo/current-plan.md": {
        "text": "# Plan\n",
        "mtime": 300
      },
      "foo/gate-plan-2026-10-03.md": {
        "text": "# Gate\n\n## Verdict\n\nFAIL\n",
        "mtime": 200
      }
    },
    "task": "foo",
    "expect": {
      "detectPhase": "plan-gated",
      "stage": "planning",
      "command": "/gate foo",
      "gateVerdict": "fail"
    }
  },
  {
    "id": "implement-gate-pass",
    "tree": {
      "foo/current-plan.md": "# Plan\n",
      "foo/gate-implement-2026-10-03.md": {
        "text": "# Gate\n\n## Verdict\n\nPASS\n",
        "mtime": 200
      }
    },
    "task": "foo",
    "expect": {
      "detectPhase": "implement-gated",
      "stage": "implement-done",
      "command": "/review foo",
      "gateVerdict": "pass"
    }
  },
  {
    "id": "implement-gate-fail",
    "tree": {
      "foo/current-plan.md": "# Plan\n",
      "foo/gate-implement-2026-10-03.md": {
        "text": "# Gate\n\n## Verdict\n\nFAIL\n",
        "mtime": 200
      }
    },
    "task": "foo",
    "expect": {
      "detectPhase": "implement-gated",
      "stage": "implement-gate-failed",
      "command": "/implement foo",
      "gateVerdict": "fail"
    }
  },
  {
    "id": "implement-gates-r2-fail-then-r3-pass-mtimes-reversed",
    "tree": {
      "foo/gate-implement-2026-10-01-r2.md": {
        "text": "---\ntask: ivg-273-opencode-m2b-workflow-parity\nphase: implement\nstage: 1\ndate: 2026-10-01\ngate-level: full\nauto_approved: false\nmode: autonomous\n---\n\n## Automated checks\n\n\n## Verdict\n\nFAIL\n",
        "mtime": 300
      },
      "foo/gate-implement-2026-10-01-r3.md": {
        "text": "# Gate\n\n## Verdict\n\nPASS\n",
        "mtime": 200
      }
    },
    "task": "foo",
    "expect": {
      "detectPhase": "implement-gated",
      "stage": "implement-done",
      "command": "/review foo",
      "gateVerdict": "pass",
      "newestGate": "gate-implement-2026-10-01-r3.md"
    }
  },
  {
    "id": "review-1",
    "tree": {
      "foo/review-1.md": "# Review\n"
    },
    "task": "foo",
    "expect": {
      "detectPhase": "review",
      "stage": "review",
      "command": "/gate foo"
    }
  },
  {
    "id": "review-gate-pass",
    "tree": {
      "foo/review-1.md": {
        "text": "# Review\n",
        "mtime": 100
      },
      "foo/gate-review-2026-10-03.md": {
        "text": "# Gate\n\n## Verdict\n\nPASS\n",
        "mtime": 200
      }
    },
    "task": "foo",
    "expect": {
      "detectPhase": "review-gated",
      "stage": "review-approved",
      "command": "/end_of_task foo",
      "gateVerdict": "pass"
    }
  },
  {
    "id": "review-gate-fail-bold-form",
    "tree": {
      "foo/review-1.md": {
        "text": "# Review\n",
        "mtime": 100
      },
      "foo/gate-review-2026-10-03.md": {
        "text": "# Gate\n\n## Verdict\n\n**FAIL**\n",
        "mtime": 200
      }
    },
    "task": "foo",
    "expect": {
      "detectPhase": "review-gated",
      "stage": "review-gate-failed",
      "command": "/review foo",
      "gateVerdict": "fail"
    }
  },
  {
    "id": "review-gate-fail-verdict-tag-form",
    "tree": {
      "foo/review-1.md": {
        "text": "# Review\n",
        "mtime": 100
      },
      "foo/gate-review-2026-10-03.md": {
        "text": "# Gate\n\n## Verdict\n\n<verdict>FAIL</verdict>\n",
        "mtime": 200
      }
    },
    "task": "foo",
    "expect": {
      "detectPhase": "review-gated",
      "stage": "review-gate-failed",
      "command": "/review foo",
      "gateVerdict": "fail"
    }
  },
  {
    "id": "review-gate-fail-heading-form",
    "tree": {
      "foo/review-1.md": {
        "text": "# Review\n",
        "mtime": 100
      },
      "foo/gate-review-2026-10-03.md": {
        "text": "# Gate\n\n## Verdict: FAIL\n",
        "mtime": 200
      }
    },
    "task": "foo",
    "expect": {
      "detectPhase": "review-gated",
      "stage": "review-gate-failed",
      "command": "/review foo",
      "gateVerdict": "fail"
    }
  },
  {
    "id": "review-gate-fail-cleared-by-newer-review",
    "tree": {
      "foo/review-1.md": {
        "text": "# Review\n",
        "mtime": 100
      },
      "foo/gate-review-2026-10-03.md": {
        "text": "# Gate\n\n## Verdict\n\nFAIL\n",
        "mtime": 200
      },
      "foo/review-2.md": {
        "text": "# Review 2\n",
        "mtime": 300
      }
    },
    "task": "foo",
    "expect": {
      "detectPhase": "review-gated",
      "stage": "review",
      "command": "/gate foo",
      "gateVerdict": "fail"
    }
  },
  {
    "id": "review-gate-tmp-leftover-only",
    "tree": {
      "foo/review-1.md": {
        "text": "# Review\n",
        "mtime": 100
      },
      "foo/gate-review-2026-10-03.md.tmp": {
        "text": "partial\n",
        "mtime": 200
      }
    },
    "task": "foo",
    "expect": {
      "detectPhase": "review-gated",
      "stage": "gate-undecided",
      "command": "/gate foo"
    }
  },
  {
    "id": "drive-conflict-gate-copy-ignored",
    "tree": {
      "foo/review-1.md": {
        "text": "# Review\n",
        "mtime": 100
      },
      "foo/gate-review-2026-10-03 1.md": {
        "text": "# Gate\n\n## Verdict\n\nPASS\n",
        "mtime": 200
      }
    },
    "task": "foo",
    "expect": {
      "detectPhase": "review",
      "stage": "review",
      "command": "/gate foo"
    }
  },
  {
    "id": "gate-postreview-name-falls-through",
    "tree": {
      "foo/current-plan.md": "# Plan\n",
      "foo/gate-postreview-2026-10-03.md": "# Gate\n\n## Verdict\n\nPASS\n"
    },
    "task": "foo",
    "expect": {
      "detectPhase": "planning",
      "stage": "planning",
      "command": "/gate foo"
    }
  },
  {
    "id": "gate-spec-name-falls-through",
    "tree": {
      "foo/spec.md": "# Spec\n",
      "foo/gate-spec-2026-10-03.md": "# Gate\n\n## Verdict\n\nPASS\n"
    },
    "task": "foo",
    "expect": {
      "detectPhase": "discover",
      "stage": "spec",
      "command": "/gate foo"
    }
  },
  {
    "id": "gate-thorough-plan-name-falls-through",
    "tree": {
      "foo/current-plan.md": "# Plan\n",
      "foo/gate-thorough_plan-2026-10-03.md": "# Gate\n\n## Verdict\n\nPASS\n"
    },
    "task": "foo",
    "expect": {
      "detectPhase": "planning",
      "stage": "planning",
      "command": "/gate foo"
    }
  },
  {
    "id": "off-pattern-round2-fail-beats-older-dated-pass",
    "tree": {
      "foo/gate-implement-2026-09-20.md": {
        "text": "---\ntask: ccr-v3-upgrade\nstage: 3\nphase: implement\nround: fix-1\ndate: 2026-09-21\ngate-level: Full\nprofile: Large\nmode: subagent-checks + foreground-approval\nhead: ef487e4\nbase: bf58e40\n---\n\n## Verdict\n\nPASS\n",
        "mtime": 400
      },
      "foo/gate-implement-2026-09-23-round2.md": {
        "text": "---\ntask: ivg-273-opencode-m2b-workflow-parity\nphase: implement\nstage: 1\ndate: 2026-10-01\ngate-level: full\nauto_approved: false\nmode: autonomous\n---\n\n## Automated checks\n\n\n## Verdict\n\nFAIL\n",
        "mtime": 300
      }
    },
    "task": "foo",
    "expect": {
      "detectPhase": "implement-gated",
      "stage": "implement-gate-failed",
      "command": "/implement foo",
      "gateVerdict": "fail",
      "newestGate": "gate-implement-2026-09-23-round2.md"
    }
  },
  {
    "id": "off-pattern-retry-before-date",
    "tree": {
      "foo/gate-post-implement-2026-09-24.md": {
        "text": "---\ntask: ivg-269-opencode-m0-qualification\nphase: post-implement\ndate: 2026-09-24\ngate-level: full\n---\n\n## Automated checks\n\n1. \u2713 Plan tasks \u2014 all 13 tasks (T-01 through T-13) implemented and committed on `feat/ivg-269-opencode-m0-qualification` in the dedicated worktree.\n2. \u2713 Affected-area test suite \u2014 683 passed, 1 skipped (pre-existing opt-in skip), 0 failed. Run with the pinned `quoin/.venv` interpreter (3.14) after sourcing selectors via `affected_tests.py --select-only`, since the tool's own upward `.venv` walk from inside the worktree finds the outer project's unrelated 3.12 `.venv` first.\n3. \u2713 CI mirror \u2014 exit 0, `ran_steps=false`, `exit_reason=no-deliverable`. N/A \u2014 no non-Python deliverable in this diff.\n\n## Verdict\n\nFAIL (mechanical, on the full-suite known-red row only; every check this task's own code controls \u2014 affected-area suite, CI mirror, deploy drift, branch hygiene, secrets/debug-code \u2014 is clean; the fail is a pre-existing, unregistered baseline gap independently verified above)\n",
        "mtime": 300
      },
      "foo/gate-post-implement-r2-2026-09-24.md": {
        "text": "---\ntask: ivg-269-opencode-m0-qualification\nphase: post-implement\ndate: 2026-09-24\ngate-level: full\n---\n\n## Automated checks\n\n1. \u2713 Plan tasks \u2014 all thirteen plan tasks remain implemented and committed on `feat/ivg-269-opencode-m0-qualification` in the dedicated worktree; the review-fix commits since round 1 address review findings, not new plan scope.\n2. \u2713 Affected-area test suite \u2014 720 passed, 1 skipped (pre-existing opt-in skip), 0 failed. Selectors sourced via `affected_tests.py --select-only` (its own upward `.venv` walk still finds the outer project's unrelated 3.12 `.venv`, as at round 1), run explicitly with the pinned `quoin/.venv` interpreter (3.14).\n3. \u2713 CI mirror \u2014 exit 0, `ran_steps=false`, `exit_reason=no-deliverable`. N/A \u2014 no non-Python deliverable in this diff.\n\n## Verdict\n\nPASS\n",
        "mtime": 200
      }
    },
    "task": "foo",
    "expect": {
      "detectPhase": "implement-gated",
      "stage": "implement-done",
      "command": "/review foo",
      "gateVerdict": "pass",
      "newestGate": "gate-post-implement-r2-2026-09-24.md"
    }
  },
  {
    "id": "off-pattern-fix-n-prefix",
    "tree": {
      "foo/gate-implement-2026-09-21.md": {
        "text": "---\ntask: ivg-273-opencode-m2b-workflow-parity\nphase: implement\nstage: 1\ndate: 2026-10-01\ngate-level: full\nauto_approved: false\nmode: autonomous\n---\n\n## Automated checks\n\n\n## Verdict\n\nFAIL\n",
        "mtime": 100
      },
      "foo/gate-implement-fix-1-2026-09-21.md": {
        "text": "---\ntask: ccr-v3-upgrade\nstage: 3\nphase: implement\nround: fix-1\ndate: 2026-09-21\ngate-level: Full\nprofile: Large\nmode: subagent-checks + foreground-approval\nhead: ef487e4\nbase: bf58e40\n---\n\n## Verdict\n\nPASS\n",
        "mtime": 200
      }
    },
    "task": "foo",
    "expect": {
      "detectPhase": "implement-gated",
      "stage": "implement-done",
      "command": "/review foo",
      "gateVerdict": "pass",
      "newestGate": "gate-implement-fix-1-2026-09-21.md"
    }
  },
  {
    "id": "off-pattern-round3-suffix-token",
    "tree": {
      "foo/gate-implement-2026-09-11.md": {
        "text": "---\ntask: ivg-273-opencode-m2b-workflow-parity\nphase: implement\nstage: 1\ndate: 2026-10-01\ngate-level: full\nauto_approved: false\nmode: autonomous\n---\n\n## Automated checks\n\n\n## Verdict\n\nFAIL\n",
        "mtime": 300
      },
      "foo/gate-implement-round3-2026-09-11.md": {
        "text": "---\ntask: prompt-audit-anthropic-skills\nstage: stage-1-continuation\nphase: implement\ndate: 2026-09-11\ngate-level: standard\n---\n\n## Automated checks\n\n1. \u2713 Read-only invariant held \u2014 `git -C quoin status --porcelain` empty, `git -C quoin rev-parse HEAD` == `2dab0b4a8100c3f5259a297955b8b1b90c2917d8`, matching the plan's pinned baseline exactly. Re-verified both before and after the fix pass. No file under `quoin/` was touched.\n2. \u2713 Plan artifact validator-clean \u2014 `validate_artifact.py current-plan.md` PASS.\n\n## Verdict\n\nPASS\n",
        "mtime": 200
      }
    },
    "task": "foo",
    "expect": {
      "detectPhase": "implement-gated",
      "stage": "implement-done",
      "command": "/review foo",
      "gateVerdict": "pass",
      "newestGate": "gate-implement-round3-2026-09-11.md"
    }
  },
  {
    "id": "off-pattern-letter-suffix-after-date",
    "tree": {
      "foo/gate-implement-2026-05-17.md": {
        "text": "---\ntask: ivg-273-opencode-m2b-workflow-parity\nphase: implement\nstage: 1\ndate: 2026-10-01\ngate-level: full\nauto_approved: false\nmode: autonomous\n---\n\n## Automated checks\n\n\n## Verdict\n\nFAIL\n",
        "mtime": 100
      },
      "foo/gate-implement-2026-05-17b.md": {
        "text": "---\ngate: review-fix \u2192 review\ntask: quoin-benchmarks\nprofile: medium\nlevel: standard\ndate: 2026-05-17\nverdict: PASS\nauto_approved: false\ncommit: 920dfb9\n---\n\n# Gate: review-fix \u2192 review\n",
        "mtime": 200
      }
    },
    "task": "foo",
    "expect": {
      "detectPhase": "implement-gated",
      "stage": "implement-done",
      "command": "/review foo",
      "gateVerdict": "pass",
      "newestGate": "gate-implement-2026-05-17b.md"
    }
  },
  {
    "id": "off-pattern-amendment-suffix",
    "tree": {
      "foo/current-plan.md": {
        "text": "# Plan\n",
        "mtime": 50
      },
      "foo/gate-plan-2026-09-30.md": {
        "text": "---\ntask: ivg-273-opencode-m2b-workflow-parity\nphase: implement\nstage: 1\ndate: 2026-10-01\ngate-level: full\nauto_approved: false\nmode: autonomous\n---\n\n## Automated checks\n\n\n## Verdict\n\nFAIL\n",
        "mtime": 300
      },
      "foo/gate-plan-2026-10-01-amendment.md": {
        "text": "---\ntask: affected-tests-deleted-test-venv-scan\nphase: plan\ndate: 2026-10-01\ngate-level: smoke\n---\n\n## Automated checks\n1. \u2713 Plan artifact exists, non-empty (current-plan.md, ~64 KB)\n2. \u2713 Pending plan tasks 11 through 18 have file paths and acceptance criteria\n3. \u2713 Medium profile: convergence summary present, final verdict PASS (round 3, 0 critical, 0 major)\n4. \u2713 `## For human` block present\n\n## Verdict\nPASS\n\n## Warnings (non-blocking)\n",
        "mtime": 200
      }
    },
    "task": "foo",
    "expect": {
      "detectPhase": "plan-gated",
      "stage": "plan-approved",
      "command": "/implement foo",
      "gateVerdict": "pass",
      "newestGate": "gate-plan-2026-10-01-amendment.md"
    }
  },
  {
    "id": "off-pattern-bare-number-before-date",
    "tree": {
      "foo/gate-implement-2026-09-05.md": {
        "text": "---\ntask: ivg-273-opencode-m2b-workflow-parity\nphase: implement\nstage: 1\ndate: 2026-10-01\ngate-level: full\nauto_approved: false\nmode: autonomous\n---\n\n## Automated checks\n\n\n## Verdict\n\nFAIL\n",
        "mtime": 300
      },
      "foo/gate-implement-2-2026-09-05.md": {
        "text": "---\ntask: ivg-258-compaction-aware-run-continuity\nstage: 7\nartifact: gate\nphase: implement\nlevel: Standard\ndate: 2026-09-05\nverdict: PASS\n---\n## For human\n\nPost-fix gate for stage 7, after the review round-1 fix commit (`fcc9cc9`) closed MAJ-1\n\n## Verdict rationale\n\nPASS. All required review round-1 findings (MAJ-1, MIN-1, MIN-2) verified closed by independent\nre-derivation, not taken on the implement/fix agent's report. Full suite is now fully green (no\n",
        "mtime": 200
      }
    },
    "task": "foo",
    "expect": {
      "detectPhase": "implement-gated",
      "stage": "implement-done",
      "command": "/review foo",
      "gateVerdict": "pass",
      "newestGate": "gate-implement-2-2026-09-05.md"
    }
  },
  {
    "id": "off-pattern-undated-ranks-below-dated",
    "tree": {
      "foo/gate-implement-2026-09-20.md": {
        "text": "---\ntask: ccr-v3-upgrade\nstage: 3\nphase: implement\nround: fix-1\ndate: 2026-09-21\ngate-level: Full\nprofile: Large\nmode: subagent-checks + foreground-approval\nhead: ef487e4\nbase: bf58e40\n---\n\n## Verdict\n\nPASS\n",
        "mtime": 100
      },
      "foo/gate-implement-amendment.md": {
        "text": "---\ntask: ivg-273-opencode-m2b-workflow-parity\nphase: implement\nstage: 1\ndate: 2026-10-01\ngate-level: full\nauto_approved: false\nmode: autonomous\n---\n\n## Automated checks\n\n\n## Verdict\n\nFAIL\n",
        "mtime": 300
      }
    },
    "task": "foo",
    "expect": {
      "detectPhase": "implement-gated",
      "stage": "implement-done",
      "command": "/review foo",
      "gateVerdict": "pass",
      "newestGate": "gate-implement-2026-09-20.md"
    }
  },
  {
    "id": "off-pattern-date-in-fix-name-is-not-a-retry",
    "tree": {
      "foo/gate-implement-2026-08-15.md": {
        "text": "---\ntask: ivg-273-opencode-m2b-workflow-parity\nphase: implement\nstage: 1\ndate: 2026-10-01\ngate-level: full\nauto_approved: false\nmode: autonomous\n---\n\n## Automated checks\n\n\n## Verdict\n\nFAIL\n",
        "mtime": 300
      },
      "foo/gate-implement-fix-2026-08-15.md": {
        "text": "---\ntask: ccr-v3-upgrade\nstage: 3\nphase: implement\nround: fix-1\ndate: 2026-09-21\ngate-level: Full\nprofile: Large\nmode: subagent-checks + foreground-approval\nhead: ef487e4\nbase: bf58e40\n---\n\n## Verdict\n\nPASS\n",
        "mtime": 200
      }
    },
    "task": "foo",
    "expect": {
      "detectPhase": "implement-gated",
      "stage": "implement-gate-failed",
      "command": "/implement foo",
      "gateVerdict": "fail",
      "newestGate": "gate-implement-2026-08-15.md"
    }
  },
  {
    "id": "off-pattern-fixn-without-hyphen",
    "tree": {
      "foo/gate-implement-2026-09-21.md": {
        "text": "---\ntask: ivg-273-opencode-m2b-workflow-parity\nphase: implement\nstage: 1\ndate: 2026-10-01\ngate-level: full\nauto_approved: false\nmode: autonomous\n---\n\n## Automated checks\n\n\n## Verdict\n\nFAIL\n",
        "mtime": 100
      },
      "foo/gate-implement-fix2-2026-09-21.md": {
        "text": "---\ntask: ccr-v3-upgrade\nstage: 3\nphase: implement\nround: fix-1\ndate: 2026-09-21\ngate-level: Full\nprofile: Large\nmode: subagent-checks + foreground-approval\nhead: ef487e4\nbase: bf58e40\n---\n\n## Verdict\n\nPASS\n",
        "mtime": 50
      },
      "foo/gate-implement-fix-1-2026-09-21.md": {
        "text": "---\ntask: ivg-273-opencode-m2b-workflow-parity\nphase: implement\nstage: 1\ndate: 2026-10-01\ngate-level: full\nauto_approved: false\nmode: autonomous\n---\n\n## Automated checks\n\n\n## Verdict\n\nFAIL\n",
        "mtime": 300
      }
    },
    "task": "foo",
    "expect": {
      "detectPhase": "implement-gated",
      "stage": "implement-done",
      "command": "/review foo",
      "gateVerdict": "pass",
      "newestGate": "gate-implement-fix2-2026-09-21.md"
    }
  },
  {
    "id": "gate-specify-and-architect-prefixes-are-gates",
    "tree": {
      "foo/spec.md": {
        "text": "# Spec\n",
        "mtime": 100
      },
      "foo/gate-specify-2026-10-02.md": {
        "text": "# Gate\n\n## Verdict\n\nPASS\n",
        "mtime": 200
      },
      "foo/gate-specify-2026-10-03-r2.md": {
        "text": "# Gate\n\n## Verdict\n\nFAIL\n",
        "mtime": 150
      }
    },
    "task": "foo",
    "expect": {
      "detectPhase": "discover",
      "stage": "spec-gate-failed",
      "command": "/specify foo",
      "gateVerdict": "fail",
      "newestGate": "gate-specify-2026-10-03-r2.md"
    }
  },
  {
    "id": "verdict-needs-decision",
    "tree": {
      "foo/current-plan.md": {
        "text": "# Plan\n",
        "mtime": 100
      },
      "foo/gate-plan-2026-10-03.md": {
        "text": "# Gate\n\n## Verdict\n\nNEEDS-DECISION\n",
        "mtime": 200
      }
    },
    "task": "foo",
    "expect": {
      "detectPhase": "plan-gated",
      "stage": "gate-undecided",
      "command": "/gate foo",
      "gateVerdict": "undecided"
    }
  },
  {
    "id": "verdict-conditional-pass",
    "tree": {
      "foo/current-plan.md": {
        "text": "# Plan\n",
        "mtime": 100
      },
      "foo/gate-plan-2026-10-03.md": {
        "text": "# Gate\n\n## Verdict\n\nCONDITIONAL PASS \u2014 T-06 tests deferred.\n",
        "mtime": 200
      }
    },
    "task": "foo",
    "expect": {
      "detectPhase": "plan-gated",
      "stage": "plan-approved",
      "command": "/implement foo",
      "gateVerdict": "pass"
    }
  },
  {
    "id": "verdict-inline-no-heading",
    "tree": {
      "foo/review-1.md": {
        "text": "# Review\n",
        "mtime": 100
      },
      "foo/gate-review-2026-10-03.md": {
        "text": "# Gate \u2014 post-review (Stage S-05) \u2014 2026-07-31\n\n**Phase boundary:** post-review (inline, invoked by /run)\n**Level:** Full\n**Verdict:** PASS\n\n## Checks\n| Check | Result | Notes |\n|-------|--------|-------|\n| Review verdict | APPROVED | review-1.md validated PASS; nothing blocks shipping |\n| Full test suite | PASS | 5034 passed / 21 skipped / 0 failures on commit 5ff8a5c (no changes since the run) |\n| Merge conflicts vs main | NONE | git merge-tree clean, no conflict markers |\n",
        "mtime": 200
      }
    },
    "task": "foo",
    "expect": {
      "detectPhase": "review-gated",
      "stage": "review-approved",
      "command": "/end_of_task foo",
      "gateVerdict": "pass"
    }
  },
  {
    "id": "verdict-frontmatter-go",
    "tree": {
      "foo/current-plan.md": {
        "text": "# Plan\n",
        "mtime": 100
      },
      "foo/gate-plan-2026-10-03.md": {
        "text": "---\nverdict: GO\nauto_approved: false\n---\n# Gate\n\n## Checks\n",
        "mtime": 200
      }
    },
    "task": "foo",
    "expect": {
      "detectPhase": "plan-gated",
      "stage": "plan-approved",
      "command": "/implement foo",
      "gateVerdict": "pass"
    }
  },
  {
    "id": "verdict-frontmatter-blocked",
    "tree": {
      "foo/current-plan.md": {
        "text": "# Plan\n",
        "mtime": 100
      },
      "foo/gate-plan-2026-10-03.md": {
        "text": "---\nverdict: BLOCKED\nauto_approved: false\n---\n# Gate\n\n## Checks\n",
        "mtime": 200
      }
    },
    "task": "foo",
    "expect": {
      "detectPhase": "plan-gated",
      "stage": "gate-undecided",
      "command": "/gate foo",
      "gateVerdict": "undecided"
    }
  },
  {
    "id": "verdict-partial-heading",
    "tree": {
      "foo/current-plan.md": {
        "text": "# Plan\n",
        "mtime": 100
      },
      "foo/gate-plan-2026-10-03.md": {
        "text": "# Gate\n\n## Verdict\n\nPARTIAL \u2014 scope limited by session\n",
        "mtime": 200
      }
    },
    "task": "foo",
    "expect": {
      "detectPhase": "plan-gated",
      "stage": "gate-undecided",
      "command": "/gate foo",
      "gateVerdict": "undecided"
    }
  },
  {
    "id": "verdict-approved-heading",
    "tree": {
      "foo/current-plan.md": {
        "text": "# Plan\n",
        "mtime": 100
      },
      "foo/gate-plan-2026-10-03.md": {
        "text": "# Gate\n\n## Verdict\n\nAPPROVED.\n",
        "mtime": 200
      }
    },
    "task": "foo",
    "expect": {
      "detectPhase": "plan-gated",
      "stage": "plan-approved",
      "command": "/implement foo",
      "gateVerdict": "pass"
    }
  },
  {
    "id": "finalized-folder-is-done",
    "tree": {
      "finalized/foo/review-1.md": "# Review\n"
    },
    "task": "finalized/foo",
    "expect": {
      "detectPhase": "done",
      "stage": "done",
      "command": null
    }
  },
  {
    "id": "multi-stage-live-stage-2-in-planning",
    "tree": {
      "foo/architecture.md": "# Architecture\n\n## Stage decomposition\n\n1. \u2705 S-1: first stage\n2. \u23f3 S-2: second stage\n3. \u23f3 S-3: third stage\n\n## Appendix\n",
      "foo/stage-2/current-plan.md": "# Plan\n"
    },
    "task": "foo",
    "phaseDir": "foo/stage-2",
    "expect": {
      "detectPhase": "planning",
      "stage": "planning",
      "command": "/gate stage 2 of foo"
    }
  },
  {
    "id": "multi-stage-1-archived-stage-2-not-started",
    "tree": {
      "foo/architecture.md": {
        "text": "# Architecture\n\n## Stage decomposition\n\n1. \u2705 S-1: first stage\n2. \u23f3 S-2: second stage\n3. \u23f3 S-3: third stage\n\n## Appendix\n",
        "mtime": 100
      },
      "foo/gate-architect-2026-10-03.md": {
        "text": "# Gate\n\n## Verdict\n\nPASS\n",
        "mtime": 200
      },
      "foo/finalized/stage-1/review-1.md": "# Review\n"
    },
    "task": "foo",
    "phaseDir": "foo",
    "expect": {
      "detectPhase": "architecture",
      "stage": "architecture-approved",
      "command": "/thorough_plan stage 2 of foo"
    }
  },
  {
    "id": "multi-stage-all-stages-archived",
    "tree": {
      "foo/architecture.md": "# Architecture\n\n## Stage decomposition\n\n1. \u23f3 S-1: first stage\n2. \u23f3 S-2: second stage\n\n## Appendix\n",
      "foo/finalized/stage-1/review-1.md": "# Review\n",
      "foo/finalized/stage-2/review-1.md": "# Review\n"
    },
    "task": "foo",
    "phaseDir": "foo",
    "expect": {
      "detectPhase": "architecture",
      "stage": "stages-done",
      "command": null
    }
  },
  {
    "id": "multi-stage-empty-newest-stage-dir",
    "tree": {
      "foo/architecture.md": "# Architecture\n\n## Stage decomposition\n\n1. \u2705 S-1: first stage\n2. \u23f3 S-2: second stage\n3. \u23f3 S-3: third stage\n\n## Appendix\n",
      "foo/stage-1/": ""
    },
    "task": "foo",
    "phaseDir": "foo/stage-1",
    "expect": {
      "detectPhase": "discover",
      "stage": "architecture-approved",
      "command": "/thorough_plan stage 1 of foo"
    }
  },
  {
    "id": "multi-stage-1-review-approved-count-2",
    "tree": {
      "foo/architecture.md": "# Architecture\n\n## Stage decomposition\n\n1. \u23f3 S-1: first stage\n2. \u23f3 S-2: second stage\n\n## Appendix\n",
      "foo/stage-1/review-1.md": {
        "text": "# Review\n",
        "mtime": 100
      },
      "foo/stage-1/gate-review-2026-10-03.md": {
        "text": "# Gate\n\n## Verdict\n\nPASS\n",
        "mtime": 200
      }
    },
    "task": "foo",
    "phaseDir": "foo/stage-1",
    "expect": {
      "detectPhase": "review-gated",
      "stage": "review-approved",
      "command": "/thorough_plan stage 2 of foo",
      "gateVerdict": "pass"
    }
  },
  {
    "id": "multi-stage-final-stage-review-approved",
    "tree": {
      "foo/architecture.md": "# Architecture\n\n## Stage decomposition\n\n1. \u23f3 S-1: first stage\n2. \u23f3 S-2: second stage\n\n## Appendix\n",
      "foo/finalized/stage-1/review-1.md": "# Review\n",
      "foo/stage-2/review-1.md": {
        "text": "# Review\n",
        "mtime": 100
      },
      "foo/stage-2/gate-review-2026-10-03.md": {
        "text": "# Gate\n\n## Verdict\n\nPASS\n",
        "mtime": 200
      }
    },
    "task": "foo",
    "phaseDir": "foo/stage-2",
    "expect": {
      "detectPhase": "review-gated",
      "stage": "review-approved",
      "command": "/end_of_task stage 2 of foo",
      "gateVerdict": "pass"
    }
  },
  {
    "id": "multi-stage-live-stage-implement-gate-fail",
    "tree": {
      "foo/architecture.md": "# Architecture\n\n## Stage decomposition\n\n1. \u2705 S-1: first stage\n2. \u23f3 S-2: second stage\n3. \u23f3 S-3: third stage\n\n## Appendix\n",
      "foo/finalized/stage-1/review-1.md": "# Review\n",
      "foo/stage-2/current-plan.md": "# Plan\n",
      "foo/stage-2/gate-implement-2026-10-03.md": {
        "text": "# Gate\n\n## Verdict\n\nFAIL\n",
        "mtime": 200
      }
    },
    "task": "foo",
    "phaseDir": "foo/stage-2",
    "expect": {
      "detectPhase": "implement-gated",
      "stage": "implement-gate-failed",
      "command": "/implement stage 2 of foo",
      "gateVerdict": "fail"
    }
  }
]
// END STAGE FIXTURES
