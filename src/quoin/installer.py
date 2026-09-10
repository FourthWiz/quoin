"""Deploy quoin artifacts to ~/.claude/ (user mode) or <project>/.claude/ (project mode)."""
from __future__ import annotations

import datetime
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
from typing import NamedTuple, Optional

# T-04: single source of truth for wheel-bundled memory files (8 Tier-1 files)
TIER1_MEMORY_FILES = (
    "terse-rubric.md",
    "format-kit.md",
    "glossary.md",
    "format-kit.sections.json",
    "summary-prompt.md",
    "format-kit-pitfalls.md",
    "sleep-signals.yaml",
    "cache-guide.md",
    # Added 2026-05-15: extracted verbose sections from CLAUDE.md
    "hooks-table.md",
    "dispatch-guide.md",
    "lifecycle-guide.md",
    "cost-ledger-format.md",
    # Added IVG-77: canonical safe branch-reset recipe (Tier-1 memory file)
    "branch-recovery.md",
    # Added IVG-50 S-3: subagent preamble warm-up guide (extracted from CLAUDE.md)
    "preamble-guide.md",
    # Added IVG-50 S-3: memory maintenance reference doc and pattern config
    "memory-maintenance.md",
    "memory-maintenance.yaml",
    # Added serena-integration: Serena MCP activation protocol (Tier-1 memory file)
    "serena-activation.md",
    # Added claude-md-trim: relocated Tier-1 catalog from CLAUDE.md
    "tier1-files.md",
    # Added IVG-106: discovery/Serena refresh routine recipe (Tier-1 memory file)
    "discovery-refresh-routine.md",
    # Added IVG-115: §V ground-truth verification verbose reference (Tier-1 memory file)
    "verification-guide.md",
    # Added checkpoint-spec-harness: /checkpoint subsystem behavior spec (Tier-1 memory file)
    "checkpoint-spec.md",
    # Added IVG-137: opt-in /schedule EOD cron recipe (Tier-1 memory file)
    "eod-refresh-routine.md",
    # Added IVG-153: opt-in --autonomous span reference (Tier-1 memory file)
    "autonomous-mode.md",
    # Added IVG-150: shared fail-closed decision-gate guard reference (Tier-1 memory file)
    "decision-gate-guard.md",
    # Added IVG-164 stage 1: generated slim-variant catalog of dropped CLAUDE.md sections
    "workflow-catalog.md",
    # Shared clean-authored-content rule (Tier-1 memory file)
    "clean-authored-content.md",
    # Category-3 comment-cleanup criteria (Tier-1 memory file)
    "comment-cleanup-criteria.md",
)

# T-05: canonical skill list — must match quoin/skills/ on disk exactly
CANONICAL_SKILLS = (
    "architect",
    "capture_insight",
    "checkpoint",
    "cleanup",
    "continue_work",
    "cost_snapshot",
    "critic",
    "discover",
    "end_of_day",
    "end_of_task",
    "enrich",
    "expand",
    "gate",
    "implement",
    "init_workflow",
    "next_steps",
    "plan",
    "pr",
    "review",
    "revise",
    "revise-fast",
    "rollback",
    "run",
    "security_review",
    "sleep",
    "specify",
    "start_of_day",
    "status",
    "thorough_plan",
    "triage",
    "weekly_review",
    "workspace",
)

# T-05: canonical script list — adapter scripts deployed to ~/.claude/scripts/
DEPLOYED_SCRIPTS = (
    "validate_artifact.py",
    "path_resolve.py",
    "cost_from_jsonl.py",
    "classify_critic_issues.py",
    "build_preambles.py",
    "session_age_guard.py",
    "pidfile_helpers.sh",
    "sleep_score.py",
    "analyze_cost_ledger.py",
    "git_root_for_dispatch.py",
    "dispatch_sidecar.py",
    "status_graph.py",
    "dashboard_cost.py",    # T-11: dashboard adapter cost provider (D-11)
    "dashboard_server.py",  # T-11: dashboard HTTP server (D-11)
    "branch_hygiene.py",   # IVG-70: branch hygiene check wrapper
    "affected_tests.py",   # IVG-71: affected-area test selector + runner wrapper
    "inject_pollution_dispatch.py",  # IVG-69 Stage A: §0' Pollution dispatch generator (standalone, DEPLOYED_SCRIPTS-only — no CORE_SCRIPTS needed)
    "spend_monitor.py",              # IVG-62: realtime token-spend monitor wrapper
    "get_session_uuid.py",           # IVG-74: reliable session-UUID capture for cost ledger
    "find_drive_conflicts.py",       # IVG-75: Drive sync-conflict sweep wrapper
    "memory_check.py",               # IVG-50: auto-memory referential-integrity checker wrapper
    "memory_select.py",              # IVG-50 S-1: selective lessons retrieval wrapper
    "dispatch_config.py",            # IVG-90: 1M-dispatch config+cache reader/writer wrapper
    "worktree_isolation.py",         # IVG-116: worktree-dispatch decider+probe reader/writer wrapper
    "generate_discovery_map.py",     # /discover optional hook — silently skips without deploy
    "select_unprocessed_sessions.py",  # authoritative session-selection helper for end_of_day/weekly_review
    "thorough_plan_checkpoint.py",   # IVG-98: phase-boundary checkpoint wrapper for /thorough_plan
    "cost_summary.py",               # IVG-96: portable cost-summary.json normalizer wrapper
    "discovery_staleness.py",        # IVG-106: discovery/Serena staleness detector wrapper
    "verify_claims.py",              # IVG-115: §V ground-truth reconciliation engine wrapper
    "inject_verification_step.py",   # IVG-115 T-04: §V block generator (standalone, DEPLOYED_SCRIPTS-only — no CORE_SCRIPTS needed)
    "deploy_drift_check.py",         # IVG-136: post-merge deploy-drift guard (adapter-only, DEPLOYED_SCRIPTS-only — no CORE_SCRIPTS twin, see D-05)
    "ci_mirror.py",  # IVG-138: CI-parity gate check for non-Python deliverables
    "checkpoint_picker.py",          # IVG-139: pure restore-picker wrapper
    "nested_root_check.py",          # IVG-119: nested/duplicate .workflow_artifacts root detector wrapper (wrapped portable-core — also in CORE_SCRIPTS)
    "lessons_guard.py",              # IVG-119: cross-project lessons verbatim-dedup guard wrapper (wrapped portable-core — also in CORE_SCRIPTS)
    "decision_gate_guard.py",        # IVG-150: fail-closed decision-gate guard wrapper (wrapped portable-core — also in CORE_SCRIPTS; parents[1]/core/scripts loader)
    "context_budget_guard.py",       # IVG-141: on-demand context-budget guard wrapper (wrapped portable-core — also in CORE_SCRIPTS)
    "boundary_checkpoint.py",        # IVG-141: phase/task-boundary checkpoint writer wrapper (wrapped portable-core — also in CORE_SCRIPTS)
    "run_state.py",                  # IVG-258: task-keyed run-state writer/reader wrapper (wrapped portable-core — also in CORE_SCRIPTS)
    "compaction_telemetry.py",       # IVG-258 stage 5: compaction-telemetry sink reader wrapper (wrapped portable-core — also in CORE_SCRIPTS)
    "agent_transcript_cost.py",      # IVG-111 S-2: nested subagent-transcript resolver + pricer (adapter-only, DEPLOYED_SCRIPTS-only — no CORE_SCRIPTS twin, mirrors cost_from_jsonl.py)
    "backfill_cost_attribution.py",  # IVG-111 S-5: historical col-8 backfill (adapter-only, DEPLOYED-only — no CORE twin, mirrors cost_from_jsonl.py / agent_transcript_cost.py)
    "known_red.py",  # IVG-144: known-red manifest reader/matcher (wrapped portable-core — also in CORE_SCRIPTS)
    "workspace.py",  # IVG-158: parallel-feature-isolation workspace create core+wrapper
    "plan_path_lint.py",  # IVG-143: cited-path resolver/linter wrapper (wrapped portable-core — also in CORE_SCRIPTS)
    "authored_content_lint.py",  # advisory authored-content lint wrapper (wrapped portable-core — also in CORE_SCRIPTS)
    "footprint_report.py",  # IVG-162 T-01: corpus byte-footprint report (standalone, DEPLOYED_SCRIPTS-only — no CORE_SCRIPTS twin, mirrors sleep_score.py)
    "build_claude_slim.py",  # IVG-164 stage 1 T-04: slim CLAUDE.md variant generator (standalone, DEPLOYED_SCRIPTS-only — no CORE_SCRIPTS twin, mirrors build_preambles.py)
    "context_bundle.py",  # IVG-164 stage 2 T-02: context bundle helper for orchestrator spawn-prompt construction (standalone, DEPLOYED_SCRIPTS-only — no CORE_SCRIPTS twin, mirrors build_claude_slim.py)
    "gate_fullsuite_sidecar.py",  # IVG-249 stage-3 gate full-suite freshness sidecar (wrapped portable-core — also in CORE_SCRIPTS)
    "handoff_measure.py",  # IVG-248: agent-handoff payload-size instrument (adapter-only, DEPLOYED_SCRIPTS-only — no CORE_SCRIPTS twin, mirrors footprint_report.py)
    "handoff_validate.py",  # IVG-248: inter-agent handoff envelope validator (wrapped portable-core — also in CORE_SCRIPTS)
    "comment_cleanup.py",  # pre-PR comment cleanup wrapper (wrapped portable-core — also in CORE_SCRIPTS)
)

# T-05: obsolete artifacts to remove from prior installs
OBSOLETE_SCRIPTS = ("summarize_for_human.py", "with_env.sh", "audit_corpus_coverage.py")
OBSOLETE_TESTS = ("test_summarize_for_human.py", "test_with_env_sh.py")

# Canonical skillOverrides tier list written to settings.json on install.
# "on" = full description in every session; "name-only" = invocable via /name, no auto-suggest description.
# Only keys in this dict are touched; user-added keys for other skills are preserved.
SKILL_OVERRIDES: dict[str, str] = {
    # Core workflow skills — always show full description
    "thorough_plan": "on",
    "plan": "on",
    "critic": "on",
    "review": "on",
    "implement": "on",
    "gate": "on",
    "triage": "on",
    "checkpoint": "on",
    "specify": "on",
    "enrich": "on",
    # Lifecycle / one-shot skills — invocable by name, no description in auto-suggest
    "pr": "name-only",
    "init_workflow": "name-only",
    "init": "name-only",          # non-quoin skill; skillOverrides applies by name regardless
    "keybindings-help": "name-only",  # non-quoin skill; same
    "start_of_day": "name-only",
    "end_of_day": "name-only",
    # Internal / rarely-typed-directly skills — name-only to stay within skill listing budget
    "sleep": "name-only",
    "cleanup": "name-only",
    "expand": "name-only",
    "cost_snapshot": "name-only",
    "status": "name-only",
    "rollback": "name-only",
    "run": "name-only",
    "weekly_review": "name-only",
    "next_steps": "name-only",
    "continue_work": "name-only",
    "security_review": "name-only",
    "workspace": "name-only",
}

_MARKER_START = "# === DEV WORKFLOW START ==="
_MARKER_END = "# === DEV WORKFLOW END ==="
DEPRECATED_SKILL_MARKERS = ("DEPRECATED LOCATION", "deprecated stub")

# ── T-06: deploy-time path substitution ──────────────────────────────────────

# File extensions that may contain __QUOIN_HOME__ placeholders.
_SUBSTITUTE_EXTS = frozenset({".md", ".py", ".sh", ".yaml", ".json", ".txt"})

# Placeholder used in source files for load-bearing ~/.claude/ references.
# Documentation-only prose keeps literal ~/.claude/... and is NOT substituted.
QUOIN_HOME_PLACEHOLDER = "__QUOIN_HOME__"

# Subdirectories under dest_root that quoin deploys to.
# assert_no_placeholders scans ONLY these (positive allowlist) to avoid
# false positives from Claude Code internal directories (projects/, todos/, etc.).
_QUOIN_DEPLOYED_SUBDIRS = frozenset({"skills", "scripts", "core", "memory", "hooks"})


def substitute_quoin_home(text: str, dest_root: pathlib.Path) -> str:
    """Replace all __QUOIN_HOME__ occurrences with str(dest_root.resolve()).

    dest_root must be absolute. Substitution is verbatim — the caller is
    responsible for only tagging load-bearing references in source files
    (per D-03 / MAJ-2: documentation-only prose stays as literal ~/.claude/).
    """
    return text.replace(QUOIN_HOME_PLACEHOLDER, str(dest_root.resolve()))


def expected_deployed_content(src: pathlib.Path, dest_root: pathlib.Path) -> bytes:
    """Return the exact bytes that _copy_with_substitution WOULD write for src.

    Text files (_SUBSTITUTE_EXTS) have __QUOIN_HOME__ substituted with dest_root
    and are returned as UTF-8 bytes; all other extensions are byte-copied verbatim.
    This is the single source of truth for "what does deploy produce for this file"
    — both _copy_with_substitution (T-01 refactor) and compute_drift (T-02) call it,
    so the drift checker can never disagree with what install actually writes.
    Pure: reads src only, never writes.
    """
    if src.suffix in _SUBSTITUTE_EXTS:
        return substitute_quoin_home(
            src.read_text(encoding="utf-8"), dest_root
        ).encode("utf-8")
    return src.read_bytes()


def _copy_with_substitution(
    src: pathlib.Path,
    dst: pathlib.Path,
    dest_root: pathlib.Path,
) -> None:
    """Copy src to dst, performing __QUOIN_HOME__ substitution for text files.

    Non-text extensions are byte-copied without substitution.
    Sets +x for .py and .sh files.
    Skips the write when the destination already contains identical content
    so that re-installs preserve file mtimes (CRIT-1 round-2).

    Behaviour-preserving delegation (T-01): the deployed bytes are computed by
    expected_deployed_content so the drift checker and the deploy path agree
    byte-for-byte. Writing bytes (rather than text) yields identical output for
    valid UTF-8 while keeping the substitution/byte-copy split intact.
    """
    new_bytes = expected_deployed_content(src, dest_root)
    if not (dst.exists() and dst.read_bytes() == new_bytes):
        # write_bytes (not write_text) is deliberate: expected_deployed_content already
        # returns the exact bytes compute_drift compares against, and write_text's
        # newline=None default would translate "\n" -> os.linesep on write (a no-op on
        # macOS/Linux where os.linesep == "\n", but CRLF on Windows) — that would make
        # a freshly-deployed file NOT byte-equal to expected_deployed_content's output,
        # breaking both drift-check parity and the mtime-preservation skip above on a
        # future Windows target. Do not "helpfully" revert this to write_text.
        dst.write_bytes(new_bytes)
    if src.suffix in (".py", ".sh"):
        os.chmod(dst, 0o755)


# ── T-13: home hook conflict detection ───────────────────────────────────────

# Canonical hook script basenames registered by quoin
_QUOIN_HOOK_BASENAMES = frozenset({
    "userpromptsubmit.sh",
    "precompact.sh",
    "postcompact.sh",
    "sessionstart.sh",
    "sessionend.sh",
})


def detect_home_hook_conflict() -> bool:
    """Return True if ~/.claude/settings.json already has quoin hook stanzas.

    Detection checks both basename AND that the command path contains
    '/.claude/hooks/' to avoid false-positives from non-quoin scripts that
    happen to share the same filename.
    """
    home_settings = pathlib.Path.home() / ".claude" / "settings.json"
    if not home_settings.exists():
        return False
    try:
        data = json.loads(home_settings.read_text(encoding="utf-8"))
        hooks = data.get("hooks", {})
        for stanzas in hooks.values():
            stanza_list = stanzas if isinstance(stanzas, list) else [stanzas]
            for stanza in stanza_list:
                if not isinstance(stanza, dict):
                    continue
                for hook in stanza.get("hooks", []):
                    if not isinstance(hook, dict):
                        continue
                    cmd = hook.get("command", "")
                    basename = cmd.rsplit("/", 1)[-1]
                    if basename in _QUOIN_HOOK_BASENAMES and "/.claude/hooks/" in cmd:
                        return True
    except (json.JSONDecodeError, KeyError, OSError):
        pass
    return False


# ── T-04 ──────────────────────────────────────────────────────────────────────

def deploy_memory(source_dir: pathlib.Path, dest_root: pathlib.Path) -> None:
    """Copy Tier-1 memory files from source_dir/memory/ to dest_root/memory/."""
    src_mem = source_dir / "memory"
    dst_mem = dest_root / "memory"
    dst_mem.mkdir(parents=True, exist_ok=True)
    for fname in TIER1_MEMORY_FILES:
        src = src_mem / fname
        if not src.exists():
            print(f"quoin: Expected {fname} at {src} but not found", file=sys.stderr)
            sys.exit(1)
        _copy_with_substitution(src, dst_mem / fname, dest_root)
        print(f"Copied {fname} to {dest_root}/memory/")


def deploy_quickstart(source_dir: pathlib.Path, dest_root: pathlib.Path) -> None:
    """Copy QUICKSTART.md to dest_root/ (not under memory/)."""
    src = source_dir / "QUICKSTART.md"
    if not src.exists():
        print(f"quoin: Expected QUICKSTART.md at {src} but not found", file=sys.stderr)
        sys.exit(1)
    dest_root.mkdir(parents=True, exist_ok=True)
    _copy_with_substitution(src, dest_root / "QUICKSTART.md", dest_root)
    print(f"QUICKSTART deployed to {dest_root}/QUICKSTART.md")


# ── T-05 ──────────────────────────────────────────────────────────────────────

def _assert_not_deprecated_skill(skill_name: str, skill_md: pathlib.Path) -> None:
    content = skill_md.read_text(encoding="utf-8")
    for marker in DEPRECATED_SKILL_MARKERS:
        if marker in content:
            print(
                f"quoin: Refusing to deploy deprecated Claude skill stub for "
                f"{skill_name}: {skill_md}",
                file=sys.stderr,
            )
            sys.exit(1)


def resolve_skill_source_md(
    src_skills: pathlib.Path,
    src_adapter: pathlib.Path,
    name: str,
) -> pathlib.Path:
    """Return the SKILL.md source path for skill `name`: adapter-preferred, else stub.

    Encodes the single source-selection rule shared by deploy_skills (T-01) and
    compute_drift (T-02): the Claude adapter SKILL.md at
    src_adapter/<name>/SKILL.md wins when it exists; otherwise fall back to the
    legacy stub at src_skills/<name>/SKILL.md. Returning the stub path even when
    it does not exist on disk is intentional — callers decide how to handle a
    missing source (deploy_skills aborts; compute_drift skips, never raises).
    """
    adapter_md = src_adapter / name / "SKILL.md"
    if adapter_md.exists():
        return adapter_md
    return src_skills / name / "SKILL.md"


def deploy_skills(source_dir: pathlib.Path, dest_root: pathlib.Path) -> int:
    """Copy skills from source_dir/skills/ to dest_root/skills/. Returns count copied.

    When a Claude adapter SKILL.md exists at
    source_dir/adapters/claude/skills/<name>/SKILL.md, it takes precedence over
    the legacy stub at source_dir/skills/<name>/SKILL.md. This supports the
    runtime-portability migration where real skill content moves to the adapter
    path and the skills/ entry becomes a thin stub.
    """
    src_skills = source_dir / "skills"
    src_adapter = source_dir / "adapters" / "claude" / "skills"
    dst_skills = dest_root / "skills"
    count = 0
    for skill_dir in sorted(src_skills.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_name = skill_dir.name
        # Skip directories not in CANONICAL_SKILLS (e.g. Drive sync artifacts like "next_steps 2")
        if skill_name not in CANONICAL_SKILLS:
            continue
        dst_skill = dst_skills / skill_name
        dst_skill.mkdir(parents=True, exist_ok=True)
        # Prefer Claude adapter path when available (runtime-portability migration).
        # T-01: source-selection rule lives in resolve_skill_source_md so deploy
        # and drift-detection share ONE rule.
        adapter_md = src_adapter / skill_name / "SKILL.md"
        skill_md = resolve_skill_source_md(src_skills, src_adapter, skill_name)
        if not skill_md.exists():
            print(f"quoin: Expected SKILL.md at {skill_md} but not found", file=sys.stderr)
            sys.exit(1)
        _assert_not_deprecated_skill(skill_name, skill_md)
        _copy_with_substitution(skill_md, dst_skill / "SKILL.md", dest_root)
        if adapter_md.exists():
            print(f"Deploying {skill_name} from Claude adapter path")
        preamble = skill_dir / "preamble.md"
        if preamble.exists():
            _copy_with_substitution(preamble, dst_skill / "preamble.md", dest_root)
        count += 1
    print(f"Copied {count} skills to {dest_root}/skills/")
    return count


# Portable core scripts deployed alongside the adapter wrappers in ~/.claude/scripts/.
CORE_SCRIPTS = (
    "validate_artifact.py",
    "path_resolve.py",
    "classify_critic_issues.py",
    "status_graph.py",
    "cost_event.py",              # required by dashboard_model.py (sibling core load)
    "dashboard_model.py",         # T-11: deferred from stage 1 — added here (D-11)
    "dispatch_sidecar.py",        # wrapped impl; required by ~/.claude/scripts/dispatch_sidecar.py parents[1] loader
    "git_root_for_dispatch.py",   # wrapped impl; required by ~/.claude/scripts/git_root_for_dispatch.py parents[1] loader
    "branch_hygiene.py",          # wrapped impl; required by ~/.claude/scripts/branch_hygiene.py parents[1] loader
    "affected_tests.py",          # IVG-71: wrapped impl; required by ~/.claude/scripts/affected_tests.py parents[1] loader
    "spend_monitor.py",           # IVG-62: realtime token-spend monitor core impl
    "get_session_uuid.py",        # IVG-74: reliable session-UUID capture for cost ledger
    "find_drive_conflicts.py",    # IVG-75: Drive sync-conflict sweep core impl
    "memory_check.py",            # IVG-50: auto-memory referential-integrity checker core impl
    "memory_select.py",           # IVG-50 S-1: selective lessons retrieval core impl
    "dispatch_config.py",         # IVG-90: 1M-dispatch config+cache reader core impl; required by ~/.claude/scripts/dispatch_config.py parents[1] loader
    "worktree_isolation.py",      # IVG-116: worktree-dispatch decider+probe core impl; required by ~/.claude/scripts/worktree_isolation.py parents[1] loader
    "generate_discovery_map.py",  # /discover optional hook — builds discovery-map.json post-scan
    "select_unprocessed_sessions.py",  # session-selection helper for end_of_day/weekly_review
    "thorough_plan_checkpoint.py",  # IVG-98: phase-boundary checkpoint core impl for /thorough_plan
    "cost_summary.py",              # IVG-96: portable cost-summary.json normalizer core impl
    "discovery_staleness.py",       # IVG-106: discovery/Serena staleness detector core impl
    "verify_claims.py",             # IVG-115: §V ground-truth reconciliation engine core impl
    "ci_mirror.py",  # IVG-138: wrapped impl; required by ~/.claude/scripts/ci_mirror.py parents[1] loader
    "checkpoint_picker.py",         # IVG-139: wrapped impl; required by ~/.claude/scripts/checkpoint_picker.py parents[1] loader
    "nested_root_check.py",         # IVG-119: wrapped impl; required by ~/.claude/scripts/nested_root_check.py parents[1] loader (imports sibling path_resolve.py)
    "lessons_guard.py",             # IVG-119: wrapped impl; required by ~/.claude/scripts/lessons_guard.py parents[1] loader
    "decision_gate_guard.py",       # IVG-150: fail-closed decision-gate guard core impl; required by ~/.claude/scripts/decision_gate_guard.py parents[1] loader
    "context_budget_guard.py",      # IVG-141: on-demand context-budget guard core impl; required by ~/.claude/scripts/context_budget_guard.py parents[1] loader
    "boundary_checkpoint.py",       # IVG-141: phase/task-boundary checkpoint writer core impl; required by ~/.claude/scripts/boundary_checkpoint.py parents[1] loader
    "run_state.py",                 # IVG-258: task-keyed run-state writer/reader core impl; required by ~/.claude/scripts/run_state.py parents[1] loader
    "compaction_telemetry.py",      # IVG-258 stage 5: compaction-telemetry sink reader core impl; required by ~/.claude/scripts/compaction_telemetry.py parents[1] loader
    "known_red.py",  # IVG-144: known-red manifest reader/matcher core impl (wrapped portable-core — also in DEPLOYED_SCRIPTS)
    "workspace.py",  # IVG-158: parallel-feature-isolation workspace create core+wrapper
    "plan_path_lint.py",  # IVG-143: wrapped impl; required by ~/.claude/scripts/plan_path_lint.py parents[1] loader
    "authored_content_lint.py",  # advisory authored-content lint core impl (wrapped portable-core)
    "gate_fullsuite_sidecar.py",  # IVG-249 stage-3 gate full-suite freshness sidecar (wrapped portable-core — also in CORE_SCRIPTS)
    "handoff_validate.py",  # IVG-248: inter-agent handoff envelope validator core impl; required by ~/.claude/scripts/handoff_validate.py parents[1] loader
    "comment_cleanup.py",  # pre-PR comment cleanup core impl; required by the wrapper's parents[1] loader
)


def deploy_core_scripts(source_dir: pathlib.Path, dest_root: pathlib.Path) -> None:
    """Copy portable core scripts from source_dir/core/scripts/ to dest_root/core/scripts/."""
    src_core = source_dir / "core" / "scripts"
    dst_core = dest_root / "core" / "scripts"
    dst_core.mkdir(parents=True, exist_ok=True)
    for fname in CORE_SCRIPTS:
        src = src_core / fname
        if not src.exists():
            print(f"quoin: Expected core script {fname} at {src} but not found", file=sys.stderr)
            sys.exit(1)
        dst = dst_core / fname
        _copy_with_substitution(src, dst, dest_root)
        print(f"Copied core {fname} to {dest_root}/core/scripts/")


# Portable core workflow docs deployed alongside core scripts in ~/.claude/core/workflow/.
# IVG-248: runtime-neutral workflow semantics (rules.md, task-layout.md, session-state.md,
# cost-ledger.md, skills.md, skills.json) plus the inter-agent handoff spec (T-06, D-10).
CORE_WORKFLOW_FILES = (
    "cost-ledger.md",
    "handoff-format-reference.md",
    "handoff-format.md",
    "rules.md",
    "session-state.md",
    "skills.json",
    "skills.md",
    "task-layout.md",
)


def deploy_core_workflow(source_dir: pathlib.Path, dest_root: pathlib.Path) -> None:
    """Copy portable core workflow docs from source_dir/core/workflow/ to dest_root/core/workflow/."""
    src_workflow = source_dir / "core" / "workflow"
    dst_workflow = dest_root / "core" / "workflow"
    dst_workflow.mkdir(parents=True, exist_ok=True)
    for fname in CORE_WORKFLOW_FILES:
        src = src_workflow / fname
        if not src.exists():
            print(f"quoin: Expected core workflow file {fname} at {src} but not found", file=sys.stderr)
            sys.exit(1)
        dst = dst_workflow / fname
        _copy_with_substitution(src, dst, dest_root)
        print(f"Copied core workflow {fname} to {dest_root}/core/workflow/")


def deploy_scripts(source_dir: pathlib.Path, dest_root: pathlib.Path) -> None:
    """Copy scripts from source_dir/scripts/ to dest_root/scripts/."""
    src_scripts = source_dir / "scripts"
    dst_scripts = dest_root / "scripts"
    dst_scripts.mkdir(parents=True, exist_ok=True)
    for fname in DEPLOYED_SCRIPTS:
        src = src_scripts / fname
        if not src.exists():
            print(f"quoin: Expected {fname} at {src} but not found", file=sys.stderr)
            sys.exit(1)
        dst = dst_scripts / fname
        _copy_with_substitution(src, dst, dest_root)
        print(f"Copied {fname} to {dest_root}/scripts/")


# ── IVG-136: read-only deploy-drift detection ────────────────────────────────

# Category names compute_drift knows how to compare. Kept in sync with the
# deploy manifests above. The CLI (deploy_drift_check.py) surfaces this list as
# `checked_categories` and names everything NOT here as `uncovered_categories`.
DRIFT_CATEGORIES: tuple[str, ...] = ("skills", "scripts", "core-scripts", "core-workflow", "memory")


class DriftEntry(NamedTuple):
    """One drifted deployed file.

    reason is "missing" (source present, deployed copy absent) or "stale"
    (deployed bytes differ from what deploy would write).
    """
    category: str
    source_path: str
    deployed_path: str
    reason: str  # "missing" | "stale"


def compute_drift(
    source_dir: pathlib.Path,
    dest_root: pathlib.Path,
    categories: Optional[tuple[str, ...]] = None,
) -> list[DriftEntry]:
    """Return the list of deployed files that drifted from source_dir (T-02).

    Iterates the SAME manifest tuples the deploy functions use — TIER1_MEMORY_FILES,
    CANONICAL_SKILLS (via resolve_skill_source_md, + preamble.md when the source stub
    carries one), DEPLOYED_SCRIPTS, CORE_SCRIPTS — and compares each deployed file
    under dest_root against expected_deployed_content(src, dest_root). Per file:
      * deployed copy absent            -> DriftEntry(..., reason="missing")
      * deployed bytes != expected      -> DriftEntry(..., reason="stale")
      * __QUOIN_HOME__ substitution parity is preserved because the comparison goes
        through expected_deployed_content (which substitutes), so a source file
        holding the placeholder is NOT flagged against its substituted deployed copy.

    Pure and total: never writes, never raises. A canonical skill whose SOURCE
    SKILL.md is entirely absent on disk (MIN-3) is SILENTLY SKIPPED — it cannot be
    compared, and deploy_skills would have aborted the install before it ever
    deployed, so an absent source is not a deploy-drift condition. Unreadable files
    degrade to "no drift for this file" (OSError swallowed). D-08's main() exception
    wrapper in the CLI is the second line of defense if this contract is violated.

    categories: restrict the comparison to a subset of DRIFT_CATEGORIES; None (default)
    checks all of them.
    """
    selected = set(categories) if categories is not None else set(DRIFT_CATEGORIES)
    drift: list[DriftEntry] = []

    def _check(category: str, src: pathlib.Path, deployed: pathlib.Path) -> None:
        if not src.exists():
            return  # source absent: cannot compare, never raise (MIN-3)
        if not deployed.exists():
            drift.append(DriftEntry(category, str(src), str(deployed), "missing"))
            return
        try:
            expected = expected_deployed_content(src, dest_root)
            actual = deployed.read_bytes()
        except (OSError, UnicodeDecodeError):
            # UnicodeDecodeError: expected_deployed_content calls read_text(encoding="utf-8")
            # for substitute-extension sources; a non-UTF-8 source file raises here, not OSError.
            # Caught alongside OSError to honor the "never raises" contract (review MINOR-1).
            return  # unreadable — degrade to "no drift for this file"
        if expected != actual:
            drift.append(DriftEntry(category, str(src), str(deployed), "stale"))

    if "memory" in selected:
        src_mem = source_dir / "memory"
        dst_mem = dest_root / "memory"
        for fname in TIER1_MEMORY_FILES:
            _check("memory", src_mem / fname, dst_mem / fname)

    if "skills" in selected:
        src_skills = source_dir / "skills"
        src_adapter = source_dir / "adapters" / "claude" / "skills"
        dst_skills = dest_root / "skills"
        for name in CANONICAL_SKILLS:
            skill_md = resolve_skill_source_md(src_skills, src_adapter, name)
            _check("skills", skill_md, dst_skills / name / "SKILL.md")
            preamble = src_skills / name / "preamble.md"
            if preamble.exists():
                _check("skills", preamble, dst_skills / name / "preamble.md")

    if "scripts" in selected:
        src_scripts = source_dir / "scripts"
        dst_scripts = dest_root / "scripts"
        for fname in DEPLOYED_SCRIPTS:
            _check("scripts", src_scripts / fname, dst_scripts / fname)

    if "core-scripts" in selected:
        src_core = source_dir / "core" / "scripts"
        dst_core = dest_root / "core" / "scripts"
        for fname in CORE_SCRIPTS:
            _check("core-scripts", src_core / fname, dst_core / fname)

    if "core-workflow" in selected:
        src_workflow = source_dir / "core" / "workflow"
        dst_workflow = dest_root / "core" / "workflow"
        for fname in CORE_WORKFLOW_FILES:
            _check("core-workflow", src_workflow / fname, dst_workflow / fname)

    return drift


# T-12: Dashboard asset directory — fixed set of SPA files
_DASHBOARD_ASSETS = ("index.html", "dashboard.css", "app.js", "memory.js")


def deploy_dashboard_assets(source_dir: pathlib.Path, dest_root: pathlib.Path) -> None:
    """Copy the dashboard SPA assets from source_dir/core/scripts/dashboard_assets/
    to dest_root/core/scripts/dashboard_assets/.

    (a) Creates the destination directory with parents=True before copying
        (fresh-install safety — _copy_with_substitution does not mkdir).
    (b) Copies index.html, dashboard.css, app.js, memory.js via _copy_with_substitution
        (no __QUOIN_HOME__ placeholders in these files; byte-copy harmless).
    (c) Fails with clear stderr + sys.exit(1) if a source asset is missing.

    Per D-11 (arch R-06): caller is _cmd_claude_install, after deploy_core_scripts.
    """
    src_assets = source_dir / "core" / "scripts" / "dashboard_assets"
    dst_assets = dest_root / "core" / "scripts" / "dashboard_assets"
    # (a) mkdir before any copy
    dst_assets.mkdir(parents=True, exist_ok=True)
    # (b) copy each asset
    for fname in _DASHBOARD_ASSETS:
        src = src_assets / fname
        if not src.exists():
            print(
                f"quoin: Expected dashboard asset {fname} at {src} but not found; "
                "ensure quoin/core/scripts/dashboard_assets/ is present in the source tree",
                file=sys.stderr,
            )
            sys.exit(1)
        dst = dst_assets / fname
        _copy_with_substitution(src, dst, dest_root)
        print(f"Copied dashboard asset {fname} to {dst_assets}/")


def _merge_skill_overrides(settings: dict) -> int:
    """Merge SKILL_OVERRIDES into settings['skillOverrides'].

    Only touches keys in SKILL_OVERRIDES. User-added keys for skills not in
    our canonical list are preserved untouched. Canonical keys are always
    set to the canonical value (user changes to canonical keys are reset on reinstall).

    Returns count of keys added or changed.
    """
    overrides = settings.setdefault("skillOverrides", {})
    changed = 0
    for skill, tier in SKILL_OVERRIDES.items():
        if overrides.get(skill) != tier:
            overrides[skill] = tier
            changed += 1
    return changed


_QUOIN_ENV_KEYS = ("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "CLAUDE_CODE_AUTO_COMPACT_WINDOW")


def _merge_env(settings: dict, *, pct: int | None, window: int | None) -> int | None:
    """Merge the auto-compaction delegation vars into settings['env'].

    Writes ONLY the keys whose value is not None (D-02: the two are
    independent). Returns the count of keys added or changed. Never
    removes a key here.

    If settings['env'] already exists and is not a dict (e.g. a
    hand-edited `"env": null` or `"env": "FOO=bar"`), returns None as a
    sentinel instead of raising or overwriting the existing value — the
    caller turns that into a stderr warning naming the key and leaves
    the file untouched.
    """
    if pct is not None and not (1 <= pct <= 100):
        raise ValueError(f"autocompact pct must be 1..100, got {pct}")
    if window is not None and not (100000 <= window <= 1000000):
        raise ValueError(f"autocompact window must be 100000..1000000, got {window}")

    if "env" in settings and not isinstance(settings["env"], dict):
        return None

    env = settings.setdefault("env", {})
    changed = 0
    if pct is not None:
        value = str(pct)
        if env.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE") != value:
            env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] = value
            changed += 1
    if window is not None:
        value = str(window)
        if env.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW") != value:
            env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = value
            changed += 1
    return changed


def _clear_env(settings: dict) -> int:
    """Remove ONLY the two keys quoin manages from settings['env'].

    Leaves every other env key untouched; deletes the 'env' dict itself
    only when it becomes empty (so a default install never leaves an
    empty {} behind for the no-env-by-default assertion in T-05). A
    non-dict 'env' (or a missing one) is treated as nothing to clear.
    """
    if "env" not in settings or not isinstance(settings["env"], dict):
        return 0
    existing = settings["env"]
    removed = 0
    for key in _QUOIN_ENV_KEYS:
        if key in existing:
            del existing[key]
            removed += 1
    if not existing:
        del settings["env"]
    return removed


def deploy_hooks(
    source_dir: pathlib.Path,
    dest_root: pathlib.Path,
    *,
    is_project_mode: bool = False,
    autocompact_pct: int | None = None,
    autocompact_window: int | None = None,
    clear_autocompact_env: bool = False,
) -> None:
    """Copy hook scripts and merge hook stanzas into dest_root/settings.json.

    In project mode, hooks are scoped to <project>/.claude/settings.json only.
    Home ~/.claude/settings.json is NOT modified.
    """
    # _lib.sh copies FIRST: userpromptsubmit.sh calls
    # run_state_probe unguarded (no `command -v` check, unlike the SKILL.md
    # arms), so during an install window — or after an install aborted
    # mid-copy — a userpromptsubmit.sh that already has the new call but
    # sources a not-yet-updated _lib.sh would emit "command not found" on
    # stderr and report no active run while one is live. Copying _lib.sh
    # first closes that window; it never depends on the other hook scripts.
    hook_scripts = ("_lib.sh", "userpromptsubmit.sh", "precompact.sh", "postcompact.sh", "sessionstart.sh", "sessionend.sh", "worktreecreate.sh")
    src_hooks = source_dir / "hooks"
    dst_hooks = dest_root / "hooks"
    dst_hooks.mkdir(parents=True, exist_ok=True)

    # Copy hook scripts (with __QUOIN_HOME__ substitution for .sh files)
    for fname in hook_scripts:
        src = src_hooks / fname
        if not src.exists():
            print(f"quoin: Expected hook {fname} at {src} but not found", file=sys.stderr)
            sys.exit(1)
        dst = dst_hooks / fname
        _copy_with_substitution(src, dst, dest_root)
        print(f"Copied hook {fname} to {dest_root}/hooks/")

    # Merge 8 hook stanzas into settings.json using Python json module
    # settings.json lives inside dest_root (~/.claude/settings.json)
    settings_path = dest_root / "settings.json"

    # Load existing settings or start fresh
    if settings_path.exists():
        try:
            with open(settings_path) as f:
                settings = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            # Back up the broken file with a timestamp so prior backups are preserved
            ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup = settings_path.parent / f"{settings_path.name}.bak-{ts}"
            shutil.copyfile(settings_path, backup)
            print(
                f"quoin: settings.json parse error ({exc}); backed up to {backup} and starting fresh",
                file=sys.stderr,
            )
            settings = {}
    else:
        settings = {}

    hooks = settings.setdefault("hooks", {})

    # Helper: append a stanza, replacing any existing entry for the same
    # (matcher, script-filename) pair — the dedup key is (matcher, script
    # basename), so a stanza on a new matcher for an already-registered
    # script is added rather than replacing an existing one. Tracks the
    # number of stanzas registered so the printed count can never drift
    # from the calls below (D-02).
    _stanza_count = [0]

    def _append_stanza(event: str, matcher: str, command: str, timeout: int) -> None:
        stanza = {
            "matcher": matcher,
            "hooks": [{"type": "command", "command": command, "timeout": timeout}],
        }
        event_list = hooks.setdefault(event, [])
        script_name = command.rsplit("/", 1)[-1]
        # Remove stale entries for the same matcher + script filename (handles path changes)
        event_list[:] = [
            s for s in event_list
            if not (
                s.get("matcher") == matcher
                and any(
                    h.get("command", "").endswith(script_name)
                    for h in s.get("hooks", [])
                )
            )
        ]
        event_list.append(stanza)
        _stanza_count[0] += 1

    hooks_dir = str(dest_root.resolve() / "hooks")
    _append_stanza("UserPromptSubmit", "*",       f"{hooks_dir}/userpromptsubmit.sh", 5)
    _append_stanza("PreCompact",       "auto",    f"{hooks_dir}/precompact.sh",       10)
    _append_stanza("PostCompact",      "auto",    f"{hooks_dir}/postcompact.sh",      5)
    _append_stanza("SessionStart",     "startup", f"{hooks_dir}/sessionstart.sh",     5)
    _append_stanza("SessionStart",     "resume",  f"{hooks_dir}/sessionstart.sh",     5)
    _append_stanza("SessionStart",     "compact", f"{hooks_dir}/sessionstart.sh",     5)
    _append_stanza("SessionEnd",       "*",       f"{hooks_dir}/sessionend.sh",       5)
    _append_stanza("WorktreeCreate",   "*",       f"{hooks_dir}/worktreecreate.sh",   10)

    # Merge rm -rf / rm -fr deny rules into permissions.deny (idempotent).
    # These prevent accidental recursive deletes while still allowing plain rm.
    _DENY_RULES = [
        "Bash(rm -rf:*)",
        "Bash(rm -rf *)",
        "Bash(rm -fr:*)",
        "Bash(rm -fr *)",
    ]
    permissions = settings.setdefault("permissions", {})
    deny_list = permissions.setdefault("deny", [])
    added = 0
    for rule in _DENY_RULES:
        if rule not in deny_list:
            deny_list.append(rule)
            added += 1

    # Merge skillOverrides (idempotent — only canonical keys, user keys preserved).
    _merge_skill_overrides(settings)

    # Opt-in platform-threshold delegation (off by default — D-02, R-08).
    if clear_autocompact_env:
        removed = _clear_env(settings)
        if removed:
            print(f"Cleared {removed} autocompact env var(s) from {settings_path}")
    elif autocompact_pct is not None or autocompact_window is not None:
        env_changed = _merge_env(settings, pct=autocompact_pct, window=autocompact_window)
        if env_changed is None:
            print(
                f"quoin: {settings_path}['env'] already exists and is not an object; "
                "leaving it untouched (autocompact env vars NOT written)",
                file=sys.stderr,
            )
        elif env_changed:
            parts = []
            if autocompact_pct is not None:
                parts.append(f"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE={autocompact_pct}")
            if autocompact_window is not None:
                parts.append(f"CLAUDE_CODE_AUTO_COMPACT_WINDOW={autocompact_window}")
            print(f"Set {', '.join(parts)} in {settings_path}['env']")
    # else: default path — the env key is never created, never read, never removed

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    with open(settings_path, "w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")
    print(f"Merged {_stanza_count[0]} hook stanzas into {settings_path}")
    if added:
        print(f"Added {added} rm -rf/rm -fr deny rule(s) to {settings_path}")
    print(f"Set {len(SKILL_OVERRIDES)} skill overrides in {settings_path}")
    if is_project_mode:
        print(
            f"Hooks registered in {settings_path} (project-scoped). "
            "Home hooks (if any) are NOT removed by this install — see T-13."
        )


def cleanup_obsolete_scripts(dest_root: pathlib.Path) -> None:
    """Remove obsolete scripts from dest_root/scripts/ if present."""
    dst_scripts = dest_root / "scripts"
    for fname in OBSOLETE_SCRIPTS:
        target = dst_scripts / fname
        if target.exists():
            target.unlink()
            print(f"Removed obsolete {fname} from {dest_root}/scripts/ (Stage 5 cleanup)")
    dst_tests = dst_scripts / "tests"
    for fname in OBSOLETE_TESTS:
        target = dst_tests / fname
        if target.exists():
            target.unlink()
            print(f"Removed obsolete {fname} from {dest_root}/scripts/tests/ (Stage 5 cleanup)")


# ── T-06 ──────────────────────────────────────────────────────────────────────

def merge_workflow_rules(
    source_dir: pathlib.Path,
    dest_root: pathlib.Path,
    *,
    force_merge: bool = False,
    claude_md_path: pathlib.Path | None = None,
    source_claude_name: str = "CLAUDE.md",
) -> None:
    """Merge quoin workflow rules into the target CLAUDE.md file.

    In user mode: target is dest_root/CLAUDE.md (i.e. ~/.claude/CLAUDE.md).
    In project mode (D-02): caller passes claude_md_path = dest_root.parent/CLAUDE.md
    (i.e. <project>/CLAUDE.md, NOT <project>/.claude/CLAUDE.md).

    T-05 / CRIT-4: in project mode, __QUOIN_HOME__ placeholders in the source
    CLAUDE.md are substituted with the actual dest_root before writing, so that
    the deployed rules refer to the correct project-scoped paths.

    IVG-164 stage 1 (T-07): source_claude_name selects which source file to
    merge from (source_dir / source_claude_name) — "CLAUDE.md" (default, full
    variant) or "CLAUDE.slim.md" (project-scope slim pilot).
    """
    source_claude = source_dir / source_claude_name
    if not source_claude.exists():
        print(f"quoin: Expected {source_claude_name} at {source_claude} but not found", file=sys.stderr)
        sys.exit(1)

    raw_rules = source_claude.read_text(encoding="utf-8")
    # CRIT-4: substitute __QUOIN_HOME__ before embedding in the marker section
    new_rules = substitute_quoin_home(raw_rules, dest_root)
    new_section = f"{_MARKER_START}\n{new_rules}\n{_MARKER_END}"

    # Resolve target CLAUDE.md path (explicit > user-mode default)
    if claude_md_path is None:
        claude_md_path = dest_root / "CLAUDE.md"

    dest_claude = claude_md_path
    content = dest_claude.read_text(encoding="utf-8") if dest_claude.exists() else ""

    pair_count = content.count(_MARKER_START)

    if pair_count == 0:
        # behavior B: append
        dest_claude.parent.mkdir(parents=True, exist_ok=True)
        with open(dest_claude, "a", encoding="utf-8") as f:
            f.write(f"\n{_MARKER_START}\n{new_rules}\n{_MARKER_END}\n")
        print(f"Appended quoin rules to {dest_claude}")

    elif pair_count == 1:
        # behavior A: replace (DOTALL — spans newlines)
        updated = re.sub(
            rf"{re.escape(_MARKER_START)}.*?{re.escape(_MARKER_END)}",
            new_section,
            content,
            flags=re.DOTALL,
        )
        dest_claude.write_text(updated, encoding="utf-8")
        print(f"Updated quoin section in {dest_claude}")

    else:
        # pair_count > 1
        if not force_merge:
            # behavior C: abort with recovery hint
            print(
                f"quoin: {dest_claude} contains {pair_count} '# === DEV WORKFLOW' marker pairs "
                f"(expected 0 or 1); run 'quoin doctor' to inspect, OR re-run "
                f"'quoin install --force-merge' to keep the first pair and remove the rest",
                file=sys.stderr,
            )
            sys.exit(2)
        else:
            # behavior D: keep first pair (with new content), delete the rest
            pattern = re.compile(
                rf"{re.escape(_MARKER_START)}.*?{re.escape(_MARKER_END)}",
                re.DOTALL,
            )
            matches = list(pattern.finditer(content))
            extra_count = len(matches) - 1

            # Emit per-deletion stderr warnings (compute line numbers before modifying)
            for m in matches[1:]:
                line_no = content[: m.start()].count("\n") + 1
                print(
                    f"quoin: removed extra '# === DEV WORKFLOW' marker pair at line {line_no}",
                    file=sys.stderr,
                )

            # Remove extra pairs from end to preserve earlier positions
            result = content
            for m in reversed(matches[1:]):
                result = result[: m.start()] + result[m.end() :]

            # Replace the first pair (now the only one) with new content
            result = re.sub(
                rf"{re.escape(_MARKER_START)}.*?{re.escape(_MARKER_END)}",
                new_section,
                result,
                count=1,
                flags=re.DOTALL,
            )
            dest_claude.write_text(result, encoding="utf-8")
            print(
                f"Updated quoin section in {dest_claude} "
                f"(--force-merge: removed {extra_count} extra marker pairs)"
            )


# ── T-07 ──────────────────────────────────────────────────────────────────────

def check_prerequisites() -> list[str]:
    """Return list of missing required tools; warn about optional ones."""
    missing: list[str] = []
    if shutil.which("claude") is None:
        missing.append("claude (Claude Code CLI)")
    if shutil.which("git") is None:
        missing.append("git")
    if shutil.which("gh") is None:
        print(
            "Warning: gh (GitHub CLI) not found — /end_of_task push will still work, but PR creation won't.",
            file=sys.stderr,
        )
    if shutil.which("npx") is None:
        print(
            "Warning: npx not found — cost tracking in /end_of_task requires npx "
            "(install Node.js from https://nodejs.org).",
            file=sys.stderr,
        )
    return missing


def deploy_agentdesk(source_dir: pathlib.Path, dest_agentdesk_dir: pathlib.Path) -> None:
    """Copy agentdesk tool files to dest_agentdesk_dir (~/.config/agentdesk/).

    If source files are missing, logs a warning and returns without error.
    agentdesk is an optional user-level tool; its absence must not abort install.
    """
    src_agentdesk = source_dir / "tools" / "agentdesk"
    if not src_agentdesk.exists():
        print(f"[warn] agentdesk source not found: {src_agentdesk}", file=sys.stderr)
        return

    dest_agentdesk_dir.mkdir(parents=True, exist_ok=True)

    # Copy agentdesk.zsh — _copy_with_substitution only sets +x for .py/.sh,
    # so we call os.chmod explicitly for the .zsh file.
    _copy_with_substitution(
        src_agentdesk / "agentdesk.zsh",
        dest_agentdesk_dir / "agentdesk.zsh",
        dest_agentdesk_dir,
    )
    os.chmod(dest_agentdesk_dir / "agentdesk.zsh", 0o755)

    # Copy setup-agentdesk.sh — .sh extension, _copy_with_substitution sets +x.
    _copy_with_substitution(
        src_agentdesk / "setup-agentdesk.sh",
        dest_agentdesk_dir / "setup-agentdesk.sh",
        dest_agentdesk_dir,
    )

    print(f"Deployed agentdesk to {dest_agentdesk_dir}")


def _has_pyyaml() -> bool:
    """True if pyyaml is importable in the current interpreter.

    A monkeypatchable seam for tests. build_preambles.py --check needs pyyaml
    to parse preamble frontmatter; checking here first avoids spawning a
    child process that would fail for a reason unrelated to staleness.
    """
    import importlib.util

    return importlib.util.find_spec("yaml") is not None


def _looks_like_source_tree(source_dir: pathlib.Path) -> bool:
    """True if source_dir looks like a working clone, not a wheel/site-packages copy.

    Mirrors conjuncts (c) and (d) of cli.py's _derive_allow_writes — copied,
    not imported, because installer.py must not import cli.py.
    """
    if not (source_dir / "skills").is_dir():
        return False
    parent = source_dir.parent
    return (parent / ".git").is_dir() or (parent / "pyproject.toml").is_file()


def _preamble_check_timeout() -> int:
    """Read QUOIN_SUBPROCESS_TIMEOUT (seconds); default 30; bad values fall back to 30.

    Self-contained local copy — do NOT cross-import; each touched script owns
    its own copy per the repo's copy-not-import convention.
    """
    try:
        return int(os.environ.get("QUOIN_SUBPROCESS_TIMEOUT", "30"))
    except (TypeError, ValueError):
        return 30


def _advise_preamble_freshness(source_dir: pathlib.Path) -> None:
    """User-mode advisory: warn (never fail) when source-tree preambles are stale.

    Runs build_preambles.py --check as a subprocess so any bad exit code or
    exception in the child can never abort `quoin install`. Silent unless the
    check is inconclusive (soft note) or the child reports stale preambles
    (exit 7, [warn] block) — install always returns 0 regardless.
    """
    if os.environ.get("QUOIN_DISABLE_PREAMBLE_CHECK") == "1":
        return
    if not _looks_like_source_tree(source_dir):
        return
    script = source_dir / "scripts" / "build_preambles.py"
    if not script.is_file():
        print("[note] preamble freshness check skipped (generator not found)")
        return
    if not _has_pyyaml():
        print("[note] preamble freshness check skipped (pyyaml is a --dev dependency)")
        return
    try:
        result = subprocess.run(
            [sys.executable, str(script), "--check"],
            capture_output=True,
            text=True,
            timeout=_preamble_check_timeout(),
        )
    except Exception:
        print("[note] preamble freshness check inconclusive")
        return
    if result.returncode == 0:
        return
    if result.returncode == 7:
        print(
            "[warn] preambles are stale relative to quoin/memory/format-kit.md and "
            "glossary.md. Regenerate and commit:\n"
            "  python3 quoin/scripts/build_preambles.py  (or bash install.sh --dev)\n"
            "then commit quoin/skills/*/preamble.md",
            file=sys.stderr,
        )
        for line in result.stderr.splitlines():
            if line.strip():
                print(f"  {line}", file=sys.stderr)
        return
    print(f"[note] preamble freshness check inconclusive (exit {result.returncode})")


def regenerate_preambles(source_dir: pathlib.Path, *, allow_writes: bool) -> None:
    """Regenerate subagent preambles if running from a writable working tree."""
    if not allow_writes:
        print("Skipping preamble regeneration (user mode — pass --dev to regenerate from source)")
        _advise_preamble_freshness(source_dir)
        return
    import runpy

    script = source_dir / "scripts" / "build_preambles.py"
    # Isolate sys.argv so build_preambles.py's argparse sees only its own script name
    old_argv = sys.argv[:]
    try:
        sys.argv = [str(script)]
        runpy.run_path(str(script), run_name="__main__")
    except SystemExit as exc:
        # Scripts end with sys.exit(main()); catch the successful exit (code 0) so it
        # doesn't propagate and abort the caller.  Re-raise on any non-zero code.
        if exc.code != 0:
            raise
    finally:
        sys.argv = old_argv
    print(f"Regenerated subagent preambles in {source_dir}/skills/*/preamble.md")


def regenerate_pollution_dispatch(source_dir: pathlib.Path, *, allow_writes: bool) -> None:
    """Regenerate §0' Pollution dispatch, §0″ Minimum-tier guard (Opus), and §0‴
    Minimum-tier guard (Sonnet) blocks.

    Regenerates §0' (10 Opus-tier leaf skills carry pollution dispatch) AND
    §0″ (same 10 skills carry minimum-tier guard) AND §0‴ (10 Sonnet-tier
    cheap-tier skills carry the mirrored minimum-tier guard, IVG-117) in the
    adapter SKILL.md files at quoin/adapters/claude/skills/*/SKILL.md.

    Must be called BEFORE deploy_skills so the freshly-injected adapter SKILL.md is the
    file that deploy_skills copies to the deploy root (IVG-69, T-06, R-11).

    Note: allow_writes=False (user-mode installs) skips regeneration entirely — the
    committed adapter SKILL.md output is the delivery vehicle for end users. The
    --check flag (run in CI) is the guard against committed drift.
    """
    if not allow_writes:
        print("Skipping pollution dispatch regeneration (user mode — pass --dev to regenerate from source)")
        return
    import runpy

    script = source_dir / "scripts" / "inject_pollution_dispatch.py"
    # Isolate sys.argv so inject_pollution_dispatch.py's argparse sees only its own script name
    old_argv = sys.argv[:]
    try:
        sys.argv = [str(script)]
        runpy.run_path(str(script), run_name="__main__")
    except SystemExit as exc:
        # Scripts end with sys.exit(main()); catch the successful exit (code 0) so it
        # doesn't propagate and abort the caller.  Re-raise on any non-zero code.
        if exc.code != 0:
            raise
    finally:
        sys.argv = old_argv
    print(f"Regenerated §0' Pollution dispatch in {source_dir}/adapters/claude/skills/*/SKILL.md")


def regenerate_verification_step(source_dir: pathlib.Path, *, allow_writes: bool) -> None:
    """Regenerate §V Ground-truth verification blocks (IVG-115 T-04).

    Regenerates the late §V-verify block in end_of_day/start_of_day/weekly_review and
    the early §V-claims block in end_of_day, in the adapter SKILL.md files at
    quoin/adapters/claude/skills/*/SKILL.md.

    Must be called BEFORE deploy_skills so the freshly-injected adapter SKILL.md is the
    file that deploy_skills copies to the deploy root (mirrors regenerate_pollution_dispatch).

    Note: allow_writes=False (user-mode installs) skips regeneration entirely — the
    committed adapter SKILL.md output is the delivery vehicle for end users. The
    --check flag (run in CI) is the guard against committed drift.
    """
    if not allow_writes:
        print("Skipping §V verification-step regeneration (user mode — pass --dev to regenerate from source)")
        return
    import runpy

    script = source_dir / "scripts" / "inject_verification_step.py"
    # Isolate sys.argv so inject_verification_step.py's argparse sees only its own script name
    old_argv = sys.argv[:]
    try:
        sys.argv = [str(script)]
        runpy.run_path(str(script), run_name="__main__")
    except SystemExit as exc:
        if exc.code != 0:
            raise
    finally:
        sys.argv = old_argv
    print(f"Regenerated §V verification blocks in {source_dir}/adapters/claude/skills/*/SKILL.md")


def check_slim_outputs_fresh(source_dir: pathlib.Path) -> list[str]:
    """Byte-compare committed CLAUDE.slim.md / memory/workflow-catalog.md against a
    fresh regen (IVG-164 review round-1 MAJOR 1).

    Project-scope installs always run with allow_writes=False (T-07 MAJ-6), so
    regenerate_pollution_dispatch's self-heal pattern above can never fire for the
    one scope where --claude-md-variant slim is legal — an edit to quoin/CLAUDE.md
    that skips re-running build_claude_slim.py would otherwise deploy stale
    generated content silently. This is a read-only CHECK, never a write: it
    borrows build_claude_slim.py's `build_outputs()` function and CLASSIFICATION
    table via runpy with a non-`"__main__"` run_name, so the script's own
    argparse/main()/file-write side effects never execute.

    Returns a list of stale output path strings (empty when both generated files
    match a fresh regen of the live source_dir/CLAUDE.md). A ClassificationError
    from the borrowed build_outputs() (heading/table drift) is treated the same as
    staleness — the generator itself would abort, so the install must too.
    """
    import runpy

    script = source_dir / "scripts" / "build_claude_slim.py"
    claude_md = source_dir / "CLAUDE.md"
    slim_output = source_dir / "CLAUDE.slim.md"
    catalog_output = source_dir / "memory" / "workflow-catalog.md"

    # review-1 minor (widened per round-2 minor 3): a missing/unparsable script,
    # a renamed symbol, or an absent CLAUDE.md must surface as a clean stale
    # entry (the caller aborts with the regenerate-and-commit message), never as
    # an uncaught traceback. runpy.run_path EXECUTES the target module, so the
    # exception surface is arbitrary user code — module-level ImportError,
    # NameError, ValueError, even SystemExit — not a known API. Catch everything.
    try:
        mod_globals = runpy.run_path(str(script), run_name="quoin_slim_staleness_check")
        build_outputs = mod_globals["build_outputs"]
        classification_error = mod_globals["ClassificationError"]
        source_text = claude_md.read_text(encoding="utf-8")
    except (Exception, SystemExit) as exc:
        return [f"{script} (staleness check unavailable: {type(exc).__name__}: {exc})"]
    try:
        slim_text, catalog_text = build_outputs(source_text)
    except classification_error as exc:
        return [f"{claude_md} (regen aborted, CLASSIFICATION/heading mismatch: {exc})"]

    stale: list[str] = []
    if not slim_output.exists() or slim_output.read_text(encoding="utf-8") != slim_text:
        stale.append(str(slim_output))
    if not catalog_output.exists() or catalog_output.read_text(encoding="utf-8") != catalog_text:
        stale.append(str(catalog_output))
    return stale


def assert_no_placeholders(dest_root: pathlib.Path) -> list[str]:
    """Return list of 'path:line_no' strings where __QUOIN_HOME__ was found after deploy.

    Call this after all deploy functions complete to verify substitution was fully applied.
    Only scans quoin-deployed paths (root-level files + _QUOIN_DEPLOYED_SUBDIRS) to avoid
    false positives from Claude Code internal directories (projects/, todos/, etc.).
    """
    violations: list[str] = []
    check_exts = (".md", ".sh", ".py", ".json", ".yaml", ".txt")

    def _scan_file(p: pathlib.Path) -> None:
        if p.is_file() and p.suffix in check_exts:
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
                for i, line in enumerate(text.splitlines(), 1):
                    if "__QUOIN_HOME__" in line:
                        violations.append(f"{p}:{i}")
            except Exception:
                pass

    # 1. Root-level files only (CLAUDE.md, QUICKSTART.md, settings.json, etc.)
    for entry in dest_root.iterdir():
        if entry.is_file():
            _scan_file(entry)

    # 2. Quoin-deployed subdirectories only
    for subdir_name in _QUOIN_DEPLOYED_SUBDIRS:
        subdir = dest_root / subdir_name
        if subdir.is_dir():
            for p in subdir.rglob("*"):
                _scan_file(p)

    return violations


def install_dev_deps() -> None:
    """Install dev Python dependencies via pip (uses quoin[dev] extras)."""
    if shutil.which("pip3") is None and shutil.which("pip") is None:
        print(
            "Warning: pip not found — install quoin[dev] manually for dev tests",
            file=sys.stderr,
        )
        return
    pip_cmd = shutil.which("pip3") or shutil.which("pip")
    assert pip_cmd is not None  # guaranteed: early return above covers the both-None case
    result = subprocess.run(
        [pip_cmd, "install", "--user", "--upgrade", "quoin[dev]"],
    )
    if result.returncode != 0:
        print(
            "Warning: pip install failed; install quoin[dev] manually for dev tests",
            file=sys.stderr,
        )
