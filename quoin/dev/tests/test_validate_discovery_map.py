"""
Tests for quoin/core/scripts/validate_discovery_map.py

13 tests covering:
- Example fixture PASS
- Malformed fixtures f01-f06 (one per invariant category)
- Corrupted JSON f07 (exit code 2 via subprocess)
- No third-party imports in validator source
- Pure function contract
- CLI exit codes
- CLI --help
- DM-09 bad path field
"""

import subprocess
import sys
from pathlib import Path

import pytest

# ── Fixture paths ─────────────────────────────────────────────────────────────

_TESTS_DIR = Path(__file__).resolve().parent
_FIXTURES_DIR = _TESTS_DIR / "fixtures" / "discovery-map"
_MALFORMED_DIR = _FIXTURES_DIR / "malformed"

_EXAMPLE = _FIXTURES_DIR / "example-map.json"
_F01 = _MALFORMED_DIR / "f01-missing-required.json"
_F02 = _MALFORMED_DIR / "f02-wrong-type.json"
_F03 = _MALFORMED_DIR / "f03-unknown-toplevel.json"
_F04 = _MALFORMED_DIR / "f04-bad-enum.json"
_F05 = _MALFORMED_DIR / "f05-bad-extensions-value.json"
_F06 = _MALFORMED_DIR / "f06-bad-repo-element.json"
_F07 = _MALFORMED_DIR / "f07-malformed-json.json"

# ── Canonical and compat script paths ────────────────────────────────────────

_REPO_ROOT = _TESTS_DIR.parents[2]  # quoin/ repo root
_CANONICAL_SCRIPT = _REPO_ROOT / "quoin" / "core" / "scripts" / "validate_discovery_map.py"
_COMPAT_SCRIPT = _REPO_ROOT / "quoin" / "scripts" / "validate_discovery_map.py"

# ── Import validator functions ────────────────────────────────────────────────
# Import via importlib so we don't need __init__.py or package installation.

import importlib.util as _ilu

def _load_validator():
    spec = _ilu.spec_from_file_location("_dm_validator", _CANONICAL_SCRIPT)
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

_validator = _load_validator()
validate = _validator.validate
validate_file = _validator.validate_file


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_example_fixture_passes():
    """Example fixture must validate with zero errors."""
    errors = validate_file(_EXAMPLE)
    assert errors == [], f"Expected no errors, got: {errors}"


def test_missing_required_field_fails():
    """f01 (missing 'tasks' key) must produce a DM-02 error."""
    errors = validate_file(_F01)
    assert errors, "Expected at least one error for f01"
    assert any(e.startswith("DM-02:") for e in errors), (
        f"Expected a DM-02: error, got: {errors}"
    )


def test_wrong_type_fails():
    """f02 (schema_version as string '1') must produce a DM-04 error."""
    errors = validate_file(_F02)
    assert errors, "Expected at least one error for f02"
    assert any(e.startswith("DM-04:") for e in errors), (
        f"Expected a DM-04: error, got: {errors}"
    )


def test_unknown_toplevel_fails():
    """f03 (extra 'foo' key at root) must produce a DM-03 error."""
    errors = validate_file(_F03)
    assert errors, "Expected at least one error for f03"
    assert any(e.startswith("DM-03:") for e in errors), (
        f"Expected a DM-03: error, got: {errors}"
    )


def test_bad_enum_fails():
    """f04 (task status 'weird') must produce a DM-06 error."""
    errors = validate_file(_F04)
    assert errors, "Expected at least one error for f04"
    assert any(e.startswith("DM-06:") for e in errors), (
        f"Expected a DM-06: error, got: {errors}"
    )


def test_bad_extensions_value_fails():
    """f05 (extensions.claude is a string, not object) must produce a DM-08 error."""
    errors = validate_file(_F05)
    assert errors, "Expected at least one error for f05"
    assert any(e.startswith("DM-08:") for e in errors), (
        f"Expected a DM-08: error, got: {errors}"
    )


def test_bad_repo_element_fails():
    """f06 (first repo missing head_sha) must produce a DM-07 error."""
    errors = validate_file(_F06)
    assert errors, "Expected at least one error for f06"
    assert any(e.startswith("DM-07:") for e in errors), (
        f"Expected a DM-07: error, got: {errors}"
    )


def test_corrupted_json_exits_2():
    """f07 (truncated/corrupted JSON) must cause the CLI to exit with code 2."""
    result = subprocess.run(
        [sys.executable, str(_CANONICAL_SCRIPT), str(_F07)],
        capture_output=True,
    )
    assert result.returncode == 2, (
        f"Expected exit code 2 for corrupted JSON, got {result.returncode}. "
        f"stdout: {result.stdout!r} stderr: {result.stderr!r}"
    )


def test_validator_no_third_party_imports():
    """Validator source must not import any module outside the stdlib allowlist."""
    _ALLOWED = frozenset({
        "argparse", "json", "os", "re", "sys", "typing", "pathlib",
        "__future__", "dataclasses",
    })
    source = _CANONICAL_SCRIPT.read_text(encoding="utf-8")
    for line in source.splitlines():
        stripped = line.strip()
        # Match top-level import lines only
        if stripped.startswith("import ") and not stripped.startswith("import "):
            pass  # skip continuation lines
        if stripped.startswith("import "):
            # "import foo" or "import foo.bar"
            module = stripped[len("import "):].split()[0].split(".")[0]
            assert module in _ALLOWED, (
                f"Unexpected stdlib import '{module}' in validator. Allowed: {_ALLOWED}"
            )
        elif stripped.startswith("from "):
            # "from foo import bar" or "from foo.bar import baz"
            parts = stripped.split()
            if len(parts) >= 2:
                module = parts[1].split(".")[0]
                assert module in _ALLOWED, (
                    f"Unexpected 'from' import '{module}' in validator. Allowed: {_ALLOWED}"
                )


def test_validator_pure_function():
    """validate({}) must return a list of strings without raising exceptions or doing I/O."""
    result = validate({})
    assert isinstance(result, list), f"Expected list, got {type(result).__name__}"
    for item in result:
        assert isinstance(item, str), f"Expected list of str, got item of type {type(item).__name__}"


def test_cli_exit_codes():
    """CLI must return exit 0 for valid map, exit 1 for invalid, exit 2 for missing file."""
    # (a) Example fixture — expect exit 0
    result_ok = subprocess.run(
        [sys.executable, str(_CANONICAL_SCRIPT), str(_EXAMPLE)],
        capture_output=True,
    )
    assert result_ok.returncode == 0, (
        f"Expected exit 0 for example fixture, got {result_ok.returncode}. "
        f"stdout: {result_ok.stdout!r} stderr: {result_ok.stderr!r}"
    )

    # (b) f01 (missing required field) — expect exit 1
    result_fail = subprocess.run(
        [sys.executable, str(_CANONICAL_SCRIPT), str(_F01)],
        capture_output=True,
    )
    assert result_fail.returncode == 1, (
        f"Expected exit 1 for f01, got {result_fail.returncode}. "
        f"stdout: {result_fail.stdout!r} stderr: {result_fail.stderr!r}"
    )

    # (c) Non-existent path — expect exit 2
    result_missing = subprocess.run(
        [sys.executable, str(_CANONICAL_SCRIPT), "/nonexistent/path/map.json"],
        capture_output=True,
    )
    assert result_missing.returncode == 2, (
        f"Expected exit 2 for missing file, got {result_missing.returncode}. "
        f"stdout: {result_missing.stdout!r} stderr: {result_missing.stderr!r}"
    )


def test_cli_help_exits_zero():
    """Compat wrapper --help must exit 0."""
    result = subprocess.run(
        [sys.executable, str(_COMPAT_SCRIPT), "--help"],
        capture_output=True,
    )
    assert result.returncode == 0, (
        f"Expected exit 0 for --help, got {result.returncode}. "
        f"stdout: {result.stdout!r} stderr: {result.stderr!r}"
    )


def test_bad_path_field_fails():
    """Mutating artifact_roots.workflow_artifacts_path to './foo' must produce a DM-09 error."""
    import json
    with open(_EXAMPLE, "r", encoding="utf-8") as fh:
        map_obj = json.load(fh)
    map_obj["artifact_roots"]["workflow_artifacts_path"] = "./foo"
    errors = validate(map_obj)
    assert errors, "Expected at least one error for bad path field"
    assert any(e.startswith("DM-09:") for e in errors), (
        f"Expected a DM-09: error, got: {errors}"
    )


def test_installer_deploys_validator_next_to_generator():
    """generate_discovery_map.py loads the validator from its own directory on
    every default run, so the installer must deploy both to core/scripts."""
    spec = _ilu.spec_from_file_location(
        "_quoin_installer_vdm_test", _REPO_ROOT / "src" / "quoin" / "installer.py"
    )
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert "generate_discovery_map.py" in mod.CORE_SCRIPTS
    assert "validate_discovery_map.py" in mod.CORE_SCRIPTS
