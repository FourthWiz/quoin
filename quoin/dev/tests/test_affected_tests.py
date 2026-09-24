"""Unit tests for quoin.core.scripts.affected_tests.

Tests are hermetic — they use git init in tmp_path and do NOT depend on
the real quoin tree or the network.

Coverage:
  - name-match: changed foo.py with test_foo.py -> selector
  - import-graph (whole-word \b{S}\b) with the real quoin dynamic-loader idiom
  - unmatched source: orphan.py with zero references -> unmatched_sources; exit 3
    without --allow-unmatched; exit 0 with --allow-unmatched when pytest passes
  - changed test file selected directly
  - --select-only does not invoke pytest (ran_pytest=False)
  - git-root resolution (CRIT-1): outer non-git dir with child git repo
  - diff-basis fallback (CRIT-2): HEAD==main + dirty .py -> worktree fallback
  - F-01 fix: committed .py source on branch with no upstream, clean worktree
    -> base-branch-diff (NOT no-changes)
  - F-01 end-to-end: committed-clean no-upstream branch + red test -> exit 1
  - F-02 fix: --allow-unmatched + single unmatched source, empty selectors -> exit 0
  - exit-code matrix: 0a, 0c, 1, 4, 2
  - docs-only branch (MAJ-1): .md/.json/SKILL.md only -> exit 0, ran_pytest=False,
    pytest NOT invoked (subprocess.run spy), exit_reason=docs-only-no-selectors
  - QUOIN_DISABLE_AFFECTED_TESTS=1 -> exit 3 + {"disabled": true}
  - determinism: selector list is sorted/stable
  - no --base flag (MIN-1)
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Helper: load the core module from its canonical source path (hermetic)
# ---------------------------------------------------------------------------

_CORE_PATH = Path(__file__).resolve().parents[2] / "core" / "scripts" / "affected_tests.py"


def _load_core():
    spec = importlib.util.spec_from_file_location("_quoin_core_affected_tests_test", _CORE_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


_at = _load_core()


# ---------------------------------------------------------------------------
# Fixture: tiny fake repo tree (no git)
# ---------------------------------------------------------------------------

@pytest.fixture()
def fake_repo(tmp_path):
    """Create a minimal fake repo with sources + test files (no git)."""
    # Source files
    (tmp_path / "foo.py").write_text("def foo(): pass\n")
    (tmp_path / "bar.py").write_text("def bar(): pass\n")
    (tmp_path / "orphan.py").write_text("def orphan(): pass\n")

    # Test files
    (tmp_path / "test_foo.py").write_text("# tests for foo\nimport foo\ndef test_foo(): pass\n")
    # test_misc.py uses the quoin dynamic-loader idiom for bar
    (tmp_path / "test_misc.py").write_text(
        "import importlib.util\n"
        "import sys\n"
        "from pathlib import Path\n"
        "_CORE_PATH = Path(__file__).resolve().parent / 'bar.py'\n"
        "_SPEC = importlib.util.spec_from_file_location('_quoin_core_bar_test', _CORE_PATH)\n"
        "_CORE = importlib.util.module_from_spec(_SPEC)\n"
        "sys.modules[_SPEC.name] = _CORE\n"
        "_SPEC.loader.exec_module(_CORE)\n"
        "def test_bar(): pass\n"
    )
    return tmp_path


# ---------------------------------------------------------------------------
# map_changed_to_tests
# ---------------------------------------------------------------------------

class TestMapChangedToTests:
    def test_name_match(self, fake_repo):
        """foo.py -> test_foo.py via name-match."""
        selectors, unmatched, ignored = _at.map_changed_to_tests(["foo.py"], fake_repo)
        assert any("test_foo.py" in s for s in selectors), f"expected test_foo.py in {selectors}"
        assert not unmatched
        assert not ignored

    def test_import_graph_whole_word(self, fake_repo):
        """bar.py has no test_bar.py but test_misc.py contains \\bbar\\b."""
        selectors, unmatched, ignored = _at.map_changed_to_tests(["bar.py"], fake_repo)
        assert any("test_misc.py" in s for s in selectors), (
            f"expected test_misc.py via whole-word grep, got {selectors}"
        )
        assert not unmatched, f"expected no unmatched, got {unmatched}"

    def test_unmatched_source(self, fake_repo):
        """orphan.py has no test anywhere -> unmatched_sources."""
        selectors, unmatched, ignored = _at.map_changed_to_tests(["orphan.py"], fake_repo)
        assert "orphan.py" in unmatched
        assert not selectors

    def test_changed_test_file_selected_directly(self, fake_repo):
        """test_foo.py itself -> included as a selector."""
        selectors, unmatched, ignored = _at.map_changed_to_tests(["test_foo.py"], fake_repo)
        assert any("test_foo.py" in s for s in selectors)
        assert not unmatched

    def test_ignored_non_py(self, fake_repo):
        """Non-.py files -> ignored (not unmatched_sources)."""
        selectors, unmatched, ignored = _at.map_changed_to_tests(
            ["gate/SKILL.md", "notes.md", "config.json"], fake_repo
        )
        assert not selectors
        assert not unmatched
        assert ignored  # all three should be in ignored

    def test_docs_to_tests_row_falls_back_to_ignored_when_mapped_test_absent(self, fake_repo):
        """A bare-filename _DOCS_TO_TESTS row (pyproject.toml) also matches
        this fake repo's own top-level pyproject.toml, but the mapped test
        (quoin/dev/tests/test_probe_gateway.py) does not exist here — the
        file must still land in `ignored`, not silently vanish with no
        selector and no ignored entry to show for it.
        """
        (fake_repo / "pyproject.toml").write_text("[project]\nname = 'fake'\n")
        selectors, unmatched, ignored = _at.map_changed_to_tests(["pyproject.toml"], fake_repo)
        assert not selectors
        assert not unmatched
        assert "pyproject.toml" in ignored

    def test_sh_test_file_not_selected_as_pytest_selector(self, fake_repo):
        """test_*.sh files must NOT be added as pytest selectors.

        IVG-95: test_sessionstart_pending_restore.sh is a changed shell-script
        test file whose name starts with test_.  Before the fix, map_changed_to_tests
        included it as a selector, causing pytest to exit 4 (collection error).
        After the fix (fpath.suffix == ".py" guard on line 400 of core/scripts/
        affected_tests.py), .sh files are routed to the ignored bucket instead.
        """
        sh_file = "dev/tests/test_sessionstart_pending_restore.sh"
        (fake_repo / "dev" / "tests").mkdir(parents=True, exist_ok=True)
        (fake_repo / sh_file).write_text("#!/usr/bin/env bash\necho ok\n")
        selectors, unmatched, ignored = _at.map_changed_to_tests([sh_file], fake_repo)
        assert not selectors, f"shell test file must not be a pytest selector, got {selectors}"
        assert not unmatched, f"shell test file must not be unmatched_sources, got {unmatched}"
        assert any(".sh" in i for i in ignored), f"shell test file must be in ignored, got {ignored}"

    def test_determinism(self, fake_repo):
        """Selector list is sorted and stable across calls."""
        s1, _, _ = _at.map_changed_to_tests(["foo.py", "bar.py"], fake_repo)
        s2, _, _ = _at.map_changed_to_tests(["bar.py", "foo.py"], fake_repo)
        assert s1 == s2, "selectors should be order-independent"
        assert s1 == sorted(s1), "selectors should be sorted"


# ---------------------------------------------------------------------------
# IVG-92: special-case docs→test mapping (uses real quoin repo tree)
# ---------------------------------------------------------------------------

# Resolve the quoin/ git repo root the same way the module's loader does:
# _CORE_PATH is <repo>/quoin/core/scripts/affected_tests.py
#   parents[0] = scripts/
#   parents[1] = core/
#   parents[2] = quoin/       (the quoin Python package)
#   parents[3] = <repo root>  (the quoin/ git repo)
_REPO_ROOT = _CORE_PATH.resolve().parents[3]

# Sanity guard: fail loudly if the index is wrong rather than silently
# testing against the wrong tree (R-02 mitigation).
assert (_REPO_ROOT / "quoin/dev/tests/test_affected_tests.py").exists(), (
    f"_REPO_ROOT ({_REPO_ROOT}) does not look like the quoin git repo root — "
    "check the parents[N] index in test_affected_tests.py"
)


class TestIvg92SpecialCaseMapping:
    """Verify that docs/source files in _DOCS_TO_TESTS map to their designated tests.

    These tests use the REAL quoin repo tree (not fake_repo) because the
    special-case block guards on test_path.exists() against the real filesystem.
    """

    def test_claude_md_triggers_size_ceiling(self):
        """quoin/CLAUDE.md -> test_claude_md_size_ceiling.py is in selectors."""
        selectors, unmatched, ignored = _at.map_changed_to_tests(
            ["quoin/CLAUDE.md"], _REPO_ROOT
        )
        assert any("test_claude_md_size_ceiling.py" in s for s in selectors), (
            f"expected test_claude_md_size_ceiling.py in selectors, got {selectors}"
        )
        assert not ignored, f"ignored should be empty for a mapped file, got {ignored}"
        assert not unmatched

    def test_glossary_triggers_preamble_freshness(self):
        """quoin/memory/glossary.md -> test_preamble_freshness.py is in selectors."""
        selectors, unmatched, ignored = _at.map_changed_to_tests(
            ["quoin/memory/glossary.md"], _REPO_ROOT
        )
        assert any("test_preamble_freshness.py" in s for s in selectors), (
            f"expected test_preamble_freshness.py in selectors, got {selectors}"
        )
        assert not ignored
        assert not unmatched

    def test_format_kit_triggers_preamble_freshness(self):
        """quoin/memory/format-kit.md -> test_preamble_freshness.py is in selectors."""
        selectors, unmatched, ignored = _at.map_changed_to_tests(
            ["quoin/memory/format-kit.md"], _REPO_ROOT
        )
        assert any("test_preamble_freshness.py" in s for s in selectors), (
            f"expected test_preamble_freshness.py in selectors, got {selectors}"
        )
        assert not ignored
        assert not unmatched

    def test_unrelated_skill_md_still_ignored(self):
        """Non-special non-.py file (SKILL.md) still lands in ignored (regression guard)."""
        selectors, unmatched, ignored = _at.map_changed_to_tests(
            ["quoin/skills/gate/SKILL.md"], _REPO_ROOT
        )
        assert not selectors, f"expected no selectors for SKILL.md, got {selectors}"
        assert not unmatched
        assert "quoin/skills/gate/SKILL.md" in ignored

    def test_bare_claude_md_does_not_match(self):
        """Bare 'CLAUDE.md' (no quoin/ parent) must NOT trigger the size-ceiling test.

        The leading-'/' guard on the suffix match ensures only paths ending in
        '.../quoin/CLAUDE.md' match; a root-level CLAUDE.md must fall through
        to ignored (R-01 false-positive mitigation).
        """
        selectors, unmatched, ignored = _at.map_changed_to_tests(
            ["CLAUDE.md"], _REPO_ROOT
        )
        assert not selectors, (
            f"bare CLAUDE.md should not map to any selector, got {selectors}"
        )
        assert "CLAUDE.md" in ignored, f"bare CLAUDE.md should be in ignored, got {ignored}"
        assert not unmatched

    def test_claude_md_triggers_build_claude_slim(self):
        """quoin/CLAUDE.md -> test_build_claude_slim.py is ALSO in selectors (IVG-164 T-05).

        Duplicate-key-safe: this ADDS to the existing size-ceiling selector
        for quoin/CLAUDE.md rather than displacing it.
        """
        selectors, unmatched, ignored = _at.map_changed_to_tests(
            ["quoin/CLAUDE.md"], _REPO_ROOT
        )
        assert any("test_build_claude_slim.py" in s for s in selectors), (
            f"expected test_build_claude_slim.py in selectors, got {selectors}"
        )
        assert any("test_claude_md_size_ceiling.py" in s for s in selectors), (
            f"quoin/CLAUDE.md row should still also select test_claude_md_size_ceiling.py, got {selectors}"
        )
        assert not ignored
        assert not unmatched

    def test_claude_slim_md_triggers_build_claude_slim(self):
        """quoin/CLAUDE.slim.md -> test_build_claude_slim.py is in selectors (IVG-164 T-05)."""
        selectors, unmatched, ignored = _at.map_changed_to_tests(
            ["quoin/CLAUDE.slim.md"], _REPO_ROOT
        )
        assert any("test_build_claude_slim.py" in s for s in selectors), (
            f"expected test_build_claude_slim.py in selectors, got {selectors}"
        )
        assert not ignored
        assert not unmatched

    def test_workflow_catalog_triggers_build_claude_slim(self):
        """quoin/memory/workflow-catalog.md -> test_build_claude_slim.py is in selectors (IVG-164 T-05)."""
        selectors, unmatched, ignored = _at.map_changed_to_tests(
            ["quoin/memory/workflow-catalog.md"], _REPO_ROOT
        )
        assert any("test_build_claude_slim.py" in s for s in selectors), (
            f"expected test_build_claude_slim.py in selectors, got {selectors}"
        )
        assert not ignored
        assert not unmatched

    def test_bare_claude_slim_md_does_not_match(self):
        """Bare 'CLAUDE.slim.md' (no quoin/ parent) must NOT trigger any selector.

        Exercises the posix == entry OR posix.endswith("/" + entry) guard for
        the new row, mirroring test_bare_claude_md_does_not_match above.
        """
        selectors, unmatched, ignored = _at.map_changed_to_tests(
            ["CLAUDE.slim.md"], _REPO_ROOT
        )
        assert not selectors, (
            f"bare CLAUDE.slim.md should not map to any selector, got {selectors}"
        )
        assert "CLAUDE.slim.md" in ignored, f"bare CLAUDE.slim.md should be in ignored, got {ignored}"
        assert not unmatched

    # -- review-1.md MAJOR 2: citation sweep selectable under affected-area gating --

    def test_claude_md_triggers_citation_sweep(self):
        """quoin/CLAUDE.md -> test_claude_md_citations.py is ALSO in selectors.

        Duplicate-key-safe: ADDS to the existing size-ceiling + build-slim
        selectors for quoin/CLAUDE.md rather than displacing them.
        """
        selectors, unmatched, ignored = _at.map_changed_to_tests(
            ["quoin/CLAUDE.md"], _REPO_ROOT
        )
        assert any("test_claude_md_citations.py" in s for s in selectors), (
            f"expected test_claude_md_citations.py in selectors, got {selectors}"
        )
        assert any("test_build_claude_slim.py" in s for s in selectors), (
            f"quoin/CLAUDE.md row should still also select test_build_claude_slim.py, got {selectors}"
        )
        assert any("test_claude_md_size_ceiling.py" in s for s in selectors), (
            f"quoin/CLAUDE.md row should still also select test_claude_md_size_ceiling.py, got {selectors}"
        )
        assert not ignored
        assert not unmatched

    def test_workflow_catalog_triggers_citation_sweep(self):
        """quoin/memory/workflow-catalog.md -> test_claude_md_citations.py is ALSO
        in selectors, in addition to test_build_claude_slim.py."""
        selectors, unmatched, ignored = _at.map_changed_to_tests(
            ["quoin/memory/workflow-catalog.md"], _REPO_ROOT
        )
        assert any("test_claude_md_citations.py" in s for s in selectors), (
            f"expected test_claude_md_citations.py in selectors, got {selectors}"
        )
        assert any("test_build_claude_slim.py" in s for s in selectors), (
            f"workflow-catalog.md row should still also select test_build_claude_slim.py, got {selectors}"
        )
        assert not ignored
        assert not unmatched

    def test_citation_fixture_triggers_citation_sweep(self):
        """The citation-disposition fixture .json -> test_claude_md_citations.py
        is in selectors (a fixture edit alone must re-run the sweep)."""
        selectors, unmatched, ignored = _at.map_changed_to_tests(
            ["quoin/dev/tests/fixtures/claude_md_citation_dispositions.json"], _REPO_ROOT
        )
        assert any("test_claude_md_citations.py" in s for s in selectors), (
            f"expected test_claude_md_citations.py in selectors, got {selectors}"
        )
        assert not ignored
        assert not unmatched

    def test_skill_md_still_not_selectable_documented_residual_gap(self):
        """Regression guard for the documented residual gap (review-1.md MAJOR 2):
        an adapter SKILL.md edit — one of the citation sweep's three in-scope
        corpora — still has no test_claude_md_citations.py row.

        Two independent assertions (IVG-249 S-03 T-07 split, round-2 MIN-4;
        first clause rotated off gate/SKILL.md at IVG-248 stage 2 T-14):
        (1) no test_claude_md_citations.py selector for review/SKILL.md — the
            citation-sweep-gap claim this test is named for. gate/SKILL.md
            stopped being the exemplar once T-14 gave it a
            test_claude_md_citations.py row (it embeds the deploy-drift
            coverage qualifier verbatim, including the literal "CLAUDE.md"
            in its "not covered" clause — the durable half of the C-03
            critical). review/SKILL.md carries an authored-content row (so
            it is not wholly ignored — see clause 2's own file for that) but
            no citation-sweep row, so it remains a genuine residual.
        (2) a SKILL.md still lands wholly in `ignored` — gate/SKILL.md no
            longer qualifies for this half once the end-of-task resilience
            rows made it selectable, and review/SKILL.md stopped qualifying
            once the clean-authored-content rule gave it a
            test_authored_content_rule_pointers.py row, so this assertion
            moved to critic/SKILL.md, which carries no _DOCS_TO_TESTS row at
            all (verified by grep).

        Rotation check: re-ran this test before and after adding the
        implement/end_of_task -> test_decision_gate_census.py selector rows.
        Both clauses' exemplars are unaffected by that edit class — clause 1's
        exemplar (review/SKILL.md) still carries no citation-sweep row, clause
        2's exemplar (critic/SKILL.md) still carries no _DOCS_TO_TESTS row at
        all — neither needed rotating."""
        selectors, unmatched, ignored = _at.map_changed_to_tests(
            ["quoin/adapters/claude/skills/review/SKILL.md"], _REPO_ROOT
        )
        assert not any("test_claude_md_citations.py" in s for s in selectors), (
            f"SKILL.md is a documented residual gap for the citation sweep; got {selectors}"
        )

        selectors2, unmatched2, ignored2 = _at.map_changed_to_tests(
            ["quoin/adapters/claude/skills/critic/SKILL.md"], _REPO_ROOT
        )
        assert not selectors2, f"expected no selectors for critic/SKILL.md, got {selectors2}"
        assert "quoin/adapters/claude/skills/critic/SKILL.md" in ignored2
        assert not unmatched2

    def test_cost_ledger_format_triggers_agent_transcript_cost(self):
        """IVG-249 T-11 (D-05/MAJ-3): quoin/memory/cost-ledger-format.md ->
        test_agent_transcript_cost.py is in selectors — repo-root-relative
        convention (NOT project-root-relative like the rest of the ivg-249
        plan; every existing _DOCS_TO_TESTS row confirms this).

        MAJ-3 (promoted to REQUIRED): a wrong-convention row would pass a
        bare "run succeeds" check while silently selecting zero tests
        (mapped_any=True, no selectors) — the len(selectors) >= 1 assertion
        below is what catches that failure mode, not merely absence of a
        crash.
        """
        selectors, unmatched, ignored = _at.map_changed_to_tests(
            ["quoin/memory/cost-ledger-format.md"], _REPO_ROOT
        )
        assert len(selectors) >= 1, (
            f"expected a non-empty selector set, got {selectors} "
            f"(wrong path convention silently selects zero tests)"
        )
        assert any("test_agent_transcript_cost.py" in s for s in selectors), (
            f"expected test_agent_transcript_cost.py in selectors, got {selectors}"
        )
        assert not ignored
        assert not unmatched

    @pytest.mark.parametrize(
        "changed_file",
        [
            "quoin/memory/clean-authored-content.md",
            "quoin/adapters/claude/skills/implement/SKILL.md",
            "quoin/adapters/claude/skills/end_of_task/SKILL.md",
            "quoin/adapters/claude/skills/pr/SKILL.md",
            "quoin/adapters/claude/skills/review/SKILL.md",
            "quoin/adapters/claude/skills/run/SKILL.md",
        ],
    )
    def test_clean_authored_content_pointer_sites_select_guard_test(self, changed_file):
        """The clean-authored-content rule file and each of its pointer sites
        must select test_authored_content_rule_pointers.py — without this
        proof, the guard test is unselectable at an affected-area gate and
        silently never runs on an edit to any of these files."""
        selectors, unmatched, ignored = _at.map_changed_to_tests(
            [changed_file], _REPO_ROOT
        )
        assert any(
            "test_authored_content_rule_pointers.py" in s for s in selectors
        ), f"expected test_authored_content_rule_pointers.py in selectors for {changed_file}, got {selectors}"


# Import SPAWN_TARGETS from the generator (drift guard) — same idiom as
# test_preamble_freshness.py:27-28. A future 10th spawn target with no
# matching _DOCS_TO_TESTS row fails TestIvg123PreambleMapping below rather
# than silently reopening the lesson-2026-07-04 blind spot.
sys.path.insert(0, str(_REPO_ROOT / "quoin" / "scripts"))
from build_preambles import SPAWN_TARGETS  # noqa: E402


class TestIvg123PreambleMapping:
    """Verify each spawn-target preamble.md maps to test_preamble_freshness.py."""

    @pytest.mark.parametrize("skill", sorted(SPAWN_TARGETS.keys()))
    def test_spawn_target_preamble_triggers_freshness_test(self, skill):
        """quoin/skills/<skill>/preamble.md -> test_preamble_freshness.py, cleanly."""
        selectors, unmatched, ignored = _at.map_changed_to_tests(
            [f"quoin/skills/{skill}/preamble.md"], _REPO_ROOT
        )
        assert any("test_preamble_freshness.py" in s for s in selectors), (
            f"expected test_preamble_freshness.py in selectors for {skill}, got {selectors}"
        )
        assert not ignored, f"ignored should be empty for a mapped file, got {ignored}"
        assert not unmatched

    def test_bare_preamble_md_does_not_match(self):
        """Bare 'preamble.md' (no quoin/skills/<skill>/ parent) must NOT match —
        anchored-suffix guard, mirrors test_bare_claude_md_does_not_match."""
        selectors, unmatched, ignored = _at.map_changed_to_tests(
            ["preamble.md"], _REPO_ROOT
        )
        assert not selectors, f"expected no selectors for bare preamble.md, got {selectors}"
        assert not unmatched
        assert "preamble.md" in ignored

    def test_non_spawn_target_preamble_md_still_ignored(self):
        """A preamble.md under a skill NOT in SPAWN_TARGETS still lands in
        ignored — proves per-file enumeration, not a directory-prefix rule."""
        selectors, unmatched, ignored = _at.map_changed_to_tests(
            ["quoin/skills/implement/preamble.md"], _REPO_ROOT
        )
        assert not selectors, f"expected no selectors, got {selectors}"
        assert not unmatched
        assert "quoin/skills/implement/preamble.md" in ignored


# ---------------------------------------------------------------------------
# resolve_repo
# ---------------------------------------------------------------------------

class TestResolveRepo:
    def test_resolves_child_git_repo(self, tmp_path):
        """Outer non-git dir with a child git repo -> returns child."""
        child = tmp_path / "myrepo"
        child.mkdir()
        (child / ".git").mkdir()
        result = _at.resolve_repo(tmp_path)
        assert result is not None
        assert result.resolve() == child.resolve()

    def test_no_git_repo_returns_none(self, tmp_path):
        """No git repos under project_root -> returns None."""
        result = _at.resolve_repo(tmp_path)
        assert result is None

    def test_project_root_is_git_repo(self, tmp_path):
        """If project_root itself is a git repo -> returns it."""
        (tmp_path / ".git").mkdir()
        result = _at.resolve_repo(tmp_path)
        assert result is not None
        assert result.resolve() == tmp_path.resolve()

    def test_multiple_repos_raises(self, tmp_path):
        """Multiple repos -> RuntimeError (caller should exit 3)."""
        (tmp_path / "repo1").mkdir()
        (tmp_path / "repo1" / ".git").mkdir()
        (tmp_path / "repo2").mkdir()
        (tmp_path / "repo2" / ".git").mkdir()
        with pytest.raises(RuntimeError, match="Multiple git repos"):
            _at.resolve_repo(tmp_path)


# ---------------------------------------------------------------------------
# changed_files (diff-basis fallback — CRIT-2)
# ---------------------------------------------------------------------------

def _git(*args, cwd):
    """Run a git command in the given directory."""
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   capture_output=True)


class TestChangedFiles:
    def test_worktree_fallback_on_main(self, tmp_path):
        """HEAD==main (no feature branch, no upstream) + dirty .py -> worktree-diff."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _git("init", "-b", "main", cwd=repo)
        _git("config", "user.email", "test@test.com", cwd=repo)
        _git("config", "user.name", "Test", cwd=repo)
        # Commit a baseline
        (repo / "base.py").write_text("# baseline\n")
        _git("add", "base.py", cwd=repo)
        _git("commit", "-m", "baseline", cwd=repo)
        # Make an uncommitted change
        (repo / "dirty.py").write_text("# dirty\n")
        _git("add", "dirty.py", cwd=repo)

        files, reason = _at.changed_files(repo)
        assert "dirty.py" in files, f"Expected dirty.py in files, got {files}"
        assert reason == "worktree-diff"

    def test_clean_tree_no_changes(self, tmp_path):
        """Genuinely clean tree -> empty list + reason no-changes."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _git("init", "-b", "main", cwd=repo)
        _git("config", "user.email", "test@test.com", cwd=repo)
        _git("config", "user.name", "Test", cwd=repo)
        (repo / "base.py").write_text("# baseline\n")
        _git("add", "base.py", cwd=repo)
        _git("commit", "-m", "baseline", cwd=repo)

        files, reason = _at.changed_files(repo)
        assert files == []
        assert reason == "no-changes"

    def test_not_a_git_repo(self, tmp_path):
        """Not a git repo -> git-error reason."""
        not_repo = tmp_path / "not_repo"
        not_repo.mkdir()
        files, reason = _at.changed_files(not_repo)
        assert reason == "git-error"

    def test_committed_clean_no_upstream_returns_base_branch_diff(self, tmp_path):
        """F-01 fix: committed .py change on branch with no upstream, clean worktree
        -> base-branch-diff (NOT no-changes).

        This is the canonical /review + /gate state: git switch -c creates a
        feature branch without --track, so @{u} does not exist.  The gate's
        'No uncommitted changes' check ensures the tree is committed-clean.
        Before F-01, this yielded 'no-changes' -> false APPROVE with zero tests run.
        After F-01, the base-branch merge-base step detects the committed change.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        _git("init", "-b", "main", cwd=repo)
        _git("config", "user.email", "test@test.com", cwd=repo)
        _git("config", "user.name", "Test", cwd=repo)
        # Commit a baseline on main
        (repo / "base.py").write_text("# baseline\n")
        _git("add", "base.py", cwd=repo)
        _git("commit", "-m", "baseline", cwd=repo)
        # Create a feature branch (no upstream — mirrors `git switch -c`)
        _git("switch", "-c", "feature/my-change", cwd=repo)
        # Commit a .py source change on the feature branch (committed-clean)
        (repo / "src.py").write_text("def src(): return 42\n")
        _git("add", "src.py", cwd=repo)
        _git("commit", "-m", "add src.py", cwd=repo)
        # Tree is clean: no uncommitted changes, no upstream

        files, reason = _at.changed_files(repo)
        assert "src.py" in files, (
            f"F-01 regression: committed src.py not detected; got files={files}, reason={reason}"
        )
        assert reason == "base-branch-diff", (
            f"Expected reason=base-branch-diff, got {reason}"
        )


# ---------------------------------------------------------------------------
# CLI exit-code matrix
# ---------------------------------------------------------------------------

def _cli(args, env=None):
    """Run main(args) and return exit code."""
    import os
    saved = {k: os.environ.get(k) for k in (env or {})}
    try:
        if env:
            for k, v in env.items():
                os.environ[k] = v
        return _at.main(args)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class TestCLIExitCodes:
    def test_bad_args_exit_2(self):
        """Missing required argument group -> exit 2."""
        rc = _cli([])
        assert rc == 2

    def test_no_base_flag(self):
        """--base is not a recognized flag (MIN-1)."""
        rc = _cli(["--base", "main", "--files", "foo.py"])
        assert rc == 2

    def test_select_only_no_pytest(self, fake_repo):
        """--select-only emits JSON without running pytest (ran_pytest=False)."""
        with mock.patch("subprocess.run") as mock_run:
            rc = _cli(["--select-only", "--files", "foo.py",
                       "--repo-root", str(fake_repo)])
            assert rc == 0
            # subprocess.run should NOT have been called for pytest
            # (only allowable calls would be for git, but we're using --files mode)
            for call in mock_run.call_args_list:
                call_args = call[0][0] if call[0] else call.args[0]
                assert "pytest" not in str(call_args), (
                    f"pytest should not be invoked with --select-only, got {call_args}"
                )

    def test_exit_0c_clean_tree(self, tmp_path):
        """--project-root on clean repo -> exit 0, exit_reason=no-changes."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _git("init", "-b", "main", cwd=repo)
        _git("config", "user.email", "test@test.com", cwd=repo)
        _git("config", "user.name", "Test", cwd=repo)
        (repo / "base.py").write_text("# baseline\n")
        _git("add", "base.py", cwd=repo)
        _git("commit", "-m", "baseline", cwd=repo)

        captured = []
        original_print = __builtins__["print"] if isinstance(__builtins__, dict) else print

        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = _cli(["--project-root", str(tmp_path)])
        assert rc == 0
        output = buf.getvalue()
        data = json.loads(output)
        assert data["exit_reason"] == "no-changes"
        assert data["ran_pytest"] is False

    def test_exit_0b_docs_only(self, fake_repo):
        """Docs-only changeset -> exit 0, ran_pytest=False, exit_reason=docs-only."""
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with mock.patch("subprocess.run") as mock_run:
                rc = _cli(["--files", "gate/SKILL.md", "notes.md",
                            "--repo-root", str(fake_repo)])
                # Assert pytest was NOT spawned
                for call in mock_run.call_args_list:
                    call_args = call[0][0] if call[0] else call.args[0]
                    assert "pytest" not in str(call_args), (
                        f"pytest must NOT be invoked for docs-only: {call_args}"
                    )
        assert rc == 0
        data = json.loads(buf.getvalue())
        assert data["exit_reason"] == "docs-only-no-selectors"
        assert data["ran_pytest"] is False
        assert data["selectors"] == []
        assert data["unmatched_sources"] == []

    def test_docs_only_distinct_from_exit_4(self, fake_repo):
        """A changed real .py source with no test -> exit 3/4 (NOT 0b)."""
        rc = _cli(["--files", "orphan.py", "--repo-root", str(fake_repo)])
        assert rc in (3, 4), f"Expected 3 or 4 for unmatched .py source, got {rc}"

    def test_docs_only_distinct_from_exit_0c(self, fake_repo):
        """Docs-only reports exit_reason=docs-only-no-selectors, NOT no-changes."""
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = _cli(["--files", "README.md",
                       "--repo-root", str(fake_repo)])
        assert rc == 0
        data = json.loads(buf.getvalue())
        assert data["exit_reason"] == "docs-only-no-selectors"
        assert data["exit_reason"] != "no-changes"

    def test_exit_1_red_tests(self, tmp_path):
        """A deliberately failing affected test -> exit 1."""
        # Write a failing test
        (tmp_path / "src.py").write_text("def src(): pass\n")
        (tmp_path / "test_src.py").write_text(
            "def test_src_fails(): assert False, 'deliberate failure'\n"
        )
        rc = _cli(["--files", "src.py", "--repo-root", str(tmp_path)])
        assert rc == 1, f"Expected exit 1 (red tests), got {rc}"

    def test_exit_3_unmatched_without_allow(self, fake_repo):
        """orphan.py with no test + no --allow-unmatched -> exit 3."""
        rc = _cli(["--files", "orphan.py", "--repo-root", str(fake_repo)])
        assert rc == 3

    def test_allow_unmatched_green(self, tmp_path):
        """--allow-unmatched: unmatched source + other tests passing -> exit 0."""
        # Create a test that PASSES (not related to orphan.py)
        (tmp_path / "orphan.py").write_text("def orphan(): pass\n")
        (tmp_path / "other.py").write_text("def other(): pass\n")
        (tmp_path / "test_other.py").write_text("def test_other(): pass\n")
        rc = _cli(["--files", "orphan.py", "other.py",
                   "--repo-root", str(tmp_path),
                   "--allow-unmatched"])
        assert rc == 0, f"Expected exit 0 with --allow-unmatched and passing tests, got {rc}"

    def test_allow_unmatched_single_unmatched_no_selectors_exit_0(self, tmp_path):
        """F-02 fix: --allow-unmatched + single unmatched .py source (no selectors)
        -> exit 0, NOT exit 4.

        The escape-hatch contract says --allow-unmatched "yields exit 0 when
        affected pytest passes."  With empty selectors there is nothing to run,
        so exit 0 with ran_pytest=False and unmatched_warning=true is the
        consistent interpretation (the user opted in to 'I know tests are missing').
        """
        (tmp_path / "orphan.py").write_text("def orphan(): pass\n")
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = _cli(["--files", "orphan.py",
                       "--repo-root", str(tmp_path),
                       "--allow-unmatched"])
        assert rc == 0, (
            f"F-02 regression: expected exit 0 with --allow-unmatched + single "
            f"unmatched source and no selectors, got {rc}"
        )
        data = json.loads(buf.getvalue())
        assert data.get("unmatched_warning") is True, (
            f"Expected unmatched_warning=true in output, got {data}"
        )
        assert data["ran_pytest"] is False

    def test_f01_committed_clean_no_upstream_red_test_exit_1(self, tmp_path):
        """F-01 end-to-end: committed-clean feature branch, no upstream, red test
        -> exit 1 (affected suite RED), NOT exit 0 (false APPROVE).

        Reproduces the exact hermetic scenario from review-1.md: a branch with a
        committed change to src.py whose test_src.py asserts False.  Before F-01
        this yielded exit 0 / no-changes.  After F-01 the red test is selected
        via the base-branch merge-base diff and exit 1 is produced.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        _git("init", "-b", "main", cwd=repo)
        _git("config", "user.email", "test@test.com", cwd=repo)
        _git("config", "user.name", "Test", cwd=repo)
        # Baseline commit on main
        (repo / "baseline.py").write_text("# baseline\n")
        _git("add", "baseline.py", cwd=repo)
        _git("commit", "-m", "baseline", cwd=repo)
        # Feature branch (no upstream)
        _git("switch", "-c", "feature/red-test", cwd=repo)
        # Commit src.py + a red test (tree committed-clean, no upstream)
        (repo / "src.py").write_text("def src(): return 42\n")
        (repo / "test_src.py").write_text(
            "def test_src_fails(): assert False, 'deliberate failure — F-01 regression guard'\n"
        )
        _git("add", "src.py", "test_src.py", cwd=repo)
        _git("commit", "-m", "add src + red test", cwd=repo)
        # Tree is clean: committed-clean, no upstream — the exact false-APPROVE state

        # Use --project-root (outer non-git) to exercise the full pipeline
        rc = _cli(["--project-root", str(tmp_path)])
        assert rc == 1, (
            f"F-01 regression: expected exit 1 (red affected test), got {rc}. "
            f"If exit 0 is returned, the base-branch merge-base diff step is not firing "
            f"and the committed-clean no-upstream false-APPROVE bug is still present."
        )

    def test_disable_env_exit_3(self):
        """QUOIN_DISABLE_AFFECTED_TESTS=1 -> exit 3 + {"disabled": true}."""
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = _cli(
                ["--files", "anything.py"],
                env={"QUOIN_DISABLE_AFFECTED_TESTS": "1"},
            )
        assert rc == 3, f"Expected exit 3 on DISABLE, got {rc}"
        data = json.loads(buf.getvalue())
        assert data.get("disabled") is True

    def test_project_root_resolves_to_child(self, tmp_path):
        """--project-root outer (no .git) with child git repo -> resolves OK."""
        repo = tmp_path / "quoin"
        repo.mkdir()
        _git("init", "-b", "main", cwd=repo)
        _git("config", "user.email", "test@test.com", cwd=repo)
        _git("config", "user.name", "Test", cwd=repo)
        (repo / "base.py").write_text("# baseline\n")
        _git("add", "base.py", cwd=repo)
        _git("commit", "-m", "baseline", cwd=repo)
        # Make a staged change so changed_files has something
        (repo / "new.py").write_text("# new\n")
        _git("add", "new.py", cwd=repo)

        # Run with --select-only so we don't need matching test files
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = _cli(["--select-only", "--project-root", str(tmp_path)])
        # Should not error with "not a git repository"
        assert rc == 0, f"Expected exit 0, got {rc}"

    def test_project_root_no_repo_exits_3(self, tmp_path):
        """--project-root pointing at a dir with no nested git -> exit 3."""
        rc = _cli(["--project-root", str(tmp_path)])
        assert rc == 3

    def test_no_pytest_invocation_when_select_only(self, fake_repo):
        """--select-only: subprocess.run is never called with pytest in args."""
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(returncode=0)
            rc = _cli(["--select-only", "--files", "foo.py",
                       "--repo-root", str(fake_repo)])
            for call in mock_run.call_args_list:
                args_used = call[0][0] if call[0] else []
                assert "-m" not in str(args_used) or "pytest" not in str(args_used), (
                    f"pytest must not run on --select-only, call: {call}"
                )


# ---------------------------------------------------------------------------
# IVG-151: has_active_task_context detector (T-01 unit table)
#
# R-TMP: every case builds paths under pytest tmp_path — NEVER a path under the
# real repo tree — so the walk-up cannot detect the real repo's task folders and
# flip the assertion. tmp_path ancestors are system temp dirs with no
# .workflow_artifacts/, so walk-up terminates at the filesystem root -> False.
# ---------------------------------------------------------------------------

class TestHasActiveTaskContext:
    def test_absent_workflow_artifacts_false(self, tmp_path):
        """(a) No .workflow_artifacts/ anywhere at/above -> False."""
        assert _at.has_active_task_context(tmp_path) is False

    def test_infra_only_false(self, tmp_path):
        """(b) Only infra folders (memory/cache/finalized/trash) -> False."""
        wa = tmp_path / ".workflow_artifacts"
        for name in ("memory", "cache", "finalized", "trash"):
            (wa / name).mkdir(parents=True)
        assert _at.has_active_task_context(tmp_path) is False

    def test_finalized_only_false(self, tmp_path):
        """(c) finalized/ only -> False."""
        (tmp_path / ".workflow_artifacts" / "finalized").mkdir(parents=True)
        assert _at.has_active_task_context(tmp_path) is False

    def test_real_task_folder_true(self, tmp_path):
        """(d) >=1 real task folder -> True."""
        (tmp_path / ".workflow_artifacts" / "some-task").mkdir(parents=True)
        assert _at.has_active_task_context(tmp_path) is True

    def test_dot_prefixed_child_only_false(self, tmp_path):
        """(e) Only a dot-prefixed child -> False (dot-prefixed is excluded)."""
        (tmp_path / ".workflow_artifacts" / ".hidden").mkdir(parents=True)
        assert _at.has_active_task_context(tmp_path) is False

    def test_subdir_walk_up_true(self, tmp_path):
        """(f) Task folder at tmp_path; call from a nested subdir -> True (walk-up)."""
        (tmp_path / ".workflow_artifacts" / "some-task").mkdir(parents=True)
        sub = tmp_path / "quoin" / "sub"
        sub.mkdir(parents=True)
        assert _at.has_active_task_context(sub) is True

    def test_oserror_degrades_to_present(self, tmp_path, monkeypatch):
        """(g) OSError on iterdir degrades to context-PRESENT (True), NOT a raise."""
        (tmp_path / ".workflow_artifacts" / "some-task").mkdir(parents=True)

        def _boom(self):
            raise OSError("simulated unreadable directory")

        monkeypatch.setattr(Path, "iterdir", _boom)
        # Must return True (degrade), not raise.
        assert _at.has_active_task_context(tmp_path) is True

    def test_filesystem_root_termination_false(self, tmp_path):
        """(h) No task folder anywhere in the chain -> False (root termination)."""
        deep = tmp_path / "a" / "b" / "c"
        deep.mkdir(parents=True)
        assert _at.has_active_task_context(deep) is False


# ---------------------------------------------------------------------------
# IVG-151: --require-task-context CLI behavior (T-03)
# reproduction, false-green guard, non-regression matrix, env precedence,
# flag-less invariance. R-TMP applies: all no-context dirs live under tmp_path.
# ---------------------------------------------------------------------------

def _init_git_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git("init", "-b", "main", cwd=repo)
    _git("config", "user.email", "test@test.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "baseline.py").write_text("# baseline\n")
    _git("add", "baseline.py", cwd=repo)
    _git("commit", "-m", "baseline", cwd=repo)


class TestRequireTaskContext:
    def test_reproduction_foreign_nongit_exit_5(self, tmp_path):
        """Foreign non-git dir, no WA + --require-task-context -> exit 5,
        exit_reason=no-quoin-task-context, pytest NOT run, git NOT resolved."""
        foreign = tmp_path / "foreign"
        foreign.mkdir()
        import io, contextlib
        buf = io.StringIO()
        with mock.patch.object(_at, "resolve_repo") as mock_resolve, \
                mock.patch("subprocess.run") as mock_run:
            with contextlib.redirect_stdout(buf):
                rc = _cli(["--project-root", str(foreign),
                           "--require-task-context", "--format", "json"])
            mock_resolve.assert_not_called()  # early return BEFORE resolve_repo
            for call in mock_run.call_args_list:
                assert "pytest" not in str(call), f"pytest must not run, got {call}"
        assert rc == 5, f"expected exit 5, got {rc}"
        data = json.loads(buf.getvalue())
        assert data["exit_reason"] == "no-quoin-task-context"
        assert data["ran_pytest"] is False

    def test_reproduction_foreign_gitrepo_exit_5(self, tmp_path):
        """git-init'd foreign repo (no WA) + flag -> exit 5 (still no task context)."""
        repo = tmp_path / "foreign"
        _init_git_repo(repo)
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = _cli(["--project-root", str(repo),
                       "--require-task-context", "--format", "json"])
        assert rc == 5, f"expected exit 5, got {rc}"
        assert json.loads(buf.getvalue())["exit_reason"] == "no-quoin-task-context"

    def test_false_green_guard_active_context_red_exit_1(self, tmp_path):
        """THE critical AC: active task context + red affected test -> exit 1,
        NEVER 5. Context presence must take priority over the no-context path."""
        repo = tmp_path / "repo"
        _init_git_repo(repo)
        # Active task context lives under the repo.
        (repo / ".workflow_artifacts" / "some-task").mkdir(parents=True)
        # A red affected test (staged dirty tree).
        (repo / "src.py").write_text("def src(): return 42\n")
        (repo / "test_src.py").write_text(
            "def test_src_fails(): assert False, 'deliberate red — false-green guard'\n"
        )
        _git("add", "src.py", "test_src.py", cwd=repo)
        rc = _cli(["--project-root", str(repo), "--require-task-context"])
        assert rc == 1, (
            f"FALSE-GREEN REGRESSION: active context + red suite must exit 1, got {rc}. "
            "exit 5 here would be a silently-skipped red suite."
        )

    def test_matrix_active_context_green_exit_0(self, tmp_path):
        """flag + task folder present + green affected test -> exit 0 (unchanged)."""
        repo = tmp_path / "repo"
        _init_git_repo(repo)
        (repo / ".workflow_artifacts" / "some-task").mkdir(parents=True)
        (repo / "src.py").write_text("def src(): return 1\n")
        (repo / "test_src.py").write_text("def test_src_ok(): assert True\n")
        _git("add", "src.py", "test_src.py", cwd=repo)
        rc = _cli(["--project-root", str(repo), "--require-task-context"])
        assert rc == 0, f"expected exit 0 (green), got {rc}"

    def test_matrix_disable_wins_over_flag(self, tmp_path):
        """QUOIN_DISABLE_AFFECTED_TESTS=1 + flag + no context -> exit 3 (disable wins)."""
        foreign = tmp_path / "foreign"
        foreign.mkdir()
        rc = _cli(["--project-root", str(foreign), "--require-task-context"],
                  env={"QUOIN_DISABLE_AFFECTED_TESTS": "1"})
        assert rc == 3, f"disable must win (exit 3), got {rc}"

    def test_env_require_zero_forces_legacy(self, tmp_path):
        """flag + no context + QUOIN_REQUIRE_TASK_CONTEXT=0 -> legacy path
        (foreign no-repo dir -> 3, NOT 5)."""
        foreign = tmp_path / "foreign"
        foreign.mkdir()
        rc = _cli(["--project-root", str(foreign), "--require-task-context"],
                  env={"QUOIN_REQUIRE_TASK_CONTEXT": "0"})
        assert rc == 3, f"env=0 must force legacy (no-repo -> 3), got {rc}"

    def test_flagless_invariance_foreign_no_repo_still_3(self, tmp_path):
        """--project-root foreign no-repo dir WITHOUT the flag -> 3 (byte-for-byte legacy)."""
        foreign = tmp_path / "foreign"
        foreign.mkdir()
        rc = _cli(["--project-root", str(foreign)])
        assert rc == 3, f"flag-less legacy path must be exit 3, got {rc}"


# ---------------------------------------------------------------------------
# FR-6 / AC-6 (S-01): non-collectable allowlist + rc-5 clean-skip
# ---------------------------------------------------------------------------

class TestNoncollectableParsing:
    """T-03 pure-function units: parser, path resolver, loader, matcher, partition."""

    def test_parse_drops_comment_and_blank_lines(self):
        text = (
            "# header comment\n"
            "\n"
            "   # indented comment\n"
            "quoin/dev/tests/spike_a.py\n"
            "  spike_b.py  \n"
            "\n"
            "# trailing comment\n"
        )
        entries = _at._parse_noncollectable(text)
        assert entries == ["quoin/dev/tests/spike_a.py", "spike_b.py"]

    def test_parse_empty_text(self):
        assert _at._parse_noncollectable("") == []
        assert _at._parse_noncollectable("# only comments\n\n") == []

    def test_is_noncollectable_exact_match(self):
        entries = ["quoin/dev/tests/spike_a.py"]
        assert _at._is_noncollectable("quoin/dev/tests/spike_a.py", entries) is True

    def test_is_noncollectable_anchored_suffix_match(self):
        """A `/`-anchored suffix entry matches a longer path ending in /entry."""
        entries = ["spike_a.py"]
        assert _at._is_noncollectable("quoin/dev/tests/spike_a.py", entries) is True
        assert _at._is_noncollectable("spike_a.py", entries) is True

    def test_is_noncollectable_anchor_guard(self):
        """Bare basename entry must NOT match a differently-prefixed longer basename."""
        entries = ["spike_a.py"]
        # 'other_spike_a.py' ends with 'spike_a.py' as a raw substring but NOT with
        # '/spike_a.py', so the anchor guard must reject it.
        assert _at._is_noncollectable("quoin/dev/tests/other_spike_a.py", entries) is False

    def test_is_noncollectable_no_match(self):
        entries = ["quoin/dev/tests/spike_a.py"]
        assert _at._is_noncollectable("quoin/dev/tests/spike_b.py", entries) is False

    def test_load_absent_file_returns_empty(self, tmp_path):
        """FR-9 fail-safe: absent allowlist file -> [] (no override, no repo file)."""
        # tmp_path has no quoin/dev/tests/non-collectable.txt
        assert _at.load_noncollectable(tmp_path) == []

    def test_load_env_override_honored(self, tmp_path, monkeypatch):
        """QUOIN_NONCOLLECTABLE_FILE absolute override wins over repo-relative path."""
        override = tmp_path / "custom-list.txt"
        override.write_text("# hi\nquoin/dev/tests/spike_x.py\n")
        monkeypatch.setenv("QUOIN_NONCOLLECTABLE_FILE", str(override))
        # repo_root is an unrelated dir with no repo-relative file; override still loads.
        assert _at.load_noncollectable(tmp_path / "somewhere") == [
            "quoin/dev/tests/spike_x.py"
        ]

    def test_load_repo_relative_resolution(self, tmp_path, monkeypatch):
        """Repo-relative resolution: repo_root/quoin/dev/tests/non-collectable.txt found."""
        monkeypatch.delenv("QUOIN_NONCOLLECTABLE_FILE", raising=False)
        rel = tmp_path / "quoin" / "dev" / "tests"
        rel.mkdir(parents=True)
        (rel / "non-collectable.txt").write_text("# c\nspike_y.py\n")
        assert _at.load_noncollectable(tmp_path) == ["spike_y.py"]

    def test_partition_preserves_order_and_splits(self):
        changed = ["a.py", "quoin/dev/tests/spike_a.py", "b.py"]
        entries = ["quoin/dev/tests/spike_a.py"]
        remaining, nc = _at.partition_noncollectable(changed, entries)
        assert remaining == ["a.py", "b.py"]
        assert nc == ["quoin/dev/tests/spike_a.py"]

    def test_partition_empty_entries_is_identity(self):
        """Empty allowlist -> everything stays in remaining (byte-for-byte legacy)."""
        changed = ["a.py", "b.py"]
        remaining, nc = _at.partition_noncollectable(changed, [])
        assert remaining == ["a.py", "b.py"]
        assert nc == []


class TestSelectionNoncollectableField:
    """T-02: dataclass field default + to_dict + text formatter."""

    def test_default_empty(self):
        sel = _at.Selection(
            changed=[], selectors=[], unmatched_sources=[], ignored=[],
            ran_pytest=False, pytest_returncode=None, exit_reason="x",
        )
        assert sel.noncollectable == []
        assert sel.to_dict()["noncollectable"] == []

    def test_populated_emitted(self):
        sel = _at.Selection(
            changed=[], selectors=[], unmatched_sources=[], ignored=[],
            ran_pytest=False, pytest_returncode=None, exit_reason="x",
            noncollectable=["spike.py"],
        )
        assert sel.to_dict()["noncollectable"] == ["spike.py"]

    def test_text_formatter_emits_only_when_nonempty(self):
        empty = _at.Selection(
            changed=[], selectors=[], unmatched_sources=[], ignored=[],
            ran_pytest=False, pytest_returncode=None, exit_reason="x",
        )
        assert "noncollectable" not in _at._format_text(empty)
        populated = _at.Selection(
            changed=[], selectors=[], unmatched_sources=[], ignored=[],
            ran_pytest=False, pytest_returncode=None, exit_reason="x",
            noncollectable=["spike.py"],
        )
        assert "noncollectable (1): spike.py" in _at._format_text(populated)


class TestAc6NoncollectablePaths:
    """AC-6(a): a designated non-collectable non-test .py (today exit 3) -> exit 0."""

    def test_allowlisted_source_exit_0(self, tmp_path, monkeypatch):
        """--files spike_src.py with the allowlist listing it -> exit 0,
        exit_reason=noncollectable-skip, in noncollectable, NOT in unmatched_sources."""
        (tmp_path / "spike_src.py").write_text("def spike(): pass\n")
        allow = tmp_path / "non-collectable.txt"
        allow.write_text("# list\nspike_src.py\n")
        monkeypatch.setenv("QUOIN_NONCOLLECTABLE_FILE", str(allow))
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = _cli(["--files", "spike_src.py", "--repo-root", str(tmp_path)])
        assert rc == 0, f"allowlisted non-test .py should exit 0, got {rc}"
        data = json.loads(buf.getvalue())
        assert data["exit_reason"] == "noncollectable-skip"
        assert "spike_src.py" in data["noncollectable"]
        assert data["unmatched_sources"] == []
        assert data["selectors"] == []
        assert data["ran_pytest"] is False

    def test_control_same_file_without_allowlist_exit_3(self, tmp_path, monkeypatch):
        """Control: SAME orphan .py WITHOUT the allowlist -> exit 3 (today-BLOCKED
        baseline; proves the allowlist is load-bearing)."""
        monkeypatch.delenv("QUOIN_NONCOLLECTABLE_FILE", raising=False)
        (tmp_path / "spike_src.py").write_text("def spike(): pass\n")
        rc = _cli(["--files", "spike_src.py", "--repo-root", str(tmp_path)])
        assert rc == 3, f"orphan .py without allowlist must still exit 3, got {rc}"

    def test_allowlisted_test_spike_never_reaches_pytest(self, tmp_path, monkeypatch):
        """AC-6: an allowlisted test_*.py spike -> exit 0, in noncollectable, and pytest
        is NEVER invoked (partition happens before selectors are built)."""
        (tmp_path / "test_spike.py").write_text("# no test functions here\n")
        allow = tmp_path / "non-collectable.txt"
        allow.write_text("test_spike.py\n")
        monkeypatch.setenv("QUOIN_NONCOLLECTABLE_FILE", str(allow))
        import io, contextlib
        buf = io.StringIO()
        with mock.patch("subprocess.run") as mock_run:
            with contextlib.redirect_stdout(buf):
                rc = _cli(["--files", "test_spike.py", "--repo-root", str(tmp_path)])
            for call in mock_run.call_args_list:
                assert "pytest" not in str(call), (
                    f"allowlisted test spike must not reach pytest, got {call}"
                )
        assert rc == 0, f"allowlisted test spike should exit 0, got {rc}"
        data = json.loads(buf.getvalue())
        assert "test_spike.py" in data["noncollectable"]
        assert data["exit_reason"] == "noncollectable-skip"

    def test_noncollectable_plus_ignored_only_exit_0(self, tmp_path, monkeypatch):
        """AC-6: a changeset of ONLY non-collectable + ignored files -> exit 0."""
        (tmp_path / "spike_src.py").write_text("def spike(): pass\n")
        allow = tmp_path / "non-collectable.txt"
        allow.write_text("spike_src.py\n")
        monkeypatch.setenv("QUOIN_NONCOLLECTABLE_FILE", str(allow))
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = _cli(["--files", "spike_src.py", "notes.md",
                       "--repo-root", str(tmp_path)])
        assert rc == 0
        data = json.loads(buf.getvalue())
        assert data["exit_reason"] == "noncollectable-skip"
        assert "spike_src.py" in data["noncollectable"]
        assert "notes.md" in data["ignored"]


class TestAc6Rc5CleanSkip:
    """AC-6(b) + R-06: pytest rc-5 remap semantics (no allowlist involved)."""

    def test_collect_nothing_test_spike_exit_0(self, tmp_path):
        """A real changed test_*.py that collects nothing -> pytest rc 5 -> exit 0,
        exit_reason=no-tests-collected-skip, pytest_returncode=5. Genuinely runs pytest."""
        # A test file with NO test_* functions -> pytest collects nothing -> rc 5.
        (tmp_path / "test_spike.py").write_text(
            "# a spike with no collectable tests\n"
            "def helper_not_a_test():\n"
            "    return 1\n"
        )
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = _cli(["--files", "test_spike.py", "--repo-root", str(tmp_path)])
        assert rc == 0, f"collect-nothing test spike should exit 0 (rc-5 remap), got {rc}"
        data = json.loads(buf.getvalue())
        assert data["exit_reason"] == "no-tests-collected-skip"
        assert data["pytest_returncode"] == 5
        assert data["ran_pytest"] is True

    def test_rc2_still_blocks(self, tmp_path):
        """R-06: pytest rc 2 (interrupted) must NOT be remapped -> exit 1 (blocking)."""
        (tmp_path / "test_x.py").write_text("def test_x(): pass\n")
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(returncode=2)
            rc = _cli(["--files", "test_x.py", "--repo-root", str(tmp_path)])
        assert rc == 1, f"rc 2 must stay blocking (exit 1), got {rc}"

    def test_rc3_still_blocks(self, tmp_path):
        """R-06: pytest rc 3 (internal error) must NOT be remapped -> exit 1."""
        (tmp_path / "test_x.py").write_text("def test_x(): pass\n")
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(returncode=3)
            rc = _cli(["--files", "test_x.py", "--repo-root", str(tmp_path)])
        assert rc == 1, f"rc 3 must stay blocking (exit 1), got {rc}"

    def test_rc1_still_blocks(self, tmp_path):
        """rc 1 (failures) stays blocking -> exit 1."""
        (tmp_path / "test_x.py").write_text("def test_x(): pass\n")
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(returncode=1)
            rc = _cli(["--files", "test_x.py", "--repo-root", str(tmp_path)])
        assert rc == 1, f"rc 1 must stay blocking (exit 1), got {rc}"

    def test_rc5_remaps_only_via_mock(self, tmp_path):
        """rc 5 -> exit 0 (mock control, complements the real-pytest test above)."""
        (tmp_path / "test_x.py").write_text("def test_x(): pass\n")
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(returncode=5)
            rc = _cli(["--files", "test_x.py", "--repo-root", str(tmp_path)])
        assert rc == 0, f"rc 5 must remap to exit 0, got {rc}"


class TestAc6NonRegression:
    """FR-9 fail-safe non-regression: absent allowlist behaves exactly as before."""

    def test_sh_only_still_exit_0_weak_guard(self, tmp_path, monkeypatch):
        """WEAK guard (NOT the BLOCKED repro): .sh-only changeset already exits 0
        (routed to `ignored` pre-change). Included as a non-regression check only."""
        monkeypatch.delenv("QUOIN_NONCOLLECTABLE_FILE", raising=False)
        (tmp_path / "x.sh").write_text("#!/usr/bin/env bash\necho ok\n")
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = _cli(["--files", "x.sh", "--repo-root", str(tmp_path)])
        assert rc == 0
        data = json.loads(buf.getvalue())
        assert "x.sh" in data["ignored"]

    def test_docs_only_run_noncollectable_empty(self, fake_repo, monkeypatch):
        """Absent allowlist: a docs-only run has noncollectable == [] and unchanged
        exit_reason (docs-only-no-selectors, NOT noncollectable-skip)."""
        monkeypatch.delenv("QUOIN_NONCOLLECTABLE_FILE", raising=False)
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = _cli(["--files", "README.md", "--repo-root", str(fake_repo)])
        assert rc == 0
        data = json.loads(buf.getvalue())
        assert data["noncollectable"] == []
        assert data["exit_reason"] == "docs-only-no-selectors"

    def test_orphan_still_exit_3_with_empty_allowlist(self, fake_repo, monkeypatch):
        """Absent allowlist: orphan.py still exits 3 (regression intact)."""
        monkeypatch.delenv("QUOIN_NONCOLLECTABLE_FILE", raising=False)
        rc = _cli(["--files", "orphan.py", "--repo-root", str(fake_repo)])
        assert rc == 3


# ---------------------------------------------------------------------------
# resolve_python / _probe_interpreter (venv interpreter resolution)
# ---------------------------------------------------------------------------

def _make_fake_venv(base: Path) -> Path:
    """Create base/.venv/bin/python as a SYMLINK to sys.executable.

    A symlink preserves the venv prefix through resolution; a copied binary
    would not, so this is the only fixture shape that can catch a stray
    .resolve() regression in the candidate handling.
    """
    bindir = base / ".venv" / "bin"
    bindir.mkdir(parents=True)
    link = bindir / "python"
    link.symlink_to(sys.executable)
    return link


class TestResolvePython:
    def test_symlinked_venv_found(self, tmp_path):
        _make_fake_venv(tmp_path)
        interp, reason = _at.resolve_python(tmp_path)
        assert reason == "venv"
        assert interp == str(tmp_path / ".venv" / "bin" / "python")

    def test_returned_path_is_not_realpath(self, tmp_path):
        """The candidate must never be .resolve()d — resolving a venv symlink
        strips the venv prefix and silently changes which site-packages it
        imports from."""
        _make_fake_venv(tmp_path)
        interp, _ = _at.resolve_python(tmp_path)
        assert interp != os.path.realpath(interp), (
            "resolve_python must return the venv symlink verbatim, not its realpath"
        )

    def test_parent_of_repo_layout(self, tmp_path):
        """venv one level above the anchor (the project root, not the git repo)."""
        _make_fake_venv(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        interp, reason = _at.resolve_python(repo)
        assert reason == "venv"
        assert interp == str(tmp_path / ".venv" / "bin" / "python")

    def test_relative_anchor_returns_absolute(self, tmp_path, monkeypatch):
        _make_fake_venv(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(tmp_path)
        interp, reason = _at.resolve_python(Path("repo"))
        assert reason == "venv"
        assert Path(interp).is_absolute()

    def test_no_venv_falls_back(self, tmp_path):
        interp, reason = _at.resolve_python(tmp_path)
        assert reason == "fallback"
        assert interp == sys.executable

    def test_non_executable_candidate_ignored(self, tmp_path):
        bindir = tmp_path / ".venv" / "bin"
        bindir.mkdir(parents=True)
        candidate = bindir / "python"
        candidate.write_text("#!/bin/sh\n")
        candidate.chmod(0o644)
        interp, reason = _at.resolve_python(tmp_path)
        assert reason == "fallback"
        assert interp == sys.executable

    def test_missing_candidate_ignored(self, tmp_path):
        (tmp_path / ".venv" / "bin").mkdir(parents=True)
        interp, reason = _at.resolve_python(tmp_path)
        assert reason == "fallback"

    def test_disable_knob_wins_over_valid_venv(self, tmp_path, monkeypatch):
        _make_fake_venv(tmp_path)
        monkeypatch.setenv("QUOIN_DISABLE_VENV_PROBE", "1")
        interp, reason = _at.resolve_python(tmp_path)
        assert (interp, reason) == (sys.executable, "disabled")

    @pytest.mark.parametrize("value", ["true", "0", ""])
    def test_disable_knob_exact_string_parsing(self, tmp_path, monkeypatch, value):
        _make_fake_venv(tmp_path)
        monkeypatch.setenv("QUOIN_DISABLE_VENV_PROBE", value)
        interp, reason = _at.resolve_python(tmp_path)
        assert reason == "venv", f"QUOIN_DISABLE_VENV_PROBE={value!r} must NOT disable"

    def test_quoin_python_accepted(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QUOIN_PYTHON", sys.executable)
        interp, reason = _at.resolve_python(tmp_path)
        assert (interp, reason) == (sys.executable, "env-override")

    def test_quoin_python_failing_probe_falls_through(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QUOIN_PYTHON", sys.executable)
        interp, reason = _at.resolve_python(tmp_path, probe="import _quoin_no_such_module_xyz")
        assert reason == "fallback"
        assert interp == sys.executable

    def test_quoin_python_missing_file_falls_through(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QUOIN_PYTHON", str(tmp_path / "no-such-python"))
        interp, reason = _at.resolve_python(tmp_path)
        assert reason == "fallback"

    def test_quoin_python_non_executable_falls_through(self, tmp_path, monkeypatch):
        bogus = tmp_path / "bogus-python"
        bogus.write_text("not a real interpreter\n")
        bogus.chmod(0o644)
        monkeypatch.setenv("QUOIN_PYTHON", str(bogus))
        interp, reason = _at.resolve_python(tmp_path)
        assert reason == "fallback"

    def test_venv_candidate_failing_probe_rejected(self, tmp_path):
        _make_fake_venv(tmp_path)
        interp, reason = _at.resolve_python(tmp_path, probe="import _quoin_no_such_module_xyz")
        assert reason == "fallback"
        assert interp == sys.executable

    def test_walk_stops_before_home(self, tmp_path, monkeypatch):
        """A .venv at $HOME must never be selected — the walk stops BEFORE it.

        Path.home must be handed an already-.resolve()d path: the walk
        anchor is .resolve()d at resolver entry, and an unresolved tmp_path
        can differ from its resolved form by a platform path prefix.
        """
        resolved_home = tmp_path.resolve()
        monkeypatch.setattr(Path, "home", staticmethod(lambda: resolved_home))
        _make_fake_venv(resolved_home)
        child = resolved_home / "child"
        child.mkdir()
        interp, reason = _at.resolve_python(child)
        assert reason == "fallback", "must not select a .venv at the home directory"

    def test_depth_cap_stops_the_walk(self, tmp_path):
        """A .venv 7 levels up (beyond _VENV_WALK_MAX_DEPTH=6) is not found."""
        deep = tmp_path
        for name in ("a", "b", "c", "d", "e", "f", "g"):
            deep = deep / name
        deep.mkdir(parents=True)
        _make_fake_venv(tmp_path)  # 7 levels above `deep`
        interp, reason = _at.resolve_python(deep)
        assert reason == "fallback", "a .venv beyond the depth cap must not be found"

    def test_probe_interpreter_missing_binary_returns_false(self, tmp_path):
        ok = _at._probe_interpreter(str(tmp_path / "no-such-binary"), "import sys")
        assert ok is False

    def test_probe_interpreter_never_calls_run_helper(self, tmp_path, monkeypatch):
        """_probe_interpreter must not go through _run() — that helper's
        FileNotFoundError branch hardcodes a git-specific error message that
        would be misleading for a python-interpreter probe."""
        calls = []
        monkeypatch.setattr(_at, "_run", lambda args: (calls.append(args), ("", "", 1))[1])
        ok = _at._probe_interpreter(str(tmp_path / "no-such-binary"), "import sys")
        assert ok is False
        assert calls == [], "_probe_interpreter must not call _run()"


# ---------------------------------------------------------------------------
# Interpreter field wiring in main() (single resolution call site)
# ---------------------------------------------------------------------------

class TestInterpreterFieldWiring:
    def test_present_in_json_venv_present(self, tmp_path):
        _make_fake_venv(tmp_path)
        (tmp_path / "test_x.py").write_text("def test_x(): pass\n")
        import io, contextlib
        buf = io.StringIO()
        # A symlinked fake venv's OWN candidate binary is often not a proper
        # venv launcher (no sibling pyvenv.cfg at that path), so invoking it
        # for a real "import pytest" probe is host-dependent. Mock the probe
        # outcome directly — resolve_python's candidate-selection logic
        # (the thing under test here) is unaffected by how the probe itself
        # is satisfied.
        with mock.patch.object(_at, "_probe_interpreter", return_value=True):
            with contextlib.redirect_stdout(buf):
                _cli(["--select-only", "--files", "test_x.py", "--repo-root", str(tmp_path)])
        data = json.loads(buf.getvalue())
        assert data["interpreter"] == str(tmp_path / ".venv" / "bin" / "python")
        assert data["interpreter_reason"] == "venv"

    def test_present_in_text_format(self, tmp_path):
        _make_fake_venv(tmp_path)
        (tmp_path / "test_x.py").write_text("def test_x(): pass\n")
        import io, contextlib
        buf = io.StringIO()
        with mock.patch.object(_at, "_probe_interpreter", return_value=True):
            with contextlib.redirect_stdout(buf):
                _cli(["--select-only", "--files", "test_x.py",
                      "--repo-root", str(tmp_path), "--format", "text"])
        out = buf.getvalue()
        assert "interpreter:" in out
        assert "interpreter_reason: venv" in out

    def test_absent_on_exit5_no_task_context(self, tmp_path):
        foreign = tmp_path / "foreign"
        foreign.mkdir()
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = _cli(["--project-root", str(foreign), "--require-task-context"])
        assert rc == 5
        data = json.loads(buf.getvalue())
        assert "interpreter" not in data
        assert "interpreter_reason" not in data

    def test_absent_on_no_changes(self, tmp_path):
        _init_git_repo(tmp_path / "repo")
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = _cli(["--project-root", str(tmp_path)])
        assert rc == 0
        data = json.loads(buf.getvalue())
        assert data["exit_reason"] == "no-changes"
        assert "interpreter" not in data
        assert "interpreter_reason" not in data

    def test_files_mode_resolves_without_raising(self, tmp_path):
        rc = _cli(["--select-only", "--files", "nope.py", "--repo-root", str(tmp_path)])
        assert rc == 0

    def test_files_from_mode_resolves_without_raising(self, tmp_path):
        src = tmp_path / "list.txt"
        src.write_text("nope.py\n")
        rc = _cli(["--select-only", "--files-from", str(src), "--repo-root", str(tmp_path)])
        assert rc == 0

    def test_pytest_argv_uses_resolved_interpreter(self, tmp_path):
        """Proves the pytest subprocess.run call uses the resolved venv
        interpreter as argv[0], not the invoking sys.executable.

        Deliberately does NOT pin --repo-root to an isolated tmp_path — this
        test's own point is to exercise the probe call site against a real,
        discoverable venv, and simultaneously serves as the collision
        regression: the probe snippet contains the substring "pytest" and
        must not trip any of the pytest-not-in-call assertions elsewhere in
        this file (those live in separate test functions, unaffected by this
        one firing correctly). subprocess.run is mocked to always report
        success for BOTH the probe and the pytest calls — whether a real
        "import pytest" subprocess succeeds through a symlinked venv
        launcher depends on host-specific pyvenv.cfg discovery unrelated to
        the wiring under test here.
        """
        fake = _make_fake_venv(tmp_path)
        (tmp_path / "test_x.py").write_text("def test_x(): pass\n")

        with mock.patch(
            "subprocess.run", return_value=subprocess.CompletedProcess([], 0)
        ) as mock_run:
            rc = _cli(["--files", "test_x.py", "--repo-root", str(tmp_path)])
            assert mock_run.call_count == 2, (
                f"expected probe + pytest calls, got {mock_run.call_args_list}"
            )
            last_call = mock_run.call_args_list[-1]
            argv = last_call[0][0] if last_call[0] else last_call.args[0]
            assert argv[0] == str(fake), (
                f"pytest argv[0] must be the resolved venv interpreter, got {argv}"
            )
        assert rc == 0

    def test_precheck_skipped_when_interpreter_is_venv(self, tmp_path):
        """When resolve_python selects a venv interpreter, the in-process
        find_spec('pytest') precheck must be bypassed — checking THIS
        process's pytest availability says nothing about the venv
        interpreter's."""
        _make_fake_venv(tmp_path)
        (tmp_path / "test_x.py").write_text("def test_x(): pass\n")

        with mock.patch("importlib.util.find_spec", return_value=None), \
                mock.patch(
                    "subprocess.run", return_value=subprocess.CompletedProcess([], 0)
                ):
            rc = _cli(["--files", "test_x.py", "--repo-root", str(tmp_path)])
        assert rc == 0, (
            f"find_spec=None must not block when a venv interpreter is resolved, got {rc}"
        )

    def test_disable_probe_pytest_missing_preserved(self, tmp_path):
        """With the panic-button knob set, the fallback interpreter is
        sys.executable, so the in-process find_spec precheck is authoritative
        again and pytest-missing must still be reachable."""
        (tmp_path / "test_x.py").write_text("def test_x(): pass\n")
        import io, contextlib
        buf = io.StringIO()
        with mock.patch("importlib.util.find_spec", return_value=None):
            with contextlib.redirect_stdout(buf):
                rc = _cli(
                    ["--files", "test_x.py", "--repo-root", str(tmp_path)],
                    env={"QUOIN_DISABLE_VENV_PROBE": "1"},
                )
        assert rc == 3
        data = json.loads(buf.getvalue())
        assert data["exit_reason"] == "pytest-missing"
        assert data.get("interpreter_reason") == "disabled"


# ---------------------------------------------------------------------------
# --print-interpreter (early return, before repo resolution)
# ---------------------------------------------------------------------------

def _capture_help() -> str:
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            _at.main(["--help"])
        except SystemExit:
            pass
    return buf.getvalue()


class TestPrintInterpreter:
    def test_exits_0_with_two_lines(self, tmp_path):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = _cli(["--print-interpreter", "--project-root", str(tmp_path)])
        assert rc == 0
        out = buf.getvalue()
        assert "interpreter:" in out
        assert "interpreter_reason:" in out

    def test_files_mode_does_not_raise(self):
        """args.project_root is legally None here (--print-interpreter is
        outside the required mode group) — must not raise TypeError."""
        rc = _cli(["--print-interpreter", "--files", "foo.py"])
        assert rc == 0

    def test_precedes_no_repo_exit3(self, tmp_path):
        """A dir with no git repo would normally exit 3 for --project-root
        mode — --print-interpreter returns 0 first, before repo resolution."""
        rc = _cli(["--print-interpreter", "--project-root", str(tmp_path)])
        assert rc == 0

    def test_precedes_require_task_context_exit5(self, tmp_path):
        rc = _cli(["--print-interpreter", "--project-root", str(tmp_path),
                   "--require-task-context"])
        assert rc == 0

    def test_disable_still_wins(self, tmp_path):
        rc = _cli(["--print-interpreter", "--project-root", str(tmp_path)],
                  env={"QUOIN_DISABLE_AFFECTED_TESTS": "1"})
        assert rc == 3

    def test_project_level_vs_repo_local_venv_divergence(self, tmp_path):
        """Pins the documented anchor divergence: --print-interpreter anchors
        at --project-root itself, which can differ from repo_root (a real
        run's anchor) when a repo-local .venv and a project-level .venv
        coexist. This is EXPECTED, not a bug — see the flag's help text."""
        project_venv = _make_fake_venv(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        repo_venv = _make_fake_venv(repo)

        import io, contextlib
        buf = io.StringIO()
        with mock.patch.object(_at, "_probe_interpreter", return_value=True):
            with contextlib.redirect_stdout(buf):
                rc = _cli(["--print-interpreter", "--project-root", str(tmp_path)])
            assert rc == 0
            assert str(project_venv) in buf.getvalue()

            real_run_interp, _ = _at.resolve_python(repo, probe="import pytest")
        assert real_run_interp == str(repo_venv)
        assert real_run_interp != str(project_venv), (
            "the divergence this test pins: --print-interpreter reports the "
            "project-level interpreter while a real run at repo_root would "
            "select the repo-local one"
        )

    def test_help_text_discloses_divergence(self):
        out = _capture_help().lower()
        assert "diverge" in out and "project-root" in out

    def test_probe_parity_rejects_broken_venv(self, tmp_path):
        """A venv that exists and is executable but fails the probe must
        report interpreter_reason=fallback, not venv — proving the probe
        argument is actually passed through and not silently dropped to
        None."""
        bindir = tmp_path / ".venv" / "bin"
        bindir.mkdir(parents=True)
        stub = bindir / "python"
        stub.write_text("#!/bin/sh\nexit 1\n")
        stub.chmod(0o755)

        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = _cli(["--print-interpreter", "--project-root", str(tmp_path)])
        assert rc == 0
        assert "interpreter_reason: fallback" in buf.getvalue()
