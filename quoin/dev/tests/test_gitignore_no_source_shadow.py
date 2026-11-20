"""CI guard: no source files under quoin/dev/ may be shadowed by a .gitignore rule.

Real harm: a shadowed file was never committed, so it is absent from every clean
checkout and CI runner — even though pytest collects it locally (filesystem walk
ignores .gitignore). This guard catches the regression on the machines where the
suite runs; see R-03 in the IVG-49 plan for the CI coverage gap note.

Detection mechanism: ``git ls-files --others --ignored --exclude-standard``
  SOLE required mechanism — ``git status --ignored --porcelain`` is PROHIBITED
  (it collapses a fully-ignored directory to a single ``!! dev/`` entry with no
  ``.py`` suffix, producing a false-negative on the exact whole-subtree regression
  this guard exists to catch — confirmed live IVG-49 2026-05-30).

Extension list: intentionally ``quoin/dev/``-centric (.py .md .json .sh .txt
.yaml .yml .toml). This is NOT a universal list; document and widen if needed.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

# Repo root: quoin/dev/tests/<this file> → parents[3] = quoin/ (the git root)
REPO_ROOT = Path(__file__).resolve().parents[3]

# Source-like extensions for quoin/dev/ — intentionally narrow (see module docstring)
_SOURCE_EXTS = {".py", ".md", ".json", ".sh", ".txt", ".yaml", ".yml", ".toml"}

# Paths to drop even if they have source extensions
_IGNORE_SUBSTRINGS = ("/__pycache__/", ".pytest_cache/", ".DS_Store", "._")
_IGNORE_SUFFIXES = (".pyc", ".pyo", ".pyd")


def find_shadowed_sources(git_root: Path, subtree: str = "quoin/dev") -> list[str]:
    """Return a list of source-like files under *subtree* that are git-ignored.

    Uses ``git ls-files --others --ignored --exclude-standard`` as the SOLE
    detection mechanism.  ``git status --ignored --porcelain`` is explicitly
    prohibited (see module docstring).

    Args:
        git_root: Absolute path to the git repository root.
        subtree: Relative path (from git_root) to scan.  Defaults to
            ``"quoin/dev"`` for the real-tree guard.  Pass ``"dev"`` for a
            tmp-repo whose layout has no ``quoin/`` prefix (T-04 negative test).

    Returns:
        Sorted list of relative file paths (from git_root) that are shadowed
        by a ``.gitignore`` rule and have a source-like extension.
    """
    result = subprocess.run(
        [
            "git",
            "-C",
            str(git_root),
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
            "--",
            subtree,
        ],
        capture_output=True,
        text=True,
    )
    violations: list[str] = []
    for line in result.stdout.splitlines():
        path = line.strip()
        if not path:
            continue
        if any(sub in path for sub in _IGNORE_SUBSTRINGS):
            continue
        if any(path.endswith(suf) for suf in _IGNORE_SUFFIXES):
            continue
        if Path(path).suffix in _SOURCE_EXTS:
            violations.append(path)
    return sorted(violations)


# ---------------------------------------------------------------------------
# T-03 — Real-tree guard: quoin/dev/ must have no shadowed source files
# ---------------------------------------------------------------------------

def test_no_shadowed_sources_in_quoin_dev():
    """No source-like file under quoin/dev/ may be hidden by a .gitignore rule.

    If this test fails, a committed test or script has been accidentally
    re-shadowed.  Run ``git check-ignore -v <path>`` on each listed file to
    find the offending rule, then either remove the rule or explicitly track
    the file with ``git add -f`` (the latter is a last resort).
    """
    # Skip cleanly when not inside a git work tree (e.g., package installs)
    _require_git_worktree(REPO_ROOT)

    violations = find_shadowed_sources(REPO_ROOT, subtree="quoin/dev")
    if violations:
        details = []
        for v in violations:
            ci_out = subprocess.run(
                ["git", "-C", str(REPO_ROOT), "check-ignore", "-v", v],
                capture_output=True,
                text=True,
            )
            rule = ci_out.stdout.strip() or "(unknown rule)"
            details.append(f"  {v!r}  ← {rule}")
        assert False, (
            "Shadowed source files found under quoin/dev/ — "
            "these files are git-ignored and absent from clean checkouts:\n"
            + "\n".join(details)
        )


def _require_git_worktree(path: Path) -> None:
    """Skip the test if *path* is not inside a git work tree."""
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip("not inside a git work tree — skipping gitignore shadow guard")


# ---------------------------------------------------------------------------
# T-03 skip-branch: verify _require_git_worktree skips cleanly (MIN-3)
# ---------------------------------------------------------------------------

def test_skip_branch_fires_outside_git(monkeypatch, tmp_path):
    """_require_git_worktree skips when git rev-parse fails (non-git dir)."""
    # Monkeypatch subprocess.run so rev-parse returns non-zero
    import subprocess as _sp

    original_run = _sp.run

    def fake_run(cmd, **kwargs):
        if "rev-parse" in cmd and "--is-inside-work-tree" in cmd:
            return _sp.CompletedProcess(cmd, returncode=128, stdout="", stderr="fatal: not a git repo")
        return original_run(cmd, **kwargs)

    monkeypatch.setattr(_sp, "run", fake_run)

    with pytest.raises(pytest.skip.Exception):
        _require_git_worktree(tmp_path)


# ---------------------------------------------------------------------------
# T-04 — Negative-path: would-fail-on-shadow coverage
# ---------------------------------------------------------------------------

def _make_git_repo(path: Path) -> Path:
    """Initialize a minimal git repo at *path*."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", str(path)], capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        capture_output=True, check=True, cwd=str(path),
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        capture_output=True, check=True, cwd=str(path),
    )
    # Initial commit so the repo is valid
    (path / ".gitkeep").write_text("placeholder")
    subprocess.run(["git", "add", ".gitkeep"], capture_output=True, check=True, cwd=str(path))
    subprocess.run(
        ["git", "commit", "-m", "init"],
        capture_output=True, check=True, cwd=str(path),
    )
    return path


def test_find_shadowed_sources_detects_shadow(tmp_path):
    """find_shadowed_sources() reports a .py file hidden by a dev/ gitignore rule.

    The tmp repo has a root-level ``.gitignore`` containing ``dev/``.  A probe
    file at ``dev/tests/test_probe.py`` is untracked and matches the rule.
    ``find_shadowed_sources(tmp_repo, subtree="dev")`` must report it.

    Note: subtree="dev" (no quoin/ prefix) matches the tmp-repo layout.
    This is the fix for CRIT-2: the old hardcoded "quoin/dev" pathspec applied
    to the tmp repo returned empty, making the negative test vacuous.
    """
    repo = _make_git_repo(tmp_path / "repo")

    # Plant the shadow rule
    (repo / ".gitignore").write_text("dev/\n")

    # Plant the probe source file (untracked + ignored by the rule)
    probe_dir = repo / "dev" / "tests"
    probe_dir.mkdir(parents=True)
    probe_file = probe_dir / "test_probe.py"
    probe_file.write_text("# probe\ndef test_nothing(): pass\n")

    violations = find_shadowed_sources(repo, subtree="dev")
    assert any("test_probe.py" in v for v in violations), (
        f"Expected test_probe.py in violations; got: {violations}"
    )


def test_find_shadowed_sources_ignores_bytecode(tmp_path):
    """Bytecode under __pycache__/ is NOT reported as a violation (positive control)."""
    repo = _make_git_repo(tmp_path / "repo")

    # Plant the shadow rule
    (repo / ".gitignore").write_text("dev/\n")

    # Plant a .pyc file — should be filtered out
    cache_dir = repo / "dev" / "tests" / "__pycache__"
    cache_dir.mkdir(parents=True)
    (cache_dir / "foo.cpython-38.pyc").write_text("")

    violations = find_shadowed_sources(repo, subtree="dev")
    assert not violations, (
        f"Expected no violations (bytecode only); got: {violations}"
    )
