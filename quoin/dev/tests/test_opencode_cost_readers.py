"""Rows written by the OpenCode cost layer parse unchanged through the shared readers."""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from quoin.opencode_adapter import cost

import _opencode_cost_helpers as ch

SOURCE = ch.SOURCE_DIR
RUN_IDS = {
    "usd": "oc-20270115T080000Z-00000001",
    "tokens": "oc-20270115T080000Z-00000002",
    "neither": "oc-20270115T080000Z-00000003",
    "hostile": "oc-20270115T080000Z-00000004",
}


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ACL = _load("_quoin_cost_reader_acl", SOURCE / "scripts" / "analyze_cost_ledger.py")
DC = _load("_quoin_cost_reader_dc", SOURCE / "scripts" / "dashboard_cost.py")
SM = _load("_quoin_cost_reader_sm", SOURCE / "core" / "scripts" / "spend_monitor.py")


def _line(kind):
    run = RUN_IDS[kind]
    priced = kind == "usd"
    events = [ch.usage_event(run, 1, "prt_f1", cost="0.5")]
    if kind == "neither":
        events = []
    record = {
        "run_id": run, "task": "demo", "request": {"phase": "plan", "effort": None},
        "prepared": dict(ch.Synth.prepared_summary(), model_priced=priced,
                         effective_model="a|b\nc" if kind == "hostile" else "quoin-p/some-model"),
        "attempts": [ch.attempt(1)],
    }
    if kind == "tokens":
        events = [ch.usage_event(run, 1, "prt_f1", cost="0.5")]
    telemetry = cost.build_telemetry(record, events, ended_as="COMPLETED", clock=ch.clock)
    line, reason = cost.format_line(SOURCE, record, telemetry, "COMPLETED", ch.clock)
    assert reason is None
    return line


@pytest.fixture
def ledger(tmp_path):
    task_dir = tmp_path / "project" / ".workflow_artifacts" / "demo"
    task_dir.mkdir(parents=True)
    path = task_dir / "cost-ledger.md"
    path.write_text("# Cost Ledger — demo\n" + "\n".join(_line(k) for k in RUN_IDS) + "\n", encoding="utf-8")
    return path


def test_rows_parse_through_the_core_parser_unchanged():
    core = cost.load_cost_event(SOURCE)
    expected = {
        "usd": ("resolved", 0.5), "tokens": ("unresolvable", None),
        "neither": ("unresolvable", None), "hostile": ("unresolvable", None),
    }
    for kind, run in RUN_IDS.items():
        line = _line(kind)
        event = core.parse_row(line)
        assert event is not None and event.uuid == run and event.category == "task"
        assert core.format_row(event) == line
        assert core.classify_attribution(event.attribution) == expected[kind]
    assert core.parse_row(_line("usd")).attribution == "usd=0.5;tok=12;src=opencode_stream"
    assert core.parse_row(_line("neither")).attribution == "src=unresolved"
    assert len(_line("hostile").split("|")) == 8


def test_the_ledger_analyzer_counts_resolved_dollars_and_unresolvable_rows(ledger, tmp_path):
    project_root = tmp_path / "project"
    rows = ACL.parse_ledger_file(ledger, task_name="demo")
    assert len(rows) == 4
    report = ACL.build_report(rows, project_root, ACL.project_hash(str(project_root)), tmp_path / "empty-home")
    assert report["resolved_total"] == pytest.approx(0.5)
    assert report["unresolvable_count"] == 3
    assert report["total_cost"] == pytest.approx(0.5)


def _provider_rows(ledger):
    return ACL.parse_ledger_file(ledger, task_name="demo")


def test_the_dashboard_provider_accepts_the_rows_without_folding_unresolvable_into_a_total(ledger, tmp_path):
    project_root = tmp_path / "project"
    provider = DC.make_cost_provider(project_root, home=tmp_path / "empty-home")
    result = provider("demo", _provider_rows(ledger))
    assert result is not None and result["mode"] == "usd"
    assert result["usd"] == pytest.approx(0.5) and result["partial"] is True


def test_the_dashboard_provider_with_only_unresolvable_rows_reports_no_dollars(tmp_path):
    task_dir = tmp_path / "project" / ".workflow_artifacts" / "demo"
    task_dir.mkdir(parents=True)
    path = task_dir / "cost-ledger.md"
    path.write_text("# Cost Ledger — demo\n" + _line("tokens") + "\n" + _line("neither") + "\n", encoding="utf-8")
    provider = DC.make_cost_provider(tmp_path / "project", home=tmp_path / "empty-home")
    result = provider("demo", ACL.parse_ledger_file(path, task_name="demo"))
    assert result is None or (result.get("usd") in (None, 0, 0.0) and result["mode"] != "usd")


def test_the_spend_monitor_finds_the_rows_and_reads_the_attribution_column(ledger, tmp_path):
    day = datetime(2027, 1, 15, 12, 0, tzinfo=timezone.utc)
    by_task, by_phase, partial = SM.scan_ledgers_today(
        tmp_path / "project", day, day + timedelta(days=1), home=tmp_path / "empty-home")
    assert by_task.get("demo") == pytest.approx(0.5)
    assert by_phase.get("plan") == pytest.approx(0.5)
    assert partial is True
