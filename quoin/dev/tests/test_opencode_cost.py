"""Pure cost layer: phase table, sanitizer, run state, usage, price rule, telemetry."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from quoin.opencode_adapter import cost, gate
from quoin.opencode_adapter import events as ev

import _opencode_cost_helpers as ch

SOURCE = ch.SOURCE_DIR
RUN = "oc-20260101T000000Z-abcdef01"


def _core():
    return cost.load_cost_event(SOURCE)


def _vocabulary():
    text = (SOURCE / "CLAUDE.md").read_text(encoding="utf-8")
    line = next(ln for ln in text.splitlines() if ln.startswith("**Phase values:**"))
    return set(re.findall(r"`([a-z-]+)`", line))


# -- phase table ------------------------------------------------------------


def test_phase_table_covers_every_supported_manifest_command():
    manifest = json.loads((SOURCE / "adapters" / "opencode" / "feature-manifest.json").read_text())
    for row in manifest["catalog_entries"]:
        if row["status"] == "supported" and (row.get("opencode") or {}).get("command"):
            assert cost.ledger_phase(row["id"]) is not None, row["id"]


def test_phase_table_values_and_exceptions():
    assert cost.ledger_phase("thorough_plan") == "thorough-plan"
    assert cost.ledger_phase("thorough-plan") == "thorough-plan"
    assert cost.ledger_phase("end_of_task") == "end-of-task"
    assert cost.ledger_phase("continue_work") == "ad-hoc"
    assert cost.ledger_phase("run") == "run-orchestrator"
    assert cost.ledger_phase("revise") is None
    assert cost.ledger_phase("nonsense") is None


def test_every_mapped_phase_is_in_the_shared_vocabulary():
    vocabulary = _vocabulary()
    assert vocabulary
    assert set(cost.LEDGER_PHASES.values()) <= vocabulary


# -- sanitizer --------------------------------------------------------------


@pytest.mark.parametrize("raw, expected", [
    ("a|b", "a_b"),
    ("a\nb", "a_b"),
    ("a\rb", "a_b"),
    ("a\tb", "a_b"),
    ("a   b  c", "a b c"),
    ("a\x00b", "a_b"),
    ("a b", "a_b"),
    ("a b", "a_b"),
    ("", "unknown"),
    ("   ", "unknown"),
    ("x" * 300, "x" * 200),
    ("a\x7fb", "a_b"),
])
def test_sanitize_table(raw, expected):
    assert cost.sanitize(raw) == expected


# -- run state --------------------------------------------------------------


@pytest.mark.parametrize("state, blocked, expected", [
    ("prepared", None, True), ("running", None, True), ("interrupted", None, True),
    ("interrupted", "effect-uncertain", False), ("completed", None, False),
    ("failed", None, False), ("cancelled", None, False), ("awaiting_approval", None, False),
])
def test_is_open_table(state, blocked, expected):
    assert cost.is_open({"state": state, "resume_blocked": blocked}) is expected


@pytest.mark.parametrize("state", ["prepared", "running", "interrupted", "completed"])
def test_superseded_by_is_never_open(state):
    assert cost.is_open({"state": state, "superseded_by": "oc-x"}) is False


def test_resume_blocked_argument_closes_an_interrupted_run():
    record = {"state": "interrupted", "resume_blocked": None}
    assert cost.is_open(record) is True
    assert cost.is_open(record, "checkpoint-invalid") is False


def test_spawned_needs_a_recorded_pid():
    assert cost.spawned({"attempts": [ch.attempt(pid=4242)]}) is True
    assert cost.spawned({"attempts": [ch.attempt(pid=None, state="interrupted")]}) is False
    assert cost.spawned({"attempts": []}) is False


# -- pricing ----------------------------------------------------------------


def _doc(cost_block):
    return {"provider": {"quoin-p": {"models": {"m": {"cost": cost_block}}}}}


def test_model_priced_rules():
    assert cost.model_priced(_doc({"input": 1, "output": 2}), "quoin-p/m") is True
    assert cost.model_priced(_doc({"input": 1}), "quoin-p/m") is False
    assert cost.model_priced(_doc({"input": -1, "output": 2}), "quoin-p/m") is False
    assert cost.model_priced(_doc({"input": "1", "output": 2}), "quoin-p/m") is False
    assert cost.model_priced(_doc({"input": float("inf"), "output": 2}), "quoin-p/m") is False
    assert cost.model_priced(_doc("free"), "quoin-p/m") is False
    assert cost.model_priced({"provider": {"quoin-p": {"models": {"m": {}}}}}, "quoin-p/m") is False
    assert cost.model_priced(_doc({"input": 1, "output": 2}), "bad") is False
    assert cost.model_priced(None, "quoin-p/m") is False


# -- usage ------------------------------------------------------------------


def test_duplicate_part_counts_once_and_revisions_use_the_latest():
    events = [
        ch.usage_event(RUN, 1, "prt_a", input_tokens=10, output_tokens=2),
        ch.usage_event(RUN, 2, "prt_a", input_tokens=40, output_tokens=5, revision=2),
        ch.usage_event(RUN, 3, "prt_b", input_tokens=1, output_tokens=1),
    ]
    usage = cost.usage_from_events(events)
    assert usage["input_tokens"] == 41 and usage["output_tokens"] == 6 and usage["tokens"] == 47


def test_a_part_missing_a_token_field_makes_the_field_and_total_unknown():
    events = [ch.usage_event(RUN, 1, "prt_a", output_tokens=None)]
    usage = cost.usage_from_events(events)
    assert usage["output_tokens"] is None and usage["tokens"] is None
    assert usage["reasons"]["output_tokens"] == "field-unreported"
    assert usage["reasons"]["tokens"] == "field-unreported"
    assert usage["input_tokens"] == 10


def test_empty_events_are_unknown_with_a_reason():
    usage = cost.usage_from_events([])
    assert usage["tokens"] is None and usage["cost"] is None
    assert set(usage["reasons"].values()) == {"no-usage-events"}


def test_unreadable_sidecar_is_unknown_with_its_own_reason():
    usage = cost.usage_from_events(None)
    assert usage["tokens"] is None
    assert set(usage["reasons"].values()) == {"sidecar-unreadable"}


@pytest.mark.parametrize("usage, priced, expected", [
    ({"cost": "0.0123", "tokens": 30}, False, (None, "cost-unpriced-model")),
    ({"cost": "0", "tokens": 30}, True, (None, "cost-unpriced-model")),
    ({"cost": "0", "tokens": None}, True, (None, "cost-unpriced-model")),
    ({"cost": "0", "tokens": 0}, True, ("0", None)),
    ({"cost": "0.0123", "tokens": 30}, True, ("0.0123", None)),
    ({"cost": "0.1234567", "tokens": 30}, True, ("0.1234567", None)),
    ({"cost": "0.1234567890123", "tokens": 30}, True, ("0.123456789012", None)),
    ({"cost": "0.0000000000001", "tokens": 30}, True, ("0.000000000001", None)),
    ({"cost": None, "tokens": 30}, True, (None, "cost-unreported")),
    ({"cost": "1.500", "tokens": 5}, True, ("1.5", None)),
])
def test_price_rule_table(usage, priced, expected):
    assert cost.priced_cost(usage, priced) == expected


def test_a_tiny_positive_cost_never_reads_as_free():
    usd, _ = cost.priced_cost({"cost": "0.0000000000001", "tokens": 3}, True)
    assert usd != "0" and float(usd) > 0


# -- attribution and note ---------------------------------------------------


@pytest.mark.parametrize("usd, tokens, text, klass", [
    ("0.0123", 30, "usd=0.0123;tok=30;src=opencode_stream", "resolved"),
    ("0.0123", None, "usd=0.0123;src=opencode_stream", "resolved"),
    (None, 30, "tok=30;src=unresolved", "unresolvable"),
    (None, None, "src=unresolved", "unresolvable"),
])
def test_attribution_forms_classify_through_the_core(usd, tokens, text, klass):
    assert cost.attribution(usd, tokens) == text
    assert _core().classify_attribution(text)[0] == klass


def test_unknown_cost_never_writes_usd_zero():
    for tokens in (None, 0, 30):
        assert "usd=0" not in cost.attribution(None, tokens)


def test_note_counts_non_staged_attempts_from_the_record():
    synth_record = {"attempts": [ch.attempt(1), ch.attempt(2), ch.attempt(3, pid=None, state="staged")]}
    telemetry = cost.build_telemetry(
        dict(synth_record, request={"phase": "plan"}, prepared={}), [], ended_as="COMPLETED", clock=ch.clock)
    assert telemetry["retries"]["attempts"] == 2
    assert cost.note("plan", "COMPLETED", telemetry["retries"]["attempts"]) == (
        "runtime=opencode command=quoin-plan outcome=completed attempts=2 scope=parent-session-only")


def test_note_spells_the_command_with_hyphens_and_the_outcome_with_underscores():
    text = cost.note("thorough_plan", "COMPLETED_UNVERIFIED", 1)
    assert "command=quoin-thorough-plan" in text and "outcome=completed_unverified" in text


# -- telemetry --------------------------------------------------------------

D07_KEYS = {
    "schema", "final", "ended_as", "recorded_at", "provider", "model", "effort", "elapsed", "retries",
    "native", "usage", "cost", "usage_provenance", "unavailable",
}


def _record(**prepared):
    return {
        "run_id": RUN, "task": "demo", "request": {"phase": "plan", "effort": None},
        "prepared": dict(ch.Synth.prepared_summary(), **prepared),
        "attempts": [ch.attempt(1, started=0, ended=5), ch.attempt(2, started=10, ended=30, state="failed")],
    }


def test_telemetry_has_every_key_with_explicit_nulls_and_reasons():
    events = [ch.usage_event(RUN, 1, "prt_f1"), ch.usage_event(RUN, 2, "prt_f2", session="ses_2")]
    t = cost.build_telemetry(_record(), events, ended_as="COMPLETED", clock=ch.clock)
    assert D07_KEYS <= set(t)
    assert t["final"] is True and t["schema"] == 1 and t["ended_as"] == "completed"
    assert t["provider"] == {"id": "p", "native_id": "quoin-p", "reason": None}
    assert t["model"]["effective"] == "quoin-p/some-model"
    assert t["effort"]["requested"] is None and t["effort"]["requested_reason"]
    assert t["effort"]["configured"] == "high" and t["effort"]["variant"] == "quoin-high"
    assert t["effort"]["effective"] == "unknown" and t["effort"]["effective_reason"]
    assert [a["seconds"] for a in t["elapsed"]["attempts"]] == [5, 20]
    assert t["elapsed"]["total_seconds"] == 25 and t["elapsed"]["wall_seconds"] == 30
    assert t["retries"] == {"attempts": 2, "failed_attempts_retried": 0, "interrupted_relaunches": 0}
    assert t["native"]["step_finish_part_ids"] == ["prt_f1", "prt_f2"]
    assert t["native"]["session_ids"] == ["ses_1", "ses_2"] and t["native"]["usage_events"] == 2
    assert t["native"]["first_sequence"] == 1 and t["native"]["last_sequence"] == 2
    assert t["usage"]["tokens"] == 24
    assert t["cost"] == {"usd": None, "usd_reason": "cost-unpriced-model", "priced": False}
    prov = t["usage_provenance"]
    assert prov["child_usage"] is None and prov["child_usage_reason"]
    assert prov["parent_cost_includes_children"] is False
    assert t["unavailable"] == ["child-session-usage", "effective-effort"]
    json.dumps(t)


def test_telemetry_for_a_record_without_prepared_fields_says_not_recorded():
    record = {"run_id": RUN, "request": {"phase": "plan"}, "prepared": {}, "attempts": [ch.attempt(1)]}
    t = cost.build_telemetry(record, [], ended_as="FAILED", clock=ch.clock)
    assert t["provider"]["id"] is None and t["provider"]["reason"] == "not-recorded"
    assert t["model"]["effective"] is None and t["model"]["reason"] == "not-recorded"


def test_unended_attempt_has_a_reason_and_no_seconds():
    record = _record()
    record["attempts"] = [ch.attempt(1, ended=None)]
    t = cost.build_telemetry(record, [], ended_as="FAILED", clock=ch.clock)
    assert t["elapsed"]["attempts"][0] == {"attempt": 1, "seconds": None, "reason": "attempt-not-ended"}
    assert t["elapsed"]["total_seconds"] is None


def test_retry_counts_split_failed_and_interrupted():
    record = _record()
    record["attempts"] = [
        ch.attempt(1, state="failed"), ch.attempt(2, state="interrupted"), ch.attempt(3, state="completed")]
    t = cost.build_telemetry(record, [], ended_as="COMPLETED", clock=ch.clock)
    assert t["retries"] == {"attempts": 3, "failed_attempts_retried": 1, "interrupted_relaunches": 1}


def test_a_priced_model_with_cost_gives_usd_in_the_telemetry():
    events = [ch.usage_event(RUN, 1, "prt_f1", cost="0.5")]
    t = cost.build_telemetry(_record(model_priced=True), events, ended_as="COMPLETED", clock=ch.clock)
    assert t["cost"]["usd"] == "0.5" and t["cost"]["usd_reason"] is None


def test_part_id_cap_sets_the_truncation_flag():
    events = [ch.usage_event(RUN, i, "prt_%04d" % i) for i in range(cost.MAX_PART_IDS + 5)]
    t = cost.build_telemetry(_record(), events, ended_as="COMPLETED", clock=ch.clock)
    assert t["native"]["part_ids_truncated"] is True
    assert len(t["native"]["step_finish_part_ids"]) == cost.MAX_PART_IDS


def test_revised_parts_counts_changed_parts():
    events = [ch.usage_event(RUN, 1, "prt_a"), ch.usage_event(RUN, 2, "prt_a", revision=2, input_tokens=40)]
    t = cost.build_telemetry(_record(), events, ended_as="COMPLETED", clock=ch.clock)
    assert t["native"]["revised_parts"] == 1 and t["native"]["usage_events"] == 2


def test_every_provenance_citation_exists_in_the_compatibility_notes():
    text = (SOURCE / "adapters" / "opencode" / "compatibility.md").read_text(encoding="utf-8")
    for key in cost.PROVENANCE_CITATIONS:
        assert "`key: %s`" % key in text or ("key: %s`" % key) in text, key
    t = cost.build_telemetry(_record(), [], ended_as="COMPLETED", clock=ch.clock)
    assert set(t["usage_provenance"]["citations"]) == set(cost.PROVENANCE_CITATIONS)
