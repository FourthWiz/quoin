"""Tests for validate_adapter_drift.py — Phase 22 runtime-portability work.

Coverage:
- test_validator_passes_on_live_repo           positive gold-master (IVG-107: also exercises AD-WR/AD-WD/AD-PK/AD-CX on clean tree)
- test_validator_passes_on_minimal_repo        positive minimal synthetic
- test_drift_co_detected                       AD-CO  core doc missing
- test_drift_ad_detected                       AD-AD  adapter SKILL.md missing
- test_drift_ls_detected                       AD-LS  legacy stub missing
- test_drift_fn_detected                       AD-FN  frontmatter name wrong
- test_drift_fm_detected                       AD-FM  frontmatter model mismatch
- test_drift_pt_detected                       AD-PT  pointer text missing
- test_drift_fb_detected                       AD-FB  frontmatter not byte-equal
- test_drift_ss_detected                       AD-SS  stub not shorter than adapter
- test_drift_s0p_detected                      AD-S0P section_0=true but block absent
- test_drift_s0a_detected                      AD-S0A section_0=false but block present
- test_drift_pe_detected                       AD-PE  spawn_target preamble missing
- test_drift_pa_detected                       AD-PA  spawn_target preamble in adapter dir
- test_drift_px_detected                       AD-PX  non-spawn has preamble
- test_drift_iv_detected                       AD-IV  ADAPTER_FOO_SRC= missing
- test_drift_ie_detected                       AD-IE  if/elif branch missing
- test_drift_io_detected                       AD-IO  preflight after for-loop
- test_json_output_is_valid                    --json flag emits parseable JSON
- test_json_output_contains_invariant_and_skill_fields  JSON violation shape
- test_unreadable_manifest_returns_65          bad manifest path → exit 65
- test_bad_cli_args_return_64                  unknown flag → exit 64
- test_validator_handles_missing_optional_fields  omitted section_0/spawn_target default=false
IVG-107 additions (T-07):
- test_drift_wr_detected                       AD-WR  core script has no wrapper
- test_drift_wd_missing_shim                   AD-WD  wrapper missing importlib shim
- test_drift_wd_has_toplevel_def               AD-WD  wrapper has top-level def
- test_drift_pk_detected                       AD-PK  force-include key missing
- test_drift_cx_detected                       AD-CX  Codex skill README missing
- test_drift_cx_manifest_detected              AD-CX  feature-manifest absent/invalid JSON
- test_new_checks_skip_when_absent             no new-ID violations on bare minimal repo
"""
import json
import subprocess
import sys
from pathlib import Path


# Module-scope repo root — the quoin/ git repo root (mirrors test_runtime_portability_docs.py line 6)
# parents[0]=tests/, parents[1]=dev/, parents[2]=quoin/ (package), parents[3]=quoin/ (git root)
REPO_ROOT = Path(__file__).resolve().parents[3]

# Canonical core script path (used by all subprocess calls)
# Script is at <repo_root>/quoin/core/scripts/validate_adapter_drift.py
SCRIPT = REPO_ROOT / "quoin" / "core" / "scripts" / "validate_adapter_drift.py"


# ---------------------------------------------------------------------------
# Minimal valid manifest for synthetic tests
# ---------------------------------------------------------------------------
_MINIMAL_MANIFEST = {
    "schema_version": 2,
    "skills": [
        {
            "name": "foo",
            "phase": "test",
            "effort": "low",
            "user_facing": True,
            "claude_model": "opus",
            "section_0": False,
            "spawn_target": False,
        }
    ],
}

# Minimal valid adapter SKILL.md for "foo" — frontmatter + pointer + body
_ADAPTER_FRONTMATTER = "\nname: foo\nmodel: opus\n"
_ADAPTER_BODY = (
    "*Portable intent doc: `quoin/core/skills/foo.md`*\n\n"
    "# Foo skill\n\n"
    "This is the adapter implementation. It is longer than the stub.\n"
    "More content here to ensure stub-shorter invariant is satisfied.\n"
    "Even more content to make this definitely longer than the stub.\n"
)
_ADAPTER_FULL = f"---{_ADAPTER_FRONTMATTER}---\n{_ADAPTER_BODY}"

# Minimal legacy stub — same frontmatter, shorter body
_STUB_FULL = f"---{_ADAPTER_FRONTMATTER}---\n*See adapter for full implementation.*\n"

# Minimal install.sh — ADAPTER_FOO_SRC= before the for-loop, with elif branch inside
_INSTALL_SH = (
    "#!/usr/bin/env bash\n"
    'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"\n'
    'ADAPTER_FOO_SRC="$SCRIPT_DIR/adapters/claude/skills/foo/SKILL.md"\n'
    "\n"
    'for skill_dir in "$SCRIPT_DIR/skills"/*/; do\n'
    '    skill_name=$(basename "$skill_dir")\n'
    '    if [ "$skill_name" = "capture_insight" ]; then\n'
    '        cp "$ADAPTER_CAPTURE_INSIGHT_SRC" "$skill_dir/SKILL.md"\n'
    '    elif [ "$skill_name" = "foo" ]; then\n'
    '        cp "$ADAPTER_FOO_SRC" "$skill_dir/SKILL.md"\n'
    "    fi\n"
    "done\n"
)


# ---------------------------------------------------------------------------
# Helper: build a minimal synthetic repo under tmp_path
# ---------------------------------------------------------------------------

def _make_minimal_repo(tmp_path: Path) -> Path:
    """Create a minimal valid repo layout for skill 'foo' under tmp_path.

    Directory tree:
        <tmp>/quoin/core/skills/foo.md
        <tmp>/quoin/adapters/claude/skills/foo/SKILL.md
        <tmp>/quoin/skills/foo/SKILL.md
        <tmp>/quoin/core/workflow/skills.json
        <tmp>/quoin/install.sh
    """
    # Core skill doc
    core_dir = tmp_path / "quoin" / "core" / "skills"
    core_dir.mkdir(parents=True)
    (core_dir / "foo.md").write_text(
        "# Foo portable intent doc\n\nThis is the portable intent.\n",
        encoding="utf-8",
    )

    # Adapter SKILL.md
    adapter_dir = tmp_path / "quoin" / "adapters" / "claude" / "skills" / "foo"
    adapter_dir.mkdir(parents=True)
    (adapter_dir / "SKILL.md").write_text(_ADAPTER_FULL, encoding="utf-8")

    # Legacy stub SKILL.md
    stub_dir = tmp_path / "quoin" / "skills" / "foo"
    stub_dir.mkdir(parents=True)
    (stub_dir / "SKILL.md").write_text(_STUB_FULL, encoding="utf-8")

    # Manifest
    manifest_dir = tmp_path / "quoin" / "core" / "workflow"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "skills.json").write_text(
        json.dumps(_MINIMAL_MANIFEST, indent=2), encoding="utf-8"
    )

    # install.sh
    (tmp_path / "quoin" / "install.sh").write_text(_INSTALL_SH, encoding="utf-8")

    return tmp_path


def _run(repo_root: Path, *extra_args: str) -> subprocess.CompletedProcess:
    """Run the validator against the given repo root."""
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--repo-root", str(repo_root), *extra_args],
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# Positive tests
# ---------------------------------------------------------------------------

def test_validator_passes_on_live_repo():
    """Gold-master test: validator exits 0 against the real live repo.

    This also validates:
    - AD-IE relaxed check handles capture_insight's leading 'if' form
    - AD-PT substring check handles revise-fast's See-also form

    IVG-107 criterion-4 clean-tree guarantee: once AD-WR/AD-WD/AD-PK/AD-CX
    are wired into main(), this existing test automatically exercises them on
    the live repo. A zero exit here confirms all four new invariants produce
    no false positives on the clean tree. No separate live-tree test is needed.
    """
    result = _run(REPO_ROOT)
    assert result.returncode == 0, (
        f"Validator failed on live repo (exit {result.returncode}):\n{result.stderr}"
    )
    assert result.stderr == "", (
        f"Validator emitted unexpected stderr on live repo:\n{result.stderr}"
    )


def test_validator_passes_on_minimal_repo(tmp_path):
    """Validator exits 0 on a correctly constructed minimal synthetic repo."""
    _make_minimal_repo(tmp_path)
    result = _run(tmp_path)
    assert result.returncode == 0, (
        f"Validator failed on minimal repo (exit {result.returncode}):\n{result.stderr}"
    )


# ---------------------------------------------------------------------------
# Negative tests — one invariant violation per test
# ---------------------------------------------------------------------------

def test_drift_co_detected(tmp_path):
    """AD-CO: missing core doc is detected."""
    _make_minimal_repo(tmp_path)
    (tmp_path / "quoin" / "core" / "skills" / "foo.md").unlink()
    result = _run(tmp_path)
    assert result.returncode == 2
    assert "DRIFT AD-CO foo:" in result.stderr


def test_drift_ad_detected(tmp_path):
    """AD-AD: missing adapter SKILL.md is detected."""
    _make_minimal_repo(tmp_path)
    (tmp_path / "quoin" / "adapters" / "claude" / "skills" / "foo" / "SKILL.md").unlink()
    result = _run(tmp_path)
    assert result.returncode == 2
    assert "DRIFT AD-AD foo:" in result.stderr


def _flag_foo_without_claude_adapter(tmp_path):
    manifest_path = tmp_path / "quoin" / "core" / "workflow" / "skills.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["skills"][0]["claude_adapter"] = False
    manifest_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def test_claude_adapter_false_passes_when_adapter_and_stub_absent(tmp_path):
    """claude_adapter=false: only AD-CO/AD-CX run; absent Claude files are fine."""
    import shutil
    _make_minimal_repo(tmp_path)
    _flag_foo_without_claude_adapter(tmp_path)
    shutil.rmtree(tmp_path / "quoin" / "adapters" / "claude" / "skills" / "foo")
    shutil.rmtree(tmp_path / "quoin" / "skills" / "foo")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr


def test_claude_adapter_false_fails_when_adapter_present(tmp_path):
    """claude_adapter=false: a leftover Claude adapter is an AD-AD violation."""
    _make_minimal_repo(tmp_path)
    _flag_foo_without_claude_adapter(tmp_path)
    result = _run(tmp_path)
    assert result.returncode == 2
    assert "DRIFT AD-AD foo:" in result.stderr
    assert "claude_adapter=false" in result.stderr


def test_missing_claude_adapter_key_keeps_full_checks(tmp_path):
    """No claude_adapter key means true: a missing adapter still fails AD-AD."""
    _make_minimal_repo(tmp_path)
    (tmp_path / "quoin" / "adapters" / "claude" / "skills" / "foo" / "SKILL.md").unlink()
    result = _run(tmp_path)
    assert result.returncode == 2
    assert "DRIFT AD-AD foo:" in result.stderr
    assert "missing quoin/adapters/claude/skills/foo/SKILL.md" in result.stderr


def test_drift_ls_detected(tmp_path):
    """AD-LS: missing legacy stub is detected."""
    _make_minimal_repo(tmp_path)
    (tmp_path / "quoin" / "skills" / "foo" / "SKILL.md").unlink()
    result = _run(tmp_path)
    assert result.returncode == 2
    assert "DRIFT AD-LS foo:" in result.stderr


def test_drift_fn_detected(tmp_path):
    """AD-FN: adapter frontmatter name mismatch is detected."""
    _make_minimal_repo(tmp_path)
    adapter_path = tmp_path / "quoin" / "adapters" / "claude" / "skills" / "foo" / "SKILL.md"
    content = adapter_path.read_text(encoding="utf-8")
    # Change name field to wrong value — stub still has original so FB will also fire
    # but we assert AD-FN specifically
    adapter_path.write_text(content.replace("name: foo", "name: wrong"), encoding="utf-8")
    result = _run(tmp_path)
    assert result.returncode == 2
    assert "DRIFT AD-FN foo:" in result.stderr


def test_drift_fm_detected(tmp_path):
    """AD-FM: adapter frontmatter model mismatch is detected."""
    _make_minimal_repo(tmp_path)
    adapter_path = tmp_path / "quoin" / "adapters" / "claude" / "skills" / "foo" / "SKILL.md"
    content = adapter_path.read_text(encoding="utf-8")
    # Change model from 'opus' to 'haiku' — mismatches manifest's claude_model=opus
    adapter_path.write_text(content.replace("model: opus", "model: haiku"), encoding="utf-8")
    result = _run(tmp_path)
    assert result.returncode == 2
    assert "DRIFT AD-FM foo:" in result.stderr


def test_drift_pt_detected(tmp_path):
    """AD-PT: missing pointer text substring is detected."""
    _make_minimal_repo(tmp_path)
    adapter_path = tmp_path / "quoin" / "adapters" / "claude" / "skills" / "foo" / "SKILL.md"
    content = adapter_path.read_text(encoding="utf-8")
    # Remove the pointer text so 'quoin/core/skills/foo.md' no longer appears
    adapter_path.write_text(
        content.replace("quoin/core/skills/foo.md", "quoin/core/skills/other.md"),
        encoding="utf-8",
    )
    result = _run(tmp_path)
    assert result.returncode == 2
    assert "DRIFT AD-PT foo:" in result.stderr


def test_drift_fb_detected(tmp_path):
    """AD-FB: stub frontmatter not byte-equal to adapter frontmatter is detected."""
    _make_minimal_repo(tmp_path)
    stub_path = tmp_path / "quoin" / "skills" / "foo" / "SKILL.md"
    # Change stub frontmatter so it no longer byte-equals adapter frontmatter
    stub_path.write_text(
        f"---\nname: foo\nmodel: opus\nextra_field: added\n---\n"
        "*See adapter for full implementation.*\n",
        encoding="utf-8",
    )
    result = _run(tmp_path)
    assert result.returncode == 2
    assert "DRIFT AD-FB foo:" in result.stderr


def test_drift_ss_detected(tmp_path):
    """AD-SS: stub longer than adapter is detected."""
    _make_minimal_repo(tmp_path)
    stub_path = tmp_path / "quoin" / "skills" / "foo" / "SKILL.md"
    adapter_path = tmp_path / "quoin" / "adapters" / "claude" / "skills" / "foo" / "SKILL.md"
    adapter_size = len(adapter_path.read_text(encoding="utf-8"))
    # Write a stub that is larger than the adapter
    big_stub = _STUB_FULL + ("x" * (adapter_size + 100))
    stub_path.write_text(big_stub, encoding="utf-8")
    result = _run(tmp_path)
    assert result.returncode == 2
    assert "DRIFT AD-SS foo:" in result.stderr


def test_drift_s0p_detected(tmp_path):
    """AD-S0P: manifest declares section_0=true but adapter lacks §0 block."""
    _make_minimal_repo(tmp_path)
    # Set section_0=true in manifest for foo
    manifest_path = tmp_path / "quoin" / "core" / "workflow" / "skills.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["skills"][0]["section_0"] = True
    manifest_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    # Adapter does NOT have the §0 block (minimal adapter has none)
    result = _run(tmp_path)
    assert result.returncode == 2
    assert "DRIFT AD-S0P foo:" in result.stderr


def test_drift_s0a_detected(tmp_path):
    """AD-S0A: manifest declares section_0=false but adapter HAS §0 block."""
    _make_minimal_repo(tmp_path)
    # Add §0 block to adapter while manifest says section_0=false (default)
    adapter_path = tmp_path / "quoin" / "adapters" / "claude" / "skills" / "foo" / "SKILL.md"
    content = adapter_path.read_text(encoding="utf-8")
    adapter_path.write_text(
        content + "\n## §0 Model dispatch (FIRST STEP — execute before anything else)\n\nDispatch block.\n",
        encoding="utf-8",
    )
    result = _run(tmp_path)
    assert result.returncode == 2
    assert "DRIFT AD-S0A foo:" in result.stderr


def test_drift_pe_detected(tmp_path):
    """AD-PE: spawn_target=true but no preamble.md in legacy stub dir."""
    _make_minimal_repo(tmp_path)
    # Mark foo as spawn_target=true in manifest
    manifest_path = tmp_path / "quoin" / "core" / "workflow" / "skills.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["skills"][0]["spawn_target"] = True
    manifest_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    # No preamble.md in stub dir
    result = _run(tmp_path)
    assert result.returncode == 2
    assert "DRIFT AD-PE foo:" in result.stderr


def test_drift_pa_detected(tmp_path):
    """AD-PA: spawn_target=true and preamble.md also in adapter folder."""
    _make_minimal_repo(tmp_path)
    # Mark foo as spawn_target=true
    manifest_path = tmp_path / "quoin" / "core" / "workflow" / "skills.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["skills"][0]["spawn_target"] = True
    manifest_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    # Put preamble.md in stub dir (so AD-PE passes)
    (tmp_path / "quoin" / "skills" / "foo" / "preamble.md").write_text(
        "# preamble\n", encoding="utf-8"
    )
    # Also put preamble.md in adapter dir (violates AD-PA)
    (tmp_path / "quoin" / "adapters" / "claude" / "skills" / "foo" / "preamble.md").write_text(
        "# preamble\n", encoding="utf-8"
    )
    result = _run(tmp_path)
    assert result.returncode == 2
    assert "DRIFT AD-PA foo:" in result.stderr


def test_drift_px_detected(tmp_path):
    """AD-PX: spawn_target=false but preamble.md exists in legacy stub dir."""
    _make_minimal_repo(tmp_path)
    # foo is spawn_target=false (default); add preamble.md in stub dir anyway
    (tmp_path / "quoin" / "skills" / "foo" / "preamble.md").write_text(
        "# preamble\n", encoding="utf-8"
    )
    result = _run(tmp_path)
    assert result.returncode == 2
    assert "DRIFT AD-PX foo:" in result.stderr


def test_drift_iv_detected(tmp_path):
    """AD-IV: ADAPTER_FOO_SRC= missing from install.sh."""
    _make_minimal_repo(tmp_path)
    install_path = tmp_path / "quoin" / "install.sh"
    content = install_path.read_text(encoding="utf-8")
    # Remove the ADAPTER_FOO_SRC= line
    new_content = "\n".join(
        line for line in content.splitlines()
        if "ADAPTER_FOO_SRC=" not in line
    )
    install_path.write_text(new_content, encoding="utf-8")
    result = _run(tmp_path)
    assert result.returncode == 2
    assert "DRIFT AD-IV foo:" in result.stderr


def test_drift_ie_detected(tmp_path):
    """AD-IE: if/elif branch for 'foo' removed from install.sh entirely."""
    _make_minimal_repo(tmp_path)
    install_path = tmp_path / "quoin" / "install.sh"
    content = install_path.read_text(encoding="utf-8")
    # Remove the elif branch for foo entirely
    new_content = "\n".join(
        line for line in content.splitlines()
        if '"foo"' not in line and "ADAPTER_FOO_SRC" not in line
    )
    install_path.write_text(new_content, encoding="utf-8")
    result = _run(tmp_path)
    assert result.returncode == 2
    assert "DRIFT AD-IE foo:" in result.stderr


def test_drift_io_detected(tmp_path):
    """AD-IO: ADAPTER_FOO_SRC= placed after the for-loop start."""
    _make_minimal_repo(tmp_path)
    install_path = tmp_path / "quoin" / "install.sh"
    # Rearrange so ADAPTER_FOO_SRC= comes after the for-loop
    reordered = (
        "#!/usr/bin/env bash\n"
        'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"\n'
        "\n"
        'for skill_dir in "$SCRIPT_DIR/skills"/*/; do\n'
        '    skill_name=$(basename "$skill_dir")\n'
        '    if [ "$skill_name" = "capture_insight" ]; then\n'
        '        cp "$ADAPTER_CAPTURE_INSIGHT_SRC" "$skill_dir/SKILL.md"\n'
        '    elif [ "$skill_name" = "foo" ]; then\n'
        '        cp "$ADAPTER_FOO_SRC" "$skill_dir/SKILL.md"\n'
        "    fi\n"
        "done\n"
        "\n"
        '# Preflight after for-loop (wrong position)\n'
        'ADAPTER_FOO_SRC="$SCRIPT_DIR/adapters/claude/skills/foo/SKILL.md"\n'
    )
    install_path.write_text(reordered, encoding="utf-8")
    result = _run(tmp_path)
    assert result.returncode == 2
    assert "DRIFT AD-IO foo:" in result.stderr


# ---------------------------------------------------------------------------
# JSON output tests
# ---------------------------------------------------------------------------

def test_json_output_is_valid(tmp_path):
    """--json flag produces valid JSON with a 'violations' key."""
    _make_minimal_repo(tmp_path)
    # Delete core doc to force a violation
    (tmp_path / "quoin" / "core" / "skills" / "foo.md").unlink()
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo-root", str(tmp_path), "--json"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    parsed = json.loads(result.stdout)
    assert "violations" in parsed
    assert isinstance(parsed["violations"], list)


def test_json_output_contains_invariant_and_skill_fields(tmp_path):
    """Each violation object has 'invariant', 'skill', and 'detail' keys."""
    _make_minimal_repo(tmp_path)
    (tmp_path / "quoin" / "core" / "skills" / "foo.md").unlink()
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo-root", str(tmp_path), "--json"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    parsed = json.loads(result.stdout)
    assert len(parsed["violations"]) > 0
    for v in parsed["violations"]:
        assert "invariant" in v, f"Missing 'invariant' key in violation: {v}"
        assert "skill" in v, f"Missing 'skill' key in violation: {v}"
        assert "detail" in v, f"Missing 'detail' key in violation: {v}"


# ---------------------------------------------------------------------------
# Error-handling tests
# ---------------------------------------------------------------------------

def test_unreadable_manifest_returns_65():
    """Non-existent manifest path causes exit 65 (DATA error)."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--manifest", "/nonexistent/path/skills.json"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 65


def test_bad_cli_args_return_64():
    """Unknown CLI flag causes exit 64 (USAGE error)."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--frobnicate"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 64


def test_validator_handles_missing_optional_fields(tmp_path):
    """Skills.json entries without section_0/spawn_target default to false; no exception raised."""
    _make_minimal_repo(tmp_path)
    # Remove optional fields from manifest
    manifest_path = tmp_path / "quoin" / "core" / "workflow" / "skills.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    for skill in data["skills"]:
        skill.pop("section_0", None)
        skill.pop("spawn_target", None)
    manifest_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    result = _run(tmp_path)
    # Should pass (defaults to false for both, which matches the minimal adapter)
    assert result.returncode == 0, (
        f"Validator raised with missing optional fields (exit {result.returncode}):\n{result.stderr}"
    )


# ---------------------------------------------------------------------------
# IVG-107 T-07: AD-WR / AD-WD / AD-PK / AD-CX tests
# ---------------------------------------------------------------------------

# Minimal importlib delegation shim body (used to build synthetic wrappers)
_SHIM_BODY = (
    '"""Wrapper."""\n'
    "import importlib.util\n"
    "import sys\n"
    "from pathlib import Path\n\n"
    '_CORE_PATH = Path(__file__).resolve().parents[1] / "core" / "scripts" / "bar.py"\n'
    '_SPEC = importlib.util.spec_from_file_location("_core_bar", _CORE_PATH)\n'
    "_CORE = importlib.util.module_from_spec(_SPEC)\n"
    "assert _SPEC.loader is not None\n"
    "_SPEC.loader.exec_module(_CORE)\n\n"
    "if __name__ == '__main__':\n"
    "    import sys; sys.exit(_CORE.main())\n"
)

# Minimal core script content (just needs to exist as a .py file)
_CORE_SCRIPT_BODY = '"""Core script stub."""\ndef main():\n    return 0\n'


def _make_minimal_repo_with_core_script(tmp_path: Path, wrapper_content: str = "") -> Path:
    """Extend a minimal repo with a synthetic core script (bar.py) and optional wrapper.

    The minimal repo has skill 'foo' with all files in place. This helper additionally
    creates quoin/core/scripts/bar.py (a synthetic core script) and optionally
    quoin/scripts/bar.py (the wrapper) with the given content.

    If wrapper_content is empty string, the wrapper file is NOT created (triggers AD-WR).
    """
    _make_minimal_repo(tmp_path)

    # Create synthetic core/scripts/ directory with bar.py
    core_scripts = tmp_path / "quoin" / "core" / "scripts"
    core_scripts.mkdir(parents=True, exist_ok=True)
    (core_scripts / "bar.py").write_text(_CORE_SCRIPT_BODY, encoding="utf-8")

    # Create wrapper directory
    scripts_dir = tmp_path / "quoin" / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)

    if wrapper_content:
        (scripts_dir / "bar.py").write_text(wrapper_content, encoding="utf-8")

    return tmp_path


def test_drift_wr_detected(tmp_path):
    """AD-WR: core script bar.py exists but has no wrapper in quoin/scripts/ -> AD-WR fires."""
    _make_minimal_repo_with_core_script(tmp_path, wrapper_content="")  # no wrapper
    result = _run(tmp_path)
    assert result.returncode == 2
    assert "DRIFT AD-WR bar.py:" in result.stderr


def test_drift_wd_missing_shim(tmp_path):
    """AD-WD: wrapper exists but lacks spec_from_file_location -> AD-WD fires.

    Uses a synthetic core script + matching wrapper in tmp_path (not the live repo).
    """
    # Wrapper with no importlib shim at all
    wrapper_without_shim = (
        '"""Bad wrapper: no importlib delegation."""\n'
        "import sys\n\n"
        "def main():\n"
        "    print('direct implementation — not a shim')\n"
    )
    _make_minimal_repo_with_core_script(tmp_path, wrapper_content=wrapper_without_shim)
    result = _run(tmp_path)
    assert result.returncode == 2
    assert "DRIFT AD-WD bar.py:" in result.stderr
    assert "spec_from_file_location" in result.stderr


def test_drift_wd_has_toplevel_def(tmp_path):
    """AD-WD: wrapper has spec_from_file_location but also defines a top-level def -> AD-WD fires.

    Uses a synthetic core script + matching wrapper in tmp_path (not the live repo).
    """
    # Wrapper with the importlib shim BUT also defines a top-level function
    wrapper_with_shim_and_def = (
        '"""Semi-bad wrapper: has shim but also defines a function."""\n'
        "import importlib.util\n"
        "import sys\n"
        "from pathlib import Path\n\n"
        '_CORE_PATH = Path(__file__).resolve().parents[1] / "core" / "scripts" / "bar.py"\n'
        '_SPEC = importlib.util.spec_from_file_location("_core_bar", _CORE_PATH)\n'
        "_CORE = importlib.util.module_from_spec(_SPEC)\n"
        "assert _SPEC.loader is not None\n"
        "_SPEC.loader.exec_module(_CORE)\n\n"
        "# This top-level def violates AD-WD\n"
        "def helper():\n"
        "    return 42\n"
    )
    _make_minimal_repo_with_core_script(tmp_path, wrapper_content=wrapper_with_shim_and_def)
    result = _run(tmp_path)
    assert result.returncode == 2
    assert "DRIFT AD-WD bar.py:" in result.stderr
    # The specific complaint is about the top-level def, not the missing shim
    assert "top-level" in result.stderr


def test_drift_pk_detected(tmp_path):
    """AD-PK: pyproject.toml missing a required force-include key -> AD-PK fires."""
    _make_minimal_repo(tmp_path)
    # Write a pyproject.toml that is missing 'quoin/core'
    pyproject_content = (
        "[tool.hatch.build.targets.wheel.force-include]\n"
        '"quoin/scripts" = "src/quoin/data/scripts"\n'
        '"quoin/skills" = "src/quoin/data/skills"\n'
        '"quoin/adapters/claude" = "src/quoin/data/adapters/claude"\n'
        '"quoin/adapters/codex" = "src/quoin/data/adapters/codex"\n'
        '"quoin/hooks" = "src/quoin/data/hooks"\n'
        '"quoin/memory" = "src/quoin/data/memory"\n'
        "# quoin/core intentionally omitted to trigger AD-PK\n"
        "\n"
        "[build-system]\n"
        'requires = ["hatchling"]\n'
    )
    (tmp_path / "pyproject.toml").write_text(pyproject_content, encoding="utf-8")
    result = _run(tmp_path)
    assert result.returncode == 2
    assert "DRIFT AD-PK quoin/core:" in result.stderr


def test_drift_cx_detected(tmp_path):
    """AD-CX: Codex skill README missing for a manifest skill -> AD-CX fires."""
    _make_minimal_repo(tmp_path)
    # Create the codex skills dir (so codex_enabled = True) but leave out foo/README.md
    codex_skills_dir = tmp_path / "quoin" / "adapters" / "codex" / "skills"
    codex_skills_dir.mkdir(parents=True)
    # Create feature-manifest.json (so manifest check passes)
    (tmp_path / "quoin" / "adapters" / "codex" / "feature-manifest.json").write_text(
        '{"skills": ["foo"]}', encoding="utf-8"
    )
    # No foo/README.md — should trigger AD-CX
    result = _run(tmp_path)
    assert result.returncode == 2
    assert "DRIFT AD-CX foo:" in result.stderr


def test_drift_cx_manifest_detected(tmp_path):
    """AD-CX: feature-manifest.json absent or invalid JSON -> AD-CX fires."""
    _make_minimal_repo(tmp_path)
    # Create codex skills dir with foo/README.md (so per-skill check passes)
    foo_codex_dir = tmp_path / "quoin" / "adapters" / "codex" / "skills" / "foo"
    foo_codex_dir.mkdir(parents=True)
    (foo_codex_dir / "README.md").write_text("# Foo Codex README\n", encoding="utf-8")
    # Write an invalid JSON feature-manifest
    (tmp_path / "quoin" / "adapters" / "codex" / "feature-manifest.json").write_text(
        "this is not json {{{", encoding="utf-8"
    )
    result = _run(tmp_path)
    assert result.returncode == 2
    assert "DRIFT AD-CX feature-manifest:" in result.stderr


def test_new_checks_skip_when_absent(tmp_path):
    """AD-WR/AD-WD/AD-PK/AD-CX: none fire on a bare minimal repo without their target dirs/files.

    Validates that all new skip-guards work correctly:
    - No quoin/core/scripts dir -> AD-WR/AD-WD skipped
    - No pyproject.toml -> AD-PK skipped
    - No quoin/adapters/codex/skills dir -> AD-CX skipped
    """
    _make_minimal_repo(tmp_path)
    # The minimal repo has no core/scripts, no pyproject.toml, no codex skills dir
    # Ensure none of the new IDs appear in the output
    result = _run(tmp_path)
    assert result.returncode == 0, (
        f"New checks fired on minimal repo (exit {result.returncode}):\n{result.stderr}"
    )
    for new_id in ("AD-WR", "AD-WD", "AD-PK", "AD-CX"):
        assert new_id not in result.stderr, (
            f"{new_id} unexpectedly fired on minimal repo:\n{result.stderr}"
        )
