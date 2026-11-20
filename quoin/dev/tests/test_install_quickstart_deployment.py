"""T-04: install.sh QUICKSTART deployment + /init_workflow Step 7 static contract tests.

Three test cases:
1. install.sh deploys QUICKSTART.md to ~/.claude/QUICKSTART.md (byte-identical to source).
2. Step 7 body has no clone-discovery prompts (negative-substring contract).
3. Step 7 fallback heredoc is present with required content and is fully static (no shell expansion).

Post-Phase-21, the content contract lives in the Claude adapter file at
`quoin/adapters/claude/skills/init_workflow/SKILL.md`.
"""
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
INSTALL_SH = REPO_ROOT / "quoin" / "install.sh"
QUICKSTART_SRC = REPO_ROOT / "quoin" / "QUICKSTART.md"
SKILL_MD = REPO_ROOT / "quoin" / "adapters" / "claude" / "skills" / "init_workflow" / "SKILL.md"


pytestmark = pytest.mark.skipif(
    shutil.which("claude") is None or shutil.which("npx") is None,
    reason=(
        "install.sh requires `claude` (hard) and `npx` (soft); test is dev-machine only. "
        "install.sh aborts on missing claude (lines 46-48), so test cannot run on CI."
    ),
)


def _skill_md_step_7_body() -> str:
    assert SKILL_MD.exists(), f"init_workflow SKILL.md not found at {SKILL_MD}"
    text = SKILL_MD.read_text()
    match = re.search(r"### Step 7:.*?(?=\n### Step \d+:)", text, re.DOTALL)
    assert match, "Step 7 heading not found in SKILL.md"
    return match.group(0)


def test_install_sh_deploys_quickstart():
    """install.sh must deploy QUICKSTART.md to ~/.claude/QUICKSTART.md (byte-identical to source).

    Also asserts:
    - Deployed file is at ~/.claude/ root, NOT under memory/.
    - install.sh stdout contains the literal substring 'QUICKSTART deployed to ~/.claude/QUICKSTART.md'.
    - install.sh exits 0.
    """
    assert INSTALL_SH.exists(), f"quoin/install.sh not found at {INSTALL_SH}"
    assert QUICKSTART_SRC.exists(), f"quoin/QUICKSTART.md not found at {QUICKSTART_SRC}"

    with tempfile.TemporaryDirectory() as tmp_home_str:
        tmp_home = Path(tmp_home_str)
        env = {**os.environ, "HOME": str(tmp_home)}
        result = subprocess.run(
            ["bash", str(INSTALL_SH)],
            env=env,
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=180,
        )
        assert result.returncode == 0, (
            f"install.sh failed: returncode={result.returncode}\n"
            f"stdout: {result.stdout[:1500]}\nstderr: {result.stderr[:1500]}"
        )

        # Deployed at root, not under memory/
        deployed = tmp_home / ".claude" / "QUICKSTART.md"
        assert deployed.exists(), (
            f"install.sh did not deploy QUICKSTART.md to ~/.claude/ — expected at {deployed}"
        )
        not_under_memory = tmp_home / ".claude" / "memory" / "QUICKSTART.md"
        assert not not_under_memory.exists(), (
            f"QUICKSTART.md was incorrectly deployed under ~/.claude/memory/ (should be at root)"
        )

        source_bytes = QUICKSTART_SRC.read_bytes()
        deployed_bytes = deployed.read_bytes()
        assert source_bytes == deployed_bytes, (
            "Deployed QUICKSTART.md is NOT byte-identical to quoin/QUICKSTART.md"
        )

        # Success summary line asserted (MIN-4); Python installer prints absolute dest path
        # (IVG-69 Stage B retarget: was "~/.claude/QUICKSTART.md", now absolute path).
        assert f"QUICKSTART deployed to {tmp_home}/.claude/QUICKSTART.md" in result.stdout, (
            f"install.sh stdout does not contain 'QUICKSTART deployed to {tmp_home}/.claude/QUICKSTART.md'\n"
            f"Actual stdout: {result.stdout[:800]}"
        )


def test_skill_md_step_7_no_clone_prompt():
    """Step 7 body must NOT contain any clone-discovery prompt substrings.

    Asserts ~/.claude/QUICKSTART.md IS present, and the broadened set of
    clone-discovery substrings are NOT present (per MAJ-3 — catches partial
    rewrites that remove only part of the clone-discovery scaffolding).
    """
    step_7_body = _skill_md_step_7_body()

    # Must reference the deployed path (T-06: source uses __QUOIN_HOME__ placeholder)
    assert "__QUOIN_HOME__/QUICKSTART.md" in step_7_body, (
        "Step 7 body must reference __QUOIN_HOME__/QUICKSTART.md (the deployed path)"
    )

    # Must NOT contain any clone-discovery prompt substrings
    absent_substrings = [
        "Where is your Quoin source clone?",
        "default: ~/code/quoin",
        "Auto-detect:",
        "Ask the user",
        "Last resort:",
        "<quoin-source-dir>",
        "embed the QUICKSTART body inline",
    ]
    found = [s for s in absent_substrings if s in step_7_body]
    assert not found, (
        f"Step 7 still contains clone-discovery substrings that should have been removed: {found}"
    )


def test_skill_md_step_7_has_fallback_and_is_deterministic():
    """Step 7 fallback heredoc must be present (Part A) and fully static (Part B).

    Part A — presence:
      - Fallback title line '# Quoin — Quickstart (fallback)'
      - User skills pointer '~/.claude/skills/'
      - Qualified HTML-guide pointer '<your-quoin-clone>/Workflow-User-Guide.html'
      - Single-quoted EOF marker <<'EOF'

    Part B — determinism (per MAJ-4 / D-02):
      - No $( command substitution
      - No backtick command substitution
      - No $HOSTNAME
      - No ' date ' (the date command)
      - No $RANDOM
      - No $$ PID expansion
    """
    step_7_body = _skill_md_step_7_body()

    # Isolate the heredoc body between <<'EOF' and ^EOF
    heredoc_match = re.search(r"<<'EOF'(.*?)^EOF$", step_7_body, re.DOTALL | re.MULTILINE)
    assert heredoc_match, (
        "Step 7 body must contain a single-quoted heredoc marker <<'EOF' ... EOF"
    )
    heredoc_body = heredoc_match.group(1)

    # Part A: required content
    assert "# Quoin — Quickstart (fallback)" in heredoc_body, (
        "Fallback heredoc body must contain the title '# Quoin — Quickstart (fallback)'"
    )
    # T-06: source uses __QUOIN_HOME__ placeholder; substituted to real path at deploy time.
    assert "__QUOIN_HOME__/skills/" in heredoc_body, (
        "Fallback heredoc body must contain pointer to user skills directory '__QUOIN_HOME__/skills/'"
    )
    assert "<your-quoin-clone>/Workflow-User-Guide.html" in heredoc_body, (
        "Fallback heredoc body must contain the qualified HTML-guide pointer "
        "'<your-quoin-clone>/Workflow-User-Guide.html' (per MIN-2)"
    )

    # Part B: no shell expansion constructs
    non_deterministic = [
        ("$(", "command substitution $(...) "),
        ("`", "backtick command substitution"),
        ("$HOSTNAME", "$HOSTNAME variable"),
        (" date ", "date command (bare word)"),
        ("$RANDOM", "$RANDOM variable"),
        ("$$", "$$ PID expansion"),
    ]
    found = [(label, token) for token, label in non_deterministic if token in heredoc_body]
    assert not found, (
        "Fallback heredoc body contains non-deterministic shell constructs: "
        + ", ".join(label for label, _ in found)
    )
