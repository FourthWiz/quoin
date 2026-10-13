"""Packaging rules for the Stream Deck component.

The static guards run everywhere. The build test packages a copy of the tree
with junk planted in the payload and agentdesk directories, once with the
repo's .gitignore files and once without, because hatchling honours a root
.gitignore even outside a git checkout: only the second build proves the
pyproject rules on their own.

The build child inherits the real environment minus PYTHONPYCACHEPREFIX (the
real HOME lets pip's cache satisfy the isolated build requirement offline),
runs with cwd and every output under tmp_path, and never touches brew,
osascript, zellij or the installer. It deliberately does not use the HOME
redirect guard.
"""
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile

import pytest

import quoin.streamdeck as sd

REPO = pathlib.Path(__file__).resolve().parents[3]
LIFECYCLE = ("paths", "macos", "jsonfile", "hooks_merge", "manifest", "prereqs", "status")
AGENTDESK_FILES = ("agentdesk.zsh", "setup-agentdesk.sh", "KEYBIND-VERIFY.md")
PLANTED = (
    "quoin/tools/agentdesk/streamdeck/dist/x.js",
    "quoin/tools/agentdesk/.DS_Store",
    "quoin/tools/agentdesk/__pycache__/x.cpython-314.pyc",
    "src/quoin/streamdeck/payload/.DS_Store",
    "src/quoin/streamdeck/payload/__pycache__/monitor.cpython-314.pyc",
    "src/quoin/streamdeck/payload/dist/x.js",
    "src/quoin/streamdeck/payload/state/agents/x.json",
    "src/quoin/streamdeck/payload/state/x.txt",
    "src/quoin/streamdeck/payload/agents/x.json",
    "src/quoin/streamdeck/payload/agents/x.md",
    "src/quoin/streamdeck/payload/health.json",
)
FORBIDDEN_MEMBER_PARTS = (
    "/dist/", ".DS_Store", "__pycache__", ".pyc", "streamdeck/dist", "payload/state/", "payload/agents/",
    "payload/health.json",
)
FORBIDDEN_BASENAMES = ("x.json", "x.txt", "x.md", "x.js")
NETWORK_ERRORS = re.compile(
    r"Could not find a version|No matching distribution|Connection|Temporary failure|Name or service|"
    r"ProxyError|Failed to establish|Network is unreachable|Max retries|timed out|SSLError|No such host", re.I)


def pyproject_text():
    return (REPO / "pyproject.toml").read_text(encoding="utf-8")


def section(name):
    text = pyproject_text()
    start = text.index("[%s]" % name)
    rest = text[start + len(name) + 2:]
    nxt = re.search(r"^\[", rest, re.M)
    return rest[:nxt.start()] if nxt else rest


# ------------------------------------------------------------------ static guards

def test_no_shipped_payload_file_matches_an_exclude():
    for rel in sd.payload_files():
        parts = rel.split("/")
        assert not rel.endswith((".json", ".log", ".lock", ".pyc")), rel
        assert not set(parts[:-1]) & {"dist", "state", "agents", "__pycache__"}, rel
        assert parts[-1] != ".DS_Store", rel


def test_agentdesk_is_packaged_per_file():
    block = section("tool.hatch.build.targets.wheel.force-include")
    assert not re.search(r'^"quoin/tools/agentdesk"\s*=', block, re.M)
    for name in AGENTDESK_FILES:
        assert '"quoin/tools/agentdesk/%s" = "src/quoin/data/tools/agentdesk/%s"' % (name, name) in block
        assert (REPO / "quoin" / "tools" / "agentdesk" / name).is_file()


def test_exclude_rules_are_present_in_both_targets():
    wheel, sdist = section("tool.hatch.build.targets.wheel"), section("tool.hatch.build.targets.sdist")
    for pattern in ("**/.DS_Store", "src/quoin/streamdeck/payload/**/*.json", "src/quoin/streamdeck/payload/**/*.log",
                    "src/quoin/streamdeck/payload/**/*.lock"):
        assert '"%s"' % pattern in wheel and '"%s"' % pattern in sdist, pattern
    assert '"src/quoin/streamdeck/payload/state"' in wheel and '"src/quoin/streamdeck/payload/state/"' in sdist
    assert '"src/quoin/streamdeck/payload/agents"' in wheel and '"src/quoin/streamdeck/payload/agents/"' in sdist
    assert '"quoin/tools/agentdesk/streamdeck/"' in sdist and '"src/**/__pycache__/"' in sdist


def test_gitignore_hides_payload_state():
    text = (REPO / ".gitignore").read_text()
    for line in ("/src/quoin/streamdeck/payload/state/", "/src/quoin/streamdeck/payload/agents/",
                 "/src/quoin/streamdeck/payload/health.json"):
        assert line in text


# ------------------------------------------------------------------ planted-tree build

def build_planted_tree(dst, gitignore=True):
    listing = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                             capture_output=True, check=True).stdout.decode().split("\0")
    for rel in listing:
        if not rel or rel.startswith(("quoin/dev/", "quoin/benchmarks/")):
            continue
        src = REPO / rel
        if not src.is_file():
            continue
        target = pathlib.Path(dst) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
    if not gitignore:
        for base, _, files in os.walk(dst):
            if ".gitignore" in files:
                os.remove(os.path.join(base, ".gitignore"))
    for rel in PLANTED:
        target = pathlib.Path(dst) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("planted\n")
    return dst


def archive_names(path):
    if str(path).endswith(".whl"):
        return zipfile.ZipFile(path).namelist()
    with tarfile.open(path) as tar:
        return tar.getnames()


@pytest.mark.slow_fs
@pytest.mark.timeout(900)
@pytest.mark.parametrize("gitignore", [True, False], ids=["gitignore", "no_gitignore"])
def test_planted_tree_wheel_and_sdist(tmp_path, gitignore):
    pytest.importorskip("build")
    tree = build_planted_tree(tmp_path / "tree", gitignore=gitignore)
    out = tmp_path / "out"
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPYCACHEPREFIX"}
    proc = subprocess.run([sys.executable, "-m", "build", "--sdist", "--wheel", "--outdir", str(out), str(tree)],
                          capture_output=True, text=True, cwd=str(tmp_path), env=env)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout)[-600:]
        if NETWORK_ERRORS.search(proc.stderr or ""):
            pytest.skip("isolated build could not resolve its build requirement: %s" % tail)
        pytest.fail("build failed: %s" % tail)
    wheel = next(out.glob("*.whl"))
    sdist = next(out.glob("*.tar.gz"))
    wheel_names, sdist_names = archive_names(wheel), archive_names(sdist)
    sdist_rel = [n.split("/", 1)[1] if "/" in n else n for n in sdist_names]
    for label, names in (("wheel", wheel_names), ("sdist", sdist_rel)):
        bad = [n for n in names if any(part in n for part in FORBIDDEN_MEMBER_PARTS)
               or n.rsplit("/", 1)[-1] in FORBIDDEN_BASENAMES]
        assert not bad, "%s ships planted junk: %s" % (label, bad)
    for module in ("__init__", "payload_build") + LIFECYCLE:
        assert "quoin/streamdeck/%s.py" % module in wheel_names, module
        assert "src/quoin/streamdeck/%s.py" % module in sdist_rel, module
    prefix = "quoin/streamdeck/payload/"
    assert sorted(n[len(prefix):] for n in wheel_names if n.startswith(prefix)) == sd.payload_files()
    for name in AGENTDESK_FILES:
        assert "quoin/data/tools/agentdesk/%s" % name in wheel_names
        assert "quoin/tools/agentdesk/%s" % name in sdist_rel
