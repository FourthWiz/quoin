"""Lifecycle fixtures and guards for the Stream Deck foundation modules.

Builds on `_streamdeck_helpers`: everything here writes only below the test's
tmp dir. The lifecycle runtime is the HOME default (it contains a space) so
`deck_home`'s stub manifest is never seen by status or detection.
"""
import dataclasses
import datetime
import hashlib
import json
import os
import shlex
import shutil
import stat
import types

import pytest

import _streamdeck_helpers as h
from _streamdeck_helpers import GuardViolation, deck_home, forbid_dangerous_subprocess  # noqa: F401
from quoin.streamdeck import hooks_merge, jsonfile, manifest, paths as paths_mod, prereqs
import quoin.streamdeck as sd

NOW = datetime.datetime(2026, 10, 13, 12, 0, 0, tzinfo=datetime.timezone.utc)
TOOL_NAMES = ("python", "node", "zellij", "osascript", "open")


def assert_paths_under(paths, root):
    root = os.path.abspath(str(root))
    bad = []
    for field in dataclasses.fields(paths):
        value = getattr(paths, field.name)
        if not h._under(value, root):
            bad.append((field.name, value))
    if bad:
        raise GuardViolation("Paths outside the test tmp dir: %r" % (bad,))


@pytest.fixture
def lifecycle_home(deck_home, monkeypatch, tmp_path):
    for key in list(os.environ):
        if key.startswith("QUOIN_STREAMDECK_") or key in ("CLAUDE_CONFIG_DIR", "QUOIN_DECK_RUNTIME"):
            monkeypatch.delenv(key)
    home = deck_home.home
    apps = tmp_path / "Applications"
    apps.mkdir()
    support = home / "Library" / "Application Support"
    built = []
    real_resolve = paths_mod.resolve

    def recording_resolve(*a, **kw):
        result = real_resolve(*a, **kw)
        built.append(result)
        return result

    monkeypatch.setattr(paths_mod, "resolve", recording_resolve)
    ns = types.SimpleNamespace(
        root=tmp_path, home=home, apps=apps, fakebin=deck_home.fakebin,
        runtime=support / "QuoinStreamDeck",
        plugins=support / "com.elgato.StreamDeck" / "Plugins",
        plugin_dir=support / "com.elgato.StreamDeck" / "Plugins" / "local.quoin.agentdesk.sdPlugin",
        state_root=home / ".config" / "agentdesk" / "streamdeck",
        claude_settings=home / ".claude" / "settings.json",
        codex_home=home / ".codex",
        raw_resolve=real_resolve,
        resolve=lambda flags=None, environ=None: paths_mod.resolve(flags, os.environ if environ is None else environ),
    )
    yield ns
    for built_paths in built:
        assert_paths_under(built_paths, tmp_path)


def tree_snapshot(root):
    root = str(root)
    out = {}
    for base, dirs, files in os.walk(root):
        for name in dirs + files:
            full = os.path.join(base, name)
            rel = os.path.relpath(full, root)
            st = os.lstat(full)
            if stat.S_ISLNK(st.st_mode):
                out[rel] = ("link", stat.S_IMODE(st.st_mode), 0, os.readlink(full))
            elif stat.S_ISDIR(st.st_mode):
                out[rel] = ("dir", stat.S_IMODE(st.st_mode), 0, None)
            else:
                with open(full, "rb") as handle:
                    digest = hashlib.sha256(handle.read()).hexdigest()
                out[rel] = ("file", stat.S_IMODE(st.st_mode), st.st_size, digest)
    return out


def write_json(path, data):
    path = str(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")


def make_settings(path, foreign=True, core=True):
    data = {}
    hooks = {}
    if foreign:
        data["theme"] = "dark"
        data["permissions"] = {"deny": ["Read(./.env)"]}
        hooks["Stop"] = [{"hooks": [{"type": "command", "command": "/usr/local/bin/notify stop"}]}]
        hooks["PreToolUse"] = [{"matcher": "Bash", "hooks": [{"type": "command", "command": "/usr/local/bin/guard"}]}]
    if core:
        for event, script in (("UserPromptSubmit", "userpromptsubmit.sh"), ("Stop", "stop.sh"), ("SessionStart", "sessionstart.sh")):
            hooks.setdefault(event, []).append(
                {"hooks": [{"type": "command", "command": "bash %s/hooks/%s" % (os.path.dirname(str(path)), script)}]})
    if hooks:
        data["hooks"] = hooks
    write_json(path, data)
    return data


def legacy_command(runtime):
    return "/usr/bin/python3 %s %s" % (shlex.quote(os.path.join(str(runtime), "monitor.py")), "claude")


def make_prototype(lh):
    runtime = lh.runtime
    runtime.mkdir(parents=True)
    for name in ("monitor.py", "dashboard.py", "controller.py", "terminal.zsh"):
        shutil.copy2(h.FIXTURE_DIR / name, runtime / name)
    (runtime / "backups").mkdir()
    (runtime / "backups" / "20261013-old").mkdir()
    (runtime / "Buttons" / "x.app").mkdir(parents=True)
    (runtime / ".DS_Store").write_text("x")
    (runtime / "health.json").write_text("{}")
    lh.plugin_dir.mkdir(parents=True)
    (lh.plugin_dir / "launch.sh").write_text('#!/bin/sh\ncd -- "$(dirname -- "$0")"\nexec /opt/x/bin/node plugin.js "$@"\n')
    (lh.plugin_dir / "plugin.js").write_text("//")
    for kind, path in (("claude", lh.claude_settings), ("codex", lh.codex_home / "hooks.json")):
        cmd = "/usr/bin/python3 %s %s" % (shlex.quote(str(runtime / "monitor.py")), kind)
        data = {"theme": "dark"}
        data["hooks"] = {ev: [{"hooks": [{"type": "command", "command": cmd, "timeout": 3}]}]
                         for ev in hooks_merge.EVENTS[kind]}
        write_json(path, data)
    return runtime


def _file_entry(root, base, full, kind, root_name):
    st = os.stat(full)
    with open(full, "rb") as handle:
        digest = hashlib.sha256(handle.read()).hexdigest()
    return {"rel": os.path.relpath(full, base).replace(os.sep, "/"), "root": root_name, "sha256": digest,
            "mode": stat.S_IMODE(st.st_mode), "kind": kind}


def _fake_exec(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return str(path)


def make_installed(lh, mode="active", now=NOW, claude_only=False):
    paths = lh.resolve()
    runtime = lh.runtime
    version_dir = runtime / "versions" / "1.0.0"
    version_dir.mkdir(parents=True)
    files = []
    for name in sd.RUNTIME_FILES:
        shutil.copy2(sd.PAYLOAD_DIR + os.sep + name, version_dir / name)
        files.append(_file_entry(runtime, runtime, str(version_dir / name), "code", "runtime"))
    shim = runtime / "bin" / "quoin-deck-hook"
    _fake_exec(shim)
    _fake_exec(runtime / "bin" / "quoin-deck-terminal")
    files.append(_file_entry(runtime, runtime, str(shim), "shim", "runtime"))
    if mode != "adoption-pending":
        os.symlink("versions/1.0.0", str(runtime / "current"))
    plugin_deployed = mode == "active"
    if plugin_deployed:
        lh.plugin_dir.mkdir(parents=True)
        for name in ("plugin.js", "manifest.json"):
            (lh.plugin_dir / name).write_text("// %s\n" % name)
            files.append(_file_entry(lh.plugin_dir, lh.plugin_dir, str(lh.plugin_dir / name), "plugin", "plugin"))
    bins = {}
    for name in TOOL_NAMES:
        bins[name] = {"path": _fake_exec(lh.root / "toolbin" / name)}
    bins["node"] = {"mode": "external", "path": bins["node"]["path"]}
    app = lh.apps / "Ghostty.app"
    app.mkdir()
    bins["ghostty_app"] = {"path": str(app)}
    agentdesk = lh.home / ".config" / "agentdesk" / "agentdesk.zsh"
    agentdesk.parent.mkdir(parents=True, exist_ok=True)
    agentdesk.write_text("AGENTDESK_API_VERSION=1\n")
    bins["agentdesk"] = {"path": str(agentdesk)}
    bins["claude"] = {"path": _fake_exec(lh.root / "toolbin" / "claude")}
    hook_records = []
    if mode == "active":
        kinds = ("claude",) if claude_only else ("claude", "codex")
        for kind in kinds:
            settings = str(lh.claude_settings if kind == "claude" else lh.codex_home / "hooks.json")
            command = hooks_merge.managed_command(str(runtime), kind)
            created = {}

            def mutate(data, kind=kind, command=command):
                new, report = hooks_merge.merge(data, kind, command, runtime_names=("QuoinStreamDeck",))
                created.update({e: True for e in report.created_event_keys})
                return new

            os.makedirs(os.path.dirname(settings), exist_ok=True)
            jsonfile.update(settings, mutate, create_parents=True)
            for event in hooks_merge.EVENTS[kind]:
                hook_records.append({"runtime": kind, "settings_path": settings, "event": event, "command": command,
                                     "timeout": 3, "created_event_key": bool(created.get(event))})
    os.makedirs(paths.state, exist_ok=True)
    health = {"updated": int(now.timestamp() * 1000) - 10_000, "agents": 0}
    (lh.state_root / "state").mkdir(parents=True, exist_ok=True)
    (lh.state_root / "state" / "health.json").write_text(json.dumps(health))
    prereq_record = {n: {"state": "ok", "path": None, "version": None, "checked_at": "2026-10-13T12:00:00Z"}
                     for n in ("python", "streamdeck-app", "node", "agentdesk", "zellij", "ghostty", "homebrew")}
    data = manifest.new_manifest(
        paths=paths, component_version=sd.COMPONENT_VERSION, payload_fingerprint=sd.payload_fingerprint(),
        quoin_version="0.0.0", platform={"system": "darwin", "macos": "15.0", "machine": "arm64"}, mode=mode,
        binaries=bins, zellij_socket_dir=str(lh.root / "zsock"), files=files,
        version_dir=str(version_dir), previous_version_dir=None, hooks=hook_records,
        plugin={"deployed": plugin_deployed}, prereqs=prereq_record, features_unavailable=[],
        adopted_from=None, legacy_owned_events=[], now=now)
    manifest.write(paths.manifest, data)
    return types.SimpleNamespace(paths=paths, manifest=data, version_dir=version_dir)


class FakeCtx:
    """Dict-driven Ctx: nothing here can reach the machine."""

    def __init__(self, tmp, *, which=None, probes=None, stdin_tty=False, stdout_tty=False, answers=(),
                 runner_rc=None, deploy_rc=0, flags=None, recorded=None, legacy=None, realpath_map=None,
                 virtual_env=None, sys_prefix="/venv/prefix", environ=None, installs=None):
        self.which_table = dict(which or {})
        self.probes = dict(probes or {})
        self.probe_calls = []
        self.runner_calls = []
        self.prompts = []
        self.deployed = []
        self.lines = []
        self.answers = list(answers)
        self.runner_rc = runner_rc if runner_rc is not None else (lambda argv: 0)
        self.deploy_rc = deploy_rc
        self.realpath_map = dict(realpath_map or {})
        self.installs = installs  # name -> callable(table) applied after a successful runner call
        (tmp / "apps").mkdir(exist_ok=True)
        self.apps = tmp / "apps"
        self.stdin_tty, self.stdout_tty = stdin_tty, stdout_tty
        self.ctx = prereqs.Ctx(
            which=lambda name: self.which_table.get(name),
            realpath=lambda p: self.realpath_map.get(p, p),
            run_probe=self._probe,
            stdin_isatty=lambda: self.stdin_tty,
            stdout_isatty=lambda: self.stdout_tty,
            prompt=self._prompt,
            runner=self._runner,
            deploy_agentdesk=self._deploy,
            environ=dict(environ or {}),
            apps_dir=str(self.apps),
            home=str(tmp / "home"),
            sys_prefix=sys_prefix,
            virtual_env=virtual_env,
            flags=dict(flags or {}),
            recorded=dict(recorded or {}),
            legacy=dict(legacy or {}),
            out=self.lines.append,
            now=lambda: "2026-10-13T12:00:00Z",
        )

    def _probe(self, argv, timeout=5):
        key = tuple(argv)
        self.probe_calls.append(key)
        if key in self.probes:
            value = self.probes[key]
            return value(key) if callable(value) else value
        return 127, "", "unscripted"

    def _prompt(self, text):
        self.prompts.append(text)
        if not self.answers:
            raise EOFError()
        return self.answers.pop(0)

    def _runner(self, argv):
        self.runner_calls.append(tuple(argv))
        rc = self.runner_rc(tuple(argv)) if callable(self.runner_rc) else self.runner_rc
        if rc == 0 and self.installs:
            action = self.installs.get(tuple(argv))
            if action:
                action(self)
        return rc

    def _deploy(self):
        self.deployed.append(True)
        if self.installs and "deploy" in self.installs:
            self.installs["deploy"](self)
        return self.deploy_rc

    def probed(self, prefix):
        return [c for c in self.probe_calls if c and c[0].startswith(prefix)]
