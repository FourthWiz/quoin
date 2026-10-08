"""cost_ledger_summary.py: totals a task cost ledger, re-pricing ledger rows whose
transcripts are now readable. All tests use a synthetic home tree; the one test
against a real finalized ledger skips when its transcripts are not on this machine."""
from __future__ import annotations

import json
import pathlib
import shutil
import sys

import pytest

SCRIPTS_DIR = pathlib.Path(__file__).parent.parent.parent / "scripts"
CORE_DIR = pathlib.Path(__file__).parent.parent.parent / "core" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(CORE_DIR))
import cost_ledger_summary as cls  # noqa: E402
import cost_from_jsonl as cfj  # noqa: E402
from cost_summary import normalize_total  # noqa: E402

PROJECT = "/fake/project"
SID = "11111111-2222-3333-4444-555555555555"
SID2 = "66666666-7777-8888-9999-000000000000"
AID = "a0123456789abcdef"
AID2 = "afedcba9876543210"
USAGE = {"input_tokens": 1000, "output_tokens": 500,
         "cache_creation_input_tokens": 200, "cache_read_input_tokens": 300}
OPUS_USD = cfj.cost_for_entry("claude-opus-5-5", USAGE)[0]
OPUS_TOK = cfj.cost_for_entry("claude-opus-5-5", USAGE)[1]


def _proj_dir(home):
    return home / ".claude" / "projects" / cfj.project_hash(PROJECT)


def _write_rows(path, model, n=1):
    rows = [{"type": "assistant", "timestamp": "2026-10-08T00:00:00Z",
             "message": {"model": model, "usage": USAGE}} for _ in range(n)]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def _agent(home, sid, aid, model="claude-opus-5-5"):
    _write_rows(_proj_dir(home) / sid / "subagents" / ("agent-%s.jsonl" % aid), model)


def _session(home, sid, model="claude-opus-5-5"):
    _write_rows(_proj_dir(home) / (sid + ".jsonl"), model)


def _ledger(tmp_path, lines):
    p = tmp_path / "task" / "cost-ledger.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# Cost Ledger — t\n\n" + "\n".join(lines) + "\n")
    return p


def _row(uuid, phase="plan", model="opus", attr=""):
    base = "%s | 2026-10-08 | %s | %s | task | note | 0" % (uuid, phase, model)
    return base + (" | " + attr if attr else "")


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


def _run(ledger, home, **kw):
    summary, disp = cls.build_summary(ledger, PROJECT, home)
    return summary, disp


def test_reprice_via_ledger_sid(tmp_path, home):
    _agent(home, SID, AID)
    ledger = _ledger(tmp_path, [
        _row(SID, "run-orchestrator"),
        _row(AID, "plan", attr="tok=999;src=unresolved"),
    ])
    s, _ = _run(ledger, home)
    assert s["repriced_count"] == 1
    assert s["estimated_count"] == 0
    assert s["per_phase"]["plan"]["cost"] == pytest.approx(OPUS_USD, abs=1e-6)


def test_reprice_via_unique_glob(tmp_path, home):
    _agent(home, SID2, AID)  # session not listed in the ledger
    ledger = _ledger(tmp_path, [_row(AID, "plan", attr="tok=999;src=unresolved")])
    s, _ = _run(ledger, home)
    assert s["repriced_count"] == 1
    assert s["resolved_total"] == pytest.approx(OPUS_USD, abs=1e-6)


def test_glob_collision_falls_to_estimate(tmp_path, home):
    _agent(home, SID, AID)
    _agent(home, SID2, AID)
    ledger = _ledger(tmp_path, [_row(AID, "plan", attr="tok=5000;src=unresolved")])
    s, _ = _run(ledger, home)
    assert s["repriced_count"] == 0
    assert s["estimated_count"] == 1
    assert s["estimated_total"] > 0


def test_tok_only_ledger_is_estimated_and_partial(tmp_path, home):
    ledger = _ledger(tmp_path, [_row(AID, "plan", attr="tok=100000;src=unresolved")])
    s, _ = _run(ledger, home)
    assert s["estimated_total"] > 0
    assert s["fallback_used"] is True
    total, partial = normalize_total(s)
    assert total is not None and total > 0
    assert partial is True


def test_row_without_tok_or_transcript_is_unresolvable(tmp_path, home):
    ledger = _ledger(tmp_path, [_row(AID, "plan", attr="src=unresolved")])
    s, _ = _run(ledger, home)
    assert s["unresolvable_count"] == 1
    assert s["estimated_count"] == 0
    assert s["priced_rows"] == 0


def test_inline_resolved_row_precedence(tmp_path, home):
    _agent(home, SID, AID)
    ledger = _ledger(tmp_path, [
        _row(SID, "run-orchestrator"),
        _row(AID, "plan", attr="usd=0.5;tok=10;src=nested_jsonl"),
    ])
    s, _ = _run(ledger, home)
    assert s["repriced_count"] == 0
    assert s["per_phase"]["plan"]["cost"] == pytest.approx(0.5)


def test_legacy_session_rows_cohort_merge(tmp_path, home):
    _session(home, SID)
    _session(home, SID2)
    ledger = _ledger(tmp_path, [
        _row(SID, "plan"), _row(SID, "critic"),   # shared session
        _row(SID2, "implement"),                  # solo session
    ])
    s, _ = _run(ledger, home)
    assert s["shared_sessions_total"] == pytest.approx(OPUS_USD, abs=1e-6)
    assert s["per_phase"]["implement"]["cost"] == pytest.approx(OPUS_USD, abs=1e-6)
    assert "plan" not in s["per_phase"]
    assert s["resolved_total"] == pytest.approx(2 * OPUS_USD, abs=1e-6)


def test_session_with_unknown_model_is_not_has_cost(tmp_path, home):
    _session(home, SID, model="claude-opus-9-9")
    ledger = _ledger(tmp_path, [_row(SID, "plan")])
    s, _ = _run(ledger, home)
    assert s["unresolvable_count"] == 1
    assert s["resolved_total"] == 0.0


def test_agent_id_legacy_row_never_reaches_session_resolver(tmp_path, home, monkeypatch):
    calls = []
    monkeypatch.setattr(cls, "_session_cost", lambda *a: calls.append(a) or (0.0, False))
    ledger = _ledger(tmp_path, [_row(AID, "plan")])
    s, _ = _run(ledger, home)
    assert calls == []
    assert s["unresolvable_count"] == 1


def test_fully_priced_ledger_without_end_of_task_row(tmp_path, home):
    ledger = _ledger(tmp_path, [_row(AID, "plan", attr="usd=1.0;tok=1;src=nested_jsonl")])
    s, _ = _run(ledger, home)
    assert s["phases_excluded"] == ["end-of-task"]
    assert "fallback_used" not in s and "fallback_note" not in s
    total, partial = normalize_total(s)
    assert total == pytest.approx(1.0)
    assert partial is False


def test_end_of_task_row_present_gives_empty_excluded(tmp_path, home):
    ledger = _ledger(tmp_path, [
        _row(AID, "end-of-task", attr="usd=1.0;tok=1;src=nested_jsonl")])
    s, _ = _run(ledger, home)
    assert s["phases_excluded"] == []


def test_off_topic_total_always_zero(tmp_path, home):
    ledger = _ledger(tmp_path, [
        "%s | 2026-10-08 | plan | opus | off-topic | n | 0" % AID,
        _row(AID2, "plan", attr="usd=1.0;tok=1;src=nested_jsonl"),
    ])
    s, _ = _run(ledger, home)
    assert s["off_topic_total"] == 0.0
    assert s["rows_total"] == 1


def test_dry_run_writes_nothing(tmp_path, home, capsys):
    ledger = _ledger(tmp_path, [_row(AID, "plan", attr="usd=1.0;tok=1;src=nested_jsonl")])
    rc = cls.main(["--ledger", str(ledger), "--project-path", PROJECT,
                   "--home", str(home), "--dry-run"])
    assert rc == 0
    assert not (ledger.parent / "cost-summary.json").exists()
    assert json.loads(capsys.readouterr().out)["grand_total"] == pytest.approx(1.0)


def test_writes_summary_and_leaves_ledger_untouched(tmp_path, home):
    ledger = _ledger(tmp_path, [_row(AID, "plan", attr="usd=1.0;tok=1;src=nested_jsonl")])
    before = ledger.read_text()
    assert cls.main(["--ledger", str(ledger), "--project-path", PROJECT,
                     "--home", str(home), "--format", "text"]) == 0
    assert ledger.read_text() == before
    assert json.loads((ledger.parent / "cost-summary.json").read_text())["rows_total"] == 1


def test_finalized_default_out_refused(tmp_path, home, capsys):
    d = tmp_path / "finalized" / "t"
    d.mkdir(parents=True)
    ledger = d / "cost-ledger.md"
    ledger.write_text("# Cost Ledger\n")
    rc = cls.main(["--ledger", str(ledger), "--home", str(home)])
    assert rc == 2
    assert not (d / "cost-summary.json").exists()
    out = tmp_path / "elsewhere.json"
    assert cls.main(["--ledger", str(ledger), "--home", str(home), "--out", str(out)]) == 0
    assert out.exists()


def test_malformed_row_yields_partial_summary_exit_0(tmp_path, home):
    ledger = _ledger(tmp_path, [
        "garbage | only | three",
        _row(AID, "plan", attr="usd=notanumber;src=x"),
        _row(AID2, "plan", attr="usd=1.0;tok=1;src=nested_jsonl"),
    ])
    rc = cls.main(["--ledger", str(ledger), "--project-path", PROJECT,
                   "--home", str(home), "--dry-run"])
    assert rc == 0
    s, _ = _run(ledger, home)
    assert s["unresolvable_count"] == 1
    assert s["resolved_total"] == pytest.approx(1.0)


def test_text_format_prints_projects_dir(tmp_path, home, capsys):
    ledger = _ledger(tmp_path, [_row(AID, "plan", attr="usd=1.0;tok=1;src=nested_jsonl")])
    cls.main(["--ledger", str(ledger), "--project-path", PROJECT, "--home", str(home),
              "--format", "text", "--dry-run"])
    assert "projects_dir: %s" % _proj_dir(home) in capsys.readouterr().out


def test_project_path_drives_hash(tmp_path, home):
    _agent(home, SID, AID)
    ledger = _ledger(tmp_path, [
        _row(SID, "run-orchestrator"), _row(AID, "plan", attr="tok=9;src=unresolved")])
    right, _ = cls.build_summary(ledger, PROJECT, home)
    wrong, _ = cls.build_summary(ledger, "/some/other/project", home)
    assert right["repriced_count"] == 1
    assert wrong["repriced_count"] == 0


REAL_LEDGER = (
    pathlib.Path(__file__).resolve().parents[4] / ".workflow_artifacts" / "finalized"
    / "ivg-283-autonomous-implement-done-resume" / "cost-ledger.md"
)


def _real_ids():
    ids = []
    for line in REAL_LEDGER.read_text().splitlines():
        first = line.split("|")[0].strip()
        if cls._AGENT_ID_RE.match(first) or cls._SESSION_ID_RE.match(first):
            ids.append(first)
    return ids


def _real_available():
    if not REAL_LEDGER.exists():
        return False
    project = str(REAL_LEDGER.resolve().parents[3])
    base = pathlib.Path.home() / ".claude" / "projects" / cfj.project_hash(project)
    for ident in _real_ids():
        if cls._SESSION_ID_RE.match(ident):
            if not (base / (ident + ".jsonl")).exists():
                return False
        else:
            if not list(base.glob("*/subagents/agent-%s.jsonl" % ident)):
                return False
    return True


@pytest.mark.skipif(not _real_available(), reason="IVG-283 transcripts not on this machine")
def test_real_ivg283_ledger_reprices(tmp_path):
    from cost_summary import main as cs_main

    project = str(REAL_LEDGER.resolve().parents[3])
    copy = tmp_path / "cost-ledger.md"
    shutil.copy(str(REAL_LEDGER), str(copy))
    before = (REAL_LEDGER.parent / "cost-summary.json")
    before_bytes = before.read_bytes() if before.exists() else None
    out = tmp_path / "cost-summary.json"
    assert cls.main(["--ledger", str(copy), "--project-path", project,
                     "--out", str(out)]) == 0
    s = json.loads(out.read_text())
    assert s["rows_total"] == 46
    assert s["repriced_count"] == 40
    assert s["estimated_count"] == 0
    assert s["unresolvable_count"] == 0
    assert s["phases_excluded"] == []
    assert s["resolved_total"] > 0
    total, partial = normalize_total(s)
    assert total > 0 and partial is False
    assert cs_main([str(out), "--format", "json"]) == 0
    after_bytes = before.read_bytes() if before.exists() else None
    assert after_bytes == before_bytes
