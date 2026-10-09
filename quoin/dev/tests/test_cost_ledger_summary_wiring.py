"""The finalize and /run cost paths call cost_ledger_summary.py and report estimates."""
from __future__ import annotations

import pathlib

REPO = pathlib.Path(__file__).resolve().parents[3]
SKILLS = REPO / "quoin" / "adapters" / "claude" / "skills"
EOT = (SKILLS / "end_of_task" / "SKILL.md").read_text(encoding="utf-8")
RUN = (SKILLS / "run" / "SKILL.md").read_text(encoding="utf-8")
FORMAT = (REPO / "quoin" / "memory" / "cost-ledger-format.md").read_text(encoding="utf-8")


def _item4() -> str:
    i = EOT.index("    4. Cost aggregation")
    return EOT[i : EOT.index("    5. Write `.workflow_artifacts/<task_name>/cost-summary.json`", i)]


def test_end_of_task_item4_runs_summary_script_with_project_path():
    item = _item4()
    assert "cost_ledger_summary.py --ledger" in item
    assert '--project-path "$(pwd)"' in item
    assert "fall back to the manual procedure" in item
    for step in ("a2.", "b. For each REMAINING", "c. Fallback", "d. Aggregate"):
        assert step in item


def test_run_inline_finish_points_at_summary_script():
    i = RUN.index("### end_of_task failure recovery (inline finish)")
    section = RUN[i : RUN.index("After completion, present the final report", i)]
    assert "cost_ledger_summary.py" in section
    assert '--project-path "$(pwd)"' in section
    assert "Sub-phase B manual steps" in section


def test_run_report_has_estimated_wording():
    assert "partial — $Y.YY estimated from token counts" in RUN


def test_run_report_nothing_priced_prints_totals_unavailable():
    assert "`priced_rows` and `estimated_count` both present and both 0" in RUN
    assert "totals unavailable — nothing could be priced" in RUN


def test_end_of_task_phase_table_has_estimated_line():
    assert "Estimated from token counts (not in the phase rows)" in EOT
    assert "(grand_total)" in EOT


def test_cost_ledger_format_documents_summary_keys():
    for key in ("estimated_total", "priced_rows", "phases_excluded", "grand_total",
                "re-priced at aggregation time"):
        assert key in FORMAT
