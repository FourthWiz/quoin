"""A real prepare turns the profile's classification into the content mode the
translator uses, so a work run never stores prompt or tool text."""
from __future__ import annotations

import json
import os

import pytest

from _opencode_merge_helpers import PROFILE_PERSONAL, PROFILE_WORK
from quoin.opencode_adapter import driver, runstore
from quoin.opencode_adapter import events as ev
from test_opencode_driver_prepare import Setup

pytestmark = pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")

SEED = "SEED-PROMPT-TEXT-0123"
RUN_ID = "oc-20260101T000000Z-0123abcd"


def _translate(prepared):
    obj = {"type": "text", "timestamp": 1, "sessionID": "ses_a",
           "part": {"id": "prt_1", "messageID": "msg_1", "text": SEED}}
    events = ev.translate(obj, run_id=RUN_ID, attempt=1, observed_at="2026-01-01T00:00:00.000Z",
                          redact=lambda text: text, content=driver._logging_content(prepared))
    return json.dumps([e.to_dict() for e in events], sort_keys=True)


def _prepare(tmp_path, monkeypatch, profile, request_profile, project_patch=None):
    env = Setup(tmp_path, monkeypatch, profile=profile)
    if project_patch:
        env.world.project.update(project_patch)
        env.world.write()
    prepared = env.driver().prepare(env.request(profile=request_profile))
    return env, prepared


def test_work_profile_prepares_metadata_and_drops_text(tmp_path, monkeypatch):
    env, prepared = _prepare(tmp_path, monkeypatch, PROFILE_WORK, "work")
    assert prepared.policy["classification"] == "work"
    assert prepared.policy["logging_content"] == "metadata"
    assert driver._logging_content(prepared) == "metadata"
    assert SEED not in _translate(prepared)
    record = runstore.load_record(runstore.store_dir(env.root), prepared.run_id)
    assert "retention_sweep" in record["prepared"]


def test_personal_profile_prepares_summary_and_keeps_text(tmp_path, monkeypatch):
    _env, prepared = _prepare(tmp_path, monkeypatch, PROFILE_PERSONAL, "personal")
    assert prepared.policy["classification"] == "personal"
    assert prepared.policy["logging_content"] == "summary"
    assert SEED in _translate(prepared)


def test_a_project_file_cannot_widen_a_work_profile_to_summary(tmp_path, monkeypatch):
    patch = {"policy": {"logging": {"content": "summary"}}}
    try:
        _env, prepared = _prepare(tmp_path, monkeypatch, PROFILE_WORK, "work", patch)
    except driver.PrepareRefused:
        return  # refusing a widening project file is also a valid outcome
    assert prepared.policy["logging_content"] == "metadata"
    assert SEED not in _translate(prepared)
