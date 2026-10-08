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

    def test_context_tracker_plugin_source_selects_its_tests(self):
        """Editing the mod's non-.py source selects both of its test files."""
        repo = Path(__file__).resolve().parents[3]
        selectors, unmatched, ignored = _at.map_changed_to_tests(
            ["quoin/plugins/context-tracker/hooks/register.tsx"], repo
        )
        joined = " ".join(str(s) for s in selectors)
        assert "test_context_tracker_install.py" in joined
        assert "test_context_tracker_plugin_ts.py" in joined
        assert not ignored

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

    def test_deleted_test_file_is_ignored_not_selected(self, fake_repo):
        """A changed test file that no longer exists on disk must not become a
        selector: pytest exits 4 on a missing path, which reads as a red
        affected area.
        """
        selectors, unmatched, ignored = _at.map_changed_to_tests(["test_removed.py"], fake_repo)
        assert not selectors
        assert not unmatched
        assert ignored == ["test_removed.py"]

    @pytest.mark.parametrize("excluded", [
        ".venv/lib/site-packages/pkg",
        "node_modules/pkg",
        ".tox/py312",
        "build/lib",
        "dist",
        "pkg.egg-info",
    ])
    def test_excluded_dirs_are_not_scanned_for_tests(self, fake_repo, excluded):
        """Test files inside a virtualenv, node_modules or build output are
        never selection candidates, even when they match the changed module
        by name.
        """
        pkg_dir = fake_repo / excluded
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "test_foo.py").write_text("import foo\ndef test_x(): pass\n")
        selectors, unmatched, _ = _at.map_changed_to_tests(["foo.py"], fake_repo)
        assert selectors == [str(fake_repo / "test_foo.py")]
        assert not unmatched

    def test_shared_exclude_names_unchanged(self):
        """_EXCLUDE_NAMES mirrors branch_hygiene's repo-discovery set; the
        wider test-walk set must live in its own constant."""
        assert _at._EXCLUDE_NAMES == frozenset({
            ".workflow_artifacts", ".git", "node_modules", ".venv", "venv",
            "__pycache__", ".idea", ".vscode",
        })
        assert _at._EXCLUDE_NAMES < _at._WALK_EXCLUDE_NAMES

    def test_selection_to_dict_has_missing_tests_default(self):
        sel = _at.Selection(
            changed=[], selectors=[], unmatched_sources=[], ignored=[],
            ran_pytest=False, pytest_returncode=None, exit_reason="x",
        )
        assert sel.to_dict()["missing_tests"] == []

    def test_conftest_selects_tests_under_its_directory(self, tmp_path):
        (tmp_path / "pkg" / "tests" / "sub").mkdir(parents=True)
        (tmp_path / "other").mkdir()
        (tmp_path / "pkg" / "tests" / "conftest.py").write_text("")
        a = tmp_path / "pkg" / "tests" / "test_a.py"
        b = tmp_path / "pkg" / "tests" / "sub" / "test_b.py"
        c = tmp_path / "other" / "test_c.py"
        for t in (a, b, c):
            t.write_text("def test_x(): pass\n")
        selectors, unmatched, _ = _at.map_changed_to_tests(
            ["pkg/tests/conftest.py"], tmp_path)
        assert selectors == sorted([str(a), str(b)])
        assert not unmatched

    def test_root_conftest_selects_all_non_excluded_tests(self, tmp_path):
        (tmp_path / "conftest.py").write_text("")
        (tmp_path / "d").mkdir()
        (tmp_path / ".venv" / "lib").mkdir(parents=True)
        a = tmp_path / "test_a.py"
        b = tmp_path / "d" / "test_b.py"
        v = tmp_path / ".venv" / "lib" / "test_v.py"
        for t in (a, b, v):
            t.write_text("def test_x(): pass\n")
        selectors, _, _ = _at.map_changed_to_tests(["conftest.py"], tmp_path)
        assert selectors == sorted([str(a), str(b)])

    def test_deleted_conftest_still_selects_its_directory(self, tmp_path):
        (tmp_path / "t").mkdir()
        a = tmp_path / "t" / "test_a.py"
        a.write_text("def test_x(): pass\n")
        selectors, unmatched, _ = _at.map_changed_to_tests(["t/conftest.py"], tmp_path)
        assert selectors == [str(a)]
        assert not unmatched

    def test_conftest_without_tests_is_unmatched(self, tmp_path):
        (tmp_path / "empty").mkdir()
        (tmp_path / "empty" / "conftest.py").write_text("")
        selectors, unmatched, _ = _at.map_changed_to_tests(["empty/conftest.py"], tmp_path)
        assert not selectors
        assert unmatched == ["empty/conftest.py"]

    def test_deleted_source_without_references_is_unmatched(self, fake_repo):
        """A removed non-test source cannot be told apart from a mistyped path
        in --files mode, so it stays fail-closed (unmatched) rather than
        being treated as green.
        """
        selectors, unmatched, ignored = _at.map_changed_to_tests(["gone.py"], fake_repo)
        assert not selectors
        assert unmatched == ["gone.py"]
        assert not ignored

    def test_deleted_source_still_referenced_selects_its_tests(self, fake_repo):
        """A removed module that tests still mention keeps selecting them: they
        fail if they still import it."""
        (fake_repo / "test_uses_gone.py").write_text("import gone\ndef test_x(): pass\n")
        selectors, unmatched, _ = _at.map_changed_to_tests(["gone.py"], fake_repo)
        assert selectors == [str(fake_repo / "test_uses_gone.py")]
        assert not unmatched


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


def _cli_capture_env(args, env=None):
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _cli(args, env=env)
    return rc, buf.getvalue()


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

    def test_print_interpreter_answered_despite_disable_knob(self, tmp_path):
        """--print-interpreter runs no tests, so the fail-closed reason for the
        disable knob (never green-light an APPROVE) does not apply: it is
        answered first instead of exiting 3."""
        rc, out = _cli_capture_env(
            ["--print-interpreter", "--interpreter-only", "--project-root", str(tmp_path)],
            env={"QUOIN_DISABLE_AFFECTED_TESTS": "1"},
        )
        assert rc == 0
        lines = out.splitlines()
        assert len(lines) == 1
        assert not lines[0].lstrip().startswith("{")

    def test_interpreter_only_prints_bare_path_under_default_format(self, tmp_path):
        rc, out = _cli_capture_env(
            ["--print-interpreter", "--interpreter-only", "--project-root", str(tmp_path)]
        )
        assert rc == 0
        lines = out.splitlines()
        assert len(lines) == 1
        assert Path(lines[0]).is_absolute()

    def test_third_line_names_anchor_default(self, tmp_path):
        rc, out = _cli_capture_env(["--print-interpreter", "--project-root", str(tmp_path)])
        assert rc == 0
        assert "interpreter_anchor: project-root" in out.splitlines()

    def _repo_with_venv(self, tmp_path):
        project_venv = _make_fake_venv(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        repo_venv = _make_fake_venv(repo)
        return project_venv, repo_venv

    def test_anchor_repo_selects_repo_local_venv(self, tmp_path):
        project_venv, repo_venv = self._repo_with_venv(tmp_path)
        with mock.patch.object(_at, "_probe_interpreter", return_value=True):
            rc, out = _cli_capture_env(
                ["--print-interpreter", "--interpreter-anchor", "repo",
                 "--project-root", str(tmp_path)]
            )
            rc2, out2 = _cli_capture_env(
                ["--print-interpreter", "--interpreter-anchor", "repo",
                 "--interpreter-only", "--project-root", str(tmp_path)]
            )
        assert rc == 0 and rc2 == 0
        expected = str(_at.discover_repos(tmp_path)[0].resolve() / ".venv" / "bin" / "python")
        assert f"interpreter: {expected}" in out.splitlines()
        assert "interpreter_anchor: repo" in out.splitlines()
        assert out2.strip() == expected
        assert str(project_venv) not in out

    def test_anchor_repo_none_falls_back(self, tmp_path):
        with mock.patch.object(_at, "_probe_interpreter", return_value=True):
            rc, out = _cli_capture_env(
                ["--print-interpreter", "--interpreter-anchor", "repo",
                 "--project-root", str(tmp_path)]
            )
        assert rc == 0
        assert "interpreter_anchor: project-root (repo unresolved)" in out.splitlines()

    def test_anchor_repo_multiple_repos_falls_back(self, tmp_path):
        for name in ("a", "b"):
            (tmp_path / name / ".git").mkdir(parents=True)
        with mock.patch.object(_at, "_probe_interpreter", return_value=True):
            rc, out = _cli_capture_env(
                ["--print-interpreter", "--interpreter-anchor", "repo",
                 "--project-root", str(tmp_path)]
            )
        assert rc == 0
        assert "interpreter_anchor: project-root (multiple repos)" in out.splitlines()

    def test_anchor_repo_reason_venv(self, tmp_path):
        _, repo_venv = self._repo_with_venv(tmp_path)
        with mock.patch.object(_at, "_probe_interpreter", return_value=True):
            rc, out = _cli_capture_env(
                ["--print-interpreter", "--interpreter-anchor", "repo",
                 "--project-root", str(tmp_path)]
            )
        assert "interpreter_reason: venv" in out.splitlines()
        # the venv path is reported as the link itself, not its resolved target
        assert not any(
            line.startswith("interpreter: ") and line.endswith(str(Path(sys.executable).resolve()))
            and ".venv" not in line
            for line in out.splitlines()
        )

    def test_anchor_repo_reason_env_override(self, tmp_path):
        self._repo_with_venv(tmp_path)
        stub = tmp_path / "usable-python"
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
        with mock.patch.object(_at, "_probe_interpreter", return_value=True):
            rc, out = _cli_capture_env(
                ["--print-interpreter", "--interpreter-anchor", "repo",
                 "--project-root", str(tmp_path)],
                env={"QUOIN_PYTHON": str(stub)},
            )
        assert "interpreter_reason: env-override" in out.splitlines()
        assert f"interpreter: {stub}" in out.splitlines()

    def test_anchor_repo_reason_disabled(self, tmp_path):
        self._repo_with_venv(tmp_path)
        rc, out = _cli_capture_env(
            ["--print-interpreter", "--interpreter-anchor", "repo",
             "--project-root", str(tmp_path)],
            env={"QUOIN_DISABLE_VENV_PROBE": "1"},
        )
        assert "interpreter_reason: disabled" in out.splitlines()

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


# ---------------------------------------------------------------------------
# Missing (deleted) test files and excluded directories, end to end
# ---------------------------------------------------------------------------

def _cli_capture(args):
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _cli(args)
    return rc, buf.getvalue()


def _pytest_calls(mock_run):
    return [c for c in mock_run.call_args_list if "pytest" in str(c)]


class TestMissingAndExcludedCli:
    def test_files_mode_missing_test_is_not_a_failure(self, tmp_path):
        with mock.patch("subprocess.run") as mock_run:
            rc, out = _cli_capture(["--files", "test_gone.py", "--repo-root", str(tmp_path)])
        d = json.loads(out)
        assert rc == 0
        assert d["exit_reason"] == "missing-tests-only"
        assert d["missing_tests"] == ["test_gone.py"]
        assert d["ran_pytest"] is False
        assert not _pytest_calls(mock_run)

    def test_git_deleted_test_is_not_a_failure(self, tmp_path):
        repo = tmp_path / "repo"
        _init_git_repo(repo)
        (repo / "test_old.py").write_text("def test_x(): pass\n")
        _git("add", "test_old.py", cwd=repo)
        _git("commit", "-m", "add test", cwd=repo)
        _git("checkout", "-b", "feature", cwd=repo)
        _git("rm", "test_old.py", cwd=repo)
        _git("commit", "-m", "remove test", cwd=repo)
        with mock.patch.object(_at, "_probe_interpreter", return_value=True):
            rc, out = _cli_capture(["--project-root", str(repo)])
        d = json.loads(out)
        assert rc == 0
        assert d["exit_reason"] == "missing-tests-only"
        assert d["missing_tests"] == ["test_old.py"]
        assert d["ran_pytest"] is False

    def test_mixed_missing_and_live_runs_only_live(self, fake_repo):
        real_run = subprocess.run

        def fake_run(cmd, *a, **kw):
            if "pytest" in cmd:
                return subprocess.CompletedProcess(cmd, 0)
            return real_run(cmd, *a, **kw)

        with mock.patch.object(_at.subprocess, "run", side_effect=fake_run) as mock_run:
            rc, out = _cli_capture(["--files", "test_gone.py", "foo.py",
                                    "--repo-root", str(fake_repo)])
        d = json.loads(out)
        assert rc == 0
        assert d["exit_reason"] == "affected-green"
        assert d["missing_tests"] == ["test_gone.py"]
        argv = [c.args[0] for c in _pytest_calls(mock_run)][0]
        joined = " ".join(argv)
        assert "test_foo.py" in joined
        assert "test_gone.py" not in joined

    def test_virtualenv_tests_never_selected(self, fake_repo):
        venv_pkg = fake_repo / ".venv" / "lib" / "site-packages" / "pkg"
        venv_pkg.mkdir(parents=True)
        (venv_pkg / "test_foo.py").write_text("import foo\n")
        rc, out = _cli_capture(["--select-only", "--files", "foo.py",
                                "--repo-root", str(fake_repo)])
        d = json.loads(out)
        assert rc == 0
        assert d["selectors"] == [str(fake_repo / "test_foo.py")]
        assert not any(".venv" in sel for sel in d["selectors"])

    @pytest.mark.parametrize("vendored", [
        ".venv/lib/python3.14/site-packages/jsonschema/tests/test_x.py",
        "site-packages/pkg/tests/test_x.py",
        "build/lib/pkg/tests/test_x.py",
        "pkg.egg-info/tests/test_x.py",
    ])
    def test_vendored_changed_test_is_never_selected(self, tmp_path, vendored):
        (tmp_path / "foo.py").write_text("def foo(): return 1\n")
        (tmp_path / "test_real.py").write_text("import foo\n")
        vfile = tmp_path / vendored
        vfile.parent.mkdir(parents=True)
        vfile.write_text("def test_v(): pass\n")
        real = str(tmp_path / "test_real.py")

        rc, out = _cli_capture(["--select-only", "--files", "foo.py", "--repo-root", str(tmp_path)])
        assert json.loads(out)["selectors"] == [real]

        for form in (vendored, str(vfile)):
            rc, out = _cli_capture(["--select-only", "--files", form, "--repo-root", str(tmp_path)])
            d = json.loads(out)
            assert rc == 0
            assert d["selectors"] == []
            assert form in d["ignored"]
            assert d["missing_tests"] == []
            assert d["exit_reason"] != "missing-tests-only"

    def test_vendored_path_that_is_missing_on_disk_is_still_missing(self, tmp_path):
        rc, out = _cli_capture(["--files", "build/lib/test_gone.py", "--repo-root", str(tmp_path)])
        d = json.loads(out)
        assert d["missing_tests"] == ["build/lib/test_gone.py"]

    def test_docs_to_tests_targets_never_under_excluded_dir(self):
        for _src, target in _at._DOCS_TO_TESTS:
            parts = Path(target).parts
            assert not any(
                part in _at._WALK_EXCLUDE_NAMES or part.endswith(".egg-info")
                for part in parts
            ), target

    def test_docs_only_stays_distinguishable(self, tmp_path):
        rc, out = _cli_capture(["--files", "README.md", "--repo-root", str(tmp_path)])
        d = json.loads(out)
        assert rc == 0
        assert d["exit_reason"] == "docs-only-no-selectors"
        assert d["missing_tests"] == []

    def test_text_format_lists_missing_tests(self, tmp_path):
        rc, out = _cli_capture(["--files", "test_gone.py", "--repo-root", str(tmp_path),
                                "--format", "text"])
        assert rc == 0
        assert "missing_tests (1): test_gone.py" in out

    def test_missing_test_alongside_unmatched_source_still_exit_3(self, fake_repo):
        rc, out = _cli_capture(["--files", "test_gone.py", "orphan.py",
                                "--repo-root", str(fake_repo)])
        assert rc == 3
        assert json.loads(out)["exit_reason"] == "unmatched-sources"

    def test_deleted_source_exits_3(self, fake_repo):
        rc, out = _cli_capture(["--files", "gone.py", "--repo-root", str(fake_repo)])
        assert rc == 3
        assert json.loads(out)["exit_reason"] == "unmatched-sources"

    def test_conftest_scope_end_to_end(self, tmp_path):
        (tmp_path / "dev" / "tests").mkdir(parents=True)
        (tmp_path / ".venv" / "x").mkdir(parents=True)
        (tmp_path / "dev" / "tests" / "conftest.py").write_text("")
        (tmp_path / "dev" / "tests" / "test_a.py").write_text("")
        (tmp_path / ".venv" / "x" / "test_v.py").write_text("")
        rc, out = _cli_capture(["--select-only", "--files", "dev/tests/conftest.py",
                                "--repo-root", str(tmp_path)])
        d = json.loads(out)
        assert rc == 0
        assert d["selectors"] == [str(tmp_path / "dev" / "tests" / "test_a.py")]


class TestPrintInterpreterStandalone:
    def test_alone_exits_0_with_both_lines(self):
        rc, out = _cli_capture(["--print-interpreter"])
        assert rc == 0
        assert "interpreter:" in out and "interpreter_reason:" in out

    def test_no_mode_still_exit_2(self):
        assert _cli([]) == 2

    def test_two_modes_together_still_exit_2(self):
        assert _cli(["--files", "a.py", "--files-from", "x"]) == 2


# ---------------------------------------------------------------------------
# Interpreter health: broken venv interpreters are reported, not guessed past
# ---------------------------------------------------------------------------

def _write_sh_interpreter(path: Path, body: str = "exit 0") -> Path:
    """Write an executable shell script standing in for a Python interpreter."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(0o755)
    return path


def _dangling_venv(base: Path, chained: bool = False) -> Path:
    """base/.venv/bin/python -> (python3.14 ->) a target that does not exist."""
    bindir = base / ".venv" / "bin"
    bindir.mkdir(parents=True)
    (base / ".venv" / "pyvenv.cfg").write_text(
        "home = /nonexistent/bin\nversion = 3.14.6\n"
    )
    link = bindir / "python"
    if chained:
        (bindir / "python3.14").symlink_to(base / "gone" / "python3.14")
        link.symlink_to("python3.14")
    else:
        link.symlink_to(base / "gone" / "python3.14")
    return link


@pytest.fixture()
def clean_interp_env(monkeypatch):
    monkeypatch.delenv("QUOIN_PYTHON", raising=False)
    monkeypatch.delenv("QUOIN_DISABLE_VENV_PROBE", raising=False)


class TestInterpreterHealth:
    @pytest.fixture(autouse=True)
    def _env(self, clean_interp_env):
        yield

    def test_dangling_link(self, tmp_path):
        _dangling_venv(tmp_path)
        interp, reason, problems = _at.resolve_python_detail(tmp_path, probe=None)
        assert (interp, reason) == (sys.executable, "fallback")
        assert [p.kind for p in problems] == ["dangling-link"]
        assert problems[0].broken
        assert "gone/python3.14" in problems[0].detail
        assert "3.14.6" in problems[0].detail

    def test_chained_dangling_link(self, tmp_path):
        _dangling_venv(tmp_path, chained=True)
        _, reason, problems = _at.resolve_python_detail(tmp_path, probe=None)
        assert reason == "fallback"
        assert [p.kind for p in problems] == ["dangling-link"]

    def test_pyvenv_cfg_without_bin_python(self, tmp_path):
        (tmp_path / ".venv" / "bin").mkdir(parents=True)
        (tmp_path / ".venv" / "pyvenv.cfg").write_text("home = /x\n")
        _, reason, problems = _at.resolve_python_detail(tmp_path, probe=None)
        assert reason == "fallback"
        assert [p.kind for p in problems] == ["missing-interpreter"]
        assert problems[0].broken

    def test_python3_present_python_missing(self, tmp_path):
        (tmp_path / ".venv" / "pyvenv.cfg").parent.mkdir(parents=True)
        (tmp_path / ".venv" / "pyvenv.cfg").write_text("home = /x\n")
        _write_sh_interpreter(tmp_path / ".venv" / "bin" / "python3")
        _, _, problems = _at.resolve_python_detail(tmp_path, probe=None)
        assert problems[0].kind == "missing-interpreter"
        assert "bin/python missing; bin/python3 present" in problems[0].detail

    def test_no_pyvenv_cfg_no_interpreter_is_silent(self, tmp_path):
        (tmp_path / ".venv" / "bin").mkdir(parents=True)
        _, reason, problems = _at.resolve_python_detail(tmp_path, probe=None)
        assert reason == "fallback"
        assert problems == []

    def test_unrunnable_stub_is_not_runnable(self, tmp_path):
        _write_sh_interpreter(tmp_path / ".venv" / "bin" / "python", "exit 1")
        _, reason, problems = _at.resolve_python_detail(tmp_path, probe="import pytest")
        assert reason == "fallback"
        assert [p.kind for p in problems] == ["not-runnable"]
        assert problems[0].broken

    def test_runnable_without_pytest_is_listed_not_broken(self, tmp_path):
        # exits 0 only for the bare `pass` probe: `$1` is -c, `$2` the code
        _write_sh_interpreter(
            tmp_path / ".venv" / "bin" / "python",
            '[ "$2" = pass ] && exit 0\nexit 1',
        )
        _, reason, problems = _at.resolve_python_detail(tmp_path, probe="import pytest")
        assert reason == "fallback"
        assert [p.kind for p in problems] == ["no-pytest"]
        assert not problems[0].broken

    def test_broken_nearest_healthy_parent(self, tmp_path):
        parent_py = _write_sh_interpreter(tmp_path / ".venv" / "bin" / "python")
        child = tmp_path / "child"
        child.mkdir()
        _dangling_venv(child)
        interp, reason, problems = _at.resolve_python_detail(child, probe=None)
        assert reason == "venv"
        assert interp == str(parent_py.resolve().parent.parent / "bin" / "python") or \
            Path(interp).samefile(parent_py)
        assert [p.kind for p in problems] == ["dangling-link"]

    def test_timeout_reported_distinctly(self, tmp_path):
        _write_sh_interpreter(tmp_path / ".venv" / "bin" / "python", "sleep 5")
        with mock.patch.object(_at, "_subprocess_timeout", return_value=1):
            _, _, problems = _at.resolve_python_detail(tmp_path, probe="import pytest")
        assert [p.kind for p in problems] == ["not-runnable"]
        assert "timed out" in problems[0].detail
        assert problems[0].broken

    def test_dangling_quoin_python_recorded_and_walk_continues(self, tmp_path):
        # a dangling QUOIN_PYTHON is broken (source QUOIN_PYTHON); the walk goes on
        link = tmp_path / "my-python"
        link.symlink_to(tmp_path / "nowhere")
        healthy = _write_sh_interpreter(tmp_path / ".venv" / "bin" / "python")
        with mock.patch.dict(os.environ, {"QUOIN_PYTHON": str(link)}):
            interp, reason, problems = _at.resolve_python_detail(tmp_path, probe=None)
        assert reason == "venv"
        assert Path(interp).samefile(healthy)
        assert problems[0].source == "QUOIN_PYTHON"
        assert problems[0].kind == "dangling-link"
        assert problems[0].broken

    def test_disable_probe_skips_detection(self, tmp_path):
        _dangling_venv(tmp_path)
        with mock.patch.dict(os.environ, {"QUOIN_DISABLE_VENV_PROBE": "1"}):
            assert _at.resolve_python_detail(tmp_path, probe=None) == (
                sys.executable, "disabled", [])

    def test_healthy_venv_has_no_problems_and_is_not_resolved(self, tmp_path):
        link = _make_fake_venv(tmp_path)
        with mock.patch.object(_at, "_probe_interpreter", return_value=True):
            interp, reason, problems = _at.resolve_python_detail(
                tmp_path, probe="import pytest")
        assert (interp, reason, problems) == (str(link), "venv", [])
        assert interp != str(link.resolve())

    def test_seam_patched_success_selects_candidate_without_subprocess(self, tmp_path):
        link = _make_fake_venv(tmp_path)
        with mock.patch.object(_at, "_probe_interpreter", return_value=True), \
                mock.patch("subprocess.run") as mock_run:
            interp, reason, problems = _at.resolve_python_detail(
                tmp_path, probe="import pytest")
        assert (interp, reason, problems) == (str(link), "venv", [])
        mock_run.assert_not_called()

    def test_seam_routes_both_probes(self, tmp_path):
        _make_fake_venv(tmp_path)
        with mock.patch.object(_at, "_probe_interpreter", side_effect=[False, True]):
            _, _, problems = _at.resolve_python_detail(tmp_path, probe="import pytest")
        assert [(p.kind, p.broken) for p in problems] == [("no-pytest", False)]
        with mock.patch.object(_at, "_probe_interpreter", side_effect=[False, False]):
            _, _, problems = _at.resolve_python_detail(tmp_path, probe="import pytest")
        assert [(p.kind, p.broken) for p in problems] == [("not-runnable", True)]

    def test_timeout_flag_skips_second_probe(self, tmp_path):
        _make_fake_venv(tmp_path)
        calls = []

        def probe(candidate, code):
            calls.append(code)
            _at._PROBE_TIMED_OUT = True
            return False

        with mock.patch.object(_at, "_probe_interpreter", side_effect=probe):
            _, _, problems = _at.resolve_python_detail(tmp_path, probe="import pytest")
        assert calls == ["import pytest"]
        assert problems[0].kind == "not-runnable"
        assert "timed out" in problems[0].detail

    def test_plain_failure_runs_second_probe(self, tmp_path):
        _make_fake_venv(tmp_path)
        calls = []

        def probe(candidate, code):
            calls.append(code)
            return False

        with mock.patch.object(_at, "_probe_interpreter", side_effect=probe):
            _at.resolve_python_detail(tmp_path, probe="import pytest")
        assert calls == ["import pytest", "pass"]

    def test_stale_timeout_flag_does_not_leak(self, tmp_path):
        _make_fake_venv(tmp_path)
        _at._PROBE_TIMED_OUT = True
        try:
            with mock.patch.object(_at, "_probe_interpreter", return_value=True):
                _, reason, problems = _at.resolve_python_detail(
                    tmp_path, probe="import pytest")
        finally:
            _at._PROBE_TIMED_OUT = False
        assert reason == "venv"
        assert problems == []

    def test_nonexistent_quoin_python_is_recorded_not_broken(self, tmp_path):
        with mock.patch.dict(os.environ, {"QUOIN_PYTHON": str(tmp_path / "nope")}):
            _, reason, problems = _at.resolve_python_detail(tmp_path, probe=None)
        assert reason == "fallback"
        assert len(problems) == 1
        assert problems[0].kind == "missing-interpreter"
        assert problems[0].source == "QUOIN_PYTHON"
        assert problems[0].broken is False

    def test_str_format(self):
        p = _at.InterpreterProblem("/a/python", "dangling-link", "gone", True)
        assert str(p) == "dangling-link: /a/python (gone)"


class TestBrokenVenvCli:
    @pytest.fixture(autouse=True)
    def _env(self, clean_interp_env):
        yield

    @staticmethod
    def _repo(tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "foo.py").write_text("def foo(): pass\n")
        (repo / "test_foo.py").write_text("import foo\ndef test_foo(): pass\n")
        return repo

    def test_broken_venv_blocks_run(self, tmp_path):
        repo = self._repo(tmp_path)
        _dangling_venv(repo)
        with mock.patch("subprocess.run") as mock_run:
            rc, out = _cli_capture(["--files", "foo.py", "--repo-root", str(repo)])
        d = json.loads(out)
        assert rc == 3
        assert d["exit_reason"] == "venv-interpreter-broken"
        assert d["interpreter_reason"] == "fallback"
        assert d["interpreter_problems"][0].startswith("dangling-link:")
        assert d["ran_pytest"] is False
        assert not _pytest_calls(mock_run)

    def test_healthy_parent_venv_is_used(self, tmp_path):
        repo = self._repo(tmp_path)
        _dangling_venv(repo)
        parent_py = _write_sh_interpreter(tmp_path / ".venv" / "bin" / "python")
        root = str((tmp_path / ".venv").resolve())

        def probe(candidate, code):
            return str(Path(candidate).parent.parent).startswith(root) or \
                str(Path(candidate).parent.parent.resolve()) == root

        with mock.patch.object(_at, "_probe_interpreter", side_effect=probe), \
                mock.patch("subprocess.run",
                           return_value=subprocess.CompletedProcess([], 0)) as mock_run:
            rc, out = _cli_capture(["--files", "foo.py", "--repo-root", str(repo)])
        d = json.loads(out)
        assert rc == 0
        assert d["interpreter_reason"] == "venv"
        assert Path(d["interpreter"]).samefile(parent_py)
        assert d["interpreter_problems"][0].startswith("dangling-link:")
        assert _pytest_calls(mock_run)

    def test_docs_only_unaffected_but_problem_listed(self, tmp_path):
        repo = self._repo(tmp_path)
        _dangling_venv(repo)
        rc, out = _cli_capture(["--files", "README.md", "--repo-root", str(repo)])
        d = json.loads(out)
        assert rc == 0
        assert d["exit_reason"] == "docs-only-no-selectors"
        assert d["interpreter_problems"][0].startswith("dangling-link:")

    def test_disable_probe_restores_old_path(self, tmp_path):
        repo = self._repo(tmp_path)
        _dangling_venv(repo)
        with mock.patch.dict(os.environ, {"QUOIN_DISABLE_VENV_PROBE": "1"}), \
                mock.patch("subprocess.run",
                           return_value=subprocess.CompletedProcess([], 0)):
            rc, out = _cli_capture(["--files", "foo.py", "--repo-root", str(repo)])
        d = json.loads(out)
        assert "interpreter_problems" not in d
        assert d["exit_reason"] != "venv-interpreter-broken"

    def test_healthy_venv_has_no_problems_key(self, tmp_path):
        repo = self._repo(tmp_path)
        _make_fake_venv(repo)
        with mock.patch.object(_at, "_probe_interpreter", return_value=True), \
                mock.patch("subprocess.run",
                           return_value=subprocess.CompletedProcess([], 0)):
            rc, out = _cli_capture(["--files", "foo.py", "--repo-root", str(repo)])
        assert rc == 0
        assert "interpreter_problems" not in json.loads(out)

    def test_nonexistent_quoin_python_with_healthy_venv(self, tmp_path):
        repo = self._repo(tmp_path)
        _make_fake_venv(repo)
        with mock.patch.dict(os.environ, {"QUOIN_PYTHON": str(tmp_path / "nope")}), \
                mock.patch.object(_at, "_probe_interpreter", return_value=True), \
                mock.patch("subprocess.run",
                           return_value=subprocess.CompletedProcess([], 0)):
            rc, out = _cli_capture(["--files", "foo.py", "--repo-root", str(repo)])
        d = json.loads(out)
        assert rc == 0
        assert d["interpreter_reason"] == "venv"
        assert d["interpreter_problems"][0].startswith("missing-interpreter:")

    def test_nonexistent_quoin_python_alone_is_not_blocking(self, tmp_path):
        repo = self._repo(tmp_path)
        with mock.patch.dict(os.environ, {"QUOIN_PYTHON": str(tmp_path / "nope")}), \
                mock.patch("subprocess.run",
                           return_value=subprocess.CompletedProcess([], 0)):
            rc, out = _cli_capture(["--files", "foo.py", "--repo-root", str(repo)])
        assert json.loads(out)["exit_reason"] != "venv-interpreter-broken"

    def test_broken_quoin_python_without_venv_blocks_with_its_own_remedy(
            self, tmp_path, capsys):
        repo = self._repo(tmp_path)
        bad = _write_sh_interpreter(tmp_path / "bad-python")
        bad.chmod(0o644)  # exists but not executable
        with mock.patch.dict(os.environ, {"QUOIN_PYTHON": str(bad)}), \
                mock.patch("subprocess.run") as mock_run:
            rc, out = _cli_capture(["--files", "foo.py", "--repo-root", str(repo)])
        d = json.loads(out)
        assert rc == 3
        assert d["exit_reason"] == "venv-interpreter-broken"
        assert "QUOIN_PYTHON" in d["interpreter_problems"][0] or \
            str(bad) in d["interpreter_problems"][0]
        err = capsys.readouterr().err
        assert "fix or unset QUOIN_PYTHON" in err
        assert "recreate the project venv" not in err
        assert not _pytest_calls(mock_run)

    def test_print_interpreter_lists_problems(self, tmp_path):
        _dangling_venv(tmp_path)
        rc, out = _cli_capture(["--print-interpreter", "--project-root", str(tmp_path)])
        assert rc == 0
        assert "interpreter_reason: fallback" in out
        assert "interpreter_problem: dangling-link:" in out

    def test_text_format_lists_problems(self, tmp_path):
        repo = self._repo(tmp_path)
        _dangling_venv(repo)
        rc, out = _cli_capture(["--files", "foo.py", "--repo-root", str(repo),
                                "--format", "text"])
        assert rc == 3
        assert "interpreter_problems (1): dangling-link:" in out


class TestNewDocRows:
    @pytest.mark.parametrize("src,expected", [
        ("quoin/adapters/claude/skills/gate/SKILL.md", "test_fullsuite_recipe_interpreter.py"),
        ("quoin/memory/autonomous-mode.md", "test_fullsuite_recipe_interpreter.py"),
        ("quoin/memory/autonomous-mode.md", "test_pytest_bound_matches_wait_budget.py"),
        ("quoin/adapters/claude/skills/review/SKILL.md", "test_review_fanout_ledger.py"),
        ("quoin/adapters/claude/skills/end_of_task/SKILL.md", "test_cost_ledger_summary_wiring.py"),
        ("quoin/adapters/claude/skills/run/SKILL.md", "test_cost_ledger_summary_wiring.py"),
        ("quoin/memory/cost-ledger-format.md", "test_cost_ledger_summary_wiring.py"),
        ("quoin/memory/dispatch-guide.md", "test_subprocess_timeout.py"),
    ])
    def test_new_doc_rows_select_new_tests(self, src, expected):
        repo = Path(__file__).resolve().parents[3]
        selectors, unmatched, _ignored = _at.map_changed_to_tests([src], repo)
        assert any(s.endswith(expected) for s in selectors), (src, expected)
        assert unmatched == []
