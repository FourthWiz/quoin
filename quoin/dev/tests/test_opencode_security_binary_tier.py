"""Opt-in tier: the work-profile attempts that change the approved model
configuration, run against the real pinned OpenCode binary with a work gateway
and a personal listener on loopback. Skipped, with its reason, when no binary
pinned to the adapter's release is on PATH.

A refused attempt must leave both listeners silent. A final test records, and
does not assert, whether the binary honours HTTPS_PROXY for provider calls, so
a maintainer run can update the compatibility row that cites it.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import _opencode_capture_helpers as ch
from _opencode_capture_helpers import egress_guard  # noqa: F401  (pytest fixture)
import _opencode_isolation_helpers as iso
import test_opencode_binary_contract as contract
from quoin.opencode_adapter import driver

SKIP_REASON = contract.skip_reason(contract._OPENCODE_BIN, contract._QUALIFIED_VERSIONS, contract._BINARY_VERSION)  # noqa: SLF001

pytestmark = pytest.mark.skipif(SKIP_REASON is not None, reason=SKIP_REASON or "")

AGENT_MD = "---\ndescription: helper\nmodel: openrouter/not-allowed\n---\nbody\n"


def test_the_tier_names_the_pin_when_skipped():
    assert SKIP_REASON is None or "1.18.32" in SKIP_REASON or "no 'opencode' binary" in SKIP_REASON


def _env(world, tmp_path, **extra):
    home = tmp_path / "real-home"
    xdg = {name: str(tmp_path / ("xdg-" + name.lower())) for name in
           ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME")}
    for path in xdg.values():
        Path(path).mkdir(parents=True, exist_ok=True)
    home.mkdir(exist_ok=True)
    return {**os.environ, **world.env, **xdg, "HOME": str(home), "OPENCODE_DISABLE_AUTOUPDATE": "1",
            "QUOIN_CORP_GW_API_KEY": "loopback-only-value", **extra}


def _world(tmp_path, monkeypatch, guard):
    world, root = contract._driver_world(tmp_path, guard.work.base_url if hasattr(guard.work, "base_url")  # noqa: SLF001
                                         else "http://127.0.0.1:%d/v1" % guard.work.port,
                                         git=driver.NON_GIT_DISCOVERY_VERIFIED is not True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: world.home))
    iso.launch_ready(world)
    return world, root


def _prepare(world, root, tmp_path):
    drv = driver.OpenCodeDriver(root, env=_env(world, tmp_path), home=world.home, **iso.seams(tmp_path))
    return drv.prepare(driver.RunRequest(project_root=root, task="sec", stage=None, phase="checkpoint", profile="work"))


@pytest.mark.parametrize("name,write,code", [
    ("project provider", lambda root: (root / "opencode.json").write_text(
        json.dumps({"provider": {"anthropic": {"options": {}}}})), "inventory-provider"),
    ("small_model", lambda root: (root / "opencode.json").write_text(json.dumps({"small_model": "x/y"})),
     "protected-key-overridden"),
    ("child agent model", lambda root: ((root / ".opencode" / "agent").mkdir(parents=True, exist_ok=True),
                                        (root / ".opencode" / "agent" / "helper.md").write_text(AGENT_MD)),
     "agent-model-outside-allowlist"),
])
def test_a_model_change_is_refused_and_both_listeners_stay_silent(tmp_path, monkeypatch, egress_guard, name, write, code):
    world, root = _world(tmp_path, monkeypatch, egress_guard)
    write(root)
    with pytest.raises(driver.PrepareRefused) as info:
        _prepare(world, root, tmp_path)
    assert info.value.code == code, name
    assert egress_guard.work.captures() == [] and egress_guard.personal.captures() == []
    assert egress_guard.proxy.records() == []


def test_record_whether_the_binary_honours_https_proxy(tmp_path, monkeypatch, egress_guard, record_property):
    """Records the observation; asserts nothing about the answer."""
    world, root = _world(tmp_path, monkeypatch, egress_guard)
    egress_guard.allow_proxy = True
    prepared = _prepare(world, root, tmp_path)
    env = dict(prepared.launch_env.materialize(), HTTPS_PROXY=egress_guard.proxy.url)
    import subprocess

    try:
        subprocess.run([contract._OPENCODE_BIN, "--version"], env=env, capture_output=True, timeout=20)  # noqa: SLF001
    except (OSError, subprocess.TimeoutExpired):
        pass
    record_property("provider_fetch_proxy_records", len(egress_guard.proxy.records()))
    record_property("provider_fetch_proxy_honoured", bool(egress_guard.proxy.records()))
