"""Name rules and manifest drift checks for the OpenCode adapter.

Expected id sets used by these tests are always local to the test function,
or read from the manifest at test time. This keeps the module free of a
module-level ALL-CAPS collection of skill ids, which the registration
roster census would otherwise pick up and require registering.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from quoin.opencode_adapter import manifest, names
from quoin.opencode_adapter.__main__ import main as opencode_main

REPO_ROOT = Path(__file__).resolve().parents[3]
SOURCE_DIR = REPO_ROOT / "quoin"
SRC_DIR = REPO_ROOT / "src"


def _subprocess_env():
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC_DIR)
    return env


def _run_check_manifest_cli(*extra_args, cwd=REPO_ROOT):
    return subprocess.run(
        [sys.executable, "-m", "quoin.opencode_adapter", "check-manifest", *extra_args],
        cwd=str(cwd),
        env=_subprocess_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_normalize_swaps_underscore_for_hyphen():
    assert names.normalize("end_of_task") == "quoin-end-of-task"
    assert names.normalize("revise-fast") == "quoin-revise-fast"


def test_name_error_rejects_invalid_names():
    assert names.name_error("quoin-a--b") is not None
    assert names.name_error("quoin-Bad") is not None
    assert names.name_error("quoin-x-") is not None
    too_long = "quoin-" + "a" * 60  # 66 chars total
    assert names.name_error(too_long) is not None


def test_name_error_accepts_boundary_length():
    exactly_64 = "quoin-" + "a" * 58
    assert len(exactly_64) == 64
    assert names.name_error(exactly_64) is None


def test_find_collisions_names_both_sources():
    messages = names.find_collisions(
        "command",
        [("quoin-end-of-task", "end_of_task"), ("quoin-end-of-task", "end-of-task")],
    )
    assert len(messages) == 1
    assert "end_of_task" in messages[0]
    assert "end-of-task" in messages[0]


def test_find_collisions_three_or_more_sources_comma_separated():
    messages = names.find_collisions(
        "skill",
        [
            ("quoin-x", "a"),
            ("quoin-x", "b"),
            ("quoin-x", "c"),
        ],
    )
    assert len(messages) == 1
    assert "'a'" in messages[0]
    assert "'b'" in messages[0]
    assert "'c'" in messages[0]


def test_same_name_different_namespaces_is_not_a_collision():
    command_messages = names.find_collisions("command", [("quoin-architect", "architect")])
    agent_messages = names.find_collisions("agent", [("quoin-architect", "architect")])
    assert command_messages == []
    assert agent_messages == []


def test_check_unique_raises_naming_both_sources():
    try:
        names.check_unique(
            "agent",
            [("quoin-gate", "gate"), ("quoin-gate", "gate-role")],
        )
    except names.NameCollisionError as exc:
        assert "gate" in str(exc)
        assert "gate-role" in str(exc)
    else:
        raise AssertionError("expected NameCollisionError")


# --- T-03: drift check against the real tree and synthetic cases ---


def _copy_source_tree(tmp_path):
    dest = tmp_path / "quoin"
    (dest / "adapters" / "opencode").mkdir(parents=True)
    (dest / "core" / "workflow").mkdir(parents=True)
    shutil.copy(
        SOURCE_DIR / "adapters" / "opencode" / "feature-manifest.json",
        dest / "adapters" / "opencode" / "feature-manifest.json",
    )
    shutil.copy(
        SOURCE_DIR / "adapters" / "opencode" / "compatibility.md",
        dest / "adapters" / "opencode" / "compatibility.md",
    )
    shutil.copy(
        SOURCE_DIR / "core" / "workflow" / "skills.json",
        dest / "core" / "workflow" / "skills.json",
    )
    return dest


def _load_manifest_dict(dest):
    path = dest / "adapters" / "opencode" / "feature-manifest.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _write_manifest_dict(dest, data):
    path = dest / "adapters" / "opencode" / "feature-manifest.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _load_catalog_dict(dest):
    path = dest / "core" / "workflow" / "skills.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _write_catalog_dict(dest, data):
    path = dest / "core" / "workflow" / "skills.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def test_check_source_dir_real_tree_is_clean():
    assert manifest.check_source_dir(SOURCE_DIR) == []


def test_bundle_membership_and_counts():
    supported = {
        "architect", "checkpoint", "continue_work", "critic", "discover",
        "end_of_task", "gate", "implement", "plan", "review", "thorough_plan",
    }
    unsupported = {"cleanup", "cost_snapshot", "end_of_day", "revise-fast", "start_of_day"}
    data = manifest.load_manifest(SOURCE_DIR)
    rows = data["catalog_entries"]
    by_status = {}
    for row in rows:
        by_status.setdefault(row["status"], set()).add(row["id"])
    assert by_status["supported"] == supported
    assert by_status["unsupported"] == unsupported
    assert len(by_status["supported"]) == 11
    assert len(by_status.get("documentation-only", set())) == 16
    assert len(by_status["unsupported"]) == 5
    assert all(row["live_runtime_evidence"] is False for row in rows)


def test_manifest_file_is_canonical_json():
    path = SOURCE_DIR / "adapters" / "opencode" / "feature-manifest.json"
    raw = path.read_bytes()
    data = json.loads(raw.decode("utf-8"))
    canonical = (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    assert raw == canonical


def test_drift_added_catalog_entry_no_row(tmp_path):
    dest = _copy_source_tree(tmp_path)
    catalog = _load_catalog_dict(dest)
    catalog["skills"].append(
        {
            "name": "brand_new_entry",
            "phase": "x",
            "effort": "low",
            "user_facing": True,
            "claude_model": "haiku",
            "section_0": False,
            "spawn_target": False,
        }
    )
    _write_catalog_dict(dest, catalog)
    errs = manifest.check_source_dir(dest)
    assert any("brand_new_entry" in e and "missing row" in e for e in errs), errs


def test_drift_orphan_row(tmp_path):
    dest = _copy_source_tree(tmp_path)
    data = _load_manifest_dict(dest)
    data["catalog_entries"].append(
        {
            "id": "zzz_orphan",
            "status": "documentation-only",
            "reason": "synthetic orphan case",
            "target_milestone": "release-hardening",
            "catalog": {"user_facing": True, "spawn_target": False},
            "opencode": {},
            "assets": [],
            "live_runtime_evidence": False,
            "evidence": [],
        }
    )
    _write_manifest_dict(dest, data)
    errs = manifest.check_source_dir(dest)
    assert any("zzz_orphan" in e and "orphan row" in e for e in errs), errs


def test_drift_duplicate_row_id(tmp_path):
    dest = _copy_source_tree(tmp_path)
    data = _load_manifest_dict(dest)
    first = data["catalog_entries"][0]
    data["catalog_entries"].insert(1, dict(first))
    _write_manifest_dict(dest, data)
    errs = manifest.check_source_dir(dest)
    assert any("duplicate row id" in e and first["id"] in e for e in errs), errs


def test_drift_empty_reason(tmp_path):
    dest = _copy_source_tree(tmp_path)
    data = _load_manifest_dict(dest)
    row = next(r for r in data["catalog_entries"] if r["id"] == "architect")
    row["reason"] = ""
    _write_manifest_dict(dest, data)
    errs = manifest.check_source_dir(dest)
    assert any("architect" in e and "empty reason" in e for e in errs), errs


def test_drift_unknown_status(tmp_path):
    dest = _copy_source_tree(tmp_path)
    data = _load_manifest_dict(dest)
    row = next(r for r in data["catalog_entries"] if r["id"] == "architect")
    row["status"] = "maybe"
    _write_manifest_dict(dest, data)
    errs = manifest.check_source_dir(dest)
    assert any("architect" in e and "status" in e for e in errs), errs


def test_drift_unknown_milestone(tmp_path):
    dest = _copy_source_tree(tmp_path)
    data = _load_manifest_dict(dest)
    row = next(r for r in data["catalog_entries"] if r["id"] == "architect")
    row["target_milestone"] = "not-a-real-milestone"
    _write_manifest_dict(dest, data)
    errs = manifest.check_source_dir(dest)
    assert any("architect" in e and "target_milestone" in e for e in errs), errs


def test_drift_none_milestone_on_supported_row(tmp_path):
    dest = _copy_source_tree(tmp_path)
    data = _load_manifest_dict(dest)
    row = next(r for r in data["catalog_entries"] if r["id"] == "architect")
    row["target_milestone"] = "none"
    _write_manifest_dict(dest, data)
    errs = manifest.check_source_dir(dest)
    assert any("architect" in e and "'none'" in e for e in errs), errs


def test_drift_flipped_spawn_target(tmp_path):
    dest = _copy_source_tree(tmp_path)
    data = _load_manifest_dict(dest)
    row = next(r for r in data["catalog_entries"] if r["id"] == "architect")
    row["catalog"]["spawn_target"] = not row["catalog"]["spawn_target"]
    _write_manifest_dict(dest, data)
    errs = manifest.check_source_dir(dest)
    assert any("architect" in e and "spawn_target" in e for e in errs), errs


def test_drift_supported_row_empty_assets(tmp_path):
    dest = _copy_source_tree(tmp_path)
    data = _load_manifest_dict(dest)
    row = next(r for r in data["catalog_entries"] if r["id"] == "architect")
    row["assets"] = []
    _write_manifest_dict(dest, data)
    errs = manifest.check_source_dir(dest)
    assert any("architect" in e and "assets" in e for e in errs), errs


def test_drift_documentation_only_row_with_assets(tmp_path):
    dest = _copy_source_tree(tmp_path)
    data = _load_manifest_dict(dest)
    row = next(r for r in data["catalog_entries"] if r["id"] == "run")
    row["assets"] = ["command", "skill"]
    _write_manifest_dict(dest, data)
    errs = manifest.check_source_dir(dest)
    assert any("run" in e and "assets" in e for e in errs), errs


def test_drift_wrong_command_name(tmp_path):
    dest = _copy_source_tree(tmp_path)
    data = _load_manifest_dict(dest)
    row = next(r for r in data["catalog_entries"] if r["id"] == "architect")
    row["opencode"]["command"] = "quoin-not-architect"
    _write_manifest_dict(dest, data)
    errs = manifest.check_source_dir(dest)
    assert any("architect" in e and "opencode.command" in e for e in errs), errs


def test_drift_unknown_agent_role(tmp_path):
    dest = _copy_source_tree(tmp_path)
    data = _load_manifest_dict(dest)
    row = next(r for r in data["catalog_entries"] if r["id"] == "architect")
    row["opencode"]["agent_role"] = "not-a-role"
    _write_manifest_dict(dest, data)
    errs = manifest.check_source_dir(dest)
    assert any("architect" in e and "agent_role" in e for e in errs), errs


def test_drift_version_mismatch_after_editing_compat_pin(tmp_path):
    dest = _copy_source_tree(tmp_path)
    compat_path = dest / "adapters" / "opencode" / "compatibility.md"
    text = compat_path.read_text(encoding="utf-8")
    text = text.replace("Version: 1.18.32", "Version: 1.18.33")
    compat_path.write_text(text, encoding="utf-8")
    errs = manifest.check_source_dir(dest)
    assert any("opencode_version" in e for e in errs), errs


def test_drift_forced_collision_names_both_ids(tmp_path):
    dest = _copy_source_tree(tmp_path)
    catalog = _load_catalog_dict(dest)
    catalog["skills"].append(
        {
            "name": "end-of-task",
            "phase": "close",
            "effort": "low",
            "user_facing": True,
            "claude_model": "sonnet",
            "section_0": True,
            "spawn_target": False,
        }
    )
    _write_catalog_dict(dest, catalog)
    data = _load_manifest_dict(dest)
    data["catalog_entries"].append(
        {
            "id": "end-of-task",
            "status": "documentation-only",
            "reason": "synthetic collision case",
            "target_milestone": "release-hardening",
            "catalog": {"user_facing": True, "spawn_target": False},
            "opencode": {},
            "assets": [],
            "live_runtime_evidence": False,
            "evidence": [],
        }
    )
    data["catalog_entries"].sort(key=lambda r: r["id"])
    _write_manifest_dict(dest, data)
    errs = manifest.check_source_dir(dest)
    collisions = [e for e in errs if "end_of_task" in e and "end-of-task" in e]
    assert collisions, errs


def test_drift_invalid_id(tmp_path):
    dest = _copy_source_tree(tmp_path)
    catalog = _load_catalog_dict(dest)
    catalog["skills"].append(
        {
            "name": "Bad_Id",
            "phase": "x",
            "effort": "low",
            "user_facing": True,
            "claude_model": "haiku",
            "section_0": False,
            "spawn_target": False,
        }
    )
    _write_catalog_dict(dest, catalog)
    errs = manifest.check_source_dir(dest)
    assert any("Bad_Id" in e for e in errs), errs


def test_role_name_collision_via_find_collisions_directly():
    # Two roles cannot literally collide as manifest dict keys (dict keys
    # are unique), so this exercises the primitive check_manifest reuses
    # for agent-namespace collisions directly.
    messages = names.find_collisions(
        "agent",
        [
            (names.role_agent_name("architect"), "architect"),
            (names.role_agent_name("architect"), "planner"),
        ],
    )
    assert messages
    assert "architect" in messages[0] and "planner" in messages[0]


def test_drift_invalid_role_name():
    data = json.loads(json.dumps(manifest.load_manifest(SOURCE_DIR)))
    catalog = manifest.load_catalog(SOURCE_DIR)
    pinned = manifest.read_pinned_version(SOURCE_DIR)
    data["roles"]["Bad Role"] = data["roles"].pop("gate")
    errs = manifest.check_manifest(data, catalog, pinned)
    assert any("Bad Role" in e for e in errs), errs


def test_missing_compat_file_raises_manifest_load_error(tmp_path):
    dest = _copy_source_tree(tmp_path)
    (dest / "adapters" / "opencode" / "compatibility.md").unlink()
    raised = False
    try:
        manifest.check_source_dir(dest)
    except manifest.ManifestLoadError:
        raised = True
    assert raised


# --- T-04: `python -m quoin.opencode_adapter check-manifest` CLI ---


def test_cli_real_tree_exits_0_from_repo_root():
    result = _run_check_manifest_cli("--source-dir", "quoin")
    assert result.returncode == 0, result.stderr
    assert "32" in result.stdout


def test_cli_broken_temp_copy_exits_1_with_offender_id_on_stderr(tmp_path):
    dest = _copy_source_tree(tmp_path)
    data = _load_manifest_dict(dest)
    row = next(r for r in data["catalog_entries"] if r["id"] == "architect")
    row["reason"] = ""
    _write_manifest_dict(dest, data)
    result = _run_check_manifest_cli("--source-dir", str(dest))
    assert result.returncode == 1
    assert "architect" in result.stderr


def test_cli_missing_directory_exits_2():
    result = _run_check_manifest_cli("--source-dir", "/no/such/opencode/source/dir")
    assert result.returncode == 2
    assert result.stderr.strip()


def test_cli_omitted_source_dir_exits_0_via_default_resolution():
    result = _run_check_manifest_cli()
    assert result.returncode == 0, result.stderr
    assert "32" in result.stdout


def test_main_in_process_returns_0_1_2_for_the_three_cases(tmp_path):
    assert opencode_main(["check-manifest", "--source-dir", str(SOURCE_DIR)]) == 0

    dest = _copy_source_tree(tmp_path)
    data = _load_manifest_dict(dest)
    row = next(r for r in data["catalog_entries"] if r["id"] == "architect")
    row["reason"] = ""
    _write_manifest_dict(dest, data)
    assert opencode_main(["check-manifest", "--source-dir", str(dest)]) == 1

    assert opencode_main(["check-manifest", "--source-dir", "/no/such/opencode/source/dir"]) == 2


def test_main_usage_error_raises_system_exit_2():
    try:
        opencode_main(["check-manifest", "--not-a-real-flag"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("expected SystemExit(2) for an unrecognized option")


# --- T-05: third CI step in adapter-check.yml ---


def test_adapter_check_workflow_step_order_and_command_matches_manifest():
    workflow_path = REPO_ROOT / ".github" / "workflows" / "adapter-check.yml"
    text = workflow_path.read_text(encoding="utf-8")
    run_lines = [line.strip()[len("run: "):] for line in text.splitlines() if line.strip().startswith("run:")]
    assert len(run_lines) == 3, run_lines
    assert "validate_adapter_drift.py" in run_lines[0], run_lines
    assert "check_registration.py" in run_lines[1], run_lines
    assert "quoin.opencode_adapter check-manifest" in run_lines[2], run_lines

    manifest_data = manifest.load_manifest(SOURCE_DIR)
    expected_command = manifest_data["validation"]["commands"][0]
    assert run_lines[2] == expected_command


# --- T-07: README support-classification section ---


def test_readme_support_classification_section():
    readme_path = REPO_ROOT / "quoin" / "adapters" / "opencode" / "README.md"
    text = readme_path.read_text(encoding="utf-8")
    heading = "## Support classification"
    start = text.index(heading)
    rest = text[start:]
    next_heading = re.search(r"\n## ", rest[len(heading):])
    section = rest[: len(heading) + next_heading.start()] if next_heading else rest
    assert section.startswith(heading)
    assert section.strip() != heading.strip()

    data = manifest.load_manifest(SOURCE_DIR)
    for status in manifest.STATUSES:
        assert ("`%s`" % status) in section, status
    for milestone in data["milestones"]:
        assert ("`%s`" % milestone) in section, milestone
