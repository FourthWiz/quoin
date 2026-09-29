"""Shared, non-collected helpers that build loaded configuration layers from
the runtime-config fixtures for the merge, qualification and role tests.

Layers are built directly (no files, no loader) so a test can mutate one
field of a valid fixture and merge it; the loader itself is covered by the
fixture-driven tests.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from quoin.opencode_adapter import config

FIXTURES = Path(__file__).resolve().parent.parent.parent / "adapters" / "opencode" / "fixtures" / "runtime-config"

PROFILE_WORK = "valid/profile-work.json"
PROFILE_MINIMAL = "valid/profile-minimal.json"
PROFILE_PERSONAL = "valid/profile-personal.json"
PROJECT_WORK = "valid/project-work.json"
PROJECT_WORK_NARROW = "valid/project-work-narrow.json"
PROJECT_PERSONAL = "valid/project-personal.json"
PROJECT_UNCLASSIFIED = "valid/project-unclassified.json"
MANAGED_WORK = "valid/managed-work.json"
MANAGED_STRICT = "valid/managed-strict.json"


def fixture(rel):
    return json.loads((FIXTURES / rel).read_text(encoding="utf-8"))


def _data(source):
    return copy.deepcopy(fixture(source)) if isinstance(source, str) else copy.deepcopy(source)


def make_layer(kind, source):
    data = _data(source)
    label = {
        "profile": "profiles/%s.json" % data.get("profile", "work"),
        "project": ".quoin/runtime.json",
        "managed": "managed policy",
    }[kind]
    if kind == "profile":
        state = data["classification"]
    elif kind == "project":
        value = data.get("classification", None) if "classification" in data else None
        state = "missing" if "classification" not in data else (
            value if value in ("work", "personal") else "unknown"
        )
    else:
        state = "missing"
    return config.Layer(kind, label, data, state)


def loaded(profile=PROFILE_WORK, project=None, managed=None):
    """`profile`, `project` and `managed` are fixture paths, dicts or None."""
    return config.LoadedConfig(
        make_layer("profile", profile),
        make_layer("project", project) if project is not None else None,
        make_layer("managed", managed) if managed is not None else None,
    )
