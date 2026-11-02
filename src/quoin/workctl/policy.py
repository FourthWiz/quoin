"""The single authorization policy every transport must call.

This release is read-only: any operation not marked read-only is denied
here, before a backend or credential is touched, whatever transport the
request came through. A service must be configured, the configuration may
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
            "workctl is read-only in this release; external writes need the approved-write broker",
        )
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
    if service.operations is not None and op.name not in service.operations:
        raise BrokerError(POLICY_DENIED, "%s is not allowed by the broker configuration" % op.name)
    return service
