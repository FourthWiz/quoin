"""The compiled work posture: language servers, formatters and web fetch."""
from __future__ import annotations

import copy
import dataclasses
import json

import pytest

import _opencode_helpers as helpers
from _opencode_merge_helpers import PROFILE_PERSONAL, World
from quoin.opencode_adapter import compiler, generate, names, paths, profile_policy, schema_check
from quoin.opencode_adapter.generate import ROLES

GOLDEN_DIR = helpers.SOURCE_DIR / "adapters" / "opencode" / "fixtures" / "compiled"


def _patched_view(monkeypatch, **changes):
    real = profile_policy.view

    def view(effective):
        return dataclasses.replace(real(effective), **changes)

    monkeypatch.setattr(compiler.profile_policy, "view", view)


@pytest.fixture
def work(tmp_path):
    return World(tmp_path)


@pytest.fixture
def personal(tmp_path):
    return World(tmp_path, profile=PROFILE_PERSONAL)


def test_work_document_denies_lsp_formatter_and_web(work):
    doc = compiler.build(work.evaluate()).document
    assert doc["lsp"] is False and doc["formatter"] is False
    assert doc["permission"] == {"webfetch": "deny"}
    for role in ROLES:
        assert doc["agent"][names.role_agent_name(role)]["permission"] == {"webfetch": "deny", "websearch": "deny"}
    assert "permission" not in doc["agent"]["title"] and "permission" not in doc["agent"]["compaction"]


def test_personal_defaults_add_no_new_keys_and_match_the_committed_golden(personal, tmp_path):
    doc = compiler.build(personal.evaluate()).document
    assert not {"lsp", "formatter", "permission"} & set(doc)
    assert all("permission" not in entry for entry in doc["agent"].values())
    committed = (GOLDEN_DIR / "personal.opencode.json").read_bytes()
    assert compiler.build(personal.evaluate()).native_bytes == committed


def test_builtin_mode_emits_true(work, monkeypatch):
    _patched_view(monkeypatch, lsp_mode="builtin", formatter_mode="builtin")
    doc = compiler.build(work.evaluate()).document
    assert doc["lsp"] is True and doc["formatter"] is True


def test_allow_listed_mode_disables_every_other_builtin(work, monkeypatch):
    _patched_view(monkeypatch, lsp_mode="allow-listed", lsp_allowed=("pyright", "gopls"),
                  formatter_mode="allow-listed", formatter_allowed=("ruff", "uv", "gofmt"))
    doc = compiler.build(work.evaluate()).document
    assert set(doc["lsp"]) == set(profile_policy.LSP_BUILTIN_IDS) - {"pyright", "gopls"}
    assert all(v == {"disabled": True} for v in doc["lsp"].values())
    assert set(doc["formatter"]) == set(profile_policy.FORMATTER_BUILTIN_IDS) - {"ruff", "uv", "gofmt"}
    assert "ruff" not in doc["formatter"] and "uv" not in doc["formatter"]


def test_personal_explicit_web_denial_emits_the_denials(personal, monkeypatch):
    _patched_view(monkeypatch, web_tools="deny")
    doc = compiler.build(personal.evaluate()).document
    assert doc["permission"] == {"webfetch": "deny"}
    assert doc["agent"]["quoin-planner"]["permission"] == {"webfetch": "deny", "websearch": "deny"}


def test_schema_gate_rejects_a_malformed_posture(work, monkeypatch):
    ev = work.evaluate()
    real = compiler.build_document

    def broken(evaluation):
        doc = real(evaluation)
        doc["lsp"] = {"pyright": {"disabled": False}}
        return doc

    monkeypatch.setattr(compiler, "build_document", broken)
    with pytest.raises(compiler.CompileGateError) as info:
        compiler.build(ev)
    assert info.value.gate == "schema"


@pytest.mark.parametrize("drop", ["lsp", "formatter", "permission", "role-permission"])
def test_constants_gate_requires_the_posture_under_work(work, monkeypatch, drop):
    ev = work.evaluate()
    real = compiler.build_document

    def stripped(evaluation):
        doc = real(evaluation)
        if drop == "role-permission":
            del doc["agent"]["quoin-gate"]["permission"]
        else:
            del doc[drop]
        return doc

    monkeypatch.setattr(compiler, "build_document", stripped)
    with pytest.raises(compiler.CompileGateError) as info:
        compiler.build(ev)
    assert info.value.gate == "constants"


def test_protected_keys_include_the_new_surfaces():
    assert {"lsp", "formatter", "permission"} <= set(compiler.PROTECTED_KEYS)


def test_owned_role_files_carry_no_web_permission_and_no_blanket_allow():
    for role in ROLES:
        permission = generate.role_permissions(role)
        assert "webfetch" not in permission and "websearch" not in permission
        assert permission.get("*") != "allow"


def test_work_goldens_differ_from_the_old_bytes_only_by_the_posture(work):
    committed = json.loads((GOLDEN_DIR / "work.opencode.json").read_text(encoding="utf-8"))
    stripped = copy.deepcopy(committed)
    for key in ("lsp", "formatter", "permission"):
        stripped.pop(key)
    for entry in stripped["agent"].values():
        entry.pop("permission", None)
    schema = json.loads(paths.native_schema_path().read_text(encoding="utf-8"))
    assert schema_check.validate(committed, schema, "config") == []
    assert set(committed) - set(stripped) == {"lsp", "formatter", "permission"}
