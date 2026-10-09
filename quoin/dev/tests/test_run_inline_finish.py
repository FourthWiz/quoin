"""IVG-249 S-03 T-06: behavior/fixture assertions for /run's Phase 6
"end_of_task failure recovery (inline finish)" contract (D-05/D-2).

Preference for behavior/fixture assertions over wording pins (lesson
2026-08-07): the col-8 precedence and partial-labeling round-trip are
exercised through the REAL reader (cost_event.parse_row / classify_attribution)
and the REAL normalizer (cost_summary.normalize_total), not re-implemented
here. The three ordering assertions on run/SKILL.md's Phase 6 span are the
exception — they pin POSITION (before archive), not prose.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]  # quoin/ repo root
RUN_SKILL = REPO_ROOT / "quoin" / "adapters" / "claude" / "skills" / "run" / "SKILL.md"
THOROUGH_PLAN_SKILL = REPO_ROOT / "quoin" / "adapters" / "claude" / "skills" / "thorough_plan" / "SKILL.md"
ARCHITECT_SKILL = REPO_ROOT / "quoin" / "adapters" / "claude" / "skills" / "architect" / "SKILL.md"
REVIEW_SKILL = REPO_ROOT / "quoin" / "adapters" / "claude" / "skills" / "review" / "SKILL.md"
COST_LEDGER_FORMAT = REPO_ROOT / "quoin" / "memory" / "cost-ledger-format.md"
COST_EVENT_PATH = REPO_ROOT / "quoin" / "core" / "scripts" / "cost_event.py"
COST_SUMMARY_PATH = REPO_ROOT / "quoin" / "core" / "scripts" / "cost_summary.py"

ZSH_AVAILABLE = shutil.which("zsh") is not None


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


cost_event = _load_module("_quoin_test_cost_event", COST_EVENT_PATH)
cost_summary = _load_module("_quoin_test_cost_summary", COST_SUMMARY_PATH)


# ---------------------------------------------------------------------------
# col-8 precedence -> resolved_total / unresolvable_count -> cost-summary.json
# -> normalize_total partial-label round-trip
# ---------------------------------------------------------------------------


def _build_ledger(tmp_path: Path) -> Path:
    ledger = tmp_path / "cost-ledger.md"
    rows = [
        # 6-col legacy row (no attribution) — parses, attribution == "".
        "uuid-legacy-1 | 2026-08-14 | plan | opus | task | \"legacy 6-col row\"",
        # 7-col row (fallback_fires present, no attribution) — still legacy.
        "uuid-legacy-2 | 2026-08-14 | critic | opus | task | \"legacy 7-col row\" | 0",
        # 8-col resolved row — inline usd, priced from nested JSONL.
        "uuid-resolved-1 | 2026-08-14 | implement | sonnet | task | \"resolved 8-col row\" | 0 | usd=0.0123;tok=45210;src=nested_jsonl",
        # 8-col unresolvable row — src=unresolved, must NOT be folded into a $0.
        "uuid-unresolved-1 | 2026-08-14 | review | opus | task | \"unresolvable 8-col row\" | 0 | src=unresolved",
    ]
    ledger.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return ledger


def _resolve_session_cost(uuid: str):
    # Stand-in session-cost source for the two legacy (col-8-empty) rows.
    known = {"uuid-legacy-1": (0.50, True), "uuid-legacy-2": (0.75, True)}
    return known.get(uuid, (0.0, False))


def test_col8_precedence_yields_resolved_total_excluding_unresolved_row(tmp_path):
    ledger = _build_ledger(tmp_path)
    events = [
        cost_event.parse_row(line, source=str(ledger), lineno=i)
        for i, line in enumerate(ledger.read_text(encoding="utf-8").splitlines(), start=1)
    ]
    events = [e for e in events if e is not None]
    assert len(events) == 4

    # Classify each row's attribution directly (col-8 precedence contract).
    verdicts = {e.uuid: cost_event.classify_attribution(e.attribution) for e in events}
    assert verdicts["uuid-legacy-1"] == ("legacy", None)
    assert verdicts["uuid-legacy-2"] == ("legacy", None)
    assert verdicts["uuid-resolved-1"] == ("resolved", 0.0123)
    assert verdicts["uuid-unresolved-1"] == ("unresolvable", None)

    result = cost_event.cohort_attribution(events, _resolve_session_cost)
    assert result is not None
    # resolved_total = 0.50 (legacy-1) + 0.75 (legacy-2) + 0.0123 (resolved-1);
    # the unresolvable row contributes nothing (never folded into a $0).
    assert round(result.resolved_total, 4) == round(0.50 + 0.75 + 0.0123, 4)
    assert result.unresolvable_count == 1


def test_partial_label_round_trip_through_cost_summary_json(tmp_path):
    ledger = _build_ledger(tmp_path)
    events = [
        e
        for e in (
            cost_event.parse_row(line, source=str(ledger), lineno=i)
            for i, line in enumerate(ledger.read_text(encoding="utf-8").splitlines(), start=1)
        )
        if e is not None
    ]
    result = cost_event.cohort_attribution(events, _resolve_session_cost)
    assert result is not None and result.unresolvable_count > 0

    summary_path = tmp_path / "cost-summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "resolved_total": result.resolved_total,
                "grand_total": result.resolved_total,
                "unresolvable_count": result.unresolvable_count,
                "fallback_used": True,
                "fallback_note": "inline-finish: 1 unresolvable row",
            }
        ),
        encoding="utf-8",
    )
    data = json.loads(summary_path.read_text(encoding="utf-8"))
    total, is_partial = cost_summary.normalize_total(data)
    assert total == result.resolved_total
    assert is_partial is True


def test_clean_case_no_unresolvable_rows_is_not_partial(tmp_path):
    summary_path = tmp_path / "cost-summary-clean.json"
    summary_path.write_text(
        json.dumps({"grand_total": 1.25, "unresolvable_count": 0, "fallback_used": False}),
        encoding="utf-8",
    )
    data = json.loads(summary_path.read_text(encoding="utf-8"))
    total, is_partial = cost_summary.normalize_total(data)
    assert total == 1.25
    assert is_partial is False


def test_absent_null_total_case(tmp_path):
    summary_path = tmp_path / "cost-summary-null.json"
    summary_path.write_text(json.dumps({}), encoding="utf-8")
    data = json.loads(summary_path.read_text(encoding="utf-8"))
    total, is_partial = cost_summary.normalize_total(data)
    assert total is None
    assert is_partial is False


def test_run_skill_renders_totals_unavailable_for_null_case():
    # Contract pairing: the null/missing-total case above must correspond to a
    # documented "totals unavailable" rendering in run/SKILL.md's Phase 6 span.
    text = RUN_SKILL.read_text(encoding="utf-8")
    assert "totals unavailable" in text


# ---------------------------------------------------------------------------
# Three ordering assertions inside the Phase 6 span (round-2 MAJ-3 rewrite):
# each its own assert so a dropped subsection FAILs rather than degrading to
# vacuity.
# ---------------------------------------------------------------------------


def _phase6_span() -> str:
    text = RUN_SKILL.read_text(encoding="utf-8")
    start = text.index("## Phase 6 — End of Task")
    after = text[start:]
    m = re.search(r"^## ", after[len("## Phase 6 — End of Task"):], re.MULTILINE)
    end = len("## Phase 6 — End of Task") + (m.start() if m else len(after))
    return after[:end]


def test_recovery_heading_present_in_phase6_span():
    span = _phase6_span()
    assert "### end_of_task failure recovery (inline finish)" in span


def test_cost_summary_json_appears_after_recovery_heading_not_in_pre_existing_prose():
    span = _phase6_span()
    heading_idx = span.index("### end_of_task failure recovery (inline finish)")
    cost_summary_idx = span.index("cost-summary.json")
    assert cost_summary_idx > heading_idx


def test_cost_summary_json_appears_before_archived_literal():
    span = _phase6_span()
    assert span.index("cost-summary.json") < span.index("Archived:")


# ---------------------------------------------------------------------------
# IVG-249 S-03 T-08: shared on-behalf post-check (F-17/F-16 fold) — normalized
# predicate equivalence across all four copies, plus a shell-execution harness
# (/bin/sh + zsh) for both the post-check predicate and the three amended
# self-write one-liner forms (6-, 7-, 8-col empty-UUID guard, T-08b).
# ---------------------------------------------------------------------------

EXPECTED_NORMALIZED_PREDICATE = (
    '{ [ -n "$AID" ] && grep -qF -e "$AID | " -- "$LEDGER" 2>/dev/null; } || \\'
)

_PREDICATE_LINE_RE = re.compile(r'^.*grep -qF -e "\$AID \| " -- .*\|\| \\\s*$')


def _extract_predicate_line(text: str, *, is_architect: bool) -> str:
    for line in text.splitlines():
        if _PREDICATE_LINE_RE.match(line):
            stripped = line.strip()
            if is_architect:
                assert stripped.startswith("#"), stripped
                stripped = stripped.lstrip("#").strip()
            return stripped
    raise AssertionError("post-check predicate line not found in text")


def _review_ledger_slice() -> str:
    text = REVIEW_SKILL.read_text(encoding="utf-8")
    i = text.index("**Ledger:**")
    return text[i : text.index("**Merge:**", i)]


def test_normalized_post_check_predicate_equal_across_all_five_copies():
    """The fifth copy is the review fan-out's on-behalf block, so it cannot drift."""
    run_pred = _extract_predicate_line(RUN_SKILL.read_text(encoding="utf-8"), is_architect=False)
    tp_pred = _extract_predicate_line(THOROUGH_PLAN_SKILL.read_text(encoding="utf-8"), is_architect=False)
    arch_pred = _extract_predicate_line(ARCHITECT_SKILL.read_text(encoding="utf-8"), is_architect=True)
    fmt_pred = _extract_predicate_line(COST_LEDGER_FORMAT.read_text(encoding="utf-8"), is_architect=False)

    assert run_pred == EXPECTED_NORMALIZED_PREDICATE
    assert tp_pred == EXPECTED_NORMALIZED_PREDICATE
    assert arch_pred == EXPECTED_NORMALIZED_PREDICATE
    assert fmt_pred == EXPECTED_NORMALIZED_PREDICATE
    review_pred = _extract_predicate_line(_review_ledger_slice(), is_architect=False)
    assert review_pred == EXPECTED_NORMALIZED_PREDICATE


def _run_shell(shell: str, script: str, env: dict) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        [shell, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )


_POST_CHECK_SCRIPT = (
    '{ [ -n "$AID" ] && grep -qF -e "$AID | " -- "$LEDGER" 2>/dev/null; } '
    "&& echo MATCH || echo NOMATCH"
)


def _run_post_check(shell: str, aid: str, ledger_path: Path) -> str:
    env = dict(os.environ)
    env["AID"] = aid
    env["LEDGER"] = str(ledger_path)
    result = _run_shell(shell, _POST_CHECK_SCRIPT, env)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _assert_post_check_scenarios(tmp_path: Path, shell: str) -> None:
    # AID present, but NOT on the last line — proves the whole-file grep (F-17)
    # is order-insensitive, unlike the old `tail -1 | grep` it replaces.
    present = tmp_path / "ledger-present.md"
    present.write_text(
        'AID-123 | 2026-08-14 | implement | sonnet | task | "row"\n'
        'uuid-x | 2026-08-14 | plan | opus | task | "row"\n',
        encoding="utf-8",
    )
    # AID well-formed but genuinely absent from the file.
    absent = tmp_path / "ledger-absent-aid.md"
    absent.write_text('uuid-x | 2026-08-14 | plan | opus | task | "row"\n', encoding="utf-8")
    missing = tmp_path / "ledger-does-not-exist.md"

    assert _run_post_check(shell, "AID-123", present) == "MATCH"
    assert _run_post_check(shell, "AID-999-not-present", absent) == "NOMATCH"
    assert _run_post_check(shell, "", present) == "NOMATCH"  # empty AID, F-16
    assert _run_post_check(shell, "AID-123", missing) == "NOMATCH"  # ledger absent
    # Leading-dash AID must not be parsed as a grep option (review round 1
    # MINOR 2, IVG-249 S-03): the old unguarded `grep -qF "$AID | " "$LEDGER"`
    # let AID="-e" combine with the residual " | " as an attached -e pattern
    # argument, matching any pipe-delimited row and producing a false MATCH
    # that suppressed the fallback row. `-e "$AID | " --` fixes this — "-e"
    # genuinely absent from `absent` must still read NOMATCH.
    assert _run_post_check(shell, "-e", absent) == "NOMATCH"


def test_post_check_predicate_scenarios_sh(tmp_path):
    _assert_post_check_scenarios(tmp_path, "/bin/sh")


@pytest.mark.skipif(not ZSH_AVAILABLE, reason="zsh not available on this system")
def test_post_check_predicate_scenarios_zsh(tmp_path):
    _assert_post_check_scenarios(tmp_path, "zsh")


def _self_write_blocks() -> list[str]:
    """Extract the three self-write ```bash code blocks (7-, 8-, 6-col) from
    cost-ledger-format.md verbatim, so the harness executes the REAL contract
    text rather than a hand-duplicated copy that could drift out of sync."""
    text = COST_LEDGER_FORMAT.read_text(encoding="utf-8")
    blocks = re.findall(r"```bash\n(.*?)\n```", text, re.DOTALL)
    # Order in the file: [0] 7-col self-write, [1] 8-col self-write,
    # [2] on-behalf if-block (post-check, covered separately above), [3] 6-col
    # self-write.
    assert len(blocks) == 4, f"expected 4 bash blocks, found {len(blocks)}"
    return [blocks[0], blocks[1], blocks[3]]


def _make_fake_python3(tmp_path: Path, mode: str) -> Path:
    """A stub `python3` on PATH standing in for get_session_uuid.py: `mode ==
    "normal"` emits a UUID and exits 0; `mode == "empty"` emits nothing and
    exits 0 (the empty-but-successful-lookup leg that only T-08b's
    `${uuid:-...}` guard — not the `|| echo` fallback, which only fires on a
    non-zero exit — can catch)."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "python3"
    if mode == "normal":
        body = "#!/bin/sh\necho real-uuid-1234\nexit 0\n"
    elif mode == "empty":
        body = "#!/bin/sh\nexit 0\n"
    else:
        raise ValueError(mode)
    fake.write_text(body, encoding="utf-8")
    fake.chmod(0o755)
    return bindir


_FALLBACK_ROW_RE = re.compile(r"^unknown-PHASE-\d{8}T\d{6}Z \| ")


def _assert_selfwrite_lands_exactly_one_row(tmp_path: Path, shell: str, block_idx: int, mode: str) -> None:
    script = _self_write_blocks()[block_idx]
    ledger = tmp_path / f"cost-ledger-{shell.lstrip('/').replace('/', '-')}-{block_idx}-{mode}.md"
    fakebin = _make_fake_python3(tmp_path, mode)

    env = dict(os.environ)
    env["PATH"] = f"{fakebin}{os.pathsep}{env.get('PATH', '')}"
    env["LEDGER"] = str(ledger)

    result = _run_shell(shell, script, env)
    assert result.returncode == 0, result.stderr

    lines = [line for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 1, f"expected exactly one row, got {lines!r}"
    row = lines[0]
    if mode == "normal":
        assert row.startswith("real-uuid-1234 | "), row
    else:
        assert _FALLBACK_ROW_RE.match(row), row


_SELFWRITE_SCENARIOS = [
    (0, "normal"),
    (0, "empty"),
    (1, "normal"),
    (1, "empty"),
    (2, "normal"),
    (2, "empty"),
]


@pytest.mark.parametrize("block_idx,mode", _SELFWRITE_SCENARIOS)
def test_selfwrite_oneliner_lands_exactly_one_row_sh(tmp_path, block_idx, mode):
    _assert_selfwrite_lands_exactly_one_row(tmp_path, "/bin/sh", block_idx, mode)


@pytest.mark.skipif(not ZSH_AVAILABLE, reason="zsh not available on this system")
@pytest.mark.parametrize("block_idx,mode", _SELFWRITE_SCENARIOS)
def test_selfwrite_oneliner_lands_exactly_one_row_zsh(tmp_path, block_idx, mode):
    _assert_selfwrite_lands_exactly_one_row(tmp_path, "zsh", block_idx, mode)
