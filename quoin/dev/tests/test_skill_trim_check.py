"""Fixture-based tests for the dev-only SKILL.md trim invariant checker."""
import ast
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "skill_trim_check.py"
SUBDIR = "quoin/adapters/claude/skills"

BASE = (
    "---\nname: demo\ndescription: \"A demo\"\n---\n"
    "# Demo\n"
    "Portable intent doc: `quoin/core/skills/demo.md`\n"
    "\n"
    "## §0 Model dispatch\n"
    "dispatch body\n"
    "<!-- §0-end -->\n"
    "\n"
    "## Steps\n"
    "Run `tool --flag` first.\n"
    "This sentence only restates what the harness already does.\n"
    "Keep this rule. See `__QUOIN_HOME__/memory/guide.md` \"Alpha\" for more.\n"
    "\n"
    "```bash\n"
    "echo hello\n"
    "```\n"
    "<!-- keep-marker -->\n"
    "\n"
    "## §V Reconcile\n"
    "<!-- §V-reconcile-begin -->\n"
    "verify\n"
    "<!-- §V-reconcile-end -->\n"
    "\n"
    "## Tail\n"
    "tail text\n"
)


def _git(root, *args):
    subprocess.run(["git", "-C", str(root)] + list(args), check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
                        "HOME": str(root)})


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    skill = root / SUBDIR / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(BASE, encoding="utf-8")
    mem = root / "quoin" / "memory"
    mem.mkdir(parents=True)
    (mem / "guide.md").write_text("# Guide\n## Alpha\ntext\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root


def _run(root, *extra):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--git-root", str(root), "--ref", "HEAD", "--skills-subdir", SUBDIR] + list(extra),
        capture_output=True, text=True)


def _write(root, text):
    (root / SUBDIR / "demo" / "SKILL.md").write_text(text, encoding="utf-8")


def test_untouched_tree_reports_no_changed_skills(repo):
    proc = _run(repo)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "no changed skills" in proc.stdout


def test_pure_prose_deletion_passes(repo):
    _write(repo, BASE.replace("This sentence only restates what the harness already does.\n", ""))
    proc = _run(repo)
    assert proc.returncode == 0, proc.stdout
    assert "removed" in proc.stdout


def test_dropped_inline_code_is_reported_not_failed(repo):
    _write(repo, BASE.replace("Run `tool --flag` first.\n", "Run the tool first.\n"))
    proc = _run(repo)
    assert proc.returncode == 0, proc.stdout
    assert "dropped-inline-code: `tool --flag`" in proc.stdout


MUTATIONS = {
    "frontmatter": lambda t: t.replace('description: "A demo"', 'description: "Other"'),
    "generated-span": lambda t: t.replace("dispatch body", "dispatch body edited"),
    "section-v-heading": lambda t: t.replace("## §V Reconcile", "## §V Reconcile (read-only)"),
    "overhang": lambda t: t.replace("<!-- §0-end -->\n\n", "<!-- §0-end -->\nnote\n\n"),
    "heading-removed": lambda t: t.replace("## Tail\ntail text\n", "tail text\n"),
    "fence-removed": lambda t: t.replace("```bash\necho hello\n```\n", ""),
    "marker-removed": lambda t: t.replace("<!-- keep-marker -->\n", ""),
    "added-must": lambda t: t.replace("Keep this rule.", "You MUST keep this rule."),
    "core-pointer": lambda t: t.replace("`quoin/core/skills/demo.md`", "the intent doc"),
    "missing-memory-file": lambda t: t.replace("memory/guide.md\" \"Alpha\"", "memory/guide.md\" \"Alpha\"").replace(
        "See `__QUOIN_HOME__/memory/guide.md` \"Alpha\" for more.",
        "See `__QUOIN_HOME__/memory/guide.md` \"Alpha\" and `__QUOIN_HOME__/memory/absent.md` \"X\"."),
    "missing-memory-heading": lambda t: t.replace(
        "See `__QUOIN_HOME__/memory/guide.md` \"Alpha\" for more.",
        "See `__QUOIN_HOME__/memory/guide.md` \"Nope\" for more."),
}


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_each_hard_invariant_fails_on_its_own_mutation(repo, name):
    mutated = MUTATIONS[name](BASE)
    assert mutated != BASE
    _write(repo, mutated)
    proc = _run(repo)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "FAIL demo:" in proc.stdout


def test_heading_removal_passes_only_with_allowance(repo):
    _write(repo, BASE.replace("## Tail\ntail text\n", "tail text\n"))
    assert _run(repo).returncode == 1
    assert _run(repo, "--allow-heading-removal", "Tail").returncode == 0


CONTENT_BASE = BASE.replace("<!-- §0-end -->\n\n", "<!-- §0-end -->\n\n### Anchor line\nfirst tail\nsecond tail\n\n")


def _commit_content_base(repo):
    _write(repo, CONTENT_BASE)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "content overhang")


def test_content_overhang_may_change_when_first_line_stays(repo):
    _commit_content_base(repo)
    _write(repo, CONTENT_BASE.replace("second tail\n", "shorter\n"))
    proc = _run(repo)
    assert proc.returncode == 0, proc.stdout


def test_content_overhang_first_line_change_fails(repo):
    _commit_content_base(repo)
    _write(repo, CONTENT_BASE.replace("### Anchor line", "### Other line"))
    proc = _run(repo)
    assert proc.returncode == 1, proc.stdout
    assert "FAIL demo:" in proc.stdout


def test_content_overhang_turning_blank_fails(repo):
    _commit_content_base(repo)
    _write(repo, BASE)
    proc = _run(repo)
    assert proc.returncode == 1, proc.stdout


def test_content_overhang_losing_its_marker_fails(repo):
    _commit_content_base(repo)
    _write(repo, CONTENT_BASE.replace("<!-- §0-end -->\n", ""))
    proc = _run(repo)
    assert proc.returncode == 1, proc.stdout
    assert "overhang" in proc.stdout


def test_blank_overhang_cannot_change(repo):
    _write(repo, BASE.replace("<!-- §0-end -->\n\n", "<!-- §0-end -->\n\n\n"))
    proc = _run(repo)
    assert proc.returncode == 1, proc.stdout


def test_unknown_ref_is_usage_error(repo):
    proc = subprocess.run([sys.executable, str(SCRIPT), "--git-root", str(repo), "--ref", "nope",
                           "--skills-subdir", SUBDIR], capture_output=True, text=True)
    assert proc.returncode == 2


def test_script_parses_as_python_38_grammar():
    ast.parse(SCRIPT.read_text(encoding="utf-8"), feature_version=(3, 8))

