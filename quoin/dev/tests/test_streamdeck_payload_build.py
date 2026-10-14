import importlib.util
import json
import os
import shutil
import struct
import subprocess
import sys
import zipfile

import pytest

import _streamdeck_helpers as h
from _streamdeck_helpers import deck_home, forbid_dangerous_subprocess  # noqa: F401
from quoin import streamdeck
from quoin.streamdeck import payload_build as pb

pytestmark = pytest.mark.usefixtures("deck_home", "forbid_dangerous_subprocess")

GOLDEN_IDS = [
    '851878af-a866-5f05-9791-c38df3f6304a', '56a6d149-8b0c-51c8-82f3-12e123c3653f',
    '54c5d174-9c0c-591a-bd65-5c8f78dadb38', 'e2ce863a-fc5d-5377-8d73-d5b603917ba1',
    '0a624b56-9fbe-5d0d-8cc4-92d81edce2fb', '18133bb5-8438-51c1-9212-d41a587c2ab4',
    '03788634-2ae2-5d83-9c87-43be3b457916', '1f428cb0-3556-5e82-9c92-637152824796',
    'a9a78e01-03c2-5282-9d77-53977deceef0', '40220862-13b8-5b5e-849d-ff6e92f26214',
    '063af3a6-6e76-5bab-b7ca-95088feea8d4', 'cb9f9acc-bb39-51f3-909b-9d0adb6421d3',
    '96530c19-9ef1-51d4-b48e-f0611c84f848', '7b8ebcc8-42f0-5522-ae77-859cfa65e8f5',
    '71f86e25-c701-5e1e-bbd3-d70c733ab9ab',
]


def page_actions():
    entries = dict(pb.profile_entries())
    page = json.loads(entries[pb.PROFILE_DIR + "/Profiles/30FF3085-BB1E-4F54-9C82-0E373957A761/manifest.json"])
    return page["Controllers"][0]["Actions"]


def test_profile_is_byte_deterministic_and_deflated(tmp_path):
    a, b = str(tmp_path / "a.sdp"), str(tmp_path / "b.sdp")
    pb.build_profile(a)
    pb.build_profile(b)
    assert open(a, "rb").read() == open(b, "rb").read()
    with zipfile.ZipFile(a) as z:
        assert all(i.compress_type == zipfile.ZIP_DEFLATED for i in z.infolist())
        assert all(i.date_time == (1980, 1, 1, 0, 0, 0) for i in z.infolist())
        assert z.namelist() == [n for n, _ in pb.profile_entries()]
    assert not os.path.exists(a + ".tmp")


def test_ids_and_names_match_the_prototype_literally():
    assert pb.PLUGIN_UUID == "local.quoin.agentdesk" and pb.ACTION_UUID == "local.quoin.agentdesk.dashboard"
    assert pb.PLUGIN_NAME == "Quoin Agent Desk Live" and pb.PLUGIN_VERSION == "2.0.0.0"
    assert pb.PROFILE_DIR == "3A28CB85-CAFE-426A-BC25-BD414402F62B.sdProfile"
    assert pb.PAGE_ID == "30ff3085-bb1e-4f54-9c82-0e373957a761"
    assert pb.PROFILE_NAME == "Quoin — Live Agents" and pb.DEVICE_MODEL == "20GAA9901"
    root = json.loads(dict(pb.profile_entries())[pb.PROFILE_DIR + "/manifest.json"])
    assert root == {"Device": {"Model": "20GAA9901", "UUID": ""}, "Name": "Quoin — Live Agents",
                    "Pages": {"Current": pb.PAGE_ID, "Default": pb.PAGE_ID, "Pages": [pb.PAGE_ID]}, "Version": "3.0"}


def test_fifteen_positions_roles_and_stable_action_ids():
    actions = page_actions()
    assert len(actions) == 15
    expect = [("%d,%d" % (i % 5, i // 5)) for i in range(15)]
    assert set(actions) == set(expect)
    assert [actions[k]["ActionID"] for k in expect] == GOLDEN_IDS
    roles = [actions[k]["Settings"] for k in expect]
    assert roles[:9] == [{"role": "agent", "slot": i} for i in range(9)]
    assert [r["role"] for r in roles[9:]] == ["attention", "desk", "claude", "codex", "prev-tab", "next-tab"]
    assert all(a["UUID"] == pb.ACTION_UUID and a["Plugin"]["UUID"] == pb.PLUGIN_UUID for a in actions.values())


def test_plugin_manifest_per_node_mode():
    bundled = pb.plugin_manifest("bundled")
    assert bundled["Nodejs"] == {"Version": "24"} and bundled["Software"]["MinimumVersion"] == "7.1"
    assert bundled["CodePath"] == "plugin.js"
    external = pb.plugin_manifest("external")
    assert "Nodejs" not in external and external["CodePath"] == "launch.sh"
    assert external["Software"]["MinimumVersion"] == "6.6"
    assert external["Icon"] == "icon" and external["CategoryIcon"] == "category"
    with pytest.raises(ValueError):
        pb.plugin_manifest("other")


def test_launch_sh_quoting(tmp_path):
    node = "/Apps/My Node/bin/node"
    text = pb.render_launch_sh(node)
    assert text.splitlines()[2] == "exec '/Apps/My Node/bin/node' plugin.js \"$@\""
    script = tmp_path / "launch.sh"
    script.write_text(text)
    assert subprocess.run(["/bin/sh", "-n", str(script)]).returncode == 0
    with pytest.raises(ValueError):
        pb.render_launch_sh("node")


def test_plugin_config_rejects_versions_and_relative():
    cfg = pb.plugin_config("/py/python", "/rt/QuoinStreamDeck", "/state")
    assert cfg == {"python": "/py/python", "runtime": "/rt/QuoinStreamDeck", "state_root": "/state"}
    for bad in (("/rt/versions/3/python", "/rt", "/s"), ("/py", "/rt", "/rt/versions/state"),
                ("py", "/rt", "/s"), ("/py", "/rt", "s")):
        with pytest.raises(ValueError):
            pb.plugin_config(*bad)
    assert pb.plugin_config("/py", "/home/versions/rt", "/s")["runtime"] == "/home/versions/rt"


@pytest.mark.parametrize("mode", ["external", "bundled"])
def test_build_plugin_dir_writes_only_inside_dest(tmp_path, mode):
    parent = tmp_path / "staging"
    parent.mkdir()
    dest = parent / "x.sdPlugin"
    pb.build_plugin_dir(str(dest), node_mode=mode, node_path="/n/node", python="/p/python",
                        runtime="/r/rt", state_root="/s/state")
    assert sorted(os.listdir(parent)) == ["x.sdPlugin"]
    names = sorted(os.listdir(dest))
    base = ["category.png", "icon.png", "icon@2x.png", "manifest.json", "plugin-config.json", "plugin.js"]
    assert names == sorted(base + (["launch.sh"] if mode == "external" else []))
    if mode == "external":
        assert os.stat(dest / "launch.sh").st_mode & 0o777 == 0o755
    assert (dest / "plugin.js").read_bytes() == (h.PAYLOAD_DIR / "plugin" / "plugin.js").read_bytes()
    assert (dest / "manifest.json").read_text().endswith("}\n")


def test_build_plugin_dir_preconditions(tmp_path):
    kw = dict(node_mode="external", node_path="/n", python="/p", runtime="/r", state_root="/s")
    with pytest.raises(ValueError):
        pb.build_plugin_dir("relative", **kw)
    existing = tmp_path / "there"
    existing.mkdir()
    with pytest.raises(ValueError):
        pb.build_plugin_dir(str(existing), **kw)
    with pytest.raises(ValueError):
        pb.build_plugin_dir(str(tmp_path / "no-parent" / "x"), **kw)


def read_png(data):
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def test_icons_are_valid_png_and_match_the_generator(tmp_path):
    spec = importlib.util.spec_from_file_location("gen", h.REPO_ROOT / "quoin" / "dev" / "scripts" / "gen_streamdeck_icons.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    plugin = h.PAYLOAD_DIR / "plugin"
    for name, size in (("icon.png", 144), ("icon@2x.png", 288), ("category.png", 28)):
        data = (plugin / name).read_bytes()
        assert read_png(data) == (size, size) and data == gen.render(size)
    gen.main(str(tmp_path))
    assert (tmp_path / "icon.png").read_bytes() == (plugin / "icon.png").read_bytes()


def test_payload_files_and_fingerprint_ignore_noise(tmp_path):
    root = tmp_path / "payload"
    shutil.copytree(h.PAYLOAD_DIR, root, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
    before = streamdeck.payload_fingerprint(str(root))
    (root / "__pycache__").mkdir(exist_ok=True)
    (root / "__pycache__" / "x.pyc").write_bytes(b"x")
    (root / ".DS_Store").write_bytes(b"x")
    (root / "plugin" / "stale.pyc").write_bytes(b"x")
    files = streamdeck.payload_files(str(root))
    assert not any(f.endswith(".pyc") or f.endswith(".DS_Store") or "__pycache__" in f for f in files)
    assert files == sorted(files) and "plugin/plugin.js" in files and "deckenv.py" in files
    assert streamdeck.payload_fingerprint(str(root)) == before
    (root / "monitor.py").write_text("changed")
    assert streamdeck.payload_fingerprint(str(root)) != before


def test_package_constants():
    assert streamdeck.COMPONENT_VERSION == "1.0.0"
    for rel in streamdeck.RUNTIME_FILES:
        assert (h.PAYLOAD_DIR / rel).is_file()
    for rel in streamdeck.PLUGIN_FILES:
        assert (h.PAYLOAD_DIR / rel).is_file()


def test_imports_only_stdlib():
    out = subprocess.run([sys.executable, "-c",
                          "import sys; sys.path.insert(0, %r); import quoin.streamdeck, quoin.streamdeck.payload_build as p;"
                          "print(sorted(m for m in sys.modules if m.startswith('quoin')))" % str(h.REPO_ROOT / "src")],
                         capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", "HOME": os.environ["HOME"]})
    assert out.returncode == 0, out.stderr
    assert "quoin.streamdeck.payload_build" in out.stdout and "cli" not in out.stdout
