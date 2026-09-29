"""Resolve which model serves each role, and whether it may run.

For every role the candidate is the first one present, in this order: a run
override, the merged role mapping (project over profile), the profile's
default model. There is no fall-through: if that candidate is blocked, the
role is blocked, and a lower-precedence model never quietly takes over. The
three auxiliary uses (title, compaction, summary) follow the auxiliary model,
then the default model.

A candidate is blocked when its provider was excluded by policy, when its
provider or profile cannot serve a work project, or when its model has no
valid qualification record. `allow_unqualified` downgrades only the last
reason to an "unqualified" status and is refused for a work project under a
managed policy.

Reasoning effort is emitted only for a model whose record shows reasoning
support, through an option key backed by recorded evidence for the provider
kind and endpoint family; `max` is never mapped. The native provider and
model reference and the final variant naming belong to the compile stage.

Resolution never resolves a credential and never touches the network.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .errors import Finding, make_finding
from .generate import ROLES
from .merge import EffectiveConfig
from .qualification import QualificationResult

ROLE_DEFAULT_EFFORT: Dict[str, str] = {
    "architect": "high",
    "planner": "high",
    "critic": "high",
    "reviewer": "high",
    "coordinator": "medium",
    "investigator": "medium",
    "implementer": "medium",
    "gate": "low",
}

AUXILIARY: Tuple[str, ...] = ("title", "compaction", "summary")

# (provider kind, endpoint family) -> path of the option key that carries the
# effort. A pair with no entry never gets an effort option.
EFFORT_OPTIONS: Mapping[Tuple[str, str], Tuple[str, ...]] = MappingProxyType(
    {
        ("openai-compatible", "chat-completions"): ("reasoningEffort",),
        ("openrouter", "chat-completions"): ("reasoning", "effort"),
    }
)

_LOG = logging.getLogger("quoin.opencode.roles")


class AllowUnqualifiedRefused(Exception):
    """Running unqualified models is refused for a work project under a
    managed policy."""

    def __init__(self) -> None:
        super().__init__("running unqualified models is not allowed for work under a managed policy")


@dataclass(frozen=True)
class RoleResolution:
    role: str
    auxiliary: bool
    model: str
    provider_id: str
    model_id: str
    provider_kind: str
    endpoint_family: str
    status: str
    reason: str
    block_reason: Optional[str]
    origin: str
    qualification_state: str
    effort: Optional[str]
    effort_origin: Optional[str]
    effort_options: Optional[Mapping[str, Any]]
    effort_diagnostic: Optional[str]


@dataclass(frozen=True)
class Resolutions:
    roles: Tuple[RoleResolution, ...]
    auxiliary: Tuple[RoleResolution, ...]
    findings: Tuple[Finding, ...]
    launchable: bool
    unqualified_models: Tuple[str, ...]


def _candidate(effective: EffectiveConfig, role: str, auxiliary: bool) -> Tuple[str, str, str]:
    """(model name, source token, origin layer) of the first present candidate."""
    values = effective.values
    if auxiliary:
        aux = values.get("auxiliary_model")
        if aux is not None:
            return aux.value, "auxiliary-model", aux.origin[0]
    else:
        mapped = values.get("roles.%s.model" % role)
        if mapped is not None:
            layer = mapped.origin[0]
            return mapped.value, ("override" if layer == "override" else "role-mapping"), layer
    default = values["default_model"]
    return default.value, "default-model", default.origin[0]


def _effort_options(kind: str, family: str, effort: str) -> Optional[Mapping[str, Any]]:
    path = EFFORT_OPTIONS.get((kind, family))
    if path is None:
        return None
    node: Any = effort
    for key in reversed(path):
        node = MappingProxyType({key: node})
    return node


def _block_reason(
    effective: EffectiveConfig, model_name: str, qualification: Optional[QualificationResult]
) -> Tuple[Optional[str], Optional[str]]:
    """(hard block reason, qualification block reason); the first that applies
    is the one to report."""
    model = effective.models[model_name]
    provider = effective.providers[model.provider]
    excluded = effective.excluded_providers.get(provider.id)
    if excluded is not None:
        return excluded, None
    if effective.classification == "work" and (
        effective.profile_classification == "personal" or provider.kind == "openrouter"
    ):
        return "classification-incompatible", None
    if qualification is None:
        return None, "qualification-missing"
    if qualification.state != "qualified":
        return None, "qualification-%s" % qualification.state
    return None, None


def resolve_all(
    effective: EffectiveConfig,
    qualifications: Mapping[str, QualificationResult],
    *,
    allow_unqualified: bool = False,
) -> Resolutions:
    if allow_unqualified and effective.classification == "work" and effective.managed_present:
        raise AllowUnqualifiedRefused()
    resolutions: List[RoleResolution] = []
    findings: List[Finding] = []
    unqualified = set()

    for role, auxiliary in [(r, False) for r in ROLES] + [(a, True) for a in AUXILIARY]:
        model_name, reason, origin = _candidate(effective, role, auxiliary)
        model = effective.models[model_name]
        provider = effective.providers[model.provider]
        qualification = qualifications.get(model_name)
        hard, qual = _block_reason(effective, model_name, qualification)
        status, block = "ok", None
        if hard is not None:
            status, block = "blocked", hard
        elif qual is not None:
            block = qual
            if allow_unqualified:
                status = "unqualified"
                unqualified.add(model_name)
            else:
                status = "blocked"
        assert block is not None or status == "ok"
        if status == "blocked" and block is not None:
            findings.append(make_finding("role-blocked", True, role, block))
        elif status == "unqualified" and block is not None:
            findings.append(make_finding("role-unqualified", True, role, block))

        effort = effort_origin = options = diagnostic = None
        if not auxiliary:
            merged = effective.values.get("roles.%s.effort" % role)
            if merged is not None:
                effort, effort_origin = merged.value, merged.origin[0]
            else:
                effort, effort_origin = ROLE_DEFAULT_EFFORT[role], "default"
            if status != "ok":
                diagnostic = "effort-unqualified"
            elif effort == "max":
                diagnostic = "effort-max"
            elif qualification is None or not qualification.reasoning_supported:
                diagnostic = "effort-no-capability"
            else:
                options = _effort_options(provider.kind, provider.endpoint_family, effort)
                if options is None:
                    diagnostic = "effort-no-mapping"
            if diagnostic is not None:
                findings.append(make_finding("effort-omitted", False, role, diagnostic))
        if role == "summary":
            findings.append(make_finding("summary-unused", False))

        _LOG.info(
            "role=%s model=%s provider=%s status=%s reason=%s origin=%s",
            role, model_name, provider.id, status, reason, origin,
        )
        resolutions.append(
            RoleResolution(
                role=role,
                auxiliary=auxiliary,
                model=model_name,
                provider_id=provider.id,
                model_id=model.model_id,
                provider_kind=provider.kind,
                endpoint_family=provider.endpoint_family,
                status=status,
                reason=reason,
                block_reason=block,
                origin=origin,
                qualification_state=qualification.state if qualification is not None else "missing",
                effort=effort,
                effort_origin=effort_origin,
                effort_options=options,
                effort_diagnostic=diagnostic,
            )
        )

    launchable = all(r.status == "ok" for r in resolutions) and not any(
        f.blocking for f in effective.findings
    )
    return Resolutions(
        roles=tuple(r for r in resolutions if not r.auxiliary),
        auxiliary=tuple(r for r in resolutions if r.auxiliary),
        findings=tuple(findings),
        launchable=launchable,
        unqualified_models=tuple(sorted(unqualified)),
    )
