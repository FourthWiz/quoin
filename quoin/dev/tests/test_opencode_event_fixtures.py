"""Replay the source-derived runtime event fixtures through the pipeline."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from quoin.opencode_adapter import events as ev

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_DIR = REPO_ROOT / "quoin" / "adapters" / "opencode" / "fixtures" / "runtime-events"
CASES_PATH = FIXTURE_DIR / "cases.json"
RUN_ID = "oc-20260101T000000Z-0123abcd"


def load_cases():
    doc = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    assert doc["schema"] == 1
    return doc["cases"]


def rebased_lines(path: Path, base_ms: int):
    """Raw lines with envelope timestamps moved onto the current clock."""
    out = []
    data = path.read_bytes()
    pieces = data.split(b"\n")
    if pieces and pieces[-1] == b"":
        pieces.pop()
    for piece in pieces:
        try:
            obj = json.loads(piece)
        except ValueError:
            out.append(piece)
            continue
        if isinstance(obj, dict) and isinstance(obj.get("timestamp"), int):
            obj["timestamp"] = base_ms + obj["timestamp"]
            out.append(json.dumps(obj).encode("utf-8"))
        else:
            out.append(piece)
    return out


def observe_jsonl(case):
    base_ms = int(time.time() * 1000)
    pipe = ev.EventPipeline(
        RUN_ID, 1, redact=lambda s: s, observed_clock=lambda: base_ms / 1000.0
    )
    produced = []
    for raw in rebased_lines(FIXTURE_DIR / case["file"], base_ms):
        produced.extend(pipe.feed_line(raw))
    payloads = [e.payload for e in produced]
    approvals = [e for e in produced if e.type is ev.EventType.APPROVAL_REQUIRED]
    usage = ev.usage_totals(produced) if any(e.type is ev.EventType.USAGE for e in produced) else None
    diagnostics = {
        k: v for k, v in pipe.counters.items() if k in ev.DIAGNOSTIC_CODES and v
    }
    return {
        "types": [e.type.value for e in produced],
        "approval": bool(approvals),
        "approval_source": approvals[0].payload.evidence_source if approvals else None,
        "delegation": [
            p.delegation for p in payloads
            if isinstance(p, ev.ProgressPayload) and p.delegation
        ],
        "permission_outcomes": [
            p.permission_outcome for p in payloads
            if isinstance(p, ev.ProgressPayload) and p.permission_outcome
        ],
        "failure_kinds": [
            p.failure_kind for p in payloads if isinstance(p, ev.ErrorPayload)
        ],
        "dropped_duplicates": pipe.counters["dropped_duplicates"],
        "revisions": pipe.counters["revisions"],
        "usage_totals": usage.to_dict() if usage else None,
        "diagnostics": diagnostics,
        "step_summary": ev.summarize_steps(produced).to_dict(),
        "sequences_contiguous": [e.sequence for e in produced]
        == list(range(1, len(produced) + 1)),
    }


def observe_stderr(case):
    text = (FIXTURE_DIR / case["file"]).read_text(encoding="utf-8")
    signals = []
    for line in text.splitlines():
        sig = ev.parse_stderr_line(line)
        if sig is not None:
            signals.append([sig.kind, sig.name, list(sig.patterns), sig.patterns_complete])
    return {"signals": signals}


def observe(case):
    return observe_stderr(case) if case["file"].endswith(".txt") else observe_jsonl(case)


CASES = load_cases()


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_case(case):
    assert case["provenance"] in ("source-derived", "captured")
    assert case["sources"], "every case cites its sources"
    observed = observe(case)
    for key, want in case["expected"].items():
        assert observed[key] == want, (case["name"], key)


def test_census_every_fixture_in_exactly_one_case():
    on_disk = {p.name for p in FIXTURE_DIR.iterdir() if p.name != "cases.json"}
    listed = [c["file"] for c in CASES]
    assert len(listed) == len(set(listed)), "a file appears in two cases"
    assert set(listed) == on_disk


def test_replayed_streams_are_contiguous():
    for case in CASES:
        if case["file"].endswith(".jsonl"):
            assert observe_jsonl(case)["sequences_contiguous"], case["name"]
