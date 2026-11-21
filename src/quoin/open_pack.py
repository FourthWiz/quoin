"""The open-model pack: run Claude Code on an open model through a gateway.

`quoin open setup` copies the pack (a plan-first skill, a review skill and the
gateway probe) to ~/.local/share/quoin/open-pack and writes a launch profile to
~/.config/quoin/open.json. `quoin open` then starts `claude` with that profile
as plain environment variables (the gateway's Anthropic-compatible endpoint and
the tier-alias variables, so `model: sonnet` in a skill resolves to the model
you chose) and loads the pack with `--plugin-dir`, so it exists only in those
sessions. Nothing is written to Claude Code's own settings or plugin list, and
the default quoin plugin stays as it is; `quoin open remove` deletes the two
paths setup wrote.

The context-budget guard is part of the profile: Claude Code does not know an
open model's window, so the profile sets its auto-compact window to the
model's context and compacts at 80% of it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

DEFAULT_PROFILE = {
    "base_url": "https://openrouter.ai/api",
    "key_env": "OPENROUTER_API_KEY",
    "models": {
        "opus": "qwen/qwen3-coder",
        "sonnet": "qwen/qwen3-coder",
        "haiku": "qwen/qwen3-coder-flash",
        "subagent": "qwen/qwen3-coder",
    },
    "context_window": 262144,
    "autocompact_pct": 80,
}


def config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "quoin"


def profile_path() -> Path:
    return config_dir() / "open.json"


def pack_dir() -> Path:
    raw = os.environ.get("QUOIN_OPEN_PACK_DIR")
    if raw:
        return Path(raw).expanduser()
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "quoin" / "open-pack"


def pack_source(source_dir: Path) -> Path:
    return source_dir / "packs" / "open"


def setup(source_dir: Path, *, overwrite_profile: bool = False) -> dict:
    """Copy the pack and write the default profile (an existing profile is kept)."""
    src = pack_source(source_dir)
    if not (src / ".claude-plugin" / "plugin.json").is_file():
        raise FileNotFoundError(f"open-model pack not found under {src}")
    dest = pack_dir()
    tmp = dest.with_name(dest.name + ".new")
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(src, tmp, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
    shutil.rmtree(dest, ignore_errors=True)
    tmp.rename(dest)
    path = profile_path()
    wrote = False
    if overwrite_profile or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(DEFAULT_PROFILE, indent=2) + "\n", encoding="utf-8")
        wrote = True
    return {"pack": str(dest), "profile": str(path), "profile_written": wrote}


def remove() -> dict:
    removed = []
    if pack_dir().exists():
        shutil.rmtree(pack_dir())
        removed.append(str(pack_dir()))
    if profile_path().exists():
        profile_path().unlink()
        removed.append(str(profile_path()))
    return {"removed": removed}


def load_profile() -> dict:
    return json.loads(profile_path().read_text(encoding="utf-8"))


def launch_env(profile: dict, base_env: Optional[dict] = None) -> dict:
    """The environment a `claude` session needs to run on the profile's models."""
    env = dict(os.environ if base_env is None else base_env)
    key = env.get(profile.get("key_env", "OPENROUTER_API_KEY"), "")
    if not key:
        raise KeyError(f"{profile.get('key_env')} is not set")
    models = profile["models"]
    env.update({
        "ANTHROPIC_BASE_URL": profile["base_url"],
        "ANTHROPIC_AUTH_TOKEN": key,
        "ANTHROPIC_API_KEY": "",
        "ANTHROPIC_DEFAULT_OPUS_MODEL": models["opus"],
        "ANTHROPIC_DEFAULT_SONNET_MODEL": models["sonnet"],
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": models["haiku"],
        "CLAUDE_CODE_SUBAGENT_MODEL": models.get("subagent", models["sonnet"]),
        "QUOIN_OPEN_MODEL": models["sonnet"],
    })
    if profile.get("context_window"):
        env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(profile["context_window"])
        env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] = str(profile.get("autocompact_pct", 80))
    return env


def launch_argv(claude_args: list[str]) -> list[str]:
    argv = ["claude", "--plugin-dir", str(pack_dir())]
    if "--model" not in claude_args:
        argv += ["--model", "sonnet"]
    return argv + claude_args


def launch(claude_args: list[str], *, runner=subprocess.call) -> int:
    if not profile_path().exists() or not pack_dir().exists():
        print("quoin open: not set up; run `quoin open setup` first", file=sys.stderr)
        return 2
    try:
        env = launch_env(load_profile())
    except (KeyError, ValueError, OSError) as exc:
        print(f"quoin open: {exc}", file=sys.stderr)
        return 2
    return runner(launch_argv(claude_args), env=env)


def probe(*, runner=subprocess.call) -> int:
    script = pack_dir() / "scripts" / "probe_gateway.py"
    if not script.is_file():
        print("quoin open: not set up; run `quoin open setup` first", file=sys.stderr)
        return 2
    return runner([sys.executable, str(script), "--profile", str(profile_path())])
