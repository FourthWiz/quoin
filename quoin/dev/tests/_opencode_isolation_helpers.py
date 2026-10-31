"""Shared, non-collected helpers for the isolation and launch-policy tests.

`fixture_roots` builds a trusted-root tree under `tmp_path` and returns the
`TrustedRoots` that read it: the "trusted uid" is the test user and the
invoking uid is one higher, so no test needs real root. Nothing here reads or
writes the machine's attestation path.
"""
from __future__ import annotations

import copy
import dataclasses
import json
import os
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from quoin.opencode_adapter import isolation

USER = "fixtureuser"

GOOD_ATTESTATION: Dict[str, Any] = {
    "attestation_format": 1,
    "environment": "container",
    "issuer": "corp-it-fixture",
    "boundary": {"filesystem": True, "secrets": True, "process_env": True, "egress": "firewall"},
}


def _mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o755)


def _write(path: Path, text: str = "") -> None:
    _mkdir(path.parent)
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o644)


def trusted_root_of(tmp_path: Path) -> Path:
    return Path(tmp_path) / "trusted-root"


def write_attestation(roots: isolation.TrustedRoots, data: Any) -> Path:
    text = data if isinstance(data, str) else json.dumps(data)
    _write(roots.attestation_path, text)
    return roots.attestation_path


def write_corroboration(root: Path, environment: str) -> None:
    if environment == "container":
        _write(root / ".dockerenv")
    elif environment == "vm":
        _write(root / "sys/class/dmi/id/sys_vendor", "QEMU\n")
    elif environment == "managed-os-user":
        _write(root / "Library" / "Managed Preferences" / USER / "ai.quoin.isolation.plist", "<plist/>")


def unverified_roots(tmp_path: Path, **overrides: Any) -> isolation.TrustedRoots:
    """Roots over an empty tree: no attestation, nothing to corroborate."""
    root = trusted_root_of(tmp_path)
    _mkdir(root)
    base = isolation.TrustedRoots(
        attestation_path=root / "etc/quoin/opencode" / "isolation-attestation.json",
        chain_top=root, owner_uid=os.getuid(), invoking_uid=os.getuid() + 1, platform="linux",
        probe_root=root, user_name=lambda uid: USER, sysctl=lambda name: None,
        clock=lambda: 1_800_000_000.0, scandir=os.scandir,
    )
    return dataclasses.replace(base, **overrides)


def fixture_roots(
    tmp_path: Path, environment: str = "container", *, attestation: Optional[Mapping[str, Any]] = None,
    corroborate: bool = True, **overrides: Any,
) -> isolation.TrustedRoots:
    """Roots whose attestation is valid and corroborated for `environment`."""
    if environment == "managed-os-user":
        overrides.setdefault("platform", "darwin")
    if environment == "vm" and overrides.get("platform") == "darwin":
        overrides.setdefault("sysctl", lambda name: "1" if name == "kern.hv_vmm_present" else None)
    roots = unverified_roots(tmp_path, **overrides)
    data = copy.deepcopy(GOOD_ATTESTATION)
    data["environment"] = environment
    if attestation:
        data.update(copy.deepcopy(dict(attestation)))
    write_attestation(roots, data)
    if corroborate:
        write_corroboration(roots.probe_root, environment)
    return roots


def empty_adapter_dirs(tmp_path: Path):
    """An adapter state root and a profiles directory that hold no profile."""
    state = Path(tmp_path) / "adapter-state"
    profiles = Path(tmp_path) / "adapter-config" / "profiles"
    _mkdir(state)
    _mkdir(profiles)
    return state, profiles


def verdict_for(roots: isolation.TrustedRoots, tmp_path: Path, profile: str = "work") -> isolation.IsolationVerdict:
    state, profiles = empty_adapter_dirs(tmp_path)
    return isolation.evaluate(roots, profile=profile, adapter_root=state, profiles_dir=profiles)


# --------------------------------------------------------------- identity


class FakeIdentity:
    """An identity checker that never touches the network. `results` maps a
    destination host key to an `IdentityResult`; any other host is `ok` over
    TLS. Every destination it was asked about is recorded."""

    def __init__(self, results: Optional[Mapping[str, Any]] = None) -> None:
        from quoin.opencode_adapter import netpolicy

        self._netpolicy = netpolicy
        self.results = dict(results or {})
        self.checked: list = []

    def check(self, destination, *, policy, proxy=None):
        self.checked.append(destination.host_key)
        if destination.host_key in self.results:
            return self.results[destination.host_key]
        return self._netpolicy.IdentityResult("ok", "tls", None, 1, "0" * 16, False)


def seams(tmp_path: Path, **kw: Any) -> Dict[str, Any]:
    """Driver keyword arguments that make a work world launchable without a
    real attestation or network: fixture trusted roots and a fake identity
    checker."""
    return {
        "isolation_roots": fixture_roots(tmp_path, **kw.pop("roots", {})),
        "identity_checker": kw.pop("identity", None) or FakeIdentity(),
    }


def launch_ready(world: Any, extra_roots: Any = ()) -> None:
    """Approve the world's project root (and any extra root) as a repository
    root in the world's own profile copy and rewrite it. The shared fixture
    files stay untouched. A root under the adapter state directory is
    refused: approving it would hide the regression the launcher-snapshot
    rule exists to prevent."""
    from quoin.opencode_adapter import profile_dirs

    state_root = os.path.realpath(str(profile_dirs.adapter_root(world.env, world.home)))
    roots = [os.path.realpath(str(world.root))]
    for extra in extra_roots:
        real = os.path.realpath(str(extra))
        if real == state_root or real.startswith(state_root + os.sep):
            raise ValueError("an approved root must not lie under the adapter state directory")
        roots.append(real)
    policy = world.profile.setdefault("policy", {})
    policy.setdefault("repositories", {})["allowed_roots"] = roots
    world.write()
