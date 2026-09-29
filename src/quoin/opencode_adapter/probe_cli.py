"""Profile-driven wiring of the gateway probe.

`run` reads a profile (and, optionally, a project), decides whether the
selected model may be probed, resolves its credential at use time and hands
it to the probe script in memory. The probe script ships as data and is
loaded from its file under a private module name; it is never imported by its
public name and never modified.

Everything the run needs from the outside (environment mappings, home
directory, clock, platform, keychain runner, error stream) is passed in, so
this module never reads or writes the process environment.

A run that does not finish writing a new record leaves the model without a
qualification: the previous record is removed before any request is sent.
"""
from __future__ import annotations

import dataclasses
import importlib.util
import inspect
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from . import config, jsonio, merge, paths, qualification
from . import secrets as credentials
from .errors import ConfigErrors

PRIVATE_NAME = "_quoin_opencode_probe_gateway"
PROBE_ENV_NAME = "QUOIN_PROBE_CREDENTIAL"

PROBE_MESSAGES: Mapping[str, str] = {
    "synthetic-only-required": (
        "the probe sends live requests to the gateway; rerun with --synthetic-only "
        "to confirm only synthetic prompts are sent"
    ),
    "unknown-model": "the requested model is not defined by this profile",
    "provider-excluded": "the model's provider is excluded by the effective policy",
    "classification-required": (
        "the project has no work or personal classification; add one to "
        ".quoin/runtime.json or omit --project-root"
    ),
    "classification-incompatible": "this profile's provider cannot serve a work project",
    "responses-unsupported": (
        "probe supports chat-completions only; this provider uses the responses endpoint family"
    ),
}
LIVE_NOTICE = (
    "quoin: this probe sends live requests to the configured gateway and may be billed; "
    "only synthetic prompts are sent."
)
UNEXPECTED_TEXT = "quoin: the probe failed unexpectedly: %s"
RECORD_REMOVE_TEXT = "quoin: the previous qualification record could not be removed; nothing was sent"
CONTRACT_TEXT = "probe script does not match this version"

_EXECUTE_PARAMETERS = ("config", "env", "extra_headers", "now", "nonce_factory")
_CONFIG_FIELDS = frozenset(
    {
        "base_url", "model", "credential_env", "provider", "runtime_version",
        "ca_file", "use_env_proxy", "output",
    }
)
_LOADED: Dict[str, Any] = {}


def _check_contract(module: Any) -> None:
    try:
        parameters = tuple(inspect.signature(module.execute).parameters)
        fields = {f.name for f in dataclasses.fields(module.ProbeConfig)}
    except (AttributeError, TypeError, ValueError):
        raise paths.AdapterDataMissing(CONTRACT_TEXT) from None
    if parameters != _EXECUTE_PARAMETERS or not _CONFIG_FIELDS <= fields:
        raise paths.AdapterDataMissing(CONTRACT_TEXT)


def load_probe_module(data_dir: Optional[Path] = None) -> Any:
    """Load `probe_gateway.py` from the adapter data directory.

    The module is registered in `sys.modules` under a private name only (its
    dataclasses need the entry while the file executes), bytecode is not
    written next to the script, and the loaded module is cached per path.
    """
    base = Path(data_dir) if data_dir is not None else paths.adapter_data_dir()
    if base is None:
        raise paths.AdapterDataMissing("adapter data directory not found")
    script = base / "probe_gateway.py"
    if not script.is_file():
        raise paths.AdapterDataMissing("probe script not found")
    key = os.path.realpath(str(script))
    cached = _LOADED.get(key)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(PRIVATE_NAME, str(script))
    if spec is None or spec.loader is None:
        raise paths.AdapterDataMissing("probe script not found")
    module = importlib.util.module_from_spec(spec)
    sys.modules[PRIVATE_NAME] = module
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
        _check_contract(module)
    except BaseException:
        sys.modules.pop(PRIVATE_NAME, None)
        raise
    finally:
        sys.dont_write_bytecode = previous
    _LOADED[key] = module
    return module


def _refuse(stderr: Any, code: str, detail: Optional[str] = None) -> int:
    text = PROBE_MESSAGES[code]
    if detail:
        text = "%s (%s)" % (text, detail)
    stderr.write("quoin: %s\n" % text)
    return 2


def run(
    *,
    profile: str,
    model: Optional[str],
    project_root: Optional[Path],
    synthetic_only: bool,
    env: Mapping[str, str],
    environ: Mapping[str, str],
    home: Path,
    now: datetime,
    platform: str,
    runner: Any = None,
    stderr: Any = None,
    probe_module: Any = None,
) -> int:
    """Qualify one profile model against its gateway. Returns the probe's exit
    code (0 qualified, 1 not qualified, 2 could not run) or 2 for a refusal.

    `env` holds only the configuration paths; `environ` is the read-only
    process environment, used only to resolve an `env:` credential reference.
    Configuration and directory errors are raised to the caller."""
    err = stderr if stderr is not None else sys.stderr
    if not synthetic_only:
        return _refuse(err, "synthetic-only-required")

    profile_layer = config.load_profile(profile, env=env, home=home)
    managed = config.load_managed(env)
    project = None
    if project_root is not None:
        project = config.load_project(project_root)
        errors = config.check_cross_layer(profile_layer, project)
        if errors:
            raise ConfigErrors(list(errors))
    effective = merge.merge(config.LoadedConfig(profile_layer, project, managed))
    if project_root is not None and effective.project_state not in ("work", "personal"):
        return _refuse(err, "classification-required")

    name = model or effective.values["default_model"].value
    model_view = effective.models.get(name)
    if model_view is None:
        return _refuse(err, "unknown-model")
    view = effective.providers[model_view.provider]
    if view.id not in effective.effective_providers:
        return _refuse(err, "provider-excluded", effective.excluded_providers.get(view.id))
    if effective.classification == "work" and (
        effective.profile_classification == "personal" or view.kind == "openrouter"
    ):
        return _refuse(err, "classification-incompatible")
    if view.endpoint_family == "responses":
        return _refuse(err, "responses-unsupported")

    pinned = qualification.pinned_version()
    probe = probe_module if probe_module is not None else load_probe_module()
    record_path = paths.qualification_path(
        model_view.qualification_ref[len("local:"):], env, home
    )
    jsonio.ensure_private_directory(record_path.parent)

    try:
        secret = credentials.default_resolver(
            environ, platform=platform, runner=runner
        ).resolve(view.credential_ref)
    except credentials.SecretResolutionError as exc:
        err.write("quoin: %s\n" % exc)
        return 2

    probe_config = probe.ProbeConfig(
        base_url=view.base_url,
        model=model_view.model_id,
        credential_env=PROBE_ENV_NAME,
        provider=view.id,
        runtime_version=pinned,
        ca_file=view.ca_file,
        use_env_proxy=view.use_env_proxy,
        output=str(record_path),
    )

    # Any run that does not finish writing a new record must leave the model
    # unqualified, never still qualified by an older record.
    if os.path.lexists(str(record_path)):
        try:
            os.unlink(str(record_path))
        except OSError:
            err.write(RECORD_REMOVE_TEXT + "\n")
            return 2

    err.write(LIVE_NOTICE + "\n")
    probe_env = {PROBE_ENV_NAME: secret.reveal()}
    try:
        code = probe.execute(probe_config, probe_env, now=now)
    except Exception as exc:  # noqa: BLE001 - the message may embed the secret
        err.write(UNEXPECTED_TEXT % type(exc).__name__ + "\n")
        code = 2
    finally:
        probe_env.clear()
        del secret
    return code
