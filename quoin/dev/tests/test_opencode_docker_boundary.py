"""Opt-in demonstration of the managed boundary in a real container.

The container has no network (`--network none`), a read-only root filesystem,
an unprivileged user, a read-only root-owned isolation attestation and the
fake `opencode` executable. It shows that the launcher's isolation check
reports `verified` against the real (not fixture) trusted roots and that the
fake's network child fails because of the container, not because of the
fixture's own allowlist.

Skipped, with a specific reason, unless QUOIN_OPENCODE_DOCKER_TESTS=1, the
`docker` executable is on PATH, the daemon answers, and the image named by
QUOIN_OPENCODE_DOCKER_IMAGE (default python:3.12-slim) is already local; the
image is never pulled.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Callable, Mapping, Optional

import pytest

import _opencode_capture_helpers as ch
import _opencode_driver_helpers as dh
import _opencode_isolation_helpers as iso
from quoin.opencode_adapter import paths

ENABLE_ENV = "QUOIN_OPENCODE_DOCKER_TESTS"
IMAGE_ENV = "QUOIN_OPENCODE_DOCKER_IMAGE"
DEFAULT_IMAGE = "python:3.12-slim"
TEST_NET_2 = "198.51.100.1"
SRC = Path(__file__).resolve().parents[3] / "src"
FAKES = Path(__file__).resolve().parent / "fakes"
ATTESTATION_DIR = str(paths.attestation_path("linux").parent)
BLOCKED_EXIT = 95

Runner = Callable[..., "subprocess.CompletedProcess"]


def skip_reason(
    environ: Mapping[str, str], which: Callable[[str], Optional[str]] = shutil.which,
    run: Runner = subprocess.run,
) -> Optional[str]:
    """Why the tier cannot run here, or None when it can."""
    if environ.get(ENABLE_ENV) != "1":
        return "set %s=1 to run the Docker boundary demonstration" % ENABLE_ENV
    if which("docker") is None:
        return "no 'docker' executable on PATH"
    try:
        info = run(["docker", "info"], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return "'docker info' could not be run"
    if info.returncode != 0:
        return "the docker daemon is not reachable ('docker info' failed)"
    image = environ.get(IMAGE_ENV, DEFAULT_IMAGE)
    try:
        found = run(["docker", "image", "inspect", image], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return "'docker image inspect %s' could not be run" % image
    if found.returncode != 0:
        return "image %s is not local (the test never pulls); set %s to a local image with python3" % (image, IMAGE_ENV)
    return None


SKIP_REASON = skip_reason(os.environ)
_ENABLED = SKIP_REASON is None


# ---------------------------------------------------------- skip logic (offline)


class _Done:
    def __init__(self, code: int) -> None:
        self.returncode = code


def _runner(info: int = 0, inspect: int = 0):
    def run(argv, **kw):
        return _Done(info if argv[1] == "info" else inspect)
    return run


def test_each_skip_reason_is_specific():
    on = {ENABLE_ENV: "1"}
    assert "QUOIN_OPENCODE_DOCKER_TESTS=1" in skip_reason({})
    assert "no 'docker'" in skip_reason(on, which=lambda name: None)
    assert "daemon" in skip_reason(on, which=lambda name: "/bin/docker", run=_runner(info=1))
    reason = skip_reason({**on, IMAGE_ENV: "example:1"}, which=lambda name: "/bin/docker", run=_runner(inspect=1))
    assert "example:1" in reason and "never pulls" in reason
    assert skip_reason(on, which=lambda name: "/bin/docker", run=_runner()) is None


def test_a_missing_docker_binary_is_a_skip_not_an_error():
    def boom(argv, **kw):
        raise FileNotFoundError("docker")
    assert "could not be run" in skip_reason({ENABLE_ENV: "1"}, which=lambda name: "/bin/docker", run=boom)


# ------------------------------------------------------------------ container

_CONTAINER_SCRIPT = r"""
import json, os, subprocess, sys
from pathlib import Path
from quoin.opencode_adapter import isolation

out = {}
home = Path(os.environ["HOME"])
(home / "adapter").mkdir(parents=True, exist_ok=True)
(home / "profiles").mkdir(parents=True, exist_ok=True)
verdict = isolation.evaluate(
    isolation.TrustedRoots.system(), profile="work",
    adapter_root=home / "adapter", profiles_dir=home / "profiles")
out["isolation"] = {"status": verdict.status, "environment": verdict.environment,
                    "evidence": list(verdict.evidence), "missing": list(verdict.missing)}
done = subprocess.run(
    [sys.executable, "/opt/fakes/fake_opencode.py", "--fake-scenario", "/opt/cfg/scenario.json",
     "--fake-state", "/tmp/state", "--", "run", "--format", "json", "--", "go"],
    capture_output=True, stdin=subprocess.DEVNULL, timeout=60)
out["fake_exit"] = done.returncode
log = Path("/tmp/state/network.jsonl")
out["network"] = [json.loads(l) for l in log.read_text().splitlines() if l.strip()] if log.exists() else []
def attempt(label, action):
    try:
        action()
        out[label] = "allowed"
    except OSError as exc:
        out[label] = type(exc).__name__
attempt("read_shadow", lambda: open("/etc/shadow", "rb").read(1))
attempt("list_root_home", lambda: os.listdir("/root"))
attempt("write_rootfs", lambda: open("/opt/quoin-write-probe", "w").write("x"))
attempt("write_source", lambda: open("/opt/quoin-src/probe", "w").write("x"))
print(json.dumps(out))
"""

_SEED_SCRIPT = r"""
import json, os, sys
os.makedirs(sys.argv[1], exist_ok=True)
path = os.path.join(sys.argv[1], sys.argv[2])
with open(path, "w") as handle:
    handle.write(os.environ["ATTESTATION"])
os.chown(sys.argv[1], 0, 0)
os.chmod(sys.argv[1], 0o755)
os.chown(path, 0, 0)
os.chmod(path, 0o644)
"""


def _docker(*args: str, timeout: int = 120) -> "subprocess.CompletedProcess":
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


@pytest.fixture
def attestation_volume():
    name = "quoin-attest-%s" % uuid.uuid4().hex[:12]
    assert _docker("volume", "create", name).returncode == 0
    yield name
    _docker("volume", "rm", "-f", name)


@pytest.mark.skipif(not _ENABLED, reason=SKIP_REASON or "")
def test_the_container_boundary_stops_what_a_convenience_world_would_allow(tmp_path, attestation_volume):
    image = os.environ.get(IMAGE_ENV, DEFAULT_IMAGE)
    target = "%s:80" % TEST_NET_2
    scenario = ch.with_allowed_targets(dh.scenario_dict(("shell_net", {"url": "http://%s/" % target})), [target])
    cfg = tmp_path / "cfg"
    dh.fake.write_scenario(cfg / "scenario.json", scenario)

    seed = _docker(
        "run", "--rm", "--user", "0", "-e", "ATTESTATION=" + json.dumps(iso.GOOD_ATTESTATION),
        "-v", "%s:%s" % (attestation_volume, ATTESTATION_DIR), image,
        "python3", "-c", _SEED_SCRIPT, ATTESTATION_DIR, paths.ATTESTATION_NAME)
    assert seed.returncode == 0, seed.stderr

    home = "/tmp/home"
    env = {"HOME": home, "PYTHONPATH": "/opt/quoin-src", "PYTHONDONTWRITEBYTECODE": "1",
           **{name: "%s/%s" % (home, name.lower()) for name in
              ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME")}}
    argv = ["run", "--rm", "--network", "none", "--read-only", "--user", "1000:1000", "--tmpfs", "/tmp",
            "-v", "%s:/opt/quoin-src:ro" % SRC, "-v", "%s:/opt/fakes:ro" % FAKES,
            "-v", "%s:/opt/cfg:ro" % cfg, "-v", "%s:%s:ro" % (attestation_volume, ATTESTATION_DIR)]
    for name, value in env.items():
        argv += ["-e", "%s=%s" % (name, value)]
    done = _docker(*argv, image, "python3", "-c", _CONTAINER_SCRIPT)
    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout.strip().splitlines()[-1])

    isolation = result["isolation"]
    assert isolation["status"] == "verified", isolation
    assert "corroboration-container" in isolation["evidence"], isolation
    assert "attestation-root-owned" in isolation["evidence"], isolation

    records = result["network"]
    assert len(records) == 1, records
    record = records[0]
    # The fixture allowed the target, so the failure is the container's.
    assert record["blocked"] is False and record["ok"] is False, record
    assert result["fake_exit"] != BLOCKED_EXIT

    for label in ("read_shadow", "list_root_home", "write_rootfs", "write_source"):
        assert result[label] != "allowed", (label, result[label])
