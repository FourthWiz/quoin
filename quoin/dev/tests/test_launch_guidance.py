"""Tests for the shared non-clobber predicate, the v3 store path helper, and
the version-aware launch-guidance helper.

Pure units plus a file-content agreement test across the three doc surfaces
(`README.md`, `quoin/CLAUDE.md`, `quoin/memory/workflow-catalog.md`). No
network, no subprocess.
"""
from __future__ import annotations

import builtins
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "src"))

from quoin.ccr_config import (  # noqa: E402
    OPENROUTER_PREFIX,
    UNKNOWN_LAUNCH_NOTE,
    V2_LAUNCH_CLAUSE,
    V2_LAUNCH_COMMAND,
    V3_LAUNCH_COMMAND,
    V3_PROFILE_NAME,
    V3_ROUTING_GAP_NOTICE,
    ccr_config_path,
    ccr_store_path,
    launch_command_phrase,
    launch_guidance,
    owned_key_is_writable,
)

# Doc surfaces live under REPO_ROOT (README.md) and REPO_ROOT/"quoin" (the
# source package dir that also holds CLAUDE.md and memory/workflow-catalog.md).


# ── owned_key_is_writable ────────────────────────────────────────────────────


def test_owned_key_is_writable_absent():
    assert owned_key_is_writable(None) is True


def test_owned_key_is_writable_openrouter_prefixed():
    assert owned_key_is_writable("openrouter,some-model") is True


def test_owned_key_is_writable_foreign():
    assert owned_key_is_writable("my-provider,my-model") is False


def test_owned_key_is_writable_non_string():
    assert owned_key_is_writable(123) is False
    assert owned_key_is_writable({"a": 1}) is False


def test_owned_key_is_writable_uses_build_router_map_prefix():
    # Mirrors the construction models.build_router_map uses.
    slug = "z-ai/glm-4.6"
    value = f"{OPENROUTER_PREFIX}{slug}"
    assert owned_key_is_writable(value) is True


# ── ccr_store_path ───────────────────────────────────────────────────────────


def test_ccr_store_path_matches_config_dir(tmp_path):
    config = ccr_config_path(home=tmp_path)
    store = ccr_store_path(home=tmp_path)
    assert store == config.parent / "config.sqlite"


def test_ccr_store_path_does_not_touch_disk(tmp_path, monkeypatch):
    calls = []
    real_open = builtins.open
    real_os_open = os.open

    def _tracking_open(*args, **kwargs):
        calls.append(("open", args, kwargs))
        return real_open(*args, **kwargs)

    def _tracking_os_open(*args, **kwargs):
        calls.append(("os.open", args, kwargs))
        return real_os_open(*args, **kwargs)

    monkeypatch.setattr(builtins, "open", _tracking_open)
    monkeypatch.setattr(os, "open", _tracking_os_open)

    result = ccr_store_path(home=tmp_path)

    assert calls == []
    assert not result.exists()


# ── launch_guidance ──────────────────────────────────────────────────────────


def test_launch_guidance_v3_names_profile():
    cmd, note = launch_guidance(3)
    assert cmd == V3_LAUNCH_COMMAND
    assert V3_PROFILE_NAME in note
    assert "configure its models" in note
    assert "routes nothing" in note  # does not claim it routes
    assert "ccr code" not in note


def test_launch_guidance_v2_is_todays_command():
    cmd, note = launch_guidance(2)
    assert cmd == V2_LAUNCH_COMMAND
    assert note == ""


def test_launch_guidance_unknown_declines():
    for major in (None, 0, 4):
        cmd, note = launch_guidance(major)
        assert cmd == ""
        assert note == UNKNOWN_LAUNCH_NOTE


def test_launch_guidance_returns_a_pair():
    for major in (None, 0, 2, 3, 4):
        result = launch_guidance(major)
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert all(isinstance(part, str) for part in result)


# ── launch_command_phrase ────────────────────────────────────────────────────


def test_launch_command_phrase_v2_names_command_and_clause():
    phrase = launch_command_phrase(2)
    assert V2_LAUNCH_COMMAND in phrase
    assert V2_LAUNCH_CLAUSE in phrase


def test_launch_command_phrase_v3_names_command_and_note():
    phrase = launch_command_phrase(3)
    assert V3_LAUNCH_COMMAND in phrase
    assert V3_PROFILE_NAME in phrase
    assert "ccr code" not in phrase


def test_launch_command_phrase_unknown_falls_back_to_empty():
    for major in (None, 0, 4):
        assert launch_command_phrase(major) == ""


# ── V3_ROUTING_GAP_NOTICE ────────────────────────────────────────────────────


def test_v3_routing_gap_notice_names_the_three_lost_routes():
    assert "background" in V3_ROUTING_GAP_NOTICE
    assert "think" in V3_ROUTING_GAP_NOTICE
    assert "longContext" in V3_ROUTING_GAP_NOTICE


def test_v3_routing_gap_notice_never_says_ccr_code():
    assert "ccr code" not in V3_ROUTING_GAP_NOTICE


def test_v3_routing_gap_notice_points_at_ccr_ui():
    assert "ccr ui" in V3_ROUTING_GAP_NOTICE


# ── doc-surface agreement ───────────────────────────────────────────────────


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_three_doc_surfaces_agree_with_helper():
    surfaces = [
        REPO_ROOT / "README.md",
        REPO_ROOT / "quoin" / "CLAUDE.md",
        REPO_ROOT / "quoin" / "memory" / "workflow-catalog.md",
    ]
    texts = [_read(p) for p in surfaces]

    v2_cmd, _ = launch_guidance(2)
    v3_cmd, _ = launch_guidance(3)

    for path, text in zip(surfaces, texts):
        assert v2_cmd in text, f"{path} missing v2 command {v2_cmd!r}"
    for path, text in zip(surfaces, texts):
        assert v3_cmd in text, f"{path} missing v3 command {v3_cmd!r}"


_DOC_SURFACES = [
    REPO_ROOT / "README.md",
    REPO_ROOT / "quoin" / "CLAUDE.md",
    REPO_ROOT / "quoin" / "memory" / "workflow-catalog.md",
]


def test_readme_names_both_paths():
    """Generalised (AC-41) from README.md-only to all three surfaces: each
    names a v3 invocation and carries a not-yet-routing clause near it, so
    a surface cannot silently claim the v3 profile routes models before
    they are configured."""
    for path in _DOC_SURFACES:
        text = _read(path)
        assert V2_LAUNCH_COMMAND in text, f"{path} missing v2 command"
        assert V3_LAUNCH_COMMAND in text, f"{path} missing v3 command"

        idx = text.index(V3_LAUNCH_COMMAND)
        window = text[max(0, idx - 500): idx + 500]
        assert "configure its models" in window, f"{path}: no configure-its-models clause nearby"
        assert "routes nothing" in window, f"{path}: does not deny routing nearby"  # no overclaim


# Known non-profile subcommands: every documented `ccr <word>` invocation
# other than the profile itself. Pinned as a tuple, not derived, so a new
# subcommand must be a conscious addition here.
_KNOWN_NON_PROFILE_SUBCOMMANDS = ("code", "start", "ui", "version")


def test_no_surface_names_a_different_profile_near_a_v3_invocation():
    """AC-41's first clause, proven negatively (round-2 MAJ-3: a literal
    'names V3_PROFILE_NAME' substring check cannot independently fail,
    since V3_PROFILE_NAME is itself a substring of the already-asserted
    V3_LAUNCH_COMMAND). Candidate set: every `ccr <word>` invocation
    documented across the three surfaces, minus the known non-profile
    subcommands. Every remaining candidate must equal V3_LAUNCH_COMMAND —
    a surface naming `ccr some-other-profile` would leave a remainder that
    fails this equality."""
    pattern = re.compile(r"`ccr [a-z][a-z0-9-]+`")
    candidates: set[str] = set()
    for path in _DOC_SURFACES:
        candidates |= set(pattern.findall(_read(path)))

    non_profile = {f"`ccr {cmd}`" for cmd in _KNOWN_NON_PROFILE_SUBCOMMANDS}
    remainder = candidates - non_profile
    assert remainder, "candidate set must be non-empty — the assertion needs a real failure mode"
    assert remainder == {f"`{V3_LAUNCH_COMMAND}`"}


def test_catalog_is_generated_from_claude_md():
    catalog_text = _read(REPO_ROOT / "quoin" / "memory" / "workflow-catalog.md")
    first_line = catalog_text.splitlines()[0]
    assert "generated by" in first_line

    claude_md_text = _read(REPO_ROOT / "quoin" / "CLAUDE.md")
    # The open-model routing section starts at the "Open-model routing" heading
    # and runs to the next top-level heading.
    start = claude_md_text.index("Open-model routing")
    rest = claude_md_text[start:]
    next_heading = rest.find("\n## ", 1)
    section = rest if next_heading == -1 else rest[:next_heading]
    assert section.strip() in catalog_text
