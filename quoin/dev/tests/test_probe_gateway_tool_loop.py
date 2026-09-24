from __future__ import annotations

import datetime

import _opencode_helpers as helpers

fake_server = helpers.load_module(helpers.OPENCODE_DIR / "fake_openai_server.py", "quoin_opencode_fake_server")
probe = helpers.load_module(helpers.OPENCODE_DIR / "probe_gateway.py", "quoin_opencode_probe_gateway")

import pytest


@pytest.fixture(autouse=True)
def _loopback(monkeypatch):
    helpers.install_loopback_guard(monkeypatch)


def test_tool_loop_20_of_20():
    now = datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.timezone.utc)
    counter = {"n": 0}

    def nonce_factory():
        counter["n"] += 1
        return "nonce-%d" % counter["n"]

    with fake_server.FakeProviderServer() as server:
        config = probe.ProbeConfig(
            base_url=server.base_url,
            model="default_ok",
            credential_env="QUOIN_PROBE_TEST_KEY",
            provider="fake",
            timeout=5.0,
        )
        env = {"QUOIN_PROBE_TEST_KEY": "sk-test-tool-loop-key"}
        ctx = probe.make_context(config, env, now=now, nonce_factory=nonce_factory)
        step1 = probe.run_step1(ctx)
        assert step1.result == "pass"
        server.clear_requests()

        failures = []
        passed_nonstream = 0
        passed_stream = 0
        for i in range(20):
            result = probe.run_tool_round_trip(ctx, stream=False, step_key="step2")
            if result.result in ("pass", "warn"):
                passed_nonstream += 1
            else:
                failures.append("iteration %d non-stream: %s" % (i, result.diagnostic.code if result.diagnostic else result.result))
            result = probe.run_tool_round_trip(ctx, stream=True, step_key="step3")
            if result.result in ("pass", "warn"):
                passed_stream += 1
            else:
                failures.append("iteration %d stream: %s" % (i, result.diagnostic.code if result.diagnostic else result.result))

        assert not failures, "\n".join(failures)
        assert passed_nonstream == 20
        assert passed_stream == 20

        recorded = server.snapshot_requests()
        assert len(recorded) == 80
        assert all(r["outcome"] == "ok" for r in recorded)
