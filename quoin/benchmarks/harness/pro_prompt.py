"""
pro_prompt.py — Prompt for SWE-bench Pro tasks.

The text is Scale's standard V2 instruction (`instruction.md` in each task
directory of scaleapi/SWE-bench_Pro-os): a fixed preamble, the PR
description holding the problem statement, the requirements and the new
interfaces, and the constraints line. The only change is the repository
location: Scale's agent works in the image at `/app`, ours in a host checkout,
so the preamble names the working directory instead. Nothing about the hidden
tests (names, files, fail-to-pass or pass-to-pass lists) is included, and the
instance id is left out so neither arm is handed a search key for the fix.
"""

from __future__ import annotations

PRO_PROMPT_TEMPLATE_VERSION = "pro-v1"

UPSTREAM_PREAMBLE = (
    "A code repository is available in the `/app` directory. Your task is to implement "
    "the changes necessary to satisfy the following Pull Request (PR) description:"
)
PREAMBLE = (
    "A code repository is available in the current working directory. Your task is to "
    "implement the changes necessary to satisfy the following Pull Request (PR) description:"
)
CONSTRAINTS = (
    "Constraints:\n"
    "* Do not reference, look up, or copy existing solutions, external PRs, or online "
    "workarounds. The implementation must be entirely your own independent work."
)


def render_instruction(problem_statement: str, requirements: str, interface: str,
                       preamble: str = PREAMBLE) -> str:
    """Scale's instruction layout; with UPSTREAM_PREAMBLE it is byte-identical to instruction.md."""
    return (
        f"{preamble}\n\n<pr_description>\n{problem_statement.strip()}\n\n"
        f"## Requirements\n\n{requirements.strip()}\n\n"
        f"## New Interfaces\n\n{interface.strip()}\n</pr_description>\n\n{CONSTRAINTS}\n"
    )


def build_pro_prompt(task_spec: dict) -> str:
    fields = [task_spec.get(k) for k in ("description", "requirements", "interface")]
    if not all(isinstance(v, str) and v.strip() for v in fields):
        raise ValueError("a Pro task needs description, requirements and interface")
    return render_instruction(*fields)
