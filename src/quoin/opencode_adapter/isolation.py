"""Managed-isolation attestation: where it lives, who may write it, and what
corroborates it.

A profile that requires managed isolation launches only when the machine
carries a root-owned declaration (the attestation) of how the profile is kept
apart, and the declaration is backed by independent evidence. The declaration
describes a setup; it does not prove the operating system enforces it, so
every verdict here is worded as "attested", never as "verified isolation".

Trust rules, all of which fail closed:

* The file lives at one fixed path beside the trusted managed policy. No
  environment variable, flag or project value can move it.
* The file and every directory above it, up to the chain top, are owned by
  the trusted uid and are not writable by group or others; nothing is
  followed through a symbolic link.
* The invoking user is not the trusted uid, so a root session cannot write
  and then honour its own declaration.
* The declaration is current and describes filesystem, secrets and process
  environment boundaries and a non-empty egress control.
* One corroboration (a container marker, a hypervisor identity, or a
  managed OS user) agrees with the declared environment.
* No other profile is visible to this one on the same account.

Every probe turns an operating-system failure into a verdict token, never an
exception: the verdict is computed on every launch, whatever the profile, so
a crash here would break launches that work today. Guards catch `Exception`
only, so an interrupt still stops the process.

Stdlib only. The attestation text is never returned raw: the issuer is a
digest, hosts are comparison keys.
"""
from __future__ import annotations

import contextlib
import os
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Optional, Tuple

from . import config, jsonio, paths
from .errors import ConfigErrors

ATTESTATION_FORMAT = 1
MAX_ATTESTATION_BYTES = 65536
MAX_READ_BYTES = 65536
MAX_LISTING = 256
MAX_EGRESS_HOSTS = 256
ENVIRONMENTS = ("container", "vm", "managed-os-user")
EGRESS_VALUES = ("firewall", "proxy", "none")
_BOUNDARY_FLAGS = ("filesystem", "secrets", "process_env")
_TOP_KEYS = frozenset(
    {"attestation_format", "environment", "issuer", "boundary", "approved_egress_hosts", "not_after"}
)
_BOUNDARY_KEYS = frozenset(_BOUNDARY_FLAGS) | {"egress"}

# The closed token set. Missing tokens explain why a verdict is unverified;
# evidence tokens explain why it is attested.
MISSING_TOKENS = (
    "attestation-missing",
    "attestation-invalid",
    "attestation-not-root-owned",
    "attestation-chain-writable",
    "invoking-user-is-root",
    "attestation-expired",
    "attestation-incomplete",
    "corroboration-missing",
    "opposite-profile-present",
)
EVIDENCE_TOKENS = (
    "attestation-root-owned",
    "attestation-chain-root-owned",
    "invoking-user-not-root",
    "attestation-current",
    "boundary-declared",
    "corroboration-container",
    "corroboration-vm",
    "corroboration-managed-os-user",
    "opposite-profile-absent",
)

REFUSAL_TEXT: Dict[str, str] = {
    "isolation-required-unverified": "the profile requires managed isolation, which no root-owned declaration attests",
    "repository-not-approved": "the project is not under a repository root the profile approves",
    "approved-repositories-unset": "managed isolation needs the profile to approve the repository roots it may work in",
}

_CONTAINER_CGROUP_MARKERS = ("docker", "kubepods", "containerd", "libpod", "lxc")
_VM_VENDORS = (
    "QEMU", "KVM", "VMware", "VirtualBox", "innotek", "Xen", "Amazon EC2",
    "Google Compute Engine", "Parallels",
)
_MANAGED_PLIST = "ai.quoin.isolation.plist"


# ------------------------------------------------------------------ roots


def _passwd_name(uid: int) -> Optional[str]:
    """The account name of `uid` from the password database, or None.

    A uid with no entry (containers run with an arbitrary `--user`, OpenShift
    and CI do) gives None, as does a name that could not safely become a path
    component. The name never comes from `USER` or `LOGNAME`."""
    try:
        import pwd

        name = pwd.getpwuid(uid).pw_name
    except (KeyError, ImportError, OSError, UnicodeError):
        return None
    if (
        not isinstance(name, str)
        or not name
        or len(name) > 255
        or "/" in name
        or "\x00" in name
        or name in (".", "..")
    ):
        return None
    return name


def _real_sysctl(name: str) -> Optional[str]:
    try:
        done = subprocess.run(
            ["/usr/sbin/sysctl", "-n", name], capture_output=True, stdin=subprocess.DEVNULL,
            timeout=2, check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout[:4096].decode("ascii", "replace").strip()


@dataclass(frozen=True)
class TrustedRoots:
    """Every place and clock the isolation checks read, as injectable data.

    Production uses `TrustedRoots.system()`. Only a constructor argument can
    change these values: no environment variable, flag or project file can,
    so a profile cannot attest itself."""

    attestation_path: Path
    chain_top: Path
    owner_uid: int
    invoking_uid: int
    platform: str
    probe_root: Path
    user_name: Callable[[int], Optional[str]]
    sysctl: Callable[[str], Optional[str]]
    clock: Callable[[], float]
    scandir: Callable[[Path], Iterable[os.DirEntry]]

    @classmethod
    def system(cls) -> "TrustedRoots":
        """The production roots. Performs no lookup, so it cannot raise."""
        return cls(
            attestation_path=paths.attestation_path(), chain_top=Path("/"), owner_uid=0,
            invoking_uid=os.getuid(), platform=sys.platform, probe_root=Path("/"),
            user_name=_passwd_name, sysctl=_real_sysctl, clock=time.time, scandir=os.scandir,
        )


# ---------------------------------------------------------------- verdict


@dataclass(frozen=True)
class IsolationVerdict:
    status: str
    environment: Optional[str] = None
    evidence: Tuple[str, ...] = ()
    missing: Tuple[str, ...] = ()
    boundary: Optional[Dict[str, Any]] = None
    issuer_digest: Optional[str] = None
    egress_hosts: FrozenSet[str] = field(default_factory=frozenset)

    @property
    def verified(self) -> bool:
        return self.status == "verified"


def unverified(tokens: Iterable[str]) -> IsolationVerdict:
    """A verdict for a failure that left nothing to inspect."""
    return IsolationVerdict(status="unverified", missing=tuple(tokens))


def record_section(verdict: IsolationVerdict) -> Dict[str, Any]:
    """The verdict as the record's isolation section: tokens, counts and a
    digest, never attestation text."""
    boundary = None
    if verdict.boundary is not None:
        boundary = {key: verdict.boundary.get(key) for key in (*_BOUNDARY_FLAGS, "egress")}
    return {
        "status": verdict.status,
        "environment": verdict.environment,
        "evidence": list(verdict.evidence),
        "missing": list(verdict.missing),
        "boundary": boundary,
        "issuer": verdict.issuer_digest,
        "approved_egress_hosts": len(verdict.egress_hosts),
    }


# ------------------------------------------------------------------ reader


@dataclass(frozen=True)
class AttestationRead:
    state: str  # absent | invalid | read
    data: Any
    tokens: Tuple[str, ...]
    evidence: Tuple[str, ...] = ()


def read_attestation(roots: TrustedRoots) -> AttestationRead:
    """Read the attestation without following a link and report every
    ownership failure. Never raises."""
    path = roots.attestation_path
    try:
        data, state = jsonio.read_trusted_json(
            path, file_label="isolation attestation", owner_uid=roots.owner_uid,
            max_bytes=MAX_ATTESTATION_BYTES,
        )
    except ConfigErrors:
        return AttestationRead("invalid", None, ("attestation-invalid",))
    if state == "absent":
        tokens: List[str] = ["attestation-missing"]
        if roots.invoking_uid == roots.owner_uid:
            tokens.append("invoking-user-is-root")
        return AttestationRead("absent", None, tuple(tokens))
    tokens = []
    evidence: List[str] = []
    if state in ("file-not-owned", "file-writable"):
        tokens.append("attestation-not-root-owned")
    else:
        evidence.append("attestation-root-owned")
    try:
        chain = jsonio.check_root_owned_chain(path, owner_uid=roots.owner_uid, top=roots.chain_top)
    except ValueError:
        chain = "chain-writable"
    if chain is not None:
        tokens.append("attestation-chain-writable")
    else:
        evidence.append("attestation-chain-root-owned")
    if roots.invoking_uid == roots.owner_uid:
        tokens.append("invoking-user-is-root")
    else:
        evidence.append("invoking-user-not-root")
    return AttestationRead("read", data, tuple(tokens), tuple(evidence))


def _parse_not_after(text: str) -> datetime:
    value = text.strip()
    if value.endswith(("Z", "z")):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def validate_attestation(data: Any, now: float) -> Tuple[Dict[str, Any], Tuple[str, ...]]:
    """Check the declaration against the closed format. Returns the usable
    fields and the failure tokens. Types are checked before any use, so no
    malformed file can raise."""
    invalid = ({}, ("attestation-invalid",))
    if not isinstance(data, dict) or set(data) - _TOP_KEYS:
        return invalid
    fmt = data.get("attestation_format")
    if isinstance(fmt, bool) or fmt != ATTESTATION_FORMAT or not isinstance(fmt, int):
        return invalid
    environment = data.get("environment")
    if environment not in ENVIRONMENTS:
        return invalid
    issuer = data.get("issuer")
    if not isinstance(issuer, str) or not 1 <= len(issuer) <= 256:
        return invalid
    boundary = data.get("boundary")
    if not isinstance(boundary, dict) or set(boundary) != _BOUNDARY_KEYS:
        return invalid
    for flag in _BOUNDARY_FLAGS:
        if not isinstance(boundary[flag], bool):
            return invalid
    if boundary["egress"] not in EGRESS_VALUES:
        return invalid
    hosts: FrozenSet[str] = frozenset()
    if "approved_egress_hosts" in data:
        listed = data["approved_egress_hosts"]
        if not isinstance(listed, list) or len(listed) > MAX_EGRESS_HOSTS:
            return invalid
        for host in listed:
            if not config.host_is_well_formed(host):
                return invalid
        hosts = frozenset(config.host_key(h) for h in listed)
    expired = False
    if "not_after" in data:
        raw = data["not_after"]
        if not isinstance(raw, str):
            return invalid
        try:
            limit = _parse_not_after(raw)
            expired = datetime.fromtimestamp(now, timezone.utc) >= limit
        except (ValueError, OverflowError, OSError):
            return invalid
    tokens: List[str] = []
    if expired:
        tokens.append("attestation-expired")
    complete = all(boundary[f] for f in _BOUNDARY_FLAGS) and boundary["egress"] in ("firewall", "proxy")
    if not complete:
        tokens.append("attestation-incomplete")
    fields = {
        "environment": environment, "issuer": issuer, "egress_hosts": hosts,
        "boundary": {
            "filesystem": boundary["filesystem"], "secrets": boundary["secrets"],
            "process_env": boundary["process_env"], "egress": boundary["egress"],
        },
    }
    return fields, tuple(tokens)


# ------------------------------------------------------------ corroboration


def _read_bounded(path: Any, owner_uid: Optional[int] = None) -> Optional[bytes]:
    """Up to 64 KiB of a regular, non-symlink file, or None on any failure
    (absent `/proc` or `/sys`, `hidepid`, a link, a wrong owner, a NUL in the
    path)."""
    try:
        before = os.lstat(path)
        if not stat.S_ISREG(before.st_mode):
            return None
        if owner_uid is not None and before.st_uid != owner_uid:
            return None
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(path, flags), "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino):
                return None
            return handle.read(MAX_READ_BYTES)
    except (OSError, ValueError):
        return None


def _text(raw: Optional[bytes]) -> str:
    return "" if raw is None else raw.decode("utf-8", "replace")


def _vm_vendor(text: str, product: str) -> bool:
    if any(v in text for v in _VM_VENDORS):
        return True
    return "Microsoft Corporation" in text and "Virtual Machine" in product


def corroborate(environment: str, roots: TrustedRoots) -> Optional[str]:
    """An evidence token when something independent of the attestation agrees
    with the declared environment, else None. Read-only; never raises."""
    try:
        probe = Path(roots.probe_root)
        if environment == "container":
            for relative in (".dockerenv", "run/.containerenv"):
                if _read_bounded(probe / relative, roots.owner_uid) is not None:
                    return "corroboration-container"
            cgroup = _text(_read_bounded(probe / "proc/1/cgroup"))
            if any(marker in cgroup for marker in _CONTAINER_CGROUP_MARKERS):
                return "corroboration-container"
            return None
        if environment == "vm":
            if roots.platform == "darwin":
                return "corroboration-vm" if roots.sysctl("kern.hv_vmm_present") == "1" else None
            if roots.platform.startswith("linux"):
                vendor = _text(_read_bounded(probe / "sys/class/dmi/id/sys_vendor"))
                product = _text(_read_bounded(probe / "sys/class/dmi/id/product_name"))
                if _vm_vendor(vendor, product) or _vm_vendor(product, product):
                    return "corroboration-vm"
            return None
        if environment == "managed-os-user":
            if roots.platform != "darwin":
                return None
            user = roots.user_name(roots.invoking_uid)
            if not user:
                return None
            plist = probe / "Library" / "Managed Preferences" / user / _MANAGED_PLIST
            if _read_bounded(plist, roots.owner_uid) is None:
                return None
            if jsonio.check_root_owned_chain(plist, owner_uid=roots.owner_uid, top=probe) is not None:
                return None
            return "corroboration-managed-os-user"
    except Exception:  # noqa: BLE001 - a probe failure is missing evidence, never an error
        return None
    return None


# ----------------------------------------------------------- opposite probe


def _entries(directory: Any, scandir: Callable[[Any], Iterable[Any]]) -> Tuple[List[Any], bool]:
    """Up to MAX_LISTING entries of `directory` and an overflow flag. A
    directory that is absent holds nothing; any other failure propagates."""
    try:
        iterator = scandir(directory)
    except (FileNotFoundError, NotADirectoryError):
        return [], False
    entries: List[Any] = []
    try:
        for entry in iterator:
            if len(entries) >= MAX_LISTING:
                return entries, True
            entries.append(entry)
    except (FileNotFoundError, NotADirectoryError):
        return entries, False
    finally:
        close = getattr(iterator, "close", None)
        if close is not None:
            with contextlib.suppress(Exception):
                close()
    return entries, False


def opposite_profiles(
    profile: Optional[str], adapter_root: Any, profiles_dir: Any, *,
    scandir: Callable[[Any], Iterable[Any]] = os.scandir,
) -> bool:
    """True when another profile is visible to this account: a directory under
    the adapter root holding a profile stamp, or a profile file. With
    `profile=None` (host-level doctor) more than one profile counts. Any
    failure to look counts as present (fail closed); an absent directory
    holds nothing."""
    names = set()
    try:
        entries, overflow = _entries(adapter_root, scandir)
        if overflow:
            return True
        for entry in entries:
            if not entry.is_dir(follow_symlinks=False):
                continue
            try:
                os.lstat(os.path.join(os.fspath(entry.path), "profile.json"))
            except (FileNotFoundError, NotADirectoryError):
                continue
            names.add(entry.name)
        entries, overflow = _entries(profiles_dir, scandir)
        if overflow:
            return True
        for entry in entries:
            if entry.name.endswith(".json"):
                names.add(entry.name[: -len(".json")])
    except Exception:  # noqa: BLE001 - an unreadable listing is treated as present
        return True
    if profile is None:
        return len(names) > 1
    return any(name != profile for name in names)


# ---------------------------------------------------------------- evaluate


def _guarded(check: Callable[[], Any], fallback: Any, failures: List[str], token: str) -> Any:
    try:
        return check()
    except Exception:  # noqa: BLE001 - never BaseException: Ctrl-C still stops
        failures.append(token)
        return fallback


def evaluate(
    roots: TrustedRoots, *, profile: Optional[str], adapter_root: Any, profiles_dir: Any,
) -> IsolationVerdict:
    """The isolation verdict for one profile. Collects every failure rather
    than stopping at the first, so doctor can list all missing evidence.
    Never raises."""
    try:
        return _evaluate(roots, profile, adapter_root, profiles_dir)
    except Exception:  # noqa: BLE001
        return unverified(("attestation-invalid",))


def _evaluate(roots: TrustedRoots, profile: Optional[str], adapter_root: Any, profiles_dir: Any) -> IsolationVerdict:
    missing: List[str] = []
    evidence: List[str] = []
    guard_failures: List[str] = []
    read = _guarded(
        lambda: read_attestation(roots), AttestationRead("invalid", None, ("attestation-invalid",)),
        guard_failures, "attestation-invalid",
    )
    missing.extend(read.tokens)
    evidence.extend(read.evidence)
    fields: Dict[str, Any] = {}
    if read.state == "read":
        now = _guarded(roots.clock, 0.0, guard_failures, "attestation-invalid")
        fields, tokens = _guarded(
            lambda: validate_attestation(read.data, now), ({}, ("attestation-invalid",)),
            guard_failures, "attestation-invalid",
        )
        missing.extend(tokens)
        if fields:
            if "attestation-expired" not in tokens:
                evidence.append("attestation-current")
            if "attestation-incomplete" not in tokens:
                evidence.append("boundary-declared")
            token = _guarded(
                lambda: corroborate(fields["environment"], roots), None, guard_failures,
                "corroboration-missing",
            )
            if token is None:
                missing.append("corroboration-missing")
            else:
                evidence.append(token)
    opposite = _guarded(
        lambda: opposite_profiles(profile, adapter_root, profiles_dir, scandir=roots.scandir),
        True, guard_failures, "opposite-profile-present",
    )
    if opposite:
        missing.append("opposite-profile-present")
    else:
        evidence.append("opposite-profile-absent")
    for token in guard_failures:
        missing.append(token)
    missing_unique = tuple(dict.fromkeys(missing))
    evidence_unique = tuple(dict.fromkeys(evidence))
    issuer_digest = None
    if fields:
        from . import effective_record

        issuer_digest = effective_record.digest_token(fields["issuer"])
    return IsolationVerdict(
        status="unverified" if missing_unique else "verified",
        environment=fields.get("environment"),
        evidence=evidence_unique,
        missing=missing_unique,
        boundary=fields.get("boundary"),
        issuer_digest=issuer_digest,
        egress_hosts=fields.get("egress_hosts", frozenset()),
    )
