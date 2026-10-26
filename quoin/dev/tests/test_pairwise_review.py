"""Offline tests for the blind pairwise review core. No real process is ever spawned."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from quoin.benchmarks.harness import pairwise_review as pr
from quoin.benchmarks.scripts import spend_ledger

SEED = 1729


@pytest.fixture(autouse=True)
def _no_real_process(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a real subprocess call was attempted")

    monkeypatch.setattr(pr.subprocess, "run", boom)
    monkeypatch.setattr(pr.subprocess, "Popen", boom)


GOOD = {
    "correctness_risk_a": 2, "correctness_risk_b": 3, "minimality_a": 4, "minimality_b": 3,
    "test_quality_a": 5, "test_quality_b": 1, "preference": "A", "rationale": "A is tighter.",
}


def reply_text(**over):
    data = dict(GOOD, **over)
    return "Here you go.\n```json\n" + json.dumps(data) + "\n```\n"


class FakeReviewer:
    def __init__(self, cost=0.5, text=None, error=None):
        self.cost, self.text, self.error = cost, text, error
        self.calls = []

    def review(self, prompt, *, model, max_budget_usd, timeout_s):
        self.calls.append({"prompt": prompt, "model": model, "cap": max_budget_usd, "timeout": timeout_s})
        return pr.ReviewReply(self.text if self.text is not None else reply_text(), self.cost, self.error)


def patch_for(path, added):
    body = "".join(f"+line {i}\n" for i in range(added))
    return f"diff --git a/{path} b/{path}\nindex 1..2 100644\n--- a/{path}\n+++ b/{path}\n@@ -0,0 +1,{added} @@\n{body}"


def make_run(tmp_path, task_ids=("t1", "t2", "t3", "t4"), tier="easy"):
    run = tmp_path / "runs" / "R"
    suite = {"tasks": []}
    for tid in task_ids:
        suite["tasks"].append({"id": tid, "tier": tier, "description": f"Issue text for {tid}."})
        for cell, n in (("simple-claude", 2), ("quoin-claude", 5)):
            d = run / cell / tid
            d.mkdir(parents=True)
            (d / "diff.patch").write_text(patch_for(f"src/{tid}.py", n))
    return run, suite, tmp_path / "ledger.jsonl"


def go(run, suite, ledger, reviewer, **over):
    kw = dict(reviewer=reviewer, ledger=ledger, run_id="R", seed=SEED, model="m", effort="high",
              cap=2.0, total_cap=80.0, authorised=3170.0)
    kw.update(over)
    return pr.run_review(run, suite, **kw)


def rows(ledger):
    return [json.loads(l) for l in Path(ledger).read_text().splitlines() if l.strip()]


# --- pure pieces ----------------------------------------------------------------

def test_quoin_is_a_is_deterministic_and_seed_sensitive():
    ids = [f"task_{i}" for i in range(8)]
    first = [pr.quoin_is_a(SEED, t) for t in ids]
    assert first == [pr.quoin_is_a(SEED, t) for t in ids]
    assert first != [pr.quoin_is_a(1730, t) for t in ids]
    assert any(first) and not all(first)


def test_prompt_has_issue_and_both_patches_and_no_banned_words():
    prompt = pr.build_prompt("Fix the widget crash.", patch_for("a.py", 1), patch_for("b.py", 2))
    assert "Fix the widget crash." in prompt and "a/a.py" in prompt and "a/b.py" in prompt
    lowered = prompt.lower()
    for word in ("quoin", "simple", "plain", "cost", "$", "resolved", "verdict"):
        assert word not in lowered


def test_parse_reply_accepts_the_schema_and_uses_the_last_fence():
    parsed, why = pr.parse_reply(reply_text())
    assert why is None and parsed["preference"] == "A" and parsed["rationale_truncated"] is False
    text = "```json\n{}\n```\nthen\n" + reply_text(preference="tie")
    assert pr.parse_reply(text)[0]["preference"] == "tie"
    assert pr.parse_reply(json.dumps(GOOD))[0] is not None


@pytest.mark.parametrize("text", [
    "no json here", "", "[1, 2]", reply_text(minimality_a=0), reply_text(minimality_a=6),
    reply_text(minimality_a=True), reply_text(minimality_a=2.5), reply_text(preference="C"),
    reply_text(preference="a"), reply_text(rationale=""), reply_text(rationale=5),
    reply_text(extra=1), "```json\n" + json.dumps({k: v for k, v in GOOD.items() if k != "rationale"}) + "\n```",
])
def test_parse_reply_rejects_without_coercion(text):
    parsed, why = pr.parse_reply(text)
    assert parsed is None and why


def test_long_rationale_is_truncated_not_invalid():
    parsed, why = pr.parse_reply(reply_text(rationale="x" * 5000))
    assert why is None and len(parsed["rationale"]) == 4000 and parsed["rationale_truncated"] is True


def test_binary_blocks_are_replaced():
    text = (
        "diff --git a/tests/d.bin b/tests/d.bin\nnew file mode 100644\nindex 0..1\n"
        "GIT binary patch\nliteral 4\nzcmZ\n\n" + patch_for("src/x.py", 1)
    )
    out = pr.blind_patch(text)
    assert "[binary change to tests/d.bin omitted]" in out and "literal" not in out
    assert "src/x.py" in out


@pytest.mark.parametrize("text,hit", [
    ("see .workflow_artifacts/x", ".workflow_artifacts"), ("QUOIN run", "quoin"),
    ("edit CLAUDE.md", "CLAUDE.md"), ("clean text", None),
])
def test_marker_hit(text, hit):
    assert pr.marker_hit(text) == hit


def test_argv_has_the_caps_and_no_tools_and_no_prompt():
    argv = pr.build_reviewer_argv("opus-x", "high", 2.0)
    assert argv[argv.index("--max-budget-usd") + 1] == "2.0"
    assert argv[argv.index("--tools") + 1] == ""
    assert "opus-x" in argv and "high" in argv and "--no-session-persistence" in argv
    assert "Fix" not in " ".join(argv)


# --- eligibility ------------------------------------------------------------------

def test_eligibility_reasons(tmp_path):
    run, suite, _ = make_run(tmp_path, ("t1",))
    assert pr.eligibility(run, "t1") == (True, "")
    assert pr.eligibility(run, "nope") == (False, "not-run")
    (run / "quoin-claude" / "t1" / "diff.patch").write_text("")
    assert pr.eligibility(run, "t1") == (False, "empty-patch")
    (run / "quoin-claude" / "t1" / "diff.patch").unlink()
    assert pr.eligibility(run, "t1") == (False, "missing-patch")
    (run / "quoin-claude" / "t1" / "diff.patch").write_text(patch_for("a.py", 1))
    (run / "quoin-claude" / "t1" / "session.json").write_text(json.dumps({"source": "swebench_verified"}))
    (run / "quoin-claude" / "t1" / "metrics.json").write_text(json.dumps({"workflow_artifacts_has_plan": False}))
    assert pr.eligibility(run, "t1") == (False, "invalid-row")


# --- the run loop -------------------------------------------------------------------

def test_one_reserve_and_one_settle_per_review(tmp_path):
    run, suite, ledger = make_run(tmp_path)
    fake = FakeReviewer(cost=0.4)
    rc, state = go(run, suite, ledger, fake)
    assert rc == 0 and len(fake.calls) == 4
    r = rows(ledger)
    assert [x["kind"] for x in r].count("reservation") == 4 and [x["kind"] for x in r].count("settlement") == 4
    assert all(x["arm"] == "review" and x["invocation"] == "pairwise-review" for x in r)
    assert all("#pairwise-review#" in x["attempt_id"] for x in r)
    assert spend_ledger.recorded_total(ledger, "R", invocation="pairwise-review") == pytest.approx(1.6)
    assert all(v["status"] == "reviewed" for v in state["tasks"].values())
    assert not (run / pr.LOCK_FILE).exists() and (run / pr.STATE_FILE).exists()
    stored = (run / pr.STATE_FILE).read_text()
    assert "Issue text for" not in stored


def test_prompt_shows_arms_in_the_seeded_order(tmp_path):
    run, suite, ledger = make_run(tmp_path, ("t1",))
    fake = FakeReviewer()
    go(run, suite, ledger, fake)
    prompt = fake.calls[0]["prompt"]
    quoin_first = pr.quoin_is_a(SEED, "t1")
    a_part = prompt.split("<patch_a>")[1].split("</patch_a>")[0]
    assert ("+line 4" in a_part) is quoin_first


def test_total_cap_stop_records_unreviewed_and_second_call_makes_no_calls(tmp_path):
    run, suite, ledger = make_run(tmp_path)
    fake = FakeReviewer(cost=1.0)
    rc, state = go(run, suite, ledger, fake, total_cap=2.0)
    assert rc == 3 and len(fake.calls) == 1
    statuses = sorted(v["status"] for v in state["tasks"].values())
    assert statuses == ["not_reviewed"] * 3 + ["reviewed"]
    assert any("review-total" in v.get("reason", "") for v in state["tasks"].values())
    n_rows = len(rows(ledger))
    rc2, _ = go(run, suite, ledger, fake, total_cap=2.0)
    assert rc2 == 3 and len(fake.calls) == 1 and len(rows(ledger)) == n_rows


def test_both_run_level_checks_use_the_one_authorised_value(tmp_path):
    run, suite, ledger = make_run(tmp_path, ("t1", "t2"))
    spend_ledger.append(ledger, {
        "ts": "x", "attempt_id": "R#quoin-claude#t1#u", "kind": "settlement", "invocation": "paired",
        "gate_id": "g", "arm": "quoin", "cap_usd": 35.0, "actual_usd": 3200.0, "run_id": "R",
        "new_ceiling_usd": None, "note": "",
    })
    fake = FakeReviewer()
    rc, state = go(run, suite, ledger, fake, authorised=3170.0)
    assert rc == 3 and fake.calls == []
    assert all("run-ceiling" in v["reason"] for v in state["tasks"].values())
    rc, state = go(run, suite, ledger, fake, authorised=4000.0)
    assert rc == 0 and len(fake.calls) == 2


def test_invalid_is_final_and_error_is_retried(tmp_path):
    run, suite, ledger = make_run(tmp_path, ("t1",))
    bad = FakeReviewer(text="not json")
    rc, state = go(run, suite, ledger, bad)
    assert rc == 0 and state["tasks"]["t1"]["status"] == "invalid" and state["tasks"]["t1"]["raw"] == "not json"
    go(run, suite, ledger, bad)
    assert len(bad.calls) == 1
    run2, suite2, ledger2 = make_run(tmp_path / "b", ("t1",))
    err = FakeReviewer(error="boom")
    rc, state = go(run2, suite2, ledger2, err)
    assert rc == 1 and state["tasks"]["t1"]["status"] == "error"
    ok = FakeReviewer()
    rc, state = go(run2, suite2, ledger2, ok)
    assert rc == 0 and state["tasks"]["t1"]["status"] == "reviewed" and len(ok.calls) == 1


def test_partial_run_then_new_rows_reviews_only_the_new_tasks(tmp_path):
    run, suite, ledger = make_run(tmp_path, ("t1", "t2"))
    shutil_dir = run / "quoin-claude" / "t2"
    (shutil_dir / "diff.patch").rename(shutil_dir / "held.patch")
    fake = FakeReviewer()
    rc, state = go(run, suite, ledger, fake)
    assert len(fake.calls) == 1 and state["tasks"]["t2"]["status"] == "ineligible"
    (shutil_dir / "held.patch").rename(shutil_dir / "diff.patch")
    rc, state = go(run, suite, ledger, fake)
    assert len(fake.calls) == 2 and state["tasks"]["t2"]["status"] == "reviewed"


def test_changed_patch_is_reviewed_again(tmp_path):
    run, suite, ledger = make_run(tmp_path, ("t1",))
    fake = FakeReviewer()
    go(run, suite, ledger, fake)
    (run / "quoin-claude" / "t1" / "diff.patch").write_text(patch_for("src/t1.py", 9))
    go(run, suite, ledger, fake)
    assert len(fake.calls) == 2


def test_marker_in_a_patch_is_a_final_skip(tmp_path):
    run, suite, ledger = make_run(tmp_path, ("t1",))
    (run / "quoin-claude" / "t1" / "diff.patch").write_text(patch_for(".workflow_artifacts/plan.md", 2))
    fake = FakeReviewer()
    rc, state = go(run, suite, ledger, fake)
    assert fake.calls == [] and state["tasks"]["t1"]["status"] == "skipped"
    assert not ledger.exists()


def test_lock_held_refuses(tmp_path, capsys):
    run, suite, ledger = make_run(tmp_path, ("t1",))
    (run / pr.LOCK_FILE).write_text("123")
    fake = FakeReviewer()
    rc, _ = go(run, suite, ledger, fake)
    assert rc == 2 and fake.calls == [] and pr.LOCK_FILE in capsys.readouterr().err
    assert (run / pr.LOCK_FILE).exists()


def test_crash_between_reply_and_settlement_is_reconciled_once(tmp_path):
    run, suite, ledger = make_run(tmp_path, ("t1",))

    class Crash(FakeReviewer):
        pass

    fake = FakeReviewer(cost=0.7)
    go(run, suite, ledger, fake)
    # Drop the settlement row to mimic a crash after the state write.
    kept = [r for r in rows(ledger) if r["kind"] != "settlement"]
    ledger.write_text("".join(json.dumps(r) + "\n" for r in kept))
    go(run, suite, ledger, fake)
    settles = [r for r in rows(ledger) if r["kind"] == "settlement"]
    assert len(settles) == 1 and settles[0]["actual_usd"] == pytest.approx(0.7)
    go(run, suite, ledger, fake)
    assert len([r for r in rows(ledger) if r["kind"] == "settlement"]) == 1
    assert len(fake.calls) == 1


def test_resume_with_a_different_identity_is_refused(tmp_path):
    run, suite, ledger = make_run(tmp_path, ("t1",))
    go(run, suite, ledger, FakeReviewer())
    for over in ({"seed": 7}, {"model": "other"}, {"effort": "low"}):
        fake = FakeReviewer()
        rc, _ = go(run, suite, ledger, fake, **over)
        assert rc == 2 and fake.calls == []


def test_reviewer_exception_is_an_error_row(tmp_path):
    run, suite, ledger = make_run(tmp_path, ("t1",))

    class Raises:
        def review(self, *a, **k):
            raise RuntimeError("bad")

    rc, state = go(run, suite, ledger, Raises())
    assert rc == 1 and state["tasks"]["t1"]["status"] == "error"
    assert [r["kind"] for r in rows(ledger)] == ["reservation", "settlement"]


# --- the real adapter, with every process call patched ---------------------------------

class FakeProc:
    def __init__(self, out="", err="", rc=0, timeout=False):
        self.out, self.err, self.returncode, self.timeout = out, err, rc, timeout
        self.pid = 4242
        self.got_input = None

    def communicate(self, input=None, timeout=None):
        self.got_input = input
        if self.timeout:
            raise subprocess.TimeoutExpired("claude", timeout)
        return self.out, self.err


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    class Box:
        home = tmp_path / "sbx"

    Box.home.mkdir()
    monkeypatch.setattr(pr.arm_sandbox, "create_sandbox", lambda kind: Box)
    monkeypatch.setattr(pr.arm_sandbox, "build_session_env", lambda *a, **k: {"X": "1"})
    removed = []
    monkeypatch.setattr(pr.arm_sandbox, "remove_sandbox", lambda s: removed.append(s))
    box = {"proc": None, "argv": None, "kwargs": None, "removed": removed}

    def fake_popen(argv, **kwargs):
        box["argv"], box["kwargs"] = argv, kwargs
        return box["proc"]

    monkeypatch.setattr(pr.subprocess, "Popen", fake_popen)
    return box


def test_adapter_sends_the_prompt_on_stdin_and_parses_cost(adapter):
    adapter["proc"] = FakeProc(out=json.dumps({"result": "hello", "total_cost_usd": 0.31}))
    reply = pr.ClaudeCliReviewer("high", "R", ("K", "v")).review("PROMPT BODY", model="m", max_budget_usd=2.0, timeout_s=9)
    assert reply == pr.ReviewReply("hello", 0.31, None)
    assert adapter["proc"].got_input == "PROMPT BODY"
    assert "PROMPT BODY" not in adapter["argv"]
    assert adapter["kwargs"]["start_new_session"] is True and adapter["removed"]


def test_adapter_error_paths(adapter):
    adapter["proc"] = FakeProc(out="garbage", err="oops", rc=1)
    reply = pr.ClaudeCliReviewer("high", "R", ("K", "v")).review("p", model="m", max_budget_usd=2.0, timeout_s=9)
    assert reply.error and reply.cost_usd is None
    adapter["proc"] = FakeProc(out=json.dumps({"result": "x", "total_cost_usd": 1.5}), rc=2)
    reply = pr.ClaudeCliReviewer("high", "R", ("K", "v")).review("p", model="m", max_budget_usd=2.0, timeout_s=9)
    assert reply.error and reply.cost_usd == 1.5


def test_adapter_timeout_kills_the_group_once(adapter, monkeypatch):
    calls = []
    monkeypatch.setattr(pr.proc_group, "kill_and_reap", lambda proc, pgid, group: calls.append((pgid, group)))
    adapter["proc"] = FakeProc(timeout=True)
    reply = pr.ClaudeCliReviewer("high", "R", ("K", "v")).review("p", model="m", max_budget_usd=2.0, timeout_s=1)
    assert reply.error == "timeout" and calls == [(4242, True)] and adapter["removed"]


def test_adapter_turns_sandbox_failure_into_an_error(monkeypatch):
    def boom(kind):
        raise RuntimeError("no sandbox")

    monkeypatch.setattr(pr.arm_sandbox, "create_sandbox", boom)
    reply = pr.ClaudeCliReviewer("high", "R", ("K", "v")).review("p", model="m", max_budget_usd=2.0, timeout_s=1)
    assert "no sandbox" in reply.error


# --- the review file is not a cell -------------------------------------------------------

def test_review_files_do_not_change_aggregation(tmp_path):
    from aggregate_fixtures import build_tiered_run
    from quoin.benchmarks.harness import aggregate
    from quoin.benchmarks.scripts.check_invariants import check_invariants

    run_dir, run_id, suite_path = build_tiered_run(tmp_path)
    before = aggregate.aggregate_run(run_dir, run_id, suite_path=suite_path)
    inv_before = check_invariants(run_dir, run_id).to_markdown()
    (run_dir / run_id / pr.STATE_FILE).write_text("{}")
    (run_dir / run_id / pr.LOCK_FILE).write_text("1")
    after = aggregate.aggregate_run(run_dir, run_id, suite_path=suite_path)
    assert sorted(before["cells"]) == sorted(after["cells"]) if "cells" in before else True
    assert before["tiered"] == after["tiered"]
    assert check_invariants(run_dir, run_id).to_markdown() == inv_before


# --- report ------------------------------------------------------------------------------

def _reviewed_run(tmp_path):
    run, suite, ledger = make_run(tmp_path)
    go(run, suite, ledger, FakeReviewer(cost=0.5))
    return run, suite, ledger


def test_report_counts_arm_mapping_and_position(tmp_path):
    run, suite, ledger = _reviewed_run(tmp_path)
    report = pr.build_review_report(run, suite)
    assert report["counts"]["quoin_preferred"] + report["counts"]["plain_preferred"] == 4
    assert report["position"]["A_preferred"] == 4
    expected_quoin = sum(1 for t in ("t1", "t2", "t3", "t4") if pr.quoin_is_a(SEED, t))
    assert report["counts"]["quoin_preferred"] == expected_quoin
    assert report["counts_by_tier"]["easy"] == report["counts"]
    task = next(t for t in report["tasks"] if t["task_id"] == "t1")
    side = "a" if pr.quoin_is_a(SEED, "t1") else "b"
    assert task["ratings"]["quoin"]["minimality"] == GOOD[f"minimality_{side}"]


def test_report_spend_comes_from_the_manifest_ledger_and_degrades(tmp_path):
    run, suite, ledger = _reviewed_run(tmp_path)
    assert pr.build_review_report(run, suite)["review_spend_usd"] is None
    assert "spend ledger" in pr.build_review_report(run, suite)["review_spend_note"]
    (run / "run-manifest.yaml").write_text(f"run_id: R\nspend_ledger: {ledger}\n")
    assert pr.build_review_report(run, suite)["review_spend_usd"] == pytest.approx(2.0)
    ledger.write_text("not json\n")
    report = pr.build_review_report(run, suite)
    assert report["review_spend_usd"] is None and report["review_spend_note"]


def test_report_is_none_without_a_state_file(tmp_path):
    run, suite, _ = make_run(tmp_path)
    assert pr.build_review_report(run, suite) is None


def test_report_counts_invalid_separately_and_markdown_text(tmp_path):
    run, suite, ledger = make_run(tmp_path, ("t1", "t2"))
    go(run, suite, ledger, FakeReviewer(text="nope"))
    report = pr.build_review_report(run, suite)
    assert report["counts"]["invalid"] == 2 and report["counts"]["quoin_preferred"] == 0
    from quoin.benchmarks.harness.aggregate import _format_md_table

    md = pr.render_review_markdown(report, _format_md_table)
    assert "## Blind pairwise review (secondary measure)" in md
    assert "never affects `resolved`" in md and "invalid replies are final" in md
    assert "1 is lowest risk" in md
