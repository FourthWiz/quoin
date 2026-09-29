"""Tests for the closed rejection-class set, template messages and the
secret-shape pattern."""
from __future__ import annotations

import contextlib
import random

import pytest

from quoin.opencode_adapter import errors
from quoin.opencode_adapter.errors import (
    LOAD_CLASSES,
    MERGE_CLASSES,
    MESSAGE_CLASS,
    MESSAGES,
    REJECTION_CLASSES,
    SECRET_SHAPE_RE,
    ConfigError,
    ConfigErrors,
    make_error,
    render_json_path,
)

B64URL = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"


def secret_shapes():
    """Secret-shaped strings, assembled at run time so no literal is committed."""
    return {
        "sk-proj": "sk-" + "proj-" + "Ab1cdEf2GhIj3KlMn4OpQr5StUv6",
        "sk-proj-underscore": "sk-" + "proj-" + "Ab1_" + "x" * 30 + "-Cd2",
        "sk-ant": "sk-" + "ant-api03-" + "Ab_9-" * 6,
        "sk-or": "sk-" + "or-v1-" + "0123456789abcdef" * 4,
        "sk-alnum": "sk-" + "a1" * 15,
        "ghp": "ghp_" + "a1B2" * 9,
        "github_pat": "github_pat_" + "Ab1_" * 8,
        "akia": "AKIA" + "ABCDEFGH12345678",
        "xox": "xoxb-" + "1234567890-abcdef",
        "bearer": "Bearer " + "abc123" * 4,
        "jwt": "eyJ" + "abcdefgh" + "." + "abcdefgh12" + "." + "abcdefgh12",
    }


SHAPES = secret_shapes()


def test_class_sets_disjoint_and_closed():
    assert not (LOAD_CLASSES & MERGE_CLASSES)
    assert REJECTION_CLASSES == LOAD_CLASSES | MERGE_CLASSES


def test_every_class_has_a_message_with_a_fix():
    covered = set()
    for message_id, (message, fix) in MESSAGES.items():
        cls = MESSAGE_CLASS[message_id]
        assert cls in REJECTION_CLASSES
        assert message and fix
        covered.add(cls)
    assert covered == REJECTION_CLASSES
    assert set(MESSAGE_CLASS) == set(MESSAGES)


def test_messages_render_without_params():
    for message_id, cls in MESSAGE_CLASS.items():
        err = make_error(cls, "f", "$", message_id)
        assert err.message and err.fix


def test_make_error_rejects_unknown_param_and_class():
    with pytest.raises(ValueError):
        make_error("invalid-type", "f", "$", "wrong-type", secret="x")
    with pytest.raises(ValueError):
        make_error("no-such-class", "f", "$", "wrong-type")
    with pytest.raises(ValueError):
        make_error("invalid-type", "f", "$", "unknown-key")


def test_str_format():
    err = make_error("invalid-json", "profiles/a.json", "$", "malformed-json", line=3, column=4)
    text = str(err)
    assert text.startswith("profiles/a.json: $: ")
    assert "line 3, column 4" in text
    assert "[invalid-json]" in text
    assert "\n  fix: " in text


def test_config_errors_dedupes_and_survives_exception_machinery():
    a = make_error("invalid-type", "f", "$.a", "wrong-type")
    exc = ConfigErrors([a, a])
    assert exc.errors == (a,)
    with contextlib.suppress(ConfigErrors):
        raise exc
    with pytest.raises(ConfigErrors) as info:
        raise ConfigErrors([a])
    assert info.value.errors == (a,)
    try:
        raise ConfigErrors([a])
    except ConfigErrors as caught:
        caught.add_note("extra context")
        assert caught.__notes__ == ["extra context"] or hasattr(caught, "__notes__")
    assert isinstance(a, ConfigError) and not isinstance(a, Exception)


@pytest.mark.parametrize(
    "segments,expected",
    [
        ((), "$"),
        (("providers", "corp-gw", "base_url"), "$.providers.corp-gw.base_url"),
        (("policy", "allowed_providers", 2), "$.policy.allowed_providers[2]"),
        (("has space",), '$["*"]'),
        (("a" * 65,), '$["*"]'),
        (("",), '$["*"]'),
    ],
)
def test_render_json_path(segments, expected):
    assert render_json_path(segments) == expected


def test_render_json_path_hides_secret_shaped_key():
    assert render_json_path(("providers", SHAPES["sk-or"])) == '$.providers["*"]'


@pytest.mark.parametrize("name", sorted(SHAPES))
def test_secret_shape_positive(name):
    assert SECRET_SHAPE_RE.search(SHAPES[name])
    assert SECRET_SHAPE_RE.search("prefix text " + SHAPES[name] + " suffix")


@pytest.mark.parametrize(
    "prefix", ["sk-" + "proj-", "sk-" + "ant-api03-", "sk-" + "svcacct-"]
)
def test_sk_base64url_property(prefix):
    rng = random.Random(271)
    length = {"sk-proj-": 156, "sk-ant-api03-": 95, "sk-svcacct-": 156}[prefix]
    misses = 0
    for _ in range(2000):
        tail = "".join(rng.choice(B64URL) for _ in range(length))
        if not SECRET_SHAPE_RE.search(prefix + tail):
            misses += 1
    assert misses == 0


NEGATIVES = [
    "sk-model",
    "sk-coder-small-instruct-model",
    "sk-work-coder-v2-longname",
    "task-planner-model-small",
    "desk-research-assistant",
    "risk-assessment-model-large",
    "disk-cache-sk-abcdefghijklmnopqrstu1",
    "example-vendor/example-model",
    "ask-gateway",
    "akia",
    "Bearer x",
    "uses Bearer auth for the gateway",
    "Bearer authentication-for-the-gateway",
    "/etc/ssl/certs/desk-ca-bundle.pem",
]


@pytest.mark.parametrize("text", NEGATIVES)
def test_secret_shape_negative(text):
    assert not SECRET_SHAPE_RE.search(text)


@pytest.mark.parametrize("text", NEGATIVES)
def test_secret_shape_negative_max_length_ids(text):
    words = ["risk", "task", "desk", "disk", "sk", "assessment", "planner"]
    ident = ""
    while len(ident) < 63:
        ident += words[len(ident) % len(words)] + "-"
    ident = ident[:63].strip("-")
    assert not SECRET_SHAPE_RE.search(ident)
    assert not SECRET_SHAPE_RE.search("sk-" + ident.replace("-", "-", 1)[:60].lower())


def test_dedupe_keeps_highest_priority_per_path():
    placeholder = make_error("unresolved-placeholder", "f", "$.a", "unresolved-placeholder")
    schema = make_error("invalid-type", "f", "$.a", "wrong-type")
    other = make_error("invalid-type", "f", "$.b", "wrong-type")
    kept = errors.dedupe_by_path([schema, other, placeholder])
    assert [e.rejection_class for e in kept] == ["unresolved-placeholder", "invalid-type"]


def test_config_errors_dedupe_is_linear_and_ordered(monkeypatch):
    calls = {"n": 0}
    real_eq = ConfigError.__eq__

    def counting_eq(self, other):
        calls["n"] += 1
        return real_eq(self, other)

    monkeypatch.setattr(ConfigError, "__eq__", counting_eq)
    errs = [make_error("invalid-type", "f", "$.p%d" % (i % 20000), "wrong-type") for i in range(40000)]
    exc = ConfigErrors(errs)
    # A quadratic scan needs on the order of 10^8 comparisons.
    assert calls["n"] <= 2 * 40000
    assert len(exc.errors) == 20000
    assert [e.json_path for e in exc.errors[:3]] == ["$.p0", "$.p1", "$.p2"]
