"""Parity and drift tests for the real context-utilization readers.

The shell reader (hooks/_lib.sh::compute_context_reading) and the Python reader
(core/scripts/context_budget_guard.py, driven through its wrapper) must give the
same answer for every transcript. Both REAL surfaces are executed per case and
compared with each other and with the committed expectation; neither is checked
against its own expression.

Import-free / bare-checkout runnable: everything goes through subprocess calls on
file paths, never `import quoin`.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
LIB_SH = REPO_ROOT / "quoin" / "hooks" / "_lib.sh"
CORE_GUARD = REPO_ROOT / "quoin" / "core" / "scripts" / "context_budget_guard.py"
WRAPPER = REPO_ROOT / "quoin" / "scripts" / "context_budget_guard.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "context_reading"

SHELLS = [s for s in ("sh", "bash", "zsh", "dash") if shutil.which(s)]
CASES = json.loads((FIXTURES / "cases.json").read_text(encoding="utf-8"))["cases"]
CASE_IDS = [c["id"] for c in CASES]

# Env vars that change the answer; the host's values must never leak in.
_HOST_KNOBS = (
    "ANTHROPIC_MODEL",
    "CLAUDE_CODE_DISABLE_1M_CONTEXT",
    "QUOIN_EFFECTIVE_CONTEXT_LIMIT",
    "QUOIN_CONTEXT_TAIL_BYTES",
    "QUOIN_BYTES_PER_TOKEN",
)


def _hermetic_env(tmp_path: Path, extra=None) -> dict:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "LC_ALL": "C"}
    env.update(extra or {})
    for knob in _HOST_KNOBS:
        assert knob not in env or knob in (extra or {}), knob
    return env


def _write_settings(tmp_path: Path, settings: dict, bom: bool = False) -> Path:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    layers = {
        "user": tmp_path / "home" / ".claude" / "settings.json",
        "project": project / ".claude" / "settings.json",
        "project_local": project / ".claude" / "settings.local.json",
    }
    for layer, model in settings.items():
        path = layers[layer]
        path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps({"model": model}).encode("utf-8")
        path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + body)
    return project


def run_shell(shell, transcript, project, env, fn="compute_context_reading", kind=False):
    script = '. "$1"; read_constants; %s "$2" "$3"%s' % (fn, ' --kind' if kind else '')
    proc = subprocess.run(
        [shell, "-c", script, "x", str(LIB_SH), str(transcript), str(project)],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert proc.returncode == 0, (shell, proc.stderr)
    return proc.stdout, proc.stderr


def parse_shell(out):
    bps, source, tokens, window = out.strip().split(" ")[:4]
    return (
        int(bps),
        source,
        None if tokens == "-" else int(tokens),
        None if window == "-" else int(window),
    )


def run_python(transcript, project, env, threshold="999999"):
    proc = subprocess.run(
        [sys.executable, str(WRAPPER), "--transcript", str(transcript),
         "--project-root", str(project), "--threshold-bps", threshold],
        capture_output=True, text=True, env=env, timeout=60,
    )
    line = proc.stdout.strip().splitlines()[-1]
    status, bps, _ = (line.split("|") + ["", ""])[:3]
    assert status in ("OK", "OVER"), proc.stdout + proc.stderr
    m = re.search(r"reading source=(\w+) tokens=(\S+) window=(\S+)", proc.stderr)
    assert m, proc.stderr
    tokens = None if m.group(2) == "-" else int(m.group(2))
    window = None if m.group(3) == "-" else int(m.group(3))
    return int(bps), m.group(1), tokens, window


def _expected(case):
    e = case["expect"]
    return (e["bps"], e["source"], e["tokens"], e["window"])


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_shell_reader_matches_expectation(case, shell, tmp_path):
    project = _write_settings(tmp_path, case["settings"], case.get("settings_bom", False))
    env = _hermetic_env(tmp_path, case["env"])
    out, _ = run_shell(shell, FIXTURES / case["file"], project, env)
    assert parse_shell(out) == _expected(case)
    assert len(out.strip().split(" ")) == 4, "default output must stay four fields"
    if "kind" in case["expect"]:
        kout, _ = run_shell(shell, FIXTURES / case["file"], project, env, kind=True)
        assert kout.strip().split(" ")[4:] == [case["expect"]["kind"]]
        assert parse_shell(kout) == _expected(case)
    # compute_utilization is a thin wrapper: it prints exactly field 1.
    util_out, _ = run_shell(shell, FIXTURES / case["file"], project, env, fn="compute_utilization")
    assert util_out.strip() == str(case["expect"]["bps"])


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_python_reader_matches_expectation(case, tmp_path):
    project = _write_settings(tmp_path, case["settings"], case.get("settings_bom", False))
    env = _hermetic_env(tmp_path, case["env"])
    assert run_python(FIXTURES / case["file"], project, env) == _expected(case)


# ---- model-id matrix --------------------------------------------------------

_MATRIX_1M = [
    "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5", "claude-sonnet-5-5",
    "claude-haiku-5-5", "claude-fable-5-1", "claude-mythos-5", "claude-opus-4-7",
    "claude-opus-4-8", "CLAUDE-OPUS-5-5", "us.anthropic.claude-opus-4-7-v1:0",
    "claude-opus-10-1",
]
_MATRIX_200K = [
    "claude-opus-4-6", "claude-opus-4-6@20250805", "claude-haiku-4-5-20251001",
    "claude-3-5-haiku-20241022", "claude-3-7-sonnet-20250219", "claude-sonnet-4-5",
    "claude-opus-4-1", "deepseek/deepseek-v4.1-flash", "",
]
_MATRIX = [(m, 1000000) for m in _MATRIX_1M] + [(m, 200000) for m in _MATRIX_200K]


def _one_record(model, total=100000):
    usage = {"input_tokens": 10, "cache_read_input_tokens": total - 510,
             "cache_creation_input_tokens": 500, "output_tokens": 5}
    message = {"usage": usage}
    if model:
        message["model"] = model
    return json.dumps({"type": "assistant", "isSidechain": False, "message": message}) + "\n"


@pytest.mark.parametrize("model,window", _MATRIX, ids=[m or "<empty>" for m, _ in _MATRIX])
def test_model_id_matrix(model, window, tmp_path):
    transcript = tmp_path / "m.jsonl"
    transcript.write_text(_one_record(model), encoding="utf-8")
    project = _write_settings(tmp_path, {})
    env = _hermetic_env(tmp_path)
    expected = (100000 * 10000 // window, "real", 100000, window)
    assert run_python(transcript, project, env) == expected
    for shell in SHELLS:
        out, _ = run_shell(shell, transcript, project, env)
        assert parse_shell(out) == expected, shell


# ---- drift between the two rule tables ---------------------------------------

def _rules_block(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    m = re.search(r"# ctx-rules-begin\n(.*?)# ctx-rules-end", text, re.S)
    assert m, f"rule markers missing in {path}"
    return m.group(1)


def _tables(block: str):
    parts = re.split(r"_ctx_rules|_CTX_RULES", block, maxsplit=1)
    assert len(parts) == 2, "strip and rules tables not both found"
    strips = re.findall(r'"([^"]+)"', parts[0])
    rules = re.findall(r'"(\^?[^"]+)"\s*,\s*([0-9]+)', parts[1])
    return strips, rules


def test_rule_tables_identical_in_shell_and_python():
    shell_strips, shell_rules = _tables(_rules_block(LIB_SH))
    py_strips, py_rules = _tables(_rules_block(CORE_GUARD))
    assert shell_rules and shell_strips and py_rules and py_strips
    assert ("^claude-(fable|mythos)(-|$)", "1000000") in py_rules
    assert shell_strips == py_strips
    assert shell_rules == py_rules
    assert py_rules[-1] == ("^claude-", "200000")


# ---- jq absent ----------------------------------------------------------------

def test_shell_without_jq_falls_back_to_estimate(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for tool in ("awk", "wc", "tail", "cat", "sed", "head", "tr"):
        found = shutil.which(tool)
        assert found, tool
        (bindir / tool).symlink_to(found)
    transcript = FIXTURES / "opus55_live.jsonl"
    env = {"PATH": str(bindir), "HOME": str(tmp_path), "LC_ALL": "C"}
    proc = subprocess.run(
        ["/bin/sh", "-c", '. "$1"; read_constants; compute_context_reading "$2" "$3"',
         "x", str(LIB_SH), str(transcript), str(tmp_path)],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    size = transcript.stat().st_size
    assert proc.stdout.strip() == "%d estimated - -" % int(size / 8.0 / 150000 * 10000)
    # Documented divergence: the Python reader needs no jq and still reads usage.
    project = _write_settings(tmp_path, {})
    real_env = _hermetic_env(tmp_path)
    assert run_python(transcript, project, real_env)[1] == "real"


# ---- byte fallback --------------------------------------------------------------

@pytest.mark.parametrize("n", [0, 8, 150000, 1079999, 1080000, 1200000, 5000000])
def test_byte_files_use_the_estimate_in_both_runtimes(n, tmp_path):
    transcript = tmp_path / "x.jsonl"
    transcript.write_bytes(b"x" * n)
    project = _write_settings(tmp_path, {})
    env = _hermetic_env(tmp_path)
    expected = (int(n / 8.0 / 150000 * 10000), "estimated", None, None)
    assert run_python(transcript, project, env) == expected
    for shell in SHELLS:
        out, _ = run_shell(shell, transcript, project, env)
        assert parse_shell(out) == expected, shell


def test_estimate_can_exceed_full_window(tmp_path):
    transcript = tmp_path / "x.jsonl"
    transcript.write_bytes(b"x" * 5000000)
    project = _write_settings(tmp_path, {})
    out, _ = run_shell("sh", transcript, project, _hermetic_env(tmp_path), fn="compute_utilization")
    assert out.strip() == "41666"


# ---- latency ---------------------------------------------------------------------

def test_shell_reading_of_a_two_megabyte_transcript_is_fast(tmp_path):
    lines = []
    filler = "z" * 4000
    for i in range(500):
        lines.append(json.dumps({"type": "user", "isSidechain": False,
                                 "message": {"role": "user", "content": filler}}))
        lines.append(_one_record("claude-opus-5-5", 50000 + i).strip())
    transcript = tmp_path / "big.jsonl"
    transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert transcript.stat().st_size > 2_000_000
    project = _write_settings(tmp_path, {})
    env = _hermetic_env(tmp_path)
    start = time.monotonic()
    out, _ = run_shell("sh", transcript, project, env)
    elapsed = time.monotonic() - start
    assert parse_shell(out) == ((50499 * 10000) // 1000000, "real", 50499, 1000000)
    assert elapsed < 3.0, elapsed


# ---- numeric / decoding guards in the Python reader ---------------------------

def _load_core():
    import importlib.util
    spec = importlib.util.spec_from_file_location("context_budget_guard_core", CORE_GUARD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_scan_survives_recursion_error_on_one_line(monkeypatch):
    core = _load_core()
    real_loads = core.json.loads

    def loads(text, *a, **kw):
        if "MARKER" in text:
            raise RecursionError("nested too deep")
        return real_loads(text, *a, **kw)

    monkeypatch.setattr(core.json, "loads", loads)
    usage = {"input_tokens": 100000, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    good = json.dumps({"type": "assistant", "message": {"model": "claude-opus-4-6", "usage": usage}})
    lines = [good.encode(), b'{"MARKER": 1}']
    tokens, model, _peak, _clamp = core._scan(lines)
    assert tokens == 100000 and model == "claude-opus-4-6"


def test_settings_model_survives_recursion_error(monkeypatch, tmp_path):
    core = _load_core()
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "settings.local.json").write_text('{"model": "MARKER"}', encoding="utf-8")
    (project / ".claude" / "settings.json").write_text('{"model": "claude-opus-4-6[1m]"}', encoding="utf-8")
    real_loads = core.json.loads

    def loads(text, *a, **kw):
        if "MARKER" in text:
            raise RecursionError("nested too deep")
        return real_loads(text, *a, **kw)

    monkeypatch.setattr(core.json, "loads", loads)
    assert core._settings_model(project, {}) == "claude-opus-4-6[1m]"
