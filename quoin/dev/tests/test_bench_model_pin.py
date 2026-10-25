"""One model table, the effort pin, the alias environment and per-model cell estimates."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "fakes"))
from fake_claude import make_fake_claude  # noqa: E402

from quoin.benchmarks.harness import arm_sandbox, cost  # noqa: E402
from quoin.benchmarks.harness.cells import quoin_claude, simple_claude  # noqa: E402
from quoin.benchmarks.scripts import run_benchmark  # noqa: E402

BENCH_ROOT = Path(__file__).resolve().parent.parent.parent / "benchmarks"
SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"


@pytest.mark.parametrize("cell", [simple_claude, quoin_claude], ids=["plain", "quoin"])
def test_both_cells_build_argv_with_model_and_effort(cell):
    argv = cell._build_claude_argv("p", cell._get_model(), cell._get_effort(), 5.0)
    i = argv.index("--model")
    assert argv[i:i + 4] == ["--model", "claude-opus-5-5", "--effort", "high"]
    assert argv[-1] == "p"
    assert "--max-budget-usd" in argv


def test_changing_the_table_changes_both_arms_together(monkeypatch):
    monkeypatch.setitem(simple_claude.BENCH_MODELS, "opus", "claude-opus-9-9")
    monkeypatch.setattr(simple_claude, "BENCH_EFFORT", "low")
    for cell in (simple_claude, quoin_claude):
        argv = cell._build_claude_argv("p", cell._get_model(), cell._get_effort(), None)
        assert "claude-opus-9-9" in argv and "low" in argv
    env = arm_sandbox.build_session_env(None, "quoin-claude", "r", ("K", "v"), environ={})
    assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "claude-opus-9-9"


@pytest.mark.parametrize("cell", ["simple-claude", "quoin-claude"])
def test_alias_env_is_set_and_subagent_override_is_not(cell):
    env = arm_sandbox.build_session_env(None, cell, "r", ("K", "v"), environ={})
    assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "claude-opus-5-5"
    assert env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "claude-sonnet-5-5"
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "claude-haiku-5-5"
    assert env["ANTHROPIC_SMALL_FAST_MODEL"] == "claude-haiku-5-5"
    assert "CLAUDE_CODE_SUBAGENT_MODEL" not in env


def test_bench_models_match_the_authoritative_aliases():
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import cost_from_jsonl

    assert simple_claude.BENCH_MODELS == {
        k: cost_from_jsonl.ALIAS_TO_MODEL[k] for k in ("opus", "sonnet", "haiku")
    }


def test_pinned_model_literal_appears_once_in_harness_and_scripts():
    hits = []
    for sub in ("harness", "scripts"):
        for path in (BENCH_ROOT / sub).rglob("*.py"):
            hits += [path.name for _ in re.findall(r"""["']claude-opus-5-5["']""", path.read_text())]
    assert hits == ["simple_claude.py"]


# -- per-model cell estimates -------------------------------------------------

INIT = {"type": "system", "subtype": "init", "skills": ["a"], "apiKeySource": "none"}


def _result(usage):
    return {"type": "result", "subtype": "success", "is_error": False,
            "total_cost_usd": 0.5, "modelUsage": usage}


def _run_cell(tmp_path, monkeypatch, usage):
    from test_cell_paired_params import _invoke, _session_env

    bin_dir = make_fake_claude(tmp_path, {"steps": [{"emit": INIT}, {"emit": _result(usage)}]})
    return _invoke(simple_claude, tmp_path, bin_dir, session_env=_session_env(bin_dir))


def test_cell_estimate_sums_per_model_and_lists_unpriced(tmp_path, monkeypatch):
    usage = {
        "claude-opus-5-5": {"inputTokens": 1_000_000, "outputTokens": 0},
        "claude-sonnet-5-5": {"inputTokens": 1_000_000, "outputTokens": 0},
    }
    result = _run_cell(tmp_path, monkeypatch, usage)
    assert result["cost_estimated_usd"] == pytest.approx(4.0 + 2.0)
    assert result["extra"]["unpriced_models"] == []


def test_cell_records_unpriced_models(tmp_path, monkeypatch):
    usage = {"claude-opus-9-9": {"inputTokens": 100, "outputTokens": 1}}
    result = _run_cell(tmp_path, monkeypatch, usage)
    assert result["extra"]["unpriced_models"] == ["claude-opus-9-9"]
    assert result["cost_estimated_usd"] is None


# -- preflight and dry run ---------------------------------------------------


def test_gate_check_names_a_bench_model_missing_from_the_card(monkeypatch):
    pricing = cost.load_pricing()
    trimmed = {**pricing, "models": {k: v for k, v in pricing["models"].items()
                                     if k != "claude-haiku-5-5"}}
    monkeypatch.setattr(cost, "load_pricing", lambda *a, **k: trimmed)
    ok, reason = run_benchmark.dry_run_gate_check(5.0)
    assert not ok
    assert "claude-haiku-5-5" in reason


def test_gate_check_passes_with_the_shipped_card():
    assert run_benchmark.dry_run_gate_check(5.0) == (True, "")


def test_dry_run_estimate_names_an_unpriced_pin(monkeypatch):
    monkeypatch.setattr(run_benchmark, "_probe_claude_model", lambda: "claude-opus-9-9")
    out = run_benchmark._estimate_cost_dry_run(["simple-claude"], [{"id": "t"}])
    assert out["simple-claude"] == "cost: not_available — unpriced model claude-opus-9-9"
    assert "$" not in out["simple-claude"]


# -- alias probe ---------------------------------------------------------------


def _probe_response(model):
    return {"modelUsage": {model: {"canonicalModel": model}}, "total_cost_usd": 0.01}


def test_verify_model_probes_each_alias_with_the_alias_env(tmp_path, monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs.get("env")))
        model = argv[argv.index("--model") + 1]
        resolved = {"sonnet": "claude-sonnet-5-5", "haiku": "claude-haiku-5-5"}.get(model, model)

        class R:
            stdout = json.dumps(_probe_response(resolved))

        return R()

    monkeypatch.setattr(run_benchmark.subprocess, "run", fake_run)
    ledger = tmp_path / "ledger.jsonl"
    code = run_benchmark.verify_model(
        ledger_path=ledger, authorised=50.0, verify_aliases=True
    )
    assert code == 0
    models = [a[a.index("--model") + 1] for a, _ in calls]
    assert models == ["claude-opus-5-5", "sonnet", "haiku"]
    assert calls[1][1]["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "claude-sonnet-5-5"
    rows = [json.loads(line) for line in ledger.read_text().splitlines() if line]
    assert [r["kind"] for r in rows].count("reservation") == 3
    assert [r["kind"] for r in rows].count("settlement") == 3


def test_verify_model_fails_on_one_mismatching_alias(monkeypatch):
    def fake_run(argv, **kwargs):
        model = argv[argv.index("--model") + 1]
        resolved = "claude-haiku-4-5" if model == "haiku" else {
            "sonnet": "claude-sonnet-5-5"}.get(model, model)

        class R:
            stdout = json.dumps(_probe_response(resolved))

        return R()

    monkeypatch.setattr(run_benchmark.subprocess, "run", fake_run)
    assert run_benchmark.verify_model(ledger_path=None, verify_aliases=True) == 1


def test_verify_model_with_aliases_refuses_when_headroom_covers_only_one_probe(tmp_path, monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        raise AssertionError("no probe may run")

    monkeypatch.setattr(run_benchmark.subprocess, "run", fake_run)
    ledger = tmp_path / "ledger.jsonl"
    # Headroom is 2x the probe cap: enough for one probe, not for three.
    code = run_benchmark.verify_model(
        ledger_path=ledger, authorised=2.0, max_budget_usd=1.0, verify_aliases=True
    )
    assert code == 2
    assert calls == []
