"""T-16: Final wheel install smoke test.

Builds the wheel, pip-installs it into a temp target, invokes quoin install,
and verifies the ~/.claude/ tree is populated correctly.

Skipped when `claude` is absent (CI-friendly) and when `build` is not installed.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tarfile
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
def test_distributions_hold_no_split_out_components(built_wheel, built_sdist):
    """The OpenCode adapter, workctl and the cockpit tools live in their own
    repositories; neither distribution may carry them or any npm build tree."""
    with zipfile.ZipFile(built_wheel) as whl:
        wheel_names = whl.namelist()
    with tarfile.open(built_sdist, "r:gz") as sdist:
        sdist_names = sdist.getnames()
    for names in (wheel_names, sdist_names):
        for name in names:
            low = name.lower()
            for part in ("opencode", "streamdeck", "agentdesk", "workctl", "/node_modules/"):
                assert part not in low, name


@_requires_build
def test_wheel_contents_include_agent_skills_and_open_pack_assets(built_wheel):
    """Wheel installs must include the repo-local assets used by Codex CLI helpers."""
    with zipfile.ZipFile(built_wheel) as whl:
        names = whl.namelist()

    required = [
        "quoin/data/core/workflow/skills.json",
        "quoin/data/core/workflow/rules.md",
        "quoin/data/core/scripts/build_agent_skills.py",
        "quoin/data/packs/open/.claude-plugin/plugin.json",
        "quoin/data/packs/open/scripts/probe_gateway.py",
    ]
    for path in required:
        assert any(name.endswith(path) for name in names), f"Missing wheel asset: {path}"


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
    """Wheel installs must include CLAUDE.slim.md: the plugin's rules hook reads
    it. pyproject.toml lists top-level data files one by one, so a missing
    force-include entry would leave it out of pip installs."""
    with zipfile.ZipFile(built_wheel) as whl:
        names = whl.namelist()
    assert any(name.endswith("quoin/data/CLAUDE.slim.md") for name in names), (
        "Missing wheel asset: quoin/data/CLAUDE.slim.md"
    )


def test_pyproject_force_include_line_for_plugins():
    """pyproject.toml must wire quoin/plugins into the wheel data dir.

    Cheap non-build guard: without this line the opt-in workflow-tasks mod
    is absent from wheel installs and the install flag has nothing to copy.
    """
    expected = '"quoin/plugins" = "src/quoin/data/plugins"'
    assert expected in _force_include_block(), (
        f"pyproject.toml force-include must contain:\n  {expected}"
    )


@_requires_build
def test_wheel_contents_include_workflow_tasks_plugin(built_wheel):
    """The wheel ships the workflow-tasks mod, minus generated type stubs."""
    with zipfile.ZipFile(built_wheel) as whl:
        names = whl.namelist()
    for suffix in (
        "quoin/data/plugins/workflow-tasks/hooks/tasks.ts",
        "quoin/data/plugins/workflow-tasks/hooks/register.tsx",
        "quoin/data/plugins/workflow-tasks/.claude-plugin/plugin.json",
    ):
        assert any(name.endswith(suffix) for name in names), (
            f"Missing wheel asset: {suffix}"
        )
    assert not any(
        "plugins/workflow-tasks/.claude-plugin/types/" in name for name in names
    ), "generated .claude-plugin/types/ must not ship in the wheel"


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


@_requires_build
def test_wheel_ships_the_plugin(built_wheel):
    """A pip install must carry everything `quoin install` stages as the plugin."""
    with zipfile.ZipFile(built_wheel) as whl:
        names = whl.namelist()
    for suffix in (
        "quoin/data/.claude-plugin/plugin.json",
        "quoin/data/.claude-plugin/marketplace.json",
        "quoin/data/hooks/hooks.json",
        "quoin/data/hooks/rules.sh",
        "quoin/data/adapters/claude/skills/run/SKILL.md",
        "quoin/data/QUICKSTART.md",
    ):
        assert any(name.endswith(suffix) for name in names), f"Missing wheel asset: {suffix}"
