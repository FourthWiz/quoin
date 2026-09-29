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


# ------------------------------------------------------ priority and findings


def test_priority_table_is_closed_and_unique():
    assert set(errors.CLASS_PRIORITY) == REJECTION_CLASSES
    assert len(errors.CLASS_PRIORITY) == len(set(errors.CLASS_PRIORITY))


def test_merge_classes_rank_after_load_classes():
    order = list(errors.CLASS_PRIORITY)
    assert order[-5:] == [
        "personal-profile-for-work",
        "personal-provider-kind-for-work",
        "allowlist-broadening",
        "limit-above-ceiling",
        "missing-classification",
    ]


@pytest.mark.parametrize(
    "message_id,cls",
    [
        ("number-too-large", "invalid-json"),
        ("unknown-classification", "missing-classification"),
        ("no-project-file", "missing-classification"),
        ("allowlist-broadening-host", "allowlist-broadening"),
        ("limit-above-profile", "limit-above-ceiling"),
        ("unknown-override-role", "unknown-role"),
        ("override-dangling-model", "dangling-reference"),
    ],
)
def test_new_message_ids_map_to_classes_with_fixes(message_id, cls):
    assert MESSAGE_CLASS[message_id] == cls
    err = make_error(cls, errors.OVERRIDE_LABEL, "$", message_id, allowed=("a", "b"))
    assert err.message and err.fix
    assert "%(" not in err.message and "%(" not in err.fix


def test_override_label_is_a_fixed_string():
    assert errors.OVERRIDE_LABEL == "command-line override"


def test_finding_codes_have_static_messages():
    assert set(errors.FINDING_MESSAGES) == set(errors.FINDING_CODES)
    for text in errors.FINDING_MESSAGES.values():
        assert text and "%" not in text


def test_every_finding_code_can_be_built_with_its_subject_shape():
    shapes = {
        "missing-classification": (),
        "provider-excluded": ("corp-gw", "managed-not-allowed"),
        "less-restrictive-ignored": ("external_writes", "project"),
        "integrations-value-ignored": ("integrations-backend", "project"),
        "no-managed-policy": (),
        "provider-ids-are-labels": (),
        "isolation-unverified": (),
        "role-blocked": ("planner", "host-not-allowed"),
        "role-unqualified": ("planner", "qualification-missing"),
        "effort-omitted": ("planner", "effort-no-capability"),
        "summary-unused": (),
    }
    assert set(shapes) == set(errors.FINDING_CODES)
    for code, subject in shapes.items():
        finding = errors.make_finding(code, False, *subject)
        assert finding.code == code and finding.subject == subject and finding.error is None
    for token in errors.FIELD_TOKENS | set(errors.LAYER_TOKENS) | errors.REASON_CODES:
        assert errors._SAFE_KEY_RE.fullmatch(token) and not SECRET_SHAPE_RE.search(token)


def test_make_finding_rejects_bad_input():
    with pytest.raises(ValueError):
        errors.make_finding("no-such-code", False)
    with pytest.raises(ValueError):
        errors.make_finding("provider-excluded", False, "gateway.example.invalid", "denied")
    with pytest.raises(ValueError):
        errors.make_finding("provider-excluded", False, "https://x", "denied")
    with pytest.raises(ValueError):
        errors.make_finding("provider-excluded", False, SHAPES["ghp"], "denied")
    with pytest.raises(ValueError):
        errors.make_finding("provider-excluded", False, "a" * 65, "denied")
    with pytest.raises(ValueError):
        errors.make_finding("provider-excluded", False, "ok\n", "denied")


# ------------------------------------------------ extended secret shapes


def _new_shapes():
    return {
        "glpat": "glpat" + "-" + "Ab1" * 8,
        "hf": "hf" + "_" + "Ab1" * 11,
        "ghs": "ghs" + "_" + "Ab1" * 12,
        "gho": "gho" + "_" + "Ab1" * 12,
        "ghu": "ghu" + "_" + "Ab1" * 12,
        "ghr": "ghr" + "_" + "Ab1" * 12,
        "sk-live": "sk" + "_live_" + "Ab1" * 8,
        "sk-test": "sk" + "_test_" + "Ab1" * 8,
        "rk-live": "rk" + "_live_" + "Ab1" * 8,
        "aiza": "AIza" + "Sy" + "a1" * 17 + "b",
    }


NEW_SHAPES = _new_shapes()


@pytest.mark.parametrize("name", sorted(NEW_SHAPES))
def test_new_secret_shapes_match(name):
    assert SECRET_SHAPE_RE.search(NEW_SHAPES[name])
    assert SECRET_SHAPE_RE.search("x = " + NEW_SHAPES[name] + " end")
    assert not SECRET_SHAPE_RE.search("my_" + NEW_SHAPES[name]) or name == "aiza"


# prefix, alphabet of the tail, minimum length, lookahead shapes
_PROPERTY_SHAPES = [
    ("glpat" + "-", B64URL, 20, True),
    ("hf" + "_", B64URL[:-2], 30, True),
    ("ghs" + "_", B64URL[:-2], 36, True),
    ("sk" + "_live_", B64URL[:-2], 24, True),
    ("sk" + "_test_", B64URL[:-2], 24, True),
    ("AIza", B64URL, 35, False),
]


@pytest.mark.parametrize("prefix,alphabet,minimum,lookahead", _PROPERTY_SHAPES)
def test_new_secret_shapes_property(prefix, alphabet, minimum, lookahead):
    rng = random.Random(271)
    digits = "0123456789"
    upper = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    misses = 0
    for length in (minimum, minimum + 17):
        for _ in range(1000):
            tail = [rng.choice(alphabet) for _ in range(length)]
            if lookahead:
                a, b = rng.sample(range(length), 2)
                tail[a] = rng.choice(digits)
                tail[b] = rng.choice(upper)
            if not SECRET_SHAPE_RE.search(prefix + "".join(tail)):
                misses += 1
    assert misses == 0


def test_lookahead_shapes_ignore_digit_free_or_uppercase_free_tokens():
    # Accepted false negatives: the trade for never matching lowercase ids.
    assert not SECRET_SHAPE_RE.search("ghs" + "_" + "a" * 36)
    assert not SECRET_SHAPE_RE.search("ghs" + "_" + "A" * 36)
    assert not SECRET_SHAPE_RE.search("sk" + "_live_" + "a" * 24)
    assert not SECRET_SHAPE_RE.search("sk" + "_live_" + "A" * 24)


NEW_NEGATIVES = [
    "hf-cache-model",
    "glpat-free-name",
    "ghs-runner",
    "sk_live_demo",
    "hf_" + "abcdefghij" * 3 + "1",
    "gh" + "s_" + "a" * 36,
    "-".join(["planner"] * 9),
    "-".join(["glpat", "coder", "model", "large", "instruct", "v2", "longname"]),
    "AIz" + "b" + "x" * 35,
    "xAIza" + "x" * 35,
]


@pytest.mark.parametrize("text", NEW_NEGATIVES)
def test_new_secret_shapes_do_not_hit_realistic_ids(text):
    assert not SECRET_SHAPE_RE.search(text)


def test_sixty_three_char_hyphenated_lowercase_ids_never_match():
    for prefix in ("glpat-", "hf_", "ghs_", "sk_live_", "sk_test_", "rk_live_"):
        ident = (prefix + "abcdefghij-" * 8)[:63]
        assert not SECRET_SHAPE_RE.search(ident), prefix


def test_committed_fixtures_have_no_secret_shape_hits():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent.parent / "adapters" / "opencode" / "fixtures"
    for path in root.rglob("*.json"):
        assert not SECRET_SHAPE_RE.search(path.read_text(encoding="utf-8")), path
