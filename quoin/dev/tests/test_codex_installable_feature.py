"""Tests for the Codex installable feature scaffold.

Verifies:
- manifest exists, parses as JSON, references portable workflow metadata,
  avoids unsupported path claims
- generator produces AGENTS.md in a temp project root
- --check passes on matching output and fails on stale output
"""

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

THIS_FILE = Path(__file__).resolve()
TESTS_DIR = THIS_FILE.parent
PKG_DIR = TESTS_DIR.parent.parent
CODEX_ADAPTER_DIR = PKG_DIR / "adapters" / "codex"
CODEX_SKILLS_DIR = CODEX_ADAPTER_DIR / "skills"
CODEX_PROCEDURES_DIR = CODEX_ADAPTER_DIR / "procedures"
MANIFEST_PATH = CODEX_ADAPTER_DIR / "feature-manifest.json"
GENERATOR_PATH = CODEX_ADAPTER_DIR / "generate_codex_assets.py"
READINESS_PATH = CODEX_ADAPTER_DIR / "verify_codex_readiness.py"
SMOKE_PATH = CODEX_ADAPTER_DIR / "smoke_codex_workflow.py"
HANDOFF_DOC_PATH = CODEX_ADAPTER_DIR / "handoff.md"
HANDOFF_VALIDATOR_PATH = CODEX_ADAPTER_DIR / "validate_codex_handoff.py"
HANDOFF_FIXTURE_PATH = CODEX_ADAPTER_DIR / "fixtures" / "valid-handoff.md"
CODEX_COST_DOC_PATH = CODEX_ADAPTER_DIR / "cost.md"
CODEX_COST_EVENT_PATH = CODEX_ADAPTER_DIR / "cost_event.py"
CONTRACT_PATH = CODEX_ADAPTER_DIR / "installable-feature.md"
SKILLS_JSON_PATH = PKG_DIR / "core" / "workflow" / "skills.json"
CORE_SKILLS_DIR = PKG_DIR / "core" / "skills"
REPO_ROOT = PKG_DIR.parent
CORE_WORKFLOW_PHASES = ["discover", "plan", "implement", "review", "gate"]

GUESSED_GLOBAL_PATTERNS = [
    "~/." + "codex",
    "/usr/local/" + "codex",
    "codex install",
    "npm install" + " -g",
]

CLAUDE_ONLY_PATH_PATTERNS = [
    "~/." + "claude",
    "$HOME/." + "claude",
    ".claude/",
    "CLAUDE.md",
]

REQUIRED_CCUSAGE_PATTERNS = [
    "requires ccusage",
    "require ccusage",
    "depends on ccusage",
    "install ccusage",
]

SLASH_COMMAND_REQUIREMENT_PATTERNS = [
    re.compile(
        r"\b(use|run|invoke|call|execute|require|required)\s+/"
        r"(discover|plan|implement|review|gate|run|architect|critic|revise)\b",
        re.IGNORECASE,
    ),
]


def _portable_skill_names():
    skills_data = json.loads(SKILLS_JSON_PATH.read_text(encoding="utf-8"))
    return [skill["name"] for skill in skills_data.get("skills", [])]


def _codex_adapter_text_files():
    suffixes = {".md", ".json", ".py"}
    return [
        path for path in CODEX_ADAPTER_DIR.rglob("*")
        if path.is_file()
        and path.suffix in suffixes
        and "__pycache__" not in path.parts
    ]


# ---------------------------------------------------------------------------
# Manifest tests
# ---------------------------------------------------------------------------


def test_manifest_exists():
    assert MANIFEST_PATH.is_file(), f"Missing {MANIFEST_PATH}"


def test_manifest_parses_as_json():
    text = MANIFEST_PATH.read_text(encoding="utf-8")
    manifest = json.loads(text)
    assert isinstance(manifest, dict), "Manifest must be a JSON object"


def test_manifest_has_required_keys():
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    required = {
        "schema_version", "feature_name", "adapter", "status",
        "entrypoints", "portable_inputs", "generated_outputs",
        "unsupported_outputs", "validation",
    }
    missing = required - set(manifest.keys())
    assert not missing, f"Manifest missing keys: {missing}"


def test_manifest_references_skills_json():
    """Manifest portable_inputs must reference skills.json by path."""
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    paths = [entry.get("path", "") for entry in manifest.get("portable_inputs", [])]
    assert any("skills.json" in p for p in paths), (
        "Manifest portable_inputs must reference skills.json"
    )


def test_manifest_exposes_readiness_check():
    """Manifest entrypoints and validation must include repo-local checks."""
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    entrypoint_paths = [entry.get("path", "") for entry in manifest.get("entrypoints", [])]
    validation = "\n".join(manifest.get("validation", {}).get("commands", []))
    assert any("verify_codex_readiness.py" in path for path in entrypoint_paths)
    assert "verify_codex_readiness.py" in validation
    assert any("smoke_codex_workflow.py" in path for path in entrypoint_paths)
    assert "smoke_codex_workflow.py" in validation
    assert any("validate_codex_handoff.py" in path for path in entrypoint_paths)
    assert "validate_codex_handoff.py" in validation
    assert any("cost_event.py" in path for path in entrypoint_paths)
    assert "cost_event.py" in validation


def test_manifest_lists_codex_workflow_procedure_docs():
    """Manifest must record the repo-local Codex workflow procedure docs."""
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    generated_paths = [entry.get("path", "") for entry in manifest.get("generated_outputs", [])]
    validation = "\n".join(manifest.get("validation", {}).get("commands", []))

    assert "quoin/adapters/codex/workflow.md" in generated_paths
    assert "quoin/adapters/codex/procedures/<phase>.md" in generated_paths
    assert "quoin/adapters/codex/handoff.md" in generated_paths
    assert "quoin/adapters/codex/validate_codex_handoff.py" in generated_paths
    assert "quoin/adapters/codex/cost.md" in generated_paths
    assert "quoin/adapters/codex/cost_event.py" in generated_paths
    assert "test_codex_installable_feature.py" in validation


def test_manifest_generated_output_is_repo_local():
    """Generated outputs must be repo-local (no global paths)."""
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    for entry in manifest.get("generated_outputs", []):
        scope = entry.get("scope", "")
        assert scope == "repo-local", (
            f"Generated output {entry.get('path')} must have scope=repo-local, got {scope!r}"
        )


def test_manifest_avoids_guessed_global_paths():
    """Manifest text must not contain known guessed global path patterns."""
    text = MANIFEST_PATH.read_text(encoding="utf-8")
    hits = [p for p in GUESSED_GLOBAL_PATTERNS if p in text]
    assert not hits, f"Manifest contains guessed global paths: {hits}"


def test_codex_adapter_active_files_avoid_guessed_global_paths():
    """Codex adapter files must not hardcode guessed global Codex paths."""
    active_files = _codex_adapter_text_files()
    combined = "\n".join(path.read_text(encoding="utf-8") for path in active_files)
    hits = [p for p in GUESSED_GLOBAL_PATTERNS if p in combined]
    assert not hits, f"Codex adapter files contain guessed global paths: {hits}"


def test_codex_adapter_files_avoid_claude_only_paths():
    """Codex facing files may document unsupported Claude behavior, but not Claude paths."""
    active_files = _codex_adapter_text_files()
    combined = "\n".join(path.read_text(encoding="utf-8") for path in active_files)
    hits = [p for p in CLAUDE_ONLY_PATH_PATTERNS if p in combined]
    assert not hits, f"Codex adapter files contain Claude-only path assumptions: {hits}"


def test_codex_adapter_files_do_not_require_ccusage():
    """Codex facing files may mention ccusage only as unsupported Claude cost tooling."""
    active_files = _codex_adapter_text_files()
    combined = "\n".join(path.read_text(encoding="utf-8").lower() for path in active_files)
    hits = [p for p in REQUIRED_CCUSAGE_PATTERNS if p in combined]
    assert not hits, f"Codex adapter files make ccusage a required dependency: {hits}"


def test_manifest_documents_unsupported_global_install():
    """Manifest must explicitly document that global install is unsupported."""
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    unsupported_names = [e.get("name", "") for e in manifest.get("unsupported_outputs", [])]
    assert any("global" in n.lower() for n in unsupported_names), (
        "Manifest must list a global install as an unsupported output"
    )


# ---------------------------------------------------------------------------
# Generator tests
# ---------------------------------------------------------------------------


def test_generator_script_exists():
    assert GENERATOR_PATH.is_file(), f"Missing {GENERATOR_PATH}"


def test_readiness_script_exists():
    assert READINESS_PATH.is_file(), f"Missing {READINESS_PATH}"


def test_smoke_script_exists():
    assert SMOKE_PATH.is_file(), f"Missing {SMOKE_PATH}"


def test_handoff_doc_and_validator_exist():
    assert HANDOFF_DOC_PATH.is_file(), f"Missing {HANDOFF_DOC_PATH}"
    assert HANDOFF_VALIDATOR_PATH.is_file(), f"Missing {HANDOFF_VALIDATOR_PATH}"
    assert HANDOFF_FIXTURE_PATH.is_file(), f"Missing {HANDOFF_FIXTURE_PATH}"


def test_codex_cost_doc_and_writer_exist():
    assert CODEX_COST_DOC_PATH.is_file(), f"Missing {CODEX_COST_DOC_PATH}"
    assert CODEX_COST_EVENT_PATH.is_file(), f"Missing {CODEX_COST_EVENT_PATH}"


def test_generator_writes_agents_md_to_project_root():
    """Generator must write AGENTS.md to <project-root>/AGENTS.md."""
    with tempfile.TemporaryDirectory() as tmpdir:
        result = subprocess.run(
            [sys.executable, str(GENERATOR_PATH), "--project-root", tmpdir],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"Generator failed: {result.stderr}"
        agents_md = Path(tmpdir) / "AGENTS.md"
        assert agents_md.exists(), "Generator did not create AGENTS.md"
        content = agents_md.read_text(encoding="utf-8")
        assert len(content) > 0, "Generated AGENTS.md is empty"


def test_generator_no_global_writes():
    """Generator must not write outside the project root."""
    with tempfile.TemporaryDirectory() as tmpdir:
        result = subprocess.run(
            [sys.executable, str(GENERATOR_PATH), "--project-root", tmpdir],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"Generator failed: {result.stderr}"
        # The only change should be AGENTS.md inside tmpdir
        written = list(Path(tmpdir).iterdir())
        assert len(written) == 1 and written[0].name == "AGENTS.md", (
            f"Generator wrote unexpected files: {written}"
        )


def test_generator_writes_codex_adapter_assets_when_requested():
    """Generator can materialize Codex skill docs without inventing runtime install paths."""
    with tempfile.TemporaryDirectory() as tmpdir, tempfile.TemporaryDirectory() as adapter_tmp:
        result = subprocess.run(
            [
                sys.executable,
                str(GENERATOR_PATH),
                "--project-root",
                tmpdir,
                "--adapter-assets",
                "--adapter-root",
                adapter_tmp,
            ],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"Generator failed: {result.stderr}"

        for name in _portable_skill_names():
            path = Path(adapter_tmp) / "skills" / name / "README.md"
            assert path.is_file(), f"Missing generated Codex adapter doc for {name}"
            text = path.read_text(encoding="utf-8")
            assert f"quoin/core/skills/{name}.md" in text
            assert "## Unsupported Claude-only translations" in text


def test_generator_check_covers_codex_adapter_assets():
    """--check reports Codex adapter asset drift when the assets flag is used."""
    with tempfile.TemporaryDirectory() as tmpdir, tempfile.TemporaryDirectory() as adapter_tmp:
        subprocess.run(
            [
                sys.executable,
                str(GENERATOR_PATH),
                "--project-root",
                tmpdir,
                "--adapter-assets",
                "--adapter-root",
                adapter_tmp,
            ],
            check=True, capture_output=True,
        )
        result = subprocess.run(
            [
                sys.executable,
                str(GENERATOR_PATH),
                "--project-root",
                tmpdir,
                "--adapter-assets",
                "--adapter-root",
                adapter_tmp,
                "--check",
            ],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr


def test_generator_check_passes_on_matching_output():
    """--check must exit 0 when AGENTS.md matches rendered output."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Generate first
        subprocess.run(
            [sys.executable, str(GENERATOR_PATH), "--project-root", tmpdir],
            check=True, capture_output=True,
        )
        # --check must pass
        result = subprocess.run(
            [sys.executable, str(GENERATOR_PATH), "--project-root", tmpdir, "--check"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, (
            f"--check failed on matching output: {result.stderr}"
        )


def test_generator_check_fails_on_stale_output():
    """--check must exit nonzero when AGENTS.md is out of date."""
    with tempfile.TemporaryDirectory() as tmpdir:
        stale_path = Path(tmpdir) / "AGENTS.md"
        stale_path.write_text("stale content", encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(GENERATOR_PATH), "--project-root", tmpdir, "--check"],
            capture_output=True, text=True,
        )
        assert result.returncode != 0, (
            "--check must exit nonzero on stale AGENTS.md"
        )


def test_generated_agents_md_preserves_architectural_boundaries():
    """Generated AGENTS.md must preserve Quoin architectural boundaries."""
    with tempfile.TemporaryDirectory() as tmpdir:
        subprocess.run(
            [sys.executable, str(GENERATOR_PATH), "--project-root", tmpdir],
            check=True, capture_output=True,
        )
        content = (Path(tmpdir) / "AGENTS.md").read_text(encoding="utf-8")
        # Must not promise global install
        assert "global Codex install" not in content or "not" in content.lower(), (
            "Generated AGENTS.md must not promise global Codex install"
        )
        # Must mention .workflow_artifacts/
        assert ".workflow_artifacts/" in content, (
            "Generated AGENTS.md must describe .workflow_artifacts/ layout"
        )
        assert "validate_codex_handoff.py" in content, (
            "Generated AGENTS.md must describe Codex handoff validation"
        )
        assert "cost_event.py" in content, (
            "Generated AGENTS.md must describe Codex cost event writing"
        )
        # Must not mention Claude slash commands as required
        assert "slash command" not in content.lower(), (
            "Generated AGENTS.md must not require Claude slash commands"
        )


def test_generated_agents_md_uses_skills_json():
    """Generated AGENTS.md must include skill names drawn from skills.json."""
    skills_data = json.loads(SKILLS_JSON_PATH.read_text(encoding="utf-8"))
    skill_names = [s["name"] for s in skills_data.get("skills", []) if s.get("user_facing")]
    with tempfile.TemporaryDirectory() as tmpdir:
        subprocess.run(
            [sys.executable, str(GENERATOR_PATH), "--project-root", tmpdir],
            check=True, capture_output=True,
        )
        content = (Path(tmpdir) / "AGENTS.md").read_text(encoding="utf-8")
        present = [name for name in skill_names if name in content]
        assert len(present) >= 5, (
            f"Generated AGENTS.md should include multiple skill names from skills.json; "
            f"found only: {present}"
        )


# ---------------------------------------------------------------------------
# Codex skill adapter coverage tests
# ---------------------------------------------------------------------------


def test_every_portable_skill_has_codex_adapter_doc():
    """Every migrated portable skill must have a Codex facing adapter doc."""
    names = _portable_skill_names()
    assert len(names) == 33
    for name in names:
        assert (CORE_SKILLS_DIR / f"{name}.md").is_file(), f"Missing core doc for {name}"
        assert (CODEX_SKILLS_DIR / name / "README.md").is_file(), (
            f"Missing Codex adapter doc for {name}"
        )


def test_codex_skill_docs_reference_core_and_unsupported_contract():
    """Each Codex skill doc points at the portable core and documents unsupported behavior."""
    for name in _portable_skill_names():
        text = (CODEX_SKILLS_DIR / name / "README.md").read_text(encoding="utf-8")
        assert f"quoin/core/skills/{name}.md" in text
        assert "## Codex invocation" in text
        assert "## Portable workflow contract" in text
        assert "## Unsupported Claude-only translations" in text
        assert "Codex global install" in text
        assert "generated\ncommand file" in text


def test_codex_skill_index_covers_all_portable_skills():
    """The Codex skill index must list every portable skill exactly once."""
    text = (CODEX_SKILLS_DIR / "README.md").read_text(encoding="utf-8")
    missing = [name for name in _portable_skill_names() if f"({name}/README.md)" not in text]
    assert not missing, f"Codex skill index missing skills: {missing}"


# ---------------------------------------------------------------------------
# Codex workflow procedure coverage tests
# ---------------------------------------------------------------------------


def test_codex_workflow_guide_exists_and_covers_core_loop():
    """Codex workflow guide must cover the Phase 33 loop and portable sources."""
    workflow = CODEX_ADAPTER_DIR / "workflow.md"
    assert workflow.is_file(), f"Missing {workflow}"

    text = workflow.read_text(encoding="utf-8")
    assert "discover -> plan -> implement -> review -> gate" in text
    assert ".workflow_artifacts/" in text
    for core_doc in [
        "quoin/core/workflow/rules.md",
        "quoin/core/workflow/task-layout.md",
        "quoin/core/workflow/session-state.md",
        "quoin/core/workflow/cost-ledger.md",
        "quoin/core/workflow/skills.json",
    ]:
        assert core_doc in text

    for phase in CORE_WORKFLOW_PHASES:
        assert f"quoin/adapters/codex/procedures/{phase}.md" in text
        assert f"quoin/core/skills/{phase}.md" in text


def test_codex_procedure_index_covers_core_loop():
    """Procedure index must list all five Phase 33 workflow phases."""
    index = CODEX_PROCEDURES_DIR / "README.md"
    assert index.is_file(), f"Missing {index}"

    text = index.read_text(encoding="utf-8")
    for phase in CORE_WORKFLOW_PHASES:
        assert f"| `{phase}` |" in text
        assert f"({phase}.md)" in text
        assert f"quoin/core/skills/{phase}.md" in text


def test_codex_procedures_link_portable_contracts_and_workflow_docs():
    """Each procedure must be grounded in the portable core contracts."""
    for phase in CORE_WORKFLOW_PHASES:
        path = CODEX_PROCEDURES_DIR / f"{phase}.md"
        assert path.is_file(), f"Missing {path}"

        text = path.read_text(encoding="utf-8")
        assert f"Portable contract: `quoin/core/skills/{phase}.md`" in text
        assert "## Codex Procedure" in text
        assert "## Codex Native Notes" in text
        assert ".workflow_artifacts/" in text
        for workflow_doc in [
            "quoin/core/workflow/rules.md",
            "quoin/core/workflow/task-layout.md",
            "quoin/core/workflow/session-state.md",
            "quoin/core/workflow/cost-ledger.md",
        ]:
            assert workflow_doc in text
        assert "quoin/adapters/codex/handoff.md" in text
        assert "validate_codex_handoff.py" in text


def test_codex_handoff_doc_links_portable_contracts_and_shape():
    """Codex handoff guidance must be repo-local and grounded in portable contracts."""
    text = HANDOFF_DOC_PATH.read_text(encoding="utf-8")
    for token in [
        ".workflow_artifacts/memory/sessions/",
        "<YYYY-MM-DD>-<task-name>-codex.md",
        "quoin/core/workflow/session-state.md",
        "quoin/core/workflow/task-layout.md",
        "quoin/core/workflow/rules.md",
        "quoin/core/skills/end_of_day.md",
        "## Required Shape",
        "## Reading Procedure",
        "validate_codex_handoff.py",
    ]:
        assert token in text


def test_codex_cost_doc_and_writer_link_portable_core():
    """Codex cost docs and writer must use portable cost core without guessed telemetry."""
    doc = CODEX_COST_DOC_PATH.read_text(encoding="utf-8")
    writer = CODEX_COST_EVENT_PATH.read_text(encoding="utf-8")
    combined = doc + "\n" + writer

    for token in [
        "quoin/core/scripts/cost_event.py",
        "quoin/core/workflow/cost-ledger.md",
        "not_available",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cost_usd",
        "telemetry_source",
        "unknown-codex-",
        "format_row",
        "parse_row",
    ]:
        assert token in combined

    for token in CLAUDE_ONLY_PATH_PATTERNS:
        assert token not in combined
    for token in REQUIRED_CCUSAGE_PATTERNS:
        assert token not in combined.lower()


def test_codex_handoff_validator_accepts_fixture():
    """The handoff validator must accept the documented fixture shape."""
    result = subprocess.run(
        [
            sys.executable,
            str(HANDOFF_VALIDATOR_PATH),
            "--self-test",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"Handoff validation failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "HANDOFF PASS" in result.stdout


def test_codex_handoff_validator_rejects_missing_required_shape():
    """The handoff validator must fail deterministically on malformed handoff files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        handoff_path = root / ".workflow_artifacts" / "memory" / "sessions" / "bad-codex.md"
        handoff_path.parent.mkdir(parents=True)
        handoff_path.write_text(
            "# Codex Session Handoff: bad\n\n## Metadata\n- runtime: codex\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            [
                sys.executable,
                str(HANDOFF_VALIDATOR_PATH),
                "--project-root",
                str(root),
                "--file",
                ".workflow_artifacts/memory/sessions/bad-codex.md",
            ],
            capture_output=True,
            text=True,
        )
    assert result.returncode != 0
    assert "HANDOFF FAIL" in result.stderr
    assert "missing required section" in result.stderr


def test_codex_procedures_use_optional_discovery_map():
    """Discovery map support must be advisory and repo-local."""
    discover = (CODEX_PROCEDURES_DIR / "discover.md").read_text(encoding="utf-8")
    workflow = (CODEX_ADAPTER_DIR / "workflow.md").read_text(encoding="utf-8")
    combined = discover + "\n" + workflow

    assert ".workflow_artifacts/discovery-map.json" in combined
    assert "quoin/scripts/generate_discovery_map.py" in discover
    assert "If generation fails" in discover
    assert "advisory" in workflow.lower()


def test_codex_procedures_avoid_install_and_invocation_leakage():
    """Procedure docs must not grow runtime install or command assumptions."""
    docs = [
        CODEX_ADAPTER_DIR / "workflow.md",
        CODEX_ADAPTER_DIR / "handoff.md",
        CODEX_PROCEDURES_DIR / "README.md",
    ]
    docs.extend(CODEX_PROCEDURES_DIR / f"{phase}.md" for phase in CORE_WORKFLOW_PHASES)

    combined = "\n".join(path.read_text(encoding="utf-8") for path in docs)
    for token in GUESSED_GLOBAL_PATTERNS:
        assert token not in combined
    for token in CLAUDE_ONLY_PATH_PATTERNS:
        assert token not in combined
    for pattern in SLASH_COMMAND_REQUIREMENT_PATTERNS:
        assert pattern.search(combined) is None

    lower = combined.lower()
    assert "global codex installer is supported" not in lower
    assert "codex command files are implemented" not in lower
    assert "native codex" in lower


# ---------------------------------------------------------------------------
# Readiness tests
# ---------------------------------------------------------------------------


def test_readiness_check_passes_for_repo_root():
    """Readiness check must pass for this repo-local Codex setup."""
    result = subprocess.run(
        [sys.executable, str(READINESS_PATH), "--project-root", str(REPO_ROOT)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f"Readiness check failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "READY: repo-local Codex setup contract is satisfied" in result.stdout


def test_readiness_check_fails_without_agents_md():
    """Readiness check must fail if the project root lacks AGENTS.md."""
    with tempfile.TemporaryDirectory() as tmpdir:
        result = subprocess.run(
            [sys.executable, str(READINESS_PATH), "--project-root", tmpdir],
            capture_output=True, text=True,
        )
        assert result.returncode != 0
        assert "agents-md" in result.stdout


def test_claude_install_remains_codex_free():
    """Claude install behavior stays isolated from the Codex setup flow."""
    install_sh = PKG_DIR / "install.sh"
    text = install_sh.read_text(encoding="utf-8").lower()
    assert "codex" not in text
    assert '"install"' in text
    assert "source-dir" in text
    assert "adapters/codex" not in text


# ---------------------------------------------------------------------------
# Contract doc test
# ---------------------------------------------------------------------------


def test_contract_doc_exists():
    assert CONTRACT_PATH.is_file(), f"Missing {CONTRACT_PATH}"


def test_contract_doc_links_required_docs():
    """installable-feature.md must link to runtime-portability.md, setup.md, and skills.json."""
    text = CONTRACT_PATH.read_text(encoding="utf-8")
    assert "runtime-portability.md" in text, "Contract must link to runtime-portability.md"
    assert "setup.md" in text, "Contract must link to setup.md"
    assert "skills.json" in text, "Contract must link to skills.json"
