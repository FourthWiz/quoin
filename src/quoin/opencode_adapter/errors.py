"""Closed rejection-class set and template-only error messages for runtime
configuration files.

Every defect a config loader reports is a `ConfigError`: a rejection class,
a display label for the file, a rendered JSON path and a message id. The
human text lives in `MESSAGES` and is rendered from templates.

Invariant: message parameters come only from closed sets or schema-authored
text (enum lists, allowed-key lists, limit names, line and column numbers).
User data is never interpolated, so an error can never carry a secret, a
URL or a host the user typed. JSON path segments taken from user keys are
sanitised the same way (see `render_json_path`).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional, Sequence, Tuple

# Classes raised while loading one layer or while cross-checking the loaded
# layers against each other.
LOAD_CLASSES = frozenset(
    {
        "invalid-json",
        "duplicate-key",
        "unknown-key",
        "invalid-type",
        "invalid-url",
        "dangling-reference",
        "unresolved-placeholder",
        "insecure-http",
        "url-credentials",
        "inline-credential",
        "unsupported-schema-version",
        "invalid-limit",
        "unknown-runtime",
        "invalid-profile-name",
        "unknown-role",
        "invalid-effort",
        "env-name-collision",
        "project-declares-endpoint",
        "endpoint-identity-change",
        "cross-profile-fallback-enabled",
        "profile-not-found",
    }
)

# Classes raised when layers are merged; declared here so the set is closed
# from the start.
MERGE_CLASSES = frozenset(
    {
        "allowlist-broadening",
        "personal-profile-for-work",
        "missing-classification",
        "limit-above-ceiling",
        "personal-provider-kind-for-work",
    }
)

REJECTION_CLASSES = LOAD_CLASSES | MERGE_CLASSES

# When several checks flag the same JSON path only the highest-priority class
# is kept. Earlier means more important.
CLASS_PRIORITY: Tuple[str, ...] = (
    "invalid-json",
    "duplicate-key",
    "unresolved-placeholder",
    "inline-credential",
    "url-credentials",
    "insecure-http",
    "invalid-url",
    "endpoint-identity-change",
    "project-declares-endpoint",
    "unsupported-schema-version",
    "unknown-runtime",
    "invalid-profile-name",
    "profile-not-found",
    "unknown-role",
    "invalid-effort",
    "invalid-limit",
    "cross-profile-fallback-enabled",
    "unknown-key",
    "invalid-type",
    "dangling-reference",
    "env-name-collision",
    "personal-profile-for-work",
    "personal-provider-kind-for-work",
    "allowlist-broadening",
    "limit-above-ceiling",
    "missing-classification",
)

OVERRIDE_LABEL = "command-line override"

_PARAM_NAMES = frozenset({"expected", "allowed", "layer", "line", "column", "limit"})

# message id -> (message, fix)
MESSAGES: Dict[str, Tuple[str, str]] = {
    "malformed-json": (
        "file is not valid JSON (line %(line)s, column %(column)s)",
        "fix the syntax at that position; JSON allows no comments or trailing commas",
    ),
    "file-too-large": (
        "file exceeds the size limit of %(limit)s bytes",
        "reduce the file size; configuration files are small",
    ),
    "not-utf8": (
        "file is not valid UTF-8",
        "save the file as UTF-8 without a byte-order mark",
    ),
    "has-bom": (
        "file starts with a byte-order mark",
        "save the file as UTF-8 without a byte-order mark",
    ),
    "non-finite-number": (
        "file contains a non-finite number literal",
        "use a finite number; NaN and Infinity are not valid JSON",
    ),
    "nesting-too-deep": (
        "file nests values deeper than %(limit)s levels",
        "flatten the structure; configuration files are shallow",
    ),
    "number-too-large": (
        "file contains a number literal that is too large",
        "use a smaller number; limits are ordinary integers",
    ),
    "unreadable-file": (
        "file could not be read",
        "check that the file exists and is readable by the current user",
    ),
    "managed-policy-unreadable": (
        "the configured managed policy file could not be read",
        "make the managed policy file readable, or unset the variable that points to it",
    ),
    "duplicate-key": (
        "the key appears more than once in the same object",
        "keep a single entry for the key",
    ),
    "unknown-key": (
        "key is not allowed here",
        "remove the key; check the spelling against the schema for this layer",
    ),
    "not-an-object": (
        "the top level of the file must be a JSON object",
        "wrap the content in a JSON object",
    ),
    "wrong-type": (
        "value has the wrong type or is not one of the allowed values",
        "correct the value to match the schema for this field",
    ),
    "missing-required": (
        "required key %(expected)s is missing",
        "add the required key",
    ),
    "openrouter-requires-chat": (
        "provider kind openrouter supports only the chat-completions endpoint family",
        "set endpoint_family to chat-completions",
    ),
    "invalid-url": (
        "base_url is not a valid http or https URL",
        "use a URL such as https://host/path with a host and an optional numeric port",
    ),
    "url-credentials": (
        "base_url must not carry userinfo, a query string or a fragment",
        "remove them and store credentials behind credential_ref",
    ),
    "insecure-http": (
        "plain http is accepted only for loopback hosts",
        "use https, or a loopback host for local testing",
    ),
    "dangling-reference": (
        "the reference does not name a declared entry",
        "declare the entry, or point the reference at one that exists",
    ),
    "unresolved-placeholder": (
        "value still contains an unresolved placeholder",
        "replace the placeholder with a real value; templating syntax is not expanded",
    ),
    "secret-shaped-value": (
        "a value or key looks like a secret",
        "never store secrets in configuration; use a credential_ref instead",
    ),
    "credential-not-a-reference": (
        "credential_ref must be env:NAME or keychain:SERVICE/ACCOUNT",
        "reference the secret by name, never by value",
    ),
    "unsupported-schema-version": (
        "schema_version is not supported",
        "set schema_version to %(expected)s",
    ),
    "invalid-limit": (
        "limit must be a positive whole number",
        "use an integer of at least 1",
    ),
    "unknown-runtime": (
        "runtime is not supported",
        "set runtime to %(allowed)s",
    ),
    "invalid-profile-name": (
        "profile name is not a valid identifier",
        "use lowercase letters, digits, hyphens and underscores, up to 63 characters",
    ),
    "profile-name-mismatch": (
        "the profile field does not match the file name",
        "make the profile field equal to the file name without its extension",
    ),
    "unknown-role": (
        "role name is not one of the known roles",
        "use one of: %(allowed)s",
    ),
    "invalid-effort": (
        "effort is not one of the allowed levels",
        "use one of: %(allowed)s",
    ),
    "env-name-collision": (
        "two providers would use the same credential variable name",
        "rename one provider id so the derived variable names differ",
    ),
    "project-declares-endpoint": (
        "project configuration must not declare providers or models",
        "move providers, models and default models into the personal profile",
    ),
    "endpoint-identity-change": (
        "project configuration redefines an endpoint of the selected profile",
        "remove the provider from the project file; endpoints belong to the profile",
    ),
    "cross-profile-fallback-enabled": (
        "cross-profile fallback must stay disabled",
        "set cross_profile_fallback to false or remove it",
    ),
    "profile-not-found": (
        "the selected profile does not exist",
        "create the profile file, or select an existing profile",
    ),
    "no-profile-selected": (
        "no profile was selected and the project does not name one",
        "pass a profile name or set the profile field in the project file",
    ),
    "allowlist-broadening": (
        "a layer widens an allow list set by a stricter layer",
        "remove the extra entries",
    ),
    "personal-profile-for-work": (
        "a personal profile cannot be used for work",
        "select a work profile",
    ),
    "allowlist-broadening-host": (
        "a layer widens a host allow list set by a stricter layer",
        "remove the extra hosts",
    ),
    "missing-classification": (
        "the project does not declare a classification",
        "set classification to work or personal",
    ),
    "unknown-classification": (
        "the project classification is not work or personal",
        "set classification to work or personal",
    ),
    "no-project-file": (
        "no project runtime file was found, so the project has no classification",
        "create .quoin/runtime.json with a classification",
    ),
    "limit-above-profile": (
        "the project raises a limit above the profile value; project limits may only narrow",
        "lower the project limit or raise it in the profile",
    ),
    "unknown-override-role": (
        "the override names a role that is not one of the known roles",
        "use one of: %(allowed)s",
    ),
    "override-dangling-model": (
        "the override names a model the profile does not declare",
        "name a model declared in the selected profile",
    ),
    "limit-above-ceiling": (
        "a limit exceeds the ceiling set by a stricter layer",
        "lower the limit to at most the ceiling",
    ),
    "personal-provider-kind-for-work": (
        "a personal provider kind cannot be used for work",
        "use a provider kind allowed for work",
    ),
}

MESSAGE_CLASS: Dict[str, str] = {
    "malformed-json": "invalid-json",
    "file-too-large": "invalid-json",
    "not-utf8": "invalid-json",
    "has-bom": "invalid-json",
    "non-finite-number": "invalid-json",
    "nesting-too-deep": "invalid-json",
    "number-too-large": "invalid-json",
    "unreadable-file": "invalid-json",
    "managed-policy-unreadable": "invalid-json",
    "duplicate-key": "duplicate-key",
    "unknown-key": "unknown-key",
    "not-an-object": "invalid-type",
    "wrong-type": "invalid-type",
    "missing-required": "invalid-type",
    "openrouter-requires-chat": "invalid-type",
    "invalid-url": "invalid-url",
    "url-credentials": "url-credentials",
    "insecure-http": "insecure-http",
    "dangling-reference": "dangling-reference",
    "unresolved-placeholder": "unresolved-placeholder",
    "secret-shaped-value": "inline-credential",
    "credential-not-a-reference": "inline-credential",
    "unsupported-schema-version": "unsupported-schema-version",
    "invalid-limit": "invalid-limit",
    "unknown-runtime": "unknown-runtime",
    "invalid-profile-name": "invalid-profile-name",
    "profile-name-mismatch": "invalid-profile-name",
    "unknown-role": "unknown-role",
    "invalid-effort": "invalid-effort",
    "env-name-collision": "env-name-collision",
    "project-declares-endpoint": "project-declares-endpoint",
    "endpoint-identity-change": "endpoint-identity-change",
    "cross-profile-fallback-enabled": "cross-profile-fallback-enabled",
    "profile-not-found": "profile-not-found",
    "no-profile-selected": "profile-not-found",
    "allowlist-broadening": "allowlist-broadening",
    "personal-profile-for-work": "personal-profile-for-work",
    "allowlist-broadening-host": "allowlist-broadening",
    "missing-classification": "missing-classification",
    "unknown-classification": "missing-classification",
    "no-project-file": "missing-classification",
    "limit-above-profile": "limit-above-ceiling",
    "unknown-override-role": "unknown-role",
    "override-dangling-model": "dangling-reference",
    "limit-above-ceiling": "limit-above-ceiling",
    "personal-provider-kind-for-work": "personal-provider-kind-for-work",
}

_LB = r"(?<![A-Za-z0-9_-])"

# Shapes of well-known secrets. Every alternative is left-bounded so a token
# embedded inside an ordinary identifier never matches. The boundary also
# excludes "_" and "-", so a token glued to a prefix such as "my_" or "token-"
# is not flagged: that keeps ordinary hyphenated identifiers from matching.
SECRET_SHAPE_RE = re.compile(
    "|".join(
        (
            # legacy pure-alphanumeric keys and hyphen-segmented keys
            _LB + r"sk-(?:[A-Za-z0-9_]+-)*(?=[A-Za-z0-9]*[0-9])[A-Za-z0-9]{20,}",
            # base64url tails that may contain _ and -
            _LB
            + r"sk-(?=[A-Za-z0-9_-]*[0-9])(?=[A-Za-z0-9_-]*[A-Z])[A-Za-z0-9_-]{20,}",
            _LB + r"ghp_[A-Za-z0-9]{36}",
            _LB + r"github_pat_[A-Za-z0-9_]{22,}",
            r"(?<![A-Za-z0-9])AKIA[0-9A-Z]{16}(?![0-9A-Z])",
            _LB + r"xox[abpr]-(?=[A-Za-z0-9-]*[0-9])[A-Za-z0-9-]{10,}",
            r"(?<![A-Za-z0-9])(?i:bearer)\s+(?=[A-Za-z0-9._~+/=-]*[0-9])"
            r"[A-Za-z0-9._~+/=-]{20,}",
            _LB + r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}",
            # Lowercase-prefixed shapes require a digit and an uppercase
            # letter in the tail so ordinary lowercase identifiers can never
            # match. The AIza prefix already contains uppercase letters that
            # no identifier can, so it needs no lookahead.
            _LB + r"glpat-(?=[A-Za-z0-9_-]*[0-9])(?=[A-Za-z0-9_-]*[A-Z])[A-Za-z0-9_-]{20,}",
            _LB + r"hf_(?=[A-Za-z0-9]*[0-9])(?=[A-Za-z0-9]*[A-Z])[A-Za-z0-9]{30,}",
            _LB + r"gh[osur]_(?=[A-Za-z0-9]*[0-9])(?=[A-Za-z0-9]*[A-Z])[A-Za-z0-9]{36}",
            _LB
            + r"[sr]k_(?:live|test)_(?=[A-Za-z0-9]*[0-9])(?=[A-Za-z0-9]*[A-Z])[A-Za-z0-9]{24,}",
            r"(?<![A-Za-z0-9])AIza[0-9A-Za-z_-]{35}",
        )
    )
)

_SAFE_KEY_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")

# A provider model id may not contain whitespace or control characters: the
# native reference splits at the first slash and reads the rest verbatim, so
# such a character could never round-trip. Shared by the compiler and the
# import preview so the two cannot disagree.
BAD_MODEL_ID_RE = re.compile(r"[\s\x00-\x1f\x7f]")


def render_json_path(segments: Iterable[Any]) -> str:
    """Render path segments as `$`, `.name` and `[N]`.

    A key segment is shown verbatim only when it is short, made of safe
    characters and does not look like a secret; anything else becomes
    `["*"]` so a user-typed key can never leak through an error.
    """
    out = ["$"]
    for seg in segments:
        if isinstance(seg, bool):
            out.append('["*"]')
        elif isinstance(seg, int):
            out.append("[%d]" % seg)
        elif (
            isinstance(seg, str)
            and _SAFE_KEY_RE.fullmatch(seg)
            and not SECRET_SHAPE_RE.search(seg)
        ):
            out.append("." + seg)
        else:
            out.append('["*"]')
    return "".join(out)


def _freeze(value: Any) -> Any:
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(value)
    return value


class _Params(dict):
    def __missing__(self, key: str) -> str:
        return "the documented value"


def _fmt(value: Any) -> str:
    if isinstance(value, tuple):
        return ", ".join(str(v) for v in value)
    return str(value)


@dataclass(frozen=True)
class ConfigError:
    """One rejected defect. A plain frozen value object, deliberately not an
    Exception subclass: a frozen exception breaks traceback assignment."""

    rejection_class: str
    file: str
    json_path: str
    message_id: str
    params: Tuple[Tuple[str, Any], ...] = ()

    @property
    def message(self) -> str:
        template = MESSAGES[self.message_id][0]
        return template % _Params((k, _fmt(v)) for k, v in self.params)

    @property
    def fix(self) -> str:
        template = MESSAGES[self.message_id][1]
        return template % _Params((k, _fmt(v)) for k, v in self.params)

    def __str__(self) -> str:
        return "%s: %s: %s [%s]\n  fix: %s" % (
            self.file,
            self.json_path,
            self.message,
            self.rejection_class,
            self.fix,
        )


def make_error(
    rejection_class: str,
    file: str,
    path: Any,
    message_id: str,
    **params: Any
) -> ConfigError:
    """Build a `ConfigError`. `path` is an already rendered string or a
    sequence of segments. Unknown classes, message ids or parameter names are
    programmer errors (ValueError)."""
    if rejection_class not in REJECTION_CLASSES:
        raise ValueError("unknown rejection class")
    if message_id not in MESSAGES or MESSAGE_CLASS.get(message_id) != rejection_class:
        raise ValueError("message id does not belong to the rejection class")
    unknown = set(params) - _PARAM_NAMES
    if unknown:
        raise ValueError("unknown message parameter")
    if not isinstance(path, str):
        path = render_json_path(path)
    return ConfigError(
        rejection_class,
        file,
        path,
        message_id,
        tuple(sorted((k, _freeze(v)) for k, v in params.items())),
    )


# ------------------------------------------------------------- findings

# Non-error diagnostics produced by merging and role resolution. A finding
# never carries user data: its subject holds only closed-set tokens or
# identifiers that already passed the identifier grammar and the secret sweep.
FINDING_CODES = frozenset(
    {
        "missing-classification",
        "provider-excluded",
        "less-restrictive-ignored",
        "integrations-value-ignored",
        "no-managed-policy",
        "provider-ids-are-labels",
        "isolation-unverified",
        "role-blocked",
        "role-unqualified",
        "effort-omitted",
        "summary-unused",
        "agent-file-missing",
        "native-id-conflict",
        "native-ref-invalid",
        "endpoint-family-unverified",
        "later-layers-can-override",
        "policies-supplementary",
    }
)

FINDING_MESSAGES: Dict[str, str] = {
    "missing-classification": "the project has no valid classification; the configuration cannot be compiled",
    "provider-excluded": "a provider was excluded by the layered policy",
    "less-restrictive-ignored": "a layer asked for a less restrictive setting than the effective one; it was ignored",
    "integrations-value-ignored": "a project integrations value would widen the profile and was ignored",
    "no-managed-policy": "no managed policy is installed on this machine",
    "provider-ids-are-labels": "managed provider allow and deny lists match provider ids, which are user labels; hosts are the primary identity",
    "isolation-unverified": "isolation is set to managed but nothing verifies it yet",
    "role-blocked": "a role cannot run because its model is blocked",
    "role-unqualified": "a role runs on a model that has no valid qualification record",
    "effort-omitted": "the reasoning effort was not applied to the request",
    "summary-unused": "the summary model has no consumer in the pinned runtime",
    "agent-file-missing": "the generated agent file for this role is missing; install the OpenCode scaffold into the project first",
    "native-id-conflict": "two providers of kind openrouter are in use; only one can map to the built-in OpenCode provider id openrouter",
    "native-ref-invalid": "the model id cannot be written as an OpenCode model reference; it must not contain whitespace or control characters",
    "endpoint-family-unverified": "this endpoint family is not verified for compiled configuration on the pinned runtime",
    "later-layers-can-override": "the compiled file is one of several OpenCode config layers; project files, .opencode files, inline config, markdown agent and mode files and organization config can override it; the effective configuration is verified at launch",
    "policies-supplementary": "provider policy statements are not evaluated on the pinned runtime's run path; the provider allowlist and per-provider model whitelists are the enforced controls",
}

# Closed reason tokens used in finding subjects and resolution results.
EXCLUSION_REASONS = frozenset(
    {"not-allowed", "denied", "host-not-allowed", "host-denied", "managed-not-allowed"}
)
QUALIFICATION_REASONS = frozenset(
    {
        "unreadable",
        "bad-schema",
        "bad-shape",
        "unsafe-permissions",
        "future-timestamp",
        "not-qualified",
        "could-not-run",
        "model-mismatch",
        "endpoint-mismatch",
        "runtime-mismatch",
        "too-old",
    }
)
ROLE_REASONS = frozenset(
    {
        "override",
        "role-mapping",
        "default-model",
        "auxiliary-model",
        "classification-incompatible",
        "qualification-missing",
        "qualification-failed",
        "qualification-stale",
        "qualification-mismatched",
        "qualification-malformed",
        "effort-max",
        "effort-no-capability",
        "effort-no-mapping",
        "effort-unqualified",
    }
)
REASON_CODES = EXCLUSION_REASONS | QUALIFICATION_REASONS | ROLE_REASONS

# Closed field and layer tokens (dot-free so they pass the key grammar).
FIELD_TOKENS = frozenset(
    {
        "sharing",
        "external_writes",
        "isolation",
        "integrations-enabled",
        "integrations-mode",
        "integrations-backend",
    }
)
LAYER_TOKENS = ("default", "profile", "project", "override", "managed")


@dataclass(frozen=True)
class Finding:
    code: str
    blocking: bool
    subject: Tuple[str, ...] = ()
    error: Optional[ConfigError] = None


def make_finding(
    code: str, blocking: bool, *subject: str, error: Optional[ConfigError] = None
) -> Finding:
    """Build a `Finding`. Unknown codes and any subject element that is not a
    plain identifier-like token, or that looks like a secret, are programmer
    errors (ValueError)."""
    if code not in FINDING_CODES:
        raise ValueError("unknown finding code")
    for element in subject:
        if (
            not isinstance(element, str)
            or not _SAFE_KEY_RE.fullmatch(element)
            or SECRET_SHAPE_RE.search(element)
        ):
            raise ValueError("finding subject must be a plain identifier token")
    return Finding(code, bool(blocking), tuple(subject), error)


class ConfigErrors(Exception):
    """The only exception config code raises for user-data defects."""

    def __init__(self, errors: Sequence[ConfigError]):
        # Errors are frozen and hashable, so a dict keeps first-seen order in
        # linear time.
        self.errors: Tuple[ConfigError, ...] = tuple(dict.fromkeys(errors))
        super().__init__(self.errors)

    def __str__(self) -> str:
        return "\n".join(str(e) for e in self.errors)

    def __repr__(self) -> str:
        return "ConfigErrors(%s)" % ", ".join(
            "%s@%s" % (e.rejection_class, e.json_path) for e in self.errors
        )


def dedupe_by_path(errors: Iterable[ConfigError]) -> Tuple[ConfigError, ...]:
    """Keep one error per (file, JSON path): the highest-priority class.
    Order is stable by (path, class priority)."""
    rank = {c: i for i, c in enumerate(CLASS_PRIORITY)}
    best: Dict[Tuple[str, str], ConfigError] = {}
    for err in errors:
        key = (err.file, err.json_path)
        cur = best.get(key)
        if cur is None or rank.get(err.rejection_class, 999) < rank.get(
            cur.rejection_class, 999
        ):
            best[key] = err
    return tuple(
        sorted(
            best.values(),
            key=lambda e: (e.file, e.json_path, rank.get(e.rejection_class, 999)),
        )
    )
