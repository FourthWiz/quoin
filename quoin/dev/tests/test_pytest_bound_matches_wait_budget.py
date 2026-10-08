"""The pytest bound ceiling plus its wait margin must stay inside the headless wait budget."""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
AT_PATH = REPO_ROOT / "quoin" / "core" / "scripts" / "affected_tests.py"
WAIT_FOR = REPO_ROOT / "quoin" / "core" / "scripts" / "wait_for.py"
AMODE = REPO_ROOT / "quoin" / "memory" / "autonomous-mode.md"


def _load_at():
    spec = importlib.util.spec_from_file_location("_at_budget", AT_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_ceiling_plus_margin_fits_both_documented_budgets():
    at = _load_at()
    wait_src = WAIT_FOR.read_text(encoding="utf-8")
    m = re.search(r'"QUOIN_WAIT_BUDGET_SECS", "(\d+)"', wait_src)
    assert m, "wait_for.py default budget not found"
    wait_budget = int(m.group(1))
    amode_text = AMODE.read_text(encoding="utf-8")
    m2 = re.search(r"`QUOIN_WAIT_BUDGET_SECS` \(default (\d+)\)", amode_text)
    assert m2, "autonomous-mode.md default budget not found"
    amode_budget = int(m2.group(1))
    assert wait_budget == amode_budget == at._WAIT_BUDGET_DEFAULT
    total = at._PYTEST_TIMEOUT_CEILING + at._PYTEST_WAIT_MARGIN
    assert total <= wait_budget
    assert total <= amode_budget


def test_amode_documents_the_pairing():
    text = " ".join(AMODE.read_text(encoding="utf-8").split())
    assert "raise `QUOIN_PYTEST_TIMEOUT` and `QUOIN_WAIT_BUDGET_SECS` together" in text
