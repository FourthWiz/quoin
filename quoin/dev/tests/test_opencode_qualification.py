"""Tests for qualification: capability records written by the gateway probe
are judged against the current endpoint, model, runtime version and clock."""
from __future__ import annotations

import ast
import json
import os
import socket
import subprocess
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import _opencode_helpers as helpers
from _opencode_merge_helpers import loaded
from quoin.opencode_adapter import jsonio, manifest, merge, paths, qualification
from quoin.opencode_adapter.qualification import QualificationResult, evaluate, evaluate_all
from test_opencode_runtime_config import RecordingEnv

SRC_DIR = helpers.SOURCE_DIR.parent / "src" / "quoin" / "opencode_adapter"
UTC = timezone.utc
NOW = datetime(2026, 9, 29, 12, 0, 0, tzinfo=UTC)
PINNED = "9.9.9"
BASE_URL = "https://gateway.example.invalid/v1"
MODEL_ID = "example-vendor/example-coder"


@pytest.fixture(scope="module")
def probe():
    return helpers.load_module(helpers.OPENCODE_DIR / "probe_gateway.py", "probe_gateway_qualification")


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("qualification must not use the network or a subprocess")

    for owner, name in (
        (socket.socket, "connect"),
        (socket, "create_connection"),
        (subprocess, "run"),
        (subprocess, "Popen"),
    ):
        monkeypatch.setattr(owner, name, boom)


class Sandbox:
    def __init__(self, tmp_path):
        self.env = RecordingEnv({"XDG_CONFIG_HOME": str(tmp_path / "xdg")})
        self.home = tmp_path / "home"
        eff = merge.merge(loaded())
        self.model = eff.models["work-coder"]
        self.provider = eff.providers["corp-gw"]
        self.effective = eff
        self.path = paths.qualification_path("work-coder", self.env, self.home)

    def write(self, record, name="work-coder", mode=0o600):
        path = paths.qualification_path(name, self.env, self.home)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)
        text = record if isinstance(record, str) else json.dumps(record)
        path.write_text(text, encoding="utf-8")
        os.chmod(path, mode)
        return path

    def evaluate(self, now=NOW, pinned=PINNED, model=None, provider=None):
        return evaluate(
            model or self.model, provider or self.provider,
            env=self.env, home=self.home, now=now, pinned_version=pinned,
        )


@pytest.fixture
def box(tmp_path):
    return Sandbox(tmp_path)


def probe_record(probe, *, verdict="qualified", results=("pass", "pass", "pass"), now=None,
                 base_url=BASE_URL, model=MODEL_ID, version=PINNED, blocking=None):
    names = ("auth_and_text", "tool_round_trip", "streaming")
    steps = [
        {"step": i + 1, "name": names[i], "result": results[i], "diagnostic": None}
        for i in range(3)
    ]
    report = probe.ProbeReport(context=None, steps=steps, verdict=verdict, blocking_step=blocking)
    cfg = probe.ProbeConfig(
        base_url=base_url, model=model, provider="corp-gw", credential_env="X", runtime_version=version
    )
    return probe.build_capability_record(report, cfg, now=now or NOW - timedelta(days=1))


def state(result):
    return (result.state, result.reason)


# ------------------------------------------------------------------ states


def test_schema_constant_matches_the_probe(probe):
    assert qualification.RECORD_SCHEMA == probe.RECORD_SCHEMA
    assert qualification.RECORD_SCHEMA_VERSION == 1


def test_qualified_record_from_the_probe(box, probe):
    box.write(probe_record(probe))
    res = box.evaluate()
    assert res == QualificationResult(
        "work-coder", "qualified", None, False, (NOW - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def test_missing_record(box):
    assert state(box.evaluate()) == ("missing", None)


@pytest.mark.parametrize(
    "verdict,blocking,expected",
    [("not_qualified", 2, ("failed", "not-qualified")), ("could_not_run", 1, ("failed", "could-not-run"))],
)
def test_failed_records(box, probe, verdict, blocking, expected):
    box.write(probe_record(probe, verdict=verdict, results=("fail", "skipped", "skipped"), blocking=blocking))
    assert state(box.evaluate()) == expected


def test_age_boundary(box, probe):
    exactly = NOW - qualification.QUALIFICATION_MAX_AGE
    box.write(probe_record(probe, now=exactly))
    assert state(box.evaluate()) == ("qualified", None)
    box.write(probe_record(probe, now=exactly - timedelta(seconds=1)))
    assert state(box.evaluate()) == ("stale", "too-old")


def test_future_timestamp_with_skew(box, probe):
    box.write(probe_record(probe, now=NOW + qualification.CLOCK_SKEW))
    assert state(box.evaluate()) == ("qualified", None)
    box.write(probe_record(probe, now=NOW + qualification.CLOCK_SKEW + timedelta(seconds=1)))
    assert state(box.evaluate()) == ("malformed", "future-timestamp")


def test_model_runtime_and_endpoint_mismatches(box, probe):
    box.write(probe_record(probe, model="example-vendor/other"))
    assert state(box.evaluate()) == ("mismatched", "model-mismatch")
    box.write(probe_record(probe, version="1.2.3"))
    assert state(box.evaluate()) == ("mismatched", "runtime-mismatch")
    rec = probe_record(probe)
    rec["key"]["runtime"]["name"] = "other"
    box.write(rec)
    assert state(box.evaluate()) == ("mismatched", "runtime-mismatch")
    box.write(probe_record(probe, base_url="https://other.example.invalid/v1"))
    assert state(box.evaluate()) == ("mismatched", "endpoint-mismatch")
    rec = probe_record(probe)
    rec["key"]["endpoint"] = None
    box.write(rec)
    assert state(box.evaluate()) == ("mismatched", "endpoint-mismatch")


def test_provider_label_is_not_part_of_the_identity(box, probe):
    rec = probe_record(probe)
    rec["key"]["provider"] = "some-other-label"
    box.write(rec)
    assert state(box.evaluate()) == ("qualified", None)


def _mutations():
    def drop(*path):
        def apply(rec):
            node = rec
            for part in path[:-1]:
                node = node[part]
            del node[path[-1]]
        return apply

    def setv(value, *path):
        def apply(rec):
            node = rec
            for part in path[:-1]:
                node = node[part]
            node[path[-1]] = value
        return apply

    return [
        ("wrong-schema", setv("other.schema", "schema"), "bad-schema"),
        ("wrong-version", setv(2, "schema_version"), "bad-schema"),
        ("bool-version", setv(True, "schema_version"), "bad-schema"),
        ("no-schema", drop("schema"), "bad-schema"),
        ("no-key", drop("key"), "bad-shape"),
        ("key-list", setv([], "key"), "bad-shape"),
        ("no-runtime", drop("key", "runtime"), "bad-shape"),
        ("runtime-string", setv("opencode", "key", "runtime"), "bad-shape"),
        ("no-verdict", drop("verdict"), "bad-shape"),
        ("bool-status", setv(True, "verdict", "status"), "bad-shape"),
        ("int-status", setv(1, "verdict", "status"), "bad-shape"),
        ("odd-status", setv("mystery", "verdict", "status"), "bad-shape"),
        ("no-probed-at", drop("probed_at"), "bad-shape"),
        ("bool-probed-at", setv(False, "probed_at"), "bad-shape"),
        ("bad-timestamp", setv("2026-09-28 12:00:00", "probed_at"), "bad-shape"),
        ("offset-timestamp", setv("2026-09-28T12:00:00+00:00", "probed_at"), "bad-shape"),
    ]


@pytest.mark.parametrize("label,mutate,reason", _mutations(), ids=[m[0] for m in _mutations()])
def test_malformed_records(box, probe, label, mutate, reason):
    rec = probe_record(probe)
    mutate(rec)
    box.write(rec)
    assert state(box.evaluate()) == ("malformed", reason)


@pytest.mark.parametrize("text", ["{not json", "[1, 2]", '"text"', "null", "", '{"a": 1, "a": 2}'])
def test_unparseable_or_non_object_records(box, text):
    box.write(text)
    got = state(box.evaluate())
    assert got[0] == "malformed" and got[1] in ("unreadable", "bad-shape")


# ------------------------------------------------------ endpoint identity


@pytest.mark.parametrize(
    "provider_url,expected",
    [
        ("https://GATEWAY.example.invalid/v1", ("qualified", None)),
        ("https://gateway.example.invalid:443/v1", ("mismatched", "endpoint-mismatch")),
        ("https://gateway.example.invalid/v1/", ("mismatched", "endpoint-mismatch")),
    ],
)
def test_endpoint_identity_matches_the_probe(box, probe, provider_url, expected):
    box.write(probe_record(probe, base_url=BASE_URL))
    data = box.provider
    other = merge.ProviderView(
        id=data.id, kind=data.kind, endpoint_family=data.endpoint_family, host_key=data.host_key,
        credential_env=data.credential_env, base_url=provider_url,
    )
    from quoin.opencode_adapter import config

    assert config.endpoint_identity(provider_url) == probe.endpoint_identity(provider_url)
    assert state(box.evaluate(provider=other)) == expected


# ------------------------------------------------------------- capabilities


def test_reasoning_support_only_when_qualified(box, probe):
    rec = probe_record(probe)
    rec["capabilities"]["reasoning_parameters"] = {"status": "supported", "source": "observed"}
    box.write(rec)
    assert box.evaluate().reasoning_supported is True
    rec = probe_record(probe, verdict="not_qualified", results=("fail", "skipped", "skipped"), blocking=1)
    rec["capabilities"]["reasoning_parameters"] = {"status": "supported"}
    box.write(rec)
    assert box.evaluate().reasoning_supported is False
    box.write(probe_record(probe))
    assert box.evaluate().reasoning_supported is False  # the probe records it as unknown


@pytest.mark.parametrize(
    "capabilities",
    [[], "text", {"reasoning_parameters": "supported"}, {"reasoning_parameters": []}, None, 5],
)
def test_malformed_capabilities_never_break_qualification(box, probe, capabilities):
    rec = probe_record(probe)
    rec["capabilities"] = capabilities
    box.write(rec)
    res = box.evaluate()
    assert state(res) == ("qualified", None) and res.reasoning_supported is False
    rec = probe_record(probe)
    del rec["capabilities"]
    box.write(rec)
    assert state(box.evaluate()) == ("qualified", None)


# ------------------------------------------------------------ trust checks


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_world_writable_record_is_not_trusted(box, probe):
    box.write(probe_record(probe), mode=0o666)
    assert state(box.evaluate()) == ("malformed", "unsafe-permissions")
    box.write(probe_record(probe), mode=0o664)
    assert state(box.evaluate()) == ("qualified", None)  # group-writable is accepted


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_record_owned_by_another_user_is_not_trusted(box, probe, monkeypatch):
    box.write(probe_record(probe))
    real = jsonio.load_strict_with_stat

    def foreign(path, **kw):
        tree, info = real(path, **kw)
        return tree, SimpleNamespace(st_mode=info.st_mode, st_uid=info.st_uid + 1)

    monkeypatch.setattr(jsonio, "load_strict_with_stat", foreign)
    assert state(box.evaluate()) == ("malformed", "unsafe-permissions")


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_world_writable_qualifications_directory_is_refused(box, probe):
    path = box.write(probe_record(probe))
    os.chmod(path.parent, 0o777)
    try:
        assert state(box.evaluate()) == ("malformed", "unsafe-permissions")
    finally:
        os.chmod(path.parent, 0o700)


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="platform lacks O_NOFOLLOW")
def test_symlinked_record_is_refused(box, probe, tmp_path):
    real = tmp_path / "real.json"
    real.write_text(json.dumps(probe_record(probe)), encoding="utf-8")
    os.chmod(real, 0o600)
    box.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(box.path.parent, 0o700)
    box.path.symlink_to(real)
    assert state(box.evaluate()) == ("malformed", "unreadable")


def test_record_that_is_a_directory_is_unreadable(box):
    box.path.mkdir(parents=True)
    os.chmod(box.path.parent, 0o700)
    assert state(box.evaluate())[0] == "malformed"


# ------------------------------------------------------------------- misc


def test_naive_now_is_a_programming_error(box):
    with pytest.raises(ValueError):
        box.evaluate(now=datetime(2026, 9, 29, 12, 0, 0))


def test_non_utc_aware_now_is_accepted(box, probe):
    box.write(probe_record(probe))
    zone = timezone(timedelta(hours=5))
    assert state(box.evaluate(now=NOW.astimezone(zone))) == ("qualified", None)


def test_pinned_version_reads_the_compatibility_document():
    assert qualification.pinned_version() == manifest.read_pinned_version(helpers.SOURCE_DIR)


def test_pinned_version_reports_missing_adapter_data(monkeypatch):
    monkeypatch.setattr(paths, "adapter_data_dir", lambda: None)
    with pytest.raises(paths.AdapterDataMissing):
        qualification.pinned_version()


def test_pinned_version_maps_manifest_errors(monkeypatch):
    def broken(source_dir):
        raise manifest.ManifestLoadError("boom")

    monkeypatch.setattr(manifest, "read_pinned_version", broken)
    with pytest.raises(paths.AdapterDataMissing):
        qualification.pinned_version()


def test_evaluate_all_covers_every_model_sorted(box, probe):
    box.write(probe_record(probe))
    results = evaluate_all(box.effective, env=box.env, home=box.home, now=NOW, pinned_version=PINNED)
    assert list(results) == ["work-coder", "work-planner"]
    assert state(results["work-coder"]) == ("qualified", None)
    assert state(results["work-planner"]) == ("missing", None)
    with pytest.raises(TypeError):
        results["x"] = 1
    assert set(box.env.reads) <= {"XDG_CONFIG_HOME"}


def test_results_never_carry_record_content_or_paths(box, probe, tmp_path):
    rec = probe_record(probe)
    rec["key"]["provider"] = "sk-test-" + "SEEDED-SECRET-0000"
    rec["verdict"]["summary"] = "sk-test-" + "SEEDED-SECRET-0000"
    box.write(rec)
    res = box.evaluate()
    assert str(tmp_path) not in repr(res)
    assert "SEEDED" not in repr(res)
    rec["verdict"]["status"] = "sk-test-" + "SEEDED-SECRET-0000"
    box.write(rec)
    res = box.evaluate()
    assert "SEEDED" not in repr(res) and str(tmp_path) not in repr(res)


def test_qualification_imports_are_limited():
    tree = ast.parse((SRC_DIR / "qualification.py").read_text(encoding="utf-8"))
    relative = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1:
            relative |= {node.module} if node.module else {a.name for a in node.names}
    assert relative <= {"config", "errors", "jsonio", "manifest", "merge", "paths"}
