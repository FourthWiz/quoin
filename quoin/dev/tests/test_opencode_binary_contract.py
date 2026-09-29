"""An optional, offline-by-default check of the generated assets against a
real OpenCode binary, when one happens to be on PATH and pinned to the
version this adapter targets. Every other opencode_adapter test exercises
the generator, installer and doctor against the rendered files directly;
this module is the one place that asks the actual binary to load them.

Never runs in CI (no pytest step invokes it there) and is not part of
`quoin doctor --runtime opencode --smoke`. It collects and skips on any
machine without a matching binary, which is the expected case almost
everywhere this suite runs.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

import pytest

from quoin.opencode_adapter import install, manifest, names

REPO_ROOT = Path(__file__).resolve().parents[3]
SOURCE_DIR = REPO_ROOT / "quoin"

_OPENCODE_BIN = shutil.which("opencode")


def _binary_version() -> Optional[str]:
    if _OPENCODE_BIN is None:
        return None
    try:
        result = subprocess.run(
            [_OPENCODE_BIN, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _pinned_version() -> Optional[str]:
    try:
        return manifest.read_pinned_version(SOURCE_DIR)
    except manifest.ManifestLoadError:
        return None


_BINARY_VERSION = _binary_version()
_PINNED_VERSION = _pinned_version()

_SKIP_REASON = (
    "no 'opencode' binary on PATH"
    if _OPENCODE_BIN is None
    else (
        "'opencode --version' did not report the pinned release "
        f"({_PINNED_VERSION!r}); saw {_BINARY_VERSION!r}"
    )
)

pytestmark = pytest.mark.skipif(
    _OPENCODE_BIN is None
    or _PINNED_VERSION is None
    or _BINARY_VERSION is None
    or _PINNED_VERSION not in _BINARY_VERSION,
    reason=_SKIP_REASON,
)


@pytest.fixture()
def installed_project(tmp_path):
    """A project with the OpenCode adapter's assets installed, and an
    environment that keeps every OpenCode config/cache dir the binary
    might touch under the fixture's own temp HOME — never the real one.
    """
    project_root = tmp_path / "proj"
    project_root.mkdir()
    (project_root / ".git").mkdir()

    home_dir = tmp_path / "home"
    home_dir.mkdir()
    xdg_config_home = tmp_path / "xdg-config"
    xdg_config_home.mkdir()

    out_sink, err_sink = io.StringIO(), io.StringIO()
    code = install.run_install(
        str(project_root), SOURCE_DIR, None, False, out_sink, err_sink,
    )
    assert code == 0, f"fixture install failed:\n{out_sink.getvalue()}\n{err_sink.getvalue()}"

    env = {
        **os.environ,
        "HOME": str(home_dir),
        "XDG_CONFIG_HOME": str(xdg_config_home),
        "OPENCODE_DISABLE_AUTOUPDATE": "1",
    }
    return project_root, env


def _run_opencode(args, cwd, env, timeout=60):
    return subprocess.run(
        [_OPENCODE_BIN, *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_binary_sees_the_generated_skills_and_agents(installed_project):
    """`opencode debug skill` lists every generated quoin-* skill.

    Prints one JSON object per discovered skill (cli/cmd/debug/skill.ts).
    """
    project_root, env = installed_project
    result = _run_opencode(["debug", "skill"], project_root, env)
    assert result.returncode == 0, result.stderr

    skill_names = set()
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        record = json.loads(line)
        name = record.get("name")
        if isinstance(name, str) and name.startswith(names.PREFIX):
            skill_names.add(name)

    expected = {
        entry["opencode"]["skill"]
        for entry in manifest.load_manifest(SOURCE_DIR)["catalog_entries"]
        if entry["status"] == "supported"
    }
    assert expected == skill_names, (skill_names, expected)
    assert len(skill_names) == 11


def test_binary_sees_the_generated_agents(installed_project):
    """`opencode agent list` lists one agent per Quoin role (cli/cmd/agent.ts)."""
    project_root, env = installed_project
    result = _run_opencode(["agent", "list"], project_root, env)
    assert result.returncode == 0, result.stderr

    agent_names = {
        line.strip().split()[0]
        for line in result.stdout.splitlines()
        if line.strip().startswith(names.PREFIX)
    }
    expected_roles = manifest.load_manifest(SOURCE_DIR)["roles"].keys()
    expected = {names.role_agent_name(role) for role in expected_roles}
    assert expected == agent_names, (agent_names, expected)
    assert len(agent_names) == 8


def test_binary_resolves_the_instruction_document(installed_project):
    """`opencode debug config` resolves `instructions` to the rendered doc
    (config/config.ts loads project config and instructions in the same
    pass debug/config.ts prints).
    """
    project_root, env = installed_project
    result = _run_opencode(["debug", "config"], project_root, env)
    assert result.returncode == 0, result.stderr

    config = json.loads(result.stdout)
    instructions = config.get("instructions") or []
    if isinstance(instructions, str):
        instructions = [instructions]
    instructions_path = str(project_root / ".opencode" / "quoin" / "instructions.md")
    assert any(
        entry == instructions_path or entry.endswith("quoin/instructions.md")
        for entry in instructions
    ), instructions


@pytest.mark.parametrize("golden_name", ["work", "work-variants"])
def test_binary_keeps_the_compiled_security_keys(tmp_path, golden_name):
    """A compiled configuration pointed at through `OPENCODE_CONFIG` keeps its
    provider allowlist, model pins, small model, title agent, sharing switch
    and policy list after the binary has merged and decoded it. Project
    config is disabled and the placeholder variables carry dummy values, so
    nothing outside the fixture influences the result."""
    golden = SOURCE_DIR / "adapters" / "opencode" / "fixtures" / "compiled" / ("%s.opencode.json" % golden_name)
    expected = json.loads(golden.read_text(encoding="utf-8"))
    config_file = tmp_path / "opencode.json"
    config_file.write_bytes(golden.read_bytes())
    workdir = tmp_path / "empty"
    workdir.mkdir()
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    xdg_config_home = tmp_path / "xdg-config"
    xdg_config_home.mkdir()
    env = {
        **os.environ,
        "HOME": str(home_dir),
        "XDG_CONFIG_HOME": str(xdg_config_home),
        "OPENCODE_DISABLE_AUTOUPDATE": "1",
        "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
        "OPENCODE_CONFIG": str(config_file),
        "QUOIN_CORP_GW_API_KEY": "dummy-value-one",
        "QUOIN_CORP_GW_B_API_KEY": "dummy-value-two",
    }
    result = _run_opencode(["debug", "config"], workdir, env)
    assert result.returncode == 0, result.stderr

    decoded = json.loads(result.stdout)
    assert decoded["enabled_providers"] == expected["enabled_providers"]
    assert set(expected["provider"]) <= set(decoded["provider"])
    assert decoded["model"] == expected["model"]
    assert decoded["small_model"] == expected["small_model"]
    assert decoded["agent"]["title"]["model"] == expected["agent"]["title"]["model"]
    assert decoded["share"] == expected["share"]
    assert decoded["experimental"]["policies"] == expected["experimental"]["policies"]
    for provider_id, provider in expected["provider"].items():
        assert decoded["provider"][provider_id]["whitelist"] == provider["whitelist"]
        for model_id, model in provider["models"].items():
            kept = decoded["provider"][provider_id]["models"][model_id]
            for variant_name, variant in model.get("variants", {}).items():
                assert kept["variants"][variant_name] == variant
    for agent_name, agent in expected["agent"].items():
        if "variant" in agent:
            assert decoded["agent"][agent_name]["variant"] == agent["variant"]
