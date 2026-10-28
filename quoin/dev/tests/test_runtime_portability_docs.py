import json
import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]


def read_rel(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def test_runtime_portability_docs_exist():
    expected = [
        "AGENTS.md",
        "quoin/docs/runtime-portability.md",
        "quoin/docs/runtime-portability-status.md",
        "quoin/docs/runtime-parity-matrix.md",
        "quoin/docs/effort-levels.md",
        "quoin/core/workflow/rules.md",
        "quoin/core/workflow/task-layout.md",
        "quoin/core/workflow/session-state.md",
        "quoin/core/workflow/cost-ledger.md",
        "quoin/core/workflow/skills.json",
        "quoin/core/workflow/skills.md",
        "quoin/core/workflow/handoff-format.md",
        "quoin/core/workflow/handoff-format-reference.md",
        "quoin/adapters/README.md",
        "quoin/adapters/claude/README.md",
        "quoin/adapters/claude/models.md",
        "quoin/adapters/codex/README.md",
        "quoin/adapters/codex/effort.md",
        "quoin/adapters/codex/setup.md",
        "quoin/adapters/codex/workflow.md",
        "quoin/adapters/codex/handoff.md",
        "quoin/adapters/codex/validate_codex_handoff.py",
        "quoin/adapters/codex/cost.md",
        "quoin/adapters/codex/cost_event.py",
        "quoin/adapters/codex/procedures/README.md",
        "quoin/adapters/codex/procedures/discover.md",
        "quoin/adapters/codex/procedures/plan.md",
        "quoin/adapters/codex/procedures/implement.md",
        "quoin/adapters/codex/procedures/review.md",
        "quoin/adapters/codex/procedures/gate.md",
        "quoin/adapters/codex/verify_codex_readiness.py",
        "quoin/adapters/codex/smoke_codex_workflow.py",
    ]
    missing = [path for path in expected if not (REPO_ROOT / path).is_file()]
    assert not missing, f"Missing runtime-portability docs: {missing}"


def test_runtime_boundary_classifies_portable_and_claude_files():
    text = read_rel("quoin/docs/runtime-portability.md")
    for portable in ["path_resolve.py", "validate_artifact.py"]:
        assert f"`{portable}` is portable" in text

    for claude_specific in [
        "cost_from_jsonl.py",
        "session_age_guard.py",
        "pollution_guard.py",
        "build_preambles.py",
    ]:
        assert f"`{claude_specific}` is Claude-specific" in text


def test_runtime_portability_docs_link_to_parity_matrix():
    for path in [
        "quoin/docs/runtime-portability.md",
        "quoin/docs/runtime-portability-status.md",
    ]:
        text = read_rel(path)
        assert "runtime-parity-matrix.md" in text


def test_runtime_parity_matrix_covers_major_semantics_and_all_migrated_skills():
    matrix = read_rel("quoin/docs/runtime-parity-matrix.md")
    manifest = json.loads(read_rel("quoin/core/workflow/skills.json"))
    skill_names = [skill["name"] for skill in manifest["skills"]]

    assert len(skill_names) == 33
    for name in skill_names:
        assert f"`{name}`" in matrix
        assert f"quoin/core/skills/{name}.md" in matrix
        entry = next(s for s in manifest["skills"] if s["name"] == name)
        row = next(line for line in matrix.splitlines() if line.startswith(f"| `{name}` |"))
        if entry.get("claude_adapter", True):
            assert f"quoin/adapters/claude/skills/{name}/SKILL.md" in matrix
        else:
            assert "not shipped on Claude Code" in row
        assert f"quoin/adapters/codex/skills/{name}/README.md" in matrix

    for semantic in [
        "Artifact layout",
        "Planning and review artifacts",
        "Memory and session handoff",
        "Codex handoff validation",
        "Cost ledger row shape",
        "Codex cost events",
        "Skill invocation",
        "Codex workflow procedures",
        "Install and setup",
        "Runtime permissions and approvals",
        "Subagents / agents",
        "Preambles and model dispatch",
        "Generated adapter coverage",
        "Smoke-test coverage",
    ]:
        assert semantic in matrix


def test_runtime_parity_matrix_does_not_overclaim_codex_support():
    matrix = read_rel("quoin/docs/runtime-parity-matrix.md")
    lower = matrix.lower()

    assert "no global codex installer is supported" in lower
    assert "codex command files" in lower
    assert "unsupported" in lower
    assert "live codex runtime execution is manual" in lower
    assert "discover-plan-implement-review-gate path" in lower
    assert "not_available" in lower

    forbidden = [
        "~/." + "codex",
        "$HOME/." + "codex",
        "/usr/local/" + "codex",
        "codex global install is supported",
        "codex command files are implemented",
    ]
    for token in forbidden:
        assert token.lower() not in lower

    for claude_only in [
        "claude slash-command invocation",
        "claude skill frontmatter",
        "section 0 model dispatch",
        "claude prompt-cache preambles",
        "claude permission files",
        "claude session-log",
        "claude installer routing",
    ]:
        assert claude_only in lower


def test_portable_scripts_have_core_implementation_and_wrappers():
    for name in [
        "path_resolve.py",
        "validate_artifact.py",
        "classify_critic_issues.py",
        "validate_adapter_drift.py",
        "handoff_validate.py",
    ]:
        core_path = REPO_ROOT / "quoin" / "core" / "scripts" / name
        wrapper_path = REPO_ROOT / "quoin" / "scripts" / name
        assert core_path.is_file(), f"Missing core script implementation: {core_path}"
        assert wrapper_path.is_file(), f"Missing compatibility wrapper: {wrapper_path}"
        assert "core\" / \"scripts" in wrapper_path.read_text(encoding="utf-8")


def test_core_workflow_docs_are_referenced_from_runtime_boundary_and_claude_rules():
    runtime = read_rel("quoin/docs/runtime-portability.md")
    claude = read_rel("quoin/CLAUDE.md")

    for name in ["rules.md", "task-layout.md", "session-state.md", "cost-ledger.md", "handoff-format.md"]:
        assert name in runtime
        assert name in claude

    assert "quoin/core/workflow/" in runtime
    assert "quoin/core/workflow/" in claude
    assert "bash quoin/install.sh" in claude
    assert "active Claude Code runtime rules file" in claude


def test_codex_handoff_and_core_spec_cross_reference():
    """The Codex session handoff and the inter-agent handoff envelope spec
    name each other, so a reader following either file finds the other.

    A bare substring check cannot catch a wrong path spelling here: the
    project-root spelling (`quoin/quoin/core/workflow/handoff-format.md`)
    contains the correct repo-root spelling as a substring, so `in` would
    pass on either. The predicate instead extracts the backticked path
    token each file cites for the other and requires it to resolve on
    disk from REPO_ROOT — a wrong spelling fails the file-existence check
    even though it would pass a containment check.
    """
    codex_handoff = read_rel("quoin/adapters/codex/handoff.md")
    core_spec = read_rel("quoin/core/workflow/handoff-format.md")

    codex_match = re.search(r"`(quoin/core/workflow/handoff-format\.md)`", codex_handoff)
    assert codex_match, "handoff.md must cite handoff-format.md by backticked repo-root path"
    assert (REPO_ROOT / codex_match.group(1)).is_file()

    spec_match = re.search(r"`(quoin/adapters/codex/handoff\.md)`", core_spec)
    assert spec_match, "handoff-format.md must cite handoff.md by backticked repo-root path"
    assert (REPO_ROOT / spec_match.group(1)).is_file()

    # both files must also state the distinction, not just the path
    assert "session continuation" in codex_handoff
    assert "session continuation" in core_spec


def test_handoff_format_core_and_reference_cross_reference():
    """The handoff-format core file and its checkable-rules reference file
    name each other, so a reader following either one finds the other.

    Both pointers use the `__QUOIN_HOME__`-substituted spelling (D-10), not
    a repo-root-relative one: a deployed child resolves the pointer from
    the deploy root, and the repo-root spelling the codex/core citation
    test above uses would not resolve there. The repo source's own
    core/workflow/ directory has the same layout as the deploy root's, so
    substituting `__QUOIN_HOME__` with `str(REPO_ROOT / "quoin")` mirrors
    `installer.substitute_quoin_home`'s semantics without a real install.
    """
    core_spec = read_rel("quoin/core/workflow/handoff-format.md")
    reference_spec = read_rel("quoin/core/workflow/handoff-format-reference.md")
    quoin_home = str(REPO_ROOT / "quoin")

    core_match = re.search(
        r"`__QUOIN_HOME__(/core/workflow/handoff-format-reference\.md)`", core_spec
    )
    assert core_match, (
        "handoff-format.md must cite handoff-format-reference.md by a "
        "backticked __QUOIN_HOME__-substituted path"
    )
    assert Path(quoin_home + core_match.group(1)).is_file()

    reference_match = re.search(
        r"`__QUOIN_HOME__(/core/workflow/handoff-format\.md)`", reference_spec
    )
    assert reference_match, (
        "handoff-format-reference.md must cite handoff-format.md by a "
        "backticked __QUOIN_HOME__-substituted path"
    )
    assert Path(quoin_home + reference_match.group(1)).is_file()


def test_skill_manifest_matches_current_claude_frontmatter():
    manifest = json.loads(read_rel("quoin/core/workflow/skills.json"))
    entries = manifest["skills"]
    by_name = {
        entry["name"]: entry for entry in entries if entry.get("claude_adapter", True)
    }

    skill_paths = sorted((REPO_ROOT / "quoin" / "skills").glob("*/SKILL.md"))
    skill_paths_by_name = {path.parent.name: path for path in skill_paths}
    missing_stubs = set(by_name) - set(skill_paths_by_name)
    assert not missing_stubs

    allowed_effort = {"low", "medium", "high", "max"}
    for name, entry in by_name.items():
        path = skill_paths_by_name[name]
        text = path.read_text(encoding="utf-8")
        frontmatter = text.split("---", 2)[1]
        model = re.search(r"^model:\s*(\S+)", frontmatter, re.MULTILINE)
        assert model is not None, f"Missing model frontmatter in {path}"

        assert entry["claude_model"] == model.group(1)
        assert entry["effort"] in allowed_effort
        assert isinstance(entry["phase"], str) and entry["phase"]
        assert isinstance(entry["user_facing"], bool)

    assert by_name["revise-fast"]["user_facing"] is False
    assert all(
        entry["user_facing"] is True
        for name, entry in by_name.items()
        if name != "revise-fast"
    )


def test_skill_manifest_does_not_include_codex_model_names():
    manifest_text = read_rel("quoin/core/workflow/skills.json").lower()
    forbidden = ["gpt-", "codex-", "o3", "o4", "o5"]
    for token in forbidden:
        assert token not in manifest_text


def test_skill_manifest_readme_documents_all_fields():
    text = read_rel("quoin/core/workflow/skills.md")
    for field in [
        "schema_version",
        "skills",
        "name",
        "phase",
        "effort",
        "user_facing",
        "claude_model",
    ]:
        assert f"`{field}`" in text
    assert "`effort` is the portable field" in text
    assert "`claude_model` exists to keep the manifest auditable" in text


def test_adapter_model_docs_define_runtime_mappings():
    claude = read_rel("quoin/adapters/claude/models.md").lower()
    codex = read_rel("quoin/adapters/codex/effort.md").lower()

    for token in ["haiku", "sonnet", "opus"]:
        assert token in claude
    for effort in ["low", "medium", "high", "max"]:
        assert f"`{effort}`" in claude
        assert f"`{effort}`" in codex

    assert "model:" in claude
    assert "skill.md" in claude
    assert "native codex controls" in codex
    assert "should not consume `claude_model`" in codex


def test_codex_docs_do_not_hardcode_model_names():
    docs = [
        read_rel("quoin/adapters/codex/README.md"),
        read_rel("quoin/adapters/codex/effort.md"),
        read_rel("quoin/adapters/codex/setup.md"),
        read_rel("quoin/adapters/codex/workflow.md"),
    ]
    combined = "\n".join(docs).lower()
    forbidden = ["gpt-", "codex-", "o3", "o4", "o5"]
    for token in forbidden:
        assert token not in combined


def test_codex_docs_do_not_guess_global_install_paths():
    docs = [
        read_rel("AGENTS.md"),
        read_rel("quoin/adapters/codex/README.md"),
        read_rel("quoin/adapters/codex/effort.md"),
        read_rel("quoin/adapters/codex/setup.md"),
        read_rel("quoin/docs/runtime-portability.md"),
        read_rel("quoin/docs/runtime-portability-status.md"),
        read_rel("quoin/docs/effort-levels.md"),
    ]
    combined = "\n".join(docs)
    forbidden = ["~/." + "codex"]
    for token in forbidden:
        assert token not in combined


def test_codex_setup_and_status_are_explicitly_not_installable():
    setup = read_rel("quoin/adapters/codex/setup.md").lower()
    status = read_rel("quoin/docs/runtime-portability-status.md").lower()

    assert "no verified stable codex" in setup
    assert "global install" in setup
    assert "native planning, approvals, sandboxing" in setup
    assert "verify_codex_readiness.py" in setup
    assert "repo-local setup/readiness only" in status
    assert "no global codex installer" in status
    assert "no global codex paths are assumed" in status
    assert "no verified global install target" in status
    assert "claude code" in status
    assert "status: installable" in status


def test_codex_docs_pin_workflow_artifacts_to_project_root():
    docs = {
        "AGENTS.md": read_rel("AGENTS.md").lower(),
        "quoin/adapters/codex/README.md": read_rel("quoin/adapters/codex/README.md").lower(),
        "quoin/adapters/codex/setup.md": read_rel("quoin/adapters/codex/setup.md").lower(),
    }
    for path, text in docs.items():
        assert "project root" in text, f"{path} must pin the Quoin project root"
        assert ".workflow_artifacts/" in text, f"{path} must mention .workflow_artifacts/"

    setup = docs["quoin/adapters/codex/setup.md"]
    assert "not inside a nested application or package directory" in setup
    assert "nested subdirectory" in setup


def test_readme_preserves_claude_install_command():
    readme = read_rel("README.md")
    assert "bash quoin/install.sh" in readme
    assert "runtime-portability-status.md" in readme
