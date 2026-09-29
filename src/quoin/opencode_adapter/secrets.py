"""Credential references and use-time secret resolution.

A reference is either `env:NAME` (an environment variable) or
`keychain:SERVICE/ACCOUNT`. This module parses that grammar and resolves a
reference to a `SecretValue` on request, through a backend registry: an
environment backend and a macOS keychain backend. Resolution happens only
when a caller explicitly asks for it (the gateway probe path); loading,
merging, qualification and role resolution never resolve anything.

A resolved value is wrapped in `SecretValue`, which cannot be printed,
formatted, pickled, copied or serialised; `reveal()` is the only accessor.
Resolution errors carry a closed code and a reference, never a value and
never subprocess output. Where the standard library's secrets module is
needed, import it as `_stdlib_secrets` to keep the two apart.
"""
from __future__ import annotations

import abc
import re
from dataclasses import dataclass
from typing import Any, Callable, ClassVar, Dict, Mapping, Optional, Sequence, Tuple, Union

_ENV_NAME = r"[A-Z_][A-Z0-9_]{0,127}"
_SERVICE = r"[A-Za-z0-9._-]{1,128}"
_ACCOUNT = r"[A-Za-z0-9._@/+-]{1,256}"

# The anchored string form; the runtime schema's credential_ref pattern
# must equal it.
CREDENTIAL_REF_PATTERN = r"^(?:env:%s|keychain:%s/%s)$" % (_ENV_NAME, _SERVICE, _ACCOUNT)

_ENV_RE = re.compile(_ENV_NAME)
_SERVICE_RE = re.compile(_SERVICE)
_ACCOUNT_RE = re.compile(_ACCOUNT)

_INVALID = "credential reference must be env:NAME or keychain:SERVICE/ACCOUNT"


@dataclass(frozen=True)
class CredentialRef:
    scheme: str
    name: Optional[str] = None
    service: Optional[str] = None
    account: Optional[str] = None

    def masked(self) -> str:
        if self.scheme == "env":
            return "env:%s" % self.name
        return "keychain:%s/***" % self.service

    def __repr__(self) -> str:
        return "CredentialRef(%s)" % self.masked()

    __str__ = __repr__


def parse(text: str) -> CredentialRef:
    """Parse a reference. The error message never includes `text`."""
    if not isinstance(text, str):
        raise ValueError(_INVALID)
    if text.startswith("env:"):
        name = text[len("env:"):]
        if _ENV_RE.fullmatch(name):
            return CredentialRef("env", name=name)
    elif text.startswith("keychain:"):
        service, sep, account = text[len("keychain:"):].partition("/")
        if sep and _SERVICE_RE.fullmatch(service) and _ACCOUNT_RE.fullmatch(account):
            return CredentialRef("keychain", service=service, account=account)
    raise ValueError(_INVALID)


# ----------------------------------------------------------- secret value

_REDACTED = "<redacted>"


class SecretValue:
    """A resolved secret. Printing, formatting, pickling, copying and JSON
    serialisation all yield a placeholder or fail; `reveal()` is the only way
    to read the value. Equality and hashing are identity-based so comparing
    two wrappers never touches the value."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return _REDACTED

    def __str__(self) -> str:
        return _REDACTED

    def __format__(self, spec: str) -> str:
        return _REDACTED

    def __reduce__(self) -> Any:
        raise TypeError("a secret value cannot be pickled or copied")

    def __reduce_ex__(self, protocol: Any) -> Any:
        raise TypeError("a secret value cannot be pickled or copied")


# ---------------------------------------------------------------- errors

_RESOLUTION_MESSAGES: Dict[str, str] = {
    "env-missing": "the environment variable is not set",
    "env-empty": "the environment variable is empty",
    "keychain-not-found": "no matching keychain item was found",
    "keychain-failed": "the keychain lookup failed",
    "keychain-timeout": "the keychain lookup timed out",
    "backend-unavailable": (
        "keychain backend unavailable on this platform; use env:NAME or run on macOS"
    ),
    "unsafe-reference": "the keychain service and account must not start with a hyphen",
}

RESOLUTION_CODES = frozenset(_RESOLUTION_MESSAGES)


class SecretResolutionError(Exception):
    """A reference could not be resolved. Carries a closed `code` and the
    reference; the text never includes a value or subprocess output."""

    def __init__(self, code: str, ref: CredentialRef) -> None:
        super().__init__(code)
        self.code = code
        self.ref = ref

    def render(self, redact: bool) -> str:
        if redact:
            shown = self.ref.masked()
        elif self.ref.scheme == "env":
            shown = "env:%s" % self.ref.name
        else:
            shown = "keychain:%s/%s" % (self.ref.service, self.ref.account)
        return "%s: %s" % (shown, _RESOLUTION_MESSAGES.get(self.code, "the reference could not be resolved"))

    def __str__(self) -> str:
        return self.render(redact=True)

    def __repr__(self) -> str:
        return "SecretResolutionError(%s, %s)" % (self.code, self.ref.masked())


# --------------------------------------------------------------- backends


class SecretResolver(abc.ABC):
    scheme: ClassVar[str]

    @abc.abstractmethod
    def resolve(self, ref: CredentialRef) -> SecretValue:
        """Return the secret named by `ref` or raise `SecretResolutionError`."""


class EnvBackend(SecretResolver):
    scheme = "env"

    def __init__(self, environ: Mapping[str, str]) -> None:
        self._environ = environ

    def resolve(self, ref: CredentialRef) -> SecretValue:
        if ref.scheme != "env" or ref.name is None:
            raise SecretResolutionError("unsafe-reference", ref)
        value = self._environ.get(ref.name)
        if value is None:
            raise SecretResolutionError("env-missing", ref)
        if value == "":
            raise SecretResolutionError("env-empty", ref)
        return SecretValue(value)


# Neutral runner contract: takes argv and a timeout, returns
# (returncode, stdout), or raises the private exceptions below.
Runner = Callable[[Sequence[str], float], Tuple[int, bytes]]

_SECURITY_BINARY = "/usr/bin/security"
_KEYCHAIN_NOT_FOUND = 44


class _RunnerTimeout(Exception):
    """The lookup did not finish in time. Carries nothing."""


class _RunnerUnavailable(Exception):
    """The lookup tool could not be started. Carries nothing."""


def _default_runner(argv: Sequence[str], timeout: float) -> Tuple[int, bytes]:
    """Run `argv` without a shell and return (returncode, stdout).

    This is the only place the subprocess machinery is named. Errors are
    recorded in the handler and raised after it, so the raised exception has
    no cause or context and cannot reach the partial output a timeout may
    have captured. stderr is never captured.
    """
    import subprocess

    failure = None
    completed = None
    try:
        completed = subprocess.run(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        failure = "timeout"
    except OSError:
        failure = "unavailable"
    if failure == "timeout":
        raise _RunnerTimeout()
    if failure == "unavailable":
        raise _RunnerUnavailable()
    if completed is None:
        raise _RunnerUnavailable()
    return completed.returncode, completed.stdout


def _keychain_outcome(result: Any) -> Union[SecretValue, str]:
    """Turn a runner result into a `SecretValue` or a closed outcome code.

    Never raises, and holds the output only in its own frame, which is gone
    before the caller raises anything. A result that is not a
    (returncode, bytes) pair is a failed lookup. Exceptions raised by the
    runner itself, other than the two private ones, are not handled here or
    by the caller: they propagate unchanged.
    """
    if not (
        isinstance(result, tuple)
        and len(result) == 2
        and isinstance(result[0], int)
        and not isinstance(result[0], bool)
        and isinstance(result[1], (bytes, bytearray))
    ):
        return "keychain-failed"
    if result[0] == _KEYCHAIN_NOT_FOUND:
        return "keychain-not-found"
    if result[0] != 0 or not result[1]:
        return "keychain-failed"
    try:
        text = bytes(result[1]).decode("utf-8")
    except UnicodeDecodeError:
        return "keychain-failed"
    if text.endswith("\n"):
        text = text[:-1]
    if not text:
        return "keychain-failed"
    return SecretValue(text)


class MacKeychainBackend(SecretResolver):
    scheme = "keychain"

    def __init__(
        self, *, runner: Optional[Runner] = None, platform: str, timeout: float = 10.0
    ) -> None:
        self._runner = runner
        self._platform = platform
        self._timeout = timeout

    def resolve(self, ref: CredentialRef) -> SecretValue:
        # Every raise below happens outside an except block and no local here
        # ever holds runner output, so an error carries no cause, no context
        # and nothing secret in its traceback frames.
        if self._platform != "darwin":
            raise SecretResolutionError("backend-unavailable", ref)
        if (
            ref.scheme != "keychain"
            or ref.service is None
            or ref.account is None
            or ref.service.startswith("-")
            or ref.account.startswith("-")
        ):
            raise SecretResolutionError("unsafe-reference", ref)
        argv = [_SECURITY_BINARY, "find-generic-password", "-s", ref.service, "-a", ref.account, "-w"]
        runner = self._runner if self._runner is not None else _default_runner
        try:
            outcome = _keychain_outcome(runner(argv, self._timeout))
        except _RunnerTimeout:
            outcome = "keychain-timeout"
        except _RunnerUnavailable:
            outcome = "backend-unavailable"
        if isinstance(outcome, SecretValue):
            return outcome
        raise SecretResolutionError(outcome, ref)


BACKENDS: Mapping[str, type] = {"env": EnvBackend, "keychain": MacKeychainBackend}


class CredentialResolver:
    def __init__(self, backends: Mapping[str, SecretResolver]) -> None:
        self._backends = dict(backends)

    def resolve(self, ref: Union[CredentialRef, str]) -> SecretValue:
        if isinstance(ref, str):
            ref = parse(ref)
        backend = self._backends.get(ref.scheme)
        if backend is None:
            raise SecretResolutionError("backend-unavailable", ref)
        return backend.resolve(ref)


def default_resolver(
    environ: Mapping[str, str], *, platform: str, runner: Optional[Runner] = None
) -> CredentialResolver:
    return CredentialResolver(
        {
            "env": EnvBackend(environ),
            "keychain": MacKeychainBackend(runner=runner, platform=platform),
        }
    )
