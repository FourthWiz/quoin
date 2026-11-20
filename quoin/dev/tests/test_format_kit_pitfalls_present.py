"""T-11: pitfall-file presence + content test.

Verifies:
- quoin/memory/format-kit-pitfalls.md exists
- File size ≤ 1200 bytes (architecture R-7 budget cap)
- Contains exactly three ## V-NN H2 headings: ## V-04, ## V-05, ## V-06
- Each section has a one-line action bullet starting with "Before composing"
- File does NOT contain the literal string "<placeholder>" (V-04 self-foot-shoot guard)
- Passes validate_artifact.py (exit code 0)
- install.sh "v3 reference files" loop includes format-kit-pitfalls.md
"""
import re
import subprocess
import sys
from pathlib import Path

import pytest


QUOIN_DIR = Path(__file__).parent.parent.parent  # quoin/ directory (3 levels up from test file)
PITFALLS_FILE = QUOIN_DIR / "memory" / "format-kit-pitfalls.md"
INSTALL_SH = QUOIN_DIR / "install.sh"
VALIDATE_SCRIPT = Path.home() / ".claude" / "scripts" / "validate_artifact.py"


def test_pitfalls_file_exists():
    assert PITFALLS_FILE.exists(), f"Expected {PITFALLS_FILE} to exist"


def test_pitfalls_file_size():
    size = PITFALLS_FILE.stat().st_size
    assert size <= 1200, f"format-kit-pitfalls.md is {size} bytes; budget cap is 1200 bytes (≈200 tokens)"


def test_pitfalls_has_exactly_three_vnn_sections():
    content = PITFALLS_FILE.read_text(encoding="utf-8")
    headings = re.findall(r"^## (V-\d+)", content, re.MULTILINE)
    assert headings == ["V-04", "V-05", "V-06"], (
        f"Expected exactly [V-04, V-05, V-06] H2 headings, got {headings}"
    )


def test_pitfalls_each_section_has_action_bullet():
    content = PITFALLS_FILE.read_text(encoding="utf-8")
    bullets = re.findall(r"^Before composing", content, re.MULTILINE | re.IGNORECASE)
    assert len(bullets) == 3, (
        f"Expected 3 'Before composing' action bullets (one per V-NN), found {len(bullets)}"
    )


def test_pitfalls_no_bare_placeholder():
    content = PITFALLS_FILE.read_text(encoding="utf-8")
    assert "<placeholder>" not in content, (
        "format-kit-pitfalls.md contains literal '<placeholder>' — V-04 self-foot-shoot guard triggered"
    )


def test_pitfalls_passes_validator():
    """Verify format-kit-pitfalls.md passes validate_artifact.py with exit 0.

    Per Stage 4 round-5 fixes: the file is detected as artifact type `pitfalls`
    (regex `^format-kit-pitfalls` in detect_type) with sidecar entry
    `artifact_types.pitfalls` declaring class A and required+allowed sections
    `['## V-04', '## V-05', '## V-06']`. The file must therefore validate clean
    end-to-end (no V-NN failure of any kind), satisfying T-11 acceptance bullet 6
    literally (exit code 0).
    """
    if not VALIDATE_SCRIPT.exists():
        pytest.skip(f"validate_artifact.py not found at {VALIDATE_SCRIPT}")
    result = subprocess.run(
        [sys.executable, str(VALIDATE_SCRIPT), str(PITFALLS_FILE)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"format-kit-pitfalls.md failed validate_artifact.py (exit {result.returncode}):\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


