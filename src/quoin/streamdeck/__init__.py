"""Optional Stream Deck component: payload scripts and the plugin/profile builder.

Stdlib only. The payload directory is never imported; its scripts run on their
own with their directory on sys.path.
"""
import hashlib
import os

COMPONENT_VERSION = "1.0.0"
PAYLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "payload")
RUNTIME_FILES = ("monitor.py", "dashboard.py", "controller.py", "deckenv.py", "terminal.zsh")
PLUGIN_FILES = ("plugin/plugin.js", "plugin/icon.png", "plugin/icon@2x.png", "plugin/category.png")
_SKIP_DIRS = ("__pycache__",)
_SKIP_NAMES = (".DS_Store",)


def payload_files(root=None):
    """Sorted POSIX relative paths of every shipped payload file."""
    root = root or PAYLOAD_DIR
    found = []
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in files:
            if name in _SKIP_NAMES or name.endswith(".pyc"):
                continue
            rel = os.path.relpath(os.path.join(base, name), root)
            found.append(rel.replace(os.sep, "/"))
    return sorted(found)


def payload_fingerprint(root=None):
    root = root or PAYLOAD_DIR
    digest = hashlib.sha256()
    for rel in payload_files(root):
        with open(os.path.join(root, rel), "rb") as handle:
            inner = hashlib.sha256(handle.read()).hexdigest()
        digest.update((rel + "\0" + inner + "\n").encode("utf-8"))
    return digest.hexdigest()
