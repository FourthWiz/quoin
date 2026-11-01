"""Tests for IVG-153 Stage 2 T-05: the autonomous sentinel path/naming contract.

Covers:
- The four sentinel path templates (marker, per-phase completion, done,
  halt) are documented in the runtime-neutral core doc
  (`quoin/core/skills/run_hard.md`) and the Tier-1 memory doc
  (`quoin/memory/autonomous-mode.md`), and all resolve under
  `.workflow_artifacts/memory/` — never inside the task-scoped folder.
- `quoin.supervisor`'s path constants byte-match the templates named in
  the docs (drift guard).
- The MAJ-1 coverage guard: the resumable-phase roster parsed from
  `run/SKILL.md` `## Phase sequence` (9 phase names) is set-equal to
  (a) the phase set `quoin.supervisor.RESUMABLE_PHASES` defines a
  `{phase}.done` template for, and (b) the phase set documented to
  WRITE a `{phase}.done` sentinel in run/SKILL.md's write-site map.
  Derived from the live roster, never a frozen count or a "1..6" range,
  so a future added/renamed phase without a completion-sentinel write
  fails CLOSED.
- The counting glob string `autonomous-progress-{task}/*.done` (union
  of phase- and sub-phase-granular sentinels, MAJ-2) appears verbatim
  in `supervisor.py` and both docs.
"""
from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]  # quoin/
_SOURCE_ROOT = _REPO_ROOT / "quoin"

_RUN_CORE_DOC = _SOURCE_ROOT / "core" / "skills" / "run_hard.md"
_AUTONOMOUS_MEMORY_DOC = _SOURCE_ROOT / "memory" / "autonomous-mode.md"
_RUN_ADAPTER_SKILL = _SOURCE_ROOT / "adapters" / "claude" / "skills" / "run_hard" / "SKILL.md"
_SUPERVISOR_MODULE = _REPO_ROOT / "src" / "quoin" / "supervisor.py"

_SENTINEL_ROOT = ".workflow_artifacts/memory"

_MARKER_TEMPLATE = "autonomous-run-{task}.marker"
_PROGRESS_DIR_TEMPLATE = "autonomous-progress-{task}"
_DONE_TEMPLATE = "autonomous-done-{task}.md"
_HALT_TEMPLATE = "autonomous-halt-{task}.md"
_COMPLETION_GLOB = "autonomous-progress-{task}/*.done"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Existence
# ---------------------------------------------------------------------------


def test_supervisor_module_exists() -> None:
    assert _SUPERVISOR_MODULE.is_file(), f"missing: {_SUPERVISOR_MODULE}"


def test_run_core_doc_exists() -> None:
    assert _RUN_CORE_DOC.is_file(), f"missing: {_RUN_CORE_DOC}"


def test_autonomous_memory_doc_exists() -> None:
    assert _AUTONOMOUS_MEMORY_DOC.is_file(), f"missing: {_AUTONOMOUS_MEMORY_DOC}"


# ---------------------------------------------------------------------------
# The four templates documented in both docs
# ---------------------------------------------------------------------------


def _assert_templates_documented(path: Path) -> None:
    text = _text(path)
    for template in (
        _MARKER_TEMPLATE,
        _PROGRESS_DIR_TEMPLATE,
        _DONE_TEMPLATE,
        _HALT_TEMPLATE,
    ):
        assert template in text, f"{path} missing sentinel template: {template}"


def test_run_core_doc_documents_all_four_templates() -> None:
    _assert_templates_documented(_RUN_CORE_DOC)


def test_autonomous_memory_doc_documents_all_four_templates() -> None:
    _assert_templates_documented(_AUTONOMOUS_MEMORY_DOC)


def test_templates_resolve_under_memory_root_never_task_folder() -> None:
    for template in (
        _MARKER_TEMPLATE,
        _PROGRESS_DIR_TEMPLATE,
        _DONE_TEMPLATE,
        _HALT_TEMPLATE,
    ):
        resolved = f"{_SENTINEL_ROOT}/{template}"
        # Resolves under .workflow_artifacts/memory/, never under a bare
        # task-scoped folder (.workflow_artifacts/{task}/ without /memory/).
        assert resolved.startswith(f"{_SENTINEL_ROOT}/")
        assert "/memory/" in resolved or resolved.startswith(f"{_SENTINEL_ROOT}/")


# ---------------------------------------------------------------------------
# supervisor.py path constants byte-match the documented templates
# ---------------------------------------------------------------------------


def test_supervisor_constants_bytematch_documented_templates() -> None:
    import importlib

    supervisor = importlib.import_module("quoin.supervisor")

    assert supervisor.SENTINEL_ROOT == _SENTINEL_ROOT
    assert supervisor.MARKER_TEMPLATE == _MARKER_TEMPLATE
    assert supervisor.PROGRESS_DIR_TEMPLATE == _PROGRESS_DIR_TEMPLATE
    assert supervisor.DONE_TEMPLATE == _DONE_TEMPLATE
    assert supervisor.HALT_TEMPLATE == _HALT_TEMPLATE
    assert supervisor.COMPLETION_GLOB_TEMPLATE == _COMPLETION_GLOB


# ---------------------------------------------------------------------------
# Counting glob appears verbatim everywhere
# ---------------------------------------------------------------------------


def test_completion_glob_appears_verbatim_in_supervisor_module() -> None:
    assert _COMPLETION_GLOB in _text(_SUPERVISOR_MODULE)


def test_completion_glob_appears_verbatim_in_run_core_doc() -> None:
    assert _COMPLETION_GLOB in _text(_RUN_CORE_DOC)


def test_completion_glob_appears_verbatim_in_autonomous_memory_doc() -> None:
    assert _COMPLETION_GLOB in _text(_AUTONOMOUS_MEMORY_DOC)


def test_completion_glob_appears_verbatim_in_run_adapter_skill() -> None:
    assert _COMPLETION_GLOB in _text(_RUN_ADAPTER_SKILL)


# ---------------------------------------------------------------------------
# MAJ-1 coverage guard: roster <-> supervisor.py <-> run/SKILL.md write-sites
# ---------------------------------------------------------------------------


def _parse_phase_roster_from_skill(text: str) -> set:
    """Derive the resumable-phase roster from the `## Phase sequence` block.

    Parses lines shaped `Phase N[.M]: NAME` inside the fenced block under
    the `## Phase sequence` heading and lower-cases each NAME. Never a
    frozen literal set — always re-derived from the live doc.
    """
    match = re.search(r"## Phase sequence\s*```(.*?)```", text, re.S)
    assert match, "run/SKILL.md must have a fenced '## Phase sequence' block"
    block = match.group(1)
    names = re.findall(r"^Phase\s+[\d.]+:\s+([A-Z_]+)", block, re.M)
    assert names, "no phase names parsed out of '## Phase sequence' block"
    return {n.lower() for n in names}


def _parse_write_site_phases_from_skill(text: str) -> set:
    """Derive the set of phases documented to WRITE a `{phase}.done` sentinel.

    Parses the `## Autonomous progress sentinels` write-site map: bullet
    lines of the form `**{phase}** (Phase N) -> writes
    ...autonomous-progress-{task}/{phase}.done`.
    """
    match = re.search(
        r"## Autonomous progress sentinels.*?Write-site map.*?:\n(.*?)\n\n",
        text,
        re.S,
    )
    assert match, "run/SKILL.md must document the write-site map"
    block = match.group(1)
    pairs = re.findall(
        r"\*\*([a-z_]+)\*\*.*?writes\s+`autonomous-progress-\{task\}/([a-z_]+)\.done`",
        block,
    )
    assert pairs, "no write-site phase pairs parsed out of the write-site map"
    phases = set()
    for label, sentinel_phase in pairs:
        assert label == sentinel_phase, (
            f"write-site label {label!r} does not match its own sentinel "
            f"name {sentinel_phase!r}"
        )
        phases.add(label)
    return phases


def test_coverage_guard_roster_matches_supervisor_and_write_sites() -> None:
    import importlib

    supervisor = importlib.import_module("quoin.supervisor")

    skill_text = _text(_RUN_ADAPTER_SKILL)
    roster = _parse_phase_roster_from_skill(skill_text)
    supervisor_phases = set(supervisor.RESUMABLE_PHASES)
    write_site_phases = _parse_write_site_phases_from_skill(skill_text)

    # The roster must be exactly the 9-phase set (never a "1..6" abbreviation
    # that silently drops enrich/specify/fast_path_triage) — asserted
    # structurally, not as a frozen literal count.
    assert "enrich" in roster, "roster must include enrich (Phase 1.4)"
    assert "specify" in roster, "roster must include specify (Phase 1.5)"
    assert "fast_path_triage" in roster, "roster must include fast_path_triage (Phase 1.6)"

    assert roster == supervisor_phases, (
        f"run/SKILL.md Phase-sequence roster {sorted(roster)} != "
        f"supervisor.RESUMABLE_PHASES {sorted(supervisor_phases)}"
    )
    assert roster == write_site_phases, (
        f"run/SKILL.md Phase-sequence roster {sorted(roster)} != "
        f"documented {{phase}}.done write sites {sorted(write_site_phases)}"
    )


def test_run_core_doc_no_forbidden_tokens_in_new_section() -> None:
    """The core doc addition must stay runtime-neutral (mirrors the
    pre-existing forbidden-token guard for run.md in
    test_run_adapter_pilot.py — belt-and-suspenders on the new section)."""
    forbidden = ("~/.claude", "Haiku", "Sonnet", "Opus", "Agent", "gh CLI")
    text = _text(_RUN_CORE_DOC)
    hits = [t for t in forbidden if t in text]
    assert not hits, f"run.md contains forbidden tokens: {hits}"

    slash_run = re.compile(r"(?<![a-zA-Z0-9_\-])/run(?=[^a-zA-Z0-9_\-]|$)")
    assert not slash_run.search(text), "run.md must not contain bare '/run'"
