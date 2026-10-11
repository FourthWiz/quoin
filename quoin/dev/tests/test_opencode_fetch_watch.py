"""Post-run detection of executable fetches under watched configuration directories."""
from __future__ import annotations

import dataclasses
import os

import pytest

import _opencode_driver_helpers as h
from quoin.opencode_adapter import driver, launch_env

pytestmark = pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")


@pytest.fixture(autouse=True)
def _no_strays(tmp_path):
    yield
    h.cleanup_fakes(tmp_path)
    assert h.stray_pids(tmp_path) == []


def _run(tmp_path, scenario, watch=True, offline=False):
    prepared = h.make_prepared(tmp_path, scenario)
    if offline:
        values = dict(prepared.launch_env.materialize(), npm_config_offline="true")
        prepared = dataclasses.replace(
            prepared, launch_env=launch_env.LaunchEnv(values, prepared.launch_env.redactor)
        )
    if watch:
        base = h.project_of(tmp_path) / ".opencode"
        prepared = dataclasses.replace(prepared, fetch_watch=((str(base), "dot-opencode"),))
    drv = h.make_driver(tmp_path)
    handle, _events = h.run_to_end(drv, prepared)
    return prepared, handle


def test_a_file_written_under_node_modules_fails_the_attempt_and_names_the_layer(tmp_path):
    prepared, handle = _run(tmp_path, "npm_fetch")
    assert (handle.outcome.state, handle.outcome.reason) == ("failed", "executable-fetch-detected")
    record = h.load_record(tmp_path, prepared.run_id)
    assert record["attempts"][0]["executable_fetch_rows"] == ["dot-opencode"]
    assert record["outcome"]["reason"] == "executable-fetch-detected"


def test_the_same_write_is_not_watched_when_installs_are_allowed(tmp_path):
    prepared, handle = _run(tmp_path, "npm_fetch", watch=False)
    assert (handle.outcome.state, handle.outcome.reason) == ("completed", None)
    assert "executable_fetch_rows" not in h.load_record(tmp_path, prepared.run_id)["attempts"][0]


def test_an_empty_node_modules_directory_is_not_a_fetch(tmp_path):
    _prepared, handle = _run(tmp_path, "empty_node_modules")
    assert handle.outcome.state == "completed"


def test_the_offline_aware_fake_writes_no_install_under_an_offline_environment(tmp_path):
    _prepared, handle = _run(tmp_path, "opencode_managed_writes", offline=True)
    assert handle.outcome.state == "completed"
    base = h.project_of(tmp_path) / ".opencode"
    assert (base / "package.json").exists() and not (base / "node_modules").exists()


def test_a_removed_file_and_a_new_symlink_are_not_a_fetch(tmp_path):
    base = tmp_path / "cfg"
    (base / "node_modules" / "pkg").mkdir(parents=True)
    (base / "node_modules" / "pkg" / "index.js").write_text("x", encoding="utf-8")
    baseline = driver._fetch_baseline(((str(base), "global"),))
    (base / "node_modules" / "pkg" / "index.js").unlink()
    os.symlink(str(tmp_path), str(base / "node_modules" / "link"))
    assert driver._fetched_layers(baseline) == set()
    (base / "node_modules" / "pkg" / "new.js").write_text("y", encoding="utf-8")
    assert driver._fetched_layers(baseline) == {"global"}


def test_the_install_without_the_offline_setting_is_detected(tmp_path):
    _prepared, handle = _run(tmp_path, "opencode_managed_writes")
    assert (handle.outcome.state, handle.outcome.reason) == ("failed", "executable-fetch-detected")


def test_a_changed_regular_file_is_a_fetch_by_size_or_by_mtime(tmp_path):
    base = tmp_path / "cfg"
    (base / "node_modules" / "pkg").mkdir(parents=True)
    target = base / "node_modules" / "pkg" / "index.js"
    target.write_text("x", encoding="utf-8")
    baseline = driver._fetch_baseline(((str(base), "global"),))
    assert driver._fetched_layers(baseline) == set()
    target.write_text("longer content", encoding="utf-8")
    assert driver._fetched_layers(baseline) == {"global"}
    baseline = driver._fetch_baseline(((str(base), "global"),))
    stat = target.stat()
    os.utime(str(target), ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    assert driver._fetched_layers(baseline) == {"global"}
