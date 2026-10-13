"""Paths, recorded binaries, zellij socket and state-store helpers for the deck.

Every payload script reads its environment through this module. It runs under
`python -E -s` with its own directory on sys.path, so it is stdlib only and has
no import-time side effects (no mkdir, no env mutation, no subprocess).

Resolution order is environment, then install manifest, then default. Paths
are made absolute with os.path.abspath and are never resolved through
symlinks, so the stable `current` symlink survives.

Install manifest (`<runtime>/install-manifest.json`), the contract the install
engine must write:
  paths.config, paths.state, paths.agentdesk
  binaries.{zellij,osascript,open,python,agentdesk}      core, string or {"path": ...}
  binaries.{claude,codex,ccr,lazygit,python3}            optional agent tools, recorded
                                                         when found on the installing shell's PATH
  zellij_socket_dir
"""
import errno
import json
import os
import re
import shutil
import sys
import tempfile
import time

PS_ENV = {"LC_ALL": "C", "PATH": "/usr/bin:/bin"}
LSTART_RE = r"^[A-Z][a-z]{2} [A-Z][a-z]{2} +\d{1,2} \d\d:\d\d:\d\d \d{4}$"
_LSTART = re.compile(LSTART_RE)
CORE_BINS = ("zellij", "osascript", "open", "python", "agentdesk")
TOOL_BINS = ("claude", "codex", "ccr", "lazygit", "python3")
DEFAULT_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
_API_RE = re.compile(r"^AGENTDESK_API_VERSION=(\d+)", re.M)


def ps_env():
    """The only environment any `ps` call in the payload may use."""
    return dict(PS_ENV)


def is_c_lstart(value):
    """True only for a string in the C-locale `ps -o lstart=` shape."""
    return isinstance(value, str) and bool(_LSTART.match(value.strip()))


class DeckEnv(object):
    __slots__ = (
        "runtime", "state_root", "config_path", "state_dir", "agents_dir",
        "legacy_agents_dir", "surfaces_path", "health_path", "errors_log",
        "monitor_lock", "legacy_monitor_lock", "controller_lock", "manifest",
        "socket_dir", "bins", "tools", "path_env", "terminal_entry", "agentdesk",
    )

    def zellij_env(self, socket=None):
        """A NEW environment for a zellij child; never touches os.environ."""
        env = dict(os.environ)
        env["PATH"] = self.path_env
        env["ZELLIJ_SOCKET_DIR"] = socket or self.socket_dir
        return env


def _nonempty(value):
    return isinstance(value, str) and value != ""


def _abs(path):
    return os.path.abspath(path)


def _read_manifest(path):
    try:
        with open(path, "r") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _bin_path(entry):
    if isinstance(entry, dict):
        entry = entry.get("path")
    return entry if _nonempty(entry) else None


def agentdesk_socket_dir(environ):
    """Agent Desk's rule: ${ZELLIJ_SOCKET_DIR:-${XDG_RUNTIME_DIR:-/tmp}/zellij-agentdesk-$UID}."""
    explicit = environ.get("ZELLIJ_SOCKET_DIR")
    if _nonempty(explicit):
        return explicit
    base = environ.get("XDG_RUNTIME_DIR")
    if not _nonempty(base):
        base = "/tmp"
    return "%s/zellij-agentdesk-%d" % (base, os.getuid())


def _agentdesk_ok(path):
    try:
        with open(path, "r") as handle:
            text = handle.read(1 << 20)
    except (OSError, ValueError):
        return False
    match = _API_RE.search(text)
    return bool(match) and int(match.group(1)) >= 1


def load(environ=None):
    env_in = os.environ if environ is None else environ
    home = env_in.get("HOME") or os.path.expanduser("~")
    runtime = env_in.get("QUOIN_DECK_RUNTIME")
    runtime = _abs(runtime) if _nonempty(runtime) else _abs(
        os.path.join(home, "Library", "Application Support", "QuoinStreamDeck"))
    manifest_path = os.path.join(runtime, "install-manifest.json")
    manifest = _read_manifest(manifest_path)
    paths = manifest.get("paths") if isinstance(manifest.get("paths"), dict) else {}

    root = env_in.get("QUOIN_DECK_STATE_ROOT")
    if not _nonempty(root):
        root = env_in.get("QUOIN_DECK_STATE")
    env_root = _abs(root) if _nonempty(root) else None
    if env_root:
        state_root = env_root
    elif _nonempty(paths.get("config")):
        state_root = _abs(os.path.dirname(paths["config"]))
    else:
        state_root = _abs(os.path.join(home, ".config", "agentdesk", "streamdeck"))
    if not env_root and _nonempty(paths.get("config")):
        config_path = _abs(paths["config"])
    else:
        config_path = os.path.join(state_root, "config.json")
    if not env_root and _nonempty(paths.get("state")):
        state_dir = _abs(paths["state"])
    else:
        state_dir = os.path.join(state_root, "state")

    recorded = manifest.get("binaries") if isinstance(manifest.get("binaries"), dict) else {}
    bins = {}
    recorded_core = []
    for name in CORE_BINS:
        value = _bin_path(recorded.get(name))
        if value:
            bins[name] = value
            recorded_core.append(value)
    if "zellij" not in bins:
        bins["zellij"] = shutil.which("zellij", path=env_in.get("PATH", DEFAULT_PATH)) or "zellij"
    bins.setdefault("osascript", "/usr/bin/osascript")
    bins.setdefault("open", "/usr/bin/open")
    bins.setdefault("python", sys.executable)
    tools = {}
    for name in TOOL_BINS:
        value = _bin_path(recorded.get(name))
        if value and os.path.isabs(value):
            tools[name] = value

    dirs = []
    for value in recorded_core + [tools[n] for n in TOOL_BINS if n in tools]:
        if os.path.isabs(value):
            dirs.append(os.path.dirname(value))
    dirs.extend(p for p in env_in.get("PATH", DEFAULT_PATH).split(":") if p)
    seen = set()
    ordered = []
    for entry in dirs:
        if entry not in seen:
            seen.add(entry)
            ordered.append(entry)

    sock = manifest.get("zellij_socket_dir")
    socket_dir = sock if _nonempty(sock) else agentdesk_socket_dir(env_in)

    candidate = _bin_path(recorded.get("agentdesk")) or (
        paths["agentdesk"] if _nonempty(paths.get("agentdesk")) else None)
    if not candidate:
        candidate = os.path.join(home, ".config", "agentdesk", "agentdesk.zsh")
    agentdesk = candidate if _agentdesk_ok(candidate) else None

    out = DeckEnv()
    out.runtime = runtime
    out.manifest = manifest_path
    out.state_root = state_root
    out.config_path = config_path
    out.state_dir = state_dir
    out.agents_dir = os.path.join(state_dir, "agents")
    out.legacy_agents_dir = os.path.join(state_root, "agents")
    out.surfaces_path = os.path.join(state_dir, "surfaces.json")
    out.health_path = os.path.join(state_dir, "health.json")
    out.errors_log = os.path.join(state_dir, "errors.log")
    out.monitor_lock = os.path.join(state_dir, "monitor.lock")
    out.legacy_monitor_lock = os.path.join(state_root, "monitor.lock")
    out.controller_lock = os.path.join(state_dir, "controller.lock")
    out.socket_dir = socket_dir
    out.bins = bins
    out.tools = tools
    out.path_env = ":".join(ordered)
    out.terminal_entry = os.path.join(runtime, "bin", "quoin-deck-terminal")
    out.agentdesk = agentdesk
    return out


_CURRENT = None


def current():
    global _CURRENT
    if _CURRENT is None:
        _CURRENT = load()
    return _CURRENT


def reset():
    global _CURRENT
    _CURRENT = None


def record_socket(record, env):
    value = record.get("socket") if isinstance(record, dict) else None
    return value if _nonempty(value) else env.socket_dir


# ---------------------------------------------------------------- state store

def ensure_dir(path):
    """Create missing directories top-down with 0700; existing ones keep their mode."""
    path = _abs(path)
    missing = []
    probe = path
    while not os.path.isdir(probe):
        missing.append(probe)
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    for directory in reversed(missing):
        try:
            os.mkdir(directory, 0o700)
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                continue
            raise
        os.chmod(directory, 0o700)


def write_json_atomic(path, obj, **dumps_kw):
    text = json.dumps(obj, **dumps_kw)
    parent = os.path.dirname(path) or "."
    fd, temp = tempfile.mkstemp(dir=parent, prefix="." + os.path.basename(path) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def read_json(path, default=None):
    try:
        with open(path, "r") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def _json_names(directory):
    try:
        return sorted(n for n in os.listdir(directory) if n.endswith(".json") and not n.startswith("."))
    except OSError:
        return []


def iter_records(env, include_shadowed=False):
    """Yield (path, record, layout). Unparsable files yield record None.

    Layout is `new` or `legacy`; a legacy file whose name also exists in the new
    layout is skipped, or yielded as `legacy-shadowed` when asked for.
    """
    new_names = set(_json_names(env.agents_dir))
    for name in sorted(new_names):
        path = os.path.join(env.agents_dir, name)
        data = read_json(path, None)
        yield path, data if isinstance(data, dict) else None, "new"
    for name in _json_names(env.legacy_agents_dir):
        shadowed = name in new_names
        if shadowed and not include_shadowed:
            continue
        path = os.path.join(env.legacy_agents_dir, name)
        data = read_json(path, None)
        yield path, data if isinstance(data, dict) else None, "legacy-shadowed" if shadowed else "legacy"


def surface_key(socket, session):
    return socket + "\t" + session


def _legacy_surfaces(env):
    cfg = read_json(env.config_path, {})
    surfaces = cfg.get("surfaces") if isinstance(cfg, dict) else None
    return surfaces if isinstance(surfaces, dict) else {}


def get_surface(env, socket, session):
    surfaces = read_json(env.surfaces_path, {})
    value = surfaces.get(surface_key(socket, session)) if isinstance(surfaces, dict) else None
    if _nonempty(value):
        return value
    if socket == env.socket_dir:
        value = _legacy_surfaces(env).get(session)
        if _nonempty(value):
            return value
    return None


def set_surface(env, socket, session, surface_id):
    surfaces = read_json(env.surfaces_path, {})
    if not isinstance(surfaces, dict):
        surfaces = {}
    surfaces[surface_key(socket, session)] = surface_id
    ensure_dir(env.state_dir)
    write_json_atomic(env.surfaces_path, surfaces)


def find_surface_owner(env, surface_id):
    owners = set()
    surfaces = read_json(env.surfaces_path, {})
    if isinstance(surfaces, dict):
        for key, value in surfaces.items():
            if value == surface_id and "\t" in key:
                socket, session = key.split("\t", 1)
                owners.add((socket, session))
    for session, value in _legacy_surfaces(env).items():
        if value == surface_id:
            owners.add((env.socket_dir, session))
    return next(iter(owners)) if len(owners) == 1 else None


def load_config(env):
    data = read_json(env.config_path, None)
    if not isinstance(data, dict):
        data = {}
    data.setdefault("project", os.path.expanduser("~"))
    data.setdefault("session", None)
    data.setdefault("projects", {})
    return data


def save_config(env, config):
    ensure_dir(os.path.dirname(env.config_path))
    write_json_atomic(env.config_path, config, ensure_ascii=False, indent=2)


def _clean(text):
    return "".join(" " if (ord(ch) < 32 or ord(ch) == 127) else ch for ch in text)


def _append_log(env, line):
    try:
        ensure_dir(env.state_dir)
        fd = os.open(env.errors_log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode("utf-8", "replace"))
        finally:
            os.close(fd)
    except Exception:
        pass


def _stamp():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def log_error(env, exc):
    """Type plus the first line of the message, at most 200 characters."""
    try:
        lines = str(exc).splitlines()
        message = _clean(lines[0] if lines else "")[:200]
        line = "%s %s: %s\n" % (_stamp(), type(exc).__name__, message) if message else \
            "%s %s\n" % (_stamp(), type(exc).__name__)
        _append_log(env, line)
    except Exception:
        pass


def log_error_type_only(env, exc):
    try:
        _append_log(env, "%s %s\n" % (_stamp(), type(exc).__name__))
    except Exception:
        pass
