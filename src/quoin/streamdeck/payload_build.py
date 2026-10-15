"""Deterministic Stream Deck plugin directory and importable profile (stdlib only)."""
import json
import os
import shlex
import shutil
import uuid
import zipfile

from quoin.streamdeck import PAYLOAD_DIR

PLUGIN_UUID = "local.quoin.agentdesk"
ACTION_UUID = PLUGIN_UUID + ".dashboard"
PLUGIN_NAME = "Quoin Agent Desk Live"
PLUGIN_VERSION = "2.0.0.0"
PROFILE_DIR = "3A28CB85-CAFE-426A-BC25-BD414402F62B.sdProfile"
PAGE_ID = "30ff3085-bb1e-4f54-9c82-0e373957a761"
PROFILE_NAME = "Quoin — Live Agents"
ACTION_NS = uuid.UUID("6f1c0a5e-8d3b-4c1e-9a52-3b7d2f41c8e0")
DEVICE_MODEL = "20GAA9901"
ROLES = tuple([{"role": "agent", "slot": i} for i in range(9)] +
              [{"role": "attention"}] + [{"role": r} for r in ("desk", "claude", "codex", "prev-tab", "next-tab")])
ICONS = ("icon.png", "icon@2x.png", "category.png")


def plugin_manifest(node_mode):
    if node_mode not in ("external", "bundled"):
        raise ValueError("node_mode must be 'external' or 'bundled'")
    manifest = {
        "Name": PLUGIN_NAME, "UUID": PLUGIN_UUID, "Author": "Quoin", "Version": PLUGIN_VERSION,
        "Description": "Live Claude Code and Codex status with exact Ghostty/Zellij routing",
        "Icon": "icon", "Category": "Quoin Agent Desk", "CategoryIcon": "category",
        "SDKVersion": 2, "OS": [{"Platform": "mac", "MinimumVersion": "13"}],
        "Actions": [{"UUID": ACTION_UUID, "Name": "Agent Desk Live", "Icon": "icon",
                     "Tooltip": "Live agent status or Agent Desk control", "UserTitleEnabled": False,
                     "States": [{"Image": "icon", "ShowTitle": False}], "SupportedInMultiActions": False,
                     "Controllers": ["Keypad"]}],
    }
    if node_mode == "external":
        manifest.update(CodePath="launch.sh", CodePathMac="launch.sh", Software={"MinimumVersion": "6.6"})
    else:
        manifest.update(CodePath="plugin.js", CodePathMac="plugin.js", Nodejs={"Version": "24"},
                        Software={"MinimumVersion": "7.1"})
    return manifest


def render_launch_sh(node_path):
    if not os.path.isabs(node_path):
        raise ValueError("node_path must be absolute")
    return '#!/bin/sh\ncd -- "$(dirname -- "$0")" || exit 1\nexec %s plugin.js "$@"\n' % shlex.quote(node_path)


def _under_versions(value, runtime):
    versions = os.path.normpath(os.path.join(runtime, "versions"))
    target = os.path.normpath(value)
    return target == versions or target.startswith(versions + os.sep)


def plugin_config(python, runtime, state_root):
    values = {"python": python, "runtime": runtime, "state_root": state_root}
    for key, value in values.items():
        if not isinstance(value, str) or not os.path.isabs(value):
            raise ValueError("%s must be an absolute path" % key)
        if key != "runtime" and _under_versions(value, runtime):
            raise ValueError("%s must not point into versions/" % key)
    return values


def build_plugin_dir(dest, *, node_mode, node_path, python, runtime, state_root):
    if not os.path.isabs(dest):
        raise ValueError("dest must be absolute")
    if os.path.exists(dest):
        raise ValueError("dest must not exist")
    if not os.path.isdir(os.path.dirname(dest)):
        raise ValueError("the parent of dest must exist")
    manifest = plugin_manifest(node_mode)
    config = plugin_config(python, runtime, state_root)
    launch = render_launch_sh(node_path) if node_mode == "external" else None
    os.mkdir(dest)
    with open(os.path.join(dest, "manifest.json"), "w") as handle:
        handle.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    shutil.copyfile(os.path.join(PAYLOAD_DIR, "plugin", "plugin.js"), os.path.join(dest, "plugin.js"))
    for name in ICONS:
        shutil.copyfile(os.path.join(PAYLOAD_DIR, "plugin", name), os.path.join(dest, name))
    with open(os.path.join(dest, "plugin-config.json"), "w") as handle:
        handle.write(json.dumps(config, indent=2, sort_keys=True) + "\n")
    if launch is not None:
        path = os.path.join(dest, "launch.sh")
        with open(path, "w") as handle:
            handle.write(launch)
        os.chmod(path, 0o755)


def _dump(obj):
    return (json.dumps(obj, sort_keys=True, ensure_ascii=False, indent=2)).encode("utf-8")


def profile_entries():
    actions = {}
    for i, settings in enumerate(ROLES):
        pos = "%d,%d" % (i % 5, i // 5)
        actions[pos] = {
            "ActionID": str(uuid.uuid5(ACTION_NS, "%s:%s" % (PAGE_ID, pos))), "LinkedTitle": False,
            "Name": "Agent Desk Live",
            "Plugin": {"Name": PLUGIN_NAME, "UUID": PLUGIN_UUID, "Version": PLUGIN_VERSION},
            "UUID": ACTION_UUID, "Settings": settings, "State": 0,
            "States": [{"ShowTitle": False, "Title": "", "Image": ""}],
        }
    root = {"Device": {"Model": DEVICE_MODEL, "UUID": ""}, "Name": PROFILE_NAME,
            "Pages": {"Current": PAGE_ID, "Default": PAGE_ID, "Pages": [PAGE_ID]}, "Version": "3.0"}
    page = {"Controllers": [{"Type": "Keypad", "Actions": actions}], "Name": "Live Agents", "Icon": ""}
    return [(PROFILE_DIR + "/manifest.json", _dump(root)),
            (PROFILE_DIR + "/Profiles/" + PAGE_ID.upper() + "/manifest.json", _dump(page))]


def build_profile(dest_file):
    temp = dest_file + ".tmp"
    with zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for arcname, data in profile_entries():
            info = zipfile.ZipInfo(arcname, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data, compresslevel=9)
    os.replace(temp, dest_file)


MAX_MANIFEST_BYTES = 1 << 20


def _read_json(path):
    try:
        if not os.path.isfile(path) or os.path.getsize(path) > MAX_MANIFEST_BYTES:
            return None
        with open(path, "rb") as handle:
            return json.loads(handle.read().decode("utf-8"))
    except (OSError, ValueError, RecursionError):
        return None


def _profile_dirs(app_dir):
    # ProfilesV2 is the legacy store that the app leaves behind once ProfilesV3 exists.
    for store in ("ProfilesV3", "ProfilesV2"):
        base = os.path.join(app_dir, store)
        try:
            names = sorted(n for n in os.listdir(base) if n.endswith(".sdProfile"))
        except OSError:
            continue
        yield [os.path.join(base, n) for n in names]
        if store == "ProfilesV3":
            return


def _has_configured_quoin_keys(root):
    try:
        pages = os.listdir(os.path.join(root, "Profiles"))
    except OSError:
        return False
    roles = set()
    for page in pages:
        data = _read_json(os.path.join(root, "Profiles", page, "manifest.json"))
        controllers = data.get("Controllers") if isinstance(data, dict) else None
        for controller in controllers if isinstance(controllers, list) else ():
            actions = controller.get("Actions") if isinstance(controller, dict) else None
            for action in actions.values() if isinstance(actions, dict) else ():
                if not isinstance(action, dict) or action.get("UUID") != ACTION_UUID:
                    continue
                settings = action.get("Settings")
                if isinstance(settings, dict) and settings.get("role"):
                    roles.add(settings["role"] if isinstance(settings["role"], str) else "")
    # A renamed import still carries the attention key next to agent keys; one hand-set key does not.
    return "attention" in roles and "agent" in roles


def profile_imported(plugins_dir):
    """Whether the Stream Deck app already holds the Quoin profile.

    The app keeps its profiles next to its plugins folder. A profile counts when it has
    our name, or when it is a renamed copy that still has our attention and agent keys.
    Unreadable or malformed files count as "not found", never as an error.
    """
    try:
        groups = list(_profile_dirs(os.path.dirname(plugins_dir)))
        for roots in groups:
            for root in roots:
                top = _read_json(os.path.join(root, "manifest.json"))
                if isinstance(top, dict) and top.get("Name") == PROFILE_NAME:
                    return True
        return any(_has_configured_quoin_keys(root) for roots in groups for root in roots)
    except Exception:
        return False
