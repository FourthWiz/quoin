"""Run swebench's evaluation harness as an x86_64 host on an arm64 machine.

Launched by the judge instead of `python -m swebench.harness.run_evaluation`
when x86_64 emulation is requested. It imports nothing from quoin and runs
under the interpreter that has swebench installed.

What it changes, checked against swebench 3.0.17 (site-packages/swebench/harness):
  * test_spec/test_spec.py:207-211 (`make_test_spec`) picks the image
    architecture from `platform.machine()`; there is no architecture
    parameter. Reporting x86_64 selects the x86_64 image names and, through the
    `platform` property (test_spec/test_spec.py:136-141), `linux/x86_64`.
  * docker_build.py:462 (`build_container`) is the only image pull, a bare
    `client.images.pull(test_spec.instance_image_key)` with no platform. On an
    arm64 daemon that would fetch an arm64 manifest or none, so the shim fills
    in `platform="linux/amd64"` for any pull that names none.
  * docker_build.py:485 creates the container with `test_spec.platform`, which
    is `linux/x86_64` once the architecture is forced.

Writes `shim-record.json` in the working directory with the forced values so
the judge can record that emulation was applied.
"""

from __future__ import annotations

import json
import os
import platform
import runpy
import sys
import warnings

FORCED_ARCH = "x86_64"
PULL_PLATFORM = "linux/amd64"
RECORD_NAME = "shim-record.json"


def _drop_own_directory() -> None:
    # The harness directory holds modules (config.py and others) that must not
    # shadow anything the evaluation imports.
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.getcwd()) != here]


def _force_architecture() -> None:
    platform.machine = lambda: FORCED_ARCH


def _default_pull_platform() -> None:
    from docker.models import images

    original = images.ImageCollection.pull

    def pull(self, repository, *args, **kwargs):
        if kwargs.get("platform") is None:
            kwargs["platform"] = PULL_PLATFORM
        return original(self, repository, *args, **kwargs)

    images.ImageCollection.pull = pull


def main() -> None:
    _drop_own_directory()
    _force_architecture()
    _default_pull_platform()
    with open(RECORD_NAME, "w", encoding="utf-8") as handle:
        json.dump({"forced_arch": FORCED_ARCH, "pull_platform": PULL_PLATFORM}, handle)
    sys.argv = ["swebench.harness.run_evaluation"] + sys.argv[1:]
    # swebench's package import already loads the module runpy is about to run.
    warnings.filterwarnings("ignore", category=RuntimeWarning, module="runpy")
    runpy.run_module("swebench.harness.run_evaluation", run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
