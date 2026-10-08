"""An unpriced model in a subagent transcript is named on stderr, never on stdout."""
from __future__ import annotations

import json
import pathlib
import sys

SCRIPTS_DIR = pathlib.Path(__file__).parent.parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
import agent_transcript_cost as atc  # noqa: E402

SID = "11111111-2222-3333-4444-555555555555"
AID = "a0000000000000001"


def _make_transcript(home, project, models):
    d = (
        pathlib.Path(home) / ".claude" / "projects" / atc.project_hash(project)
        / SID / "subagents"
    )
    d.mkdir(parents=True)
    rows = [
        {"message": {"model": m, "usage": {"input_tokens": 100, "output_tokens": 50,
                                           "cache_creation_input_tokens": 0,
                                           "cache_read_input_tokens": 0}}}
        for m in models
    ]
    (d / f"agent-{AID}.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def _run(monkeypatch, home, project):
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: pathlib.Path(home)))
    return atc.main(["--sid", SID, "--agent-id", AID, "--project-path", project])


def test_unknown_model_warns_on_stderr_and_stdout_unchanged(tmp_path, monkeypatch, capsys):
    project = "/fake/project"
    _make_transcript(tmp_path, project, ["claude-opus-9-9"])
    assert _run(monkeypatch, tmp_path, project) == 0
    out = capsys.readouterr()
    assert out.out == "tok=150;src=unresolved\n"
    assert "'claude-opus-9-9'" in out.err
    assert "PRICES needs an entry" in out.err


def test_priced_model_emits_no_warning(tmp_path, monkeypatch, capsys):
    project = "/fake/project"
    _make_transcript(tmp_path, project, ["claude-opus-5-5"])
    assert _run(monkeypatch, tmp_path, project) == 0
    out = capsys.readouterr()
    assert "src=nested_jsonl" in out.out
    assert "PRICES needs an entry" not in out.err


def test_same_unknown_model_twice_warns_once(tmp_path, monkeypatch, capsys):
    project = "/fake/project"
    _make_transcript(tmp_path, project, ["claude-opus-9-9", "claude-opus-9-9"])
    _run(monkeypatch, tmp_path, project)
    err = capsys.readouterr().err
    assert err.count("agent_transcript_cost: unknown model 'claude-opus-9-9'") == 1
