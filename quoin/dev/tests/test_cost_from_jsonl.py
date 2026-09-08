"""test_cost_from_jsonl.py — parity + structural tests for cost_from_jsonl.py.

Five original test cases, plus IVG-249 T-09 additions (Claude 5 PRICES entries,
parse_session's additive unknown_models/priceable keys):
  1. test_parity_against_ccusage           — real JSONL vs ccusage, ≤1% delta
  2. test_missing_uuid_exit_code           — exit 2 + "not found" on stderr
  3. test_malformed_jsonl_does_not_crash   — valid rows counted, malformed skipped
  4. test_unknown_model_does_not_crash     — unknown model → costUSD=0, no crash
  5. test_project_hash_function            — empirical transform rule (spaces → '-')
"""

import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile

import pytest

# ---------------------------------------------------------------------------
# Path resolution helpers
# ---------------------------------------------------------------------------
REPO_ROOT = pathlib.Path(__file__).parent.parent.parent.parent  # project root
SCRIPTS_DIR = pathlib.Path(__file__).parent.parent.parent / "scripts"  # quoin/scripts/
FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures" / "cost_from_jsonl"
UUID_FIXTURE = FIXTURES_DIR / "uuids.txt"
SCRIPT = SCRIPTS_DIR / "cost_from_jsonl.py"

# Project path for --project-path (the real dev-machine project root)
PROJECT_PATH = str(REPO_ROOT)

sys.path.insert(0, str(SCRIPTS_DIR))
from cost_from_jsonl import project_hash, parse_session, cost_for_entry  # noqa: E402

# ---------------------------------------------------------------------------
# Load fixture UUIDs at module-import time (each becomes a parametrized case)
# ---------------------------------------------------------------------------
def _load_fixture_uuids():
    if not UUID_FIXTURE.exists():
        return []
    lines = UUID_FIXTURE.read_text().strip().splitlines()
    return [ln.strip() for ln in lines if ln.strip()]


FIXTURE_UUIDS = _load_fixture_uuids()

# Compute the expected project hash once for reuse in parity checks
EXPECTED_HASH = project_hash(PROJECT_PATH)


# ---------------------------------------------------------------------------
# Helper: check if ccusage is available
# ---------------------------------------------------------------------------
def _ccusage_available() -> bool:
    if not _npx_available():
        return False
    result = subprocess.run(
        ["npx", "ccusage", "--version"],
        capture_output=True, timeout=30,
    )
    return result.returncode == 0


def _npx_available() -> bool:
    import shutil
    return shutil.which("npx") is not None


# ---------------------------------------------------------------------------
# Test 1: parity against ccusage (per-UUID parametrized, each independently skipable)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("uuid", FIXTURE_UUIDS)
def test_parity_against_ccusage(uuid):
    if not _npx_available():
        pytest.skip("ccusage not installed (npx not found)")
    if not _ccusage_available():
        pytest.skip("ccusage not installed or non-zero version exit")

    # Detect hash mismatch before silent-skip
    proj_hash = project_hash(PROJECT_PATH)
    projects_dir = pathlib.Path.home() / ".claude" / "projects"
    jsonl_path = projects_dir / proj_hash / f"{uuid}.jsonl"

    if not jsonl_path.exists():
        # The fixture UUIDs were recorded on a specific machine; the jsonl may not
        # exist locally. Skip cleanly — the hash-function correctness is separately
        # covered by test_project_hash_function (always-passing structural test).
        # Note: the old sibling-prefix collision check was removed because it
        # produced false pytest.fail when an unrelated project happened to share the
        # 10-char hash prefix (environment-coupling, not a real hash-function bug).
        pytest.skip(f"UUID jsonl not found locally: {jsonl_path}")

    # Run our script
    local_result = subprocess.run(
        [sys.executable, str(SCRIPT), "session", "-i", uuid, "--json",
         "--project-path", PROJECT_PATH],
        capture_output=True, text=True, timeout=30,
    )
    assert local_result.returncode == 0, (
        f"cost_from_jsonl.py exited {local_result.returncode}: {local_result.stderr}"
    )
    local_data = json.loads(local_result.stdout)
    local_cost = local_data["totalCost"]

    # Run ccusage
    ccusage_result = subprocess.run(
        ["npx", "ccusage", "session", "-i", uuid, "--json"],
        capture_output=True, text=True, timeout=30,
    )
    assert ccusage_result.returncode == 0, (
        f"ccusage exited {ccusage_result.returncode}: {ccusage_result.stderr}"
    )
    ccusage_data = json.loads(ccusage_result.stdout)
    ccusage_cost = ccusage_data["totalCost"]

    # Parity: ≤1% relative tolerance
    denom = max(ccusage_cost, 0.01)
    rel_diff = abs(local_cost - ccusage_cost) / denom
    local_entries = {e["model"]: e for e in local_data.get("entries", [])}
    ccusage_entries = {e["model"]: e for e in ccusage_data.get("entries", [])}
    assert rel_diff < 0.01, (
        f"UUID {uuid}: parity FAIL — "
        f"local={local_cost:.6f}, ccusage={ccusage_cost:.6f}, "
        f"rel_diff={rel_diff:.4%}\n"
        f"local entries: {local_entries}\n"
        f"ccusage entries: {ccusage_entries}"
    )


# ---------------------------------------------------------------------------
# Test 2: missing UUID exits with code 2 and stderr "not found"
# ---------------------------------------------------------------------------
def test_missing_uuid_exit_code():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "session",
         "-i", "00000000-0000-0000-0000-000000000000",
         "--json",
         "--project-path", PROJECT_PATH],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 2, (
        f"Expected exit code 2, got {result.returncode}. stderr: {result.stderr!r}"
    )
    assert "not found" in result.stderr.lower(), (
        f"Expected 'not found' in stderr, got: {result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# Test 3: malformed JSONL does not crash; valid rows are still counted
# ---------------------------------------------------------------------------
def test_malformed_jsonl_does_not_crash(capsys, tmp_path):
    import io
    from cost_from_jsonl import parse_session

    # Write a JSONL with one valid message row and one malformed line
    valid_row = {
        "message": {
            "model": "claude-sonnet-4-6",
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 500,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        }
    }
    jsonl_file = tmp_path / "00000000-0000-0000-0000-000000000001.jsonl"
    jsonl_file.write_text(
        json.dumps(valid_row) + "\n"
        "this is NOT valid json !!!!\n"
    )

    # Capture stderr
    import io as _io
    captured_stderr = _io.StringIO()
    old_stderr = sys.stderr
    sys.stderr = captured_stderr
    try:
        result = parse_session(jsonl_file)
    finally:
        sys.stderr = old_stderr

    stderr_output = captured_stderr.getvalue()

    # Valid row was counted
    assert result["totalCost"] > 0, (
        f"Expected totalCost > 0 from the valid row; got {result['totalCost']}"
    )
    # Malformed line was warned about
    assert "malformed" in stderr_output.lower() or "skipping" in stderr_output.lower(), (
        f"Expected 'malformed' or 'skipping' warning in stderr; got: {stderr_output!r}"
    )


# ---------------------------------------------------------------------------
# Test 4: unknown model does not crash; costUSD=0, stderr warning
# ---------------------------------------------------------------------------
def test_unknown_model_does_not_crash(tmp_path):
    from cost_from_jsonl import parse_session

    row = {
        "message": {
            "model": "claude-future-99",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        }
    }
    jsonl_file = tmp_path / "00000000-0000-0000-0000-000000000002.jsonl"
    jsonl_file.write_text(json.dumps(row) + "\n")

    import io as _io
    captured_stderr = _io.StringIO()
    old_stderr = sys.stderr
    sys.stderr = captured_stderr
    try:
        result = parse_session(jsonl_file)
    finally:
        sys.stderr = old_stderr

    stderr_output = captured_stderr.getvalue()

    # The unknown model entry must be present with costUSD=0
    unknown_entries = [e for e in result["entries"] if e["model"] == "claude-future-99"]
    assert len(unknown_entries) == 1, (
        f"Expected one entry for 'claude-future-99'; got entries: {result['entries']}"
    )
    assert unknown_entries[0]["costUSD"] == 0.0, (
        f"Expected costUSD=0 for unknown model; got {unknown_entries[0]['costUSD']}"
    )
    # A stderr warning must have been emitted
    assert "unknown" in stderr_output.lower() or "claude-future-99" in stderr_output, (
        f"Expected unknown-model warning in stderr; got: {stderr_output!r}"
    )


# ---------------------------------------------------------------------------
# Test 5: project_hash empirical transform — spaces → '-', broader than slash-only
# ---------------------------------------------------------------------------
def test_project_hash_function():
    from cost_from_jsonl import project_hash

    # (a) Spaces are replaced with '-' (NOT preserved)
    # A slash-only implementation would return "-Users-x-My Drive" (space kept).
    # The correct empirical rule replaces ALL non-[A-Za-z0-9-] chars, including space.
    assert project_hash("/Users/x/My Drive") == "-Users-x-My-Drive", (
        "project_hash must replace spaces with '-' (empirical rule, not slash-only). "
        "Got: " + repr(project_hash("/Users/x/My Drive"))
    )

    # (b) Full developer-machine path — golden value verified 2026-04-27 by
    #     `ls ~/.claude/projects/ | grep Claude-workflow` on the dev machine.
    full_path = (
        "/Users/ivgo/Library/CloudStorage/"
        "GoogleDrive-ivan.gorban@gmail.com/"
        "My Drive/Storage/Claude_workflow"
    )
    expected = (
        "-Users-ivgo-Library-CloudStorage-"
        "GoogleDrive-ivan-gorban-gmail-com-"
        "My-Drive-Storage-Claude-workflow"
    )
    assert project_hash(full_path) == expected, (
        f"project_hash golden value mismatch.\n"
        f"  got:      {project_hash(full_path)!r}\n"
        f"  expected: {expected!r}"
    )


# ---------------------------------------------------------------------------
# IVG-249 T-09: Claude 5 PRICES entries — pin the exact four rates each
# ---------------------------------------------------------------------------
def test_prices_claude5_entries_pinned():
    from cost_from_jsonl import PRICES

    expected = {
        "claude-fable-5":   {"input": 10.00, "output": 50.00,
                              "cache_create": 12.50, "cache_read": 1.00},
        "claude-opus-5":    {"input": 5.00, "output": 25.00,
                              "cache_create": 6.25, "cache_read": 0.50},
        "claude-sonnet-5":  {"input": 2.00, "output": 10.00,
                              "cache_create": 2.50, "cache_read": 0.20},
        "claude-haiku-4-5": {"input": 1.00, "output": 5.00,
                              "cache_create": 1.25, "cache_read": 0.10},
    }
    rate_fields = ("input", "output", "cache_create", "cache_read")
    for slug, rates in expected.items():
        assert slug in PRICES, f"{slug} missing from PRICES"
        # Compare the four rate fields explicitly (not the whole dict) — the
        # entry also carries "src"/"verified" provenance fields (IVG-260).
        got_rates = {k: PRICES[slug][k] for k in rate_fields}
        assert got_rates == rates, (
            f"{slug} rate mismatch: got {got_rates}, expected {rates}"
        )


def test_last_updated_pinned():
    from cost_from_jsonl import LAST_UPDATED

    assert LAST_UPDATED == "2026-09-08", (
        f"LAST_UPDATED mismatch: got {LAST_UPDATED!r}"
    )


# ---------------------------------------------------------------------------
# IVG-249 T-09: parse_session's additive unknown_models/priceable keys
# ---------------------------------------------------------------------------
def test_parse_session_known_model_is_priceable(tmp_path):
    from cost_from_jsonl import parse_session

    row = {
        "message": {
            "model": "claude-sonnet-4-6",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        }
    }
    jsonl_file = tmp_path / "00000000-0000-0000-0000-000000000003.jsonl"
    jsonl_file.write_text(json.dumps(row) + "\n")

    result = parse_session(jsonl_file)
    assert result["priceable"] is True, (
        f"Expected priceable=True for a known-model session; got {result['priceable']}"
    )
    assert result["unknown_models"] == [], (
        f"Expected unknown_models=[] for a known-model session; got {result['unknown_models']}"
    )


def test_parse_session_unknown_model_is_not_priceable_and_warns(tmp_path):
    from cost_from_jsonl import parse_session

    row = {
        "message": {
            "model": "claude-imaginary-9",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        }
    }
    jsonl_file = tmp_path / "00000000-0000-0000-0000-000000000004.jsonl"
    jsonl_file.write_text(json.dumps(row) + "\n")

    import io as _io
    captured_stderr = _io.StringIO()
    old_stderr = sys.stderr
    sys.stderr = captured_stderr
    try:
        result = parse_session(jsonl_file)
    finally:
        sys.stderr = old_stderr

    assert result["priceable"] is False, (
        f"Expected priceable=False for an unknown-model session; got {result['priceable']}"
    )
    assert result["unknown_models"] == ["claude-imaginary-9"], (
        f"Expected unknown_models=['claude-imaginary-9']; got {result['unknown_models']}"
    )
    stderr_output = captured_stderr.getvalue()
    assert "claude-imaginary-9" in stderr_output, (
        f"Expected the deduped unknown-model WARN naming the slug; got: {stderr_output!r}"
    )


# ---------------------------------------------------------------------------
# IVG-260 T-10: negative path and provenance
# ---------------------------------------------------------------------------
def test_live_non_anthropic_slug_is_never_priceable(tmp_path):
    """deepseek/deepseek-v4-pro (a real, live CCR-routed slug — 1075 rows
    across sampled transcripts) must never resolve to a price, and the
    deduplicated stderr warning must fire exactly once for two rows of the
    same slug."""
    from cost_from_jsonl import parse_session

    row = {
        "message": {
            "model": "deepseek/deepseek-v4-pro",
            "usage": {
                "input_tokens": 100, "output_tokens": 50,
                "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
            },
        }
    }
    jsonl_file = tmp_path / "00000000-0000-0000-0000-000000000010.jsonl"
    jsonl_file.write_text((json.dumps(row) + "\n") * 2)

    import io as _io
    captured_stderr = _io.StringIO()
    old_stderr = sys.stderr
    sys.stderr = captured_stderr
    try:
        result = parse_session(jsonl_file)
    finally:
        sys.stderr = old_stderr

    assert result["totalCost"] == 0.0
    assert result["unknown_models"] == ["deepseek/deepseek-v4-pro"]
    assert result["priceable"] is False
    stderr_output = captured_stderr.getvalue()
    assert stderr_output.count("deepseek/deepseek-v4-pro") == 1, (
        f"Expected exactly one deduped warning; got: {stderr_output!r}"
    )


def test_one_rewrite_away_slug_stays_unpriced():
    """claude-opus-4-5-legacy (the fixture at
    test_backfill_cost_attribution.py:109) sits exactly one banned rewrite
    away from the newly-added claude-opus-4-5 key. Under the two-rule
    resolver (trailing [1m], trailing -DDDDDDDD) it stays a miss — neither
    rule matches "-legacy"."""
    from cost_from_jsonl import resolve_prices

    assert resolve_prices("claude-opus-4-5-legacy") is None


def test_one_rewrite_away_slug_session_not_priceable(tmp_path):
    from cost_from_jsonl import parse_session

    row = {
        "message": {
            "model": "claude-opus-4-5-legacy",
            "usage": {
                "input_tokens": 100, "output_tokens": 50,
                "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
            },
        }
    }
    jsonl_file = tmp_path / "00000000-0000-0000-0000-000000000011.jsonl"
    jsonl_file.write_text(json.dumps(row) + "\n")
    result = parse_session(jsonl_file)
    assert result["totalCost"] == 0.0
    assert result["unknown_models"] == ["claude-opus-4-5-legacy"]
    assert result["priceable"] is False


def test_every_prices_entry_has_provenance():
    import re as _re
    from cost_from_jsonl import PRICES

    for model, entry in PRICES.items():
        src = entry.get("src", "")
        assert isinstance(src, str) and src.startswith("http"), (
            f"{model}: 'src' missing or not an http(s) URL: {src!r}"
        )
        verified = entry.get("verified", "")
        assert _re.match(r"^\d{4}-\d{2}-\d{2}$", verified or ""), (
            f"{model}: 'verified' missing or not YYYY-MM-DD: {verified!r}"
        )


# ---------------------------------------------------------------------------
# IVG-260 T-11: the <synthetic> sentinel through cost_from_jsonl's own reader
# ---------------------------------------------------------------------------
_SYNTHETIC_ROW = {
    "message": {
        "model": "<synthetic>",
        "usage": {
            "input_tokens": 0, "output_tokens": 0,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
        },
    }
}


def test_synthetic_only_session_is_priceable_and_free(tmp_path):
    from cost_from_jsonl import parse_session

    jsonl_file = tmp_path / "00000000-0000-0000-0000-000000000012.jsonl"
    jsonl_file.write_text(json.dumps(_SYNTHETIC_ROW) + "\n")

    import io as _io
    captured_stderr = _io.StringIO()
    old_stderr = sys.stderr
    sys.stderr = captured_stderr
    try:
        result = parse_session(jsonl_file)
    finally:
        sys.stderr = old_stderr

    assert result["totalCost"] == 0.0
    assert result["entries"] == []
    assert result["unknown_models"] == []
    assert result["priceable"] is True
    assert result["sentinel_rows"] == 1
    assert captured_stderr.getvalue() == ""


def test_mixed_synthetic_and_real_session_stays_priceable(tmp_path):
    from cost_from_jsonl import parse_session

    real_row = {
        "message": {
            "model": "claude-sonnet-4-6",
            "usage": {
                "input_tokens": 100, "output_tokens": 50,
                "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
            },
        }
    }
    jsonl_file = tmp_path / "00000000-0000-0000-0000-000000000013.jsonl"
    jsonl_file.write_text(json.dumps(_SYNTHETIC_ROW) + "\n" + json.dumps(real_row) + "\n")

    result = parse_session(jsonl_file)
    assert result["priceable"] is True
    assert len(result["entries"]) == 1
    assert result["sentinel_rows"] == 1


# ---------------------------------------------------------------------------
# IVG-260 T-09: newly-priced model IDs and normalization
# ---------------------------------------------------------------------------
def _one_row_jsonl(tmp_path, model, usage, name="00000000-0000-0000-0000-000000000099.jsonl"):
    row = {"message": {"model": model, "usage": usage}}
    jsonl_file = tmp_path / name
    jsonl_file.write_text(json.dumps(row) + "\n")
    return jsonl_file


_USAGE_BLOCK = {
    "input_tokens": 1000,
    "output_tokens": 500,
    "cache_creation_input_tokens": 200,
    "cache_read_input_tokens": 300,
}


@pytest.mark.parametrize(
    "model",
    ["claude-fable-5-1", "claude-opus-4-6", "claude-opus-4-5", "claude-sonnet-4-5"],
)
def test_newly_priced_model_is_costable_and_priceable(tmp_path, model):
    from cost_from_jsonl import parse_session

    jsonl_file = _one_row_jsonl(tmp_path, model, _USAGE_BLOCK)
    result = parse_session(jsonl_file)
    assert result["totalCost"] > 0, (
        f"Expected totalCost > 0 for newly-priced model {model!r}; got {result['totalCost']}"
    )
    assert result["unknown_models"] == [], (
        f"Expected no unknown_models for {model!r}; got {result['unknown_models']}"
    )
    assert result["priceable"] is True, (
        f"Expected priceable=True for {model!r}; got {result['priceable']}"
    )


def test_bracketed_1m_slug_prices_same_as_base_slug():
    from cost_from_jsonl import cost_for_entry

    # Defensive normalization only — no live transcript row carries a
    # bracketed slug (message.model is never bracketed across 82 sampled
    # transcripts; "[1m]" appears only in toolUseResult.resolvedModel /
    # fallbackModel, fields the cost path never reads). This fixture is
    # synthetic by design.
    assert cost_for_entry("claude-opus-5[1m]", _USAGE_BLOCK) == cost_for_entry(
        "claude-opus-5", _USAGE_BLOCK
    )


def test_dated_snapshot_slug_prices_same_as_base_slug():
    from cost_from_jsonl import cost_for_entry

    assert cost_for_entry(
        "claude-sonnet-4-6-20260101", _USAGE_BLOCK
    ) == cost_for_entry("claude-sonnet-4-6", _USAGE_BLOCK)


def test_dated_haiku_alias_byte_identity_cost():
    """claude-haiku-4-5-20251001 must price at its OWN entry's rates, not the
    bare claude-haiku-4-5 alias's (they carry equal rates today, so this only
    discriminates exact-match-first ordering via the hardcoded expectation
    below, not via a value that happens to differ)."""
    from cost_from_jsonl import cost_for_entry, PRICES

    own_rates = PRICES["claude-haiku-4-5-20251001"]
    assert own_rates == {
        "input": 1.00, "output": 5.00, "cache_create": 1.25, "cache_read": 0.10,
        "src": own_rates["src"], "verified": own_rates["verified"],
    }
    expected = round(
        (1000 * 1.00 + 500 * 5.00 + 200 * 1.25 + 300 * 0.10) / 1_000_000.0, 6
    )
    cost, _ = cost_for_entry("claude-haiku-4-5-20251001", _USAGE_BLOCK)
    assert round(cost, 6) == expected


def test_dated_haiku_alias_object_identity():
    """The dated entry and the bare alias hold equal rates but are distinct
    dict objects — `is` (unlike `==`) genuinely discriminates exact-match-first
    from normalize-first resolver ordering."""
    from cost_from_jsonl import resolve_prices, PRICES

    assert resolve_prices("claude-haiku-4-5-20251001") is PRICES["claude-haiku-4-5-20251001"]


def test_sentinel_set_is_exactly_one_non_model_string():
    from cost_from_jsonl import PRICES, NON_MODEL_SENTINELS, is_costable

    assert len(NON_MODEL_SENTINELS) == 1
    for key in PRICES:
        if key.startswith("claude-"):
            assert is_costable(key), f"{key} should be costable"
    # Cheap partial fix (round-1 MIN-2): expressible against the
    # pre-existing PRICES symbol alone, so it carries behavioural content
    # even before is_costable/NON_MODEL_SENTINELS existed.
    assert "<synthetic>" not in PRICES


# ---------------------------------------------------------------------------
# IVG-249 T-09: no test in the suite may pin a CLOSED PRICES key set — the new
# Claude 5 keys (and any future addition) must not break a test that enumerates
# PRICES exhaustively (e.g. `len(PRICES) == N` or `set(PRICES.keys()) == {...}`).
# ---------------------------------------------------------------------------
def test_no_closed_prices_key_set_enumeration_in_suite():
    tests_dir = pathlib.Path(__file__).parent
    closed_set_re = re.compile(
        r'len\(\s*(?:cfj\.|_cfj\.|sm\.)?PRICES\s*\)\s*==|'
        r'set\(\s*(?:cfj\.|_cfj\.|sm\.)?PRICES(?:\.keys\(\))?\s*\)\s*==|'
        r'(?:cfj\.|_cfj\.|sm\.)?PRICES\.keys\(\)\s*==\s*\{'
    )
    offenders = []
    for py_file in tests_dir.glob("test_*.py"):
        if py_file.name == "test_cost_from_jsonl.py":
            continue  # this file's own PRICES membership checks are per-key, not closed-set
        text = py_file.read_text(encoding="utf-8")
        if closed_set_re.search(text):
            offenders.append(py_file.name)
    assert not offenders, (
        f"Found test(s) that pin a CLOSED PRICES key set — these break every time "
        f"a slug is added: {offenders}"
    )
