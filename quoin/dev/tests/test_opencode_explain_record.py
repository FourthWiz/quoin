"""Explain and doctor read the same inventory and record a launch uses."""
from __future__ import annotations

import ast
import json
import os
import stat
from datetime import datetime
from pathlib import Path

import pytest

from _opencode_merge_helpers import NOW
from quoin import cli
from quoin.opencode_adapter import doctor, effective_record, explain, runstore
from test_opencode_driver_prepare import SECRET as DRIVER_SECRET
from test_opencode_driver_prepare import Setup

pytestmark = pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")

SEEDS = (
    "SEEDEDPASS123", "SEEDEDTOKEN9999", "sk-SEEDEDAPIKEY-1234", "sk-SEEDEDAUTH-5678",
    "SEEDEDKEYCHAIN-acct", DRIVER_SECRET, "leak-1234567890",
)


class _Frozen(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


@pytest.fixture
def denv(tmp_path, monkeypatch):
    return Setup(tmp_path, monkeypatch)


def _global(denv):
    return denv.world.tmp / "xdg" / "opencode"


def _ambient(denv):
    return dict(denv.driver()._env)  # noqa: SLF001


def _activate(denv, monkeypatch, extra=None):
    """The cli sees the same process environment the driver launches with."""
    env = _ambient(denv)
    env.update(extra or {})
    monkeypatch.setattr(os, "environ", env)
    monkeypatch.setattr(cli, "datetime", _Frozen)
    monkeypatch.setattr(cli, "_make_opencode_driver", lambda root: denv.driver(env=env))
    return env


def _explain(capsys, denv, *extra):
    code = cli.main(["opencode", "config", "explain", "--profile", "work", "--project-root", str(denv.root), *extra])
    out = capsys.readouterr()
    return code, out.out, out.err


def _doctor_code(denv, env):
    findings = doctor._profile_findings(  # noqa: SLF001
        denv.root, "work", {k: env[k] for k in doctor_keys() if k in env}, denv.world.home, NOW, ambient_env=env,
    )
    refused = [f for f in findings if f.id == "inventory-refused"]
    clean = [f for f in findings if f.id == "inventory-clean"]
    assert len(refused) + len(clean) == 1, [f.id for f in findings]
    if not refused:
        return None, None
    import re

    match = re.search(r"refused: (\S+) \(layer (\S+)\)", refused[0].message)
    assert match and refused[0].path == match.group(2)
    return match.group(1), refused[0].path


def doctor_keys():
    return ("HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "QUOIN_OPENCODE_MANAGED_POLICY")


def _worlds(denv):
    g = _global(denv)

    def write_global(doc):
        g.mkdir(parents=True, exist_ok=True)
        (g / "opencode.json").write_text(json.dumps(doc), encoding="utf-8")

    return [
        ("plugin file", lambda: denv.write(".opencode/plugin/x.js", "x"), "plugin-directory-present"),
        ("plugin key", lambda: denv.write("opencode.json", json.dumps({"plugin": ["x"]})), "protected-key-overridden"),
        ("small_model", lambda: denv.write("opencode.json", json.dumps({"small_model": "x/y"})), "protected-key-overridden"),
        ("agent.title", lambda: denv.write("opencode.json", json.dumps({"agent": {"title": {"model": "x/y"}}})), "protected-key-overridden"),
        ("quoin shadow", lambda: denv.write(".opencode/agent/quoin-x.md", "---\n---\n"), "agent-file-overridden"),
        ("unreadable project config", lambda: denv.write("opencode.json", "{"), "config-layer-unreadable"),
        ("global lsp", lambda: write_global({"lsp": {"pyright": {}}}), "inventory-lsp"),
        ("project local mcp", lambda: denv.write("opencode.json", json.dumps({"mcp": {"s": {"type": "local", "command": ["x"]}}})), "inventory-mcp-local"),
        ("global provider", lambda: write_global({"provider": {"anthropic": {"options": {}}}}), "inventory-provider"),
    ]


@pytest.mark.parametrize("index", range(9))
def test_prepare_explain_and_doctor_agree_on_the_first_refusal(denv, monkeypatch, capsys, index):
    label, setup, expected = _worlds(denv)[index]
    setup()
    error = denv.refused(expected)
    env = _activate(denv, monkeypatch)
    preview = denv.driver(env=env).effective_preview("work", ambient_env=env)
    assert preview is not None, label
    record, layer_check = preview
    assert layer_check.refusal.code == expected
    code, out, _ = _explain(capsys, denv, "--json")
    section = json.loads(out)["effective_config"]
    assert section["first_refusal"] == expected
    assert section["first_refusal_layer"] == layer_check.refusal.layer
    found, layer = _doctor_code(denv, env)
    assert (found, layer) == (expected, layer_check.refusal.layer)
    # a legacy refusal never exposes its message, and a seeded path never appears
    text = out.replace('"%s"' % denv.root, '"<approved-root>"')  # the profile's own approved root
    assert str(denv.root) not in text and str(denv.world.home) not in text
    assert error.code == expected


def test_an_ambient_config_dir_agrees_across_prepare_explain_and_doctor(denv, monkeypatch, capsys):
    foreign = denv.tmp / "foreign-config"
    foreign.mkdir()
    extra = {"OPENCODE_CONFIG_DIR": str(foreign)}
    drv_env = dict(_ambient(denv), **extra)
    denv.refused("config-env-opencode-config-dir", drv=denv.driver(env=drv_env))
    env = _activate(denv, monkeypatch, extra)
    code, out, _ = _explain(capsys, denv, "--json")
    assert json.loads(out)["effective_config"]["first_refusal"] == "config-env-opencode-config-dir"
    assert _doctor_code(denv, env)[0] == "config-env-opencode-config-dir"
    assert str(foreign) not in out


def test_a_clean_world_explains_the_digest_prepare_persists(denv, monkeypatch, capsys):
    prepared = denv.driver().prepare(denv.request())
    _activate(denv, monkeypatch)
    code, out, _ = _explain(capsys, denv, "--json")
    assert code == 0
    section = json.loads(out)["effective_config"]
    assert section["first_refusal"] is None
    assert section["record"]["digest"] == prepared.effective_config_digest
    rows = section["inventory"]
    assert rows and all(set(r) == {"kind", "layer", "name", "verdict", "code"} for r in rows)
    text_code, text, _ = _explain(capsys, denv)
    assert "Effective-config record" in text and "Inventory" in text and prepared.effective_config_digest in text


def test_run_prints_the_persisted_record_even_when_the_configuration_now_fails(denv, monkeypatch, capsys):
    prepared = denv.driver().prepare(denv.request())
    _activate(denv, monkeypatch)
    denv.write(".quoin/runtime.json", "{ not json")
    code = cli.main(["opencode", "config", "explain", "--run", prepared.run_id, "--project-root", str(denv.root), "--json"])
    out = capsys.readouterr().out
    assert code == 0
    doc = json.loads(out)
    assert doc["run_id"] == prepared.run_id
    assert doc["effective_config"]["record"]["digest"] == prepared.effective_config_digest
    code = cli.main(["opencode", "config", "explain", "--run", prepared.run_id, "--project-root", str(denv.root)])
    text = capsys.readouterr().out
    assert code == 0 and prepared.effective_config_digest in text


@pytest.mark.parametrize("bad", ["../x", "", "r" * 300, "run id", "missing-run-0000"])
def test_run_with_a_bad_or_unknown_id_exits_2_without_a_traceback(denv, monkeypatch, capsys, bad):
    denv.driver().prepare(denv.request())
    _activate(denv, monkeypatch)
    code = cli.main(["opencode", "config", "explain", "--run", bad, "--project-root", str(denv.root)])
    captured = capsys.readouterr()
    assert code == 2 and "Traceback" not in captured.err and captured.out == ""


def test_export_writes_the_live_record_privately(denv, monkeypatch, capsys):
    denv.driver().prepare(denv.request())
    _activate(denv, monkeypatch)
    code, out, err = _explain(capsys, denv, "--json", "--export")
    assert code == 0
    assert err.startswith("export written: ") and "explain-" in err and str(denv.root) not in err
    live = json.loads(out)["effective_config"]["record"]
    exports = denv.world.tmp / "state" / "quoin" / "opencode" / "work" / "exports"
    files = sorted(exports.iterdir())
    assert len(files) == 1 and files[0].name == "explain-%s.json" % live["digest"].split(":")[1][:12]
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600 and stat.S_IMODE(exports.stat().st_mode) == 0o700
    assert json.loads(files[0].read_text(encoding="utf-8")) == live


def test_export_works_for_a_world_a_launch_would_refuse(denv, monkeypatch, capsys):
    denv.write(".opencode/plugin/x.js", "x")
    _activate(denv, monkeypatch)
    code, out, err = _explain(capsys, denv, "--export")
    assert code == 0 and "first refusal: plugin-directory-present" in out and "export written" in err




def test_export_without_a_record_exits_2(denv, monkeypatch, capsys):
    import shutil

    shutil.rmtree(denv.root / ".quoin")
    _activate(denv, monkeypatch)
    code, out, err = _explain(capsys, denv, "--export")
    assert code == 2 and "Traceback" not in err


def test_export_with_run_is_a_usage_error(denv, monkeypatch, capsys):
    _activate(denv, monkeypatch)
    code = cli.main(["opencode", "config", "explain", "--run", "x", "--export", "--project-root", str(denv.root)])
    assert code == 2


def test_blocked_or_uninstalled_configuration_shows_a_note_instead_of_a_record(denv, monkeypatch, capsys):
    import shutil

    shutil.rmtree(denv.root / ".quoin")
    _activate(denv, monkeypatch)
    code, out, _ = _explain(capsys, denv, "--json")
    section = json.loads(out)["effective_config"]
    assert section == {"record": None, "note": explain.NO_RECORD_NOTE, "launch_refusal": None}


def test_no_seeded_secret_reaches_text_json_or_export_output(denv, monkeypatch, capsys):
    prepared = denv.driver().prepare(denv.request())
    data = Path(prepared.launch_env.materialize()["XDG_DATA_HOME"]) / "opencode"
    data.mkdir(parents=True, exist_ok=True)
    (data / "auth.json").write_text(json.dumps({"openrouter": {"type": "api", "key": "sk-SEEDEDAUTH-5678"}}), encoding="utf-8")
    g = _global(denv)
    g.mkdir(parents=True, exist_ok=True)
    (g / "opencode.json").write_text(json.dumps({
        "instructions": ["https://user:SEEDEDPASS123@host.invalid/x.md"],
        "mcp": {"s": {"type": "remote", "url": "https://h.invalid", "headers": {"Authorization": "Bearer SEEDEDTOKEN9999"}}},
        "provider": {"anthropic": {"options": {"apiKey": "sk-SEEDEDAPIKEY-1234"}}},
    }), encoding="utf-8")
    _activate(denv, monkeypatch)
    seen = []
    for extra in ((), ("--json",), ("--json", "--export"), ("--redact",)):
        code, out, err = _explain(capsys, denv, *extra)
        seen.append(out + err)
    exports = denv.world.tmp / "state" / "quoin" / "opencode" / "work" / "exports"
    seen.extend(p.read_text(encoding="utf-8") for p in exports.iterdir())
    for blob in seen:
        for seed in SEEDS:
            assert seed not in blob, seed
        # The profile's own approved repository root is an operator-written
        # value that explain shows unless redacted; no other form of the path may appear.
        assert str(denv.root) not in blob.replace('"%s"' % denv.root, '"<approved-root>"')


def test_doctor_does_not_import_the_driver_at_module_level():
    tree = ast.parse(Path(doctor.__file__).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            assert not any(a.name == "driver" for a in node.names), "doctor imports driver at module level"
            assert node.module != "driver"


def test_the_preview_creates_no_directory_and_resolves_no_credential(denv):
    class Boom:
        def resolve(self, ref):
            raise AssertionError("credential resolved")

    drv = denv.driver(resolver_factory=lambda environ, platform: Boom())
    state = denv.world.tmp / "state"
    before = sorted(p for p in state.rglob("*")) if state.exists() else []
    got = drv.effective_preview("work")
    assert got is not None
    after = sorted(p for p in state.rglob("*")) if state.exists() else []
    assert before == after


def test_the_preview_digest_equals_prepare_for_a_clean_world(denv):
    prepared = denv.driver().prepare(denv.request())
    record, check = denv.driver().effective_preview("work")
    assert check.refusal is None
    assert record["digest"] == prepared.effective_config_digest == effective_record.digest(record)
