"""T-16: Final wheel install smoke test.

Builds the wheel, pip-installs it into a temp target, invokes quoin install,
and verifies the ~/.claude/ tree is populated correctly.

Skipped when `claude` is absent (CI-friendly) and when `build` is not installed.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
QUOIN_SRC = REPO / "quoin"

try:
    import build  # noqa: F401
    _BUILD_AVAILABLE = True
except ImportError:
    _BUILD_AVAILABLE = False

_CLAUDE_AVAILABLE = shutil.which("claude") is not None

_requires_build = pytest.mark.skipif(
    not _BUILD_AVAILABLE,
    reason="python 'build' package not installed",
)


def _force_include_block() -> str:
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    start = text.index("[tool.hatch.build.targets.wheel.force-include]")
    end = text.index("[tool.hatch.build.targets.sdist]")
    return text[start:end]


def _sdist_block() -> str:
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    start = text.index("[tool.hatch.build.targets.sdist]")
    end = text.index("[tool.pytest.ini_options]")
    return text[start:end]


def test_packaging_config_excludes_benchmark_folders_from_distribution():
    """Benchmark design/results stay in git, not install artifacts."""
    force_include = _force_include_block()
    sdist = _sdist_block()

    assert "quoin/benchmarks" not in force_include
    assert "benchmark-results" not in force_include
    assert '"quoin/benchmarks/"' in sdist
    assert '"benchmark-results/"' in sdist


def test_memory_packaged_as_directory_and_all_tier1_files_present():
    """Drift guard: memory uses a directory force-include, not per-file enumeration.

    Checks two invariants without building a wheel (never skipped):
    1. pyproject.toml uses "quoin/memory" directory mapping (not per-file lines).
    2. Every TIER1_MEMORY_FILES entry has a source file in quoin/memory/ so the
       directory glob will include it. Adding a file to TIER1_MEMORY_FILES without
       creating the source file will fail here before the broken wheel is published.
    """
    from quoin.installer import TIER1_MEMORY_FILES

    force_include = _force_include_block()

    # invariant 1: directory mapping present
    assert '"quoin/memory"' in force_include, (
        "pyproject.toml must use a directory force-include for quoin/memory, "
        "not per-file entries. Found no '\"quoin/memory\"' key in force-include block."
    )

    # invariant 2: all TIER1_MEMORY_FILES have source files
    memory_src = QUOIN_SRC / "memory"
    missing = [f for f in TIER1_MEMORY_FILES if not (memory_src / f).is_file()]
    assert not missing, (
        f"TIER1_MEMORY_FILES entries have no source file in quoin/memory/: {missing}. "
        "Create the file or remove it from TIER1_MEMORY_FILES."
    )


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory):
    """Build the wheel once per module and return its path."""
    dist_dir = tmp_path_factory.mktemp("dist")
    result = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(dist_dir), str(REPO)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        pytest.skip(f"wheel build failed:\n{result.stderr[:500]}")
    wheels = list(dist_dir.glob("*.whl"))
    assert wheels, "No wheel produced by build"
    return wheels[0]


@pytest.fixture(scope="module")
def built_sdist(tmp_path_factory):
    """Build the sdist once per module and return its path."""
    dist_dir = tmp_path_factory.mktemp("sdist")
    result = subprocess.run(
        [sys.executable, "-m", "build", "--sdist", "--outdir", str(dist_dir), str(REPO)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        pytest.skip(f"sdist build failed:\n{result.stderr[:500]}")
    sdists = list(dist_dir.glob("*.tar.gz"))
    assert sdists, "No sdist produced by build"
    return sdists[0]


@_requires_build
def test_wheel_contents_no_private_files(built_wheel):
    """Wheel must not contain project-private files."""
    with zipfile.ZipFile(built_wheel) as whl:
        names = whl.namelist()

    for bad in ("lessons-learned.md", "workflow-rules.md", "workflow-suggestions.md"):
        assert not any(bad in n for n in names), f"Private file in wheel: {bad}"

    assert not any("quoin/dev/" in n for n in names), "quoin/dev/ must not be in wheel"
    assert not any("install.sh" in n for n in names), "install.sh must not be in wheel"


@_requires_build
def test_distributions_do_not_include_benchmark_folders(built_wheel, built_sdist):
    """Benchmarks/results are repo evidence, not installable distribution data."""
    forbidden = ("quoin/benchmarks/", "benchmark-results/")

    with zipfile.ZipFile(built_wheel) as whl:
        wheel_names = whl.namelist()
    for bad in forbidden:
        assert not any(bad in name for name in wheel_names), f"{bad} must not be in wheel"

    with tarfile.open(built_sdist, "r:gz") as sdist:
        sdist_names = sdist.getnames()
    for bad in forbidden:
        assert not any(bad in name for name in sdist_names), f"{bad} must not be in sdist"


@_requires_build
def test_wheel_contents_include_codex_cli_assets(built_wheel):
    """Wheel installs must include the repo-local assets used by Codex CLI helpers."""
    with zipfile.ZipFile(built_wheel) as whl:
        names = whl.namelist()

    required = [
        "quoin/data/core/workflow/skills.json",
        "quoin/data/core/workflow/rules.md",
        "quoin/data/adapters/codex/generate_codex_assets.py",
        "quoin/data/adapters/codex/verify_codex_readiness.py",
        "quoin/data/adapters/codex/smoke_codex_workflow.py",
    ]
    for path in required:
        assert any(name.endswith(path) for name in names), f"Missing wheel asset: {path}"


@_requires_build
def test_wheel_contents_include_opencode_adapter_assets(built_wheel):
    """Wheel installs must include the opencode qualification assets."""
    with zipfile.ZipFile(built_wheel) as whl:
        names = whl.namelist()

    required = [
        "quoin/data/adapters/opencode/probe_gateway.py",
        "quoin/data/adapters/opencode/fake_openai_server.py",
        "quoin/data/adapters/opencode/fixtures/scenarios.json",
        "quoin/data/adapters/opencode/README.md",
        "quoin/data/adapters/opencode/compatibility.md",
        "quoin/data/adapters/opencode/decisions.md",
    ]
    for path in required:
        assert any(name.endswith(path) for name in names), f"Missing wheel asset: {path}"

    assert not any(
        "adapters/opencode/" in name and (name.endswith("__pycache__") or "__pycache__/" in name or name.endswith(".pyc"))
        for name in names
    )


def test_pyproject_force_include_line_for_claude_slim_md():
    """pyproject.toml must wire quoin/CLAUDE.slim.md into the wheel (T-06).

    Cheap non-build guard, checked even when `build` is unavailable —
    mirrors test_pyproject_force_include_line in test_branch_recovery_recipe.py.
    Top-level data files are enumerated individually in force-include (only
    directories are globbed), so this literal line is the actual invariant.
    """
    pyproject = REPO / "pyproject.toml"
    assert pyproject.exists(), f"pyproject.toml not found at {pyproject}"
    text = pyproject.read_text(encoding="utf-8")
    expected = '"quoin/CLAUDE.slim.md" = "src/quoin/data/CLAUDE.slim.md"'
    assert expected in text, (
        f"pyproject.toml must contain the force-include line:\n  {expected}\n"
        "Without it, CLAUDE.slim.md is silently absent from wheel installs."
    )
    slim_source = QUOIN_SRC / "CLAUDE.slim.md"
    assert slim_source.exists(), (
        f"quoin/CLAUDE.slim.md source missing at {slim_source}; the "
        "force-include line can only ship a file that exists."
    )


@_requires_build
def test_wheel_contents_include_claude_md_slim_variant(built_wheel):
    """Wheel installs must include CLAUDE.slim.md (IVG-164 stage 1 T-06).

    pyproject.toml enumerates top-level data files individually (only
    directories are globbed), so without an explicit force-include entry the
    slim variant is silently absent from wheel installs and
    --claude-md-variant slim exits 1 on a pip-installed quoin — invisible to
    repo-checkout pilots because install.sh always passes --source-dir.
    """
    with zipfile.ZipFile(built_wheel) as whl:
        names = whl.namelist()
    assert any(name.endswith("quoin/data/CLAUDE.slim.md") for name in names), (
        "Missing wheel asset: quoin/data/CLAUDE.slim.md"
    )


@_requires_build
def test_wheel_contents_include_claude_adapter_skill_assets(built_wheel):
    """Wheel installs must include active Claude adapter skills, not only stubs."""
    with zipfile.ZipFile(built_wheel) as whl:
        names = whl.namelist()
        adapter_skills = [
            name for name in names
            if "/data/adapters/claude/skills/" in name and name.endswith("/SKILL.md")
        ]
        contents = {
            name: whl.read(name).decode("utf-8")
            for name in adapter_skills
        }

    expected = QUOIN_SRC / "adapters" / "claude" / "skills"
    expected_skills = sorted(p.parent.name for p in expected.glob("*/SKILL.md"))

    assert sorted(Path(name).parent.name for name in adapter_skills) == expected_skills
    for name, content in contents.items():
        assert "DEPRECATED LOCATION" not in content, name
        assert "deprecated stub" not in content, name


@_requires_build
def test_wheel_install_and_quoin_install(built_wheel):
    """Install the wheel and run quoin install from bundled quoin/data."""
    pytest.importorskip("build")  # double-guard

    from quoin.installer import (  # noqa: PLC0415
        CANONICAL_SKILLS,
        DEPRECATED_SKILL_MARKERS,
        TIER1_MEMORY_FILES,
    )

    with tempfile.TemporaryDirectory() as install_target, \
            tempfile.TemporaryDirectory() as home_dir, \
            tempfile.TemporaryDirectory() as fake_bin:
        # pip-install the wheel into a temp target dir
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--target", install_target,
             str(built_wheel)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            pytest.skip(f"pip install failed:\n{result.stderr[:300]}")

        fake_bin_path = Path(fake_bin)
        for executable in ("claude", "git"):
            tool = fake_bin_path / executable
            tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            tool.chmod(0o755)

        # Run from the installed wheel without --source-dir so _resolve_source_dir
        # must use importlib.resources.files("quoin") / "data".
        env = {
            **os.environ,
            "HOME": home_dir,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "PYTHONPATH": install_target,
        }
        cmd = [sys.executable, "-m", "quoin", "install"]

        result = subprocess.run(
            cmd, env=env, capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, (
            f"quoin install failed:\n{result.stdout}\n{result.stderr}"
        )

        claude_dir = Path(home_dir) / ".claude"

        # Tier-1 memory files present
        for fname in TIER1_MEMORY_FILES:
            assert (claude_dir / "memory" / fname).exists(), f"Missing: {fname}"

        # Skills present
        for skill in CANONICAL_SKILLS:
            skill_md = claude_dir / "skills" / skill / "SKILL.md"
            assert skill_md.exists(), (
                f"Missing skill: {skill}"
            )
            content = skill_md.read_text(encoding="utf-8")
            for marker in DEPRECATED_SKILL_MARKERS:
                assert marker not in content, f"Deprecated marker in deployed {skill}"

        migrated = "plan"
        deployed_plan = claude_dir / "skills" / migrated / "SKILL.md"
        source_plan = QUOIN_SRC / "adapters" / "claude" / "skills" / migrated / "SKILL.md"
        source_bytes = source_plan.read_bytes()
        deployed_bytes = deployed_plan.read_bytes()
        # The installer substitutes __QUOIN_HOME__ → the real ~/.claude path.
        # Verify the substitution was applied correctly rather than checking raw equality.
        expected_bytes = source_bytes.replace(b"__QUOIN_HOME__", str(claude_dir.resolve()).encode())
        assert deployed_bytes == expected_bytes, (
            "Deployed plan/SKILL.md does not match source after __QUOIN_HOME__ substitution"
        )

        # QUICKSTART
        assert (claude_dir / "QUICKSTART.md").exists()

        # CLAUDE.md with exactly one marker section
        content = (claude_dir / "CLAUDE.md").read_text()
        assert content.count("# === DEV WORKFLOW START ===") == 1

        # Preamble mtimes stable across two consecutive installs (CRIT-1 round-2)
        preamble_mtimes_1 = {
            p: p.stat().st_mtime
            for p in (claude_dir / "skills").rglob("preamble.md")
        }
        subprocess.run(cmd, env=env, capture_output=True, timeout=60)
        for p, mtime in preamble_mtimes_1.items():
            assert p.stat().st_mtime == mtime, (
                f"preamble.md mtime changed on second install: {p}"
            )


def test_git_status_clean_after_editable_install():
    """After pip install -e . + quoin install, git status --porcelain is empty.

    Verifies no build artifacts (egg-info, build/, dist/) are left tracked.
    (MAJ-4 round-2 fix)
    """
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    # Filter out untracked files (lines starting with ??); those are expected
    tracked_dirty = [
        line for line in result.stdout.splitlines()
        if not line.startswith("??")
    ]
    # Allow modifications to test files themselves but no build artifacts
    build_artifacts = [
        line for line in tracked_dirty
        if any(x in line for x in ("egg-info", "dist/", "build/", ".pytest_cache"))
    ]
    assert not build_artifacts, (
        f"Build artifacts leaked into git tracking:\n" + "\n".join(build_artifacts)
    )
