"""Tests for the portable continuation record validator and store."""

import copy
import datetime
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

QUOIN_DIR = Path(__file__).resolve().parents[2]
SCRIPT = QUOIN_DIR / "core" / "scripts" / "continuation_handoff.py"
DOC = QUOIN_DIR / "core" / "workflow" / "continuation-handoff.md"


def _load():
    spec = importlib.util.spec_from_file_location("continuation_handoff_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


ch = _load()


def valid_record():
    record = ch.example_record()
    record["created_at"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return record


def get_path(record, dotted):
    node = record
    parts = dotted.split(".")
    for part in parts[:-1]:
        node = node[part]
    return node, parts[-1]


def test_valid_record_passes():
    assert ch.validate(valid_record()) == []


def test_every_spec_item_is_carried():
    record = valid_record()
    carried = {
        "current phase": "phase",
        "completed": "completed",
        "pending": "pending",
        "decisions": "decisions",
        "artifact references": "artifacts",
        "repo revisions": "repo_revisions",
        "validation results": "validation",
        "context provenance": "provenance",
        "unavailable telemetry": "unavailable_telemetry",
        "profile": "scope.profile",
        "classification": "scope.classification",
    }
    for label, dotted in carried.items():
        parent, leaf = get_path(record, dotted)
        assert leaf in parent, label
    assert "native" not in record
    assert ch.validate(record) == []


def test_required_fields_floor():
    assert len(ch.REQUIRED_FIELDS) >= 25


@pytest.mark.parametrize("name", ch.REQUIRED_FIELDS)
def test_required_field_missing(name):
    record = valid_record()
    parent, leaf = get_path(record, name)
    del parent[leaf]
    assert "field-missing:" + name in ch.validate(record)


@pytest.mark.parametrize(
    "name", ["notes", "native", "provenance.scope_source", "repo_revisions[0].source_error"]
)
def test_optional_fields_removable(name):
    record = valid_record()
    record["notes"] = ["a note"]
    record["native"] = {"run_id": "run-1", "session_id": None}
    record["provenance"]["scope_source"] = "operator-flag"
    record["repo_revisions"][0]["source_error"] = "git-failed"
    assert ch.validate(record) == []
    if name.startswith("repo_revisions"):
        del record["repo_revisions"][0]["source_error"]
    else:
        parent, leaf = get_path(record, name)
        del parent[leaf]
    assert ch.validate(record) == []


@pytest.mark.parametrize("value", [True, 0, "false", None])
def test_transcripts_imported_must_be_false(value):
    record = valid_record()
    record["provenance"]["transcripts_imported"] = value
    assert "transcripts-imported" in ch.validate(record)


@pytest.mark.parametrize(
    "bad", ["../x", "a/../b", "/abs", "~/x", "a\\b", "a//b", "a/", "C:/x", "a\x00b", "."]
)
def test_artifact_path_refused(bad):
    record = valid_record()
    record["artifacts"][0]["path"] = bad
    assert "path-invalid:artifacts[0].path" in ch.validate(record)


@pytest.mark.parametrize("good", [".", "..", "../..", "sub"])
def test_repo_path_accepted(good):
    record = valid_record()
    record["repo_revisions"][0]["path"] = good
    assert ch.validate(record) == []


@pytest.mark.parametrize("bad", ["../x", "a/..", "./a", ""])
def test_repo_path_refused(bad):
    record = valid_record()
    record["repo_revisions"][0]["path"] = bad
    assert "path-invalid:repo_revisions[0].path" in ch.validate(record)


def test_vocabulary_real_names():
    record = valid_record()
    names = [
        "max_run_seconds",
        "max_tool_calls",
        "max_context_tokens",
        "max_output_tokens",
        "max_transient_retries",
        "subagent_depth",
    ]
    ceiling = record["scope"]["policy_ceiling"]
    ceiling["limits"] = {n: 5 for n in names}
    ceiling["role_models"] = {"planner": "prov/model_x:latest"}
    ceiling["provider_allowlist"] = ["prov/model_x:latest"]
    record["scope"]["profile"] = "my_profile"
    record["native"] = {"run_id": "oc-run_1", "session_id": "ses_01ABC_def"}
    assert ch.validate(record) == []


@pytest.mark.parametrize("model", ["a b", "a\nb", "a\u202eb", ""])
def test_model_ref_refused(model):
    record = valid_record()
    record["scope"]["policy_ceiling"]["role_models"]["planner"] = model
    assert "field-invalid:scope.policy_ceiling.role_models.planner" in ch.validate(record)


@pytest.mark.parametrize("token", ["_x", "a b", "a\nb"])
def test_token_refused(token):
    record = valid_record()
    record["scope"]["profile"] = token
    assert "field-invalid:scope.profile" in ch.validate(record)


def test_limits_vocabulary_in_adapter_names():
    from quoin.opencode_adapter import merge

    record = valid_record()
    record["scope"]["policy_ceiling"]["limits"] = {n: 1 for n in merge.LIMIT_NAMES}
    assert ch.validate(record) == []


def test_size_boundary(tmp_path):
    record = valid_record()
    record["notes"] = ["x"]
    base = len(ch.canonical_bytes(record))
    # pad notes (100 entries of up to 2000 chars) to sit at the boundary
    record["notes"] = ["x" * 2000 for _ in range(100)]
    assert ch.validate(record) == []
    original_max = ch.MAX_RECORD_BYTES
    try:
        size = len(ch.canonical_bytes(record))
        assert size > base
        ch.MAX_RECORD_BYTES = size - 1
        assert ch.validate(record) == ["record-too-large"]
        ch.MAX_RECORD_BYTES = size
        assert ch.validate(record) == []
        target = tmp_path / "r.json"
        ch.write_record(str(target), record)
        assert target.exists()
    finally:
        ch.MAX_RECORD_BYTES = original_max


def test_text_rules():
    record = valid_record()
    record["decisions"][0]["text"] = "x" * 2001
    assert "text-oversized:decisions[0].text" in ch.validate(record)
    for bad in ("a\x1bb", "a\u2028b", "a\u202eb"):
        record["decisions"][0]["text"] = bad
        assert "text-control-character:decisions[0].text" in ch.validate(record)
    record["decisions"][0]["text"] = "line one\n\tline two"
    assert ch.validate(record) == []
    record["decisions"][0]["source"] = "a\nb"
    assert "field-invalid:decisions[0].source" in ch.validate(record)


def test_unknown_keys_and_schema():
    record = valid_record()
    record["extra"] = 1
    record["scope"]["policy_ceiling"]["extra"] = 1
    reasons = ch.validate(record)
    assert "field-unknown:extra" in reasons
    assert "field-unknown:scope.policy_ceiling.extra" in reasons
    record = valid_record()
    record["schema"] = "quoin-continuation/2"
    assert ch.validate(record) == ["schema-unsupported"]
    record["schema"] = "quoin-continuation"
    assert "field-invalid:schema" in ch.validate(record)


def test_duplicates_and_overlap():
    record = valid_record()
    record["completed"].append(copy.deepcopy(record["completed"][0]))
    assert "duplicate-entry:completed" in ch.validate(record)
    record = valid_record()
    record["pending"].append({"phase": "implement", "stage": 1})
    assert "duplicate-entry:pending" in ch.validate(record)
    record = valid_record()
    record["validation"].append(copy.deepcopy(record["validation"][0]))
    assert "duplicate-entry:validation" in ch.validate(record)
    record = valid_record()
    record["artifacts"].append(copy.deepcopy(record["artifacts"][0]))
    assert "duplicate-entry:artifacts" in ch.validate(record)
    record = valid_record()
    record["repo_revisions"].append(copy.deepcopy(record["repo_revisions"][0]))
    assert "duplicate-entry:repo_revisions" in ch.validate(record)
    record = valid_record()
    record["pending"].append({"phase": "plan", "stage": 1})
    assert "pending-overlaps-completed" in ch.validate(record)


def test_phase_inconsistent_branches():
    record = valid_record()
    record["phase"] = {"current": None, "stage": None, "status": "done"}
    assert "phase-inconsistent" in ch.validate(record)  # pending not empty
    record = valid_record()
    record["pending"] = []
    assert "phase-inconsistent" in ch.validate(record)  # current set, pending empty
    record = valid_record()
    record["phase"] = {"current": "review", "stage": 2, "status": "pending"}
    assert "phase-inconsistent" in ch.validate(record)  # not a pending member
    record = valid_record()
    record["phase"]["status"] = "done"
    assert "phase-inconsistent" in ch.validate(record)
    record = valid_record()
    record["pending"] = []
    record["phase"] = {"current": None, "stage": None, "status": "done"}
    assert ch.validate(record) == []


@pytest.mark.parametrize("stage", [True, 0, "1"])
def test_stage_values_refused(stage):
    record = valid_record()
    record["pending"][0]["stage"] = stage
    assert any(r.startswith("field-invalid:pending[0].stage") for r in ch.validate(record))


def test_stageless_phase_refuses_stage():
    record = valid_record()
    record["pending"].append({"phase": "architect", "stage": 1})
    assert "field-invalid:pending[2].stage" in ch.validate(record)


def test_list_caps():
    for name, cap in (("completed", 1000), ("decisions", 100), ("notes", 100), ("artifacts", 5000)):
        record = valid_record()
        record[name] = [None] * (cap + 1)
        assert "list-too-long:" + name in ch.validate(record)
    record = valid_record()
    record["scope"]["policy_ceiling"]["enabled_providers"] = ["p"] * 257
    assert "list-too-long:scope.policy_ceiling.enabled_providers" in ch.validate(record)
    record = valid_record()
    record["unavailable_telemetry"] = ["t%d" % i for i in range(33)]
    assert "list-too-long:unavailable_telemetry" in ch.validate(record)


def test_not_object():
    assert ch.validate([]) == ["record-not-object"]


# -- compare_scope -----------------------------------------------------------


def scope():
    return copy.deepcopy(valid_record()["scope"])


def test_compare_equal_and_narrowing():
    assert ch.compare_scope(scope(), scope()) == []
    req = scope()
    c = req["policy_ceiling"]
    c["enabled_providers"] = ["alpha"]
    c["provider_allowlist"] = ["alpha/model-one"]
    c["role_models"] = {}
    c["limits"] = {"max_run_seconds": 100, "max_turns": 7}
    assert ch.compare_scope(scope(), req) == []


def test_compare_profile_and_classification():
    req = scope()
    req["profile"] = "other"
    assert ("profile-mismatch", "profile") in ch.compare_scope(scope(), req)
    for before, after in (("work", "personal"), ("personal", "work")):
        rec, req = scope(), scope()
        rec["classification"], req["classification"] = before, after
        assert ch.compare_scope(rec, req) == [("classification-mismatch", "classification")]


@pytest.mark.parametrize(
    "mutate,detail",
    [
        (lambda c: c["enabled_providers"].append("gamma"), "policy_ceiling.enabled_providers"),
        (lambda c: c["provider_allowlist"].append("gamma/m"), "policy_ceiling.provider_allowlist"),
        (lambda c: c["role_models"].update(planner="beta/model-two"), "policy_ceiling.role_models.planner"),
        (lambda c: c["role_models"].update(critic="alpha/model-one"), "policy_ceiling.role_models.critic"),
        (lambda c: c["limits"].update(max_run_seconds=601), "policy_ceiling.limits.max_run_seconds"),
        (lambda c: c["limits"].update(max_run_seconds=None), "policy_ceiling.limits.max_run_seconds"),
        (lambda c: c.update(network="open"), "policy_ceiling.network"),
    ],
)
def test_compare_widened(mutate, detail):
    req = scope()
    mutate(req["policy_ceiling"])
    assert ("policy-widened", detail) in ch.compare_scope(scope(), req)


def test_compare_multiple_and_malformed():
    req = scope()
    req["profile"] = "other"
    req["policy_ceiling"]["network"] = "open"
    codes = {c for c, _ in ch.compare_scope(scope(), req)}
    assert codes == {"profile-mismatch", "policy-widened"}
    assert ch.compare_scope({}, scope()) == [("scope-invalid", "recorded")]
    assert ch.compare_scope(scope(), "x") == [("scope-invalid", "requested")]


def test_values_never_in_details():
    req = scope()
    req["policy_ceiling"]["limits"]["max_run_seconds"] = 987654
    for _, detail in ch.compare_scope(scope(), req):
        assert "987654" not in detail


# -- load and write ----------------------------------------------------------


def make_dir(tmp_path):
    d = tmp_path / "cont"
    d.mkdir()
    return d


def test_round_trip_and_prev(tmp_path):
    d = make_dir(tmp_path)
    path = str(d / "t.json")
    first = valid_record()
    ch.write_record(path, first)
    first_bytes = Path(path).read_bytes()
    assert ch.load_record(path) == first
    ch.write_record(path, first)
    assert Path(path).read_bytes() == first_bytes
    second = valid_record()
    second["task"] = "other-task"
    ch.write_record(path, second)
    assert (d / "t.prev.json").read_bytes() == first_bytes
    assert sorted(p.name for p in d.iterdir()) == ["t.json", "t.prev.json"]
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"


def test_invalid_refused_before_write(tmp_path):
    d = make_dir(tmp_path)
    record = valid_record()
    del record["scope"]
    with pytest.raises(ch.RecordError) as exc:
        ch.write_record(str(d / "t.json"), record)
    assert exc.value.code == "record-invalid"
    assert "field-missing:scope" in exc.value.reasons
    assert list(d.iterdir()) == []


def test_symlinks_refused(tmp_path):
    d = make_dir(tmp_path)
    real = tmp_path / "real.json"
    real.write_text("{}")
    (d / "t.json").symlink_to(real)
    with pytest.raises(ch.RecordError) as exc:
        ch.write_record(str(d / "t.json"), valid_record())
    assert exc.value.code == "unsafe-path"
    with pytest.raises(ch.RecordError) as exc:
        ch.load_record(str(d / "t.json"))
    assert exc.value.code == "unsafe-path"
    link = tmp_path / "linkdir"
    link.symlink_to(d)
    with pytest.raises(ch.RecordError) as exc:
        ch.write_record(str(link / "x.json"), valid_record())
    assert exc.value.code == "unsafe-path"
    with pytest.raises(ch.RecordError) as exc:
        ch.load_record(str(link / "x.json"))
    assert exc.value.code == "unsafe-path"


@pytest.mark.parametrize(
    "content,code",
    [
        (b'{"a": 1, "a": 2}', "json-invalid"),
        (b'{"a": NaN}', "json-invalid"),
        (b"\xff\xfe", "json-invalid"),
        (b"[1]", "record-not-object"),
    ],
)
def test_load_errors(tmp_path, content, code):
    d = make_dir(tmp_path)
    (d / "t.json").write_bytes(content)
    with pytest.raises(ch.RecordError) as exc:
        ch.load_record(str(d / "t.json"))
    assert exc.value.code == code


def test_load_missing_and_oversize(tmp_path, monkeypatch):
    d = make_dir(tmp_path)
    with pytest.raises(ch.RecordError) as exc:
        ch.load_record(str(d / "nope.json"))
    assert exc.value.code == "record-missing"
    monkeypatch.setattr(ch, "MAX_RECORD_BYTES", 10)
    (d / "big.json").write_bytes(b"{" + b" " * 50 + b"}")
    with pytest.raises(ch.RecordError) as exc:
        ch.load_record(str(d / "big.json"))
    assert exc.value.code == "record-too-large"
    (d / "sub").mkdir()
    with pytest.raises(ch.RecordError) as exc:
        ch.load_record(str(d / "sub"))
    assert exc.value.code == "record-unreadable"


def test_no_temp_left_after_failure(tmp_path, monkeypatch):
    d = make_dir(tmp_path)

    def boom(*args, **kwargs):
        raise OSError("injected")

    monkeypatch.setattr(ch.os, "replace", boom)
    with pytest.raises(OSError):
        ch.write_record(str(d / "t.json"), valid_record())
    assert list(d.iterdir()) == []


# -- CLI ---------------------------------------------------------------------


def run_cli(*args):
    return subprocess.run([sys.executable, str(SCRIPT)] + list(args), capture_output=True, text=True)


def test_cli_validate(tmp_path):
    good = tmp_path / "good.json"
    good.write_text(json.dumps(valid_record()))
    bad = tmp_path / "bad.json"
    record = valid_record()
    del record["scope"]
    bad.write_text(json.dumps(record))
    assert run_cli("--validate", str(good)).returncode == 0
    result = run_cli("--validate", str(bad))
    assert result.returncode == 1
    assert "FAIL field-missing:scope" in result.stdout
    result = run_cli("--validate", str(tmp_path / "none.json"))
    assert result.returncode == 2
    assert "ERROR record-missing" in result.stdout
    assert run_cli().returncode == 2


def test_cli_self_test():
    result = run_cli("--self-test")
    assert result.returncode == 0
    assert result.stdout.startswith("PASS: --self-test")


# -- doc agreement -----------------------------------------------------------


def test_doc_table_matches_script():
    names = set()
    for line in DOC.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\| `([^`]+)` \| ([RO]) \|", line)
        if m:
            names.add(m.group(1))
            expected = "R" if (m.group(1) in ch.REQUIRED_FIELDS or m.group(1) in ch.ITEM_FIELDS) else "O"
            if m.group(1) not in ch.ITEM_FIELDS:
                assert m.group(2) == expected, m.group(1)
    script = set(ch.REQUIRED_FIELDS) | set(ch.OPTIONAL_FIELDS) | set(ch.ITEM_FIELDS)
    assert names == script


def test_many_distinct_bad_fields_validate_quickly_with_capped_reasons():
    import time

    record = valid_record()
    for i in range(20000):
        record["x%d" % i] = 1
    start = time.monotonic()
    reasons = ch.validate(record)
    assert time.monotonic() - start < 5
    assert len(reasons) <= ch.MAX_REASONS + 1
    assert reasons[-1] == "reasons-truncated"


def test_duplicate_reasons_are_reported_once():
    chk = ch._Checker()
    for _ in range(5):
        chk.add("field-invalid", "a")
    assert chk.reasons == ["field-invalid:a"]


def test_previous_record_slot_task_is_refused(tmp_path):
    record = valid_record()
    record["task"] = "foo.prev"
    with pytest.raises(ch.RecordError):
        ch.write_record(str(tmp_path / "foo.prev.json"), record)
