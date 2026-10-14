"""The Stream Deck component installed from a built wheel into a clean virtual environment.

Why the in-process guards are not used here: every effect happens in child processes whose environment is built
from scratch with a temporary HOME, and the test itself never opens anything under the real home folder. Only the
wheel build child inherits the real environment (read-only use of the pip cache and pip configuration, so the
isolated build works offline); its working directory and every output are temporary directories. Creating the
virtual environment, installing the wheel and every `quoin` command run with the temporary HOME.

The child still makes two read-only host probes, which is allowed: the Homebrew check runs
`/bin/zsh -lc 'command -v brew'` (that reads the system `/etc/zprofile`), and the Stream Deck running check lists
real processes. Nothing here asserts on the "Stream Deck is running" note or on the Homebrew prerequisite record.

The component refuses to run anywhere but macOS, so the lifecycle test only runs there.
"""
import hashlib
import json
import os
import plistlib
import shlex
import stat
import subprocess
import sys

import pytest

import quoin.streamdeck as sd
from _streamdeck_build import REPO, build, copy_tracked_tree, skip_or_fail

pytestmark = [pytest.mark.slow_fs, pytest.mark.timeout(900)]

CODEX_REVIEW_LINE = "Codex: review the changed hooks in Codex /hooks (trust is never written by quoin)"
SAMPLE_STOP = json.dumps({"session_id": "s-1", "hook_event_name": "Stop"}).encode()


def run_quiet(argv, env, timeout=300):
    return subprocess.run(argv, env=env, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)


def scratch_env(home, path):
    """A child environment built from nothing: only what the component and pip need."""
    return {"HOME": str(home), "PATH": path, "PYTHONNOUSERSITE": "1", "PIP_NO_INDEX": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1", "LC_ALL": "C"}


@pytest.fixture(scope="module")
def wheel_venv(tmp_path_factory):
    """One wheel built from the committed tree and one virtual environment with it installed."""
    pytest.importorskip("build")
    base = tmp_path_factory.mktemp("wheel-e2e")
    tree = copy_tracked_tree(base / "tree", cached_only=True)
    proc, artifacts = build(tree, base / "out", kinds=("--wheel",))
    if proc.returncode != 0:
        skip_or_fail(proc)
    wheels = [a for a in artifacts if a.name.endswith(".whl")]
    assert len(wheels) == 1, artifacts
    home = base / "venv-home"
    home.mkdir()
    venv = base / "venv"
    env = scratch_env(home, "/usr/bin:/bin")
    made = run_quiet([sys.executable, "-m", "venv", str(venv)], env)
    if made.returncode != 0 or not (venv / "bin" / "python").exists():
        pytest.skip("could not create a virtual environment: %s" % (made.stderr or made.stdout)[-400:])
    installed = run_quiet([str(venv / "bin" / "python"), "-m", "pip", "install", "--no-index", "--no-deps",
                           "--disable-pip-version-check", str(wheels[0])], env)
    if installed.returncode != 0:
        pytest.skip("pip could not install the wheel: %s" % (installed.stderr or installed.stdout)[-400:])
    return {"venv": venv, "wheel": wheels[0], "base": base}


def base_python():
    return os.path.realpath(getattr(sys, "_base_executable", None) or sys.executable)


def make_world(tmp_path, venv):
    """Home, a working directory outside the repo, stub tools, a planted Stream Deck app and the child env."""
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    work = tmp_path / "work"
    work.mkdir()
    stub = tmp_path / "stub" / "bin"
    stub.mkdir(parents=True)

    def script(name, text):
        path = stub / name
        path.write_text(text)
        path.chmod(0o755)
        return str(path)

    python3 = script("python3", "#!/bin/sh\nexec %s \"$@\"\n" % shlex.quote(base_python()))
    script("node", "#!/bin/sh\ncase \"$1\" in --version) echo v22.11.0;; *) exit 0;; esac\n")
    script("zellij", "#!/bin/sh\ncase \"$1\" in\n  action) echo 'list-panes focus-pane-id';;\n"
                     "  --version) echo 'zellij 0.43.1';;\n  *) exit 0;;\nesac\n")
    apps = tmp_path / "apps"
    info = apps / "Elgato Stream Deck.app" / "Contents" / "Info.plist"
    info.parent.mkdir(parents=True)
    info.write_bytes(plistlib.dumps({"CFBundleShortVersionString": "7.6.0",
                                     "CFBundleIdentifier": "com.elgato.StreamDeck"}))
    env = scratch_env(home, "%s:%s:/usr/bin:/bin" % (venv / "bin", stub))
    env["QUOIN_STREAMDECK_APPS_DIR"] = str(apps)
    return {"home": home, "work": work, "stub": stub, "python3": python3, "env": env, "apps": apps}


def quoin(world, venv, *args):
    return subprocess.run([str(venv / "bin" / "quoin"), "streamdeck", *args], env=world["env"], cwd=str(world["work"]),
                          stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)


def file_digests(root):
    """sha256 of every regular file under `root`, with the manifest's verification time ignored."""
    out = {}
    for base, _dirs, files in os.walk(str(root)):
        for name in files:
            full = os.path.join(base, name)
            if os.path.islink(full):
                out[os.path.relpath(full, str(root))] = "link:" + os.readlink(full)
                continue
            with open(full, "rb") as handle:
                data = handle.read()
            if name == "install-manifest.json":
                parsed = json.loads(data.decode("utf-8"))
                parsed.pop("last_verified_at", None)
                data = json.dumps(parsed, sort_keys=True).encode()
            out[os.path.relpath(full, str(root))] = hashlib.sha256(data).hexdigest()
    return out


def hook_commands(home):
    commands = []
    for path in (home / ".claude" / "settings.json", home / ".codex" / "hooks.json"):
        data = json.loads(path.read_text())
        for groups in (data.get("hooks") or {}).values():
            for group in groups:
                for hook in group.get("hooks", []):
                    if "quoin-deck-hook" in hook.get("command", ""):
                        commands.append(hook["command"])
    return commands


def run_hooks(world, commands):
    results = []
    for command in dict.fromkeys(commands):
        proc = subprocess.run(["/bin/sh", "-c", command], input=SAMPLE_STOP, capture_output=True,
                              env=world["env"], cwd=str(world["work"]), timeout=60)
        results.append((command, proc.returncode, proc.stdout))
    return results


# ------------------------------------------------------------------ the wheel carries what the component needs

def test_installed_wheel_runs_without_the_checkout(wheel_venv, tmp_path):
    venv = wheel_venv["venv"]
    world = make_world(tmp_path, venv)
    code = ("import json, sys, quoin, quoin.streamdeck as sd\n"
            "print(json.dumps({'file': quoin.__file__, 'payload': sd.PAYLOAD_DIR, 'path': sys.path,\n"
            "                  'files': sd.payload_files(sd.PAYLOAD_DIR), 'fingerprint': sd.payload_fingerprint()}))\n")
    proc = run_quiet([str(venv / "bin" / "python"), "-I", "-c", code], world["env"])
    assert proc.returncode == 0, proc.stderr
    info = json.loads(proc.stdout)
    real_venv = os.path.realpath(str(venv))
    assert os.path.realpath(info["file"]).startswith(real_venv + os.sep)
    assert os.path.realpath(info["payload"]).startswith(real_venv + os.sep)
    repo = os.path.realpath(str(REPO))
    assert not [p for p in info["path"] if p and os.path.realpath(p).startswith(repo + os.sep)], info["path"]
    assert info["files"] == sd.payload_files()
    assert info["fingerprint"] == sd.payload_fingerprint()


# ------------------------------------------------------------------ the whole lifecycle, from the wheel

@pytest.mark.skipif(sys.platform != "darwin", reason="the component only installs on macOS")
def test_component_lifecycle_from_the_wheel(wheel_venv, tmp_path):
    venv = wheel_venv["venv"]
    world = make_world(tmp_path, venv)
    home, env = world["home"], world["env"]
    git_before = subprocess.run(["git", "-C", str(REPO), "status", "--porcelain"], capture_output=True,
                                text=True).stdout

    # 0. every path the child resolves is under the temporary home, before any command runs
    real_home = os.path.realpath(os.path.expanduser("~"))
    assert not os.path.realpath(str(home)).startswith(real_home + os.sep), "the temp HOME is inside the real home"
    probe = run_quiet([str(venv / "bin" / "python"), "-I", "-c",
                       "import json, os; from quoin.streamdeck import paths; "
                       "print(json.dumps(paths.resolve({}, os.environ).manifest_paths()))"], env)
    assert probe.returncode == 0, probe.stderr
    resolved = json.loads(probe.stdout)
    assert resolved and all(os.path.realpath(v).startswith(os.path.realpath(str(home)) + os.sep)
                            for v in resolved.values()), resolved

    # 1. nothing installed yet
    first = quoin(world, venv, "status", "--json")
    assert first.returncode == 10, first.stdout + first.stderr

    # 2. install
    installed = quoin(world, venv, "install", "--prereqs", "none", "--python", world["python3"])
    assert installed.returncode == 0, installed.stdout + installed.stderr
    assert CODEX_REVIEW_LINE in installed.stdout.splitlines()
    runtime = home / "Library" / "Application Support" / "QuoinStreamDeck"
    data = json.loads((runtime / "install-manifest.json").read_text())
    assert data["mode"] == "active" and data["component_version"] == sd.COMPONENT_VERSION
    from quoin import __about__
    assert data["quoin_version"] == __about__.__version__
    plugin = home / "Library" / "Application Support" / "com.elgato.StreamDeck" / "Plugins" / "local.quoin.agentdesk.sdPlugin"
    assert json.loads((plugin / "manifest.json").read_text())["UUID"] == "local.quoin.agentdesk"
    link = os.readlink(str(runtime / "current"))
    assert link.startswith("versions/%s-" % sd.COMPONENT_VERSION) and len(link.rsplit("-", 1)[1]) == 12
    commands = hook_commands(home)
    claude = json.loads((home / ".claude" / "settings.json").read_text())["hooks"]
    codex = json.loads((home / ".codex" / "hooks.json").read_text())["hooks"]
    from quoin.streamdeck import hooks_merge
    for name, hooks in (("claude", claude), ("codex", codex)):
        events = hooks_merge.EVENTS[name]
        assert sorted(hooks) == sorted(events), name
        for event in events:
            ours = [h for g in hooks[event] for h in g["hooks"] if "quoin-deck-hook" in h["command"]]
            assert len(ours) == 1, (name, event)

    # 3. nothing the component wrote points into the venv, site-packages or the checkout
    forbidden = [str(venv), os.path.realpath(str(venv)), "site-packages", str(REPO), os.path.realpath(str(REPO))]
    for base, _dirs, files in os.walk(str(home)):
        for name in files:
            full = os.path.join(base, name)
            if os.path.islink(full):
                continue
            try:
                text = open(full, encoding="utf-8").read()
            except (UnicodeDecodeError, OSError):
                continue
            for needle in forbidden:
                assert needle not in text, (full, needle)
    for command in commands:
        assert "versions/" not in command, command
    assert "versions/" not in (plugin / "plugin-config.json").read_text()
    for entry in data["binaries"].values():
        path = entry.get("path") if isinstance(entry, dict) else None
        assert not path or not path.startswith(str(venv)), data["binaries"]

    # 4. healthy
    status = quoin(world, venv, "status", "--json")
    report = json.loads(status.stdout)
    assert status.returncode == 0 and report["problems"] == [], status.stdout

    # 5. repeating install and upgrade changes nothing
    snapshot = file_digests(home)
    again = quoin(world, venv, "install", "--prereqs", "none", "--python", world["python3"])
    assert again.returncode == 0, again.stdout + again.stderr
    assert file_digests(home) == snapshot
    upgraded = quoin(world, venv, "upgrade", "--prereqs", "none")
    assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr
    assert file_digests(home) == snapshot

    # 6. the recorded hook commands run quietly
    for command, code, out in run_hooks(world, commands):
        assert code == 0 and out == b"", command

    # 7. uninstall, twice, and the hooks left in running agents stay inert
    removed = quoin(world, venv, "uninstall")
    assert removed.returncode == 0, removed.stdout + removed.stderr
    nothing = quoin(world, venv, "uninstall")
    assert nothing.returncode == 0 and "nothing to remove" in nothing.stdout
    gone = quoin(world, venv, "status", "--json")
    assert gone.returncode == 10, gone.stdout
    for command, code, out in run_hooks(world, commands):
        assert code == 0 and out == b"", command

    # 8. purge keeps only the settings reference copies, and nothing of the component state
    purged = quoin(world, venv, "uninstall", "--purge")
    assert purged.returncode == 0, purged.stdout + purged.stderr
    assert runtime.is_dir()
    assert sorted(p.name for p in runtime.iterdir()) == ["backups"]
    assert all(p.name.startswith("quoin-") and p.is_dir() for p in (runtime / "backups").iterdir())
    for gone_path in (plugin, home / ".config" / "agentdesk" / "streamdeck"):
        assert not gone_path.exists(), gone_path
    for path in (home / ".claude" / "settings.json", home / ".codex" / "hooks.json"):
        assert "quoin-deck" not in path.read_text()

    # 9. the checkout was never touched
    git_after = subprocess.run(["git", "-C", str(REPO), "status", "--porcelain"], capture_output=True,
                               text=True).stdout
    assert git_after == git_before
