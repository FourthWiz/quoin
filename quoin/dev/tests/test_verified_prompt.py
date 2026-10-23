"""Prompt for Verified tasks and the compatibility of the new harness modules."""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from quoin.benchmarks.harness import repo_prep, session_state, verified_prompt
from quoin.benchmarks.harness.cells import quoin_claude, simple_claude
from quoin.benchmarks.harness.verified_prompt import (
    VERIFIED_PROMPT_TEMPLATE_VERSION, build_verified_prompt,
)

BENCH = Path(repo_prep.__file__).resolve().parent.parent
SPEC = {
    "id": "verified_easy_000", "source": "swebench_verified",
    "source_id": "django__django-11119", "repo": "django/django",
    "base_commit": "d4df5e1b0b1c643fe0fc521add0236764ec8e92a",
    "description": "Engine.render_to_string() should honor autoescape\nDetails.\n",
}


@pytest.mark.parametrize("module", [simple_claude, quoin_claude])
def test_cell_prompts_for_verified(module):
    prompt = module._build_prompt(SPEC)
    assert SPEC["description"] in prompt
    assert SPEC["base_commit"] in prompt
    assert SPEC["source_id"] not in prompt


def test_other_sources_are_unchanged():
    lite = {"source": "swebench_lite", "source_id": "x__y-1", "description": "D"}
    assert simple_claude._build_prompt(lite) == (
        "Fix the following GitHub issue from the SWE-bench Lite benchmark.\n\n"
        "Instance ID: x__y-1\nDescription: D\n\n"
        "Implement the fix in the repository. When done, your changes will be "
        "evaluated by the SWE-bench harness."
    )
    he = {"source": "evalplus_humaneval_plus", "source_id": "HumanEval/1", "description": "D"}
    assert simple_claude._build_prompt(he).startswith("Solve the following HumanEval+")


def test_template_version_matches_suite():
    suite = json.loads((BENCH / "suite-tiered-small.json").read_text(encoding="utf-8"))
    assert suite["tiered"]["prompt_template_version"] == VERIFIED_PROMPT_TEMPLATE_VERSION


@pytest.mark.parametrize("missing", ["repo", "base_commit", "description"])
def test_missing_field_raises(missing):
    with pytest.raises(ValueError):
        build_verified_prompt(dict(SPEC, **{missing: ""}))


@pytest.mark.parametrize("module", [repo_prep, session_state, verified_prompt])
def test_modules_parse_as_py38_and_defer_annotations(module):
    src = Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src, feature_version=(3, 8))
    first = [n for n in tree.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))][0]
    assert isinstance(first, ast.ImportFrom) and first.module == "__future__"
    assert first.names[0].name == "annotations"
