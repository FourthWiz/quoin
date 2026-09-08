"""test_agent_transcript_cost.py — fixture-only tests for agent_transcript_cost.py
(stage 2 of ivg-111-cost-attribution: nested subagent-transcript resolver + pricer).

All tests point resolvers at a SYNTHETIC fixture tree materialized under
<tmp_path>/.claude/projects/<hash>/<sid>/subagents/ (copied at test time from the
committed fixtures/agent_transcript_cost/projects/ source tree) via the
home=/project_path= override params — nothing here reads the developer's live
~/.claude (lessons 2026-06-16: subagent transcripts are not resolvable/portable
across machines, so tests must be fixture-only).

Note on fixture layout: the committed source tree under fixtures/agent_transcript_cost/
deliberately has NO literal ".claude" path segment, because quoin/.gitignore:3 ignores
any ".claude/" directory anywhere in the repo — a committed ".claude"-named fixture tree
would be silently invisible in a clean checkout (caught by
test_gitignore_no_source_shadow.py::test_no_shadowed_sources_in_quoin_dev). The
FIXTURES_HOME fixture below copies the source tree into a tmp_path-rooted
"<tmp>/.claude/projects/..." tree at test time instead, mirroring the established
pattern in test_dashboard_cost.py.

Cases (mirrors stage-2/current-plan.md T-07):
  (a) PRIMARY hit — resolve_by_agent_id + resolve_attribution -> nested_jsonl, priced
  (b) MODEL-LESS row — priceable=False path exercised via the model-less guard
  (c) MALFORMED-TOLERANCE — one junk line does not flip an otherwise-priceable transcript
  (d) FLUSH-guard -> unresolved — truncated last line
  (e) TOOLUSE secondary — resolve by tool_use_id when agent_id is omitted
  (f) MISSING transcript — absent id -> src=unresolved, no crash
  (g) UNKNOWN-model -> tok kept, no usd, no crash (parse_session's stderr warning expected)
  (h) PRICE-PARITY — price_agent_jsonl matches a hand computation via cost_for_entry
  (i) SYMMETRY — core.cost_event.parse_attribution(resolve_attribution(...)) round-trips
"""

import pathlib
import shutil
import sys

import pytest

# ---------------------------------------------------------------------------
# Path resolution helpers
# ---------------------------------------------------------------------------
REPO_ROOT = pathlib.Path(__file__).parent.parent.parent.parent  # quoin/ repo root
SCRIPTS_DIR = pathlib.Path(__file__).parent.parent.parent / "scripts"  # quoin/quoin/scripts/
CORE_SCRIPTS_DIR = pathlib.Path(__file__).parent.parent.parent / "core" / "scripts"

FIXTURES_SRC_PROJECTS = pathlib.Path(__file__).parent / "fixtures" / "agent_transcript_cost" / "projects"

sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(CORE_SCRIPTS_DIR))
import agent_transcript_cost as atc  # noqa: E402
from cost_event import parse_attribution  # noqa: E402

FAKE_PROJECT_PATH = "/fake/project"
FAKE_SID = "sess-test-uuid-001"

# Expected prices per PRICES table (quoin/quoin/scripts/cost_from_jsonl.py):
#   claude-opus-4-7:   input $5.00/1M,  output $25.00/1M
#   claude-sonnet-4-6: input $3.00/1M,  output $15.00/1M
# primary/malformed/toolusehit fixtures share this shape: opus row (1000 in / 200 out)
# + sonnet row (500 in / 100 out).
EXPECTED_USD = round(
    (1000 * 5.00 + 200 * 25.00) / 1_000_000.0
    + (500 * 3.00 + 100 * 15.00) / 1_000_000.0,
    6,
)
EXPECTED_TOK = 1000 + 200 + 500 + 100


@pytest.fixture(scope="module")
def fixtures_home(tmp_path_factory):
    """Materialize the committed fixture tree under <tmp>/.claude/projects/...
    so resolver functions (which hardcode the ".claude" path segment) can read
    it via home=<this fixture>, without any ".claude"-named path ever being
    committed to the repo (see module docstring)."""
    home = tmp_path_factory.mktemp("agent_transcript_cost_home")
    dest = home / ".claude" / "projects"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(FIXTURES_SRC_PROJECTS, dest)
    return home


def _resolve(home, **kwargs):
    kwargs.setdefault("project_path", FAKE_PROJECT_PATH)
    kwargs["home"] = home
    return atc.resolve_attribution(**kwargs)


# ---------------------------------------------------------------------------
# (a) PRIMARY hit + (h) PRICE-PARITY
# ---------------------------------------------------------------------------
def test_primary_hit_priced_and_matches_hand_computation(fixtures_home):
    jf = atc.resolve_by_agent_id(
        sid=FAKE_SID, agent_id="primary001",
        project_path=FAKE_PROJECT_PATH, home=fixtures_home,
    )
    assert jf is not None
    assert jf.name == "agent-primary001.jsonl"

    r = atc.price_agent_jsonl(jf)
    assert r["priceable"] is True
    assert r["tok"] == EXPECTED_TOK
    assert abs(r["usd"] - EXPECTED_USD) / EXPECTED_USD <= 0.01  # <=1% parity

    attr = _resolve(fixtures_home, sid=FAKE_SID, agent_id="primary001")
    assert attr == f"usd={EXPECTED_USD};tok={EXPECTED_TOK};src=nested_jsonl"


def test_resolve_by_agent_id_absent_id_returns_none(fixtures_home):
    jf = atc.resolve_by_agent_id(
        sid=FAKE_SID, agent_id="not-a-real-agent-id",
        project_path=FAKE_PROJECT_PATH, home=fixtures_home,
    )
    assert jf is None


# ---------------------------------------------------------------------------
# (b) MODEL-LESS
# ---------------------------------------------------------------------------
def test_modelless_row_forces_unpriceable(fixtures_home):
    jf = atc.resolve_by_agent_id(
        sid=FAKE_SID, agent_id="modelless001",
        project_path=FAKE_PROJECT_PATH, home=fixtures_home,
    )
    assert jf is not None
    r = atc.price_agent_jsonl(jf)
    assert r["priceable"] is False
    assert r["usd"] is None
    assert r["tok"] > 0  # tok kept even though unpriceable (only the priced row's tok)

    attr = _resolve(fixtures_home, sid=FAKE_SID, agent_id="modelless001")
    assert attr.startswith("tok=")
    assert attr.endswith(";src=unresolved")
    assert "usd=" not in attr


# ---------------------------------------------------------------------------
# (c) MALFORMED-TOLERANCE
# ---------------------------------------------------------------------------
def test_malformed_line_does_not_flip_priceable_transcript(fixtures_home):
    jf = atc.resolve_by_agent_id(
        sid=FAKE_SID, agent_id="malformed001",
        project_path=FAKE_PROJECT_PATH, home=fixtures_home,
    )
    assert jf is not None
    r = atc.price_agent_jsonl(jf)
    assert r["priceable"] is True
    assert r["tok"] == EXPECTED_TOK
    assert abs(r["usd"] - EXPECTED_USD) / EXPECTED_USD <= 0.01

    attr = _resolve(fixtures_home, sid=FAKE_SID, agent_id="malformed001")
    assert attr == f"usd={EXPECTED_USD};tok={EXPECTED_TOK};src=nested_jsonl"


# ---------------------------------------------------------------------------
# (d) FLUSH-guard -> unresolved
# ---------------------------------------------------------------------------
def test_truncated_last_line_fails_flush_guard(fixtures_home):
    jf = atc.resolve_by_agent_id(
        sid=FAKE_SID, agent_id="truncated001",
        project_path=FAKE_PROJECT_PATH, home=fixtures_home,
    )
    assert jf is not None
    assert atc.last_row_usage_present(jf) is False

    attr = _resolve(fixtures_home, sid=FAKE_SID, agent_id="truncated001")
    assert attr == "src=unresolved"


def test_empty_file_fails_flush_guard(tmp_path):
    empty = tmp_path / "agent-empty.jsonl"
    empty.write_text("")
    assert atc.last_row_usage_present(empty) is False


# ---------------------------------------------------------------------------
# (e) TOOLUSE secondary
# ---------------------------------------------------------------------------
def test_tooluse_secondary_resolver_hit(fixtures_home):
    jf = atc.resolve_by_tooluse(
        sid=FAKE_SID, tool_use_id="toolu_FIXTURE_TOOLUSE_HIT_001",
        project_path=FAKE_PROJECT_PATH, home=fixtures_home,
    )
    assert jf is not None
    assert jf.name == "agent-toolusehit001.jsonl"

    attr = _resolve(fixtures_home, sid=FAKE_SID, tool_use_id="toolu_FIXTURE_TOOLUSE_HIT_001")
    assert attr == f"usd={EXPECTED_USD};tok={EXPECTED_TOK};src=nested_jsonl"


def test_tooluse_secondary_resolver_no_match(fixtures_home):
    jf = atc.resolve_by_tooluse(
        sid=FAKE_SID, tool_use_id="toolu_DOES_NOT_EXIST",
        project_path=FAKE_PROJECT_PATH, home=fixtures_home,
    )
    assert jf is None

    attr = _resolve(fixtures_home, sid=FAKE_SID, tool_use_id="toolu_DOES_NOT_EXIST")
    assert attr == "src=unresolved"


# ---------------------------------------------------------------------------
# (f) MISSING transcript
# ---------------------------------------------------------------------------
def test_missing_transcript_no_agent_id_no_tooluse_id(fixtures_home):
    attr = _resolve(fixtures_home, sid=FAKE_SID)
    assert attr == "src=unresolved"


def test_missing_transcript_unknown_agent_id_no_crash(fixtures_home):
    attr = _resolve(fixtures_home, sid=FAKE_SID, agent_id="totally-absent-id")
    assert attr == "src=unresolved"


def test_missing_transcript_unknown_sid_no_crash(fixtures_home):
    attr = _resolve(fixtures_home, sid="sess-does-not-exist", agent_id="primary001")
    assert attr == "src=unresolved"


# ---------------------------------------------------------------------------
# (g) UNKNOWN-model
# ---------------------------------------------------------------------------
def test_unknown_model_keeps_tok_no_usd_stderr_warning_expected(fixtures_home, capsys):
    jf = atc.resolve_by_agent_id(
        sid=FAKE_SID, agent_id="unknownmodel001",
        project_path=FAKE_PROJECT_PATH, home=fixtures_home,
    )
    assert jf is not None
    r = atc.price_agent_jsonl(jf)
    assert r["priceable"] is False
    assert r["usd"] is None
    assert r["tok"] == 300 + 50
    assert r["models"] == ["claude-imaginary-9"]

    attr = _resolve(fixtures_home, sid=FAKE_SID, agent_id="unknownmodel001")
    assert attr == f"tok={300 + 50};src=unresolved"

    # parse_session (reused by price_agent_jsonl) emits a stderr warning for
    # unknown models — EXPECT it; do NOT assert clean stderr (MIN-3).
    captured = capsys.readouterr()
    assert "unknown model 'claude-imaginary-9'" in captured.err


# ---------------------------------------------------------------------------
# (i) SYMMETRY — adapter output parses under the core parser
# ---------------------------------------------------------------------------
def test_symmetry_with_core_parse_attribution(fixtures_home):
    attr = _resolve(fixtures_home, sid=FAKE_SID, agent_id="primary001")
    parsed = parse_attribution(attr)
    assert parsed == {"usd": str(EXPECTED_USD), "tok": str(EXPECTED_TOK), "src": "nested_jsonl"}


def test_symmetry_unresolved_case(fixtures_home):
    attr = _resolve(fixtures_home, sid=FAKE_SID, agent_id="does-not-exist")
    parsed = parse_attribution(attr)
    assert parsed == {"src": "unresolved"}


# ---------------------------------------------------------------------------
# (j) CLAUDE-5 RESOLVABLE — IVG-249 T-08 regression test. Proves the sidecar
# root cause (unpriced Claude 5 slugs, not a broken locate path) is fixed:
# a transcript naming two newly-priced Claude 5 slugs, each with a nonzero
# cache_read_input_tokens row, now resolves to a positive usd via
# src=nested_jsonl instead of src=unresolved.
#
# Expected USD computed as NUMERIC LITERALS (mirrors the EXPECTED_USD idiom
# above) — deliberately NOT derived from cfj.PRICES, so this assertion isn't
# tautological against the same table it is meant to pin. Rates per the
# Anthropic pricing page, verified 2026-09-08:
#   claude-sonnet-5: input $2.00/1M, output $10.00/1M, cache_read $0.20/1M
#   claude-opus-5:   input $5.00/1M, output $25.00/1M, cache_read $0.50/1M
# ---------------------------------------------------------------------------
EXPECTED_CLAUDE5_USD = round(
    (1000 * 2.00 + 500 * 10.00 + 200 * 0.20) / 1_000_000.0
    + (2000 * 5.00 + 300 * 25.00 + 400 * 0.50) / 1_000_000.0,
    6,
)
EXPECTED_CLAUDE5_TOK = (1000 + 500 + 200) + (2000 + 300 + 400)


def test_claude5_resolvable_prices_to_positive_usd_via_nested_jsonl(fixtures_home):
    jf = atc.resolve_by_agent_id(
        sid=FAKE_SID, agent_id="claude5resolvable001",
        project_path=FAKE_PROJECT_PATH, home=fixtures_home,
    )
    assert jf is not None
    assert jf.name == "agent-claude5resolvable001.jsonl"

    r = atc.price_agent_jsonl(jf)
    assert r["priceable"] is True
    assert r["usd"] == pytest.approx(EXPECTED_CLAUDE5_USD, rel=1e-9)
    assert r["usd"] > 0
    assert r["tok"] == EXPECTED_CLAUDE5_TOK

    attr = _resolve(fixtures_home, sid=FAKE_SID, agent_id="claude5resolvable001")
    assert attr == f"usd={EXPECTED_CLAUDE5_USD};tok={EXPECTED_CLAUDE5_TOK};src=nested_jsonl"
    parsed = parse_attribution(attr)
    assert parsed["src"] == "nested_jsonl"
    assert float(parsed["usd"]) > 0
    assert int(parsed["tok"]) == EXPECTED_CLAUDE5_TOK


def test_claude5resolvable_fixture_tooluseid_is_distinct(fixtures_home):
    # MIN-5: every sidecar fixture under this directory must have a distinct
    # toolUseId, or resolve_by_tooluse sees two matches and returns None
    # (breaking test_tooluse_secondary_resolver_hit).
    subagents_dir = (
        fixtures_home / ".claude" / "projects" / "-fake-project" / FAKE_SID / "subagents"
    )
    import json as _json

    seen = {}
    for meta_path in sorted(subagents_dir.glob("*.meta.json")):
        tool_use_id = _json.loads(meta_path.read_text())["toolUseId"]
        assert tool_use_id not in seen, (
            f"duplicate toolUseId {tool_use_id!r} in {meta_path.name} and {seen.get(tool_use_id)}"
        )
        seen[tool_use_id] = meta_path.name
    assert "toolu_FIXTURE_CLAUDE5_RESOLVABLE_001" in seen


# ---------------------------------------------------------------------------
# subagents_dir path-building sanity
# ---------------------------------------------------------------------------
def test_subagents_dir_builds_expected_path(fixtures_home):
    d = atc.subagents_dir(project_path=FAKE_PROJECT_PATH, sid=FAKE_SID, home=fixtures_home)
    expected = fixtures_home / ".claude" / "projects" / "-fake-project" / FAKE_SID / "subagents"
    assert d == expected
    assert d.is_dir()


# ---------------------------------------------------------------------------
# fail-open on completely bogus input (never raise)
# ---------------------------------------------------------------------------
def test_resolve_attribution_never_raises_on_bogus_input(fixtures_home):
    attr = atc.resolve_attribution(
        sid=None, agent_id=None, tool_use_id=None,
        project_path=FAKE_PROJECT_PATH, home=fixtures_home,
    )
    assert attr == "src=unresolved"


def test_resolve_by_agent_id_fail_open_on_none_sid(fixtures_home):
    # sid=None makes the path-join fail internally (TypeError) — fail-open -> None.
    jf = atc.resolve_by_agent_id(
        sid=None, agent_id="primary001",
        project_path=FAKE_PROJECT_PATH, home=fixtures_home,
    )
    assert jf is None

    attr = atc.resolve_attribution(
        sid=None, agent_id="primary001",
        project_path=FAKE_PROJECT_PATH, home=fixtures_home,
    )
    assert attr == "src=unresolved"


# ---------------------------------------------------------------------------
# T-08 (stage 3, IVG-111): CLI entrypoint (T-01) behavioral tests + on-behalf
# ledger-row schema round-trip (MAJ-1) + D-10/R-11 rollout-gating invariant.
#
# CLI subprocess tests point resolve_attribution's `home=Path.home()` default at
# the fixtures tree via a HOME env override (main() takes no --home flag by
# design — mirrors get_session_uuid.py's real invocation shape).
# ---------------------------------------------------------------------------
import os
import subprocess

from cost_event import CostEvent, format_row, parse_row  # noqa: E402

CLI_SCRIPT = SCRIPTS_DIR / "agent_transcript_cost.py"


def _run_cli(fixtures_home, args):
    env = dict(os.environ)
    env["HOME"] = str(fixtures_home)
    return subprocess.run(
        [sys.executable, str(CLI_SCRIPT), *args],
        capture_output=True, text=True, env=env, timeout=30,
    )


# (a) CLI happy path
def test_cli_happy_path_priced_fixture(fixtures_home):
    result = _run_cli(fixtures_home, [
        "--sid", FAKE_SID, "--agent-id", "primary001",
        "--project-path", FAKE_PROJECT_PATH,
    ])
    assert result.returncode == 0
    assert result.stdout.strip() == f"usd={EXPECTED_USD};tok={EXPECTED_TOK};src=nested_jsonl"


# (b) deterministic agentId correlation — same agentId always resolves the exact
# agent-<id>.jsonl file (no mtime-based guessing).
def test_cli_deterministic_agent_id_correlation(fixtures_home):
    outputs = {
        _run_cli(fixtures_home, [
            "--sid", FAKE_SID, "--agent-id", "primary001",
            "--project-path", FAKE_PROJECT_PATH,
        ]).stdout.strip()
        for _ in range(3)
    }
    assert outputs == {f"usd={EXPECTED_USD};tok={EXPECTED_TOK};src=nested_jsonl"}


# (c) flush-guard via CLI — truncated last line -> src=unresolved, exit 0
def test_cli_truncated_transcript_unresolved(fixtures_home):
    result = _run_cli(fixtures_home, [
        "--sid", FAKE_SID, "--agent-id", "truncated001",
        "--project-path", FAKE_PROJECT_PATH,
    ])
    assert result.returncode == 0
    assert result.stdout.strip() == "src=unresolved"


# (d) fail-open — missing transcript / missing --sid / no args -> src=unresolved,
# exit 0, never a traceback on stdout.
def test_cli_missing_transcript_fail_open(fixtures_home):
    result = _run_cli(fixtures_home, [
        "--sid", FAKE_SID, "--agent-id", "does-not-exist",
        "--project-path", FAKE_PROJECT_PATH,
    ])
    assert result.returncode == 0
    assert result.stdout.strip() == "src=unresolved"


def test_cli_missing_required_sid_fail_open(fixtures_home):
    result = _run_cli(fixtures_home, ["--agent-id", "primary001"])
    assert result.returncode == 0
    assert result.stdout.strip() == "src=unresolved"
    assert "Traceback" not in result.stdout


def test_cli_no_args_fail_open(fixtures_home):
    result = _run_cli(fixtures_home, [])
    assert result.returncode == 0
    assert result.stdout.strip() == "src=unresolved"
    assert "Traceback" not in result.stdout


# (e) secondary key via CLI — --tool-use-id without --agent-id resolves via sidecar
def test_cli_tool_use_id_secondary_key(fixtures_home):
    result = _run_cli(fixtures_home, [
        "--sid", FAKE_SID, "--tool-use-id", "toolu_FIXTURE_TOOLUSE_HIT_001",
        "--project-path", FAKE_PROJECT_PATH,
    ])
    assert result.returncode == 0
    assert result.stdout.strip() == f"usd={EXPECTED_USD};tok={EXPECTED_TOK};src=nested_jsonl"


# (f) round-trip — CLI stdout parses cleanly through core parse_attribution
# (symmetry between the adapter CLI and cost_event.py, mirrors
# test_symmetry_with_core_parse_attribution for the in-process resolver).
def test_cli_output_parses_through_core_parse_attribution(fixtures_home):
    result = _run_cli(fixtures_home, [
        "--sid", FAKE_SID, "--agent-id", "primary001",
        "--project-path", FAKE_PROJECT_PATH,
    ])
    parsed = parse_attribution(result.stdout.strip())
    assert parsed == {"usd": str(EXPECTED_USD), "tok": str(EXPECTED_TOK), "src": "nested_jsonl"}


# (g) on-behalf row schema round-trip (MAJ-1) — a composed ledger row with
# uuid=<agentId> present parses back cleanly through cost_event.parse_row, AND
# the proc:agentid-capture fallback path (agentId capture failed -> unique
# fallback UUID, ATTR forced to src=unresolved) is still a well-formed,
# never-empty/never-malformed col-1 row.
def test_onbehalf_row_with_resolved_attribution_round_trips(fixtures_home):
    result = _run_cli(fixtures_home, [
        "--sid", FAKE_SID, "--agent-id", "primary001",
        "--project-path", FAKE_PROJECT_PATH,
    ])
    attr = result.stdout.strip()
    event = CostEvent(
        uuid="primary001", date="2026-07-25", phase="critic", model_or_effort="opus",
        category="task", note="on-behalf: critic via /architect", fallback_fires=0,
        attribution=attr,
    )
    row = format_row(event)
    reparsed = parse_row(row)
    assert reparsed == event
    assert reparsed.uuid  # never empty
    parsed_attr = parse_attribution(reparsed.attribution)
    assert parsed_attr == {"usd": str(EXPECTED_USD), "tok": str(EXPECTED_TOK), "src": "nested_jsonl"}


def test_onbehalf_row_fallback_uuid_never_malformed_on_agentid_capture_failure():
    """proc:agentid-capture fallback (R-12): when the harness doesn't surface
    agentId, AID = TUID if present, else "<parent-session-uuid>-<phase>-<utc-ts>",
    and ATTR is forced to src=unresolved (MIN-2: discard any sidecar hit). The row
    must still be present, unique, and parse cleanly — never an empty/malformed
    col-1."""
    fallback_uuid = "parent-session-uuid-1234-critic-20260725T120000Z"
    event = CostEvent(
        uuid=fallback_uuid, date="2026-07-25", phase="critic", model_or_effort="opus",
        category="task", note="on-behalf: critic via /architect (agentId capture failed)",
        fallback_fires=0, attribution="src=unresolved",
    )
    row = format_row(event)
    reparsed = parse_row(row)
    assert reparsed == event
    assert reparsed.uuid not in ("", "unknown")
    assert reparsed.uuid == fallback_uuid
    assert parse_attribution(reparsed.attribution) == {"src": "unresolved"}


# ---------------------------------------------------------------------------
# IVG-249 S-02: QUOIN_INLINE_COST_CAPTURE is default ON as of this stage — the
# stage-4 col-8-aware readers have shipped (cost_event.parse_row, analyze_cost_ledger,
# dashboard_cost, core spend_monitor, end_of_task Sub-phase B all apply col-8
# precedence), so gating the on-behalf write off by default is no longer needed.
# Opt out with QUOIN_INLINE_COST_CAPTURE=0.
# ---------------------------------------------------------------------------
def test_inline_cost_capture_flag_defaults_on_in_docs():
    """The authoritative operator doc (T-01) must carry BOTH the on-by-default
    bash idiom and the rollout note explaining col-8 readers have shipped — this
    is the doc a human reads before touching the flag."""
    memory_doc = (
        pathlib.Path(__file__).parent.parent.parent / "memory" / "cost-ledger-format.md"
    )
    text = memory_doc.read_text(encoding="utf-8")
    assert '${QUOIN_INLINE_COST_CAPTURE:-1}' in text
    assert '!= "0"' in text
    assert "default ON" in text
    assert "MUST stay OFF in production until" not in text


def test_inline_cost_capture_flag_gated_not_unconditional_in_orchestrators():
    """Each of the 3 scoped orchestrators (architect/thorough_plan/run) still
    documents the on-behalf write as FLAG-CONDITIONAL (not unconditional), even
    though the default flipped to ON — an opt-out branch (=0) must be documented,
    and the retired opt-in-only literal (=1) must be fully gone."""
    skills_dir = pathlib.Path(__file__).parent.parent.parent / "adapters" / "claude" / "skills"
    for name in ("architect", "thorough_plan", "run"):
        text = (skills_dir / name / "SKILL.md").read_text(encoding="utf-8")
        assert "QUOIN_INLINE_COST_CAPTURE" in text, f"{name}: flag must still be named"
        assert (
            "QUOIN_INLINE_COST_CAPTURE=0" in text or 'QUOIN_INLINE_COST_CAPTURE != "0"' in text
        ), f"{name}: an opt-out branch must be documented"
        assert "QUOIN_INLINE_COST_CAPTURE=1" not in text, (
            f"{name}: retired opt-in-only literal must be fully gone"
        )

    run_text = (skills_dir / "run" / "SKILL.md").read_text(encoding="utf-8")
    assert run_text.count("runs FIRST. Either way") == 6
    assert "this step is instead the on-behalf write" not in run_text
    assert "THEN verify the cost ledger has new entries for" in run_text
    assert "reverts to the pre-`IVG-249` behavior" in run_text

    tp_text = (skills_dir / "thorough_plan" / "SKILL.md").read_text(encoding="utf-8")
    assert "reverts to the pre-`IVG-249` behavior" in tp_text
    assert "FIRST; either way, THEN append" in tp_text
    assert "the on-behalf write rather than a guess" in tp_text


# ---------------------------------------------------------------------------
# IVG-260 T-11: the <synthetic> sentinel through price_agent_jsonl
# ---------------------------------------------------------------------------
def test_synthetic_only_transcript_is_priceable_via_price_agent_jsonl(tmp_path):
    import json

    row = {
        "message": {
            "model": "<synthetic>",
            "usage": {
                "input_tokens": 0, "output_tokens": 0,
                "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
            },
        }
    }
    jf = tmp_path / "00000000-0000-0000-0000-000000000014.jsonl"
    jf.write_text(json.dumps(row) + "\n")

    r = atc.price_agent_jsonl(jf)
    assert r["priceable"] is True
    assert r["usd"] == 0.0
    assert r["models"] == []
