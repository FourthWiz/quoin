"""Install-time questions for ``quoin install``.

Questions are asked only when both stdin and stdout are terminals and
``QUOIN_INSTALL_NO_PROMPT`` is unset. Everything here is stdlib only and takes
filesystem locations as parameters; the CLI owns where things live.
"""
from __future__ import annotations

import os
import pathlib
import sys

NO_PROMPT_ENV = "QUOIN_INSTALL_NO_PROMPT"

MOD_COMMANDS = {"workflow-tasks": "/quoin-tasks"}


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


