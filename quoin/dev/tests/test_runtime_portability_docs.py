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


def test_handoff_format_core_and_reference_cross_reference():
    """The handoff-format core file and its checkable-rules reference file
    name each other, so a reader following either one finds the other.

    Both pointers use the `${CLAUDE_PLUGIN_ROOT}`-substituted spelling (D-10), not
    a repo-root-relative one: a deployed child resolves the pointer from
    the deploy root, and the repo-root spelling the codex/core citation
    test above uses would not resolve there. The repo source's own
    core/workflow/ directory has the same layout as the deploy root's, so
    substituting `${CLAUDE_PLUGIN_ROOT}` with `str(REPO_ROOT / "quoin")` mirrors
    `installer.substitute_quoin_home`'s semantics without a real install.
    """
    core_spec = read_rel("quoin/core/workflow/handoff-format.md")
    reference_spec = read_rel("quoin/core/workflow/handoff-format-reference.md")
    quoin_home = str(REPO_ROOT / "quoin")

    core_match = re.search(
        r"`\$\{CLAUDE_PLUGIN_ROOT\}(/core/workflow/handoff-format-reference\.md)`", core_spec
    )
    assert core_match, (
        "handoff-format.md must cite handoff-format-reference.md by a "
        "backticked ${CLAUDE_PLUGIN_ROOT}-substituted path"
    )
    assert Path(quoin_home + core_match.group(1)).is_file()

    reference_match = re.search(
        r"`\$\{CLAUDE_PLUGIN_ROOT\}(/core/workflow/handoff-format\.md)`", reference_spec
    )
    assert reference_match, (
        "handoff-format-reference.md must cite handoff-format.md by a "
        "backticked ${CLAUDE_PLUGIN_ROOT}-substituted path"
    )
    assert Path(quoin_home + reference_match.group(1)).is_file()


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


def test_readme_preserves_claude_install_command():
    readme = read_rel("README.md")
    assert "bash quoin/install.sh" in readme
    assert "runtime-portability-status.md" in readme
