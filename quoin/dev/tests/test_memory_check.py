"""IVG-50 T-04: tests for memory_check referential-integrity checker.

Two-tier structure:

1. Tier 1 (CI-safe, no `claude`):
   Import installer.py via importlib and assert "memory_check.py"
   is present in BOTH DEPLOYED_SCRIPTS and CORE_SCRIPTS. Also asserts both
   source files exist in the repo.

2. Behavior tests (tmp_path, no external deps):
   Build fixture memory dirs, call the core check() API directly.
   Covers valid set (ok=True), dangling link, orphaned fact-file,
   --allow-forward-links flag, --json output, and stdlib-purity guard (D-S2-3).
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]  # quoin/ repo root
INSTALLER_PY = REPO_ROOT / "src" / "quoin" / "installer.py"
WRAPPER_SRC = REPO_ROOT / "quoin" / "scripts" / "memory_check.py"
CORE_IMPL_SRC = REPO_ROOT / "quoin" / "core" / "scripts" / "memory_check.py"

# ---------------------------------------------------------------------------
# Tier 1: installer membership (CI-safe, no claude binary)
# ---------------------------------------------------------------------------


def _load_installer():
    spec = importlib.util.spec_from_file_location("installer", INSTALLER_PY)
    assert spec is not None, f"Could not load spec from {INSTALLER_PY}"
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_installer_deployed_scripts_contains_memory_check():
    """DEPLOYED_SCRIPTS must contain memory_check.py (wrapper deployment guard)."""
    mod = _load_installer()
    assert "memory_check.py" in mod.DEPLOYED_SCRIPTS, (
        "installer.py DEPLOYED_SCRIPTS must contain 'memory_check.py'. "
        "Missing entry means install.sh won't deploy the wrapper to "
        "~/.claude/scripts/memory_check.py."
    )


def test_installer_core_scripts_contains_memory_check():
    """CORE_SCRIPTS must contain memory_check.py (wrapper loader guard)."""
    mod = _load_installer()
    assert "memory_check.py" in mod.CORE_SCRIPTS, (
        "installer.py CORE_SCRIPTS must contain 'memory_check.py'. "
        "Missing entry means the wrapper's parents[1] loader fails at runtime."
    )


def test_source_files_exist():
    """Both source files must exist in the repo."""
    assert WRAPPER_SRC.is_file(), f"Wrapper source missing: {WRAPPER_SRC}"
    assert CORE_IMPL_SRC.is_file(), f"Core impl source missing: {CORE_IMPL_SRC}"


def test_wrapper_help_exits_zero():
    """Wrapper --help must exit 0 (exercises the importlib loader chain)."""
    result = subprocess.run(
        [sys.executable, str(WRAPPER_SRC), "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"--help failed:\n{result.stderr}"
    assert "memory_check" in result.stdout.lower()


# ---------------------------------------------------------------------------
# Core loader helper
# ---------------------------------------------------------------------------

def _load_core():
    """Load the core module directly for behaviour tests (mandatory importlib pattern)."""
    spec = importlib.util.spec_from_file_location("_mc_core", CORE_IMPL_SRC)
    assert spec is not None, f"Could not load spec from {CORE_IMPL_SRC}"
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Load once for all tests in this module
MC = _load_core()


# ---------------------------------------------------------------------------
# Fixture builder helper
# ---------------------------------------------------------------------------

def _build_memory_dir(root: Path, links: list[str], files: list[str]) -> Path:
    """Populate *root* as a memory dir with MEMORY.md and sibling fact-files.

    *links* — list of .md filenames referenced in MEMORY.md
    *files* — list of .md fact-files to create on disk (excluding MEMORY.md)
    """
    # Build MEMORY.md content with one link per entry
    lines = []
    for fname in links:
        title = fname.replace(".md", "").replace("_", " ").title()
        lines.append(f"- [{title}]({fname}) — auto-generated test fixture")
    (root / "MEMORY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    # Create fact-files
    for fname in files:
        (root / fname).write_text(f"# {fname}\nTest fixture content.\n", encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# Fixture (a): valid set — MEMORY.md links A, B, C + fact-files A, B, C
# ---------------------------------------------------------------------------

@pytest.mark.slow_fs
def test_valid_set_ok(tmp_path):
    """Valid memory dir (all links resolve, no orphans) → ok=True, exit 0."""
    files = ["fact_a.md", "fact_b.md", "fact_c.md"]
    mem_dir = _build_memory_dir(tmp_path, links=files, files=files)

    result = MC.check(mem_dir)
    assert result["ok"] is True, f"Expected ok=True, got: {result}"
    assert result["dangling"] == []
    assert result["orphans"] == []
    assert result["forward"] == []

    # main() exit code
    rc = MC.main([str(mem_dir)])
    assert rc == 0, f"Expected exit 0, got {rc}"


@pytest.mark.slow_fs
def test_valid_set_json(tmp_path):
    """--json on valid dir → ok=True JSON, exit 0."""
    files = ["fact_a.md", "fact_b.md"]
    mem_dir = _build_memory_dir(tmp_path, links=files, files=files)

    result = subprocess.run(
        [sys.executable, str(WRAPPER_SRC), "--json", str(mem_dir)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert data["ok"] is True
    assert data["dangling"] == []
    assert data["orphans"] == []
    assert "forward" in data


# ---------------------------------------------------------------------------
# Fixture (b): dangling link — MEMORY.md links A, B + only fact-file A
# ---------------------------------------------------------------------------

@pytest.mark.slow_fs
def test_dangling_link_exit1(tmp_path):
    """MEMORY.md link to missing file → dangling listed; exit 1 by default."""
    mem_dir = _build_memory_dir(tmp_path, links=["fact_a.md", "fact_b.md"], files=["fact_a.md"])

    result = MC.check(mem_dir)
    assert result["ok"] is False
    assert "fact_b.md" in result["dangling"]
    assert result["orphans"] == []

    rc = MC.main([str(mem_dir)])
    assert rc == 1, f"Expected exit 1, got {rc}"


@pytest.mark.slow_fs
def test_dangling_link_allow_forward_links_exit0(tmp_path):
    """--allow-forward-links demotes dangling to allowed → exit 0."""
    mem_dir = _build_memory_dir(tmp_path, links=["fact_a.md", "fact_b.md"], files=["fact_a.md"])

    result = MC.check(mem_dir, allow_forward_links=True)
    assert result["ok"] is True, f"Expected ok=True with allow_forward_links, got: {result}"
    assert "fact_b.md" in result["dangling"]  # still reported, but not an error

    rc = MC.main([str(mem_dir), "--allow-forward-links"])
    assert rc == 0, f"Expected exit 0 with --allow-forward-links, got {rc}"


@pytest.mark.slow_fs
def test_dangling_link_json(tmp_path):
    """--json on dangling dir → ok=False, dangling listed, exit 1."""
    mem_dir = _build_memory_dir(tmp_path, links=["fact_a.md", "fact_b.md"], files=["fact_a.md"])

    result = subprocess.run(
        [sys.executable, str(WRAPPER_SRC), "--json", str(mem_dir)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 1
    data = json.loads(result.stdout)
    assert data["ok"] is False
    assert "fact_b.md" in data["dangling"]


# ---------------------------------------------------------------------------
# Fixture (c): orphaned fact-file — MEMORY.md links A + fact-files A, B
# ---------------------------------------------------------------------------

@pytest.mark.slow_fs
def test_orphan_exit1(tmp_path):
    """Fact-file not referenced by MEMORY.md → orphan listed; exit 1."""
    mem_dir = _build_memory_dir(tmp_path, links=["fact_a.md"], files=["fact_a.md", "fact_b.md"])

    result = MC.check(mem_dir)
    assert result["ok"] is False
    assert "fact_b.md" in result["orphans"]
    assert result["dangling"] == []

    rc = MC.main([str(mem_dir)])
    assert rc == 1, f"Expected exit 1, got {rc}"


@pytest.mark.slow_fs
def test_orphan_allow_forward_links_still_exit1(tmp_path):
    """--allow-forward-links does NOT suppress orphan errors — exit 1 regardless."""
    mem_dir = _build_memory_dir(tmp_path, links=["fact_a.md"], files=["fact_a.md", "fact_b.md"])

    rc = MC.main([str(mem_dir), "--allow-forward-links"])
    assert rc == 1, f"Expected exit 1 (orphans are always errors), got {rc}"


@pytest.mark.slow_fs
def test_orphan_json(tmp_path):
    """--json on orphan dir → ok=False, orphan listed, exit 1."""
    mem_dir = _build_memory_dir(tmp_path, links=["fact_a.md"], files=["fact_a.md", "fact_b.md"])

    result = subprocess.run(
        [sys.executable, str(WRAPPER_SRC), "--json", str(mem_dir)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 1
    data = json.loads(result.stdout)
    assert data["ok"] is False
    assert "fact_b.md" in data["orphans"]


# ---------------------------------------------------------------------------
# Error handling: missing MEMORY.md → exit 2
# ---------------------------------------------------------------------------

@pytest.mark.slow_fs
def test_missing_memory_md_exit2(tmp_path):
    """Directory with no MEMORY.md → exit 2 (usage/IO error)."""
    rc = MC.main([str(tmp_path)])
    assert rc == 2, f"Expected exit 2 for missing MEMORY.md, got {rc}"


# ---------------------------------------------------------------------------
# D-S2-4: check_index_pointers stub
# ---------------------------------------------------------------------------

@pytest.mark.slow_fs
def test_check_index_pointers_returns_empty(tmp_path):
    """check_index_pointers() must return [] until S-1 ships."""
    result = MC.check_index_pointers(tmp_path)
    assert result == [], f"Expected [], got {result}"


# ---------------------------------------------------------------------------
# D-S2-3 purity guard: core must not import from adapter layer
# ---------------------------------------------------------------------------

def test_no_adapter_import():
    """core/scripts/memory_check.py must not import from quoin.scripts (D-S2-3)."""
    text = CORE_IMPL_SRC.read_text(encoding="utf-8")
    assert "from quoin.scripts" not in text, (
        "core/scripts/memory_check.py must not import from quoin.scripts "
        "(D-S2-3 portable-core purity violation)."
    )
    assert "import quoin.scripts" not in text, (
        "core/scripts/memory_check.py must not import quoin.scripts "
        "(D-S2-3 portable-core purity violation)."
    )
