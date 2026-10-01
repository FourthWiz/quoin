"""Launch-time enforcement for headless OpenCode runs.

Three responsibilities live here, none of which needs the driver itself:

* build the child environment from an allowlist, so no ambient provider key
  or ``OPENCODE_*`` override reaches the child and only the credentials the
  compiled configuration names are exported;
* verify the compiled configuration and the installed files before a launch;
* scan every configuration layer OpenCode reads, so a project, global or
  managed layer cannot override the compiled model, agents, providers or
  policy, and a markdown or JSON command cannot shadow an installed
  ``quoin-*`` one.

Secrets are held only inside ``LaunchEnv`` (whose repr shows names only) and
registered with a ``Redactor`` so any text derived from the child is scrubbed.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from . import compiler, doctor, errors, install, jsonio, paths
from . import secrets as credential_refs

MAX_OWNED_BYTES = install.MAX_OWNED_BYTES
MIN_REDACTED_LENGTH = 8

# The extra key the launcher refuses. It lives here rather than in the
# compiler's protected list because that list is written into the compile
# sidecar, and changing it would make every existing compiled pair stale.
REFUSED_KEYS: Tuple[str, ...] = ("experimental.continue_loop_on_deny",)

_ALLOWED_ENV = frozenset(
    (
        "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TERM", "TZ", "TMPDIR",
        "XDG_CONFIG_HOME", "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
    )
)
_PROXY_ENV = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY")
_AGENT_SUBDIRS = ("agent", "agents", "mode", "modes")
_PLUGIN_SUBDIRS = ("plugin", "plugins")
_COMMAND_SUBDIRS = ("command", "commands")
_OWNED_PREFIX = "quoin-"
_REDACTED = "<redacted>"

_MANAGED_DIR_DARWIN = "/Library/Application Support/opencode"
_MANAGED_DIR_OTHER = "/etc/opencode"
_PLIST_NAME = "ai.opencode.managed.plist"


class LaunchRefused(Exception):
    """A launch was refused before spawn. ``category`` is one of the driver's
    refusal categories (passed in by the raise site, so this module does not
    import the driver); ``code`` is a stable machine identifier."""

    def __init__(self, category: str, code: str, message: str) -> None:
        super().__init__(message)
        self.category = category
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return self.message


# ------------------------------------------------------------- redaction


class Redactor:
    """Replaces every registered secret value and every well-known secret
    shape in a piece of text. Never raises."""

    def __init__(self) -> None:
        self._values: List[str] = []

    def add(self, value: Any) -> None:
        # Shorter values would redact ordinary text and are not realistic keys.
        if isinstance(value, str) and len(value) >= MIN_REDACTED_LENGTH and value not in self._values:
            self._values.append(value)
            self._values.sort(key=len, reverse=True)

    def __call__(self, text: Any) -> str:
        try:
            out = text if isinstance(text, str) else str(text)
            for value in self._values:
                out = out.replace(value, _REDACTED)
            return errors.SECRET_SHAPE_RE.sub(_REDACTED, out)
        except Exception:  # noqa: BLE001 - redaction must never raise
            return _REDACTED

    def __repr__(self) -> str:
        return "Redactor(values=%d)" % len(self._values)


# ---------------------------------------------------------- environment


class LaunchEnv:
    """The child environment. Values stay in a private field; the repr lists
    names only, and ``materialize`` is meant for the spawn call alone."""

    __slots__ = ("_values", "redactor")

    def __init__(self, values: Mapping[str, str], redactor: Optional[Redactor] = None) -> None:
        self._values = dict(values)
        self.redactor = redactor if redactor is not None else Redactor()

    def names(self) -> List[str]:
        return sorted(self._values)

    def materialize(self) -> Dict[str, str]:
        return dict(self._values)

    def __repr__(self) -> str:
        return "LaunchEnv(names=%s)" % (self.names(),)

    def __reduce__(self) -> Any:
        raise TypeError("a launch environment cannot be pickled or copied")


def _allowed_ambient(name: str) -> bool:
    return name in _ALLOWED_ENV or name.startswith("LC_")


def _providers_by_id(providers: Union[Mapping[str, Any], Iterable[Any]]) -> Dict[str, Any]:
    if isinstance(providers, Mapping):
        return dict(providers)
    return {view.id: view for view in providers}


def _register_proxy_secret(redactor: Redactor, value: str) -> None:
    """Proxy URLs may carry ``user:pass@``; keep the password out of logs."""
    if "@" not in value:
        return
    # The whole value is registered too: short or oddly delimited credentials
    # (an unencoded ``/`` in the password) escape the structured parse.
    redactor.add(value)
    try:
        parts = urlsplit(value)
        userinfo = parts.netloc.rsplit("@", 1)[0] if "@" in parts.netloc else ""
        username, password = parts.username, parts.password
    except ValueError:
        userinfo, username, password = "", None, None
    # An unencoded ``/`` in the password ends the authority early, so also cut
    # at the last ``@`` of the raw text.
    raw = value.split("://", 1)[-1].rsplit("@", 1)[0]
    redactor.add(raw)
    if ":" in raw:
        redactor.add(raw.split(":", 1)[1])
    if userinfo:
        redactor.add(userinfo)
    if username:
        redactor.add(username)
    if password:
        redactor.add(password)


def build_env(
    *,
    ambient: Mapping[str, str],
    compile_sidecar: Mapping[str, Any],
    providers: Union[Mapping[str, Any], Iterable[Any]],
    resolver: Any,
    data_dir: Union[str, Path],
    config_path: Union[str, Path],
    redactor: Optional[Redactor] = None,
) -> LaunchEnv:
    """Allowlist the ambient environment, then add the launcher's own
    variables and the resolved credentials the compiled file names."""
    redactor = redactor if redactor is not None else Redactor()
    by_id = _providers_by_id(providers)
    credential_env = compile_sidecar.get("credential_env") or {}
    emitted = [by_id[pid] for pid in credential_env.values() if pid in by_id]

    values: Dict[str, str] = {}
    for name, value in ambient.items():
        if _allowed_ambient(name) and isinstance(value, str):
            values[name] = value
    if any(getattr(view, "use_env_proxy", False) for view in emitted):
        for base in _PROXY_ENV:
            for name in (base, base.lower()):
                if name in ambient and isinstance(ambient[name], str):
                    values[name] = ambient[name]
                    _register_proxy_secret(redactor, ambient[name])

    values["OPENCODE_CONFIG"] = str(config_path)
    values["XDG_DATA_HOME"] = str(data_dir)

    for env_name in sorted(credential_env):
        provider_id = credential_env[env_name]
        view = by_id.get(provider_id)
        ref = getattr(view, "credential_ref", "") if view is not None else ""
        if not ref:
            raise _credential_refusal(env_name)
        try:
            secret = resolver.resolve(ref)
            value = secret.reveal()
        except (credential_refs.SecretResolutionError, ValueError):
            raise _credential_refusal(env_name) from None
        if not value:
            raise _credential_refusal(env_name)
        values[env_name] = value
        redactor.add(value)
    return LaunchEnv(values, redactor)


def _credential_refusal(env_name: str) -> LaunchRefused:
    return LaunchRefused(
        "invalid-configuration",
        "credential-unresolved",
        "the credential for %s could not be resolved to a non-empty value" % env_name,
    )


def data_dir(env: Mapping[str, str], home: Union[str, Path], profile: str) -> Path:
    """The private data directory the launcher isolates and reuses across
    attempts of the same profile."""
    target = paths.state_dir(env, Path(home)) / profile / "data"
    try:
        jsonio.ensure_private_directory(target)
    except (jsonio.UnsafeDirectoryError, OSError) as exc:
        raise LaunchRefused(
            "invalid-configuration",
            "data-dir-unsafe",
            "the data directory %s cannot be used privately: %s" % (target, exc),
        ) from exc
    return target


# --------------------------------------------------- file verification


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify_owned_file(project_root: Union[str, Path], rel_path: str, expected_sha256: str) -> bool:
    """True when the project-relative file is a regular, non-symlink file
    whose bytes hash to ``expected_sha256``. Any read failure is a mismatch.
    The one comparison used both by the pre-launch install check and by the
    layer scan, so their rules cannot drift apart."""
    got = jsonio.read_regular_bytes(Path(project_root) / rel_path, max_bytes=MAX_OWNED_BYTES)
    if got is None:
        return False
    return _sha256(got[0]) == expected_sha256


def verify_compiled(config_path: Union[str, Path], expected_sha256: str) -> None:
    got = jsonio.read_regular_bytes(config_path, max_bytes=MAX_OWNED_BYTES)
    if got is None or _sha256(got[0]) != expected_sha256:
        raise LaunchRefused(
            "invalid-configuration",
            "compiled-file-changed",
            "the compiled configuration file changed or cannot be read; compile it again",
        )


# ---------------------------------------------------------- layer scan


def _refuse(category: str, code: str, message: str) -> LaunchRefused:
    return LaunchRefused(category, code, message)


def _default_plutil(path: Path) -> str:
    proc = subprocess.run(
        ["plutil", "-convert", "json", "-o", "-", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=10,
        check=False,
    )
    if proc.returncode != 0:
        raise OSError("plutil failed")
    return proc.stdout.decode("utf-8")


def _same_path(a: Union[str, Path], b: Union[str, Path]) -> bool:
    return os.path.realpath(str(a)) == os.path.realpath(str(b))


def _dotted_present(doc: Mapping[str, Any], dotted: str) -> bool:
    node: Any = doc
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def _check_layer(
    doc: Mapping[str, Any],
    shown: str,
    below_compiled: bool,
    compiled_doc: Mapping[str, Any],
) -> None:
    for key in REFUSED_KEYS:
        if _dotted_present(doc, key):
            raise _refuse(
                "invalid-configuration",
                "continue-loop-on-deny",
                "%s sets %s, which would keep a run going after a denial" % (shown, key),
            )
    commands = doc.get("command")
    if isinstance(commands, dict):
        for name in commands:
            if str(name).startswith(_OWNED_PREFIX):
                raise _refuse(
                    "policy-denial",
                    "command-key-overridden",
                    "%s defines command.%s, which would alter an installed command" % (shown, name),
                )
    # OpenCode folds every legacy ``mode.NAME`` into ``agent.NAME`` after all
    # layers merge, so the fold applies at every rank, the global one included.
    modes = doc.get("mode")
    if isinstance(modes, dict):
        compiled_agents = compiled_doc.get("agent")
        compiled_agent_names = compiled_agents if isinstance(compiled_agents, dict) else {}
        for name in modes:
            if name in compiled_agent_names or str(name).startswith(_OWNED_PREFIX):
                raise _refuse(
                    "policy-denial",
                    "protected-key-overridden",
                    "%s overrides mode.%s, which the compiled configuration controls" % (shown, name),
                )
    elif "mode" in doc and compiled_doc.get("agent"):
        raise _refuse(
            "policy-denial",
            "protected-key-overridden",
            "%s sets mode to a non-object value" % shown,
        )

    def overridden(dotted: str) -> LaunchRefused:
        return _refuse(
            "policy-denial",
            "protected-key-overridden",
            "%s overrides %s, which the compiled configuration controls" % (shown, dotted),
        )

    # OpenCode concatenates ``plugin`` arrays across layers, so a plugin from
    # any layer (the global one included) can hook permission requests.
    if _dotted_present(doc, "plugin"):
        raise overridden("plugin")
    if below_compiled:
        return

    if "$schema" in doc and doc["$schema"] != compiler.CONFIG_SCHEMA_URL:
        raise _refuse(
            "policy-denial",
            "protected-key-overridden",
            "%s sets $schema to a value other than the expected %s"
            % (shown, compiler.CONFIG_SCHEMA_URL),
        )
    for dotted in (
        "model",
        "small_model",
        "share",
        "autoupdate",
        "enabled_providers",
        "experimental.policies",
        "permission",
        "tools",
    ):
        if _dotted_present(doc, dotted):
            raise overridden(dotted)
    for key in ("agent", "provider"):
        if key not in doc:
            continue
        section = doc[key]
        if not isinstance(section, dict):
            raise overridden(key)
        compiled_section = compiled_doc.get(key)
        compiled_names = compiled_section if isinstance(compiled_section, dict) else {}
        for name in section:
            if name in compiled_names:
                raise overridden("%s.%s" % (key, name))


def _markdown_files(base: Path, subdirs: Sequence[str], display: Callable[[Path], str]):
    """Yield ``(path, name)`` for every ``*.md`` under ``base/<subdir>`` whose
    path-derived name (or last segment) starts with ``quoin-``."""

    def onerror(exc: OSError) -> None:
        raise _refuse(
            "invalid-configuration",
            "config-layer-unreadable",
            "%s could not be read" % display(Path(getattr(exc, "filename", None) or base)),
        )

    for sub in subdirs:
        directory = base / sub
        if not os.path.isdir(str(directory)):
            continue
        for current, dirnames, filenames in os.walk(str(directory), onerror=onerror):
            dirnames.sort()
            for filename in sorted(filenames):
                if not filename.endswith(".md"):
                    continue
                path = Path(current) / filename
                name = path.relative_to(directory).as_posix()[: -len(".md")]
                if name.startswith(_OWNED_PREFIX) or name.rsplit("/", 1)[-1].startswith(_OWNED_PREFIX):
                    yield path, name


def _project_relative(path: Path, cwd: Path) -> Optional[str]:
    rel = os.path.relpath(os.path.abspath(str(path)), os.path.abspath(str(cwd)))
    if rel == ".." or rel.startswith(".." + os.sep):
        return None
    return Path(rel).as_posix()


def _managed_defaults(env: Mapping[str, str]) -> Tuple[Path, List[Path]]:
    if sys.platform == "darwin":
        user = env.get("USER") or env.get("LOGNAME") or ""
        prefs = [Path("/Library/Managed Preferences") / _PLIST_NAME]
        if user:
            prefs.insert(0, Path("/Library/Managed Preferences") / user / _PLIST_NAME)
        return Path(_MANAGED_DIR_DARWIN), prefs
    return Path(_MANAGED_DIR_OTHER), []


def check_config_layers(
    *,
    cwd: Union[str, Path],
    env: Mapping[str, str],
    home: Union[str, Path],
    compiled_doc: Mapping[str, Any],
    owned_agents: Mapping[str, str],
    owned_commands: Mapping[str, str],
    managed_dir: Optional[Union[str, Path]] = None,
    managed_prefs: Optional[Sequence[Union[str, Path]]] = None,
    plutil: Optional[Callable[[Path], str]] = None,
) -> None:
    """Refuse the launch when any configuration layer OpenCode reads would
    override the compiled file or shadow an installed ``quoin-*`` unit.

    ``env`` is the child environment (``OPENCODE_CONFIG`` names the compiled
    file, which is excluded from the scan). ``owned_agents`` and
    ``owned_commands`` map project-relative paths to the install record's
    sha256. When the project is not inside a git worktree OpenCode walks
    every ancestor up to the filesystem root, and so does this scan.
    """
    cwd = Path(cwd)
    home = Path(home)
    worktree = doctor._worktree_root(cwd)  # noqa: SLF001 - shares the doctor's discovery walk
    roots = doctor._build_roots(cwd, env, home)  # noqa: SLF001

    def shown(path: Path) -> str:
        return doctor.display_path(path, roots)

    docs, findings = doctor._read_config_files(cwd, worktree, env, home, roots)  # noqa: SLF001
    for finding in findings:
        if finding.id == "config-unreadable":
            raise _refuse(
                "invalid-configuration",
                "config-layer-unreadable",
                "%s could not be read as configuration" % (finding.path or "a configuration file"),
            )

    compiled_file = env.get("OPENCODE_CONFIG")
    global_dir = doctor._xdg_config_dir(env, home)  # noqa: SLF001
    for path, doc in docs:
        if compiled_file and _same_path(path, compiled_file):
            continue
        below = _same_path(path.parent, global_dir)
        _check_layer(doc, shown(path), below, compiled_doc)

    managed_root, default_prefs = _managed_defaults(env)
    managed_root = Path(managed_dir) if managed_dir is not None else managed_root
    prefs = [Path(p) for p in managed_prefs] if managed_prefs is not None else default_prefs
    for name in ("opencode.json", "opencode.jsonc"):
        path = managed_root / name
        try:
            payload = doctor._load_config_file(path)  # noqa: SLF001
        except doctor._ConfigUnreadable:  # noqa: SLF001
            raise _refuse(
                "invalid-configuration",
                "config-layer-unreadable",
                "the managed configuration file %s could not be read" % path,
            ) from None
        if payload is not None:
            _check_layer(payload, "managed configuration %s" % path, False, compiled_doc)
    convert = plutil if plutil is not None else _default_plutil
    for plist in prefs:
        if not os.path.lexists(str(plist)):
            continue
        try:
            payload = json.loads(convert(plist))
        except Exception:  # noqa: BLE001 - any failure to convert is a refusal
            payload = None
        if not isinstance(payload, dict):
            raise _refuse(
                "invalid-configuration",
                "config-layer-unreadable",
                "the managed preferences file %s could not be converted" % plist,
            )
        _check_layer(payload, "managed preferences %s" % plist, False, compiled_doc)

    _check_markdown(cwd, env, home, worktree, global_dir, managed_root, owned_agents, owned_commands, shown)


def _config_bases(cwd: Path, home: Path, worktree: Path, global_dir: Path) -> List[Path]:
    """Every ``.opencode`` style base directory OpenCode scans for markdown
    and plugin files, in scan order and without duplicates."""
    bases: List[Path] = [d / ".opencode" for d in doctor._project_config_dirs(worktree, cwd)]  # noqa: SLF001
    home_opencode = home / ".opencode"
    if os.path.isdir(str(home_opencode)):
        bases.append(home_opencode)
    bases.append(global_dir)
    seen = set()
    unique: List[Path] = []
    for base in bases:
        key = os.path.abspath(str(base))
        if key not in seen:
            seen.add(key)
            unique.append(base)
    return unique


def _plugin_entries(bases: Sequence[Path]) -> List[Tuple[Path, str]]:
    """Plugin directories that need attention, in the order the launch check
    visits them: ``populated`` when one holds a top-level ``.ts`` or ``.js``
    file that is not a directory, ``unreadable`` when listing it failed."""
    found: List[Tuple[Path, str]] = []
    for base in bases:
        for sub in _PLUGIN_SUBDIRS:
            directory = base / sub
            try:
                populated = os.path.isdir(str(directory)) and any(
                    entry.name.endswith((".ts", ".js")) and not entry.is_dir(follow_symlinks=False)
                    for entry in os.scandir(str(directory))
                )
            except OSError:
                found.append((directory, "unreadable"))
                continue
            if populated:
                found.append((directory, "populated"))
    return found


def plugin_directories(
    cwd: Path, env: Mapping[str, str], home: Path, managed_root: Path
) -> List[Tuple[Path, str]]:
    """Ordered ``(path, "populated" | "unreadable")`` plugin directory
    entries for a project, exactly as the launch check visits them. Shared
    with the doctor so both report the same directory first."""
    cwd, home = Path(cwd), Path(home)
    worktree = doctor._worktree_root(cwd)  # noqa: SLF001
    global_dir = doctor._xdg_config_dir(env, home)  # noqa: SLF001
    return _plugin_entries(_config_bases(cwd, home, worktree, global_dir) + [Path(managed_root)])


def _check_markdown(
    cwd: Path,
    env: Mapping[str, str],
    home: Path,
    worktree: Path,
    global_dir: Path,
    managed_root: Path,
    owned_agents: Mapping[str, str],
    owned_commands: Mapping[str, str],
    shown: Callable[[Path], str],
) -> None:
    unique = _config_bases(cwd, home, worktree, global_dir)

    def drift(path: Path) -> LaunchRefused:
        return _refuse(
            "workflow-validation",
            "owned-file-drift",
            "%s differs from the installed copy; reinstall or restore it" % shown(path),
        )

    # Plugin files under a config directory load automatically and can hook
    # permission requests, so any of them defeats the approval contract.
    for directory, state in _plugin_entries(unique + [managed_root]):
        if state == "unreadable":
            raise _refuse(
                "invalid-configuration",
                "config-layer-unreadable",
                "%s could not be read" % shown(directory),
            )
        raise _refuse(
            "policy-denial",
            "plugin-directory-present",
            "%s holds plugin files, which could loosen permission handling" % shown(directory),
        )

    for base in unique:
        for path, _name in _markdown_files(base, _AGENT_SUBDIRS, shown):
            rel = _project_relative(path, cwd)
            expected = owned_agents.get(rel) if rel is not None else None
            if expected is None:
                raise _refuse(
                    "policy-denial",
                    "agent-file-overridden",
                    "%s defines a quoin agent or mode that is not part of the install" % shown(path),
                )
            if not verify_owned_file(cwd, rel, expected):
                raise drift(path)
    for base in unique + [managed_root]:
        for path, _name in _markdown_files(base, _COMMAND_SUBDIRS, shown):
            rel = _project_relative(path, cwd)
            expected = owned_commands.get(rel) if rel is not None else None
            if expected is None:
                raise _refuse(
                    "policy-denial",
                    "command-file-overridden",
                    "%s defines a quoin command that is not part of the install" % shown(path),
                )
            if not verify_owned_file(cwd, rel, expected):
                raise drift(path)
