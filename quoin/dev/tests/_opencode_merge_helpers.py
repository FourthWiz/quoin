"""Shared, non-collected helpers that build loaded configuration layers from
the runtime-config fixtures for the merge, qualification and role tests.

Layers are built directly (no files, no loader) so a test can mutate one
field of a valid fixture and merge it; the loader itself is covered by the
fixture-driven tests.
"""
from __future__ import annotations

import copy
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import _opencode_helpers as helpers
from quoin.opencode_adapter import config, merge, paths, qualification
from quoin.opencode_adapter.errors import ConfigErrors
from quoin.opencode_adapter.roles import resolve_all

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


class RecordingEnv(dict):
    """Environment mapping that records every key read."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.reads = []

    def get(self, key, default=None):
        self.reads.append(key)
        return super().get(key, default)

    def __getitem__(self, key):
        self.reads.append(key)
        return super().__getitem__(key)

    def __contains__(self, key):
        self.reads.append(key)
        return super().__contains__(key)


# =============================================================================
# Whole-pipeline matrix: profile x project x managed, through real files
# =============================================================================

NOW = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)
PROFILES = {"work": PROFILE_WORK, "minimal": PROFILE_MINIMAL, "personal": PROFILE_PERSONAL}
PROJECTS = {
    "none": None,
    "work": PROJECT_WORK,
    "narrow": PROJECT_WORK_NARROW,
    "personal": PROJECT_PERSONAL,
    "unclassified": "valid/project-unclassified.json",
}
MANAGEDS = {"none": None, "managed-work": MANAGED_WORK, "managed-strict": MANAGED_STRICT}
ALLOWED_ENV_READS = {"XDG_CONFIG_HOME", "XDG_STATE_HOME", "QUOIN_OPENCODE_MANAGED_POLICY"}

_PROBE = {}


def probe_module():
    if "m" not in _PROBE:
        _PROBE["m"] = helpers.load_module(
            helpers.OPENCODE_DIR / "probe_gateway.py", "probe_gateway_role_matrix"
        )
    return _PROBE["m"]


# Outcome literals: ("load-error", CLASS), ("merge-error", CLASS) or
# ("ok", effective_providers, excluded_providers, classification, launchable,
# reason the second-model group is blocked for, or None).
# "Second-model group": for the work profile the planner-side roles and the
# auxiliaries, which use work-planner on the second gateway; for the minimal
# and personal profiles every role, which all use the single default model.
DANGLING = ("load-error", "dangling-reference")
_W2, _W1 = ("corp-gw", "corp-gw-b"), ("corp-gw",)
COMBINATIONS = {
    # ---- profile work
    ("work", "none", "none"): ("ok", _W2, {}, "work", False, None),
    ("work", "none", "managed-work"): ("ok", _W1, {"corp-gw-b": "managed-not-allowed"}, "work", False, "managed-not-allowed"),
    ("work", "none", "managed-strict"): ("ok", _W1, {"corp-gw-b": "denied"}, "work", False, "denied"),
    ("work", "work", "none"): ("ok", _W1, {"corp-gw-b": "not-allowed"}, "work", False, "not-allowed"),
    ("work", "work", "managed-work"): ("ok", _W1, {"corp-gw-b": "managed-not-allowed"}, "work", False, "managed-not-allowed"),
    ("work", "work", "managed-strict"): ("ok", _W1, {"corp-gw-b": "not-allowed"}, "work", False, "not-allowed"),
    ("work", "narrow", "none"): ("ok", _W1, {"corp-gw-b": "host-not-allowed"}, "work", False, "host-not-allowed"),
    ("work", "narrow", "managed-work"): ("ok", _W1, {"corp-gw-b": "managed-not-allowed"}, "work", False, "managed-not-allowed"),
    ("work", "narrow", "managed-strict"): ("ok", _W1, {"corp-gw-b": "denied"}, "work", False, "denied"),
    ("work", "personal", "none"): ("ok", _W2, {}, "work", True, None),
    ("work", "personal", "managed-work"): ("ok", _W1, {"corp-gw-b": "managed-not-allowed"}, "work", False, "managed-not-allowed"),
    ("work", "personal", "managed-strict"): ("ok", _W1, {"corp-gw-b": "denied"}, "work", False, "denied"),
    ("work", "unclassified", "none"): ("ok", _W2, {}, "work", False, None),
    ("work", "unclassified", "managed-work"): ("ok", _W1, {"corp-gw-b": "managed-not-allowed"}, "work", False, "managed-not-allowed"),
    ("work", "unclassified", "managed-strict"): ("ok", _W1, {"corp-gw-b": "denied"}, "work", False, "denied"),
    # ---- profile minimal
    ("minimal", "none", "none"): ("ok", ("local-gw",), {}, "work", False, None),
    ("minimal", "none", "managed-work"): ("ok", (), {"local-gw": "managed-not-allowed"}, "work", False, "managed-not-allowed"),
    ("minimal", "none", "managed-strict"): ("ok", (), {"local-gw": "host-not-allowed"}, "work", False, "host-not-allowed"),
    ("minimal", "work", "none"): DANGLING,
    ("minimal", "work", "managed-work"): DANGLING,
    ("minimal", "work", "managed-strict"): DANGLING,
    ("minimal", "narrow", "none"): DANGLING,
    ("minimal", "narrow", "managed-work"): DANGLING,
    ("minimal", "narrow", "managed-strict"): DANGLING,
    ("minimal", "personal", "none"): ("ok", ("local-gw",), {}, "work", True, None),
    ("minimal", "personal", "managed-work"): ("ok", (), {"local-gw": "managed-not-allowed"}, "work", False, "managed-not-allowed"),
    ("minimal", "personal", "managed-strict"): ("ok", (), {"local-gw": "host-not-allowed"}, "work", False, "host-not-allowed"),
    ("minimal", "unclassified", "none"): ("ok", ("local-gw",), {}, "work", False, None),
    ("minimal", "unclassified", "managed-work"): ("ok", (), {"local-gw": "managed-not-allowed"}, "work", False, "managed-not-allowed"),
    ("minimal", "unclassified", "managed-strict"): ("ok", (), {"local-gw": "host-not-allowed"}, "work", False, "host-not-allowed"),
    # ---- profile personal
    ("personal", "none", "none"): ("ok", ("openrouter",), {}, "personal", False, None),
    ("personal", "none", "managed-work"): ("ok", (), {"openrouter": "managed-not-allowed"}, "personal", False, "managed-not-allowed"),
    ("personal", "none", "managed-strict"): ("ok", (), {"openrouter": "host-not-allowed"}, "personal", False, "host-not-allowed"),
    ("personal", "work", "none"): DANGLING,
    ("personal", "work", "managed-work"): DANGLING,
    ("personal", "work", "managed-strict"): DANGLING,
    ("personal", "narrow", "none"): DANGLING,
    ("personal", "narrow", "managed-work"): DANGLING,
    ("personal", "narrow", "managed-strict"): DANGLING,
    ("personal", "personal", "none"): ("ok", ("openrouter",), {}, "personal", True, None),
    ("personal", "personal", "managed-work"): ("ok", (), {"openrouter": "managed-not-allowed"}, "personal", False, "managed-not-allowed"),
    ("personal", "personal", "managed-strict"): ("ok", (), {"openrouter": "host-not-allowed"}, "personal", False, "host-not-allowed"),
    ("personal", "unclassified", "none"): ("ok", ("openrouter",), {}, "personal", False, None),
    ("personal", "unclassified", "managed-work"): ("ok", (), {"openrouter": "managed-not-allowed"}, "personal", False, "managed-not-allowed"),
    ("personal", "unclassified", "managed-strict"): ("ok", (), {"openrouter": "host-not-allowed"}, "personal", False, "host-not-allowed"),
}



class Outcome:
    def __init__(self):
        self.stage = "load"
        self.error = None
        self.loaded = self.effective = self.qualifications = self.resolutions = None
        self.env = None


def _install(env, home, rel):
    text = (helpers.SOURCE_DIR / "adapters" / "opencode" / "fixtures" / "runtime-config" / rel).read_text(
        encoding="utf-8"
    )
    name = json.loads(text)["profile"]
    target = paths.profile_path(name, env, home)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return name


def write_probe_records(env, home, effective, now=NOW):
    """A qualified record, built by the probe itself, for every profile model."""
    probe = probe_module()
    pinned = qualification.pinned_version()
    for model in effective.models.values():
        provider = effective.providers[model.provider]
        steps = [
            {"step": i + 1, "name": name, "result": "pass", "diagnostic": None}
            for i, name in enumerate(("auth_and_text", "tool_round_trip", "streaming"))
        ]
        report = probe.ProbeReport(context=None, steps=steps, verdict="qualified", blocking_step=None)
        cfg = probe.ProbeConfig(
            base_url=merge.provider_base_url(provider), model=model.model_id, provider=provider.id,
            credential_env=provider.credential_env, runtime_version=pinned,
        )
        record = probe.build_capability_record(report, cfg, now=now - timedelta(days=1))
        target = paths.qualification_path(model.qualification_ref[len("local:"):], env, home)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(target.parent, 0o700)
        target.write_text(json.dumps(record), encoding="utf-8")
        os.chmod(target, 0o600)


def run_pipeline(tmp_path, key):
    """load_all -> merge -> evaluate_all -> resolve_all as far as it goes."""
    profile_key, project_key, managed_key = key
    env = RecordingEnv({"XDG_CONFIG_HOME": str(tmp_path / "xdg"), "XDG_STATE_HOME": str(tmp_path / "state")})
    home, project_root = tmp_path / "home", tmp_path / "project"
    out = Outcome()
    out.env = env
    name = _install(env, home, PROFILES[profile_key])
    if PROJECTS[project_key] is not None:
        (project_root / ".quoin").mkdir(parents=True)
        (project_root / ".quoin" / "runtime.json").write_text(
            (helpers.SOURCE_DIR / "adapters" / "opencode" / "fixtures" / "runtime-config" / PROJECTS[project_key]).read_text(
                encoding="utf-8"
            ),
            encoding="utf-8",
        )
    if MANAGEDS[managed_key] is not None:
        managed_file = tmp_path / "managed.json"
        managed_file.write_text(
            (helpers.SOURCE_DIR / "adapters" / "opencode" / "fixtures" / "runtime-config" / MANAGEDS[managed_key]).read_text(
                encoding="utf-8"
            ),
            encoding="utf-8",
        )
        env["QUOIN_OPENCODE_MANAGED_POLICY"] = str(managed_file)
    try:
        out.loaded = config.load_all(project_root=project_root, profile=name, env=env, home=home)
    except ConfigErrors as exc:
        out.error = exc
        return out
    out.stage = "merge"
    try:
        out.effective = merge.merge(out.loaded)
    except ConfigErrors as exc:
        out.error = exc
        return out
    out.stage = "resolve"
    write_probe_records(env, home, out.effective)
    out.qualifications = qualification.evaluate_all(
        out.effective, env=env, home=home, now=NOW, pinned_version=qualification.pinned_version()
    )
    out.resolutions = resolve_all(out.effective, out.qualifications)
    return out




# =============================================================================
# A complete on-disk world for compiler tests
# =============================================================================


class World:
    """A temporary home, project, profile, optional managed policy, generated
    agent stubs and probe-built qualification records, as real files."""

    def __init__(self, tmp_path, profile=PROFILE_WORK, project="open", managed=None,
                 agents=True, records=True):
        self.tmp = Path(tmp_path)
        self.env = {
            "XDG_CONFIG_HOME": str(self.tmp / "xdg"),
            "XDG_STATE_HOME": str(self.tmp / "state"),
        }
        self.home = self.tmp / "home"
        self.home.mkdir(parents=True, exist_ok=True)
        self.root = self.tmp / "project"
        self.root.mkdir(parents=True, exist_ok=True)
        self.profile = _data(profile)
        if project == "open":
            # A classified project that narrows nothing.
            self.project = {
                "schema_version": 1, "runtime": "opencode",
                "profile": self.profile["profile"],
                "classification": self.profile["classification"],
            }
        else:
            self.project = _data(project) if project is not None else None
        self.managed = _data(managed) if managed is not None else None
        self.agents = agents
        self.write()
        if records:
            self.write_records()

    def write(self):
        target = paths.profile_path(self.profile["profile"], self.env, self.home)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.profile, indent=2), encoding="utf-8")
        if self.project is not None:
            (self.root / ".quoin").mkdir(exist_ok=True)
            (self.root / ".quoin" / "runtime.json").write_text(json.dumps(self.project), encoding="utf-8")
        if self.managed is not None:
            managed_file = self.tmp / "managed.json"
            managed_file.write_text(json.dumps(self.managed), encoding="utf-8")
            self.env["QUOIN_OPENCODE_MANAGED_POLICY"] = str(managed_file)
        if self.agents:
            from quoin.opencode_adapter.generate import ROLES

            agent_dir = self.root / ".opencode" / "agents"
            agent_dir.mkdir(parents=True, exist_ok=True)
            for role in ROLES:
                (agent_dir / ("quoin-%s.md" % role)).write_text("stub\n", encoding="utf-8")

    def effective(self):
        loaded = config.load_all(
            project_root=self.root, profile=self.profile["profile"], env=self.env, home=self.home
        )
        return merge.merge(loaded)

    def write_records(self, now=None):
        write_probe_records(self.env, self.home, self.effective(), now=now or NOW)

    def evaluate(self, **kw):
        from quoin.opencode_adapter import compiler

        kw.setdefault("now", NOW)
        return compiler.evaluate(
            project_root=self.root, profile=self.profile["profile"], env=self.env, home=self.home, **kw
        )
