"""Cross-surface parity for the child-takeover text: the supervisor module
and the standalone auto_resume script each render their own copy, so every
comparison here runs both real implementations over the same inputs."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from quoin import cli, takeover
from quoin import supervisor as sup

REPO_ROOT = Path(__file__).resolve().parents[3]
U = "0b1c2d3e-4f50-4a61-8b72-93a4b5c6d7e8"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def ar():
    return _load(REPO_ROOT / "quoin" / "core" / "scripts" / "auto_resume.py", "ar_parity")


@pytest.fixture(scope="module")
def rs():
    return _load(REPO_ROOT / "quoin" / "core" / "scripts" / "run_state.py", "rs_parity")


ROOTS = ["/a/b", "/My Drive/x", "/it's/here"]
SIDS = [U, None, "../x", "", U.upper()]


@pytest.mark.parametrize("root", ROOTS)
@pytest.mark.parametrize("sid", SIDS)
def test_pointer_and_hint_parity(ar, root, sid):
    assert sup.takeover_pointer("demo", root) == ar._takeover_pointer("demo", root)
    assert sup.takeover_hint("demo", root, sid) == ar._takeover_hint("demo", root, sid)
    assert sup.is_child_session_id(sid) == ar._is_child_session_id(sid)


NOTICE_ROOTS = ROOTS + ["/a\\b", "/a  b"]


@pytest.mark.parametrize("root", NOTICE_ROOTS)
def test_notice_pointer_parity(ar, root):
    assert sup.takeover_notice_pointer("demo", root) == ar._takeover_notice_pointer("demo", root)


def test_notice_pointer_literals():
    for root in ("/a/b", "/My Drive/x"):
        assert sup.takeover_notice_pointer("demo", root).startswith("quoin run --takeover demo --project-root ")
    for root in ("/it's/here", "/a\\b", "/a  b"):
        assert sup.takeover_notice_pointer("demo", root) == "quoin run --takeover demo"
    for root in NOTICE_ROOTS:
        assert "--project-root" in sup.takeover_pointer("demo", root)
        assert "--project-root" in sup.takeover_hint("demo", root, U)


def test_notice_pointer_agrees_with_real_sanitizer(rs):
    for root in NOTICE_ROOTS:
        full = sup.takeover_pointer("demo", root)
        unchanged = rs._sanitize(full) == full
        assert (sup.takeover_notice_pointer("demo", root) == full) is unchanged


def test_templates_and_regexes_agree(ar, rs):
    assert takeover.ARM_TEMPLATE == ar.ARM_TEMPLATE
    assert takeover._TASK_PATTERN == rs._TASK_RE.pattern
    assert cli._FIRST_CHILD_ENV == ar._CHILD_ENV


def test_takeover_halt_reason_is_documented():
    doc = (REPO_ROOT / "quoin" / "memory" / "autonomous-mode.md").read_text()
    assert takeover.HALT_REASON in doc


FR9_DOCS = (
    "quoin/memory/autonomous-mode.md",
    "quoin/core/skills/run.md",
    "quoin/adapters/claude/skills/run/SKILL.md",
    "quoin/QUICKSTART.md",
)


@pytest.mark.parametrize("rel", FR9_DOCS)
def test_docs_name_the_takeover_command(rel):
    assert "quoin run --takeover" in (REPO_ROOT / rel).read_text()


def test_version_bumped():
    from quoin.__about__ import __version__

    # The takeover text shipped in 0.36.0; assert that floor rather than an
    # exact pin, which would break on every later release.
    version = tuple(int(part) for part in __version__.split(".")[:3])
    assert version >= (0, 36, 0)
