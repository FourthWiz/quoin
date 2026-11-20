"""Generate repo-local Codex assets for a Quoin project.

Produces AGENTS.md at <project-root>/AGENTS.md. All writes are repo-local.
No global Codex paths or install registries are touched.

Usage:
    python3 generate_codex_assets.py --project-root <path>
    python3 generate_codex_assets.py --project-root <path> --check
    python3 generate_codex_assets.py --project-root <path> --adapter-assets --check
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
# quoin/adapters/codex/ -> up 2 levels -> quoin/ package directory
QUOIN_PKG_DIR = SCRIPT_DIR.parent.parent
SKILLS_JSON = QUOIN_PKG_DIR / "core" / "workflow" / "skills.json"
CORE_SKILLS_DIR = QUOIN_PKG_DIR / "core" / "skills"


def _load_skills() -> List[dict]:
    """Load portable skill metadata from skills.json."""
    with SKILLS_JSON.open(encoding="utf-8") as f:
        data = json.load(f)
    return data.get("skills", [])


def _all_skill_names(skills: Iterable[dict]) -> List[str]:
    return [skill["name"] for skill in skills]


def _render_skill_table(skills: List[dict]) -> str:
    """Render a phase-grouped skill reference table."""
    rows = []
    for skill in skills:
        if not skill.get("user_facing", False):
            continue
        name = skill["name"]
        phase = skill.get("phase", "")
        effort = skill.get("effort", "")
        rows.append(f"| `{name}` | {phase} | {effort} |")
    table = "| Skill | Phase | Effort |\n|-------|-------|--------|\n"
    table += "\n".join(rows)
    return table


def render_agents_md(skills: List[dict]) -> str:
    """Render the AGENTS.md content from portable skill metadata."""
    skill_table = _render_skill_table(skills)
    return f"""\
## Purpose
This repository contains Quoin, a workflow-memory toolkit. Its core value is the
artifact-centric workflow system, not any single coding agent runtime.

## Architectural intent
- Maintain one repository with a shared portable core plus thin runtime adapters.
- Preserve shared workflow semantics: `.workflow_artifacts/`, planning/review
  artifacts, stage-aware task structure, lessons learned, session handoff, and
  cost ledger.
- Do not duplicate Codex-native functionality such as approvals, sandboxing, or
  repo-scoped instruction handling.
- Prefer configuration and adapter boundaries over vendor-specific branching
  scattered throughout the codebase.

## Workflow conventions
Quoin stores all planning and review artifacts under `.workflow_artifacts/` at the
project root. Use this layout:

```
.workflow_artifacts/
  <task-name>/
    architecture.md
    current-plan.md
    cost-ledger.md
    review-1.md
  memory/
    lessons-learned.md
    sessions/
      <YYYY-MM-DD>-<task-name>-codex.md
    daily/
```

Ask for Quoin workflow phases in natural language, for example:
- "Use Quoin to create an architecture artifact for this task."
- "Use Quoin to write a current plan under `.workflow_artifacts/`."
- "Use Quoin to review this implementation against the current plan."
- "Update Quoin session handoff and lessons learned."

## Portable skill reference
User-facing workflow skills with effort levels (from `quoin/core/workflow/skills.json`):

{skill_table}

Codex performs each phase natively using its own planning and reasoning capabilities.
Use native Codex behavior for planning, progress tracking, approvals, sandboxing,
repo-scoped instructions, and model or reasoning controls.
No Claude slash-command compatibility is required or implied.

## Codex workflow procedures
Repo-local Codex execution procedures live under `quoin/adapters/codex/`:

- `quoin/adapters/codex/workflow.md` covers the practical workflow loop:
  `discover -> plan -> implement -> review -> gate`.
- `quoin/adapters/codex/procedures/discover.md`
- `quoin/adapters/codex/procedures/plan.md`
- `quoin/adapters/codex/procedures/implement.md`
- `quoin/adapters/codex/procedures/review.md`
- `quoin/adapters/codex/procedures/gate.md`

These procedure docs link to the portable contracts under `quoin/core/skills/`
and use project-root `.workflow_artifacts/`. They are not Codex command files
or global install behavior.

## Codex session handoff
For continuation across Codex sessions, write handoff state under
`.workflow_artifacts/memory/sessions/<YYYY-MM-DD>-<task-name>-codex.md`.
Use `quoin/adapters/codex/handoff.md` for the required shape and validate it
with:

```
python3 quoin/adapters/codex/validate_codex_handoff.py --project-root . --file .workflow_artifacts/memory/sessions/<date>-<task>-codex.md
```

The handoff must summarize current task status, unfinished work, decisions,
finalized artifact paths, continuation context, lesson candidates, and cost
recording status. This is repo-local validation, not live Codex hook
automation.

## Codex cost events
When a task context exists, Codex can append a portable cost-ledger row with:

```
python3 quoin/adapters/codex/cost_event.py write --project-root . --task <task> --phase <phase> --effort <low|medium|high|max|unknown>
python3 quoin/adapters/codex/cost_event.py validate --project-root . --task <task> --expect-codex
```

This records known local values such as runtime, task, phase, timestamp,
session id when supplied, and effort. Codex token counts and dollar costs are
not exposed through a verified repository interface, so the adapter records
those telemetry fields as `not_available` rather than estimating them.

## Refactor guidance
- Separate portable workflow logic from Claude-specific runtime integration.
- Keep Claude-specific assumptions isolated in the Claude adapter (`quoin/adapters/claude/`).
- Build only thin Codex adapter scaffolding unless repository evidence supports more.
- Do not invent unverified Codex local install paths or packaging details.
- If a runtime detail is uncertain, define an interface, placeholder, or documentation
  note instead of hardcoding a guess.

## Editing principles
- Prefer incremental refactors over broad renames.
- Preserve backward compatibility where reasonable.
- Minimize duplication of templates, scripts, and rules.
- Keep documentation honest about implemented vs planned behavior.

## Discovery map (structured project index)

The portable generator `quoin/scripts/generate_discovery_map.py` produces a structured
`discovery-map.json` index at `<project_root>/.workflow_artifacts/discovery-map.json`.
Codex can run it directly without any global-path assumption:

- `python3 quoin/scripts/generate_discovery_map.py "$PROJECT_ROOT" --quiet`

The generator is optional; `discover` MUST NOT fail if it errors.

## Validation
- Run relevant checks after making changes.
- Report exactly which checks were run and which were not.
- Key validation commands:
  ```
  python3 -m pytest quoin/dev/tests/
  python3 quoin/adapters/codex/verify_codex_readiness.py --project-root .
  python3 quoin/adapters/codex/smoke_codex_workflow.py --project-root .
  ```
"""


def _render_unsupported_translations() -> str:
    return """\
# Unsupported Claude-Only Translations

Codex adapter files document Quoin's portable workflow behavior for Codex. They
do not translate Claude runtime mechanics into Codex behavior.

Unsupported translations:

- Claude slash-command invocation is not a Codex command system.
- Claude skill frontmatter and model tier names are not Codex packaging or model
  selection rules.
- Claude subagent dispatch prompts are not Codex adapter requirements.
- Claude prompt-cache preambles are not generated for Codex.
- Claude session-log, usage, pricing, and cost-capture plumbing are not
  translated into Codex behavior. Codex cost rows use
  `quoin/adapters/codex/cost_event.py` and mark unavailable telemetry as
  `not_available`.
- Claude installer routing is not reused for Codex.

Codex should use native planning, progress tracking, approvals, sandboxing,
repo-scoped instructions, and model or reasoning controls. If a Codex runtime
extension point is later verified, it should be added as a new adapter contract
instead of inferred from Claude behavior.
"""


def _render_codex_skill_doc(skill: dict) -> str:
    name = skill["name"]
    phase = skill.get("phase", "")
    effort = skill.get("effort", "")
    user_facing = "yes" if skill.get("user_facing", False) else "no"
    core_doc = f"quoin/core/skills/{name}.md"
    return f"""\
# {name} Codex Adapter

Generated/scaffolded from portable Quoin metadata.

Portable source:

- `{core_doc}`
- `quoin/core/workflow/skills.json`

## Codex invocation

Ask for this workflow phase in natural language. Codex does not get a generated
command file for `{name}` in this phase.

## Portable workflow contract

Follow the runtime-neutral contract in `{core_doc}`. Preserve Quoin artifact
semantics under the project-root `.workflow_artifacts/` directory:

- phase: `{phase}`
- effort: `{effort}`
- user-facing: `{user_facing}`

Use `quoin/core/workflow/` for shared task layout, session state, cost-ledger,
artifact, and skill metadata rules.

## Codex runtime notes

- Treat the repository root containing `AGENTS.md` as the Quoin project root.
- Read and write workflow artifacts at that project root, even when editing code
  in a nested package.
- Use Codex-native planning, progress tracking, approvals, sandboxing,
  repo-scoped instructions, and model or reasoning controls.
- Do not create a Codex global install, command file, approval layer, sandbox
  layer, or model-dispatch mechanism from this adapter file.

## Unsupported Claude-only translations

This adapter file intentionally does not translate Claude runtime mechanics:

- Claude slash-command invocation for this skill is unsupported in Codex.
- Claude skill frontmatter and model tier routing are not Codex packaging.
- Claude subagent dispatch and prompt-cache preamble behavior are not Codex
  requirements.
- Claude session-log and cost-capture plumbing are not implemented for Codex.
- Claude installer routing is not reused for Codex.

See `quoin/adapters/codex/unsupported-claude-behavior.md` for the shared
unsupported-behavior contract.
"""


def _render_codex_skill_index(skills: List[dict]) -> str:
    rows = []
    for skill in skills:
        name = skill["name"]
        phase = skill.get("phase", "")
        effort = skill.get("effort", "")
        user_facing = "yes" if skill.get("user_facing", False) else "no"
        rows.append(f"| [`{name}`]({name}/README.md) | {phase} | {effort} | {user_facing} |")
    table = "| Skill | Phase | Effort | User-facing |\n|-------|-------|--------|-------------|\n"
    table += "\n".join(rows)
    return f"""\
# Codex Skill Adapter Docs

These files are Codex facing docs generated/scaffolded from Quoin's portable
skill contracts under `quoin/core/skills/` and metadata in
`quoin/core/workflow/skills.json`.

They are repo-local adapter docs only. They do not define Codex command files,
global install paths, approval behavior, sandbox behavior, or model-dispatch
mechanics.

{table}
"""


def render_codex_adapter_assets(skills: List[dict]) -> Dict[Path, str]:
    """Render Codex adapter docs keyed by paths relative to adapters/codex."""
    assets: Dict[Path, str] = {
        Path("skills") / "README.md": _render_codex_skill_index(skills),
        Path("unsupported-claude-behavior.md"): _render_unsupported_translations(),
    }
    for skill in skills:
        assets[Path("skills") / skill["name"] / "README.md"] = _render_codex_skill_doc(skill)
    return assets


def _check_core_skill_docs(skills: List[dict]) -> List[str]:
    missing = []
    for name in _all_skill_names(skills):
        if not (CORE_SKILLS_DIR / f"{name}.md").is_file():
            missing.append(name)
    return missing


def _write_or_check_assets(
    assets: Dict[Path, str],
    output_root: Path,
    check: bool,
) -> Tuple[bool, List[str]]:
    messages: List[str] = []
    ok = True

    for rel_path, content in sorted(assets.items(), key=lambda item: str(item[0])):
        path = output_root / rel_path
        if check:
            if not path.is_file():
                messages.append(f"FAIL: {path} does not exist")
                ok = False
                continue
            if path.read_text(encoding="utf-8") != content:
                messages.append(f"FAIL: {path} is out of date")
                ok = False
            else:
                messages.append(f"OK: {path} is up to date")
            continue

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        messages.append(f"Written: {path}")

    return ok, messages


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-root",
        required=True,
        help="Project root directory; AGENTS.md is written here.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Compare rendered output against existing files; exit nonzero on drift.",
    )
    parser.add_argument(
        "--adapter-assets",
        action="store_true",
        help=(
            "Also generate/check Codex adapter docs under adapters/codex. "
            "This is a repo-development output, not a global Codex install."
        ),
    )
    parser.add_argument(
        "--adapter-root",
        default=str(SCRIPT_DIR),
        help="Codex adapter root for --adapter-assets; defaults to this script directory.",
    )
    args = parser.parse_args()

    project_root = Path(args.project_root).resolve()
    if not project_root.is_dir():
        print(f"error: --project-root {project_root} is not a directory", file=sys.stderr)
        return 1

    skills = _load_skills()
    missing_core_docs = _check_core_skill_docs(skills)
    if missing_core_docs:
        print(f"error: missing core skill docs for: {', '.join(missing_core_docs)}", file=sys.stderr)
        return 1

    rendered = render_agents_md(skills)
    output_path = project_root / "AGENTS.md"
    adapter_root = Path(args.adapter_root).resolve()

    if args.check:
        ok = True
        if not output_path.exists():
            print(f"FAIL: {output_path} does not exist (run without --check to generate)", file=sys.stderr)
            ok = False
        else:
            current = output_path.read_text(encoding="utf-8")
            if current == rendered:
                print(f"OK: {output_path} is up to date")
            else:
                print(f"FAIL: {output_path} is out of date (run without --check to regenerate)", file=sys.stderr)
                ok = False

        if args.adapter_assets:
            asset_ok, messages = _write_or_check_assets(
                render_codex_adapter_assets(skills),
                adapter_root,
                check=True,
            )
            ok = ok and asset_ok
            for message in messages:
                stream = sys.stdout if message.startswith("OK:") else sys.stderr
                print(message, file=stream)

        return 0 if ok else 1

    output_path.write_text(rendered, encoding="utf-8")
    print(f"Written: {output_path}")

    if args.adapter_assets:
        if not adapter_root.is_dir():
            print(f"error: --adapter-root {adapter_root} is not a directory", file=sys.stderr)
            return 1
        _, messages = _write_or_check_assets(
            render_codex_adapter_assets(skills),
            adapter_root,
            check=False,
        )
        for message in messages:
            print(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
