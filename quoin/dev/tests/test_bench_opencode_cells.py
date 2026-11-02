"""OpenCode benchmark cells: provenance, pairing guard, thresholds and report."""

from __future__ import annotations

import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from quoin.benchmarks.harness import opencode_cells as oc
from quoin.benchmarks.harness.cells import quoin_opencode, simple_opencode
from quoin.benchmarks.harness.config import BudgetSpec

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES = REPO_ROOT / "quoin" / "adapters" / "opencode" / "fixtures" / "runtime-events"
ENDPOINT = "http://127.0.0.1:9/v1"


def _prov(cell, model="gw/coder", endpoint=ENDPOINT):
    return {"cell": cell, "model": model, "endpoint": endpoint}


def _rows(scenario, verdicts, cell="quoin-opencode"):
    return [{"cell": cell, "scenario": scenario, "verdict": v, "provenance": _prov(cell),
             "failure_reason": None if v == "pass" else "malformed-tool-args"} for v in verdicts]


def _status(rows, inv=True):
    return {t["id"]: t for t in oc.evaluate_thresholds(rows, inv)}


def test_provenance_carries_every_frozen_field_and_redacts_the_endpoint(tmp_path):
    cfg = tmp_path / "opencode.json"
    cfg.write_text("{}", encoding="utf-8")
    env = {oc.ENV_MODEL: "gw/coder", oc.ENV_BASE_URL: "https://user:tok@gw.example.invalid:8443/v1/?key=s3cret",
           oc.ENV_CONFIG: str(cfg), oc.ENV_CONTEXT_LIMIT: "131072", oc.ENV_TASK_SEED: "7"}
    record = oc.build_provenance("quoin-opencode", tmp_path, env, "1.18.32")
    assert set(oc.load_spec()["provenance_fields"]) <= set(record)
    assert record["endpoint"] == "https://gw.example.invalid:8443/v1"
    assert record["config_digest"].startswith("sha256:")
    assert (record["context_limit"], record["output_limit"]) == (131072, None)


@pytest.mark.parametrize("records, match", [
    ([_prov("simple-opencode"), _prov("quoin-opencode", model="other/model")], "different model"),
    ([_prov("simple-opencode"), _prov("quoin-opencode", endpoint="https://openrouter.ai/api/v1")], "different endpoint"),
    ([_prov("quoin-opencode")], "missing simple-opencode"),
    ([_prov("simple-opencode", model=None), _prov("quoin-opencode")], "not recorded"),
])
def test_pairing_refuses_mismatched_cells(records, match):
    with pytest.raises(oc.PairingError, match=match):
        oc.check_pairing(records)


def test_thresholds_judge_the_frozen_sample_per_cell():
    tool, scoped = "tool-call-reliability", "scoped-code-change"
    both = _rows(tool, ["pass"] * 19 + ["fail"]) + _rows(tool, ["pass"] * 20, cell="simple-opencode")
    passing = _status(both + _rows(scoped, ["pass"] * 8 + ["fail"] * 2))
    assert passing["gateway_tool_loop:quoin-opencode"]["status"] == "pass"
    assert passing["gateway_tool_loop:quoin-opencode"]["failure_modes"] == {"malformed-tool-args": 1}
    assert passing["gateway_tool_loop:simple-opencode"]["status"] == "pass"
    assert passing["scoped_workflow_tasks"]["status"] == "pass"
    assert passing["deterministic_invariants"]["status"] == "pass"

    # Two half-size samples are not pooled into one passing sample.
    pooled = _status(_rows(tool, ["pass"] * 10) + _rows(tool, ["pass"] * 10, cell="simple-opencode"))
    assert pooled["gateway_tool_loop:quoin-opencode"]["status"] == "pending"

    # Failures over the allowance fail early; runs past the sample never rescue it.
    assert _status(_rows(tool, ["fail", "fail"]))["gateway_tool_loop:quoin-opencode"]["status"] == "fail"
    late = _status(_rows(tool, ["fail", "fail"] + ["pass"] * 38))["gateway_tool_loop:quoin-opencode"]
    assert (late["status"], late["runs"], late["surplus_runs"]) == ("fail", 20, 20)

    pending = _status(_rows(tool, ["pass"] * 5), inv=None)
    assert pending["scoped_workflow_tasks"]["status"] == "pending"
    assert pending["deterministic_invariants"]["status"] == "pending"
    # Scoped tasks count only the Quoin cell.
    assert _status(_rows(scoped, ["pass"] * 10, cell="simple-opencode"))["scoped_workflow_tasks"]["runs"] == 0


def test_report_keeps_quality_and_overhead_apart_and_refuses_unpaired_rows():
    rows = [
        {"cell": c, "scenario": "scoped-code-change", "verdict": v, "provenance": _prov(c),
         "wall_seconds": 10, "turns": 3, "tool_errors": 1, "tokens": None, "cost_usd": None}
        for c, v in (("simple-opencode", "fail"), ("quoin-opencode", "pass"))
    ]
    report = oc.build_report(rows, invariants_passed=None)
    assert report["pairing"] == {"status": "paired"}
    quoin = report["cells"]["quoin-opencode"]
    assert set(quoin) == {"quality", "overhead"}
    assert report["cells"]["simple-opencode"]["quality"]["unsuccessful"] == 1
    assert (quoin["overhead"]["known_cost_usd"], quoin["overhead"]["runs_with_unknown_cost"]) == (None, 1)
    text = oc.render_markdown(report)
    assert "## Task quality" in text and "## Overhead" in text

    for broken in ({**rows[1], "provenance": _prov("quoin-opencode", model="other/model")},
                   {**rows[1], "provenance": None}):
        refused = oc.build_report([rows[0], rows[1], broken], invariants_passed=None)
        assert refused["pairing"]["status"] == "refused" and refused["cells"] == {}


def test_digest_events_reuses_adapter_translation():
    lines = (FIXTURES / "tool-denied-then-finish.jsonl").read_text(encoding="utf-8").splitlines()
    digest = oc.digest_events(lines + ["not json"], "bench-run")
    assert (digest["tool_errors"], digest["turn_count"]) == (1, 1)
    assert (digest["tokens_in"], digest["tokens_out"]) == (100, 20)


# ---------------------------------------------------------------- invocation


def _fake_opencode(tmp_path, mode="ok"):
    # A shell wrapper, because a shebang cannot hold an interpreter path with spaces.
    body = tmp_path / "fake_opencode.py"
    events = (FIXTURES / "plain-complete.jsonl").read_text(encoding="utf-8")
    body.write_text(
        "import json, os, sys, time\n"
        "if sys.argv[1:] == ['--version']:\n    print('opencode 1.18.32'); sys.exit(0)\n"
        "json.dump({k: os.environ.get(k) for k in ('HOME', 'OPENCODE_CONFIG', 'OPENCODE_CONFIG_CONTENT',"
        " 'OPENCODE_DISABLE_CLAUDE_CODE')}, open('env.json', 'w'))\n"
        "open('new_file.py', 'w').write('x = 1\\n')\n"
        f"sys.stdout.write({events!r}); sys.stdout.flush()\n"
        f"mode = {mode!r}\n"
        "if mode == 'hang':\n    time.sleep(60)\n"
        "if mode == 'exit':\n    sys.exit(3)\n",
        encoding="utf-8",
    )
    script = tmp_path / "fake-opencode"
    script.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{body}" "$@"\n', encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


@pytest.fixture
def cell_env(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=work, check=True)
    subprocess.run(["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t",
                    "commit", "-q", "--allow-empty", "-m", "base"], cwd=work, check=True)
    cfg = tmp_path / "opencode.json"
    cfg.write_text(json.dumps({"provider": {"gw": {"options": {"baseURL": ENDPOINT}}}}), encoding="utf-8")
    monkeypatch.setenv(oc.ENV_MODEL, "gw/coder")
    monkeypatch.setenv(oc.ENV_BASE_URL, ENDPOINT)
    monkeypatch.setenv(oc.ENV_CONFIG, str(cfg))
    monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", "{}")
    return work, cfg


def test_simple_cell_runs_isolated_and_records_provenance(tmp_path, monkeypatch, cell_env):
    work, cfg = cell_env
    monkeypatch.setenv(oc.ENV_BINARY, str(_fake_opencode(tmp_path)))
    result = simple_opencode.invoke({"prompt": "hi", "scenario": "scoped-code-change"}, work, BudgetSpec(), "r")
    assert result["verdict"] is None and result["turn_count"] == 2
    extra = result["extra"]
    assert extra["provenance"]["runtime_version"] == "1.18.32"
    assert extra["provenance"]["adapter"] == "none"
    assert "runtime_version_unqualified" not in extra
    assert extra["stripped_env"] == ["OPENCODE_CONFIG_CONTENT"]
    seen = json.loads((work / "env.json").read_text(encoding="utf-8"))
    assert seen["HOME"] != str(Path.home()) and seen["OPENCODE_CONFIG"] == str(cfg)
    assert seen["OPENCODE_CONFIG_CONTENT"] is None and seen["OPENCODE_DISABLE_CLAUDE_CODE"] == "1"
    assert "new_file.py" in result["diff_patch"]


def test_timeout_and_nonzero_exit(tmp_path, monkeypatch, cell_env):
    work, _ = cell_env
    monkeypatch.setenv(oc.ENV_BINARY, str(_fake_opencode(tmp_path, "hang")))
    timed = oc.invoke_opencode("simple-opencode", {"prompt": "hi"}, work, 3, "r")
    assert timed["verdict"] == "timeout" and timed["turn_count"] == 2
    monkeypatch.setenv(oc.ENV_BINARY, str(_fake_opencode(tmp_path, "exit")))
    failed = oc.invoke_opencode("simple-opencode", {"prompt": "hi"}, work, 30, "r")
    assert (failed["verdict"], failed["extra"]["failure_reason"]) == ("error", "opencode-exit-3")


@pytest.mark.parametrize("change, reason", [
    ({oc.ENV_BASE_URL: "http://127.0.0.1:10/v1"}, "endpoint-mismatch"),
    ({oc.ENV_MODEL: "other/coder"}, "endpoint-not-in-config"),
    ({oc.ENV_MODEL: ""}, "missing-pairing-inputs"),
])
def test_cells_refuse_unpaired_inputs_but_keep_provenance(tmp_path, monkeypatch, cell_env, change, reason):
    work, _ = cell_env
    monkeypatch.setenv(oc.ENV_BINARY, str(_fake_opencode(tmp_path)))
    for name, value in change.items():
        monkeypatch.setenv(name, value)
    result = quoin_opencode.invoke({"prompt": "hi"}, work, BudgetSpec(), "r")
    assert result["verdict"] == "error"
    assert result["extra"]["failure_reason"].startswith(reason)
    assert result["extra"]["provenance"]["cell"] == "quoin-opencode"


def test_quoin_cell_install_failure_and_verified_tasks_are_refused(tmp_path, monkeypatch, cell_env):
    work, _ = cell_env
    monkeypatch.setenv(oc.ENV_BINARY, str(_fake_opencode(tmp_path)))
    monkeypatch.setattr(oc, "_install_quoin", lambda *_: "quoin-install-exit-1")
    result = quoin_opencode.invoke({"prompt": "hi"}, work, BudgetSpec(), "r")
    assert result["extra"]["failure_reason"] == "quoin-install-exit-1"
    assert result["prompt"].startswith(oc.QUOIN_PROMPT_PREFIX)
    verified = quoin_opencode.invoke({"source": "swebench_verified"}, work, BudgetSpec(), "r")
    assert verified["extra"]["failure_reason"] == "verified-tasks-unsupported"


def test_report_cli_reads_a_run_directory(tmp_path):
    for cell, verdict in (("simple-opencode", "fail"), ("quoin-opencode", "fail")):
        for n in range(2):
            task = tmp_path / cell / f"tool-{n}"
            task.mkdir(parents=True)
            (task / "metrics.json").write_text(json.dumps({
                "task_id": task.name, "cell": cell, "turn_count": 1, "scenario": "tool-call-reliability",
                "provenance": _prov(cell), "failure_reason": "malformed-tool-args"}), encoding="utf-8")
            (task / "judge.json").write_text(json.dumps({"verdict": verdict}), encoding="utf-8")
    script = REPO_ROOT / "quoin" / "benchmarks" / "scripts" / "opencode_report.py"
    proc = subprocess.run([sys.executable, str(script), str(tmp_path)], capture_output=True, text=True)
    assert proc.returncode == 1
    assert "| Harmless gateway tool loop (quoin-opencode) | fail | 0 / 2 |" in proc.stdout
