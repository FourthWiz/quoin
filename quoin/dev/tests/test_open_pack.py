"""Open-model pack: setup and removal touch only the pack and its profile, the
launch environment maps the tiers to the gateway's models, and the gateway probe
separates protocol failures from task failures."""

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

from quoin import open_pack

PKG = Path(__file__).resolve().parent.parent.parent
PROBE = PKG / "packs" / "open" / "scripts" / "probe_gateway.py"


def _load_probe():
    spec = importlib.util.spec_from_file_location("_probe_gateway", PROBE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "home" / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "home" / ".local" / "share"))
    monkeypatch.delenv("QUOIN_OPEN_PACK_DIR", raising=False)
    (tmp_path / "home" / ".claude").mkdir(parents=True)
    (tmp_path / "home" / ".claude" / "settings.json").write_text('{"model": "opus"}')
    return tmp_path / "home"


def _tree(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def test_setup_and_remove_touch_only_the_pack_and_its_profile(home):
    before = _tree(home)
    result = open_pack.setup(PKG)
    assert result["profile_written"]
    added = sorted(set(_tree(home)) - set(before))
    assert all(p.startswith((".local/share/quoin/open-pack/", ".config/quoin/open.json")) for p in added)
    assert ".local/share/quoin/open-pack/skills/plan/SKILL.md" in added
    assert (home / ".claude" / "settings.json").read_text() == '{"model": "opus"}'
    assert open_pack.remove()["removed"]
    assert _tree(home) == before


def test_setup_keeps_an_edited_profile(home):
    open_pack.setup(PKG)
    open_pack.profile_path().write_text(json.dumps({**open_pack.DEFAULT_PROFILE, "base_url": "https://gw.example/api"}))
    assert open_pack.setup(PKG)["profile_written"] is False
    assert open_pack.load_profile()["base_url"] == "https://gw.example/api"
    assert open_pack.setup(PKG, overwrite_profile=True)["profile_written"] is True


def test_launch_environment_maps_tiers_and_sets_the_context_guard():
    env = open_pack.launch_env(open_pack.DEFAULT_PROFILE, {"OPENROUTER_API_KEY": "sk-test", "PATH": "/bin"})
    assert env["ANTHROPIC_BASE_URL"] == "https://openrouter.ai/api"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-test" and env["ANTHROPIC_API_KEY"] == ""
    assert env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "qwen/qwen3-coder"
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "qwen/qwen3-coder-flash"
    assert env["CLAUDE_CODE_SUBAGENT_MODEL"] == "qwen/qwen3-coder"
    assert env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "262144" and env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] == "80"
    with pytest.raises(KeyError):
        open_pack.launch_env(open_pack.DEFAULT_PROFILE, {})


def test_launch_loads_the_pack_and_refuses_when_not_set_up(home, monkeypatch):
    assert open_pack.launch([], runner=lambda *a, **k: 0) == 2
    open_pack.setup(PKG)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    calls = []
    assert open_pack.launch(["-p", "hi"], runner=lambda argv, env: calls.append((argv, env)) or 0) == 0
    argv, env = calls[0]
    assert argv[:3] == ["claude", "--plugin-dir", str(open_pack.pack_dir())]
    assert argv[-4:] == ["--model", "sonnet", "-p", "hi"]
    assert env["QUOIN_OPEN_MODEL"] == "qwen/qwen3-coder"
    assert open_pack.launch_argv(["--model", "opus"]).count("--model") == 1


def test_pack_manifest_names_its_skills():
    manifest = json.loads((PKG / "packs" / "open" / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["name"] == "quoin-open" and manifest["skills"] == "./skills/"
    for skill in ("plan", "review"):
        head = (PKG / "packs" / "open" / "skills" / skill / "SKILL.md").read_text().split("\n---", 1)[0]
        assert re.search(rf"^name: {skill}$", head, re.M)


GOOD_MODELS = {"data": [{"id": "qwen/qwen3-coder", "context_length": 262144, "supported_parameters": ["tools"]},
                        {"id": "qwen/qwen3-coder-flash", "context_length": 1000000, "supported_parameters": ["tools"]}]}


def test_probe_passes_a_healthy_gateway(monkeypatch):
    probe = _load_probe()

    def fake(url, key, body=None, *, anthropic=True):
        if url.endswith("/models"):
            assert anthropic is False
            return 200, GOOD_MODELS
        if body.get("tools"):
            return 200, {"content": [{"type": "tool_use", "name": "record_number", "input": {"value": 42}}]}
        return 200, {"content": [{"type": "text", "text": "ready"}]}

    monkeypatch.setattr(probe, "_request", fake)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    result = probe.probe(open_pack.DEFAULT_PROFILE)
    assert result["ok"] and set(result["checks"]) == {"models", "message", "tools"}


def test_probe_names_the_failing_check(monkeypatch):
    probe = _load_probe()

    def fake(url, key, body=None, *, anthropic=True):
        if url.endswith("/models"):
            return 200, {"data": [{"id": "qwen/qwen3-coder"}]}
        if body.get("tools"):
            return 200, {"content": [{"type": "text", "text": "I would record 42"}]}
        return 200, {"content": [{"type": "text", "text": "ready"}]}

    monkeypatch.setattr(probe, "_request", fake)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    result = probe.probe(open_pack.DEFAULT_PROFILE)
    assert result["ok"] is False
    assert result["checks"]["message"]["ok"] is True
    assert result["checks"]["tools"]["detail"] == "no tool_use block in the reply"
    assert "not in the gateway's model list" in result["checks"]["models"]["detail"]["haiku"]


def test_probe_without_a_key_does_not_call_the_gateway(monkeypatch):
    probe = _load_probe()
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(probe, "_request", lambda *a, **k: pytest.fail("called the gateway"))
    assert probe.probe(open_pack.DEFAULT_PROFILE)["checks"]["key"]["ok"] is False


def test_native_coverage_list_dates_every_row():
    text = (PKG / "docs" / "native-coverage.md").read_text()
    rows = [line for line in text.splitlines() if line.startswith("| ") and not line.startswith(("| Component", "| ---"))]
    assert len(rows) >= 20
    assert all(re.search(r"\| \d{4}-\d{2}-\d{2} \|$", row) for row in rows), [r for r in rows if not r.rstrip().endswith("|")]
