#!/usr/bin/env python3
"""Portable core implementation of affected-area test selection and runner.

Given a set of changed files (via --project-root, --files-from, or --files),
this helper maps the changed files to affected test files and runs them.
The result is used by /gate and /review as a HARD PRECONDITION for APPROVED.

Exit-code semantics intentionally INVERT branch_hygiene's convention:
  0  — APPROVABLE (two sub-cases disambiguated by the `ran_pytest` output field):
       0a: affected-area suite GREEN (`ran_pytest=true`, `exit_reason="affected-green"`)
           pytest ran on a non-empty selector set and returned 0;
           `unmatched_sources` is empty (or --allow-unmatched was passed).
       0b: docs-only / no selectors (`ran_pytest=false`,
           `exit_reason="docs-only-no-selectors"`)
           no changed .py source or test file needs testing (docs/SKILL.md/JSON
           only) so there is legitimately nothing to test.  pytest is NOT invoked (HARD GUARD).
       0c: clean tree (`ran_pytest=false`, `exit_reason="no-changes"`)
           git ran cleanly and the working tree is genuinely unchanged.
       0d: changed test files missing on disk (`ran_pytest=false`,
           `exit_reason="missing-tests-only"`)
           every changed test file was deleted by the change (or a wrong path
           was passed via --files), so there is nothing to run.  The paths are
           listed in `missing_tests`.  pytest is NOT invoked.
  1  — affected-area suite RED: pytest ran and returned non-zero. BLOCKING.
  2  — argparse / malformed input.
  3  — UNDETERMINABLE (fail-CLOSED): git-root resolution failed, git error,
       `unmatched_sources` non-empty without --allow-unmatched, pytest
       binary missing, or a project venv interpreter found up the directory tree is
       broken (dangling link, missing, or not runnable) and no healthy venv
       with pytest was found
       (`exit_reason="venv-interpreter-broken"`, details in
       `interpreter_problems`).  Treat as "cannot confirm green → do NOT auto-approve."
       NOTE: QUOIN_DISABLE_AFFECTED_TESTS=1 also exits 3 (not 0) because
       disabling detection must not silently green-light an APPROVE — this
       is the OPPOSITE of branch_hygiene's env opt-out which exits 0.
  4  — a .py source changed AND its selectors resolved to the empty set
       (changed source with nothing to run).  Distinct from 3 so the gate
       message can say "no affected tests found for changed sources."
       gate/review treat 3 and 4 identically (both blocking-surface).
  5  — NO active quoin task context (NON-approving, NON-blocking).  Reachable
       ONLY with --require-task-context in --project-root mode when
       QUOIN_REQUIRE_TASK_CONTEXT!=0 and no active task folder is found at or
       above the project root (IVG-151).  Distinct from 0/1/2/3/4: it is a
       CLEAN-SKIP / N/A signal for a non-quoin session, never a WARN or a gate
       FAIL.  With an active task context this code can NEVER be returned — the
       real check runs and the existing 0/1/3/4 matrix is byte-for-byte intact.

Env:
  QUOIN_DISABLE_AFFECTED_TESTS=1 — exit 3 immediately (fail-CLOSED opt-out)
  QUOIN_REQUIRE_TASK_CONTEXT — literal "0" ONLY forces legacy always-run even
      when --require-task-context is passed (disarms the exit-5 branch); unset
      or any other value honors the flag (IVG-151).
  QUOIN_BASE_BRANCH — override the base branch probe order (default: tries
      origin/main, origin/master, main, master in order).
  QUOIN_SUBPROCESS_TIMEOUT — seconds, default 30; bounds every SHORT git
      subprocess run by this module (see _subprocess_timeout()). The pytest
      subprocess gets a generous DERIVED bound max(600, QUOIN_SUBPROCESS_TIMEOUT)
      instead (D-05) — a TimeoutExpired there maps to exit 3 with
      exit_reason="pytest-timeout" (BLOCKING-SURFACE, never a silent GREEN,
      never a hard-RED false block; see proc P-03).
  QUOIN_PYTHON — interpreter to run pytest under; used only if it exists, is
      executable and can import pytest, otherwise the venv walk continues.
  QUOIN_DISABLE_VENV_PROBE=1 — skip interpreter discovery and broken-venv
      detection entirely; pytest runs under the invoking interpreter.
  QUOIN_DISABLE_CHILD_REPO_SCAN=1 — skip the depth-1 child-.git discovery scan
      in discover_repos(); single-repo view only. Distinct from
      QUOIN_DISABLE_DISPATCH_CWD (a different concern, see D-08).

Git-root resolution note (CRIT-1 / IVG-70 remedy):
  The outer quoin project root is NOT a git repo; only the quoin/ subtree is.
  When given --project-root, this helper resolves the git repo itself via a
  depth-1 discover_repos-style scan (mirroring branch_hygiene.py), then runs
  all git commands INSIDE that repo.  The caller (gate/review) NEVER runs git
  directly — the helper owns the resolution + diff-basis fallback.

Diff-basis fallback chain (F-01 fix — no-upstream committed-branch gap):
  1. If upstream exists: git -C <repo> diff --name-only @{u}...HEAD (three-dot).
  2. If empty / no upstream: resolve the base branch (try origin/main,
     origin/master, main, master — or QUOIN_BASE_BRANCH override) and run
     git -C <repo> diff --name-only <merge_base>...HEAD (three-dot merge-base).
     This is the critical step for the committed-clean no-upstream case
     (the normal state during /review and both /gate invocations before
     /end_of_task pushes the branch).
  3. If still empty: worktree + staged fallback:
     git diff --name-only HEAD ∪ --name-only --cached.
  4. If STILL empty AND git ran cleanly: exit 0c (no-changes).
  5. On any git error: exit 3 (undeterminable).
  Fail-CLOSED note: if no base branch resolves AND there is no upstream AND
  the tree is committed-clean, prefer exit 3 (undeterminable) over silently
  approving with no-changes — because there may well be committed changes
  that simply cannot be diffed without a reference point.

Untracked-file blind spot (MIN-2):
  The worktree fallback (HEAD ∪ cached) does NOT list untracked (never-added)
  files.  This is NOT a false-green hole because the gate's separate
  "No uncommitted changes" check runs FIRST and blocks on any untracked file,
  so the helper never sees a state with untracked .py sources.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Directories to exclude from depth-1 repo scan (mirrors branch_hygiene._EXCLUDE_NAMES)
_EXCLUDE_NAMES: frozenset[str] = frozenset({
    ".workflow_artifacts",
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".idea",
    ".vscode",
})

# Directories pruned while walking a repo for test files.  Broader than
# _EXCLUDE_NAMES, which mirrors branch_hygiene's depth-1 repo discovery: a test
# walk also has to skip tool caches, build output and installed packages, none
# of which hold this project's tests.  Directories ending in ".egg-info" are
# pruned separately.
_WALK_EXCLUDE_NAMES: frozenset[str] = _EXCLUDE_NAMES | frozenset({
    ".tox",
    ".nox",
    ".eggs",
    "site-packages",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "build",
    "dist",
})

# Special-case mapping: certain docs/source files that are not .py themselves
# must trigger specific test files when changed.  Each entry is a
# (src_suffix, test_rel) pair where:
#   src_suffix — posix suffix that must appear at the END of the changed path
#                (with a leading "/" guard to avoid matching bare basenames from
#                 other repos, e.g. bare "CLAUDE.md" does NOT match "quoin/CLAUDE.md").
#   test_rel   — path of the test file, relative to the quoin/ git repo root.
# The guard is applied as: posix == src_suffix OR posix.endswith("/" + src_suffix).
_DOCS_TO_TESTS: tuple[tuple[str, str], ...] = (
    # Instruction files must not bring back shell rm, rmdir or mv; the six artifact
    # writers also carry the temp-file cleanup contract.
    (
        "quoin/memory/autonomous-mode.md",
        "quoin/dev/tests/test_no_shell_rm_mv_in_instructions.py",
    ),
    (
        "quoin/memory/autonomous-mode.md",
        "quoin/dev/tests/test_supervisor_progress_docs.py",
    ),
    (
        "quoin/memory/autonomous-mode.md",
        "quoin/dev/tests/test_child_watch_contract.py",
    ),
    (
        "quoin/adapters/claude/skills/run/SKILL.md",
        "quoin/dev/tests/test_child_watch_contract.py",
    ),
    (
        "quoin/core/skills/run.md",
        "quoin/dev/tests/test_child_watch_contract.py",
    ),
    (
        "quoin/memory/cost-ledger-format.md",
        "quoin/dev/tests/test_no_shell_rm_mv_in_instructions.py",
    ),
    (
        "quoin/memory/lifecycle-guide.md",
        "quoin/dev/tests/test_no_shell_rm_mv_in_instructions.py",
    ),
    (
        "quoin/memory/cleanup-task-bookkeeping.md",
        "quoin/dev/tests/test_no_shell_rm_mv_in_instructions.py",
    ),
    (
        "quoin/memory/checkpoint-spec.md",
        "quoin/dev/tests/test_no_shell_rm_mv_in_instructions.py",
    ),
    (
        "quoin/adapters/claude/skills/plan/SKILL.md",
        "quoin/dev/tests/test_no_shell_rm_mv_in_instructions.py",
    ),
    (
        "quoin/adapters/claude/skills/plan/SKILL.md",
        "quoin/dev/tests/test_tmp_cleanup_contract.py",
    ),
    (
        "quoin/adapters/claude/skills/architect/SKILL.md",
        "quoin/dev/tests/test_no_shell_rm_mv_in_instructions.py",
    ),
    (
        "quoin/adapters/claude/skills/architect/SKILL.md",
        "quoin/dev/tests/test_tmp_cleanup_contract.py",
    ),
    (
        "quoin/adapters/claude/skills/review/SKILL.md",
        "quoin/dev/tests/test_no_shell_rm_mv_in_instructions.py",
    ),
    (
        "quoin/adapters/claude/skills/review/SKILL.md",
        "quoin/dev/tests/test_tmp_cleanup_contract.py",
    ),
    (
        "quoin/adapters/claude/skills/revise/SKILL.md",
        "quoin/dev/tests/test_no_shell_rm_mv_in_instructions.py",
    ),
    (
        "quoin/adapters/claude/skills/revise/SKILL.md",
        "quoin/dev/tests/test_tmp_cleanup_contract.py",
    ),
    (
        "quoin/adapters/claude/skills/revise-fast/SKILL.md",
        "quoin/dev/tests/test_no_shell_rm_mv_in_instructions.py",
    ),
    (
        "quoin/adapters/claude/skills/revise-fast/SKILL.md",
        "quoin/dev/tests/test_tmp_cleanup_contract.py",
    ),
    (
        "quoin/adapters/claude/skills/security_review/SKILL.md",
        "quoin/dev/tests/test_no_shell_rm_mv_in_instructions.py",
    ),
    (
        "quoin/adapters/claude/skills/security_review/SKILL.md",
        "quoin/dev/tests/test_tmp_cleanup_contract.py",
    ),
    (
        "quoin/CLAUDE.md",
        "quoin/dev/tests/test_claude_md_size_ceiling.py",
    ),
    (
        "quoin/memory/format-kit.md",
        "quoin/dev/tests/test_preamble_freshness.py",
    ),
    (
        "quoin/memory/glossary.md",
        "quoin/dev/tests/test_preamble_freshness.py",
    ),
    # Added IVG-164 stage 1 (T-05): the slim-CLAUDE.md generator's regen
    # byte-identity drift guards (architecture D-01) live in
    # test_build_claude_slim.py, not the size-ceiling test above — without
    # these rows an affected-area /gate run selects only the size-ceiling
    # test on a CLAUDE.md edit and the drift guards never execute (lesson
    # 2026-07-04). Duplicate-key-safe: this consumer iterates all rows into
    # a set, so this row ADDS to the existing quoin/CLAUDE.md selector above
    # rather than displacing it.
    (
        "quoin/CLAUDE.md",
        "quoin/dev/tests/test_build_claude_slim.py",
    ),
    (
        "quoin/CLAUDE.slim.md",
        "quoin/dev/tests/test_build_claude_slim.py",
    ),
    (
        "quoin/memory/workflow-catalog.md",
        "quoin/dev/tests/test_build_claude_slim.py",
    ),
    # Added review-1.md MAJOR 2 (IVG-164 stage 1 fix round): the fail-closed
    # CLAUDE.md-citation disposition sweep (T-09) polices exactly the corpora an
    # adapter SKILL.md / quoin/memory/*.md edit can stale, but without these rows
    # test_claude_md_citations.py was unreachable from any affected-area selector —
    # verbatim the lesson-2026-07-04 blind spot the rows above were written to
    # close, now reopened for the sweep's own two doc sources plus its fixture.
    # Duplicate-key-safe (same iterate-all-rows-into-a-set consumer as above): both
    # doc rows ADD to their existing selectors rather than displacing them.
    (
        "quoin/CLAUDE.md",
        "quoin/dev/tests/test_claude_md_citations.py",
    ),
    (
        "quoin/memory/workflow-catalog.md",
        "quoin/dev/tests/test_claude_md_citations.py",
    ),
    (
        "quoin/dev/tests/fixtures/claude_md_citation_dispositions.json",
        "quoin/dev/tests/test_claude_md_citations.py",
    ),
    # IVG-164 stage 2 T-08: context_bundle exclusion drift test is reachable
    # when the SKILL.md corpus changes (bundle emission sites + review/gate
    # adapters + implement parity fix all land in the same stage).
    (
        "quoin/scripts/context_bundle.py",
        "quoin/dev/tests/test_context_bundle_exclusions.py",
    ),
    # Added IVG-249 T-11 (D-05): cost-ledger-format.md's mktemp-based
    # stderr-capture idiom (T-07) is pinned by test_agent_transcript_cost.py's
    # `$ATTR` shape assertion — without this row a doc-only edit to
    # cost-ledger-format.md is unselectable at a Standard gate and that
    # pinning test never runs (same lesson-2026-07-04 blind spot the rows
    # above were written to close). Duplicate-key-safe (same iterate-all-
    # rows-into-a-set consumer as above).
    (
        "quoin/memory/cost-ledger-format.md",
        "quoin/dev/tests/test_agent_transcript_cost.py",
    ),
    # Added IVG-249 T-08 (stage 2, S-02, D-7): the default-ON on-behalf
    # cost-capture flip touches four SKILL.md files' flag prose. Without
    # these rows a Standard gate selects ZERO of the tests that pin this
    # stage's contract — same lesson-2026-07-04 blind spot the rows above
    # were written to close. Duplicate-key-safe (same iterate-all-rows-
    # into-a-set consumer as above).
    (
        "quoin/adapters/claude/skills/run/SKILL.md",
        "quoin/dev/tests/test_onbehalf_default_on.py",
    ),
    (
        "quoin/adapters/claude/skills/run/SKILL.md",
        "quoin/dev/tests/test_onbehalf_writer_predicate.py",
    ),
    (
        "quoin/adapters/claude/skills/run/SKILL.md",
        "quoin/dev/tests/test_agent_transcript_cost.py",
    ),
    (
        "quoin/adapters/claude/skills/thorough_plan/SKILL.md",
        "quoin/dev/tests/test_onbehalf_default_on.py",
    ),
    (
        "quoin/adapters/claude/skills/thorough_plan/SKILL.md",
        "quoin/dev/tests/test_onbehalf_writer_predicate.py",
    ),
    (
        "quoin/adapters/claude/skills/thorough_plan/SKILL.md",
        "quoin/dev/tests/test_agent_transcript_cost.py",
    ),
    (
        "quoin/adapters/claude/skills/architect/SKILL.md",
        "quoin/dev/tests/test_onbehalf_default_on.py",
    ),
    (
        "quoin/adapters/claude/skills/architect/SKILL.md",
        "quoin/dev/tests/test_onbehalf_writer_predicate.py",
    ),
    (
        "quoin/adapters/claude/skills/architect/SKILL.md",
        "quoin/dev/tests/test_agent_transcript_cost.py",
    ),
    (
        "quoin/adapters/claude/skills/end_of_task/SKILL.md",
        "quoin/dev/tests/test_onbehalf_writer_predicate.py",
    ),
    # Round 4 (MAJOR-1(d)): closes the Site-7 (run/SKILL.md:729) blast-radius
    # gap — test_run_fast_path.py pins the route-conditional model literal
    # this stage rewrites there. No collision: the two SKILL.md-is-ignored
    # tests (test_affected_tests.py:211,341) use gate/SKILL.md and
    # critic/SKILL.md respectively (gate/SKILL.md
    # stopped being the shared unselectable exemplar once the T-07 rows
    # below made it selectable for test_eot_resilience_contract.py).
    (
        "quoin/adapters/claude/skills/run/SKILL.md",
        "quoin/dev/tests/test_run_fast_path.py",
    ),
    # IVG-249 S-03 T-07: gate/end_of_task/run SKILL.md edits (T-03/T-04/T-05)
    # now select the new contract/behavior test files T-06 added. gate/SKILL.md
    # is the ONLY genuinely unrepresented file among the three before this row
    # set — run/end_of_task already carry on-behalf-guard rows above.
    (
        "quoin/adapters/claude/skills/gate/SKILL.md",
        "quoin/dev/tests/test_eot_resilience_contract.py",
    ),
    (
        "quoin/adapters/claude/skills/end_of_task/SKILL.md",
        "quoin/dev/tests/test_eot_resilience_contract.py",
    ),
    (
        "quoin/adapters/claude/skills/run/SKILL.md",
        "quoin/dev/tests/test_eot_resilience_contract.py",
    ),
    (
        "quoin/adapters/claude/skills/run/SKILL.md",
        "quoin/dev/tests/test_run_inline_finish.py",
    ),
    (
        "quoin/memory/cost-ledger-format.md",
        "quoin/dev/tests/test_onbehalf_default_on.py",
    ),
    # T-08's one-liner execution harness (the empty-UUID writer guard's shell
    # tests) lands in test_run_inline_finish.py — without this row a future
    # re-break of the self-write chain is not affected-area-selectable from
    # the one file whose edit actually guards it.
    (
        "quoin/memory/cost-ledger-format.md",
        "quoin/dev/tests/test_run_inline_finish.py",
    ),
    # The clean-authored-content rule's guard test lives in
    # test_authored_content_rule_pointers.py — without these rows an
    # affected-area /gate run on the rule file or any of its pointer sites
    # never selects the test that guards them.
    (
        "quoin/memory/clean-authored-content.md",
        "quoin/dev/tests/test_authored_content_rule_pointers.py",
    ),
    (
        "quoin/adapters/claude/skills/implement/SKILL.md",
        "quoin/dev/tests/test_authored_content_rule_pointers.py",
    ),
    (
        "quoin/adapters/claude/skills/end_of_task/SKILL.md",
        "quoin/dev/tests/test_authored_content_rule_pointers.py",
    ),
    (
        "quoin/adapters/claude/skills/pr/SKILL.md",
        "quoin/dev/tests/test_authored_content_rule_pointers.py",
    ),
    (
        "quoin/adapters/claude/skills/review/SKILL.md",
        "quoin/dev/tests/test_authored_content_rule_pointers.py",
    ),
    (
        "quoin/adapters/claude/skills/run/SKILL.md",
        "quoin/dev/tests/test_authored_content_rule_pointers.py",
    ),
    (
        "quoin/adapters/claude/skills/end_of_task/SKILL.md",
        "quoin/dev/tests/test_authored_content_lint_wiring.py",
    ),
    (
        "quoin/adapters/claude/skills/review/SKILL.md",
        "quoin/dev/tests/test_authored_content_lint_wiring.py",
    ),
    # IVG-123: each of the 9 spawn-target skills' generated preamble.md
    # (quoin/skills/<skill>/preamble.md — see build_preambles.py's
    # SPAWN_TARGETS) selects test_preamble_freshness.py directly. Per-file
    # rows, not a directory-prefix rule (D-04): a prefix rule would widen
    # matching semantics for every existing row above. Duplicate-key-safe
    # (same iterate-all-rows-into-a-set consumer as above) — these are the
    # first rows for these specific paths, so no existing selector is
    # widened or displaced.
    (
        "quoin/skills/critic/preamble.md",
        "quoin/dev/tests/test_preamble_freshness.py",
    ),
    (
        "quoin/skills/revise/preamble.md",
        "quoin/dev/tests/test_preamble_freshness.py",
    ),
    (
        "quoin/skills/revise-fast/preamble.md",
        "quoin/dev/tests/test_preamble_freshness.py",
    ),
    (
        "quoin/skills/plan/preamble.md",
        "quoin/dev/tests/test_preamble_freshness.py",
    ),
    (
        "quoin/skills/review/preamble.md",
        "quoin/dev/tests/test_preamble_freshness.py",
    ),
    (
        "quoin/skills/architect/preamble.md",
        "quoin/dev/tests/test_preamble_freshness.py",
    ),
    (
        "quoin/skills/gate/preamble.md",
        "quoin/dev/tests/test_preamble_freshness.py",
    ),
    (
        "quoin/skills/specify/preamble.md",
        "quoin/dev/tests/test_preamble_freshness.py",
    ),
    (
        "quoin/skills/enrich/preamble.md",
        "quoin/dev/tests/test_preamble_freshness.py",
    ),
    # IVG-248 stage-1 T-08: the handoff_measure fixture tree is committed
    # non-Python JSONL (subagent + parent-session transcripts) plus a
    # future committed snapshot (T-05/T-10). The selector name-matches
    # test_<stem>*.py against files on disk, so a fixture-only edit lands
    # in `ignored` and selects zero tests without an explicit row here —
    # same lesson-2026-07-04 blind spot the rows above were written to
    # close. Both key and value are REPO-relative (resolved against the
    # git root), matching every other row in this table.
    (
        "quoin/dev/tests/fixtures/handoff_measure/projects/-fake-project/sid-case-a/subagents/agent-case-a.jsonl",
        "quoin/dev/tests/test_handoff_measure.py",
    ),
    (
        "quoin/dev/tests/fixtures/handoff_measure/projects/-fake-project/sid-case-b/subagents/agent-case-b.jsonl",
        "quoin/dev/tests/test_handoff_measure.py",
    ),
    (
        "quoin/dev/tests/fixtures/handoff_measure/projects/-fake-project/sid-case-c/subagents/agent-case-c.jsonl",
        "quoin/dev/tests/test_handoff_measure.py",
    ),
    (
        "quoin/dev/tests/fixtures/handoff_measure/projects/-fake-project/sid-case-d/subagents/agent-case-d.jsonl",
        "quoin/dev/tests/test_handoff_measure.py",
    ),
    (
        "quoin/dev/tests/fixtures/handoff_measure/projects/-fake-project/sid-case-e/subagents/agent-case-e.jsonl",
        "quoin/dev/tests/test_handoff_measure.py",
    ),
    (
        "quoin/dev/tests/fixtures/handoff_measure/projects/-fake-project/sid-case-f/subagents/agent-case-f.jsonl",
        "quoin/dev/tests/test_handoff_measure.py",
    ),
    (
        "quoin/dev/tests/fixtures/handoff_measure/projects/-fake-project/sid-case-g/subagents/agent-case-g.jsonl",
        "quoin/dev/tests/test_handoff_measure.py",
    ),
    (
        "quoin/dev/tests/fixtures/handoff_measure/projects/-fake-project/sid-case-h/subagents/agent-case-h.jsonl",
        "quoin/dev/tests/test_handoff_measure.py",
    ),
    (
        "quoin/dev/tests/fixtures/handoff_measure/projects/-fake-project/sid-case-i/subagents/agent-case-i.jsonl",
        "quoin/dev/tests/test_handoff_measure.py",
    ),
    (
        "quoin/dev/tests/fixtures/handoff_measure/projects/-fake-project/sid-case-j/subagents/agent-case-j.jsonl",
        "quoin/dev/tests/test_handoff_measure.py",
    ),
    (
        "quoin/dev/tests/fixtures/handoff_measure/projects/-fake-project/sid-case-k/subagents/agent-case-k.jsonl",
        "quoin/dev/tests/test_handoff_measure.py",
    ),
    (
        "quoin/dev/tests/fixtures/handoff_measure/projects/-fake-project/sid-case-l/subagents/agent-case-l.jsonl",
        "quoin/dev/tests/test_handoff_measure.py",
    ),
    (
        "quoin/dev/tests/fixtures/handoff_measure/projects/-fake-project/sid-case-m/subagents/agent-case-m.jsonl",
        "quoin/dev/tests/test_handoff_measure.py",
    ),
    (
        "quoin/dev/tests/fixtures/handoff_measure/projects/-fake-project/sid-case-n/subagents/agent-case-n.jsonl",
        "quoin/dev/tests/test_handoff_measure.py",
    ),
    # T-02 (agent-handoff-format stage 4): envelope partition / D-07
    # envelope-anchored phase discriminator / contract-read channel fixtures,
    # fresh sid-env-a..i namespace (see test_handoff_measure.py's own
    # collision-avoidance note next to the sid-case-* fixtures above).
    (
        "quoin/dev/tests/fixtures/handoff_measure/projects/-fake-project/sid-env-a/subagents/agent-env-a.jsonl",
        "quoin/dev/tests/test_handoff_measure.py",
    ),
    (
        "quoin/dev/tests/fixtures/handoff_measure/projects/-fake-project/sid-env-b/subagents/agent-env-b.jsonl",
        "quoin/dev/tests/test_handoff_measure.py",
    ),
    (
        "quoin/dev/tests/fixtures/handoff_measure/projects/-fake-project/sid-env-c/subagents/agent-env-c.jsonl",
        "quoin/dev/tests/test_handoff_measure.py",
    ),
    (
        "quoin/dev/tests/fixtures/handoff_measure/projects/-fake-project/sid-env-d/subagents/agent-env-d.jsonl",
        "quoin/dev/tests/test_handoff_measure.py",
    ),
    (
        "quoin/dev/tests/fixtures/handoff_measure/projects/-fake-project/sid-env-e/subagents/agent-env-e.jsonl",
        "quoin/dev/tests/test_handoff_measure.py",
    ),
    (
        "quoin/dev/tests/fixtures/handoff_measure/projects/-fake-project/sid-env-f/subagents/agent-env-f.jsonl",
        "quoin/dev/tests/test_handoff_measure.py",
    ),
    (
        "quoin/dev/tests/fixtures/handoff_measure/projects/-fake-project/sid-env-g/subagents/agent-env-g.jsonl",
        "quoin/dev/tests/test_handoff_measure.py",
    ),
    (
        "quoin/dev/tests/fixtures/handoff_measure/projects/-fake-project/sid-env-h/subagents/agent-env-h.jsonl",
        "quoin/dev/tests/test_handoff_measure.py",
    ),
    (
        "quoin/dev/tests/fixtures/handoff_measure/projects/-fake-project/sid-env-i/subagents/agent-env-i.jsonl",
        "quoin/dev/tests/test_handoff_measure.py",
    ),
    (
        "quoin/dev/tests/fixtures/handoff_measure/projects/-fake-project/sid-env-j/subagents/agent-env-j.jsonl",
        "quoin/dev/tests/test_handoff_measure.py",
    ),
    (
        "quoin/dev/tests/fixtures/handoff_measure/projects/-fake-project/sid-joint.jsonl",
        "quoin/dev/tests/test_handoff_measure.py",
    ),
    (
        "quoin/dev/tests/fixtures/handoff_measure/projects/-fake-project/sid-joint/subagents/agent-joint.jsonl",
        "quoin/dev/tests/test_handoff_measure.py",
    ),
    (
        "quoin/dev/tests/fixtures/handoff_measure/baseline/handoff-baseline-snapshot.json",
        "quoin/dev/tests/test_handoff_measure.py",
    ),
    # IVG-248 stage 2 T-14: a doc-only edit to the inter-agent handoff spec
    # is unselectable at a Standard gate without these rows — same
    # lesson-2026-07-04 blind spot the rows above were written to close.
    (
        "quoin/core/workflow/handoff-format.md",
        "quoin/dev/tests/test_core_workflow_portability_tokens.py",
    ),
    (
        "quoin/core/workflow/handoff-format.md",
        "quoin/dev/tests/test_handoff_validate.py",
    ),
    # Stage 5 (T-03): the checkable-rule table and its interaction cascade
    # moved to a companion file — a doc-only edit there needs the same
    # selectability the core file already has, above.
    (
        "quoin/core/workflow/handoff-format-reference.md",
        "quoin/dev/tests/test_core_workflow_portability_tokens.py",
    ),
    (
        "quoin/core/workflow/handoff-format-reference.md",
        "quoin/dev/tests/test_handoff_validate.py",
    ),
    # IVG-248 stage 2 T-14 (durable half of the C-03 critical): gate/SKILL.md
    # embeds the deploy-drift coverage qualifier VERBATIM, including the
    # literal "CLAUDE.md" inside its "not covered" clause, so a gate/SKILL.md
    # edit can stale the citation-sweep fixture the same way a memory/*.md
    # edit can — but until this row, only the memory/*.md and CLAUDE.md
    # sides of that sweep were selectable, not the adapter-SKILL.md side.
    (
        "quoin/adapters/claude/skills/gate/SKILL.md",
        "quoin/dev/tests/test_claude_md_citations.py",
    ),
    # This stage's agent-handoff envelope section and its emit
    # directives touch structural regions of run/SKILL.md that the named
    # structural guards plus several additive-edit traps already pin —
    # without these rows an affected-area /gate run on this file selects
    # none of them and the guards only run at the full suite.
    (
        "quoin/adapters/claude/skills/run/SKILL.md",
        "quoin/dev/tests/test_run_partial_continuation.py",
    ),
    (
        "quoin/adapters/claude/skills/run/SKILL.md",
        "quoin/dev/tests/test_run_fast_path_heading_freeze.py",
    ),
    (
        "quoin/adapters/claude/skills/run/SKILL.md",
        "quoin/dev/tests/test_plain_run_unchanged.py",
    ),
    (
        "quoin/adapters/claude/skills/run/SKILL.md",
        "quoin/dev/tests/test_autonomous_sentinel_contract.py",
    ),
    (
        "quoin/adapters/claude/skills/run/SKILL.md",
        "quoin/dev/tests/test_run_resume_idempotent.py",
    ),
    (
        "quoin/adapters/claude/skills/run/SKILL.md",
        "quoin/dev/tests/test_claude_md_citations.py",
    ),
    (
        "quoin/adapters/claude/skills/run/SKILL.md",
        "quoin/dev/tests/test_decision_gate_census.py",
    ),
    (
        "quoin/adapters/claude/skills/run/SKILL.md",
        "quoin/dev/tests/test_path_resolve_e2e.py",
    ),
    (
        "quoin/adapters/claude/skills/run/SKILL.md",
        "quoin/dev/tests/test_autonomous_hooks_untouched.py",
    ),
    (
        "quoin/adapters/claude/skills/run/SKILL.md",
        "quoin/dev/tests/test_budget_roster_census.py",
    ),
    (
        "quoin/adapters/claude/skills/run/SKILL.md",
        "quoin/dev/tests/test_handoff_producer_conformance.py",
    ),
    # This stage's envelope-versus-inline-summary branch and its
    # extended guard touch implement, review and thorough_plan's summary
    # sections — without these rows a Standard gate on any of the three
    # selects neither the section guard nor the producer-conformance guard.
    (
        "quoin/adapters/claude/skills/implement/SKILL.md",
        "quoin/dev/tests/test_inline_step_summary_present.py",
    ),
    (
        "quoin/adapters/claude/skills/implement/SKILL.md",
        "quoin/dev/tests/test_handoff_producer_conformance.py",
    ),
    (
        "quoin/adapters/claude/skills/review/SKILL.md",
        "quoin/dev/tests/test_inline_step_summary_present.py",
    ),
    (
        "quoin/adapters/claude/skills/review/SKILL.md",
        "quoin/dev/tests/test_handoff_producer_conformance.py",
    ),
    (
        "quoin/adapters/claude/skills/thorough_plan/SKILL.md",
        "quoin/dev/tests/test_inline_step_summary_present.py",
    ),
    (
        "quoin/adapters/claude/skills/thorough_plan/SKILL.md",
        "quoin/dev/tests/test_handoff_producer_conformance.py",
    ),
    # This stage's end_of_task fail-closed no-envelope clauses are
    # read by a guard function in test_inline_step_summary_present.py —
    # without this row the affected-area gate after this file's own edit
    # never runs the guard that covers it.
    (
        "quoin/adapters/claude/skills/end_of_task/SKILL.md",
        "quoin/dev/tests/test_inline_step_summary_present.py",
    ),
    # This stage's fail-closed no-envelope clauses in implement and
    # end_of_task sit alongside pre-existing decision-gate markers in both
    # files; the universal census guard covers them today only when
    # run/SKILL.md changes in the same diff (it already carries a row
    # above). These two rows make an implement- or end_of_task-only edit
    # select the census guard on its own.
    (
        "quoin/adapters/claude/skills/implement/SKILL.md",
        "quoin/dev/tests/test_decision_gate_census.py",
    ),
    (
        "quoin/adapters/claude/skills/end_of_task/SKILL.md",
        "quoin/dev/tests/test_decision_gate_census.py",
    ),
    # gate/SKILL.md's own no-envelope clause at its fail-closed site now sits
    # alongside pre-existing decision-gate markers, guarded by both the
    # inline-summary co-occurrence check (test_fail_closed_sites_emit_no_envelope,
    # which already reads gate) and the universal census. Neither of gate's two
    # existing rows (test_claude_md_citations.py, test_eot_resilience_contract.py)
    # selects either guard, so a gate-only edit deleting that clause would not
    # select the test that exists to catch it.
    (
        "quoin/adapters/claude/skills/gate/SKILL.md",
        "quoin/dev/tests/test_inline_step_summary_present.py",
    ),
    (
        "quoin/adapters/claude/skills/gate/SKILL.md",
        "quoin/dev/tests/test_decision_gate_census.py",
    ),
    # core/skills/run.md carried no selector row at all
    # before this stage. Four rows close the hole for the guards this
    # stage's edit class can break: the partial-continuation and autonomous-
    # sentinel structural guards, plus the two forbidden-token scanners the
    # portable-mirror edit must stay clean against.
    (
        "quoin/core/skills/run.md",
        "quoin/dev/tests/test_run_partial_continuation.py",
    ),
    (
        "quoin/core/skills/run.md",
        "quoin/dev/tests/test_autonomous_sentinel_contract.py",
    ),
    (
        "quoin/core/skills/run.md",
        "quoin/dev/tests/test_run_adapter_pilot.py",
    ),
    (
        "quoin/core/skills/run.md",
        "quoin/dev/tests/test_run_core_autonomous.py",
    ),
    # IVG-258 T-14: run-state wiring landed across three SKILL.md files.
    # cleanup/SKILL.md carried ZERO rows before this stage -- an edit there
    # selected no test at all -- and run/SKILL.md and thorough_plan/SKILL.md's
    # existing rows do not reach the new run-state wiring/precedence guards.
    (
        "quoin/adapters/claude/skills/cleanup/SKILL.md",
        "quoin/dev/tests/test_cleanup_runstate_sweep.py",
    ),
    (
        "quoin/adapters/claude/skills/cleanup/SKILL.md",
        "quoin/dev/tests/test_cleanup_sentinels.py",
    ),
    (
        "quoin/adapters/claude/skills/cleanup/SKILL.md",
        "quoin/dev/tests/test_cleanup_skill_structure.py",
    ),
    (
        "quoin/adapters/claude/skills/cleanup/SKILL.md",
        "quoin/dev/tests/test_sentinel_family_parity.py",
    ),
    (
        "quoin/adapters/claude/skills/run/SKILL.md",
        "quoin/dev/tests/test_run_state_wiring.py",
    ),
    (
        "quoin/adapters/claude/skills/run/SKILL.md",
        "quoin/dev/tests/test_run_state_resume_precedence.py",
    ),
    (
        "quoin/adapters/claude/skills/thorough_plan/SKILL.md",
        "quoin/dev/tests/test_run_state_wiring.py",
    ),
    # Task-bookkeeping pass prose spans cleanup/SKILL.md,
    # checkpoint/SKILL.md, the runtime-neutral core doc, the reference doc,
    # and README.md's Utilities row -- none of their existing rows reach the
    # bookkeeping-pass slicer assertions.
    (
        "quoin/adapters/claude/skills/cleanup/SKILL.md",
        "quoin/dev/tests/test_cleanup_bookkeeping_skill.py",
    ),
    (
        "quoin/adapters/claude/skills/checkpoint/SKILL.md",
        "quoin/dev/tests/test_cleanup_bookkeeping_skill.py",
    ),
    (
        "quoin/core/skills/cleanup.md",
        "quoin/dev/tests/test_cleanup_bookkeeping_skill.py",
    ),
    (
        "quoin/memory/cleanup-task-bookkeeping.md",
        "quoin/dev/tests/test_cleanup_bookkeeping_skill.py",
    ),
    (
        "README.md",
        "quoin/dev/tests/test_cleanup_bookkeeping_skill.py",
    ),
    # The frozen POSIX-sh reader body lives outside quoin/dev/tests/ (it is a
    # spike file, not a .py source file), so an edit to its bytes alone was
    # NOT selectable by the generic .py-mtime rule and fell through to the
    # "ignored" bucket, though test_run_state_writer.py round-trips against
    # these exact bytes via the writer-to-reader interop tests.
    (
        "quoin/dev/spikes/run_state_read.sh",
        "quoin/dev/tests/test_run_state_writer.py",
    ),
    # IVG-258 stage 5 (T-07): no row existed for anything under quoin/hooks/,
    # so a hooks-only diff selected none of the tests that pin it at a
    # Standard gate.
    (
        "quoin/hooks/postcompact.sh",
        "quoin/dev/tests/test_compaction_telemetry.py",
    ),
    (
        "quoin/hooks/precompact.sh",
        "quoin/dev/tests/test_compaction_telemetry.py",
    ),
    (
        "quoin/hooks/_lib.sh",
        "quoin/dev/tests/test_compaction_telemetry.py",
    ),
    # round-1 critic MIN-4: T-01 (stage 5) edits _lib.sh and these two generic
    # hooks-hygiene guards had no row today.
    (
        "quoin/hooks/_lib.sh",
        "quoin/dev/tests/test_autonomous_hooks_untouched.py",
    ),
    (
        "quoin/hooks/_lib.sh",
        "quoin/dev/tests/test_lib_thresholds_unchanged.py",
    ),
    (
        "quoin/memory/hooks-table.md",
        "quoin/dev/tests/test_hook_stanza_count_parity.py",
    ),
    (
        "quoin/memory/hooks-table.md",
        "quoin/dev/tests/test_compaction_telemetry.py",
    ),
    # round-1 critic MIN-4: T-08 (stage 5) edits lifecycle-guide.md and this
    # guard had no row today.
    (
        "quoin/memory/lifecycle-guide.md",
        "quoin/dev/tests/test_lifecycle_guide_same_session_docs.py",
    ),
    # Every new non-Python file under the opencode adapter tree needs a row
    # here in the same change; the self-check test in test_probe_gateway.py
    # enforces that rule.
    (
        "quoin/adapters/opencode/feature-manifest.json",
        "quoin/dev/tests/test_opencode_manifest.py",
    ),
    (
        "quoin/adapters/opencode/feature-manifest.json",
        "quoin/dev/tests/test_opencode_docs.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/scenarios.json",
        "quoin/dev/tests/test_fake_openai_server.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/scenarios.json",
        "quoin/dev/tests/test_probe_gateway.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/scenarios.json",
        "quoin/dev/tests/test_probe_gateway_tool_loop.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/install-cases.json",
        "quoin/dev/tests/test_opencode_install.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/install-cases.json",
        "quoin/dev/tests/test_opencode_docs.py",
    ),
    # Runtime event fixtures: replayed by the fixture tests and read by the
    # shipped-text scans in the docs tests.
    (
        "quoin/adapters/opencode/fixtures/runtime-events/cases.json",
        "quoin/dev/tests/test_opencode_event_fixtures.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-events/cases.json",
        "quoin/dev/tests/test_opencode_docs.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-events/doom-loop-denied.jsonl",
        "quoin/dev/tests/test_opencode_event_fixtures.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-events/doom-loop-denied.jsonl",
        "quoin/dev/tests/test_opencode_docs.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-events/doom-loop-rejected.jsonl",
        "quoin/dev/tests/test_opencode_event_fixtures.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-events/doom-loop-rejected.jsonl",
        "quoin/dev/tests/test_opencode_docs.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-events/duplicate-lines.jsonl",
        "quoin/dev/tests/test_opencode_event_fixtures.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-events/duplicate-lines.jsonl",
        "quoin/dev/tests/test_opencode_docs.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-events/malformed.jsonl",
        "quoin/dev/tests/test_opencode_event_fixtures.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-events/malformed.jsonl",
        "quoin/dev/tests/test_opencode_docs.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-events/native-error-auth.jsonl",
        "quoin/dev/tests/test_opencode_event_fixtures.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-events/native-error-auth.jsonl",
        "quoin/dev/tests/test_opencode_docs.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-events/native-error-connect.jsonl",
        "quoin/dev/tests/test_opencode_event_fixtures.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-events/native-error-connect.jsonl",
        "quoin/dev/tests/test_opencode_docs.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-events/native-error-http-429.jsonl",
        "quoin/dev/tests/test_opencode_event_fixtures.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-events/native-error-http-429.jsonl",
        "quoin/dev/tests/test_opencode_docs.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-events/native-error-http-503.jsonl",
        "quoin/dev/tests/test_opencode_event_fixtures.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-events/native-error-http-503.jsonl",
        "quoin/dev/tests/test_opencode_docs.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-events/native-error-unknown.jsonl",
        "quoin/dev/tests/test_opencode_event_fixtures.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-events/native-error-unknown.jsonl",
        "quoin/dev/tests/test_opencode_docs.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-events/plain-complete.jsonl",
        "quoin/dev/tests/test_opencode_event_fixtures.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-events/plain-complete.jsonl",
        "quoin/dev/tests/test_opencode_docs.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-events/reasoning.jsonl",
        "quoin/dev/tests/test_opencode_event_fixtures.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-events/reasoning.jsonl",
        "quoin/dev/tests/test_opencode_docs.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-events/replayed-stream.jsonl",
        "quoin/dev/tests/test_opencode_event_fixtures.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-events/replayed-stream.jsonl",
        "quoin/dev/tests/test_opencode_docs.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-events/revised-part.jsonl",
        "quoin/dev/tests/test_opencode_event_fixtures.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-events/revised-part.jsonl",
        "quoin/dev/tests/test_opencode_docs.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-events/stderr-agent-fallback.txt",
        "quoin/dev/tests/test_opencode_event_fixtures.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-events/stderr-agent-fallback.txt",
        "quoin/dev/tests/test_opencode_docs.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-events/stderr-approval-notice.txt",
        "quoin/dev/tests/test_opencode_event_fixtures.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-events/stderr-approval-notice.txt",
        "quoin/dev/tests/test_opencode_docs.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-events/task-background.jsonl",
        "quoin/dev/tests/test_opencode_event_fixtures.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-events/task-background.jsonl",
        "quoin/dev/tests/test_opencode_docs.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-events/task-errored-denied-tail.jsonl",
        "quoin/dev/tests/test_opencode_event_fixtures.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-events/task-errored-denied-tail.jsonl",
        "quoin/dev/tests/test_opencode_docs.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-events/task-errored-nested-rejection.jsonl",
        "quoin/dev/tests/test_opencode_event_fixtures.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-events/task-errored-nested-rejection.jsonl",
        "quoin/dev/tests/test_opencode_docs.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-events/task-errored-other.jsonl",
        "quoin/dev/tests/test_opencode_event_fixtures.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-events/task-errored-other.jsonl",
        "quoin/dev/tests/test_opencode_docs.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-events/task-errored-rejection.jsonl",
        "quoin/dev/tests/test_opencode_event_fixtures.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-events/task-errored-rejection.jsonl",
        "quoin/dev/tests/test_opencode_docs.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-events/task-promoted.jsonl",
        "quoin/dev/tests/test_opencode_event_fixtures.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-events/task-promoted.jsonl",
        "quoin/dev/tests/test_opencode_docs.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-events/task-sync-completed.jsonl",
        "quoin/dev/tests/test_opencode_event_fixtures.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-events/task-sync-completed.jsonl",
        "quoin/dev/tests/test_opencode_docs.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-events/tool-denied-adversarial.jsonl",
        "quoin/dev/tests/test_opencode_event_fixtures.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-events/tool-denied-adversarial.jsonl",
        "quoin/dev/tests/test_opencode_docs.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-events/tool-denied-then-finish.jsonl",
        "quoin/dev/tests/test_opencode_event_fixtures.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-events/tool-denied-then-finish.jsonl",
        "quoin/dev/tests/test_opencode_docs.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-events/tool-rejected.jsonl",
        "quoin/dev/tests/test_opencode_event_fixtures.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-events/tool-rejected.jsonl",
        "quoin/dev/tests/test_opencode_docs.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-events/unknown-type.jsonl",
        "quoin/dev/tests/test_opencode_event_fixtures.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-events/unknown-type.jsonl",
        "quoin/dev/tests/test_opencode_docs.py",
    ),
    # Runtime configuration schema, fixtures and their case inventory: any change
    # here re-runs the loader tests, and the schema also re-runs the validator and
    # the standard-validator cross-check.
    (
        "quoin/adapters/opencode/schemas/runtime-config.schema.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/schemas/runtime-config.schema.json",
        "quoin/dev/tests/test_opencode_schema_check.py",
    ),
    (
        "quoin/adapters/opencode/schemas/runtime-config.schema.json",
        "quoin/dev/tests/test_opencode_schema_jsonschema_crosscheck.py",
    ),
    # The native configuration subset schema and the compiled reference
    # documents: a change re-runs the schema, compiler and closure tests.
    (
        "quoin/adapters/opencode/schemas/opencode-1.18.32-config.subset.schema.json",
        "quoin/dev/tests/test_opencode_native_schema.py",
    ),
    (
        "quoin/adapters/opencode/schemas/opencode-1.18.32-config.subset.schema.json",
        "quoin/dev/tests/test_opencode_schema_check.py",
    ),
    (
        "quoin/adapters/opencode/schemas/opencode-1.18.32-config.subset.schema.json",
        "quoin/dev/tests/test_opencode_schema_jsonschema_crosscheck.py",
    ),
    (
        "quoin/adapters/opencode/schemas/opencode-1.18.32-config.subset.schema.json",
        "quoin/dev/tests/test_opencode_compiler.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/compiled/work.opencode.json",
        "quoin/dev/tests/test_opencode_compiler.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/compiled/work.opencode.json",
        "quoin/dev/tests/test_opencode_native_schema.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/compiled/work.opencode.json",
        "quoin/dev/tests/test_opencode_docs.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/compiled/work-variants.opencode.json",
        "quoin/dev/tests/test_opencode_compiler.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/compiled/work-variants.opencode.json",
        "quoin/dev/tests/test_opencode_native_schema.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/compiled/work-variants.opencode.json",
        "quoin/dev/tests/test_opencode_docs.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/compiled/personal.opencode.json",
        "quoin/dev/tests/test_opencode_compiler.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/compiled/personal.opencode.json",
        "quoin/dev/tests/test_opencode_native_schema.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/compiled/personal.opencode.json",
        "quoin/dev/tests/test_opencode_docs.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/cases.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/invalid/cross-profile-fallback-enabled.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/invalid/dangling-allowed-provider.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/invalid/dangling-default-model.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/invalid/dangling-model-provider.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/invalid/dangling-project-role-model.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/invalid/dangling-role-model.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/invalid/duplicate-key-nested.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/invalid/endpoint-identity-change.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/invalid/env-name-collision.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/invalid/inline-credential-ref.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/invalid/inline-credential-shape.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/invalid/insecure-http.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/invalid/invalid-effort.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/invalid/invalid-json-nan.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/invalid/invalid-json-overflow.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/invalid/invalid-json-too-deep.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/invalid/invalid-json.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/invalid/invalid-limit-bool.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/invalid/invalid-limit-float.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/invalid/invalid-limit-zero.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/invalid/invalid-profile-name-pattern.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/invalid/invalid-profile-name-stem.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/invalid/invalid-type-openrouter-responses.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/invalid/invalid-type-provider-kind.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/invalid/invalid-url-port.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/invalid/invalid-url-host-backslash.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/invalid/invalid-url-host-percent.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/invalid/invalid-url-port-zero.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/invalid/invalid-qualification-ref-newline.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/invalid/profile-not-found.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/invalid/project-declares-endpoint-models.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/invalid/project-declares-endpoint-providers.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/invalid/unknown-key-managed-roles.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/invalid/unknown-key-provider.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/invalid/unknown-role.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/invalid/unknown-runtime.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/invalid/unresolved-placeholder-file.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/invalid/unresolved-placeholder-replace-with.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/invalid/unsupported-schema-version.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/invalid/url-credentials-query.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/invalid/url-credentials-userinfo.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/valid/managed-work.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/valid/profile-minimal.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/valid/profile-personal.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/valid/profile-work.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/valid/project-personal.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/valid/project-unclassified.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/valid/project-work.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    # Fixtures that exercise merging and role resolution as well as loading.
    (
        "quoin/adapters/opencode/fixtures/runtime-config/invalid/allowlist-broadening-host.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/invalid/allowlist-broadening-provider.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/invalid/limit-above-ceiling.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/invalid/limit-above-profile.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/invalid/missing-classification-unknown.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/invalid/missing-classification.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/invalid/personal-profile-for-work.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/invalid/personal-provider-kind-for-work.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/valid/managed-strict.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/valid/project-work-narrow.json",
        "quoin/dev/tests/test_opencode_runtime_config.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/valid/managed-strict.json",
        "quoin/dev/tests/test_opencode_role_resolution.py",
    ),
    (
        "quoin/adapters/opencode/fixtures/runtime-config/valid/project-work-narrow.json",
        "quoin/dev/tests/test_opencode_role_resolution.py",
    ),
    (
        ".github/workflows/adapter-check.yml",
        "quoin/dev/tests/test_opencode_manifest.py",
    ),
    # Bare filename match: this row also matches any project's own
    # top-level pyproject.toml once this script is deployed there, since
    # the allowlist is a flat suffix match with no directory-prefix rule
    # (see the module note below). That is safe: a match only takes the
    # file out of "ignored" when the mapped test path actually exists on
    # disk (see the mapped_any check below), so in a project without this
    # test file the row simply does not fire.
    (
        "pyproject.toml",
        "quoin/dev/tests/test_probe_gateway.py",
    ),
    (
        "quoin/adapters/opencode/README.md",
        "quoin/dev/tests/test_probe_gateway.py",
    ),
    (
        "quoin/adapters/opencode/compatibility.md",
        "quoin/dev/tests/test_opencode_docs.py",
    ),
    (
        "quoin/adapters/opencode/compatibility.md",
        "quoin/dev/tests/test_opencode_manifest.py",
    ),
    (
        "quoin/adapters/opencode/compatibility.md",
        "quoin/dev/tests/test_opencode_native_schema.py",
    ),
    (
        "quoin/adapters/opencode/decisions.md",
        "quoin/dev/tests/test_opencode_docs.py",
    ),
    (
        "quoin/adapters/opencode/README.md",
        "quoin/dev/tests/test_opencode_docs.py",
    ),
    (
        "quoin/adapters/opencode/README.md",
        "quoin/dev/tests/test_opencode_manifest.py",
    ),
    (
        "quoin/adapters/README.md",
        "quoin/dev/tests/test_opencode_docs.py",
    ),
    (
        "quoin/adapters/README.md",
        "quoin/dev/tests/test_runtime_portability_docs.py",
    ),
    (
        "quoin/docs/runtime-portability-status.md",
        "quoin/dev/tests/test_opencode_docs.py",
    ),
    (
        "quoin/docs/runtime-portability-status.md",
        "quoin/dev/tests/test_runtime_portability_docs.py",
    ),
    # IVG-280 (T-09): the four run-continuation hook scripts are .sh, not
    # .py, so the generic mtime rule never selected their behavioral
    # fixtures or the token-clean guard on a hooks-only diff.
    (
        "quoin/hooks/stop.sh",
        "quoin/dev/tests/test_auto_resume_hook_shell_suite.py",
    ),
    (
        "quoin/hooks/stop.sh",
        "quoin/dev/tests/test_autonomous_hooks_untouched.py",
    ),
    (
        "quoin/hooks/sessionstart.sh",
        "quoin/dev/tests/test_auto_resume_hook_shell_suite.py",
    ),
    (
        "quoin/hooks/sessionstart.sh",
        "quoin/dev/tests/test_autonomous_hooks_untouched.py",
    ),
    (
        "quoin/hooks/userpromptsubmit.sh",
        "quoin/dev/tests/test_auto_resume_hook_shell_suite.py",
    ),
    (
        "quoin/hooks/userpromptsubmit.sh",
        "quoin/dev/tests/test_autonomous_hooks_untouched.py",
    ),
    (
        "quoin/hooks/sessionend.sh",
        "quoin/dev/tests/test_auto_resume_hook_shell_suite.py",
    ),
    (
        "quoin/hooks/sessionend.sh",
        "quoin/dev/tests/test_autonomous_hooks_untouched.py",
    ),
    # run/SKILL.md and autonomous-mode.md's run-continuation wiring
    # (IVG-280 T-07) has its own dedicated contract pin.
    (
        "quoin/adapters/claude/skills/run/SKILL.md",
        "quoin/dev/tests/test_run_skill_auto_resume_contract.py",
    ),
    (
        "quoin/memory/autonomous-mode.md",
        "quoin/dev/tests/test_run_skill_auto_resume_contract.py",
    ),
    (
        "quoin/docs/hooks-guide.md",
        "quoin/dev/tests/test_hook_stanza_count_parity.py",
    ),
    (
        "quoin/docs/runtime-parity-matrix.md",
        "quoin/dev/tests/test_opencode_docs.py",
    ),
    (
        "quoin/docs/runtime-parity-matrix.md",
        "quoin/dev/tests/test_runtime_portability_docs.py",
    ),
    (
        "quoin/core/workflow/skills.json",
        "quoin/dev/tests/test_opencode_manifest.py",
    ),
    (
        "quoin/adapters/opencode/feature-manifest.json",
        "quoin/dev/tests/test_opencode_generate.py",
    ),
    (
        "quoin/adapters/opencode/overlays.json",
        "quoin/dev/tests/test_opencode_generate.py",
    ),
    (
        "quoin/adapters/opencode/overlays.json",
        "quoin/dev/tests/test_opencode_docs.py",
    ),
    (
        "quoin/adapters/opencode/templates/command.md",
        "quoin/dev/tests/test_opencode_generate.py",
    ),
    (
        "quoin/adapters/opencode/templates/command.md",
        "quoin/dev/tests/test_opencode_docs.py",
    ),
    (
        "quoin/adapters/opencode/templates/skill.md",
        "quoin/dev/tests/test_opencode_generate.py",
    ),
    (
        "quoin/adapters/opencode/templates/skill.md",
        "quoin/dev/tests/test_opencode_docs.py",
    ),
    (
        "quoin/adapters/opencode/templates/agent.md",
        "quoin/dev/tests/test_opencode_generate.py",
    ),
    (
        "quoin/adapters/opencode/templates/agent.md",
        "quoin/dev/tests/test_opencode_docs.py",
    ),
    (
        "quoin/adapters/opencode/templates/instructions.md",
        "quoin/dev/tests/test_opencode_generate.py",
    ),
    (
        "quoin/adapters/opencode/templates/instructions.md",
        "quoin/dev/tests/test_opencode_docs.py",
    ),
    # The generator reads rules.md, skills.json and every supported bundle
    # contract at render time (`generate.load_inputs`), so an edit to any of
    # them needs to select the generator's own test file too.
    (
        "quoin/core/workflow/rules.md",
        "quoin/dev/tests/test_opencode_generate.py",
    ),
    (
        "quoin/core/workflow/skills.json",
        "quoin/dev/tests/test_opencode_generate.py",
    ),
    (
        "quoin/core/skills/architect.md",
        "quoin/dev/tests/test_opencode_generate.py",
    ),
    (
        "quoin/core/skills/checkpoint.md",
        "quoin/dev/tests/test_opencode_generate.py",
    ),
    (
        "quoin/core/skills/continue_work.md",
        "quoin/dev/tests/test_opencode_generate.py",
    ),
    (
        "quoin/core/skills/critic.md",
        "quoin/dev/tests/test_opencode_generate.py",
    ),
    (
        "quoin/core/skills/discover.md",
        "quoin/dev/tests/test_opencode_generate.py",
    ),
    (
        "quoin/core/skills/end_of_task.md",
        "quoin/dev/tests/test_opencode_generate.py",
    ),
    (
        "quoin/core/skills/gate.md",
        "quoin/dev/tests/test_opencode_generate.py",
    ),
    (
        "quoin/core/skills/implement.md",
        "quoin/dev/tests/test_opencode_generate.py",
    ),
    (
        "quoin/core/skills/plan.md",
        "quoin/dev/tests/test_opencode_generate.py",
    ),
    (
        "quoin/core/skills/review.md",
        "quoin/dev/tests/test_opencode_generate.py",
    ),
    (
        "quoin/core/skills/thorough_plan.md",
        "quoin/dev/tests/test_opencode_generate.py",
    ),
    # Completion-marker, retry-budget and headless no-yield prose guards.
    (
        "quoin/adapters/claude/skills/run/SKILL.md",
        "quoin/dev/tests/test_run_tasks_complete_entry.py",
    ),
    (
        "quoin/adapters/claude/skills/run/SKILL.md",
        "quoin/dev/tests/test_headless_no_yield_rule.py",
    ),
    (
        "quoin/adapters/claude/skills/implement/SKILL.md",
        "quoin/dev/tests/test_implement_autonomous.py",
    ),
    (
        "quoin/adapters/claude/skills/gate/SKILL.md",
        "quoin/dev/tests/test_headless_no_yield_rule.py",
    ),
    (
        "quoin/core/skills/gate.md",
        "quoin/dev/tests/test_headless_no_yield_rule.py",
    ),
    (
        "quoin/memory/autonomous-mode.md",
        "quoin/dev/tests/test_headless_no_yield_rule.py",
    ),
    (
        "quoin/core/skills/run.md",
        "quoin/dev/tests/test_run_tasks_complete_entry.py",
    ),
    # Opt-in context-tracker mod: its non-.py files would otherwise be ignored
    # by map_changed_to_tests. No package.json is added, so the CI mirror keeps
    # treating the plugin as having no deliverable of its own.
    (
        "quoin/plugins/context-tracker/.claude-plugin/plugin.json",
        "quoin/dev/tests/test_context_tracker_install.py",
    ),
    (
        "quoin/plugins/context-tracker/.claude-plugin/plugin.json",
        "quoin/dev/tests/test_context_tracker_plugin_ts.py",
    ),
    (
        "quoin/plugins/context-tracker/hooks/hooks.json",
        "quoin/dev/tests/test_context_tracker_install.py",
    ),
    (
        "quoin/plugins/context-tracker/hooks/hooks.json",
        "quoin/dev/tests/test_context_tracker_plugin_ts.py",
    ),
    (
        "quoin/plugins/context-tracker/hooks/register.test.ts",
        "quoin/dev/tests/test_context_tracker_install.py",
    ),
    (
        "quoin/plugins/context-tracker/hooks/register.test.ts",
        "quoin/dev/tests/test_context_tracker_plugin_ts.py",
    ),
    (
        "quoin/plugins/context-tracker/hooks/register.tsx",
        "quoin/dev/tests/test_context_tracker_install.py",
    ),
    (
        "quoin/plugins/context-tracker/hooks/register.tsx",
        "quoin/dev/tests/test_context_tracker_plugin_ts.py",
    ),
    (
        "quoin/plugins/context-tracker/tsconfig.json",
        "quoin/dev/tests/test_context_tracker_install.py",
    ),
    (
        "quoin/plugins/context-tracker/tsconfig.json",
        "quoin/dev/tests/test_context_tracker_plugin_ts.py",
    ),
    (
        "quoin/plugins/context-tracker/types/index.d.ts",
        "quoin/dev/tests/test_context_tracker_install.py",
    ),
    (
        "quoin/plugins/context-tracker/types/index.d.ts",
        "quoin/dev/tests/test_context_tracker_plugin_ts.py",
    ),
    (
        "quoin/install.sh",
        "quoin/dev/tests/test_context_tracker_install.py",
    ),
    (
        "quoin/docs/hooks-guide.md",
        "quoin/dev/tests/test_context_tracker_install.py",
    ),
    # Opt-in workflow-tasks mod: same reasoning as the context-tracker rows. The
    # shared fixture table, the pure logic and its tests also feed the Python
    # parity check against status_graph.detect_phase.
    (
        "quoin/plugins/workflow-tasks/.claude-plugin/plugin.json",
        "quoin/dev/tests/test_workflow_tasks_install.py",
    ),
    (
        "quoin/plugins/workflow-tasks/.claude-plugin/plugin.json",
        "quoin/dev/tests/test_workflow_tasks_plugin_ts.py",
    ),
    (
        "quoin/plugins/workflow-tasks/hooks/hooks.json",
        "quoin/dev/tests/test_workflow_tasks_install.py",
    ),
    (
        "quoin/plugins/workflow-tasks/hooks/hooks.json",
        "quoin/dev/tests/test_workflow_tasks_plugin_ts.py",
    ),
    (
        "quoin/plugins/workflow-tasks/hooks/register.test.ts",
        "quoin/dev/tests/test_workflow_tasks_install.py",
    ),
    (
        "quoin/plugins/workflow-tasks/hooks/register.test.ts",
        "quoin/dev/tests/test_workflow_tasks_plugin_ts.py",
    ),
    (
        "quoin/plugins/workflow-tasks/hooks/register.tsx",
        "quoin/dev/tests/test_workflow_tasks_install.py",
    ),
    (
        "quoin/plugins/workflow-tasks/hooks/register.tsx",
        "quoin/dev/tests/test_workflow_tasks_plugin_ts.py",
    ),
    (
        "quoin/plugins/workflow-tasks/hooks/stage-fixtures.ts",
        "quoin/dev/tests/test_workflow_tasks_install.py",
    ),
    (
        "quoin/plugins/workflow-tasks/hooks/stage-fixtures.ts",
        "quoin/dev/tests/test_workflow_tasks_plugin_ts.py",
    ),
    (
        "quoin/plugins/workflow-tasks/hooks/stage-fixtures.ts",
        "quoin/dev/tests/test_workflow_tasks_stage_parity.py",
    ),
    (
        "quoin/plugins/workflow-tasks/hooks/tasks.test.ts",
        "quoin/dev/tests/test_workflow_tasks_install.py",
    ),
    (
        "quoin/plugins/workflow-tasks/hooks/tasks.test.ts",
        "quoin/dev/tests/test_workflow_tasks_plugin_ts.py",
    ),
    (
        "quoin/plugins/workflow-tasks/hooks/tasks.test.ts",
        "quoin/dev/tests/test_workflow_tasks_stage_parity.py",
    ),
    (
        "quoin/plugins/workflow-tasks/hooks/tasks.ts",
        "quoin/dev/tests/test_workflow_tasks_install.py",
    ),
    (
        "quoin/plugins/workflow-tasks/hooks/tasks.ts",
        "quoin/dev/tests/test_workflow_tasks_plugin_ts.py",
    ),
    (
        "quoin/plugins/workflow-tasks/hooks/tasks.ts",
        "quoin/dev/tests/test_workflow_tasks_stage_parity.py",
    ),
    (
        "quoin/plugins/workflow-tasks/tsconfig.json",
        "quoin/dev/tests/test_workflow_tasks_install.py",
    ),
    (
        "quoin/plugins/workflow-tasks/tsconfig.json",
        "quoin/dev/tests/test_workflow_tasks_plugin_ts.py",
    ),
    (
        "quoin/plugins/workflow-tasks/types/index.d.ts",
        "quoin/dev/tests/test_workflow_tasks_install.py",
    ),
    (
        "quoin/plugins/workflow-tasks/types/index.d.ts",
        "quoin/dev/tests/test_workflow_tasks_plugin_ts.py",
    ),
    (
        "quoin/install.sh",
        "quoin/dev/tests/test_workflow_tasks_install.py",
    ),
    (
        "quoin/docs/hooks-guide.md",
        "quoin/dev/tests/test_workflow_tasks_install.py",
    ),
    # Install questions and the agentdesk setup flag.
    (
        "quoin/install.sh",
        "quoin/dev/tests/test_install_questions_cli.py",
    ),
    (
        "quoin/docs/hooks-guide.md",
        "quoin/dev/tests/test_install_questions_cli.py",
    ),
    (
        "README.md",
        "quoin/dev/tests/test_install_questions_cli.py",
    ),
    (
        "quoin/install.sh",
        "quoin/dev/tests/test_agentdesk_setup_flag.py",
    ),
    (
        "quoin/tools/agentdesk/setup-agentdesk.sh",
        "quoin/dev/tests/test_install_prompts.py",
    ),
)

# SKILL.md coverage residual gap (review-1.md MAJOR 2, documented-acceptance branch):
# _DOCS_TO_TESTS is a flat, per-file allowlist — there is no directory-prefix rule,
# so an edit to any of the remaining 30 adapter SKILL.md files (the third in-scope
# citation-sweep corpus, alongside the two rows above) is NOT selectable for
# test_claude_md_citations.py and still falls through to the generic non-.py
# "ignored" bucket for that sweep specifically (test_unrelated_skill_md_still_ignored
# pins this as expected, not a bug); gate/SKILL.md is covered by the row above
# (IVG-248 stage 2 T-14). A directory-prefix rule would widen _DOCS_TO_TESTS's
# matching semantics for every existing row, a larger behavior change than this
# seam-local fix round's scope; the residual gap is accepted here rather than
# silently left undocumented. The full-suite gate (not affected-area) remains
# the backstop that catches a SKILL.md edit that stales the citation fixture.
#
# Orthogonal addition (IVG-249 S-02): four SKILL.md files (run, thorough_plan,
# architect, end_of_task) now carry rows above selecting the on-behalf guard
# tests; this does NOT make them selectable for the citation sweep
# (test_claude_md_citations.py), which remains gapped for the remaining 30
# adapter SKILL.md files exactly as before.
#
# Orthogonal addition (IVG-249 S-03 T-07): gate/SKILL.md (plus end_of_task and
# run, already selectable above) now also carries rows selecting
# test_eot_resilience_contract.py / test_run_inline_finish.py. gate/SKILL.md is
# therefore no longer a member of the residual gap's "wholly unselectable"
# exemplar set — test_skill_md_still_not_selectable_documented_residual_gap
# (test_affected_tests.py:342) moved its "lands in ignored" assertion to
# critic/SKILL.md, which still carries no _DOCS_TO_TESTS row at all. gate/SKILL.md
# itself later gained a citation-sweep row (IVG-248 stage 2 T-14, see above), so
# test_claude_md_citations.py now remains gapped for the remaining 30 adapter
# SKILL.md files, not all 32 — only gate/SKILL.md's general unselectability, and
# later its citation coverage, changed.
#
# Orthogonal addition (IVG-123): the 9 spawn-target preamble.md files
# (quoin/skills/<skill>/preamble.md) are now covered per-file above, selecting
# test_preamble_freshness.py. This is unrelated to the SKILL.md citation-sweep
# gap described above — preamble.md and SKILL.md are different files in the
# same skill directory, and the citation-sweep gap for the remaining 30 adapter
# SKILL.md files is unchanged.
#
# Orthogonal addition (this stage): run/SKILL.md now also carries a
# citation-sweep row (see above), alongside gate/SKILL.md's existing one, so
# the residual gap for test_claude_md_citations.py narrows from 31 remaining
# adapter SKILL.md files to 30. The same commit also gives implement, review,
# thorough_plan and end_of_task rows for the new envelope-branch guard, and
# core/skills/run.md its first four rows ever — none of which touch the
# citation-sweep gap described above, which remains about that one sweep only.


# ---------------------------------------------------------------------------
# Non-collectable allowlist (FR-6 / AC-6)
# ---------------------------------------------------------------------------
#
# A committed allowlist of intentionally-non-collectable .py files lives at
# quoin/dev/tests/non-collectable.txt (repo-root-relative).  A changed file that
# matches an allowlist entry is routed to the `noncollectable` bucket instead of
# `unmatched_sources`/selectors, so an uncollectable non-test .py never drives
# exit 3 and an allowlisted collect-nothing test spike never reaches pytest.
#
# Matching MIRRORS the _DOCS_TO_TESTS anchoring idiom verbatim:
#   posix == entry OR posix.endswith("/" + entry).
# Absent/unreadable file degrades to an EMPTY allowlist (FR-9 fail-safe): repos
# without the file behave byte-for-byte as before.

_NONCOLLECTABLE_REL = "quoin/dev/tests/non-collectable.txt"


def _parse_noncollectable(text: str) -> list[str]:
    """Parse allowlist file text into effective entries.

    Drops blank lines and comment lines (stripped form starts with `#`).
    Returns the remaining stripped entries in file order.
    """
    entries: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        entries.append(stripped)
    return entries


def _noncollectable_path(repo_root: Path) -> Path:
    """Resolve the allowlist path.

    QUOIN_NONCOLLECTABLE_FILE (non-empty) wins as an absolute override; otherwise
    resolve repo-root-relative (repo_root / quoin/dev/tests/non-collectable.txt) —
    NOT relative to the deployed __file__, so a self-hosting quoin run finds its
    own committed list and a foreign repo without the file degrades to empty.
    """
    override = os.environ.get("QUOIN_NONCOLLECTABLE_FILE", "").strip()
    if override:
        return Path(override)
    return repo_root / _NONCOLLECTABLE_REL


def load_noncollectable(repo_root: Path) -> list[str]:
    """Load + parse the allowlist; absent/unreadable file → [] (fail-safe)."""
    path = _noncollectable_path(repo_root)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    return _parse_noncollectable(text)


def _is_noncollectable(changed_path: str, entries: list[str]) -> bool:
    """True if changed_path matches any allowlist entry (anchored, MIRRORS _DOCS_TO_TESTS)."""
    posix = PurePosixPath(changed_path).as_posix()
    for entry in entries:
        if posix == entry or posix.endswith("/" + entry):
            return True
    return False


def partition_noncollectable(
    changed: list[str],
    entries: list[str],
) -> tuple[list[str], list[str]]:
    """Split changed into (remaining, noncollectable), preserving input order.

    An empty allowlist (the default for repos without the file) leaves `changed`
    entirely in `remaining` — byte-for-byte the pre-change behavior.
    """
    remaining: list[str] = []
    noncollectable: list[str] = []
    for path in changed:
        if entries and _is_noncollectable(path, entries):
            noncollectable.append(path)
        else:
            remaining.append(path)
    return remaining, noncollectable


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class Selection:
    """Result of mapping changed files to test selectors."""
    changed: list[str]
    selectors: list[str]          # sorted, deduplicated test file paths
    unmatched_sources: list[str]  # .py sources with zero matched test
    ignored: list[str]            # non-.py files (docs, JSON, SKILL.md, ...)
    ran_pytest: bool
    pytest_returncode: int | None
    exit_reason: str              # see exit code doc above
    unmatched_warning: bool = False  # set when --allow-unmatched in use
    # Files matched by the committed non-collectable allowlist
    # (quoin/dev/tests/non-collectable.txt).  These are intentionally-uncollectable
    # .py files (collect-nothing test spikes or designated non-test sources) that
    # must NEVER become a pytest selector or an unmatched_source — routing them here
    # kills the spurious exit-3/hard-RED false block (FR-6/AC-6).  Placed AFTER
    # unmatched_warning to satisfy dataclass default-ordering.
    noncollectable: list[str] = dataclasses.field(default_factory=list)
    # Changed test files that do not exist on disk: deleted by the change, or a
    # wrong path passed via --files.  They have nothing to run, so they never
    # become selectors, but they are always reported by name.
    missing_tests: list[str] = dataclasses.field(default_factory=list)
    # Interpreter the pytest subprocess ran (or would run) under, and why it
    # was selected — see resolve_python().  Both default to "" so every
    # pre-resolution Selection() call site stays untouched and both fields
    # are omitted from output until a caller actually resolves an interpreter.
    interpreter: str = ""
    interpreter_reason: str = ""
    # Interpreters found but unusable, one "<kind>: <path> (<detail>)" entry
    # each; omitted from output when empty.
    interpreter_problems: list[str] = dataclasses.field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "changed": self.changed,
            "selectors": self.selectors,
            "unmatched_sources": self.unmatched_sources,
            "ignored": self.ignored,
            "noncollectable": self.noncollectable,
            "missing_tests": self.missing_tests,
            "ran_pytest": self.ran_pytest,
            "pytest_returncode": self.pytest_returncode,
            "exit_reason": self.exit_reason,
        }
        if self.unmatched_warning:
            d["unmatched_warning"] = True
        if self.interpreter:
            d["interpreter"] = self.interpreter
        if self.interpreter_reason:
            d["interpreter_reason"] = self.interpreter_reason
        if self.interpreter_problems:
            d["interpreter_problems"] = self.interpreter_problems
        return d


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def _subprocess_timeout() -> int:
    """Read QUOIN_SUBPROCESS_TIMEOUT (seconds); default 30; bad values fall back to 30.

    Self-contained local copy (D-06) — do NOT cross-import; each touched core
    script owns its own copy per the repo's copy-not-import convention.
    """
    try:
        return int(os.environ.get("QUOIN_SUBPROCESS_TIMEOUT", "30"))
    except (TypeError, ValueError):
        return 30


def _run(args: list[str]) -> tuple[str, str, int]:
    """Run a subprocess and return (stdout, stderr, returncode)."""
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=_subprocess_timeout(),
        )
        return proc.stdout.strip(), proc.stderr.strip(), proc.returncode
    except subprocess.TimeoutExpired:
        return "", "timeout", 1
    except FileNotFoundError:
        return "", "git not found", 1
    except Exception as exc:  # noqa: BLE001
        return "", str(exc), 1


# ---------------------------------------------------------------------------
# Interpreter resolution
# ---------------------------------------------------------------------------

_VENV_WALK_MAX_DEPTH = 6


# Set by _probe_interpreter when the probe subprocess timed out. The classifier
# resets it before each probe call and reads it right after, so a patched
# probe (which never touches it) always reads "no timeout".
_PROBE_TIMED_OUT = False


def _probe_interpreter(candidate: str, probe: str) -> bool:
    """Run `candidate -c probe` and report whether it exits 0.

    Deliberately self-contained rather than routed through _run() — that
    helper's FileNotFoundError branch hardcodes a git-specific message that
    would be misleading here.
    """
    global _PROBE_TIMED_OUT
    try:
        proc = subprocess.run(
            [candidate, "-c", probe],
            capture_output=True,
            text=True,
            timeout=_subprocess_timeout(),
        )
        return proc.returncode == 0
    except subprocess.TimeoutExpired:
        _PROBE_TIMED_OUT = True
        return False
    except Exception:  # noqa: BLE001 - FileNotFoundError, OSError, ...
        return False


@dataclasses.dataclass
class InterpreterProblem:
    """A venv or override interpreter that was found but is not usable as-is."""

    path: str
    kind: str  # dangling-link | missing-interpreter | not-runnable | no-pytest
    detail: str
    # True when the candidate is a project interpreter that exists but cannot
    # run. False for no-pytest (a healthy venv without pytest) and for a
    # QUOIN_PYTHON path that does not exist at all.
    broken: bool
    source: str = "venv"  # "venv" or "QUOIN_PYTHON"

    def __str__(self) -> str:
        return "%s: %s (%s)" % (self.kind, self.path, self.detail)


def _venv_base_hint(venv_dir: Path) -> str:
    """Describe the Python a venv was built from, using its pyvenv.cfg."""
    try:
        text = (venv_dir / "pyvenv.cfg").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    cfg = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            cfg[key.strip().lower()] = value.strip()
    home = cfg.get("home", "")
    version = cfg.get("version", "") or cfg.get("version_info", "")
    if not home and not version:
        return ""
    parts = ["venv built from Python"]
    if version:
        parts.append(version)
    if home:
        parts.append("at " + home)
    return " ".join(parts)


def _classify_candidate(
    candidate: Path, probe: Optional[str], source: str = "venv"
) -> Tuple[bool, Optional[InterpreterProblem]]:
    """Decide whether `candidate` is a usable interpreter.

    Returns (ok, problem). `problem` is None when the candidate is usable or
    when there is nothing at that path worth reporting. Every probe goes
    through _probe_interpreter so a patched probe controls the outcome.
    """
    global _PROBE_TIMED_OUT
    path = str(candidate)
    venv_dir = candidate.parent.parent
    hint = _venv_base_hint(venv_dir)

    if os.path.lexists(path) and not candidate.exists():
        target = os.path.realpath(path)
        detail = "link target %s does not exist" % target
        if hint:
            detail += "; " + hint
        return False, InterpreterProblem(path, "dangling-link", detail, True, source)

    if not os.path.lexists(path):
        if source == "QUOIN_PYTHON":
            return False, InterpreterProblem(
                path,
                "missing-interpreter",
                "QUOIN_PYTHON names a path that does not exist",
                False,
                source,
            )
        if not (venv_dir / "pyvenv.cfg").is_file():
            return False, None
        detail = "bin/python missing"
        if (candidate.parent / "python3").exists():
            detail += "; bin/python3 present"
        if hint:
            detail += "; " + hint
        return False, InterpreterProblem(path, "missing-interpreter", detail, True, source)

    if not (candidate.is_file() and os.access(candidate, os.X_OK)):
        return False, InterpreterProblem(
            path, "not-runnable", "not an executable file", True, source
        )

    if probe is None:
        return True, None

    timeout_detail = "probe timed out after %ss" % _subprocess_timeout()
    _PROBE_TIMED_OUT = False
    if _probe_interpreter(path, probe):
        return True, None
    if _PROBE_TIMED_OUT:
        return False, InterpreterProblem(path, "not-runnable", timeout_detail, True, source)

    _PROBE_TIMED_OUT = False
    if _probe_interpreter(path, "pass"):
        return False, InterpreterProblem(
            path, "no-pytest", "interpreter runs but the probe failed", False, source
        )
    if _PROBE_TIMED_OUT:
        return False, InterpreterProblem(path, "not-runnable", timeout_detail, True, source)
    detail = "interpreter cannot be executed"
    if hint:
        detail += "; " + hint
    return False, InterpreterProblem(path, "not-runnable", detail, True, source)


def resolve_python_detail(
    project_root: Path, probe: Optional[str] = None
) -> Tuple[str, str, List[InterpreterProblem]]:
    """Resolve the Python interpreter to run pytest with, and report problems.

    Resolution order: QUOIN_DISABLE_VENV_PROBE=1 short-circuits to the
    invoking interpreter; QUOIN_PYTHON is honored if it points at an
    executable file that passes the probe; otherwise an upward walk from
    project_root looks for a `.venv/bin/python`, bounded by
    _VENV_WALK_MAX_DEPTH levels and stopped before the home directory; if
    nothing qualifies, falls back to the invoking interpreter.

    A candidate that exists but cannot be used is kept walking past and
    recorded as an InterpreterProblem, in encounter order: a dangling link, a
    venv whose bin/python is gone, or an interpreter that cannot run at all
    (broken); a runnable interpreter that only lacks pytest is listed but not
    broken. A healthy venv found higher up still wins.

    The candidate path is returned verbatim, never `.resolve()`d — resolving
    a venv interpreter symlink strips the venv prefix and silently changes
    which site-packages it imports from.
    """
    if os.environ.get("QUOIN_DISABLE_VENV_PROBE", "").strip() == "1":
        return sys.executable, "disabled", []

    problems: List[InterpreterProblem] = []

    env_py = os.environ.get("QUOIN_PYTHON", "").strip()
    if env_py:
        candidate = Path(env_py)
        ok, problem = _classify_candidate(candidate, probe, source="QUOIN_PYTHON")
        if problem is not None:
            problems.append(problem)
        if ok:
            return str(candidate), "env-override", problems
        # else fall through to the venv walk — never return an unprobed override

    try:
        anchor = Path(project_root).expanduser().resolve()
    except (OSError, TypeError):
        anchor = Path.cwd()

    try:
        home = Path.home()
    except RuntimeError:
        home = None  # undeterminable home -> the stop condition never fires

    for d in [anchor, *anchor.parents][: 1 + _VENV_WALK_MAX_DEPTH]:
        if home is not None and d == home:
            break  # stop BEFORE the home directory; never probe ~/.venv
        candidate = d / ".venv" / "bin" / "python"
        ok, problem = _classify_candidate(candidate, probe)
        if problem is not None:
            problems.append(problem)
        if ok:
            return str(candidate), "venv", problems

    return sys.executable, "fallback", problems


def resolve_python(project_root: Path, probe: Optional[str] = None) -> Tuple[str, str]:
    """Resolve the Python interpreter to run pytest with.

    Thin wrapper over resolve_python_detail that drops the problem list.
    """
    interp, reason, _problems = resolve_python_detail(project_root, probe)
    return interp, reason


def discover_repos(project_root: Path) -> list[Path]:
    """Discover git repositories under project_root (depth-1 scan).

    Mirrors branch_hygiene.discover_repos exactly.
    - If project_root/.git exists, include project_root.resolve().
    - Iterate depth-1 children; include any child dir with .git not in _EXCLUDE_NAMES.
    - Returns sorted, deduplicated absolute Path list.
    - On OSError, returns [].

    D-08 / T-08: when QUOIN_DISABLE_CHILD_REPO_SCAN=1, the depth-1 per-child
    .git stat loop is skipped entirely and this returns a single-repo view
    ([root] if root/.git exists, else []). Distinct from
    QUOIN_DISABLE_DISPATCH_CWD; default (unset) is byte-identical to the
    pre-existing behavior below.
    """
    if os.environ.get("QUOIN_DISABLE_CHILD_REPO_SCAN") == "1":
        try:
            root = project_root.resolve()
        except OSError:
            return []
        return [root] if (root / ".git").exists() else []

    repos: list[Path] = []
    seen: set[str] = set()

    def _add(p: Path) -> None:
        canonical = str(p.resolve())
        if canonical not in seen:
            seen.add(canonical)
            repos.append(p.resolve())

    try:
        root = project_root.resolve()
        if (root / ".git").exists():
            _add(root)
        try:
            children = sorted(root.iterdir())
        except OSError:
            children = []
        for child in children:
            if child.name in _EXCLUDE_NAMES:
                continue
            if child.is_dir() and (child / ".git").exists():
                _add(child)
    except OSError:
        return []

    return sorted(repos, key=lambda p: str(p))


def resolve_repo(project_root: Path) -> Path | None:
    """Resolve the single git repo under project_root via depth-1 scan.

    Returns the repo Path when exactly one repo is found.
    Returns None when zero repos found (caller should exit 3).
    Raises RuntimeError with a message when >1 repos found (caller should exit 3).
    """
    repos = discover_repos(project_root)
    if len(repos) == 0:
        return None
    if len(repos) > 1:
        paths = ", ".join(str(r) for r in repos)
        raise RuntimeError(
            f"Multiple git repos found under {project_root}; "
            f"pass --repo-root explicitly to disambiguate: {paths}"
        )
    return repos[0]


# Infra folders under .workflow_artifacts/ that do NOT count as an active task
# context — they exist regardless of whether any task is in flight (IVG-151).
_TASK_CONTEXT_INFRA: frozenset[str] = frozenset({"memory", "cache", "finalized", "trash"})


def has_active_task_context(project_root: Path) -> bool:
    """Return True if an active quoin task context is detectable at/above project_root.

    Git-free detector (lives in the Git-helpers region for locality only).
    Walks UP from project_root to the filesystem root looking for a
    ``.workflow_artifacts/`` directory that contains at least one REAL task
    folder — a child directory whose name is NOT dot-prefixed and is NOT one of
    the infra folders (memory / cache / finalized / trash).

    Direction invariants (the only never-false-green-safe choices — IVG-151
    architecture R-01 / R-06 / R-07):
      - Walk-up only ADDS context: a ``.workflow_artifacts/`` with no qualifying
        task child does NOT short-circuit to False; the walk keeps going upward.
        This is the subdir-safety guarantee — e.g. a check run from inside
        ``quoin/`` still finds the workflow root one level up.
      - OSError degrades to context-PRESENT (return True): an unreadable
        ``.workflow_artifacts/`` must fail toward RUNNING the real check, never
        toward silently skipping it, and never toward a crash.
    Both directions fail toward RUNNING the real check — a false-skip of a real
    red suite is the one outcome this design forbids.
    """
    try:
        cur = project_root.resolve()
        while True:
            wa = cur / ".workflow_artifacts"
            if wa.is_dir():
                for child in wa.iterdir():
                    if (
                        child.is_dir()
                        and not child.name.startswith(".")
                        and child.name not in _TASK_CONTEXT_INFRA
                    ):
                        return True
                # WA present but no qualifying task child — keep walking up
                # (do NOT early-return False here — walk-up only adds context).
            if cur.parent == cur:  # reached the filesystem root
                return False
            cur = cur.parent
    except OSError:
        # Degrade to context-PRESENT: fail toward RUNNING the real check.
        return True


def _resolve_base_branch(repo_str: str) -> str | None:
    """Probe candidate base branches in order and return the first that resolves.

    Probe order:
      1. QUOIN_BASE_BRANCH env var (if set and non-empty)
      2. origin/main
      3. origin/master
      4. main
      5. master

    Returns the ref name string if resolvable, None if none resolve.
    """
    env_override = os.environ.get("QUOIN_BASE_BRANCH", "").strip()
    candidates: list[str] = []
    if env_override:
        candidates.append(env_override)
    candidates.extend(["origin/main", "origin/master", "main", "master"])

    for ref in candidates:
        out, err, rc = _run(
            ["git", "-C", repo_str, "rev-parse", "--verify", ref]
        )
        if rc == 0 and out.strip():
            return ref
    return None


def changed_files(repo: Path) -> tuple[list[str], str]:
    """Compute the set of changed files in repo using the diff-basis fallback chain.

    Returns (files, exit_reason) where exit_reason is one of:
      "upstream-diff"    — obtained from @{u}...HEAD (three-dot, merge-base diff)
      "base-branch-diff" — obtained from <base>...HEAD (merge-base diff vs base branch)
      "worktree-diff"    — obtained from HEAD ∪ cached diff
      "no-changes"       — git ran cleanly, tree is genuinely clean
      "git-error"        — git command failed (caller should exit 3)

    Fallback chain (F-01 fix):
      1. Upstream @{u}...HEAD (if upstream exists and yields non-empty diff)
      2. Base-branch merge-base: <base>...HEAD where <base> is resolved via
         _resolve_base_branch() — this handles the committed-clean no-upstream
         case (the canonical /review + /gate state before /end_of_task push).
      3. Worktree + staged fallback (handles uncommitted dirty trees)
      4. no-changes (genuinely clean)
      Fail-CLOSED: if no base resolves AND no upstream AND tree is clean but
      HEAD has commits (i.e. is not the root), we still return no-changes
      (the git state is genuinely unambiguous at that point — an initial commit
      on a brand-new repo truly has nothing to diff against).

    NOTE: Three-dot @{u}...HEAD shares the @{u} ANCHOR with review Step 6a's
    two-dot @{u}..HEAD rev-list count, but uses the merge-base operator — they
    agree on a feature branch strictly ahead of an unmoved upstream and diverge
    only if upstream advanced past the branch point.  Three-dot is correct here
    (we want "what changed on this branch"), so Step 6a is left unchanged (MIN-1).

    NOTE: worktree fallback intentionally excludes untracked files — the gate's
    "No uncommitted changes" check is the backstop that prevents false-greens
    from untracked new .py sources (MIN-2 documented in module docstring).
    """
    repo_str = str(repo.resolve())

    # Step 1: try upstream three-dot diff
    ups_out, ups_err, ups_rc = _run(
        ["git", "-C", repo_str, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]
    )
    if ups_rc == 0 and ups_out:
        diff_out, diff_err, diff_rc = _run(
            ["git", "-C", repo_str, "diff", "--name-only", "@{u}...HEAD"]
        )
        if diff_rc != 0:
            return [], "git-error"
        files = [f for f in diff_out.splitlines() if f.strip()]
        if files:
            return files, "upstream-diff"
        # Empty upstream diff — fall through to base-branch step

    # Step 2 (F-01 fix): base-branch merge-base diff — handles the committed-clean
    # no-upstream case (feature branch created with `git switch -c`, not yet pushed).
    # This is the NORMAL state during /review and both /gate invocations.
    base_ref = _resolve_base_branch(repo_str)
    if base_ref is not None:
        # Compute the merge-base between <base> and HEAD
        merge_base_out, mb_err, mb_rc = _run(
            ["git", "-C", repo_str, "merge-base", base_ref, "HEAD"]
        )
        if mb_rc == 0 and merge_base_out.strip():
            merge_base = merge_base_out.strip()
            diff_out, diff_err, diff_rc = _run(
                ["git", "-C", repo_str, "diff", "--name-only", f"{merge_base}...HEAD"]
            )
            if diff_rc != 0:
                return [], "git-error"
            files = [f for f in diff_out.splitlines() if f.strip()]
            if files:
                return files, "base-branch-diff"
            # Empty base-branch diff — fall through to worktree fallback

    # Step 3: worktree + staged fallback
    head_out, head_err, head_rc = _run(
        ["git", "-C", repo_str, "diff", "--name-only", "HEAD"]
    )
    if head_rc != 0:
        return [], "git-error"
    cached_out, cached_err, cached_rc = _run(
        ["git", "-C", repo_str, "diff", "--name-only", "--cached"]
    )
    if cached_rc != 0:
        return [], "git-error"

    combined: set[str] = set()
    for line in head_out.splitlines():
        if line.strip():
            combined.add(line.strip())
    for line in cached_out.splitlines():
        if line.strip():
            combined.add(line.strip())

    if not combined:
        # Step 4: genuinely clean tree
        return [], "no-changes"

    return sorted(combined), "worktree-diff"


# ---------------------------------------------------------------------------
# Detection algorithm
# ---------------------------------------------------------------------------

def _is_py_test_name(name: str) -> bool:
    """True for a Python test module name: test_*.py or *_test.py.

    The .py guard keeps non-Python files such as test_x.sh from becoming pytest
    selectors, which pytest could not collect (exit 4).
    """
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def _collect_test_files(repo_root: Path) -> list[Path]:
    """Return all test_*.py / *_test.py files under repo_root.

    Directories named in _WALK_EXCLUDE_NAMES (and *.egg-info) are pruned, so
    third-party tests shipped inside a virtualenv, node_modules or a build tree
    are never selection candidates.
    """
    results: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [
            d for d in dirnames
            if d not in _WALK_EXCLUDE_NAMES and not d.endswith(".egg-info")
        ]
        for name in filenames:
            if _is_py_test_name(name):
                results.append(Path(dirpath) / name)
    return sorted(results)


def map_changed_to_tests(
    changed: list[str],
    repo_root: Path,
) -> tuple[list[str], list[str], list[str]]:
    """Map a list of changed file paths to test selectors.

    Returns (selectors, unmatched_sources, ignored) where:
      selectors         — sorted, deduped absolute-or-relative test file paths to run
      unmatched_sources — .py sources with ZERO matched tests (fail-CLOSED signal)
      ignored           — non-.py files (docs, JSON, SKILL.md, ...) — excluded from
                          unmatched_sources; a changeset of ONLY ignored files is
                          a docs-only changeset (exit 0b, not exit 4).
                          Exception: files listed in _DOCS_TO_TESTS are mapped to
                          a specific test file and do NOT land in ignored.
                          Also holds changed test files that no longer exist
                          on disk; main() reports those as missing_tests.

    Detection algorithm:
      0. A changed conftest.py → every test file under its directory (pytest's
         conftest scope), unioned with the steps below.
      1. Changed test files that exist → included directly as selectors;
         missing ones → ignored.
      2. Changed non-test .py files with stem S → name-match: any test file whose
         basename matches test_{S}*.py or {S}_test.py (PRIMARY signal).
      3. Import-graph grep (BEST-EFFORT supplement): whole-word \\b{S}\\b match
         anywhere in each test file — catches spec_from_file_location paths,
         string literals, _CORE_PATH assignments, _quoin_core_{S} aliases.
         More false-positives → SAFE (runs more tests, never fewer).
      4. If a .py source has ZERO selectors after steps 1-3 → unmatched_source.
         Non-.py files → ignored.
    """
    test_files = _collect_test_files(repo_root)
    selectors: set[str] = set()
    unmatched_sources: list[str] = []
    ignored: list[str] = []

    for changed_file in changed:
        fpath = Path(changed_file)
        name = fpath.name

        # Is this file itself a Python test file?
        # Guard on .py suffix to avoid selecting non-Python test files (e.g., .sh)
        # as pytest selectors — pytest would fail to collect them (exit 4).
        if _is_py_test_name(name):
            # Include directly as a selector; resolve against repo_root if relative
            full = (repo_root / changed_file).resolve() if not fpath.is_absolute() else fpath.resolve()
            if full.exists():
                selectors.add(str(full))
            else:
                # A test file removed by the change has nothing to run, and
                # handing pytest the missing path makes it exit 4, which
                # would read as a red affected area.
                ignored.append(changed_file)
            continue

        # Special-case: certain non-.py docs/source files map to specific tests.
        # Runs BEFORE the generic non-.py "ignored" fallback so these files
        # are treated as selector sources (exit 0a) rather than docs-only (exit 0b).
        if fpath.suffix != ".py":
            posix = PurePosixPath(changed_file).as_posix()
            mapped_any = False
            for src_suffix, test_rel in _DOCS_TO_TESTS:
                if posix == src_suffix or posix.endswith("/" + src_suffix):
                    test_path = repo_root / test_rel
                    # A row only takes the file out of "ignored" when its
                    # mapped test actually exists here — a bare-filename
                    # row (e.g. "pyproject.toml") also matches an unrelated
                    # project's own top-level file once this script is
                    # deployed there, and that file must still land in
                    # "ignored" rather than silently vanish with no
                    # selector to show for it.
                    if test_path.exists():
                        selectors.add(str(test_path))
                        mapped_any = True
            if mapped_any:
                continue
            # Generic non-.py file → ignored
            ignored.append(changed_file)
            continue

        # .py non-test source → attempt name-match + import-graph grep
        stem = fpath.stem
        matched: set[str] = set()

        # A conftest.py supplies fixtures and hooks to every test below its
        # directory, so a change (or deletion) there can break any of them.
        if name == "conftest.py":
            full = fpath if fpath.is_absolute() else repo_root / changed_file
            scope = full.resolve().parent
            for tf in test_files:
                if scope in tf.resolve().parents:
                    matched.add(str(tf))

        # Step 2: name-match
        for tf in test_files:
            tfname = tf.name
            if tfname.startswith(f"test_{stem}") or tfname == f"{stem}_test.py":
                matched.add(str(tf))

        # Step 3: whole-word grep in test file content (BEST-EFFORT)
        if stem:
            pattern = re.compile(r"\b" + re.escape(stem) + r"\b")
            for tf in test_files:
                if str(tf) in matched:
                    continue  # already matched
                try:
                    content = tf.read_text(encoding="utf-8", errors="ignore")
                    if pattern.search(content):
                        matched.add(str(tf))
                except OSError:
                    pass  # unreadable test file — skip

        if matched:
            selectors.update(matched)
        else:
            unmatched_sources.append(changed_file)

    return sorted(selectors), unmatched_sources, ignored


# ---------------------------------------------------------------------------
# Text formatter (T-02)
# ---------------------------------------------------------------------------

def _format_text(sel: Selection) -> str:
    """Human-readable text summary of a Selection."""
    lines: list[str] = []
    lines.append(f"exit_reason: {sel.exit_reason}")
    lines.append(f"ran_pytest: {sel.ran_pytest}")
    if sel.pytest_returncode is not None:
        lines.append(f"pytest_returncode: {sel.pytest_returncode}")
    lines.append(f"changed ({len(sel.changed)}): {', '.join(sel.changed) or '(none)'}")
    lines.append(f"selectors ({len(sel.selectors)}): {', '.join(sel.selectors) or '(none)'}")
    if sel.unmatched_sources:
        lines.append(f"unmatched_sources ({len(sel.unmatched_sources)}): {', '.join(sel.unmatched_sources)}")
    if sel.ignored:
        lines.append(f"ignored ({len(sel.ignored)}): {', '.join(sel.ignored)}")
    if sel.noncollectable:
        lines.append(f"noncollectable ({len(sel.noncollectable)}): {', '.join(sel.noncollectable)}")
    if sel.missing_tests:
        lines.append(f"missing_tests ({len(sel.missing_tests)}): {', '.join(sel.missing_tests)}")
    if sel.unmatched_warning:
        lines.append("unmatched_warning: true (--allow-unmatched in use)")
    if sel.interpreter:
        lines.append(f"interpreter: {sel.interpreter}")
    if sel.interpreter_reason:
        lines.append(f"interpreter_reason: {sel.interpreter_reason}")
    if sel.interpreter_problems:
        lines.append(
            f"interpreter_problems ({len(sel.interpreter_problems)}): "
            + "; ".join(sel.interpreter_problems)
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Returns exit code:
      0 — APPROVABLE (affected-area green, docs-only, or clean tree)
      1 — affected-area suite RED (pytest returned non-zero)
      2 — argparse / malformed input
      3 — UNDETERMINABLE (fail-CLOSED): git-root failure, git error, unmatched
          sources, pytest missing, or QUOIN_DISABLE_AFFECTED_TESTS=1
      4 — .py source changed but selectors resolved to empty set
      5 — no active quoin task context (NON-approving, NON-blocking); reachable
          only with --require-task-context in --project-root mode when
          QUOIN_REQUIRE_TASK_CONTEXT!=0 (IVG-151)
    """
    # Env opt-out — exits 3 (NOT 0) so disabling cannot silently green-light APPROVE
    if os.environ.get("QUOIN_DISABLE_AFFECTED_TESTS", "").strip() == "1":
        print(json.dumps({"disabled": True}))
        return 3

    parser = argparse.ArgumentParser(
        description=(
            "Map changed files to affected test files and run them. "
            "A GREEN result is a hard precondition for APPROVED in /gate and /review."
        ),
        add_help=True,
    )
    mode = parser.add_mutually_exclusive_group(required=False)
    mode.add_argument(
        "--project-root",
        type=Path,
        metavar="PATH",
        help=(
            "PRIMARY workflow path.  The helper resolves the git repo under PATH "
            "and computes the changed-file set itself (CRIT-1/CRIT-2 fix).  "
            "The caller never runs git directly."
        ),
    )
    mode.add_argument(
        "--files-from",
        metavar="PATH",
        help=(
            "Newline-delimited list of changed files from PATH (use '-' for stdin).  "
            "Portable override — no git dependency; used by unit tests."
        ),
    )
    mode.add_argument(
        "--files",
        nargs="+",
        metavar="FILE",
        help="Changed files passed inline.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        metavar="PATH",
        default=None,
        help=(
            "Root for test-file discovery and the pytest invocation.  "
            "In --project-root mode this is set automatically to the resolved git repo.  "
            "Optional override for --files-from / --files modes."
        ),
    )
    parser.add_argument(
        "--select-only",
        action="store_true",
        help="Print resolved test selectors as JSON and exit WITHOUT running pytest.",
    )
    parser.add_argument(
        "--allow-unmatched",
        action="store_true",
        help=(
            "When set, unmatched_sources being non-empty does NOT force exit 3 — "
            "it adds unmatched_warning=true and the exit code is driven by pytest. "
            "Default OFF (fail-CLOSED)."
        ),
    )
    parser.add_argument(
        "--require-task-context",
        action="store_true",
        dest="require_task_context",
        help=(
            "Opt-in: in --project-root mode, if no active quoin task context is "
            "found (and QUOIN_REQUIRE_TASK_CONTEXT!=0), exit 5 (no-quoin-task-context) "
            "WITHOUT running pytest. Inert in --files/--files-from modes."
        ),
    )
    parser.add_argument(
        "--format",
        choices=["json", "text"],
        default="json",
        help="Output format (default: json).",
    )
    parser.add_argument(
        "--pytest-arg",
        action="append",
        dest="pytest_args",
        metavar="ARG",
        default=[],
        help="Extra argument appended to the pytest invocation (repeatable).",
    )
    parser.add_argument(
        "--print-interpreter",
        action="store_true",
        dest="print_interpreter",
        help=(
            "Print the resolved interpreter and exit 0, without running anything else. "
            "Anchors at --project-root itself, not the resolved git repo; may diverge "
            "from the interpreter a real run selects when a repo-local .venv differs "
            "from a project-level one."
        ),
    )

    try:
        args = parser.parse_args(argv)
        if (
            not args.print_interpreter
            and args.project_root is None
            and args.files_from is None
            and args.files is None
        ):
            parser.error("one of the arguments --project-root --files-from --files is required")
    except SystemExit as exc:
        return int(exc.code) if exc.code is not None else 2

    fmt = args.format

    # --print-interpreter: early return, before repo resolution and every
    # other early-exit path, so nothing can swallow it. It may be given alone,
    # so args.project_root can legally be None here, hence the Path.cwd() guard.
    if args.print_interpreter:
        anchor = args.project_root if args.project_root is not None else Path.cwd()
        interp, interp_reason, interp_found = resolve_python_detail(
            anchor, probe="import pytest"
        )
        print(f"interpreter: {interp}")
        print(f"interpreter_reason: {interp_reason}")
        for found in interp_found:
            print(f"interpreter_problem: {found}")
        return 0

    # ------------------------------------------------------------------
    # Step 1: resolve changed files
    # ------------------------------------------------------------------
    changed: list[str] = []
    repo_root: Path | None = args.repo_root

    if args.project_root is not None:
        # IVG-151: opt-in early exit-5 when NO active quoin task context is
        # found. This is the FIRST statement in the --project-root block,
        # BEFORE resolve_repo(), so a non-quoin session never resolves a
        # foreign git root or runs any git subprocess (that noise is exactly
        # what the ticket removes).
        # Precedence invariants (pin — do NOT reorder):
        #   - QUOIN_DISABLE_AFFECTED_TESTS=1 already returned 3 at the very top
        #     of main() (before argparse), so disable NATURALLY wins over this.
        #   - QUOIN_REQUIRE_TASK_CONTEXT literal "0" forces legacy always-run
        #     (mirrors the QUOIN_DISABLE_* literal-value parsing convention).
        if (
            args.require_task_context
            and os.environ.get("QUOIN_REQUIRE_TASK_CONTEXT", "").strip() != "0"
            and not has_active_task_context(args.project_root)
        ):
            sel = Selection(
                changed=[],
                selectors=[],
                unmatched_sources=[],
                ignored=[],
                ran_pytest=False,
                pytest_returncode=None,
                exit_reason="no-quoin-task-context",
            )
            if fmt == "text":
                print(_format_text(sel))
            else:
                print(json.dumps(sel.to_dict(), indent=2))
            return 5
        # --project-root mode: resolve git repo, compute diff
        try:
            repo = resolve_repo(args.project_root)
        except RuntimeError as exc:
            print(json.dumps({
                "error": str(exc),
                "exit_reason": "undeterminable-multiple-repos",
                "ran_pytest": False,
                "pytest_returncode": None,
                "changed": [],
                "selectors": [],
                "unmatched_sources": [],
                "ignored": [],
            }), file=sys.stderr)
            return 3
        if repo is None:
            print(json.dumps({
                "error": f"No git repo found under --project-root {args.project_root}",
                "exit_reason": "undeterminable-no-repo",
                "ran_pytest": False,
                "pytest_returncode": None,
                "changed": [],
                "selectors": [],
                "unmatched_sources": [],
                "ignored": [],
            }), file=sys.stderr)
            return 3

        # Set repo_root if not explicitly overridden
        if repo_root is None:
            repo_root = repo

        files, reason = changed_files(repo)
        if reason == "git-error":
            print(json.dumps({
                "error": "git error while computing changed files",
                "exit_reason": "undeterminable-git-error",
                "ran_pytest": False,
                "pytest_returncode": None,
                "changed": [],
                "selectors": [],
                "unmatched_sources": [],
                "ignored": [],
            }), file=sys.stderr)
            return 3
        if reason == "no-changes":
            sel = Selection(
                changed=[],
                selectors=[],
                unmatched_sources=[],
                ignored=[],
                ran_pytest=False,
                pytest_returncode=None,
                exit_reason="no-changes",
            )
            if fmt == "text":
                print(_format_text(sel))
            else:
                print(json.dumps(sel.to_dict(), indent=2))
            return 0
        # Paths from git are relative to repo; keep them as-is for map_changed_to_tests
        changed = files

    elif args.files_from is not None:
        # --files-from mode
        if args.files_from == "-":
            raw = sys.stdin.read()
        else:
            try:
                raw = Path(args.files_from).read_text(encoding="utf-8")
            except OSError as exc:
                print(f"error: cannot read --files-from {args.files_from}: {exc}", file=sys.stderr)
                return 2
        changed = [l.strip() for l in raw.splitlines() if l.strip()]

    else:
        # --files mode
        changed = list(args.files)

    # ------------------------------------------------------------------
    # Step 2: map changed files to selectors
    # ------------------------------------------------------------------
    if repo_root is None:
        repo_root = Path.cwd()

    # Resolve the interpreter pytest will run under. Anchored at
    # repo_root — the only point where it is defined in all three modes — and
    # probed for pytest importability so a candidate venv that can't run
    # pytest falls through rather than being selected and failing later.
    interp, interp_reason, interp_found = resolve_python_detail(
        repo_root, probe="import pytest"
    )
    interp_problems = [str(found) for found in interp_found]

    # FR-6/AC-6: partition intentionally-non-collectable files OUT of `changed`
    # BEFORE mapping.  An allowlisted .py (test or non-test) must never become a
    # selector or an unmatched_source.  Absent allowlist → empty entries →
    # `changed_remaining == changed`, so mapping is byte-for-byte unchanged.
    nc_entries = load_noncollectable(repo_root)
    changed_remaining, noncollectable = partition_noncollectable(changed, nc_entries)

    selectors, unmatched_sources, ignored = map_changed_to_tests(changed_remaining, repo_root)
    # Only the missing-on-disk branch can leave a test-named .py in `ignored`.
    missing_tests = [f for f in ignored if _is_py_test_name(Path(f).name)]

    # ------------------------------------------------------------------
    # Step 3: --select-only path — print and exit without running pytest
    # ------------------------------------------------------------------
    if args.select_only:
        sel = Selection(
            changed=changed,
            selectors=selectors,
            unmatched_sources=unmatched_sources,
            ignored=ignored,
            ran_pytest=False,
            pytest_returncode=None,
            exit_reason="select-only",
            unmatched_warning=bool(unmatched_sources and args.allow_unmatched),
            noncollectable=noncollectable,
            missing_tests=missing_tests,
            interpreter=interp,
            interpreter_reason=interp_reason,
            interpreter_problems=interp_problems,
        )
        if fmt == "text":
            print(_format_text(sel))
        else:
            print(json.dumps(sel.to_dict(), indent=2))
        return 0

    # ------------------------------------------------------------------
    # Step 4: post-selection routing (MAJ-1 — BEFORE any pytest call)
    # ------------------------------------------------------------------

    # 4a: unmatched sources without --allow-unmatched → exit 3 (fail-CLOSED)
    if unmatched_sources and not args.allow_unmatched:
        sel = Selection(
            changed=changed,
            selectors=selectors,
            unmatched_sources=unmatched_sources,
            ignored=ignored,
            ran_pytest=False,
            pytest_returncode=None,
            exit_reason="unmatched-sources",
            noncollectable=noncollectable,
            missing_tests=missing_tests,
            interpreter=interp,
            interpreter_reason=interp_reason,
            interpreter_problems=interp_problems,
        )
        if fmt == "text":
            print(_format_text(sel))
        else:
            print(json.dumps(sel.to_dict(), indent=2))
        return 3

    # 4b: empty selectors branch — determine WHY and exit BEFORE touching pytest
    if not selectors:
        # Use the already-computed unmatched_sources to determine why selectors is empty.
        # F-02 fix: with --allow-unmatched, the escape-hatch contract yields exit 0 (not 4)
        # even when all .py sources were unmatched — the flag means "I know tests are
        # missing; don't block me."  Exit 4 is only reachable without --allow-unmatched,
        # but that path is handled in 4a above (unmatched_sources + no flag → exit 3).
        # Therefore the only remaining empty-selector cases here are:
        #   - unmatched_sources non-empty AND --allow-unmatched set → exit 0b (warn)
        #   - unmatched_sources empty → truly docs-only → exit 0b
        if unmatched_sources and args.allow_unmatched:
            # --allow-unmatched with all sources unmatched and no selectors:
            # exit 0 with unmatched_warning — consistent with escape-hatch contract.
            sel = Selection(
                changed=changed,
                selectors=[],
                unmatched_sources=unmatched_sources,
                ignored=ignored,
                ran_pytest=False,
                pytest_returncode=None,
                exit_reason="docs-only-no-selectors",
                unmatched_warning=True,
                noncollectable=noncollectable,
                missing_tests=missing_tests,
                interpreter=interp,
                interpreter_reason=interp_reason,
                interpreter_problems=interp_problems,
            )
            if fmt == "text":
                print(_format_text(sel))
            else:
                print(json.dumps(sel.to_dict(), indent=2))
            return 0

        # Docs-only / non-collectable-only: zero changed .py selectors → exit 0b.
        # (also catches genuinely-empty changed list when called via --files [])
        # AC-6: a changeset of ONLY non-collectable (and/or ignored) files resolves
        # to exit 0 with a distinct exit_reason for gate/audit observability.
        # unmatched_sources is empty here (4a already returned exit 3 for the
        # unmatched-without-allow case; the allow-unmatched case returned above).
        if noncollectable:
            empty_reason = "noncollectable-skip"
        elif missing_tests:
            empty_reason = "missing-tests-only"
        else:
            empty_reason = "docs-only-no-selectors"
        sel = Selection(
            changed=changed,
            selectors=[],
            unmatched_sources=[],
            ignored=ignored,
            ran_pytest=False,
            pytest_returncode=None,
            exit_reason=empty_reason,
            noncollectable=noncollectable,
            missing_tests=missing_tests,
            interpreter=interp,
            interpreter_reason=interp_reason,
            interpreter_problems=interp_problems,
        )
        if fmt == "text":
            print(_format_text(sel))
        else:
            print(json.dumps(sel.to_dict(), indent=2))
        return 0

    # ------------------------------------------------------------------
    # Step 5: run pytest on the resolved selectors (GUARDED — selectors non-empty)
    # ------------------------------------------------------------------
    # HARD GUARD: this line is only reachable when selectors is non-empty.
    assert selectors, "BUG: pytest invocation reached with empty selectors"

    # review round-2 minor 17: pytest not importable in the resolved
    # interpreter exits rc=1 ("No module named pytest"), which the classifier
    # below would misreport as affected-red — an environment fault presented
    # as a red affected area. When the resolver fell back to sys.executable,
    # find_spec against THIS process is authoritative for what that
    # subprocess will see; when it resolved a venv interpreter instead, that
    # candidate was already probed for pytest importability by resolve_python
    # above, so this in-process check is skipped (checking sys.executable's
    # pytest would say nothing about the venv interpreter's).
    # A project interpreter that exists but cannot run (dangling link, missing
    # bin/python, unrunnable binary) with no healthy venv higher up: running
    # pytest under the invoking interpreter instead would test the project
    # under an interpreter nobody chose, so stop and name the problem.
    if interp_reason == "fallback" and any(found.broken for found in interp_found):
        sel = Selection(
            changed=changed,
            selectors=selectors,
            unmatched_sources=unmatched_sources,
            ignored=ignored,
            ran_pytest=False,
            pytest_returncode=None,
            exit_reason="venv-interpreter-broken",
            unmatched_warning=bool(unmatched_sources and args.allow_unmatched),
            noncollectable=noncollectable,
            missing_tests=missing_tests,
            interpreter=interp,
            interpreter_reason=interp_reason,
            interpreter_problems=interp_problems,
        )
        if fmt == "text":
            print(_format_text(sel))
        else:
            print(json.dumps(sel.to_dict(), indent=2))
        broken_sources = {found.source for found in interp_found if found.broken}
        if broken_sources == {"QUOIN_PYTHON"}:
            remedy = "fix or unset QUOIN_PYTHON, or set QUOIN_DISABLE_VENV_PROBE=1 to skip the check"
        else:
            remedy = (
                "recreate the project venv on this machine, remove a stray .venv in a "
                "parent folder, or point QUOIN_PYTHON at a working interpreter "
                "(QUOIN_DISABLE_VENV_PROBE=1 skips the check)"
            )
        print(
            "affected_tests: a project venv interpreter up the directory tree is broken, so tests were not run: "
            + "; ".join(interp_problems)
            + ". To fix: "
            + remedy,
            file=sys.stderr,
        )
        return 3

    import importlib.util

    if interp == sys.executable and importlib.util.find_spec("pytest") is None:
        sel = Selection(
            changed=changed,
            selectors=selectors,
            unmatched_sources=unmatched_sources,
            ignored=ignored,
            ran_pytest=False,
            pytest_returncode=None,
            exit_reason="pytest-missing",
            unmatched_warning=bool(unmatched_sources and args.allow_unmatched),
            noncollectable=noncollectable,
            missing_tests=missing_tests,
            interpreter=interp,
            interpreter_reason=interp_reason,
            interpreter_problems=interp_problems,
        )
        if fmt == "text":
            print(_format_text(sel))
        else:
            print(json.dumps(sel.to_dict(), indent=2))
        return 3

    try:
        proc = subprocess.run(
            [interp, "-m", "pytest", *selectors, *args.pytest_args],
            cwd=str(repo_root),
            timeout=max(600, _subprocess_timeout()),
        )
        rc = proc.returncode
    except FileNotFoundError:
        # pytest binary missing → exit 3 (undeterminable, non-blocking warn)
        sel = Selection(
            changed=changed,
            selectors=selectors,
            unmatched_sources=unmatched_sources,
            ignored=ignored,
            ran_pytest=False,
            pytest_returncode=None,
            exit_reason="pytest-missing",
            unmatched_warning=bool(unmatched_sources and args.allow_unmatched),
            noncollectable=noncollectable,
            missing_tests=missing_tests,
            interpreter=interp,
            interpreter_reason=interp_reason,
            interpreter_problems=interp_problems,
        )
        if fmt == "text":
            print(_format_text(sel))
        else:
            print(json.dumps(sel.to_dict(), indent=2))
        return 3
    except subprocess.TimeoutExpired:
        # pytest subprocess exceeded the derived bound → exit 3 (undeterminable,
        # BLOCKING-SURFACE at the gate). NEITHER a false-GREEN (exit 0) NOR a
        # hard false-RED (exit 1) — the human decides (MAJ-3 / D-05 / proc P-03).
        sel = Selection(
            changed=changed,
            selectors=selectors,
            unmatched_sources=unmatched_sources,
            ignored=ignored,
            ran_pytest=False,
            pytest_returncode=None,
            exit_reason="pytest-timeout",
            unmatched_warning=bool(unmatched_sources and args.allow_unmatched),
            noncollectable=noncollectable,
            missing_tests=missing_tests,
            interpreter=interp,
            interpreter_reason=interp_reason,
            interpreter_problems=interp_problems,
        )
        if fmt == "text":
            print(_format_text(sel))
        else:
            print(json.dumps(sel.to_dict(), indent=2))
        return 3

    # T-05 / AC-6(b) / R-06: classify the pytest return code.
    #   rc == 0 → affected-green (exit 0).
    #   rc == 5 → "no tests collected": a collect-nothing test spike is NOT a real
    #             failure, so remap to a clean skip (exit 0, distinct exit_reason).
    #             This is INDEPENDENT of the allowlist.
    #   else    → affected-red (exit 1) — covers rc 1/2/3/4 so a genuine collection,
    #             internal, or usage error is NEVER masked (R-06).
    if rc == 0:
        exit_reason = "affected-green"
        code = 0
    elif rc == 5:
        exit_reason = "no-tests-collected-skip"
        code = 0
    else:
        exit_reason = "affected-red"
        code = 1
    sel = Selection(
        changed=changed,
        selectors=selectors,
        unmatched_sources=unmatched_sources,
        ignored=ignored,
        ran_pytest=True,
        pytest_returncode=rc,
        exit_reason=exit_reason,
        unmatched_warning=bool(unmatched_sources and args.allow_unmatched),
        noncollectable=noncollectable,
        missing_tests=missing_tests,
        interpreter=interp,
        interpreter_reason=interp_reason,
        interpreter_problems=interp_problems,
    )
    if fmt == "text":
        print(_format_text(sel))
    else:
        print(json.dumps(sel.to_dict(), indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
