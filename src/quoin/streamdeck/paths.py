"""Resolved filesystem locations for the Stream Deck component.

Precedence per field: flag, then environment, then the HOME default. Paths are
made absolute with os.path.abspath and are never resolved through symlinks, so
the stable `current` link and a runtime directory containing a space survive.
The installer-side env names are distinct from the payload's QUOIN_DECK_*
variables, which the deployed runtime reads.
"""
import os
from dataclasses import dataclass

PLUGIN_DIR_NAME = "local.quoin.agentdesk.sdPlugin"
ENV_RUNTIME = "QUOIN_STREAMDECK_RUNTIME_DIR"
ENV_PLUGINS = "QUOIN_STREAMDECK_PLUGINS_DIR"
ENV_STATE_ROOT = "QUOIN_STREAMDECK_STATE_ROOT"
ENV_CLAUDE_DIR = "CLAUDE_CONFIG_DIR"
ENV_CODEX_HOME = "CODEX_HOME"
LEGACY_ROOT_SCRIPTS = ("monitor.py", "dashboard.py", "controller.py", "terminal.zsh")


@dataclass(frozen=True)
class Paths:
    home: str
    runtime: str
    plugins_dir: str
    plugin_dir: str
    state_root: str
    config: str
    state: str
    claude_settings: str
    codex_home: str
    codex_hooks: str
    codex_config: str
    agentdesk: str

    @property
    def manifest(self):
        return os.path.join(self.runtime, "install-manifest.json")

    @property
    def operation_marker(self):
        return os.path.join(self.runtime, "operation.json")

    @property
    def lock(self):
        return os.path.join(self.runtime, ".install.lock")

    @property
    def uninstalled(self):
        return os.path.join(self.runtime, "uninstalled.json")

    @property
    def versions_dir(self):
        return os.path.join(self.runtime, "versions")

    @property
    def current(self):
        return os.path.join(self.runtime, "current")

    @property
    def bin_dir(self):
        return os.path.join(self.runtime, "bin")

    @property
    def hook_shim(self):
        return os.path.join(self.bin_dir, "quoin-deck-hook")

    @property
    def terminal_entry(self):
        return os.path.join(self.bin_dir, "quoin-deck-terminal")

    @property
    def profile_dir(self):
        return os.path.join(self.runtime, "profile")

    @property
    def profile(self):
        return os.path.join(self.profile_dir, "Quoin Live Agents.streamDeckProfile")

    @property
    def staging(self):
        return os.path.join(self.runtime, "staging")

    @property
    def backups(self):
        return os.path.join(self.runtime, "backups")

    @property
    def plugin_config(self):
        return os.path.join(self.plugin_dir, "plugin-config.json")

    @property
    def health(self):
        return os.path.join(self.state, "health.json")

    @property
    def agents_dir(self):
        return os.path.join(self.state, "agents")

    @property
    def legacy_root_scripts(self):
        return tuple(os.path.join(self.runtime, name) for name in LEGACY_ROOT_SCRIPTS)

    def manifest_paths(self):
        return {
            "runtime": self.runtime,
            "plugin_dir": self.plugin_dir,
            "config": self.config,
            "state": self.state,
            "claude_settings": self.claude_settings,
            "codex_hooks": self.codex_hooks,
            "agentdesk": self.agentdesk,
        }


def _clean(value):
    return value if isinstance(value, str) and value != "" else None


def resolve(flags=None, environ=None):
    flags = flags or {}
    environ = os.environ if environ is None else environ
    home = _clean(environ.get("HOME"))
    if home is None:
        import pwd
        home = pwd.getpwuid(os.getuid()).pw_dir

    def absolute(value):
        if value == "~" or value.startswith("~/"):
            value = home + value[1:]
        return os.path.abspath(value)

    def pick(flag, env_name, default):
        value = _clean(flags.get(flag))
        if value is None and env_name:
            value = _clean(environ.get(env_name))
        return absolute(value if value is not None else default)

    app_support = os.path.join(home, "Library", "Application Support")
    runtime = pick("runtime_dir", ENV_RUNTIME, os.path.join(app_support, "QuoinStreamDeck"))
    plugins = pick("plugins_dir", ENV_PLUGINS, os.path.join(app_support, "com.elgato.StreamDeck", "Plugins"))
    state_root = pick("state_root", ENV_STATE_ROOT, os.path.join(home, ".config", "agentdesk", "streamdeck"))
    claude = _clean(flags.get("claude_settings"))
    if claude is None:
        claude_dir = _clean(environ.get(ENV_CLAUDE_DIR))
        claude = os.path.join(claude_dir, "settings.json") if claude_dir else os.path.join(home, ".claude", "settings.json")
    codex_home = pick("codex_home", ENV_CODEX_HOME, os.path.join(home, ".codex"))
    agentdesk = pick("agentdesk", None, os.path.join(home, ".config", "agentdesk", "agentdesk.zsh"))
    return Paths(
        home=absolute(home),
        runtime=runtime,
        plugins_dir=plugins,
        plugin_dir=os.path.join(plugins, PLUGIN_DIR_NAME),
        state_root=state_root,
        config=os.path.join(state_root, "config.json"),
        state=os.path.join(state_root, "state"),
        claude_settings=absolute(claude),
        codex_home=codex_home,
        codex_hooks=os.path.join(codex_home, "hooks.json"),
        codex_config=os.path.join(codex_home, "config.toml"),
        agentdesk=agentdesk,
    )
