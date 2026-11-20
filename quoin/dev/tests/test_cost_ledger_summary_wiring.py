"""The finalize and /run cost paths call cost_ledger_summary.py and report estimates."""
from __future__ import annotations

import pathlib

REPO = pathlib.Path(__file__).resolve().parents[3]
SKILLS = REPO / "quoin" / "adapters" / "claude" / "skills"
EOT = (SKILLS / "end_of_task" / "SKILL.md").read_text(encoding="utf-8")
RUN = (SKILLS / "run_hard" / "SKILL.md").read_text(encoding="utf-8")
FORMAT = (REPO / "quoin" / "memory" / "cost-ledger-format.md").read_text(encoding="utf-8")


def _item4() -> str:
    i = EOT.index("    4. Cost aggregation")
    return EOT[i : EOT.index("    5. Write `.workflow_artifacts/<task_name>/cost-summary.json`", i)]


def test_cost_ledger_format_documents_summary_keys():
    for key in ("estimated_total", "priced_rows", "phases_excluded", "grand_total",
                "re-priced at aggregation time"):
        assert key in FORMAT
