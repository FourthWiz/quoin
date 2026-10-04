"""The manifest's evidence entries name real files that mention the command they
back, and every supported row carries at least one."""
from __future__ import annotations

import copy
from pathlib import Path

import pytest

from quoin.opencode_adapter import manifest

REPO_ROOT = Path(__file__).resolve().parents[3]
SOURCE_DIR = REPO_ROOT / "quoin"


@pytest.fixture(scope="module")
def parts():
    return (
        manifest.load_manifest(SOURCE_DIR), manifest.load_catalog(SOURCE_DIR),
        manifest.read_pinned_version(SOURCE_DIR),
    )


def check(parts, edit=None, root=REPO_ROOT):
    data, catalog, pinned = parts
    data = copy.deepcopy(data)
    if edit is not None:
        edit({r["id"]: r for r in data["catalog_entries"]})
    return manifest.check_manifest(data, catalog, pinned, repo_root=root)


def test_the_real_tree_passes(parts):
    assert check(parts) == []
    assert manifest.check_source_dir(SOURCE_DIR) == []
    assert manifest.repo_root_for(SOURCE_DIR) == REPO_ROOT


def test_a_missing_file_names_the_row(parts):
    errs = check(parts, lambda rows: rows["plan"].update(evidence=["quoin/dev/tests/test_no_such_file.py"]))
    assert len(errs) == 1 and "row 'plan'" in errs[0] and "regular file" in errs[0]


@pytest.mark.parametrize("bad", ["/etc/hosts", "quoin/../quoin/dev/tests/test_opencode_workflow_e2e.py", "C:\\x.py"])
def test_an_absolute_or_escaping_path_is_refused(parts, bad):
    errs = check(parts, lambda rows: rows["discover"].update(evidence=[bad]))
    assert len(errs) == 1 and "row 'discover'" in errs[0] and "repo-relative" in errs[0]


def test_a_file_without_the_command_name_is_refused(parts):
    other = "quoin/dev/tests/test_opencode_manifest.py"
    errs = check(parts, lambda rows: rows["critic"].update(evidence=[other]))
    assert len(errs) == 1 and "row 'critic'" in errs[0] and "quoin-critic" in errs[0]


def test_the_run_row_needs_the_workflow_flag(parts):
    errs = check(parts, lambda rows: rows["run"].update(evidence=["quoin/dev/tests/test_opencode_single_phase_evidence.py"]))
    assert len(errs) == 1 and "row 'run'" in errs[0] and "--workflow" in errs[0]


def test_a_symlink_is_not_a_regular_file(parts, tmp_path):
    (tmp_path / "quoin" / "dev" / "tests").mkdir(parents=True)
    (tmp_path / "quoin" / "dev" / "tests" / "link.py").symlink_to(REPO_ROOT / "quoin/dev/tests/test_opencode_workflow_e2e.py")
    errs = check(parts, lambda rows: rows["plan"].update(evidence=["quoin/dev/tests/link.py"]), root=tmp_path)
    assert any("row 'plan'" in e and "regular file" in e for e in errs)


def test_without_a_repo_root_evidence_is_not_checked(parts):
    data, catalog, pinned = parts
    data = copy.deepcopy(data)
    for row in data["catalog_entries"]:
        if row["id"] == "plan":
            row["evidence"] = ["anything.py"]
    assert manifest.check_manifest(data, catalog, pinned) == []


def test_every_supported_row_has_evidence(parts):
    data = parts[0]
    for row in data["catalog_entries"]:
        if row["status"] == "supported":
            assert row["evidence"], row["id"]
            assert row["live_runtime_evidence"] is False, row["id"]


def test_cost_snapshot_stays_unsupported_with_the_new_reason(parts):
    row = {r["id"]: r for r in parts[0]["catalog_entries"]}["cost_snapshot"]
    assert row["status"] == "unsupported"
    assert row["reason"] == (
        "OpenCode ledger rows carry inline attribution; the snapshot skill still needs an OpenCode cost "
        "resolver and live evidence"
    )
