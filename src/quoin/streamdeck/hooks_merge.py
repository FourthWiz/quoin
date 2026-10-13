"""Pure merge/remove/inspect of the component's hook entries in a settings dict.

Ownership is decided by command signature, never by position: the managed shim
command, the prototype's `python .../monitor.py <kind>` form, or an exact
command string recorded in the manifest. Everything else is foreign and is
never modified, reordered or removed.
"""
import copy
import os
import shlex

EVENTS = {
    "claude": (
        "SessionStart", "SessionEnd", "UserPromptSubmit", "PreToolUse", "PermissionRequest",
        "PostToolUse", "Stop", "PreCompact", "PostCompact", "PostToolUseFailure",
        "Notification", "StopFailure", "Elicitation", "ElicitationResult",
    ),
    "codex": (
        "SessionStart", "SessionEnd", "UserPromptSubmit", "PreToolUse", "PermissionRequest",
        "PostToolUse", "Stop", "PreCompact", "PostCompact", "Interrupt",
    ),
}
HOOK_TIMEOUT = 3
SHIM_NAME = "quoin-deck-hook"
KINDS = ("claude", "codex")


class HooksShapeError(Exception):
    pass


class MergeReport:
    def __init__(self):
        self.changed = False
        self.created_event_keys = []
        self.stale_removed = []


class RemoveReport:
    def __init__(self):
        self.changed = False
        self.removed = []
        self.removed_event_keys = []
        self.shifted_events = []


def managed_command(runtime, kind):
    return shlex.quote(os.path.join(runtime, "bin", SHIM_NAME)) + " " + kind


def canonical(command):
    return {"type": "command", "command": command, "timeout": HOOK_TIMEOUT}


def classify(command, *, runtime_names=("QuoinStreamDeck",), recorded=()):
    if not isinstance(command, str):
        return None
    if command in recorded:
        return "recorded"
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    if len(argv) == 2 and os.path.basename(argv[0]) == SHIM_NAME and argv[1] in KINDS:
        return "managed"
    if (len(argv) == 3 and argv[2] in KINDS and os.path.basename(argv[1]) == "monitor.py"
            and os.path.basename(os.path.dirname(argv[1])) in tuple(runtime_names)):
        return "legacy"
    return None


def _owner(hook, runtime_names, recorded):
    if not isinstance(hook, dict):
        return None
    return classify(hook.get("command"), runtime_names=runtime_names, recorded=recorded)


def _groups(lst):
    """Yield (group_index, group) for groups whose `hooks` is a list."""
    for gi, group in enumerate(lst):
        if isinstance(group, dict) and isinstance(group.get("hooks"), list):
            yield gi, group


def _owned_positions(lst, runtime_names, recorded):
    found = []
    for gi, group in _groups(lst):
        for hi, hook in enumerate(group["hooks"]):
            if _owner(hook, runtime_names, recorded):
                found.append((gi, hi))
    return found


def _hooks_dict(data, create):
    hooks = data.get("hooks")
    if hooks is None:
        if not create:
            return None
        hooks = data["hooks"] = {}
    if not isinstance(hooks, dict):
        raise HooksShapeError("`hooks` is not an object")
    return hooks


def _delete_owned(lst, positions):
    """Delete owned hook objects; drop only the groups we emptied. Returns True if anything changed."""
    touched = set()
    for gi, hi in sorted(positions, reverse=True):
        del lst[gi]["hooks"][hi]
        touched.add(gi)
    for gi in sorted(touched, reverse=True):
        if not lst[gi]["hooks"]:
            del lst[gi]
    return bool(positions)


def merge(data, kind, command, *, runtime_names=("QuoinStreamDeck",), recorded=()):
    new = copy.deepcopy(data)
    report = MergeReport()
    hooks = _hooks_dict(new, create=True)
    registry = EVENTS[kind]
    for event in registry:
        lst = hooks.get(event)
        if lst is not None and not isinstance(lst, list):
            raise HooksShapeError("hooks.%s is not a list" % event)
        owned = _owned_positions(lst, runtime_names, recorded) if lst is not None else []
        if not owned:
            if lst is None:
                lst = hooks[event] = []
                report.created_event_keys.append(event)
            lst.append({"hooks": [canonical(command)]})
            continue
        gi, hi = owned[0]
        lst[gi]["hooks"][hi] = canonical(command)
        _delete_owned(lst, owned[1:])
    for event, lst in list(hooks.items()):
        if event in registry or not isinstance(lst, list):
            continue
        positions = _owned_positions(lst, runtime_names, recorded)
        if positions:
            _delete_owned(lst, positions)
            report.stale_removed.append(event)
    report.changed = new != data
    return new, report


def remove(data, kind, *, runtime_names=("QuoinStreamDeck",), recorded=(), removable_event_keys=()):
    new = copy.deepcopy(data)
    report = RemoveReport()
    hooks = _hooks_dict(new, create=False)
    if hooks is None:
        return new, report
    for event, lst in list(hooks.items()):
        if not isinstance(lst, list):
            continue
        positions = _owned_positions(lst, runtime_names, recorded)
        if not positions:
            continue
        before = _foreign_layout(lst, positions)
        _delete_owned(lst, positions)
        report.removed.append(event)
        if before != _foreign_layout_after(lst):
            report.shifted_events.append(event)
        if not lst and event in removable_event_keys:
            del hooks[event]
            report.removed_event_keys.append(event)
    report.changed = new != data
    return new, report


def _foreign_layout(lst, owned):
    """Positions of foreign entries as (group_index, hook_index) pairs, in order, before removal."""
    skip = set(owned)
    layout = []
    for gi, group in enumerate(lst):
        if isinstance(group, dict) and isinstance(group.get("hooks"), list):
            for hi in range(len(group["hooks"])):
                if (gi, hi) not in skip:
                    layout.append((gi, hi))
        else:
            layout.append((gi, None))
    return layout


def _foreign_layout_after(lst):
    layout = []
    for gi, group in enumerate(lst):
        if isinstance(group, dict) and isinstance(group.get("hooks"), list):
            for hi in range(len(group["hooks"])):
                layout.append((gi, hi))
        else:
            layout.append((gi, None))
    return layout


def inspect(data, kind, expected_command, *, runtime_names=("QuoinStreamDeck",), recorded=()):
    """Per-event state: ok | missing | duplicate | stale | shadowed (non-registry owned: stale)."""
    hooks = data.get("hooks") if isinstance(data, dict) else None
    hooks = hooks if isinstance(hooks, dict) else {}
    states = {}
    want = canonical(expected_command)
    for event in EVENTS[kind]:
        lst = hooks.get(event)
        owned = _owned_positions(lst, runtime_names, recorded) if isinstance(lst, list) else []
        if not owned:
            states[event] = "missing"
        elif len(owned) > 1:
            states[event] = "duplicate"
        else:
            gi, hi = owned[0]
            group = lst[gi]
            if group["hooks"][hi] != want:
                states[event] = "stale"
            elif "matcher" in group or len(group["hooks"]) > 1:
                states[event] = "shadowed"
            else:
                states[event] = "ok"
    for event, lst in hooks.items():
        if event not in EVENTS[kind] and isinstance(lst, list) and _owned_positions(lst, runtime_names, recorded):
            states[event] = "stale"
    return states


def owned_commands(data, kind, *, runtime_names=("QuoinStreamDeck",), recorded=()):
    """[(event, group_index, hook_index)] for every owned object (registry events only)."""
    hooks = data.get("hooks") if isinstance(data, dict) else None
    out = []
    if not isinstance(hooks, dict):
        return out
    for event in EVENTS[kind]:
        lst = hooks.get(event)
        if isinstance(lst, list):
            out.extend((event, gi, hi) for gi, hi in _owned_positions(lst, runtime_names, recorded))
    return out


def legacy_only_events(data, kind, runtime_names=("QuoinStreamDeck",)):
    hooks = data.get("hooks") if isinstance(data, dict) else None
    out = []
    if not isinstance(hooks, dict):
        return out
    for event in EVENTS[kind]:
        lst = hooks.get(event)
        if not isinstance(lst, list):
            continue
        seen = 0
        all_legacy = True
        for group in lst:
            if not (isinstance(group, dict) and isinstance(group.get("hooks"), list)):
                all_legacy = False
                continue
            for hook in group["hooks"]:
                seen += 1
                if _owner(hook, runtime_names, ()) != "legacy":
                    all_legacy = False
        if seen and all_legacy:
            out.append(event)
    return out
