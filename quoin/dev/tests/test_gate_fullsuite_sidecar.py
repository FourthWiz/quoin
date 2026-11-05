"""IVG-249 stage-3 T-02: unit tests for gate_fullsuite_sidecar.py.

Fixtures build real throwaway git repos under tmp_path. No test asserts
exit 0 on any degraded path — every non-zero `check` exit must mean re-run.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
CORE_SCRIPT = REPO_ROOT / "quoin" / "core" / "scripts" / "gate_fullsuite_sidecar.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("_test_gate_fullsuite_sidecar", CORE_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


sidecar = _load_module()


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "-c", "user.email=t@example.com", "-c", "user.name=Test", "commit", "-q", "--allow-empty", "-m", "init")
    return path


def _record(
    project_root: Path, rc: int, kr_exit: int, task_profile: str = "large",
    gate_phase: str | None = "post-implement",
) -> tuple[int, dict]:
    # gate_phase defaults to "post-implement" (review round 1 MAJOR fix (b),
    # IVG-249 S-03): `check`'s new provenance guard rejects any sidecar whose
    # gate_phase isn't "post-implement"/"post-review", so every existing
    # happy-path/degraded-path case here must record a valid one to keep
    # exercising its OWN reason, not fall through to `no-provenance`.
    argv = [
        "record", "--project-root", str(project_root),
        "--rc", str(rc), "--known-red-exit", str(kr_exit),
        "--task-profile", task_profile, "--format", "json",
    ]
    if gate_phase is not None:
        argv += ["--gate-phase", gate_phase]
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = sidecar.main(argv)
    out = buf.getvalue().strip()
    data = json.loads(out) if out else {}
    return code, data


def _check(project_root: Path, sidecar_path: Path | None = None) -> tuple[int, dict]:
    argv = ["check", "--project-root", str(project_root), "--format", "json"]
    if sidecar_path is not None:
        argv += ["--sidecar", str(sidecar_path)]
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = sidecar.main(argv)
    out = buf.getvalue().strip()
    data = json.loads(out) if out else {}
    return code, data


def _cache_dir(project_root: Path) -> Path:
    return project_root / ".workflow_artifacts" / "cache"


# (a) clean repo, rc=0, kr_exit=0, task_profile=large -> PASS
def test_record_clean_large_all_green(tmp_path):
    _init_repo(tmp_path)
    code, data = _record(tmp_path, rc=0, kr_exit=0, task_profile="large")
    assert code == 0
    assert data["verdict"] == "PASS"
    files = list(_cache_dir(tmp_path).glob("*.freshness.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text())
    assert payload["all_clean"] is True
    assert payload["verdict"] == "PASS"


# (b) rc=1, kr_exit=0, task_profile=large -> PASS (known-red-downgraded run)
def test_record_downgraded_large_still_pass(tmp_path):
    _init_repo(tmp_path)
    code, data = _record(tmp_path, rc=1, kr_exit=0, task_profile="large")
    assert code == 0
    assert data["verdict"] == "PASS"


# (c) rc=1, kr_exit=1 (net-new failure) -> FAIL
def test_record_net_new_failure_fails(tmp_path):
    _init_repo(tmp_path)
    code, data = _record(tmp_path, rc=1, kr_exit=1, task_profile="large")
    assert code == 0
    assert data["verdict"] == "FAIL"


# (d) kr_exit=3 (UNRECONCILED, IVG-166) -> FAIL
def test_record_unreconciled_fails(tmp_path):
    _init_repo(tmp_path)
    code, data = _record(tmp_path, rc=0, kr_exit=3, task_profile="large")
    assert code == 0
    assert data["verdict"] == "FAIL"


# (e) kr_exit=2 (malformed manifest) -> FAIL
def test_record_malformed_manifest_fails(tmp_path):
    _init_repo(tmp_path)
    code, data = _record(tmp_path, rc=0, kr_exit=2, task_profile="large")
    assert code == 0
    assert data["verdict"] == "FAIL"


# (f) record on a dirty tree -> all_clean false; a following check -> exit 1 dirty-at-record
def test_record_dirty_tree_then_check_dirty_at_record(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "untracked.txt").write_text("x")
    code, data = _record(tmp_path, rc=0, kr_exit=0, task_profile="large")
    assert code == 0
    assert data["verdict"] == "PASS"
    files = list(_cache_dir(tmp_path).glob("*.freshness.json"))
    payload = json.loads(files[0].read_text())
    assert payload["all_clean"] is False

    code, data = _check(tmp_path)
    assert code == 1
    assert data["reason"] == "dirty-at-record"


# (g) check after a new commit -> exit 1 sha-mismatch
def test_check_after_new_commit_sha_mismatch(tmp_path):
    _init_repo(tmp_path)
    code, _ = _record(tmp_path, rc=0, kr_exit=0, task_profile="large")
    assert code == 0
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "second")

    code, data = _check(tmp_path)
    assert code == 1
    assert data["reason"] == "sha-mismatch"


# (h) check after adding an untracked file post-record -> exit 1 dirty-now
def test_check_after_untracked_file_dirty_now(tmp_path):
    _init_repo(tmp_path)
    code, _ = _record(tmp_path, rc=0, kr_exit=0, task_profile="large")
    assert code == 0
    (tmp_path / "new_untracked.txt").write_text("x")

    code, data = _check(tmp_path)
    assert code == 1
    assert data["reason"] == "dirty-now"


# (i) check happy path -> exit 0, reuse: true
# NOTE: the repo lives in a child dir of tmp_path (mirroring production, where
# .workflow_artifacts/ sits at the project root, separate from the git repo(s)
# under it) — writing the sidecar directly into a git repo would itself dirty
# that repo's tree.
def test_check_happy_path_reuse_true(tmp_path):
    _init_repo(tmp_path / "repo")
    code, _ = _record(tmp_path, rc=0, kr_exit=0, task_profile="large")
    assert code == 0

    code, data = _check(tmp_path)
    assert code == 0
    assert data["reuse"] is True
    assert "sha12" in data and len(data["sha12"]) == 12
    assert data["repos"] == 1


# (j) QUOIN_DISABLE_FULLSUITE_REUSE=1 on an otherwise-reusable state -> exit 1 disabled-by-env
def test_check_disabled_by_env(tmp_path, monkeypatch):
    _init_repo(tmp_path)
    code, _ = _record(tmp_path, rc=0, kr_exit=0, task_profile="large")
    assert code == 0

    monkeypatch.setenv("QUOIN_DISABLE_FULLSUITE_REUSE", "1")
    code, data = _check(tmp_path)
    assert code == 1
    assert data["reason"] == "disabled-by-env"


# (k) no sidecar present -> exit 1 no-sidecar (never exit 0)
def test_check_no_sidecar(tmp_path):
    _init_repo(tmp_path)
    code, data = _check(tmp_path)
    assert code == 1
    assert data["reason"] == "no-sidecar"


# (l) newest-by-recorded_at: older recorded_at given newer mtime -> selection must key on
# recorded_at, never mtime ("mtimes never trusted").
def test_newest_sidecar_selects_by_recorded_at_not_mtime(tmp_path):
    _init_repo(tmp_path)
    cache_dir = _cache_dir(tmp_path)
    cache_dir.mkdir(parents=True, exist_ok=True)

    older = cache_dir / "gate-fullsuite-old.freshness.json"
    newer = cache_dir / "gate-fullsuite-new.freshness.json"
    older.write_text(json.dumps({"schema_version": 1, "recorded_at": "2020-01-01T00:00:00Z"}))
    newer.write_text(json.dumps({"schema_version": 1, "recorded_at": "2030-01-01T00:00:00Z"}))

    # Give the OLDER recorded_at file the NEWER mtime.
    os.utime(newer, (1000000000, 1000000000))
    os.utime(older, (2000000000, 2000000000))

    selected = sidecar._newest_sidecar(cache_dir)
    assert selected == newer


# (m) multi-repo: two depth-1 child repos, one dirty -> exit 1
def test_multi_repo_one_dirty_blocks_reuse(tmp_path):
    repo_a = _init_repo(tmp_path / "repo_a")
    repo_b = _init_repo(tmp_path / "repo_b")
    (repo_b / "dirty.txt").write_text("x")

    code, data = _record(tmp_path, rc=0, kr_exit=0, task_profile="large")
    assert code == 0
    assert data["verdict"] == "PASS"

    code, data = _check(tmp_path)
    assert code == 1
    assert data["reason"] == "dirty-at-record"


# (n) unknown schema_version -> exit 1 schema-unsupported
def test_check_unknown_schema_version(tmp_path):
    _init_repo(tmp_path)
    cache_dir = _cache_dir(tmp_path)
    cache_dir.mkdir(parents=True, exist_ok=True)
    sidecar_file = cache_dir / "gate-fullsuite-x.freshness.json"
    sidecar_file.write_text(json.dumps({"schema_version": 99, "recorded_at": "2030-01-01T00:00:00Z"}))

    code, data = _check(tmp_path)
    assert code == 1
    assert data["reason"] == "schema-unsupported"


# (o) task_profile=small, rc=1, kr_exit=0 -> FAIL (regression anchor for size-aware verdict).
# Same assertion with task_profile=medium is covered in the same test fn (both non-large).
def test_record_small_and_medium_downgrade_case_fails(tmp_path):
    _init_repo(tmp_path)
    code, data = _record(tmp_path, rc=1, kr_exit=0, task_profile="small")
    assert code == 0
    assert data["verdict"] == "FAIL"

    code, data = _record(tmp_path, rc=1, kr_exit=0, task_profile="medium")
    assert code == 0
    assert data["verdict"] == "FAIL"


# (p) review round 1 MAJOR fix (b): a sidecar recorded WITHOUT --gate-phase
# (gate_phase: null in the payload — e.g. a hand-authored `record` invocation
# that omits the flag, exactly the AC-6-acceptance-recipe hazard the review
# flagged) must not be honored -> exit 1 no-provenance.
def test_check_rejects_missing_gate_phase(tmp_path):
    _init_repo(tmp_path / "repo")
    code, _ = _record(tmp_path, rc=0, kr_exit=0, task_profile="large", gate_phase=None)
    assert code == 0

    code, data = _check(tmp_path)
    assert code == 1
    assert data["reason"] == "no-provenance"


# (q) a sidecar with an out-of-band gate_phase value (unreachable via the CLI's
# own --gate-phase choices, but reachable via a hand-authored/corrupt sidecar
# file — the same threat model as case (n)'s unknown schema_version) must also
# be rejected -> exit 1 no-provenance.
def test_check_rejects_invalid_gate_phase(tmp_path):
    _init_repo(tmp_path)
    cache_dir = _cache_dir(tmp_path)
    cache_dir.mkdir(parents=True, exist_ok=True)
    sidecar_file = cache_dir / "gate-fullsuite-bogus-phase.freshness.json"
    sidecar_file.write_text(json.dumps({
        "schema_version": 1,
        "recorded_at": "2030-01-01T00:00:00Z",
        "gate_phase": "some-other-phase",
        "verdict": "PASS",
        "all_clean": True,
        "repos": [],
    }))

    code, data = _check(tmp_path)
    assert code == 1
    assert data["reason"] == "no-provenance"


# (r) review round 1 MINOR 1 (fold-in): an empty recorded+current repo set
# ("repos": [] paired with discover_repos also returning []) must not grant
# reuse with zero evidence -> exit 1 no-repos. Distinct from case (k)
# (no-sidecar at all) and case (g)/(m) (a real, non-empty repo-set mismatch).
def test_check_empty_repo_set_rejected(tmp_path):
    # tmp_path itself is never git-init'd, so discover_repos(tmp_path) == [].
    cache_dir = _cache_dir(tmp_path)
    cache_dir.mkdir(parents=True, exist_ok=True)
    sidecar_file = cache_dir / "gate-fullsuite-empty-repos.freshness.json"
    sidecar_file.write_text(json.dumps({
        "schema_version": 1,
        "recorded_at": "2030-01-01T00:00:00Z",
        "gate_phase": "post-implement",
        "verdict": "PASS",
        "all_clean": True,
        "repos": [],
    }))

    code, data = _check(tmp_path)
    assert code == 1
    assert data["reason"] == "no-repos"


def test_derive_verdict_pure_function_matrix():
    assert sidecar.derive_verdict(0, 0, "large") == "PASS"
    assert sidecar.derive_verdict(0, 1, "large") == "PASS"
    assert sidecar.derive_verdict(1, 0, "large") == "FAIL"
    assert sidecar.derive_verdict(0, 0, "small") == "PASS"
    assert sidecar.derive_verdict(0, 1, "small") == "FAIL"
    assert sidecar.derive_verdict(0, 1, "medium") == "FAIL"


@pytest.mark.parametrize("flag", ["--rc", "--known-red-exit"])
def test_empty_exit_code_names_the_portable_capture(tmp_path, capsys, flag):
    # An empty value is what a bash-only PIPESTATUS read yields under zsh.
    argv = ["record", "--project-root", str(tmp_path), "--rc", "1",
            "--known-red-exit", "0", "--task-profile", "medium"]
    argv[argv.index(flag) + 1] = ""
    rc = sidecar.main(argv)
    assert rc == 2
    err = capsys.readouterr().err
    assert "RC=$?" in err and "PIPESTATUS" in err
