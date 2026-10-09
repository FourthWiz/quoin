"""The OpenCode task may change the shared affected-test mapper by data rows only.

An existing repository guard requires a mapper row for every shipped adapter file, so the
mapper gains rows that map an OpenCode adapter file to an OpenCode test. Nothing else that
the Claude install deploys may differ from the base branch. The guard applies on the task
branch only (see ``_opencode_task_scope``).
"""
from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from _opencode_task_scope import task_guard_active

TESTS_DIR = Path(__file__).resolve().parent
LIST_PATH = TESTS_DIR / "opencode_changed_tests.json"
MAPPER = "quoin/core/scripts/affected_tests.py"
TABLE_NAME = "_DOCS_TO_TESTS"
RECORDED_BASE = "7671eb76"
ADDED_LINE_PATTERNS = (
    re.compile(r"^\s*\($"),
    re.compile(r"^\s*\),?$"),
    re.compile(r'^\s*"quoin/adapters/opencode/[^"]+",$'),
    re.compile(r'^\s*"quoin/dev/tests/test_opencode_[a-z0-9_]+\.py",$'),
)
OTHER_DEPLOYED = (
    "quoin/QUICKSTART.md",
    "quoin/plugins",
    "quoin/tools/agentdesk",
    "quoin/install.sh",
    "src/quoin/installer.py",
    "quoin/adapters/claude",
)


def _git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=False
    )


def _git_root():
    if shutil.which("git") is None:
        return None
    result = _git(TESTS_DIR, "rev-parse", "--show-toplevel")
    return Path(result.stdout.strip()) if result.returncode == 0 else None


def deployed_subdirs(installer_source):
    """Names in the installer's ``_QUOIN_DEPLOYED_SUBDIRS`` assignment, from a literal only."""
    tree = ast.parse(installer_source)
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
            value = node.value
        else:
            continue
        if "_QUOIN_DEPLOYED_SUBDIRS" not in targets:
            continue
        if isinstance(value, ast.Call):
            func = value.func
            if not (isinstance(func, ast.Name) and func.id in ("frozenset", "set")):
                raise AssertionError("unsupported call in _QUOIN_DEPLOYED_SUBDIRS")
            if len(value.args) != 1 or value.keywords:
                raise AssertionError("unsupported arguments in _QUOIN_DEPLOYED_SUBDIRS")
            value = value.args[0]
        if not isinstance(value, (ast.Set, ast.List, ast.Tuple)):
            raise AssertionError("_QUOIN_DEPLOYED_SUBDIRS is not a literal collection")
        names = []
        for element in value.elts:
            if not (isinstance(element, ast.Constant) and isinstance(element.value, str)):
                raise AssertionError("non-string element in _QUOIN_DEPLOYED_SUBDIRS")
            names.append(element.value)
        return sorted(names)
    raise AssertionError("no _QUOIN_DEPLOYED_SUBDIRS assignment")


def pathspec(root, installer_source):
    paths = ["quoin/%s" % name for name in deployed_subdirs(installer_source)]
    paths.extend(OTHER_DEPLOYED)
    claude_files = sorted(
        "quoin/%s" % item.name for item in (root / "quoin").glob("CLAUDE*.md")
    )
    paths.extend(claude_files)
    return sorted(set(paths))


def _table_bounds(head_text):
    lines = head_text.splitlines()
    start = next(
        (i + 1 for i, line in enumerate(lines) if line.startswith(TABLE_NAME)), None
    )
    assert start is not None, "no %s assignment in the base copy" % TABLE_NAME
    end = next((i + 1 for i in range(start, len(lines)) if lines[i].startswith(")")), None)
    assert end is not None, "no closing line for %s" % TABLE_NAME
    return start, end


def mapper_violations(root, base, path=MAPPER):
    """Reasons the mapper diff against ``base`` is more than allowed OpenCode rows."""
    head = _git(root, "show", "%s:%s" % (base, path))
    assert head.returncode == 0, head.stderr
    start, _old_end = _table_bounds(head.stdout)
    current = (Path(root) / path).read_text(encoding="utf-8")
    cur_start, cur_end = _table_bounds(current)
    diff = _git(root, "diff", "-U0", base, "--", path)
    assert diff.returncode == 0, diff.stderr
    problems = []
    new_line = 0
    for line in diff.stdout.splitlines():
        hunk = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
        if hunk:
            new_line = int(hunk.group(1))
            if not (cur_start < new_line < cur_end):
                problems.append("hunk outside %s: %s" % (TABLE_NAME, line))
            continue
        if line.startswith(("---", "+++", "diff ", "index ")):
            continue
        if line.startswith("-"):
            problems.append("removed line: %s" % line)
        elif line.startswith("+"):
            body = line[1:]
            if not any(p.match(body) for p in ADDED_LINE_PATTERNS):
                problems.append("added line is not an OpenCode row: %s" % line)
    return problems


def _active_root():
    root = _git_root()
    if root is None:
        pytest.skip("no git checkout")
    data = json.loads(LIST_PATH.read_text(encoding="utf-8"))
    reason = task_guard_active(root, data["task_branch"])
    if reason is not None:
        pytest.skip(reason)
    return root


def _base(root):
    for ref in ("origin/main", "main"):
        result = _git(root, "merge-base", "HEAD", ref)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    result = _git(root, "rev-parse", "--verify", RECORDED_BASE)
    if result.returncode == 0:
        return result.stdout.strip()
    pytest.skip("no base branch available")


def test_the_installed_claude_files_differ_only_by_opencode_mapper_rows():
    root = _active_root()
    base = _base(root)
    installer = (root / "src" / "quoin" / "installer.py").read_text(encoding="utf-8")
    spec = pathspec(root, installer)
    result = _git(root, "diff", "--name-only", base, "--", *spec)
    assert result.returncode == 0, result.stderr
    changed = [line for line in result.stdout.splitlines() if line]
    assert set(changed) <= {MAPPER}, changed
    if changed:
        assert mapper_violations(root, base) == []


def test_the_derived_pathspec_covers_every_deployed_source():
    root = _git_root()
    if root is None:
        pytest.skip("no git checkout")
    installer = (root / "src" / "quoin" / "installer.py").read_text(encoding="utf-8")
    spec = pathspec(root, installer)
    for expected in (
        "quoin/skills",
        "quoin/scripts",
        "quoin/core",
        "quoin/memory",
        "quoin/hooks",
        "quoin/QUICKSTART.md",
        "quoin/plugins",
        "quoin/tools/agentdesk",
        "quoin/CLAUDE.md",
        "quoin/CLAUDE.slim.md",
    ):
        assert expected in spec


def test_the_subdir_parser_shapes():
    assert deployed_subdirs('_QUOIN_DEPLOYED_SUBDIRS = frozenset({"a", "b"})\n') == ["a", "b"]
    assert deployed_subdirs('_QUOIN_DEPLOYED_SUBDIRS = {"b", "a"}\n') == ["a", "b"]
    with pytest.raises(AssertionError):
        deployed_subdirs("_QUOIN_DEPLOYED_SUBDIRS = frozenset(SOME_NAME)\n")
    with pytest.raises(AssertionError):
        deployed_subdirs("OTHER = 1\n")
    with pytest.raises(AssertionError):
        deployed_subdirs("_QUOIN_DEPLOYED_SUBDIRS = frozenset({1})\n")


TABLE_TEXT = (
    "_DOCS_TO_TESTS = (\n"
    "    (\n"
    '        "quoin/skills/a.md",\n'
    '        "quoin/dev/tests/test_a.py",\n'
    "    ),\n"
    ")\n"
)


def _fixture_repo(tmp_path, new_text):
    if shutil.which("git") is None:
        pytest.skip("git is not available")
    repo = tmp_path / "repo"
    (repo / "quoin/core/scripts").mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    target = repo / MAPPER
    target.write_text("PRE = 1\n" + TABLE_TEXT + "POST = 2\n", encoding="utf-8")
    _git(repo, "add", "-A")
    assert _git(repo, "commit", "-q", "-m", "base").returncode == 0
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    target.write_text(new_text, encoding="utf-8")
    return repo, base


def test_an_added_opencode_row_passes(tmp_path):
    row = (
        "    (\n"
        '        "quoin/adapters/opencode/fixtures/x.json",\n'
        '        "quoin/dev/tests/test_opencode_runtime_config.py",\n'
        "    ),\n"
    )
    text = "PRE = 1\n" + TABLE_TEXT.replace(")\n", row + ")\n", 1) + "POST = 2\n"
    repo, base = _fixture_repo(tmp_path, text)
    assert mapper_violations(repo, base) == []


def test_a_removed_line_fails(tmp_path):
    text = "PRE = 1\n" + TABLE_TEXT.replace('        "quoin/skills/a.md",\n', "") + "POST = 2\n"
    repo, base = _fixture_repo(tmp_path, text)
    assert any("removed line" in p for p in mapper_violations(repo, base))


def test_an_added_non_opencode_row_fails(tmp_path):
    row = (
        "    (\n"
        '        "quoin/skills/b.md",\n'
        '        "quoin/dev/tests/test_b.py",\n'
        "    ),\n"
    )
    text = "PRE = 1\n" + TABLE_TEXT.replace(")\n", row + ")\n", 1) + "POST = 2\n"
    repo, base = _fixture_repo(tmp_path, text)
    assert any("not an OpenCode row" in p for p in mapper_violations(repo, base))


def test_a_hunk_outside_the_table_fails(tmp_path):
    text = "PRE = 1\n" + TABLE_TEXT + "POST = 2\n" + '    "quoin/adapters/opencode/z",\n'
    repo, base = _fixture_repo(tmp_path, text)
    assert any("hunk outside" in p for p in mapper_violations(repo, base))
