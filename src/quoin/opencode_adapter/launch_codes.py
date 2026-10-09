"""Closed registry of launch refusal codes and their categories.

A launch refusal is a `PrepareRefused` or `LaunchRefused` carrying a stable
machine code and one of the shared problem categories. This module lists every
code the adapter can raise today, plus the codes that later isolation work will
introduce, so a code is declared here before any raise site uses it. Tests scan
the launch modules and fail on a code that is missing from the registry or
registered under a different category.

Nothing here is wired into a raise site: the table only describes. Probe and
test-run refusals are separate output channels with their own closed sets and
are deliberately not part of this registry.

Stdlib only; imports `categories` and `errors`, which import nothing else from
the adapter.
"""
from __future__ import annotations

from types import MappingProxyType
from typing import Dict, Mapping

from . import categories, errors

# Every code a literal raise site passes today, with the category at that site.
EXISTING_CODES: Dict[str, str] = {
    "adapter-data-missing": "workflow-validation",
    "agent-file-overridden": "policy-denial",
    "allow-unqualified-refused": "policy-denial",
    "command-agent-not-primary": "workflow-validation",
    "command-file-overridden": "policy-denial",
    "command-key-overridden": "policy-denial",
    "compile-blocked": "invalid-configuration",
    "compiled-file-changed": "invalid-configuration",
    "compiled-output-refused": "invalid-configuration",
    "compiled-output-unwritable": "invalid-configuration",
    "compiled-stale": "invalid-configuration",
    "config-layer-unreadable": "invalid-configuration",
    "context-ref-invalid": "workflow-validation",
    "continue-loop-on-deny": "invalid-configuration",
    "credential-unresolved": "invalid-configuration",
    "data-dir-unsafe": "invalid-configuration",
    "install-record-invalid": "workflow-validation",
    "invalid-project-root": "workflow-validation",
    "invalid-stage": "workflow-validation",
    "invalid-task-name": "workflow-validation",
    "non-git-root-unverified": "workflow-validation",
    "not-installed": "workflow-validation",
    "not-launchable": "unqualified-gateway",
    "opencode-binary-absent": "missing-binary",
    "opencode-version": "unsupported-version",
    "owned-file-drift": "workflow-validation",
    "phase-unsupported": "workflow-validation",
    "plugin-directory-present": "policy-denial",
    "process-groups-unsupported": "workflow-validation",
    "project-root-mismatch": "workflow-validation",
    "protected-key-overridden": "policy-denial",
    "resume-request-mismatch": "workflow-validation",
    "resume-run-missing": "workflow-validation",
    "role-unresolved": "workflow-validation",
    "run-record-invalid": "workflow-validation",
    "sidecar-dir-unwritable": "workflow-validation",
    "whole-task-unavailable": "workflow-validation",
    "workspace-invalid": "workflow-validation",
    "workspace-missing": "workflow-validation",
    "workspace-not-installed": "workflow-validation",
    "workspace-not-isolated": "workflow-validation",
}

# The driver refuses a configuration defect with the rejection class as the
# code, or `config-invalid` when no error object is available.
CONFIG_DERIVED_CODES: Dict[str, str] = {
    cls: categories.CONFIG_CLASS_CATEGORIES[cls] for cls in sorted(errors.REJECTION_CLASSES)
}
CONFIG_DERIVED_CODES["config-invalid"] = "invalid-configuration"

# `compiled-` plus each reason `compiler.check` can return other than the
# two the driver repairs by rewriting (`missing`, `stale`).
COMPILED_REASON_CODES: Dict[str, str] = {
    "compiled-not-private": "invalid-configuration",
    "compiled-flag-mismatch": "invalid-configuration",
}

# Codes the profile-isolation work introduces in later stages.
PLANNED_CODES: Dict[str, str] = {}
for _code in (
    "profile-switch-in-process",
    "profile-stamp-mismatch",
    "repository-not-approved",
    "approved-repositories-unset",
    "isolation-required-unverified",
    "inventory-plugin",
    "inventory-provider",
    "inventory-disabled-providers",
    "inventory-mcp-local",
    "inventory-mcp-remote",
    "inventory-tool",
    "inventory-lsp",
    "inventory-formatter",
    "inventory-agent-permission",
    "inventory-config-permission",
    "inventory-remote-config",
    "inventory-remote-instructions",
    "inventory-unknown-key",
    "inventory-legacy-config",
    "agent-model-outside-allowlist",
    "command-model-outside-allowlist",
    "config-env-opencode-config",
    "config-env-opencode-config-dir",
    "config-env-opencode-config-content",
    "executable-fetch-possible",
    "executable-fetch-detected",
    "allowed-hosts-unset",
    "destination-not-approved",
    "web-tools-denied",
    "mcp-remote-denied",
    "proxy-route-not-approved",
    "destination-identity-mismatch",
    "destination-unverifiable",
    "destination-plaintext",
):
    PLANNED_CODES[_code] = "policy-denial"
for _code in ("profile-dir-unsafe", "env-path-outside-profile"):
    PLANNED_CODES[_code] = "invalid-configuration"
for _code in ("profile-selection-unverified", "profile-unknown", "purge-run-active"):
    PLANNED_CODES[_code] = "workflow-validation"
del _code


def _build() -> Mapping[str, str]:
    merged: Dict[str, str] = {}
    for group in (EXISTING_CODES, CONFIG_DERIVED_CODES, COMPILED_REASON_CODES, PLANNED_CODES):
        for code, category in group.items():
            if code in merged and merged[code] != category:
                raise AssertionError("launch code registered under two categories")
            merged[code] = category
    return MappingProxyType(merged)


REGISTRY: Mapping[str, str] = _build()


def category(code: str) -> str:
    """Category of a registered launch code; ``KeyError`` for an unknown one."""
    return REGISTRY[code]


def is_registered(code: str) -> bool:
    return code in REGISTRY
