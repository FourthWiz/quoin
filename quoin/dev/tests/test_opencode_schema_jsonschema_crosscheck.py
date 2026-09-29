"""Cross-check the committed runtime-config schema against a standard
validator, and guard the package's empty runtime dependency list."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURES = ROOT / "adapters" / "opencode" / "fixtures" / "runtime-config"
SCHEMA = ROOT / "adapters" / "opencode" / "schemas" / "runtime-config.schema.json"
PYPROJECT = ROOT.parent / "pyproject.toml"


def test_no_runtime_dependencies():
    text = PYPROJECT.read_text(encoding="utf-8")
    match = re.search(r"^\[project\]\n(.*?)(?=^\[)", text, re.S | re.M)
    assert match, "no [project] table"
    block = match.group(1)
    assert not re.search(r"^dependencies\s*=", block, re.M)


def _cases():
    cases = json.loads((FIXTURES / "cases.json").read_text(encoding="utf-8"))
    return [c for c in cases["valid"] + cases["invalid"] if c["jsonschema_valid"] is not None]


def test_fixture_reads_come_from_the_committed_tree():
    for case in _cases():
        assert (FIXTURES / case["file"]).is_file()
    assert str(FIXTURES).startswith(str(ROOT))


def test_committed_fixtures_agree_with_a_standard_validator():
    jsonschema = pytest.importorskip("jsonschema", reason="jsonschema is a dev-only dependency")
    root = json.loads(SCHEMA.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(root)
    checked = 0
    for case in _cases():
        instance = json.loads((FIXTURES / case["file"]).read_text(encoding="utf-8"))
        wrapper = {"$ref": "#/$defs/" + case["layer"], "$defs": root["$defs"]}
        valid = jsonschema.Draft202012Validator(wrapper).is_valid(instance)
        assert valid == case["jsonschema_valid"], case["id"]
        checked += 1
    assert checked > 20


def test_native_subset_schema_agrees_with_a_standard_validator(tmp_path):
    jsonschema = pytest.importorskip("jsonschema", reason="jsonschema is a dev-only dependency")
    import copy

    import test_opencode_native_schema as native
    from _opencode_merge_helpers import World
    from quoin.opencode_adapter import compiler

    jsonschema.Draft202012Validator.check_schema(native.SCHEMA)
    validator = jsonschema.Draft202012Validator(
        {"$ref": "#/$defs/config", "$defs": native.SCHEMA["$defs"]}
    )
    doc = compiler.build(World(tmp_path).evaluate()).document
    assert validator.is_valid(doc)
    checked = 0
    for name, mutate in sorted(native.NEGATIVES.items()):
        broken = copy.deepcopy(doc)
        mutate(broken)
        assert not validator.is_valid(broken), name
        checked += 1
    assert checked >= 15
