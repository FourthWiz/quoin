"""test_onbehalf_default_on.py — IVG-249 stage 2 (S-02) tests for default-ON
on-behalf cost capture.

S-02 flips `QUOIN_INLINE_COST_CAPTURE` from opt-in (`=1`) to default-ON
(opt-out via `=0`) across `run/SKILL.md`, `thorough_plan/SKILL.md`,
`architect/SKILL.md`, and `quoin/memory/cost-ledger-format.md`. These tests
cover the two BEHAVIOR acceptance criteria the wording-only guard tests
(T-06/T-07) cannot: AC-1 (priced col-8 agentId rows survive
`cohort_attribution` end to end, real sidecar chain) and AC-8 (mixed
legacy/col-8 ledger rows attribute without double-counting or silent
zeroing), plus the WORDING acceptance criterion AC-2 (the opt-out warning
fires only on explicit `=0`, per decision D-2 in stage-2/current-plan.md).

Reuses two established idioms verbatim (per T-08):
- The importlib `spec_from_file_location` loader for `cost_event.py`, from
  `test_cohort_attribution.py` (L20-36).
- The `sys.path.insert` + `import agent_transcript_cost as atc` idiom and the
  module-scoped `fixtures_home` fixture (materializing the committed fixture
  tree under `tmp_path/.claude/projects/...`), from
  `test_agent_transcript_cost.py` (L47-49, L68-78).

RG-CENSUS safety: no skill-name/phase collection here is a module-level
ALL-CAPS roster — phase names appear only as function-local literals inside
each test body (same constraint documented in
`test_onbehalf_writer_predicate.py`'s header).
"""
from __future__ import annotations

import importlib.util
import pathlib
import shutil
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]  # quoin/ repo root
CORE_SCRIPTS_DIR = pathlib.Path(__file__).parent.parent.parent / "core" / "scripts"
SCRIPTS_DIR = pathlib.Path(__file__).parent.parent.parent / "scripts"

_CE_PATH = CORE_SCRIPTS_DIR / "cost_event.py"
_SPEC = importlib.util.spec_from_file_location(
    "_quoin_onbehalf_default_on_cost_event", _CE_PATH
)
_CE = importlib.util.module_from_spec(_SPEC)
sys.modules["_quoin_onbehalf_default_on_cost_event"] = _CE
_SPEC.loader.exec_module(_CE)

parse_row = _CE.parse_row
format_row = _CE.format_row
cohort_attribution = _CE.cohort_attribution
CostEvent = _CE.CostEvent

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
import agent_transcript_cost as atc  # noqa: E402

FIXTURES_SRC_PROJECTS = (
    pathlib.Path(__file__).parent / "fixtures" / "agent_transcript_cost" / "projects"
)

FAKE_PROJECT_PATH = "/fake/project"
FAKE_SID = "sess-test-uuid-001"

# Expected prices per PRICES table (quoin/quoin/scripts/cost_from_jsonl.py) —
# mirrors test_agent_transcript_cost.py's EXPECTED_USD/EXPECTED_TOK formula
# verbatim (primary001 fixture: opus row 1000in/200out + sonnet row
# 500in/100out).
EXPECTED_USD = round(
    (1000 * 5.00 + 200 * 25.00) / 1_000_000.0
    + (500 * 3.00 + 100 * 15.00) / 1_000_000.0,
    6,
)
EXPECTED_TOK = 1000 + 200 + 500 + 100

# claude5resolvable001 fixture — mirrors test_agent_transcript_cost.py's
# EXPECTED_CLAUDE5_USD/EXPECTED_CLAUDE5_TOK formula verbatim.
EXPECTED_CLAUDE5_USD = round(
    (1000 * 2.00 + 500 * 10.00 + 200 * 0.20) / 1_000_000.0
    + (2000 * 5.00 + 300 * 25.00 + 400 * 0.50) / 1_000_000.0,
    6,
)
EXPECTED_CLAUDE5_TOK = (1000 + 500 + 200) + (2000 + 300 + 400)


@pytest.fixture(scope="module")
def fixtures_home(tmp_path_factory):
    """Materialize the committed fixture tree under <tmp>/.claude/projects/...
    — identical idiom to test_agent_transcript_cost.py's fixture, given its
    own tmp_path root so the two test modules never share state."""
    home = tmp_path_factory.mktemp("onbehalf_default_on_home")
    dest = home / ".claude" / "projects"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(FIXTURES_SRC_PROJECTS, dest)
    return home


class _CallRecorder:
    """Call-recording resolver stub for `cohort_attribution`'s
    `resolve_session_cost` parameter. Records every uuid it is invoked with
    (`call_count`/`calls`) and returns a fixed `(cost, has_cost)` result.

    `raises=True` makes it ALSO raise `AssertionError` on invocation — kept
    as decoration only (round-4 MIN-2 fix): `_cohort_resolve_safe`
    (cost_event.py) wraps the resolver call in a bare `except Exception:`
    and fails open to `(0.0, False)`, so a raised AssertionError is silently
    swallowed rather than surfacing as a test failure. The LOAD-BEARING
    guard is `call_count`, not the raise.
    """

    def __init__(self, *, result=(0.0, False), raises=False):
        self.call_count = 0
        self.calls: list[str] = []
        self._result = result
        self._raises = raises

    def __call__(self, uuid):
        self.call_count += 1
        self.calls.append(uuid)
        if self._raises:
            raise AssertionError("resolver must not be called")
        return self._result


# ---------------------------------------------------------------------------
# Test 8a (AC-1, BEHAVIOR, real end-to-end chain)
# ---------------------------------------------------------------------------
def test_priced_agentid_rows_survive_cohort_attribution_end_to_end(fixtures_home):
    jf_primary = atc.resolve_by_agent_id(
        sid=FAKE_SID, agent_id="primary001",
        project_path=FAKE_PROJECT_PATH, home=fixtures_home,
    )
    assert jf_primary is not None
    r_primary = atc.price_agent_jsonl(jf_primary)
    assert r_primary["priceable"] is True
    assert r_primary["usd"] == pytest.approx(EXPECTED_USD, rel=1e-9)
    assert r_primary["tok"] == EXPECTED_TOK
    primary_attr = f"usd={r_primary['usd']};tok={r_primary['tok']};src=nested_jsonl"

    jf_claude5 = atc.resolve_by_agent_id(
        sid=FAKE_SID, agent_id="claude5resolvable001",
        project_path=FAKE_PROJECT_PATH, home=fixtures_home,
    )
    assert jf_claude5 is not None
    r_claude5 = atc.price_agent_jsonl(jf_claude5)
    assert r_claude5["priceable"] is True
    assert r_claude5["usd"] == pytest.approx(EXPECTED_CLAUDE5_USD, rel=1e-9)
    assert r_claude5["tok"] == EXPECTED_CLAUDE5_TOK
    claude5_attr = f"usd={r_claude5['usd']};tok={r_claude5['tok']};src=nested_jsonl"

    event_primary = CostEvent(
        uuid="primary001", date="2026-08-14", phase="architect",
        model_or_effort="opus", category="task",
        note="on-behalf: architect via /run", fallback_fires=0,
        attribution=primary_attr,
    )
    event_claude5 = CostEvent(
        uuid="claude5resolvable001", date="2026-08-14", phase="review",
        model_or_effort="opus", category="task",
        note="on-behalf: review via /run", fallback_fires=0,
        attribution=claude5_attr,
    )

    row_primary = parse_row(format_row(event_primary))
    row_claude5 = parse_row(format_row(event_claude5))
    assert row_primary is not None
    assert row_claude5 is not None

    resolver = _CallRecorder(raises=True)
    result = cohort_attribution([row_primary, row_claude5], resolver)

    assert resolver.call_count == 0
    assert result.shared_bucket == {}
    assert result.by_phase["architect"]["cost"] == pytest.approx(r_primary["usd"])
    assert result.by_phase["architect"]["count"] == 1
    assert result.by_phase["review"]["cost"] == pytest.approx(r_claude5["usd"])
    assert result.by_phase["review"]["count"] == 1
    assert result.resolved_total == pytest.approx(r_primary["usd"] + r_claude5["usd"])
    assert result.unresolvable_count == 0


# ---------------------------------------------------------------------------
# Test 8b (AC-1 negative control — keeps 8a non-vacuous)
# ---------------------------------------------------------------------------
def test_shared_parent_uuid_no_attribution_is_the_regressed_shape():
    phases = ["discover", "architect", "thorough-plan", "implement", "review"]
    rows = [
        {
            "uuid": "shared-parent-001",
            "date_str": "2026-08-14",
            "phase": phase,
            "model": "opus",
            "note": f"on-behalf: {phase} via /run",
            "attribution": "",
        }
        for phase in phases
    ]

    resolver = _CallRecorder(result=(42.0, True))
    result = cohort_attribution(rows, resolver)

    assert result.shared_bucket != {}
    assert result.shared_bucket["uuids"] == 1
    assert result.by_phase == {}


# ---------------------------------------------------------------------------
# Test 8c (AC-2, WORDING — D-2's explicit-opt-out reconciliation)
# ---------------------------------------------------------------------------
def test_run_setup_warns_on_explicit_optout():
    skill_path = (
        REPO_ROOT / "quoin" / "adapters" / "claude" / "skills" / "run" / "SKILL.md"
    )
    text = skill_path.read_text(encoding="utf-8")

    start = text.index("## Setup")
    end = text.index("## Perfectionist depth-within-profile")
    assert start < end
    region = text[start:end]

    required_tokens = [
        "QUOIN_INLINE_COST_CAPTURE=0",
        "per-phase cost attribution",
        "parent /run session UUID",
        "unset the variable to restore attribution",
        "Unset or any value other than `0` means capture is ON; "
        "no warning is emitted.",
    ]
    for token in required_tokens:
        assert token in region, f"missing required Setup-region token: {token!r}"


# ---------------------------------------------------------------------------
# Test 8d (AC-8 cohort half, BEHAVIOR — no double-count, no silent zeroing)
# ---------------------------------------------------------------------------
def test_mixed_legacy_and_col8_ledger_no_double_count_no_zeroing():
    lines = []
    # (i) three 7-col legacy rows sharing uuid parent-A — discover/architect/review
    for phase in ("discover", "architect", "review"):
        lines.append(
            f"parent-A | 2026-08-14 | {phase} | opus | task | "
            f"on-behalf: {phase} via /run | 0"
        )
    # (ii) one 6-col solo legacy row — implement
    lines.append("solo-B | 2026-08-14 | implement | sonnet | task | solo legacy row")
    # (iii) three 8-col rows, distinct agentIds, src=nested_jsonl — plan/critic/revise
    for phase, agent_id in (
        ("plan", "agent-plan-001"),
        ("critic", "agent-critic-001"),
        ("revise", "agent-revise-001"),
    ):
        lines.append(
            f"{agent_id} | 2026-08-14 | {phase} | opus | task | "
            f"on-behalf: {phase} via /run | 0 | usd=2.0;tok=100;src=nested_jsonl"
        )
    # (iv) one 8-col src=unresolved row — enrich
    lines.append(
        "agent-enrich-001 | 2026-08-14 | enrich | opus | task | "
        "on-behalf: enrich via /run | 0 | src=unresolved"
    )

    rows = [parse_row(line) for line in lines]
    assert all(r is not None for r in rows)

    resolver = _CallRecorder(result=(10.0, True))
    result = cohort_attribution(rows, resolver)

    assert resolver.call_count == 2
    assert sorted(resolver.calls) == ["parent-A", "solo-B"]
    assert result.shared_bucket["cost"] == pytest.approx(10.0)
    assert result.shared_bucket["uuids"] == 1
    assert result.by_phase["implement"]["cost"] == pytest.approx(10.0)
    assert result.by_phase["plan"]["cost"] == pytest.approx(2.0)
    assert result.by_phase["critic"]["cost"] == pytest.approx(2.0)
    assert result.by_phase["revise"]["cost"] == pytest.approx(2.0)
    for phase in ("discover", "architect", "review"):
        assert phase not in result.by_phase
    assert result.resolved_total == pytest.approx(26.0)
    assert result.unresolvable_count == 1
