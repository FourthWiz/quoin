"""Import-order independence for the OpenCode adapter modules.

`install.py`, `generate.py` and `scripts.py` import from each other and
`quoin.cli` imports all three. A cycle introduced anywhere in that group
would only show up when the modules are imported in a particular order (the
first import binds the half-built module into `sys.modules`, and every
later one wins or loses depending on which happened first), so this test
runs each order in a fresh subprocess rather than relying on the fixed
order pytest happens to use when it collects the rest of this suite.
"""
from __future__ import annotations

import itertools
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = REPO_ROOT / "src"

_ADAPTER_MODULES = (
    "quoin.opencode_adapter.install",
    "quoin.opencode_adapter.generate",
    "quoin.opencode_adapter.scripts",
)

_CONFIG_MODULES = tuple(
    "quoin.opencode_adapter." + name
    for name in (
        "errors", "paths", "jsonio", "schema_check", "secrets", "config",
        "merge", "qualification", "roles", "compiler", "explain", "probe_cli", "import_preview", "retry", "events",
        "proctree", "runstore", "launch_env",
    )
)

_ORDERS = list(itertools.permutations(_ADAPTER_MODULES))
_CLI_FIRST_ORDERS = [("quoin.cli",) + order for order in _ORDERS]

# Each config module alone, followed by the CLI, and the CLI followed by it;
# a full permutation of the new set would add nothing but time.
_CONFIG_ORDERS = (
    [(m,) for m in _CONFIG_MODULES]
    + [(m, "quoin.cli") for m in _CONFIG_MODULES]
    + [("quoin.cli", m) for m in _CONFIG_MODULES]
)

_ALL_ORDERS = _ORDERS + _CLI_FIRST_ORDERS + _CONFIG_ORDERS
_IDS = ["-".join(m.rsplit(".", 1)[-1] for m in order) for order in _ALL_ORDERS]


@pytest.mark.parametrize("order", _ALL_ORDERS, ids=_IDS)
def test_each_import_order_exits_zero(order):
    code = "; ".join("import %s" % module for module in order)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC_DIR)
    proc = subprocess.run(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(REPO_ROOT),
        env=env,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
