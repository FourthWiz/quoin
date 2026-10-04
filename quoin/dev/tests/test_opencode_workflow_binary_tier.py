"""Opt-in tier: the whole-task coordinator against a real OpenCode binary and a
loopback provider fake. Skipped, with its reason, when no binary pinned to the
adapter's release is on PATH, which is the expected case almost everywhere.

Commands proved here: quoin-discover, quoin-run (the `--workflow` form).
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

import test_opencode_binary_contract as contract
from quoin import cli

SKIP_REASON = contract.skip_reason(contract._OPENCODE_BIN, contract._PINNED_VERSION, contract._BINARY_VERSION)  # noqa: SLF001


def test_the_tier_names_why_it_is_skipped():
    """Always on: a machine without the pinned binary skips the tier with text,
    never silently."""
    assert contract._PINNED_VERSION == "1.18.32"  # noqa: SLF001
    assert "no 'opencode' binary" in contract.skip_reason(None, "1.18.32", None)
    assert "1.18.32" in contract.skip_reason("/bin/opencode", "1.18.32", "1.0.0")
    assert contract.skip_reason("/bin/opencode", "1.18.32", "opencode 1.18.32") is None
    assert SKIP_REASON is None or isinstance(SKIP_REASON, str)


@pytest.mark.skipif(SKIP_REASON is not None, reason=SKIP_REASON or "")
def test_workflow_discover_launches_the_pinned_binary(tmp_path, monkeypatch, capsys):
    import _opencode_helpers as helpers
    from quoin.opencode_adapter import driver, runstore

    helpers.install_loopback_guard(monkeypatch)
    server_module = helpers.load_module(
        helpers.OPENCODE_DIR / "fake_openai_server.py", "fake_openai_server_workflow_case"
    )
    with server_module.FakeProviderServer() as server:
        world, root = contract._driver_world(tmp_path, server.base_url, git=driver.NON_GIT_DISCOVERY_VERIFIED is not True)  # noqa: SLF001
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: world.home))
        env = {**os.environ, **world.env, "HOME": str(world.home),
               "OPENCODE_DISABLE_AUTOUPDATE": "1", "QUOIN_CORP_GW_API_KEY": "loopback-only-value"}
        monkeypatch.setattr(cli, "_make_opencode_driver", lambda r: driver.OpenCodeDriver(r, env=env, home=world.home))
        monkeypatch.setattr(cli, "_opencode_backoff", lambda n: 0)
        (root / ".workflow_artifacts" / "wf-demo").mkdir(parents=True, exist_ok=True)
        code = cli.main(["run", "wf-demo", "--runtime", "opencode", "--project-root", str(root),
                         "--profile", "work", "--workflow", "--from-discover", "--through", "discover",
                         "--no-pause"])
        summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        # a scripted model may not write valid discover files, so the gate verdict is not asserted
        assert summary["mode"] == "workflow", summary
        assert summary["phases"] and summary["phases"][0]["phase"] == "discover", summary
        run_ids = summary["phases"][0]["run_ids"]
        assert run_ids, summary
        record = runstore.load_record(runstore.store_dir(root), run_ids[0])
        assert record["request"]["phase"] == "discover"
        assert record["request"]["non_interactive"] is True
        state = runstore.load_workflow_state(runstore.store_dir(root), "wf-demo") or {}
        assert any(e["phase"] == "discover" for e in state.get("entries") or []), state
        survivors = subprocess.run(["pgrep", "-f", str(root)], capture_output=True, text=True, timeout=10).stdout.split()
        assert not [pid for pid in survivors if pid != str(os.getpid())], survivors
        assert code in (0, 2, 4, 5, 7, 8)
