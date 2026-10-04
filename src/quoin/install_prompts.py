"""Install-time questions for ``quoin install``.

Questions are asked only when both stdin and stdout are terminals and
``QUOIN_INSTALL_NO_PROMPT`` is unset. Everything here is stdlib only and takes
filesystem locations as parameters; the CLI owns where things live.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys

NO_PROMPT_ENV = "QUOIN_INSTALL_NO_PROMPT"

MOD_COMMANDS = {"context-tracker": "/ctx", "workflow-tasks": "/quoin-tasks"}

AGENTDESK_QUESTION = (
    "Run the agentdesk setup now? It installs Homebrew if missing (may ask for "
    "your password); installs zellij, lazygit and fzf and the Ghostty app with "
    "brew; writes the agentdesk helper, the zellij layout and zellij config.kdl "
    "(backups kept); and adds lines to ~/.zshrc (backup kept). The script may "
    "ask which clipboard tool to use."
)

# Must match SOURCE_LINE in tools/agentdesk/setup-agentdesk.sh (a test pins it).
AGENTDESK_SOURCE_LINE = (
    '[ -f "$HOME/.config/agentdesk/agentdesk.zsh" ] && '
    'source "$HOME/.config/agentdesk/agentdesk.zsh"'
)


def is_interactive() -> bool:
    """True only when both stdin and stdout are terminals and prompting is allowed."""
    if os.environ.get(NO_PROMPT_ENV) == "1":
        return False
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except Exception:
        return False


def ask_yes_no(question: str, *, default: bool) -> bool:
    """Ask a yes/no question on stdout; Enter picks ``default``.

    EOF or Ctrl-C aborts the whole install with exit status 1.
    """
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        print(f"{question} {suffix} ", end="", flush=True)
        try:
            answer = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.", file=sys.stderr)
            sys.exit(1)
        if answer == "":
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("Please answer y or n.")


def mod_question(name: str, folder: pathlib.Path) -> str:
    command = MOD_COMMANDS.get(name, "/" + name)
    return (
        f"Install the {name} mod ({command} pane) into {folder}? "
        f"Answer n to skip; --with-{name} does the same without asking."
    )


def agentdesk_already_set_up(zshrc: pathlib.Path) -> bool:
    try:
        return AGENTDESK_SOURCE_LINE in zshrc.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False


def run_agentdesk_setup(script: pathlib.Path) -> int:
    """Run the setup script with inherited stdio; never raises."""
    try:
        return subprocess.run(["bash", str(script)], check=False).returncode
    except KeyboardInterrupt:
        return 130
    except OSError:
        return 127
