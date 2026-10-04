"""The window store: saved pre-run listings for resumed coordinator runs."""
from __future__ import annotations

import hashlib
import json
import os

import pytest

import _opencode_cost_helpers as ch
import _opencode_gate_helpers as gh
from quoin.opencode_adapter import boundaries, run_hooks, runstore, window


@pytest.fixture
def w(tmp_path, monkeypatch):
    return ch.HookWorld(tmp_path, monkeypatch)


def store(w, tmp_path):
    return window.WindowStore(tmp_path / "state-root", w.root, w.task)


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def test_listing_json_round_trip_of_a_real_listing(w):
    listing = boundaries.take_listing(w.root)
    assert listing.entries
    again = boundaries.listing_from_json(json.loads(json.dumps(boundaries.listing_to_json(listing))))
    assert again == listing


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(version=2),
    lambda d: d.update(entries=[]),
    lambda d: d["entries"].update(bad=["x", 1, 1, None]),
    lambda d: d["entries"].update(bad=["f", "1", 1, None]),
    lambda d: d["entries"].update(bad=["f", 1, 1]),
    lambda d: d.update(truncated="no"),
    lambda d: d.update(live_task_locks="t"),
    lambda d: d.update(repos=["x"]),
    lambda d: d.update(error=3),
])
def test_malformed_listing_data_returns_none(w, mutate):
    data = json.loads(json.dumps(boundaries.listing_to_json(boundaries.take_listing(w.root))))
    mutate(data)
    assert boundaries.listing_from_json(data) is None
    assert boundaries.listing_from_json("nope") is None


def test_bind_after_save_and_load(w, tmp_path):
    s = store(w, tmp_path)
    listing = boundaries.take_listing(w.root)
    assert not s.bind("oc-20260101T000000Z-aaaaaaaa")
    s.save_pending(1, "implement", listing)
    assert s.bind("oc-20260101T000000Z-aaaaaaaa")
    got, writes = s.load("oc-20260101T000000Z-aaaaaaaa")
    assert got == listing and writes == {}
    assert s.note_write("oc-20260101T000000Z-aaaaaaaa", "a/b.json", "x" * 64)
    assert s.load("oc-20260101T000000Z-aaaaaaaa")[1] == {"a/b.json": "x" * 64}
    assert not s.note_write("oc-20260101T000000Z-aaaaaaaa", "../escape", "x")
    s.drop("oc-20260101T000000Z-aaaaaaaa")
    assert s.load("oc-20260101T000000Z-aaaaaaaa") is None
    assert oct(os.stat(s.directory()).st_mode & 0o777) == "0o700"


def test_a_malformed_or_oversized_file_loads_as_none(w, tmp_path, monkeypatch):
    s = store(w, tmp_path)
    run_id = "oc-20260101T000000Z-bbbbbbbb"
    s.save_pending(1, "implement", boundaries.take_listing(w.root))
    s.bind(run_id)
    path = s.directory() / (run_id + ".json")
    path.write_text("{broken")
    assert s.load(run_id) is None
    monkeypatch.setattr(window, "MAX_FILE_BYTES", 10)
    with pytest.raises(window.WindowError):
        s.save_pending(1, "implement", boundaries.take_listing(w.root))


def test_reconcile_accepts_an_unchanged_noted_write_and_rejects_another_change(w):
    rel = ".workflow_artifacts/memory/continuation/t1.json"
    before = boundaries.take_listing(w.root)
    gh.write(w.root / rel, "one\n")
    after = boundaries.take_listing(w.root)
    kept = window.reconcile(before, after, {rel: sha("one\n")}, w.root)
    assert kept.entries[rel] == after.entries[rel]
    gh.write(w.root / rel, "two\n")
    changed = boundaries.take_listing(w.root)
    refused = window.reconcile(before, changed, {rel: sha("one\n")}, w.root)
    assert rel not in refused.entries


def _implement(w, tmp_path, *, store_listing, stray):
    import io

    import _opencode_helpers as helpers
    from quoin.opencode_adapter import install

    assert install.run_install(str(w.root), helpers.SOURCE_DIR, None, False, io.StringIO(), io.StringIO()) == 0
    s = store(w, tmp_path)
    before = boundaries.take_listing(w.root)
    run_id = w.exec_run("implement", writes={})
    if stray:
        gh.write(w.root / ".workflow_artifacts" / "other-task" / "stage-1" / "x.md", "stray\n")
    s.save_pending(1, "implement", before)
    s.bind(run_id)
    loaded = s.load(run_id) if store_listing else None
    listing = loaded[0] if loaded else boundaries.take_listing(w.root)
    out = run_hooks.after_phase_run(
        w.root, w.task, w.result(run_id), mark=w.mark, source_dir=ch.SOURCE_DIR,
        superseded_candidate=run_id, clock=ch.clock, origin="coordinator",
        boundary_before=listing, window_complete=loaded is not None, other_task_policy="violation")
    assert out.entry_recorded
    return w.live("implement")


def test_a_resumed_implement_against_the_saved_listing_is_ok(w, tmp_path):
    assert _implement(w, tmp_path, store_listing=True, stray=False)["boundary"] == "ok"


def test_a_stray_write_during_the_gap_is_a_violation(w, tmp_path):
    assert _implement(w, tmp_path, store_listing=True, stray=True)["boundary"] == "violation"


def test_a_missing_window_file_gives_a_partial_window(w, tmp_path):
    entry = _implement(w, tmp_path, store_listing=False, stray=False)
    assert entry["boundary"] is None and entry["boundary_reason"] == "boundary-window-partial"
