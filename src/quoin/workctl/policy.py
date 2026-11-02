"""The single authorization policy every transport must call.

The read broker is read-only: any operation not marked read-only is denied
by `authorize`, before a backend or credential is touched, whatever
transport the request came through. A write is only ever prepared as a
local proposal (`authorize_proposal`, which touches no credential) and is
executed by the separate writer after a trusted approval
(`authorize_write`). A service must be configured, the configuration may
narrow the operations it allows, and a configuration bound to a profile
runs only inside a launcher session of that profile (outside any session
it is refused). The binding guards against accidental cross-profile use;
it is not a security boundary, since the session marker lives in the
caller's environment.
"""
from __future__ import annotations

from typing import Optional

from .config import GUIDANCE, BrokerConfig, ServiceConfig
from .envelope import POLICY_DENIED, BrokerError
from .operations import Operation


def authorize(
    op: Operation, config: Optional[BrokerConfig], *, launcher_profile: Optional[str]
) -> ServiceConfig:
    if not op.read_only:
        raise BrokerError(
            POLICY_DENIED,
            "the workctl broker only reads; prepare a local proposal with `workctl propose` and hand it "
            "to the user for approval or manual submission",
        )
    return _service(op, config, launcher_profile=launcher_profile)


WRITES_DISABLED = (
    "writes are disabled: no trusted approval boundary is configured, so deliver the local draft "
    "for manual submission"
)


def authorize_proposal(
    op: Operation, config: Optional[BrokerConfig], *, launcher_profile: Optional[str]
) -> ServiceConfig:
    """A proposal is a local draft bound to a configured service; preparing
    one needs no credential and no approval."""
    if op.read_only:
        raise BrokerError(POLICY_DENIED, "%s is not a write operation" % op.name)
    return _service(op, config, launcher_profile=launcher_profile)


def authorize_write(op: Operation, config: Optional[BrokerConfig]) -> ServiceConfig:
    """The writer's policy: writes must be enabled by a declared approval
    boundary and the operation listed among the allowed writes."""
    if op.read_only:
        raise BrokerError(POLICY_DENIED, "%s is not a write operation" % op.name)
    if config is None:
        raise BrokerError(POLICY_DENIED, "no broker configuration found; " + GUIDANCE)
    if not config.writes.enabled:
        raise BrokerError(POLICY_DENIED, WRITES_DISABLED)
    if op.name not in config.writes.operations:
        raise BrokerError(POLICY_DENIED, "%s is not among the configured write operations" % op.name)
    return _service(op, config, launcher_profile=config.profile)


def _service(op: Operation, config: Optional[BrokerConfig], *, launcher_profile: Optional[str]) -> ServiceConfig:
    if config is None:
        raise BrokerError(POLICY_DENIED, "no broker configuration found; " + GUIDANCE)
    if config.profile is not None and launcher_profile != config.profile:
        raise BrokerError(
            POLICY_DENIED,
            "the broker configuration is bound to profile %s, which is not the running session's profile"
            % config.profile,
        )
    service = config.services.get(op.service)
    if service is None:
        raise BrokerError(POLICY_DENIED, "%s is not configured; %s" % (op.service, GUIDANCE))
    # The per-service allowlist names reads only; writes are allowed by writes.operations.
    if op.read_only and service.operations is not None and op.name not in service.operations:
        raise BrokerError(POLICY_DENIED, "%s is not allowed by the broker configuration" % op.name)
    return service
