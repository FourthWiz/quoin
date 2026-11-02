"""Metadata logging: what the driver writes under work and personal content modes."""
from __future__ import annotations

import dataclasses
import json
import os

import pytest

import _opencode_driver_helpers as h
from quoin.opencode_adapter import driver, events as ev, launch_env, runstore

pytestmark = pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")

SUFFIXES = ("text", "reasoning", "title", "toolerr", "native", "stderr")


@pytest.fixture(autouse=True)
def _no_strays(tmp_path):
    yield
    h.cleanup_fakes(tmp_path)
    assert h.stray_pids(tmp_path) == []


def _run(tmp_path, scenario, mode, **kw):
    prepared = h.make_prepared(tmp_path, scenario, **kw)
    policy = None if mode is None else {"logging_content": mode}
    prepared = dataclasses.replace(prepared, policy=policy)
    drv = h.make_driver(tmp_path)
    handle, events = h.run_to_end(drv, prepared)
    return prepared, handle, events


def _quoin_files(tmp_path, prepared):
    store = h.store_of(tmp_path)
    blobs = []
    for path in sorted(store.iterdir()):
        if path.is_file():
            blobs.append((path.name, path.read_text(encoding="utf-8", errors="replace")))
    return blobs


def _seed_scenario(seed="SEED"):
    return ("seeded_content", {"seed": seed})


def test_metadata_mode_keeps_every_content_site_out_of_quoin_files(tmp_path):
    prepared, handle, _ = _run(tmp_path, _seed_scenario(), "metadata")
    blobs = _quoin_files(tmp_path, prepared)
    assert blobs
    for suffix in SUFFIXES:
        for name, text in blobs:
            assert "SEED-" + suffix not in text, (suffix, name)
    record = h.load_record(tmp_path, prepared.run_id)
    assert record["stderr_tail"] == ""
    assert record["stderr_summary"]["lines"] >= 1
    assert record["stderr_summary"]["signals"] == []
    assert len(record["stderr_summary"]["sha256_prefix"]) == 12
    assert record["attempts"][0]["logging_content"] == "metadata"


def test_a_missing_policy_reads_as_metadata(tmp_path):
    prepared, handle, _ = _run(tmp_path, _seed_scenario(), None)
    for name, text in _quoin_files(tmp_path, prepared):
        assert "SEED-" not in text, name
    assert h.load_record(tmp_path, prepared.run_id)["attempts"][0]["logging_content"] == "metadata"


@pytest.mark.parametrize("suffix", SUFFIXES)
def test_summary_mode_keeps_each_content_site_readable(tmp_path, suffix):
    prepared, handle, _ = _run(tmp_path, _seed_scenario(), "summary")
    joined = "\n".join(text for _, text in _quoin_files(tmp_path, prepared))
    assert "SEED-" + suffix in joined
    assert h.load_record(tmp_path, prepared.run_id)["attempts"][0]["logging_content"] == "summary"


def test_summary_and_metadata_runs_reach_the_same_outcome(tmp_path):
    a, handle_a, _ = _run(tmp_path / "a", _seed_scenario(), "metadata")
    b, handle_b, _ = _run(tmp_path / "b", _seed_scenario(), "summary")
    oa, ob = handle_a.outcome, handle_b.outcome
    assert (oa.state, oa.evidence, oa.reason) == (ob.state, ob.evidence, ob.reason)
    ra = h.load_record(tmp_path / "a", a.run_id)
    rb = h.load_record(tmp_path / "b", b.run_id)
    assert ra["usage_totals"] == rb["usage_totals"]


def test_metadata_approval_patterns_are_digests(tmp_path):
    prepared, handle, events = _run(tmp_path, ("seeded_approval", {"seed": "SEED"}), "metadata")
    assert handle.outcome.state == "awaiting_approval"
    approvals = [e for e in events if e.type is ev.EventType.APPROVAL_REQUIRED]
    assert len(approvals) == 1
    patterns = approvals[0].payload.patterns
    assert patterns and all(ev.parse_metadata_summary(p) for p in patterns)
    for name, text in _quoin_files(tmp_path, prepared):
        assert "SEED-pattern" not in text, name
    assert h.load_record(tmp_path, prepared.run_id)["stderr_summary"]["signals"] == ["approval"]


def test_summary_approval_patterns_stay_readable(tmp_path):
    prepared, handle, events = _run(tmp_path, ("seeded_approval", {"seed": "SEED"}), "summary")
    approvals = [e for e in events if e.type is ev.EventType.APPROVAL_REQUIRED]
    assert any("SEED-pattern" in p for p in approvals[0].payload.patterns)


def test_the_decoded_proxy_password_is_redacted():
    redactor = launch_env.Redactor()
    launch_env._register_proxy_secret(redactor, "http://proxyus%40er:s3cr%40tpassword@proxy.invalid:8080")
    for plain in ("s3cr@tpassword", "proxyus@er", "proxyus@er:s3cr@tpassword"):
        assert plain not in redactor("seen " + plain + " here"), plain


def test_the_decoded_proxy_password_is_absent_from_stderr_tail(tmp_path):
    prepared = h.make_prepared(
        tmp_path, {"attempts": [{"steps": [
            {"do": "stderr", "text": "proxy said s3cr@tpassword"},
            {"do": "emit_fixture", "name": "plain-complete.jsonl"}, {"do": "exit", "code": 0}]}]})
    launch_env._register_proxy_secret(prepared.launch_env.redactor, "http://user:s3cr%40tpassword@proxy.invalid:8080")
    prepared = dataclasses.replace(prepared, policy={"logging_content": "summary"})
    h.run_to_end(h.make_driver(tmp_path), prepared)
    assert "s3cr@tpassword" not in h.load_record(tmp_path, prepared.run_id)["stderr_tail"]
