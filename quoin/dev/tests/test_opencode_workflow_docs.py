"""The shipped OpenCode prose says what the coordinator does, and does not
overclaim: no model-diversity wording, every workflow flag and exit code named,
residual notes current."""
from __future__ import annotations

import io
import re
from pathlib import Path

import pytest

from quoin.opencode_adapter import install

REPO_ROOT = Path(__file__).resolve().parents[3]
ADAPTER = REPO_ROOT / "quoin" / "adapters" / "opencode"
README = (ADAPTER / "README.md").read_text(encoding="utf-8")
DECISIONS = (ADAPTER / "decisions.md").read_text(encoding="utf-8")
WORKFLOW_FLAGS = (
    "--workflow", "--continue", "--no-pause", "--through", "--from-discover", "--max-critic-rounds",
    "--test-command", "--test-include", "--test-timeout", "--rerun-from", "--adopt",
)
TEXT_SUFFIXES = (".md", ".json", ".txt", ".tmpl", ".template", ".yaml", ".yml")


def shipped_text_files():
    roots = [ADAPTER, REPO_ROOT / "quoin" / "docs"]
    for root in roots:
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix in TEXT_SUFFIXES and "fixtures" not in path.parts:
                yield path


def test_no_shipped_doc_template_or_overlay_claims_model_diversity():
    pattern = re.compile(r"model\s+diversity", re.IGNORECASE)
    offenders = [str(p.relative_to(REPO_ROOT)) for p in shipped_text_files()
                 if pattern.search(p.read_text(encoding="utf-8"))]
    assert offenders == []


def test_no_rendered_file_claims_model_diversity(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()
    code = install.run_install(str(project), REPO_ROOT / "quoin", None, False, io.StringIO(), io.StringIO())
    assert code == 0
    pattern = re.compile(r"model\s+diversity", re.IGNORECASE)
    offenders = [str(p.relative_to(project)) for p in sorted((project / ".opencode").rglob("*"))
                 if p.is_file() and pattern.search(p.read_text(encoding="utf-8", errors="replace"))]
    assert offenders == []


@pytest.mark.parametrize("flag", WORKFLOW_FLAGS)
def test_the_readme_names_every_workflow_flag(flag):
    assert flag in README


def test_the_readme_names_the_new_exit_codes_and_sections():
    assert "## Workflow coordinator" in README and "## Deterministic gate" in README
    assert "## Continuation record" in README
    assert re.search(r"`GATE_REFUSED`\s+7", README) and re.search(r"`GATE_ARTIFACT_FAILED`\s+8", README)
    assert "finding-superseded" in README and "run-closed" in README
    assert "tests-settings-changed" in README


def test_decisions_no_longer_defer_whole_task_runs():
    block = DECISIONS.split("### Whole-task runs on OpenCode", 1)[1].split("###", 1)[0]
    assert "Status: decided" in block and "deferred" not in block
    assert "--workflow" in block


def test_decisions_keep_the_settings_hand_edit_and_privilege_lines():
    assert "hand-edited in the workflow record always refuse" in DECISIONS
    assert "runs implementer-written code" in DECISIONS and "`--test-command` is the operator's opt-in" in DECISIONS
    assert "A closed-run rerun that re-reports its stored violation has no dedicated" not in DECISIONS
    assert "window store" in DECISIONS and "lag" in DECISIONS and "drain window" in DECISIONS


def test_the_compatibility_audit_cites_existing_rows():
    text = (ADAPTER / "compatibility.md").read_text(encoding="utf-8")
    section = text.split("## Whole-task coordinator reliance", 1)[1]
    keys = set(re.findall(r"`([a-z0-9-]+)`", section))
    for key in ("headless-deny-rules", "continuation-flags", "task-sync-output-to-parent",
                "continuation-agent", "continuation-no-replay", "child-events-filtered"):
        assert key in keys
        assert "`key: %s`" % key in text.split("## Whole-task coordinator reliance", 1)[0]
