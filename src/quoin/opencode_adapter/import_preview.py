"""Propose a personal OpenCode profile from the tier-to-model mapping that
`quoin models` already keeps.

Nothing here changes that mapping, the router or the claude-code-router
store: the mapping is only read, through a lazy import (its module pulls in
router code that this package otherwise never loads). The proposal is
validated in memory through the same path a profile file goes through, and it
is written only by `apply`, which requires the caller to confirm every
provider model id explicitly.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence, Tuple

from . import config, jsonio, paths
from .errors import BAD_MODEL_ID_RE
from .generate import ROLES
from .install import PROFILE_RE

TIERS = ("opus", "sonnet", "haiku")
ROLE_TIER: Mapping[str, str] = {
    "architect": "opus",
    "planner": "opus",
    "critic": "opus",
    "reviewer": "opus",
    "coordinator": "sonnet",
    "investigator": "sonnet",
    "implementer": "sonnet",
    "gate": "haiku",
}
DEFAULT_TIER = "sonnet"
AUXILIARY_TIER = "haiku"

PROVIDER_ID = "openrouter"
PROVIDER = {
    "kind": "openrouter",
    "endpoint_family": "chat-completions",
    "base_url": "https://openrouter.ai/api/v1",
    "credential_ref": "env:OPENROUTER_API_KEY",
}

PROFILE_NAME_TEXT = "the profile name must be lowercase letters, digits, hyphens or underscores"
TIERS_TEXT = "the model mapping must name a model for each of opus, sonnet and haiku"
MODEL_ID_TEXT = "a model id is empty or contains whitespace or control characters"

REFUSAL_TEXT: Mapping[str, str] = {
    "unconfirmed": "every proposed provider model id must be confirmed, and nothing else",
    "exists": "the target profile already exists; pass --force to replace it",
}


class ImportRefused(Exception):
    """The write was refused; nothing was changed. Carries a closed code."""

    def __init__(self, code: str, *, missing: Sequence[str] = (), extra_count: int = 0) -> None:
        super().__init__(code)
        self.code = code
        self.missing = tuple(missing)
        self.extra_count = extra_count

    def __str__(self) -> str:
        text = REFUSAL_TEXT[self.code]
        if self.code == "unconfirmed":
            if self.missing:
                text += "; not confirmed: " + ", ".join(self.missing)
            if self.extra_count:
                text += "; %d confirmed id(s) are not part of the proposal" % self.extra_count
        return text


@dataclass(frozen=True)
class Proposal:
    document: Dict[str, Any]
    model_ids: Tuple[str, ...]
    text: str
    profile_name: str


def propose(tiers: Mapping[str, str], *, profile_name: str = "personal") -> Proposal:
    """Build the proposed profile. Pure: no files, no environment."""
    if not isinstance(profile_name, str) or not PROFILE_RE.fullmatch(profile_name):
        raise ValueError(PROFILE_NAME_TEXT)
    slugs: Dict[str, str] = {}
    for tier in TIERS:
        value = tiers.get(tier) if isinstance(tiers, Mapping) else None
        if not isinstance(value, str) or not value.strip():
            raise ValueError(TIERS_TEXT)
        if BAD_MODEL_ID_RE.search(value):
            raise ValueError(MODEL_ID_TEXT)
        slugs[tier] = value

    name_of_slug: Dict[str, str] = {}
    tier_model: Dict[str, str] = {}
    models: Dict[str, Any] = {}
    for tier in TIERS:
        slug = slugs[tier]
        if slug not in name_of_slug:
            name = "or-" + tier
            name_of_slug[slug] = name
            models[name] = {
                "provider": PROVIDER_ID,
                "model_id": slug,
                "qualification_ref": "local:" + name,
            }
        tier_model[tier] = name_of_slug[slug]

    document: Dict[str, Any] = {
        "schema_version": 1,
        "runtime": "opencode",
        "profile": profile_name,
        "classification": "personal",
        "providers": {PROVIDER_ID: dict(PROVIDER)},
        "models": models,
        "default_model": tier_model[DEFAULT_TIER],
        "auxiliary_model": tier_model[AUXILIARY_TIER],
        "roles": {role: {"model": tier_model[ROLE_TIER[role]]} for role in ROLES},
    }
    config.validate_layer_data(
        document, "profile", file_label="profiles/%s.json" % profile_name, expected_profile=profile_name
    )
    text = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    return Proposal(document, tuple(sorted(set(slugs.values()))), text, profile_name)


def read_source(home) -> Tuple[Dict[str, str], bool]:
    """The effective tier mapping, read only, and whether it comes from the
    user's models file (as opposed to the built-in defaults)."""
    from quoin import models

    tiers = models.read_effective_models(home=Path(home))
    return dict(tiers), models.quoin_models_path(Path(home)).exists()


def apply(
    proposal: Proposal,
    *,
    confirmed: Sequence[str],
    force: bool,
    env: Mapping[str, str],
    home,
) -> Path:
    """Write the proposed profile. Refuses unless `confirmed` is exactly the
    set of provider model ids in the proposal, and refuses to replace an
    existing profile without `force`."""
    wanted = set(proposal.model_ids)
    given = set(confirmed)
    missing, extra = wanted - given, given - wanted
    if missing or extra:
        raise ImportRefused("unconfirmed", missing=sorted(missing), extra_count=len(extra))
    target = paths.profile_path(proposal.profile_name, env, home)
    if os.path.lexists(str(target)) and not force:
        raise ImportRefused("exists")
    jsonio.ensure_private_directory(target.parent)
    jsonio.write_private_atomic(target, proposal.text.encode("utf-8"))
    return target
