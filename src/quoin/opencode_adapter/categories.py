"""The seven problem categories shared by run refusals and doctor findings.

A refusal from the runtime driver and a doctor finding about the same problem
name the category with the same slug. This module holds the closed tables;
tests derive the full id lists from the doctor, the error catalogue and the
driver, so a new finding or class without a category fails the suite.

Stdlib only, and nothing from the rest of the adapter is imported at module
level, so the doctor can load it lazily without an import cycle.
"""
from __future__ import annotations

from typing import Dict, Tuple

CATEGORIES: Tuple[str, ...] = (
    "missing-binary",
    "unsupported-version",
    "invalid-configuration",
    "unqualified-gateway",
    "policy-denial",
    "missing-optional-integration",
    "workflow-validation",
)

LABELS: Dict[str, str] = {
    "missing-binary": "missing binary",
    "unsupported-version": "unsupported version",
    "invalid-configuration": "invalid configuration",
    "unqualified-gateway": "unqualified gateway",
    "policy-denial": "policy denial",
    "missing-optional-integration": "missing optional integration",
    "workflow-validation": "workflow validation failure",
}


def _table(category: str, *ids: str) -> Dict[str, str]:
    return {i: category for i in ids}


FINDING_CATEGORIES: Dict[str, str] = {}
FINDING_CATEGORIES.update(_table("missing-binary", "opencode-binary-absent"))
FINDING_CATEGORIES.update(
    _table("unsupported-version", "opencode-version", "opencode-version-unknown")
)
FINDING_CATEGORIES.update(
    _table(
        "invalid-configuration",
        "flags-set",
        "project-config-disabled",
        "config-env-set",
        "config-unreadable",
        "subagent-depth-raised",
        "permission-loosened",
        "skills-path-missing",
        "skill-duplicate",
        "skill-duplicate-unnamed",
        "quoin-skill-outside-project",
        "legacy-claude-skills",
        "runtime-config-invalid",
        "runtime-compile-blocked",
    )
)
FINDING_CATEGORIES.update(_table("unqualified-gateway", "runtime-gateway-unqualified"))
FINDING_CATEGORIES.update(
    _table("policy-denial", "runtime-config-policy-denied", "runtime-plugin-directory-present")
)
FINDING_CATEGORIES.update(
    _table(
        "missing-optional-integration",
        "quoin-not-on-path",
        "skills-url-not-scanned",
        "rules-global-claude-md",
        "rules-project-claude-md",
        "rules-agents-md-present",
        "census-unverified",
        "census-truncated",
    )
)
FINDING_CATEGORIES.update(
    _table(
        "workflow-validation",
        "smoke-render",
        "smoke-roundtrip",
        "smoke-names",
        "smoke-frontmatter",
        "smoke-digests",
        "smoke-bundle",
        "smoke-read-only-roles",
        "smoke-task-graph",
        "smoke-forbidden-output",
        "smoke-script-refs",
        "smoke-config",
        "smoke-ok",
        "render-failed",
        "manifest-drift",
        "manifest-unreadable",
        "install-metadata-invalid",
        "install-absent",
        "install-current",
        "install-version-differs",
        "owned-missing",
        "owned-modified",
        "owned-stale",
        "owned-not-installed",
        "owned-unreadable",
        "temp-leftover",
        "doctor-internal-error",
        "runtime-process-groups-unsupported",
        "runtime-sidecar-dir-unwritable",
        "runtime-orphan-run",
        "runtime-stale-lock",
        "runtime-command-agent-not-primary",
        "runtime-profile-launchable",
        "runtime-adapter-data-missing",
    )
)

# Rejection classes the configuration pipeline can report, by category. The
# driver refuses these as policy denials; every other class is a plain
# invalid configuration.
_POLICY_CONFIG_CLASSES = (
    "personal-profile-for-work",
    "personal-provider-kind-for-work",
    "allowlist-broadening",
    "cross-profile-fallback-enabled",
)
_OTHER_CONFIG_CLASSES = (
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
    "limit-above-ceiling",
    "unknown-key",
    "invalid-type",
    "dangling-reference",
    "env-name-collision",
    "missing-classification",
)

CONFIG_CLASS_CATEGORIES: Dict[str, str] = {}
CONFIG_CLASS_CATEGORIES.update(_table("policy-denial", *_POLICY_CONFIG_CLASSES))
CONFIG_CLASS_CATEGORIES.update(_table("invalid-configuration", *_OTHER_CONFIG_CLASSES))


def category_for(finding_id: str) -> str:
    """Category slug of a doctor finding id; ``KeyError`` for an unmapped id."""
    return FINDING_CATEGORIES[finding_id]


def config_class_category(rejection_class: str) -> str:
    """Category slug of a configuration rejection class."""
    return CONFIG_CLASS_CATEGORIES[rejection_class]
