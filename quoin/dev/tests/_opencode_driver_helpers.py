"""Shared, non-collected helpers for the OpenCode driver tests.

`make_prepared` builds a `PreparedRun` directly around the fake OpenCode
executable, so lifecycle tests never need a configuration, an install or a
compiled file that means anything. Locations derive from `tmp_path`:
`project_of`, `state_of` (the fake's state directory: invocations, effects
log, sessions) and `store_of` (the run store).
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from quoin.opencode_adapter import driver, launch_env, retry, runstore

FAKE_PATH = Path(__file__).resolve().parent / "fakes" / "fake_opencode.py"
TASK = "demo"


def load_fake():
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec = importlib.util.spec_from_file_location("fake_opencode_for_driver", FAKE_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.dont_write_bytecode = previous


fake = load_fake()


def project_of(tmp_path: Path) -> Path:
    return Path(tmp_path) / "project"


def state_of(tmp_path: Path) -> Path:
    return Path(tmp_path) / "fake-state"


def store_of(tmp_path: Path) -> Path:
    return project_of(tmp_path) / ".workflow_artifacts" / "memory" / "runtime" / "opencode"


def scenario_dict(scenario: Union[str, Dict[str, Any], tuple]) -> Dict[str, Any]:
    if isinstance(scenario, dict):
        return scenario
    if isinstance(scenario, tuple):
        name, kwargs = scenario
        return fake.SCENARIOS[name](**kwargs)
    return fake.SCENARIOS[scenario]()


def make_prepared(
    tmp_path: Path,
    scenario: Union[str, Dict[str, Any], tuple],
    *,
    task: str = TASK,
    stage: Optional[str] = None,
    timeout_s: Optional[float] = None,
    secret: Optional[str] = None,
    context_refs: tuple = (),
) -> "driver.PreparedRun":
    tmp_path = Path(tmp_path)
    project = project_of(tmp_path)
    (project / ".git").mkdir(parents=True, exist_ok=True)
    task_dir = project / ".workflow_artifacts" / task
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "notes.md").write_text("notes\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)

    scenario_path = fake.write_scenario(tmp_path / "scenario.json", scenario_dict(scenario))
    shim = fake.write_shim(tmp_path / "bin", scenario_path, state_of(tmp_path))

    compiled = tmp_path / "compiled" / "opencode.json"
    compiled.parent.mkdir(exist_ok=True)
    compiled.write_text("{}\n", encoding="utf-8")
    sha = hashlib.sha256(compiled.read_bytes()).hexdigest()

    redactor = launch_env.Redactor()
    values = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "OPENCODE_CONFIG": str(compiled),
        "XDG_DATA_HOME": str(tmp_path / "data"),
    }
    if secret is not None:
        values["PROV_KEY"] = secret
        redactor.add(secret)
    env = launch_env.LaunchEnv(values, redactor)

    directory = runstore.store_dir(project, create=True)
    run_id, run_paths = runstore.reserve_run_id(directory)
    request = driver.RunRequest(
        project_root=project, task=task, stage=stage, phase="plan", profile="work",
        timeout_s=timeout_s, context_refs=tuple(context_refs),
    )
    arg = task if stage is None else "stage %s of %s" % (stage, task)
    argv = (str(shim), "run", "--format", "json", "--command", "quoin-plan", "--", arg)
    hashes = runstore.hash_inputs(project, task, context_refs)
    record = runstore.new_run_record(run_id, task, {"task": task, "stage": stage, "phase": "plan", "profile": "work"}, {})
    record["input_hashes"] = hashes
    runstore.write_record(directory, record)
    runstore.write_pointer(directory, runstore.new_pointer(task, run_id))
    return driver.PreparedRun(
        request=request, run_id=run_id, binary=shim, runtime_version="1.18.32", role="plan",
        effective_model="fake/model", config_digest="sha256:test", native_sha256=sha,
        config_path=compiled, cwd=project, argv=argv, env_names=tuple(env.names()),
        policy=None, retry=retry.RetryPolicy.from_limits({}), artifact_paths=run_paths,
        input_hashes=hashes, repo_revisions=(), launch_env=env,
    )


def make_driver(tmp_path: Path, **kw: Any) -> "driver.OpenCodeDriver":
    tmp_path = Path(tmp_path)
    (tmp_path / "home").mkdir(exist_ok=True)
    defaults = dict(
        env={"PATH": os.environ.get("PATH", "")}, home=tmp_path / "home",
        descendant_scan_s=0.2, eof_grace_s=1.0, exit_drain_s=0.5,
        grace_s=2.0, kill_grace_s=1.0, leftover_grace_s=1.0,
    )
    defaults.update(kw)
    return driver.OpenCodeDriver(project_of(tmp_path), **defaults)


def run_to_end(drv: "driver.OpenCodeDriver", prepared: "driver.PreparedRun", **kw: Any):
    handle = drv.start(prepared, **kw)
    events = list(drv.observe(handle))
    return handle, events


def load_record(tmp_path: Path, run_id: str) -> Dict[str, Any]:
    return runstore.load_record(store_of(tmp_path), run_id)


def read_events(prepared: "driver.PreparedRun"):
    return list(runstore.read_sidecar(prepared.artifact_paths.sidecar).events)


# ------------------------------------------------------------ processes


def _recorded_pids(state: Path) -> List[int]:
    pids: List[int] = []
    for name in ("grandchildren.txt", "intermediates.txt"):
        path = state / name
        if path.exists():
            pids += [int(x) for x in path.read_text().split() if x.strip().isdigit()]
    inv = state / "invocations.jsonl"
    if inv.exists():
        import json

        for line in inv.read_text().splitlines():
            if line.strip():
                pid = json.loads(line).get("pid")
                if isinstance(pid, int):
                    pids.append(pid)
    return pids


def _is_fake_process(pid: int, state: Path) -> bool:
    try:
        out = subprocess.run(["ps", "-p", str(pid), "-o", "stat=,command="], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    text = out.stdout.strip()
    if out.returncode != 0 or not text or text.startswith("Z"):
        return False
    return str(state) in text or "fake_opencode" in text


def stray_pids(tmp_path: Path) -> List[int]:
    state = state_of(tmp_path)
    return [pid for pid in sorted(set(_recorded_pids(state))) if _is_fake_process(pid, state)]


def cleanup_fakes(tmp_path: Path) -> None:
    """Stop every process a test's fake left behind."""
    state = state_of(tmp_path)
    state.mkdir(parents=True, exist_ok=True)
    (state / "stop").write_text("", encoding="utf-8")
    for pid in stray_pids(tmp_path):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
