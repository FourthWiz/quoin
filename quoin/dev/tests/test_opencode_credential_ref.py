"""Tests for the credential reference grammar."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from quoin.opencode_adapter import secrets as refs

SCHEMA = (
    Path(__file__).resolve().parent.parent.parent
    / "adapters" / "opencode" / "schemas" / "runtime-config.schema.json"
)


def test_valid_env_and_keychain():
    env = refs.parse("env:QUOIN_WORK_API_KEY")
    assert (env.scheme, env.name) == ("env", "QUOIN_WORK_API_KEY")
    kc = refs.parse("keychain:quoin/work/qwen")
    assert (kc.scheme, kc.service, kc.account) == ("keychain", "quoin", "work/qwen")
    assert refs.parse("keychain:svc.name-1/a@b+c").account == "a@b+c"
    assert refs.parse("env:_X").name == "_X"


def _sk_shaped():
    return "sk-" + "proj-" + "Ab1cdEf2GhIj3KlMn4OpQr5"


@pytest.mark.parametrize(
    "text",
    [
        "env:lowercase",
        "env:",
        "env:1X",
        "keychain:svc",
        "keychain:/acct",
        "keychain:svc/",
        "env:A B",
        " env:X",
        "env:X ",
        "{env:X}",
        "file:x",
        "other:thing",
        "",
        "env:" + "A" * 129,
        "keychain:" + "s" * 129 + "/a",
        "keychain:svc/" + "a" * 257,
        "keychain:svc/acct\n",
        "env:X\n",
        _sk_shaped(),
    ],
)
def test_rejections_never_echo_input(text):
    with pytest.raises(ValueError) as info:
        refs.parse(text)
    if len(text) > 8:
        assert text not in str(info.value)


def test_non_string_rejected():
    with pytest.raises(ValueError):
        refs.parse(None)


def test_masking_and_repr_hide_account():
    kc = refs.parse("keychain:quoin/very-secret-account")
    assert kc.masked() == "keychain:quoin/***"
    assert "very-secret-account" not in repr(kc)
    assert "very-secret-account" not in str(kc)
    assert refs.parse("env:ABC").masked() == "env:ABC"
    assert repr(refs.parse("env:ABC")) == "CredentialRef(env:ABC)"


def test_schema_pattern_equals_grammar_constant():
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    assert schema["$defs"]["credential_ref"]["pattern"] == refs.CREDENTIAL_REF_PATTERN
    assert re.search(refs.CREDENTIAL_REF_PATTERN, "env:ABC")


def test_module_never_reads_the_process_environment():
    # The subprocess machinery is confined to one function; the resolver tests
    # check that placement with an AST walk.
    text = Path(refs.__file__).read_text(encoding="utf-8")
    for word in ("os.environ", "getenv", "import os"):
        assert word not in text
