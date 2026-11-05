"""Shell portability of the gate/review/run test recipes.

The recipes run in the session shell, which may be bash or zsh, from a project
path that may contain spaces (a Google Drive "My Drive" folder, for example).
Two patterns break there:

- expanding a list through an unquoted variable (`pytest $SEL`): zsh does not
  word-split it, so the command gets one giant argument, while bash splits it
  inside "My Drive" as well;
- reading an exit code from `PIPESTATUS`: zsh only has lowercase, 1-indexed
  `pipestatus`, so the value comes back empty.

This file lints the recipe text for both patterns and then runs the gate's
affected-area and full-suite recipes, as written, under bash and under zsh
(when installed) in a temporary project whose path contains a space.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PKG = REPO_ROOT / "quoin"
SKILLS = PKG / "adapters" / "claude" / "skills"
GATE_SKILL = SKILLS / "gate" / "SKILL.md"
LINTED = [
    SKILLS / "gate" / "SKILL.md",
    SKILLS / "review" / "SKILL.md",
    SKILLS / "run" / "SKILL.md",
    PKG / "memory" / "autonomous-mode.md",
]

# ---------------------------------------------------------------------------
# Lint: quote-aware scan of shell snippets
# ---------------------------------------------------------------------------

_VAR_RE = re.compile(r"\$(\{[A-Za-z_][^}]*\}|[A-Za-z_][A-Za-z0-9_]*)")
_ASSIGN_TAIL_RE = re.compile(r"(^|[\s;&|(])[A-Za-z_][A-Za-z0-9_]*=$")
_PIPESTATUS_RE = re.compile(r"\$\{?(PIPESTATUS|pipestatus)\b")


def unquoted_expansions(line: str) -> list[str]:
    """Return `$NAME` / `${...}` expansions on `line` that are not double-quoted.

    Tracks single quotes, double quotes, `$( ... )` (which resets quoting) and
    `$(( ... ))` arithmetic. Special parameters such as `$?` never match, and the
    right-hand side of a plain assignment (`X=$Y`) is allowed because neither
    shell splits it.
    """
    found: list[str] = []
    stack = ["top"]
    i, n = 0, len(line)
    while i < n:
        c = line[i]
        ctx = stack[-1]
        if ctx == "sq":
            if c == "'":
                stack.pop()
            i += 1
            continue
        if c == "\\":
            i += 2
            continue
        if line.startswith("$((", i):
            stack.append("arith")
            i += 3
            continue
        if ctx == "arith":
            if line.startswith("))", i):
                stack.pop()
                i += 2
            else:
                i += 1
            continue
        if line.startswith("$(", i):
            stack.append("cmd")
            i += 2
            continue
        if ctx == "dq":
            if c == '"':
                stack.pop()
            i += 1
            continue
        if c == '"':
            stack.append("dq")
        elif c == "'":
            stack.append("sq")
        elif c == ")" and ctx == "cmd":
            stack.pop()
        elif c == "$":
            m = _VAR_RE.match(line, i)
            if m:
                if not _ASSIGN_TAIL_RE.search(line[:i]):
                    found.append(m.group(0))
                i = m.end()
                continue
        i += 1
    return found


def shell_snippets(text: str):
    """Yield (line_no, snippet): fenced-block lines and multi-token inline code."""
    in_fence = False
    for no, line in enumerate(text.splitlines(), 1):
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            yield no, line
            continue
        for span in re.findall(r"`([^`]+)`", line):
            if "$" in span and " " in span.strip():
                yield no, span


def lint_text(text: str) -> list[str]:
    problems = []
    for no, line in enumerate(text.splitlines(), 1):
        if _PIPESTATUS_RE.search(line):
            problems.append(f"line {no}: exit code read through PIPESTATUS: {line.strip()}")
    for no, snippet in shell_snippets(text):
        bad = unquoted_expansions(snippet)
        if bad:
            problems.append(f"line {no}: unquoted expansion {bad}: {snippet.strip()}")
    return problems


class TestLintSelfCheck:
    @pytest.mark.parametrize("snippet", [
        "pytest $SEL",
        "known_red.py --selectors $SEL --observed-rc 1",
        'echo "$(cat ${FILES})" $(ls $DIR)',
    ])
    def test_flags_unquoted_list_expansion(self, snippet):
        assert unquoted_expansions(snippet)

    @pytest.mark.parametrize("snippet", [
        'pytest "$SEL"',
        'RC=$?',
        'X=$Y; echo "$X"',
        'PY="$(python3 a.py --project-root "$PROJECT_ROOT")" && [ -x "$PY" ]',
        "echo '$NOT_EXPANDED'",
        'echo $((1 + 2)) "${arr[@]}"',
    ])
    def test_accepts_quoted_forms(self, snippet):
        assert unquoted_expansions(snippet) == []

    def test_flags_pipestatus_in_either_case(self):
        assert lint_text("cmd | grep x; KR=${PIPESTATUS[0]}")
        assert lint_text("cmd | grep x; KR=$pipestatus[1]")
        assert lint_text("never read the exit code through `PIPESTATUS`") == []

    def test_scans_fenced_blocks_and_inline_commands(self):
        text = "```\npytest $SEL\n```\nRun `known_red.py --selectors $SEL`; see `$AFF.txt`.\n"
        problems = lint_text(text)
        assert len(problems) == 2, problems


@pytest.mark.parametrize("path", LINTED, ids=lambda p: p.parent.name if p.name == "SKILL.md" else p.name)
def test_recipes_never_rely_on_word_splitting_or_pipestatus(path):
    problems = lint_text(path.read_text(encoding="utf-8"))
    assert problems == [], f"{path.relative_to(REPO_ROOT)}:\n" + "\n".join(problems)


# ---------------------------------------------------------------------------
# Run the gate recipes under bash and zsh from a path with a space
# ---------------------------------------------------------------------------

_SHELLS = [
    pytest.param("bash", ["-c"], id="bash"),
    pytest.param("zsh", ["-f", "-c"], id="zsh"),
]


def _fenced_blocks(text: str) -> list[str]:
    blocks, current, in_fence = [], [], False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            if in_fence:
                blocks.append(textwrap.dedent("\n".join(current)))
                current = []
            in_fence = not in_fence
            continue
        if in_fence:
            current.append(line)
    return blocks


def gate_recipe(marker: str) -> str:
    matches = [b for b in _fenced_blocks(GATE_SKILL.read_text(encoding="utf-8")) if marker in b]
    assert len(matches) == 1, f"expected one gate recipe containing {marker!r}, found {len(matches)}"
    return matches[0]


def _materialize(recipe: str) -> str:
    """Point the recipe at this checkout and fill its placeholders."""
    exe = sys.executable
    scripts = PKG / "scripts"
    out = re.sub(
        r"python3 __QUOIN_HOME__/scripts/(\w+\.py)",
        lambda m: f'"{exe}" "{scripts / m.group(1)}"',
        recipe,
    )
    out = re.sub(r"\bpython3\b", f'"{exe}"', out)
    assert "__QUOIN_HOME__" not in out
    return re.sub(
        r"<[^<>\n]+>",
        lambda m: "large" if "large" in m.group(0) else "tok-1",
        out,
    )


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture()
def spaced_project(tmp_path):
    """<tmp>/My Drive/proj with a nested git repo mirroring the quoin layout."""
    project = tmp_path / "My Drive" / "proj"
    repo = project / "quoin"
    tests = repo / "quoin" / "dev" / "tests"
    tests.mkdir(parents=True)
    (project / ".workflow_artifacts").mkdir()
    (repo / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (repo / ".gitignore").write_text(".pytest_cache/\n__pycache__/\n", encoding="utf-8")
    # The affected-area run starts pytest inside the repo and the full-suite run
    # starts it from the project root, so the same test can surface with or
    # without the repo folder prefix; list both so each recipe has a match.
    (tests / "known-red.toml").write_text(
        "".join(
            f'[[entry]]\nid = "{nodeid}"\nreason = "baseline"\nadded = "2026-01-01"\n'
            for nodeid in (
                "quoin/dev/tests/test_mod.py::test_known",
                "quoin/quoin/dev/tests/test_mod.py::test_known",
            )
        ),
        encoding="utf-8",
    )
    (tests / "test_mod.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    # Uncommitted change: the affected-area helper selects test_mod.py.
    (tests / "test_mod.py").write_text(
        "def test_ok():\n    assert True\n\n\n"
        "def test_known():\n    assert False, 'known baseline'\n",
        encoding="utf-8",
    )
    return project


def _run_recipe(shell, flags, script, cwd):
    if shutil.which(shell) is None:
        pytest.skip(f"{shell} not installed")
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("QUOIN_", "PYTEST_"))}
    env["QUOIN_PYTHON"] = sys.executable
    env["QUOIN_REQUIRE_TASK_CONTEXT"] = "0"
    proc = subprocess.run(
        [shell, *flags, script], cwd=cwd, env=env,
        capture_output=True, text=True, timeout=300,
    )
    return proc.stdout + proc.stderr


@pytest.mark.parametrize("shell,flags", _SHELLS)
def test_affected_area_recipe_downgrades_manifest_only_red(shell, flags, spaced_project):
    script = _materialize(gate_recipe("--selectors-from"))
    out = _run_recipe(shell, flags, script, spaced_project)
    assert "AT_RC=1" in out, out
    assert "## Known-baseline (downgraded)" in out, out
    assert "KR_RC=0" in out, out
    assert "UNRECONCILED" not in out, out


@pytest.mark.parametrize("shell,flags", _SHELLS)
def test_full_suite_recipe_records_integer_exit_codes(shell, flags, spaced_project):
    recipe = _materialize(gate_recipe("--full-suite"))
    script = recipe + '\necho "SIDECAR_RC=$?"\necho "RC=$RC KR_RC=$KR_RC"\n'
    out = _run_recipe(shell, flags, script, spaced_project)
    assert "RC=1 KR_RC=0" in out, out
    assert "## Known-baseline (downgraded)" in out, out
    assert "SIDECAR_RC=0" in out, out
    assert "invalid" not in out, out
