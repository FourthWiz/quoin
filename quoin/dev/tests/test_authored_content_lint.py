"""Unit tests for authored_content_lint.py.

Loaded via a sys.path insert + plain import (not spec_from_file_location) —
the module under test does a sibling-core import
(`from affected_tests import _resolve_base_branch`) that only resolves when
its own directory is already on sys.path, mirroring test_nested_root_check.py.

Fixtures build real tmp_path git repos and use synthetic tracker-shaped comment text (e.g. a line reading "T-04" or "D-02") to exercise the taxonomy.  quoin-lint: allow
"""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "core" / "scripts"))
import authored_content_lint as acl  # noqa: E402


# ---------------------------------------------------------------------------
# Git repo fixture helpers
# ---------------------------------------------------------------------------

def _git(repo, *args):
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result.stdout.strip()


def _init_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "initial commit")
    _git(repo, "switch", "-c", "feature")
    return repo


def _commit_all(repo, message="commit"):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)


def _run_scan(repo, basis, *, triage=False):
    base_ref = None
    if basis in ("union", "committed"):
        base_ref = acl._resolve_base_branch(str(repo))
        assert base_ref is not None
    return acl.scan(repo, basis, triage=triage, base_ref=base_ref)


# ---------------------------------------------------------------------------
# Loader-shape regression
# ---------------------------------------------------------------------------

def test_spec_from_file_location_fails_for_sibling_import():
    """Reproduces the failure the sys.path.insert loader avoids: loading via
    spec_from_file_location does not add the module's own directory to
    sys.path, so the sibling `affected_tests` import breaks.

    Runs in a fresh subprocess — the shared pytest process accumulates many
    other test modules' own sys.path.insert(core/scripts) calls over the
    course of a full-suite run, so an in-process repro is not hermetic.
    """
    core_path = Path(__file__).parent.parent.parent / "core" / "scripts" / "authored_content_lint.py"
    probe = (
        "import importlib.util\n"
        f"spec = importlib.util.spec_from_file_location('_acl_spec_probe', {str(core_path)!r})\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(mod)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True
    )
    assert result.returncode != 0
    assert "ModuleNotFoundError" in result.stderr
    assert "affected_tests" in result.stderr


def test_module_importable_via_sys_path_insert():
    assert hasattr(acl, "scan")
    assert hasattr(acl, "main")


# ---------------------------------------------------------------------------
# Tracker-prefix derivation — pure function
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "branch,expected",
    [
        ("feat/ivg-252-cleaner-prs-s2", {"IVG"}),
        ("main", set()),
        ("feat/utf-8-fix", set()),  # stoplist strips UTF
        ("chore/no-tracker-here", set()),
    ],
)
def test_derive_tracker_prefixes_pure(branch, expected):
    assert acl._derive_tracker_prefixes(branch) == expected


def test_resolve_tracker_prefixes_env_authoritative_when_set_empty(monkeypatch):
    monkeypatch.setenv("QUOIN_TRACKER_PREFIXES", "")
    assert acl.resolve_tracker_prefixes("feat/ivg-999-something") == set()


def test_resolve_tracker_prefixes_env_authoritative_when_set_nonempty(monkeypatch):
    monkeypatch.setenv("QUOIN_TRACKER_PREFIXES", "abc,xyz")
    assert acl.resolve_tracker_prefixes("feat/ivg-999-something") == {"ABC", "XYZ"}


def test_resolve_tracker_prefixes_falls_back_to_branch_when_env_unset(monkeypatch):
    monkeypatch.delenv("QUOIN_TRACKER_PREFIXES", raising=False)
    assert acl.resolve_tracker_prefixes("feat/ivg-252-cleaner-prs-s2") == {"IVG"}


# ---------------------------------------------------------------------------
# Taxonomy matching — one case per Tier A / Tier B family
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        "# T-04 handles the retry path",
        "# D-02 chose the simpler wrapper shape",
        "# R-07 covers the timeout case",
        "# CRIT-1 fix applied here",
        "# MAJ-3 fix applied here",
        "# MIN-1 fix applied here",
        "# AC-6 requires this branch",
        "# FR-9 requires this fallback",
        "# review round 2 changed this",
        "# critic round-3 changed this",
        "# see lessons-2026-04-13 for context",
        "# see lessons-learned for context",
        "# .workflow_artifacts holds the plan",
        "# cites current-plan.md directly",
        "# cites architecture.md directly",
        "# see critic-response-1.md",
        "# see review-2.md",
        "# see gate-implement-2026-08-01.md",
        "# see enriched-prompt.md",
    ],
)
def test_tier_a_unconditional_matches(text):
    assert acl._match_taxonomy(text, set())[0] == "A"


@pytest.mark.parametrize(
    "text",
    [
        "# gate_phase provenance guard (review round 1 MAJOR, IVG-249 S-03): ...",
        "# S-2 plan T-02 section — the combined gate is evaluated against this",
        "# CRIT-1 fix (D-05): subtract BOTH archived AND read_only ...",
        "# T-03 — load + validate (tomllib-or-internal)",
    ],
)
def test_tier_a_matches_real_world_examples(text):
    """Real comment lines pulled verbatim from this codebase's own history,
    each expected to match Tier A through at least one taxonomy pattern."""
    assert acl._match_taxonomy(text, set())[0] == "A"


@pytest.mark.parametrize(
    "text",
    [
        "# plan: MAJOR issue found here",
        "# critic: MINOR issue found here",
        "# gate: verdict recorded here",
        "# review: confidence 80 assigned",
        "# gate: PASS recorded here",
        "# critic: FAIL recorded here",
        "# review: REVISE requested",
        "# plan: round 3 changed this",
        "# gate: stage-2 wiring",
        "# critic: phase 4 review",
    ],
)
def test_tier_b_cue_gated_matches(text):
    assert acl._match_taxonomy(text, set())[0] == "B"


@pytest.mark.parametrize(
    "text",
    [
        "# retry up to 3 times, rounding to 2 decimals",
        "# stage 2 of the ETL pipeline writes the parquet shard",
        "# phase 1 of the boot sequence enables the watchdog",
        "validate_artifact.py:61   metasyntactic form AND a V-NN invariant ID on one line",
        '# Last activity: ISO-8601 from max mtime, or None if 0.0',
        "# ... a non-UTF-8 source file raises here, not OSError.",
    ],
)
def test_control_set_never_flagged(text):
    assert acl._match_taxonomy(text, set()) is None


def test_tracker_id_matches_when_prefix_resolved():
    assert acl._match_taxonomy("# see IVG-252 for background", {"IVG"})[0] == "A"


def test_tracker_id_not_matched_when_prefix_empty():
    assert acl._match_taxonomy("# see IVG-252 for background", set()) is None


# ---------------------------------------------------------------------------
# --triage superset predicate (non-circular from the taxonomy above)
# ---------------------------------------------------------------------------

def test_triage_predicate_matches_id_shape_and_cue_words():
    assert acl._is_triage_candidate("# see MAJ-04 for context")
    assert acl._is_triage_candidate("# the plan changed here")
    assert not acl._is_triage_candidate("# ordinary comment, nothing special")


def test_triage_predicate_id_shape_requires_two_letter_minimum():
    # Single-letter Tier A prefixes (T/D/R/F/Q/S) are below the triage probe's
    # 2-6 letter floor by design — the taxonomy catches them unconditionally
    # without needing the broad-recall probe to also flag them.
    assert not acl._is_triage_candidate("# see T-04 for context")


# ---------------------------------------------------------------------------
# Basis cases (architecture I-2)
# ---------------------------------------------------------------------------

def test_basis_union_uncommitted_change_found(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "mod.py").write_text("x = 1\n# T-04 uncommitted pollution\n", encoding="utf-8")
    result = _run_scan(repo, "union")
    assert any(f["file"] == "mod.py" and f["line"] == 2 for f in result["findings"])


def test_basis_union_untracked_file_found_at_whole_file_scope(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "new_mod.py").write_text("y = 2\n# D-02 untracked pollution\n", encoding="utf-8")
    result = _run_scan(repo, "union")
    assert any(f["file"] == "new_mod.py" and f["line"] == 2 for f in result["findings"])


def test_basis_union_committed_change_reported_at_true_worktree_line(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "mod.py").write_text("x = 1\n# R-07 committed pollution\n", encoding="utf-8")
    _commit_all(repo, "add mod.py")
    prefix = "\n".join(f"# padding line {i}" for i in range(5)) + "\n"
    text = (repo / "mod.py").read_text(encoding="utf-8")
    (repo / "mod.py").write_text(prefix + text, encoding="utf-8")
    result = _run_scan(repo, "union")
    assert any(f["file"] == "mod.py" and f["line"] == 7 for f in result["findings"])


def test_basis_committed_reports_head_blob_line_ignoring_worktree_edits(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "mod.py").write_text("x = 1\n# R-07 committed pollution\n", encoding="utf-8")
    _commit_all(repo, "add mod.py")
    prefix = "\n".join(f"# padding line {i}" for i in range(5)) + "\n"
    text = (repo / "mod.py").read_text(encoding="utf-8")
    (repo / "mod.py").write_text(prefix + text, encoding="utf-8")
    result = _run_scan(repo, "committed")
    assert any(f["file"] == "mod.py" and f["line"] == 2 for f in result["findings"])


def test_basis_committed_leading_blank_lines_report_true_line_number(tmp_path):
    """The HEAD blob read for committed basis must preserve leading blank
    lines exactly — trimming them would shift every extracted line number
    away from the diff's post-image frame."""
    repo = _init_repo(tmp_path)
    (repo / "mod.py").write_text("\n\n# T-04 padded pollution\n", encoding="utf-8")
    _commit_all(repo, "add mod.py")
    result = _run_scan(repo, "committed")
    assert any(f["file"] == "mod.py" and f["line"] == 3 for f in result["findings"])


# ---------------------------------------------------------------------------
# Whole-tree enumeration (gitignore regression guard)
# ---------------------------------------------------------------------------

def test_whole_tree_enumeration_excludes_gitignored_files(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "tracked.py").write_text("# T-04 tracked\n", encoding="utf-8")
    (repo / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    _commit_all(repo, "add tracked + gitignore")
    (repo / "ignored.py").write_text("# T-04 ignored, must never surface\n", encoding="utf-8")
    (repo / "untracked.py").write_text("# T-04 untracked not ignored\n", encoding="utf-8")

    result = acl.scan(repo, "whole-tree", triage=False, base_ref=None)
    files = {f["file"] for f in result["findings"]}
    assert "tracked.py" in files
    assert "untracked.py" in files
    assert "ignored.py" not in files


def test_whole_tree_file_count_matches_independent_ls_files_computation(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "a.py").write_text("# T-04\n", encoding="utf-8")
    (repo / "b.sh").write_text("# T-04\n", encoding="utf-8")
    (repo / "c.md").write_text("# T-04\n", encoding="utf-8")  # not scanned
    _commit_all(repo, "add files")
    (repo / "d.js").write_text("// T-04\n", encoding="utf-8")

    tracked = _git(repo, "ls-files").splitlines()
    untracked = _git(repo, "ls-files", "--others", "--exclude-standard").splitlines()
    scanned_suffixes = {".py", ".sh", ".zsh", ".ts", ".js", ".toml", ".yml", ".yaml"}
    expected = {f for f in set(tracked) | set(untracked) if Path(f).suffix in scanned_suffixes}

    result = acl.scan(repo, "whole-tree", triage=False, base_ref=None)
    assert result["files_scanned"] == len(expected)


def test_live_quoin_tree_whole_tree_file_count_matches_independent_computation():
    """Automated equivalent of the manual live-tree enumeration check documented
    in the detector's own module docstring."""
    repo_root = acl.resolve_repo_root(Path(__file__).parent)
    assert repo_root is not None
    tracked = _git(repo_root, "ls-files").splitlines()
    untracked = _git(repo_root, "ls-files", "--others", "--exclude-standard").splitlines()
    scanned_suffixes = {".py", ".sh", ".zsh", ".ts", ".js", ".toml", ".yml", ".yaml"}
    # Mirrors the detector's own structural exclusions independently — this is
    # the one deliberate narrowing on top of the raw git enumeration.
    excluded_paths = {
        "quoin/core/scripts/authored_content_lint.py",
        "quoin/scripts/authored_content_lint.py",
        "quoin/core/scripts/comment_cleanup.py",
        "quoin/scripts/comment_cleanup.py",
    }
    expected = {
        f
        for f in set(tracked) | set(untracked)
        if Path(f).suffix in scanned_suffixes
        and f not in excluded_paths
        and ".workflow_artifacts/" not in f
        and "testdata/" not in f
    }

    result = acl.scan(repo_root, "whole-tree", triage=True, base_ref=None)
    assert result["files_scanned"] == len(expected)


# ---------------------------------------------------------------------------
# Tracker asymmetry — by construction, no commit-text input at all
# ---------------------------------------------------------------------------

def test_tracker_id_in_comment_flagged_but_commit_message_never_read(tmp_path, monkeypatch):
    monkeypatch.setenv("QUOIN_TRACKER_PREFIXES", "IVG")
    repo = _init_repo(tmp_path)
    (repo / "mod.py").write_text("x = 1\n# IVG-252 in a comment\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "IVG-252 mentioned only in the commit message")
    result = _run_scan(repo, "committed")
    assert any(f["token"] == "IVG-252" for f in result["findings"])
    # The commit message text itself is never inspected — scan() only ever
    # reads `git show HEAD:{path}` blob content, never `git log` output.
    assert not any("commit message" in str(f) for f in result["findings"])


# ---------------------------------------------------------------------------
# Pragma suppression + load-bearing regression
# ---------------------------------------------------------------------------

def test_pragma_suppresses_a_tier_a_line(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "mod.py").write_text(
        "x = 1\n# T-04 discussed as an example here  quoin-lint: allow\n",
        encoding="utf-8",
    )
    result = _run_scan(repo, "union")
    assert not result["findings"]


def test_pragma_strip_regression_on_this_test_modules_own_docstring():
    """Strips this file's own load-bearing pragma line in a scratch copy and
    asserts a finding now appears — proving the pragma is load-bearing rather
    than decorative."""
    src = Path(__file__)
    text = src.read_text(encoding="utf-8")
    assert "quoin-lint: allow" in text
    mutated = text.replace("quoin-lint: allow\n", "\n", 1)
    assert "quoin-lint: allow" not in mutated or mutated.count("quoin-lint: allow") < text.count(
        "quoin-lint: allow"
    )

    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir) / "repo"
        repo.mkdir()
        _git(repo, "init", "-b", "main")
        _git(repo, "config", "user.email", "test@example.com")
        _git(repo, "config", "user.name", "Test")
        (repo / "README.md").write_text("init\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "initial commit")
        _git(repo, "switch", "-c", "feature")
        (repo / "mutated_test.py").write_text(mutated, encoding="utf-8")
        result = _run_scan(repo, "union")
        assert result["findings"], "expected the stripped pragma to unmask a finding"


# ---------------------------------------------------------------------------
# Fail-OPEN matrix
# ---------------------------------------------------------------------------

def test_non_git_tree_exit_3(tmp_path):
    not_a_repo = tmp_path / "plain_dir"
    not_a_repo.mkdir()
    rc = acl.main(["--basis", "whole-tree", "--project-root", str(not_a_repo)])
    assert rc == 3


def test_missing_base_branch_exit_3(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "onlybranch")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "f.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "only commit, no main/master to diff against")
    rc = acl.main(["--basis", "union", "--repo", str(repo)])
    assert rc == 3


def test_unparseable_python_source_exit_3(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "broken.py").write_text("def f(:\n    pass\n", encoding="utf-8")
    rc = acl.main(["--basis", "whole-tree", "--repo", str(repo)])
    assert rc == 3


def test_disable_env_exit_0(monkeypatch, capsys):
    monkeypatch.setenv("QUOIN_DISABLE_AUTHORED_CONTENT_LINT", "1")
    rc = acl.main(["--basis", "whole-tree"])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"disabled": true' in out


def test_triage_requires_whole_tree_basis(tmp_path, capsys):
    repo = _init_repo(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        acl.main(["--basis", "union", "--triage", "--repo", str(repo)])
    assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# Scope boundary: .workflow_artifacts/ content -> zero findings  quoin-lint: allow
# ---------------------------------------------------------------------------

def test_workflow_artifacts_content_never_flagged(tmp_path):
    repo = _init_repo(tmp_path)
    wa = repo / ".workflow_artifacts" / "some-task"
    wa.mkdir(parents=True)
    fabricated = wa / "helper.py"
    fabricated.write_text("# T-04 fabricated pollution inside the artifacts tree\n", encoding="utf-8")
    _commit_all(repo, "add artifacts tree")
    result = _run_scan(repo, "committed")
    assert not result["findings"]


# ---------------------------------------------------------------------------
# Self-scan: the detector, its wrapper, and its own test files
# ---------------------------------------------------------------------------

def test_self_scan_zero_findings():
    repo_root = acl.resolve_repo_root(Path(__file__).parent)
    assert repo_root is not None
    base_ref = acl._resolve_base_branch(str(repo_root))
    basis = "committed" if base_ref is not None else "whole-tree"
    result = acl.scan(repo_root, basis, triage=False, base_ref=base_ref)
    own_files = {
        "quoin/core/scripts/authored_content_lint.py",
        "quoin/scripts/authored_content_lint.py",
        "quoin/dev/tests/test_authored_content_lint.py",
        "quoin/dev/tests/test_authored_content_lint_wiring.py",
        "quoin/dev/tests/test_install_authored_content_lint_deployed.py",
    }
    own_findings = [f for f in result["findings"] if f["file"] in own_files]
    assert not own_findings, own_findings


# ---------------------------------------------------------------------------
# T-01 promotion (IVG-255): public names are aliases, not copies
# ---------------------------------------------------------------------------

def test_promoted_public_names_are_identical_objects_to_private_aliases():
    assert acl.resolve_candidates is acl._resolve_candidates
    assert acl.read_text_source is acl._read_text_source
    assert acl.match_taxonomy is acl._match_taxonomy
    assert acl.Undeterminable is acl._Undeterminable


def test_private_aliases_retained():
    assert hasattr(acl, "_resolve_candidates")
    assert hasattr(acl, "_read_text_source")
    assert hasattr(acl, "_match_taxonomy")
    assert hasattr(acl, "_Undeterminable")


def test_exclude_paths_contains_both_comment_cleanup_paths():
    assert "quoin/core/scripts/comment_cleanup.py" in acl._EXCLUDE_PATHS
    assert "quoin/scripts/comment_cleanup.py" in acl._EXCLUDE_PATHS
