"""
Unit tests for path_resolve.py — 22 deterministic cases, no LLM, no subprocess
except case (p/q/r) which call the CLI.

All cases use the T-01 fixture corpus under fixtures/path_resolve/.
"""

import ast
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# Add core scripts dir so `from path_resolve import ...` tests the portable implementation.
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "core" / "scripts"))
from path_resolve import (
    task_path,
    _lookup_stage_by_name,
    resolve_artifact_root,
    _find_self_or_ancestor_root,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "path_resolve"
SCRIPT_PATH = Path(__file__).parent.parent.parent / "core" / "scripts" / "path_resolve.py"

_ENV_ARTIFACT_ROOT = "QUOIN_ARTIFACT_ROOT"


# T-05 MIN-1: autouse, module-scoped scrub so EVERY test in this file (not just the
# new marker cases) is protected from an ambient QUOIN_ARTIFACT_ROOT. T-02 rewires
# --print-project-root to resolve_artifact_root, which makes the env override
# highest-precedence — the four pre-existing test_print_project_root_* subprocess
# tests are newly env-sensitive. Because those are SUBPROCESS tests, scrubbing the
# actual process env (which subprocess.run inherits by default) covers them too.
@pytest.fixture(autouse=True, scope="function")
def _scrub_artifact_root_env(monkeypatch):
    monkeypatch.delenv(_ENV_ARTIFACT_ROOT, raising=False)


# ---------------------------------------------------------------------------
# Shared corpus fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def corpus(tmp_path, request):
    """Copy the named fixture subdir into tmp_path/.workflow_artifacts/ for isolation.

    The resolver expects project_root/.workflow_artifacts/<task>/ so we mount the
    fixture tree under tmp_path/.workflow_artifacts/ and return tmp_path as the
    project_root (the 'corpus' variable used by tests as project_root).
    """
    subdir_name = request.param
    src = FIXTURES_DIR / subdir_name
    dst = tmp_path / ".workflow_artifacts"
    # Copy the fixture's task-* subdirs into tmp_path/.workflow_artifacts/
    shutil.copytree(str(src), str(dst), dirs_exist_ok=True)
    return tmp_path


# ---------------------------------------------------------------------------
# Test cases (22 total)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("corpus", ["legacy"], indirect=True)
def test_legacy_default_returns_task_root(corpus):
    """Rule 3: stage=None → task root even when arch.md exists."""
    result = task_path("task-a", project_root=corpus)
    assert result == corpus / ".workflow_artifacts" / "task-a"


@pytest.mark.parametrize("corpus", ["legacy"], indirect=True)
def test_legacy_explicit_int_returns_stage_path_even_when_absent(corpus):
    """Rule 1: explicit int returns path even when stage dir doesn't exist."""
    result = task_path("task-a", stage=1, project_root=corpus)
    assert result == corpus / ".workflow_artifacts" / "task-a" / "stage-1"
    # Directory must NOT exist on disk (caller's job to mkdir)
    assert not result.exists()


@pytest.mark.parametrize("corpus", ["multi-stage"], indirect=True)
def test_multi_stage_explicit_int(corpus):
    """Rule 1: explicit int → stage-2/ directory."""
    result = task_path("task-b", stage=2, project_root=corpus)
    assert result == corpus / ".workflow_artifacts" / "task-b" / "stage-2"


@pytest.mark.parametrize("corpus", ["multi-stage"], indirect=True)
def test_multi_stage_default_returns_task_root(corpus):
    """Rule 3 (I-05 grandfathering): stage=None → task root even with arch+decomp."""
    result = task_path("task-b", project_root=corpus)
    assert result == corpus / ".workflow_artifacts" / "task-b"


@pytest.mark.parametrize("corpus", ["multi-stage"], indirect=True)
def test_multi_stage_name_lookup_first_token(corpus):
    """Rule 2: exact stage name match via architecture.md decomposition."""
    result = task_path("task-b", stage="stage-two-name", project_root=corpus)
    assert result == corpus / ".workflow_artifacts" / "task-b" / "stage-2"


@pytest.mark.parametrize("corpus", ["multi-stage"], indirect=True)
def test_multi_stage_name_lookup_normalized(corpus):
    """Rule 2: underscores normalized to spaces for lookup."""
    result = task_path("task-b", stage="stage_two_name", project_root=corpus)
    assert result == corpus / ".workflow_artifacts" / "task-b" / "stage-2"


@pytest.mark.parametrize("corpus", ["multi-stage"], indirect=True)
def test_multi_stage_name_lookup_substring(corpus):
    """Rule 2: substring match — 'two' matches 'stage-two-name'."""
    result = task_path("task-b", stage="two", project_root=corpus)
    assert result == corpus / ".workflow_artifacts" / "task-b" / "stage-2"


@pytest.mark.parametrize("corpus", ["multi-stage"], indirect=True)
def test_multi_stage_name_lookup_miss_raises(corpus):
    """Rule 2b: stage name not found → ValueError."""
    with pytest.raises(ValueError, match="not found in architecture.md"):
        task_path("task-b", stage="nonexistent-stage", project_root=corpus)


@pytest.mark.parametrize("corpus", ["mixed-with-decomp-only"], indirect=True)
def test_mixed_layout_default_returns_root_per_I05(corpus):
    """Rule 3 (I-05): stage=None → root even when stage-1/ folder exists."""
    result = task_path("task-c", project_root=corpus)
    assert result == corpus / ".workflow_artifacts" / "task-c"


@pytest.mark.parametrize("corpus", ["no-arch"], indirect=True)
def test_no_arch_default_returns_root(corpus):
    """Rule 3: no architecture.md → task root without error."""
    result = task_path("task-d", project_root=corpus)
    assert result == corpus / ".workflow_artifacts" / "task-d"


@pytest.mark.parametrize("corpus", ["no-arch"], indirect=True)
def test_no_arch_name_lookup_raises(corpus):
    """Rule 2a: stage str + no architecture.md → ValueError."""
    with pytest.raises(ValueError, match="architecture.md missing"):
        task_path("task-d", stage="anything", project_root=corpus)


@pytest.mark.parametrize("corpus", ["decomp-only"], indirect=True)
def test_decomp_only_name_lookup_constructs_absent_path(corpus):
    """Rule 2: resolver constructs path even when stage-1/ doesn't exist."""
    result = task_path("task-e", stage="stage-one-name", project_root=corpus)
    assert result == corpus / ".workflow_artifacts" / "task-e" / "stage-1"
    # The stage-1 directory must NOT exist on disk
    assert not result.exists()


def test_explicit_int_zero_raises():
    """Rule 1 defensive: stage=0 raises ValueError."""
    with pytest.raises(ValueError, match="must be >= 1"):
        task_path("task-a", stage=0)


def test_explicit_int_negative_raises():
    """Rule 1 defensive: stage=-1 raises ValueError."""
    with pytest.raises(ValueError, match="must be >= 1"):
        task_path("task-a", stage=-1)


def test_module_imports_stdlib_only():
    """T-04 case (o): assert no non-stdlib imports in path_resolve.py."""
    # os/json added for the IVG-158 S-02 marker-aware resolve_artifact_root() branch
    # (env override + marker JSON parse) — both stdlib.
    allowed = {"pathlib", "re", "argparse", "sys", "os", "json"}
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                assert top in allowed, (
                    f"path_resolve.py imports non-stdlib module '{alias.name}'. "
                    f"Only stdlib imports allowed: {sorted(allowed)}"
                )
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                top = node.module.split(".")[0]
                assert top in allowed, (
                    f"path_resolve.py imports non-stdlib module '{node.module}'. "
                    f"Only stdlib imports allowed: {sorted(allowed)}"
                )


@pytest.mark.parametrize("corpus", ["legacy"], indirect=True)
def test_cli_default_prints_root(corpus):
    """CLI: no --stage prints task root path."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--task", "task-a",
         "--project-root", str(corpus)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    expected = str(corpus / ".workflow_artifacts" / "task-a")
    assert result.stdout.strip() == expected


@pytest.mark.parametrize("corpus", ["legacy"], indirect=True)
def test_cli_explicit_int(corpus):
    """CLI: --stage 1 prints stage-1 path."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--task", "task-a",
         "--stage", "1", "--project-root", str(corpus)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    expected = str(corpus / ".workflow_artifacts" / "task-a" / "stage-1")
    assert result.stdout.strip() == expected


@pytest.mark.parametrize("corpus", ["multi-stage"], indirect=True)
def test_cli_name_miss_exits_2(corpus):
    """CLI: bad stage name → exit 2 + 'not found' in stderr."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--task", "task-b",
         "--stage", "nonexistent", "--project-root", str(corpus)],
        capture_output=True, text=True,
    )
    assert result.returncode == 2
    assert "not found" in result.stderr


def test_inflight_task_grandfathering_real_repo():
    """T-04 case (s): optional live filesystem snapshot for local in-flight tasks.

    This check is strict when the developer's local `.workflow_artifacts/`
    snapshot exists. Fresh clones and cleaned worktrees do not carry those
    untracked task folders, so they should not fail the deterministic resolver
    suite.
    """
    repo_root = Path(__file__).parents[3]
    snapshot_file = FIXTURES_DIR / "_inflight-snapshot.txt"
    assert snapshot_file.exists(), f"Snapshot file missing: {snapshot_file}"

    rows = [
        line.strip()
        for line in snapshot_file.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]

    missing_tasks = []
    for row in rows:
        parts = [p.strip() for p in row.split("|")]
        assert len(parts) == 4, f"Bad snapshot row: {row!r}"
        name = parts[0]
        task_folder = repo_root / ".workflow_artifacts" / name
        if not task_folder.exists():
            missing_tasks.append(name)

    if missing_tasks:
        pytest.skip(
            "Local in-flight workflow snapshot is absent for task(s): "
            + ", ".join(sorted(missing_tasks))
            + ". This optional live-state check only runs when the untracked "
            ".workflow_artifacts task folders are present."
        )

    for row in rows:
        parts = [p.strip() for p in row.split("|")]
        assert len(parts) == 4, f"Bad snapshot row: {row!r}"
        name, arch_status, plan_status, stage_list = parts
        task_folder = repo_root / ".workflow_artifacts" / name

        # Verify architecture.md status
        arch_md = task_folder / "architecture.md"
        live_arch_status: str
        if not arch_md.exists():
            live_arch_status = "absent"
        else:
            has_decomp = "## Stage decomposition" in arch_md.read_text(encoding="utf-8")
            live_arch_status = "present-with-decomp" if has_decomp else "present-without-decomp"

        assert live_arch_status == arch_status, (
            f"In-flight task '{name}' snapshot mismatch: expected arch_status "
            f"'{arch_status}' but found '{live_arch_status}'. "
            f"Was this task finalized? If so, REMOVE this row from _inflight-snapshot.txt "
            f"— do NOT mask the regression with a silent skip."
        )

        # Verify current-plan.md status
        plan_md = task_folder / "current-plan.md"
        live_plan_status = "present" if plan_md.exists() else "absent"
        assert live_plan_status == plan_status, (
            f"In-flight task '{name}' snapshot mismatch: expected plan_status "
            f"'{plan_status}' but found '{live_plan_status}'."
        )

        # Verify stage folders
        live_stages = sorted(
            p.name for p in task_folder.iterdir()
            if p.is_dir() and p.name.startswith("stage-")
        )
        if stage_list == "(none)":
            expected_stages: list = []
        else:
            expected_stages = sorted(stage_list.split(","))

        live_stage_str = ",".join(live_stages) if live_stages else "(none)"
        expected_stage_str = ",".join(expected_stages) if expected_stages else "(none)"
        assert live_stage_str == expected_stage_str, (
            f"In-flight task '{name}' snapshot mismatch: expected stages "
            f"'{expected_stage_str}' but found '{live_stage_str}'."
        )

        # Load-bearing R-09 / I-05 assertion: rule-3 default must return task root
        resolved = task_path(name, project_root=repo_root)
        assert resolved == repo_root / ".workflow_artifacts" / name, (
            f"task_path('{name}') returned '{resolved}', expected task root. "
            f"Rule-3 OPT-IN grandfathering broken."
        )


@pytest.mark.parametrize("corpus", ["arch-no-decomp"], indirect=True)
def test_explicit_arch_no_decomp_default_returns_root(corpus):
    """Production-shape: arch.md without decomp → task root (caveman shape)."""
    result = task_path("task-f", project_root=corpus)
    assert result == corpus / ".workflow_artifacts" / "task-f"


@pytest.mark.parametrize("corpus", ["arch-absent-with-stage-folder"], indirect=True)
def test_arch_absent_with_stage_folder_default_returns_root(corpus):
    """Production-shape: no arch.md + stage-5/ present → task root NOT stage-5/."""
    result = task_path("task-g", project_root=corpus)
    assert result == corpus / ".workflow_artifacts" / "task-g"


def test_substring_multimatch_raises(tmp_path):
    """T-04 case (v): multi-match on stage name → ValueError with diagnostics."""
    # Build a synthetic fixture with two stages sharing 'data' as substring
    arch_dir = tmp_path / ".workflow_artifacts" / "task-x"
    arch_dir.mkdir(parents=True)
    arch_file = arch_dir / "architecture.md"
    arch_file.write_text(
        "---\ntask: task-x\n---\n"
        "## Stage decomposition\n\n"
        "1. ⏳ S-01: data-migration\n"
        "2. ⏳ S-02: data-cleanup\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as exc_info:
        task_path("task-x", stage="data", project_root=tmp_path)

    msg = str(exc_info.value)
    assert "matches 2 stages" in msg, f"Expected 'matches 2 stages' in: {msg}"
    assert "disambiguate by using --stage <integer>" in msg, (
        f"Expected disambiguation hint in: {msg}"
    )


def test_cli_verify_root_not_nested(tmp_path):
    """--verify-root exits 0 when project root has no ancestor with .workflow_artifacts/."""
    project = tmp_path / "project"
    project.mkdir()
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--task", "foo",
         "--project-root", str(project), "--verify-root"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f"Expected exit 0 (no nesting), got {result.returncode}; stderr={result.stderr!r}"
    )
# Validates absence of false positives only. That the guard exists is validated by test_cli_verify_root_nested.


def test_cli_verify_root_nested(tmp_path):
    """--verify-root exits 3 when an ancestor directory contains .workflow_artifacts/."""
    (tmp_path / ".workflow_artifacts").mkdir()   # ancestor owns artifact tree
    subproject = tmp_path / "subproject"
    subproject.mkdir()
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--task", "foo",
         "--project-root", str(subproject), "--verify-root"],
        capture_output=True, text=True,
    )
    assert result.returncode == 3, (
        f"Expected exit 3 (nested root), got {result.returncode}; stderr={result.stderr!r}"
    )
    assert ".workflow_artifacts" in result.stderr, (
        f"Expected ancestor path in stderr, got: {result.stderr!r}"
    )
# FAILS if --verify-root is missing (returncode 0) or check not implemented (returncode 0).


def test_cli_verify_root_no_flag_ignores_nesting(tmp_path):
    """Without --verify-root, nested root is silently accepted (no exit 3)."""
    (tmp_path / ".workflow_artifacts").mkdir()
    subproject = tmp_path / "subproject"
    subproject.mkdir()
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--task", "foo",
         "--project-root", str(subproject)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f"Expected exit 0 without flag, got {result.returncode}; stderr={result.stderr!r}"
    )
# FAILS if check fires unconditionally (breaks existing callers that don't pass --verify-root).


def test_task_root_resolves_without_spec_md(tmp_path):
    """R-09 grandfather canary (specify-skill stage 1, T-07): a task directory with
    no spec.md must resolve cleanly — task_path() never enumerates artifact
    filenames, so absence of spec.md cannot raise or otherwise affect resolution."""
    task_dir = tmp_path / ".workflow_artifacts" / "legacy-task"
    task_dir.mkdir(parents=True)

    result = task_path("legacy-task", stage=None, project_root=tmp_path)

    assert result == task_dir
    assert not (task_dir / "spec.md").exists()


# ---------------------------------------------------------------------------
# T-09 — --print-project-root mode (IVG-119)
# ---------------------------------------------------------------------------


def test_print_project_root_alone_exit0_one_path(tmp_path):
    """MAJ-1: --print-project-root ALONE → exit 0 + exactly one stdout path line
    (NOT exit 2, NOT empty)."""
    (tmp_path / ".workflow_artifacts").mkdir()
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--print-project-root",
         "--start", str(tmp_path)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"stderr={result.stderr!r}"
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    assert len(lines) == 1, f"expected one path line, got {lines!r}"
    assert Path(lines[0]) == tmp_path.resolve()


def test_print_project_root_spaces_in_path(tmp_path):
    """A start dir whose path contains spaces → single correct path, no argparse error."""
    spaced = tmp_path / "sp ace dir"
    (spaced / ".workflow_artifacts").mkdir(parents=True)
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--print-project-root",
         "--start", str(spaced)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"stderr={result.stderr!r}"
    assert result.stdout.strip() == str(spaced.resolve())
    assert "error" not in result.stderr.lower()


def test_print_project_root_from_nested_warns_on_stderr(tmp_path):
    """From INSIDE a nested root: stderr carries a WARN, stdout stays a clean path."""
    (tmp_path / ".workflow_artifacts").mkdir()
    nested = tmp_path / "subproject"
    (nested / ".workflow_artifacts").mkdir(parents=True)
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--print-project-root",
         "--start", str(nested)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == str(nested.resolve())
    assert "WARN" in result.stderr and ".workflow_artifacts" in result.stderr


def test_print_project_root_walks_up_to_ancestor(tmp_path):
    """--start below the root walks up to the nearest .workflow_artifacts ancestor."""
    (tmp_path / ".workflow_artifacts").mkdir()
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--print-project-root",
         "--start", str(deep)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == str(tmp_path.resolve())


def test_neither_task_nor_print_root_exits_2(tmp_path):
    """Regression: neither --task nor --print-project-root → exit 2, empty stdout."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH)],
        capture_output=True, text=True, cwd=str(tmp_path),
    )
    assert result.returncode == 2
    assert result.stdout.strip() == ""


def test_task_still_resolves_without_print_flag(tmp_path):
    """Regression: --task foo (no --print-project-root) resolves as before."""
    (tmp_path / ".workflow_artifacts" / "foo").mkdir(parents=True)
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--task", "foo",
         "--project-root", str(tmp_path)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == str(tmp_path.resolve() / ".workflow_artifacts" / "foo")


# ---------------------------------------------------------------------------
# T-05 — resolve_artifact_root() marker-branch tests (IVG-158 S-02, R-01 mitigation)
# ---------------------------------------------------------------------------


def _write_marker(marker_path: Path, artifact_root, feature="feat", repos=None):
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(
        json.dumps({
            "artifact_root": str(artifact_root),
            "feature": feature,
            "repos": repos or ["repoA"],
            "created": "2026-07-30T00:00:00Z",
        }),
        encoding="utf-8",
    )


def test_resolve_artifact_root_byte_identity_no_marker(tmp_path):
    """R-01 GUARD: with no marker anywhere and no env var, resolve_artifact_root
    must return EXACTLY what the untouched _find_self_or_ancestor_root oracle
    returns, across a corpus of start-dir shapes. Proves the no-marker path is
    byte-identical to today, not eyeballed."""
    # (i) start dir IS a project root
    root = tmp_path / "root_is_start"
    (root / ".workflow_artifacts").mkdir(parents=True)
    assert resolve_artifact_root(root) == _find_self_or_ancestor_root(root)

    # (ii) deep descendant under a project root
    deep_root = tmp_path / "deep_root"
    (deep_root / ".workflow_artifacts").mkdir(parents=True)
    deep_start = deep_root / "a" / "b" / "c"
    deep_start.mkdir(parents=True)
    assert resolve_artifact_root(deep_start) == _find_self_or_ancestor_root(deep_start)

    # (iii) no .workflow_artifacts ancestor at all — fallback-to-start
    no_root_start = tmp_path / "no_root_here"
    no_root_start.mkdir(parents=True)
    assert resolve_artifact_root(no_root_start) == _find_self_or_ancestor_root(no_root_start)


def test_resolve_artifact_root_from_inside_workspace(tmp_path):
    """POSITIVE, DISCRIMINATING (MAJ-1): the marker's artifact_root points at a
    DISTINCT valid root (rootB) that plain walk-up would NEVER return — walk-up
    from inside the workspace halts at rootA. Asserting == rootB (not rootA)
    fails if the marker-honor branch is removed."""
    rootA = tmp_path / "rootA"
    (rootA / ".workflow_artifacts").mkdir(parents=True)
    workspace_repo = rootA / ".workspaces" / "feat" / "repoA"
    workspace_repo.mkdir(parents=True)

    rootB = tmp_path / "rootB"
    (rootB / ".workflow_artifacts").mkdir(parents=True)

    marker = rootA / ".workspaces" / "feat" / ".quoin-workspace.json"
    _write_marker(marker, rootB.resolve())

    result = resolve_artifact_root(workspace_repo)
    assert result == rootB.resolve()
    assert result != rootA.resolve()


def test_resolve_artifact_root_invalid_marker_falls_through(tmp_path):
    """GUARD: a marker whose artifact_root lacks .workflow_artifacts/ is IGNORED —
    resolver falls through to the real canonical walk-up root (rootA), not the bad
    artifact_root, and does not raise."""
    rootA = tmp_path / "rootA"
    (rootA / ".workflow_artifacts").mkdir(parents=True)
    workspace_repo = rootA / ".workspaces" / "feat" / "repoA"
    workspace_repo.mkdir(parents=True)

    bad_target = tmp_path / "no_workflow_artifacts_here"
    bad_target.mkdir()

    marker = rootA / ".workspaces" / "feat" / ".quoin-workspace.json"
    _write_marker(marker, bad_target.resolve())

    result = resolve_artifact_root(workspace_repo)
    assert result == rootA.resolve()


def test_resolve_artifact_root_corrupt_marker_fail_open(tmp_path):
    """GUARD: a marker that is not valid JSON does not raise — fail-OPEN to the
    walk-up root."""
    rootA = tmp_path / "rootA"
    (rootA / ".workflow_artifacts").mkdir(parents=True)
    workspace_repo = rootA / ".workspaces" / "feat" / "repoA"
    workspace_repo.mkdir(parents=True)

    marker = rootA / ".workspaces" / "feat" / ".quoin-workspace.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("{ broken", encoding="utf-8")

    result = resolve_artifact_root(workspace_repo)
    assert result == rootA.resolve()


@pytest.mark.parametrize(
    "marker_body",
    [
        "[]",       # empty JSON array
        "[1, 2]",   # non-empty JSON array
        "42",       # JSON number
        '"str"',    # JSON string
        "null",     # JSON null
    ],
)
def test_resolve_artifact_root_nonobject_marker_fail_open(tmp_path, marker_body):
    """GUARD (MAJ-1, DISCRIMINATING): a marker whose body is VALID JSON but is NOT
    an object (array / number / string / null) must NOT raise AttributeError from
    the internal ``data.get(...)`` — it must fail-OPEN to the walk-up root exactly
    like a corrupt marker. Pre-fix, ``json.loads`` succeeds for these bodies, then
    ``data.get("artifact_root")`` raises an uncaught AttributeError that propagates
    out of resolve_artifact_root and crashes --print-project-root. The isinstance
    gate makes each of these fall through to rootA."""
    rootA = tmp_path / "rootA"
    (rootA / ".workflow_artifacts").mkdir(parents=True)
    workspace_repo = rootA / ".workspaces" / "feat" / "repoA"
    workspace_repo.mkdir(parents=True)

    marker = rootA / ".workspaces" / "feat" / ".quoin-workspace.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(marker_body, encoding="utf-8")

    result = resolve_artifact_root(workspace_repo)
    assert result == rootA.resolve()


def test_resolve_artifact_root_env_override(tmp_path, monkeypatch):
    """QUOIN_ARTIFACT_ROOT, when set and valid, wins over the walk even from an
    unrelated start dir."""
    env_root = tmp_path / "env_root"
    (env_root / ".workflow_artifacts").mkdir(parents=True)

    unrelated_start = tmp_path / "unrelated"
    (unrelated_start / ".workflow_artifacts").mkdir(parents=True)

    monkeypatch.setenv(_ENV_ARTIFACT_ROOT, str(env_root))
    result = resolve_artifact_root(unrelated_start)
    assert result == env_root.resolve()


def test_resolve_artifact_root_env_invalid_ignored(tmp_path, monkeypatch):
    """QUOIN_ARTIFACT_ROOT set to a dir WITHOUT .workflow_artifacts/ is ignored —
    the walk result is returned instead."""
    invalid_env_root = tmp_path / "invalid_env_root"
    invalid_env_root.mkdir()

    real_root = tmp_path / "real_root"
    (real_root / ".workflow_artifacts").mkdir(parents=True)

    monkeypatch.setenv(_ENV_ARTIFACT_ROOT, str(invalid_env_root))
    result = resolve_artifact_root(real_root)
    assert result == real_root.resolve()


def test_cli_print_project_root_from_workspace(tmp_path):
    """CLI parity, DISCRIMINATING (MAJ-1): --print-project-root from inside a
    workspace prints the marker's target (rootB), NOT the walk-up target (rootA).
    Env is explicitly cleared from the subprocess so an ambient value cannot
    hijack the result."""
    rootA = tmp_path / "rootA"
    (rootA / ".workflow_artifacts").mkdir(parents=True)
    workspace_repo = rootA / ".workspaces" / "feat" / "repoA"
    workspace_repo.mkdir(parents=True)

    rootB = tmp_path / "rootB"
    (rootB / ".workflow_artifacts").mkdir(parents=True)

    marker = rootA / ".workspaces" / "feat" / ".quoin-workspace.json"
    _write_marker(marker, rootB.resolve())

    clean_env = {k: v for k, v in os.environ.items() if k != _ENV_ARTIFACT_ROOT}
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--print-project-root",
         "--start", str(workspace_repo)],
        capture_output=True, text=True, env=clean_env,
    )
    assert result.returncode == 0, f"stderr={result.stderr!r}"
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    assert len(lines) == 1
    assert Path(lines[0]) == rootB.resolve()


def test_light_run_stage_template_resolves_by_name():
    """The architecture.md template in the light /run skill must stay parseable."""
    skill = (Path(__file__).parent.parent.parent / "adapters" / "claude" / "skills"
             / "run" / "SKILL.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```markdown\n(.*?)```", skill, re.DOTALL)
    template = next(b for b in blocks if "## Stage decomposition" in b)
    names = iter(["add billing schema", "wire billing api"])
    arch = re.sub(r"<short stage name>", lambda _m: next(names), template)
    assert _lookup_stage_by_name(arch, "billing schema") == 1
    assert _lookup_stage_by_name(arch, "wire-billing") == 2
